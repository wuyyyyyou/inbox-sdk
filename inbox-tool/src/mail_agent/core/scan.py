"""扫描计划构建器和邮件扫描器。

使用真实 Gmail API 进行邮件搜索和获取。
设计文档 §10。

主要流程：
  build_scan_plan() → 根据策略和用户请求构建扫描计划
  run_mail_scan()  → 分页拉取 Gmail threads，每页 50 个，返回 MessageLite 列表
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from ..planning.strategies import get as get_strategy
from ..domain.types import MailStrategy, MailTaskPlan, MessageLite, ScanPolicy, ScanBudget

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
_logger = logging.getLogger(__name__)


# ── 扫描计划构建 ─────────────────────────────────────────────────

def _quote_or_term(term: str) -> str:
    term = term.strip()
    if not term:
        return ""
    if term.startswith('"') and term.endswith('"'):
        return term
    if " " in term and ":" not in term:
        return '"' + term.replace('"', "") + '"'
    return term


def _normalize_gmail_query(query: str) -> str:
    """把常见但 Gmail API 容易拒绝的查询写法改成兼容语法。"""
    q = str(query or "").strip()
    if not q:
        return q
    q = re.sub(r"\bin:draft\b", "in:drafts", q, flags=re.IGNORECASE)

    def replace_or_group(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not re.search(r"\bOR\b", inner, flags=re.IGNORECASE):
            return match.group(0)
        terms = [_quote_or_term(part) for part in re.split(r"\s+OR\s+", inner, flags=re.IGNORECASE)]
        terms = [term for term in terms if term]
        return "{" + " ".join(terms) + "}" if terms else ""

    previous = ""
    while previous != q:
        previous = q
        q = re.sub(r"\(([^()]*\bOR\b[^()]*)\)", replace_or_group, q, flags=re.IGNORECASE)

    if re.search(r"\bOR\b", q, flags=re.IGNORECASE) and ":" not in q and "{" not in q:
        terms = [_quote_or_term(part) for part in re.split(r"\s+OR\s+", q, flags=re.IGNORECASE)]
        terms = [term for term in terms if term]
        q = "{" + " ".join(terms) + "}" if terms else q

    return re.sub(r"\s+", " ", q).strip()


def build_scan_plan(task_plan: MailTaskPlan, strategy: MailStrategy) -> dict[str, Any]:
    """从任务计划和策略配置构建扫描计划。

    返回的 dict 包含：
      strategy_mode: 策略标识
      queries: 策略默认查询 + 用户关键词查询（如有）
      budget: 扫描资源预算限制
    """
    sp = strategy.scan_policy
    queries = _apply_user_scope_to_queries(sp.default_queries, task_plan.scope)

    return {
        "strategy_mode": strategy.id,
        "queries": queries,
        "budget": sp.budget,
    }


def _apply_user_scope_to_queries(
    queries: list[dict[str, Any]],
    user_scope: dict[str, Any],
) -> list[dict[str, Any]]:
    """将用户请求中提取的关键词追加为额外的 Gmail 查询。

    如果用户说"帮我找YouTube合作的邮件"，extract_keywords 提取了 ["YouTube"]，
    则追加一个 "YouTube" 查询。
    """
    if not user_scope:
        return queries

    result = list(queries)
    extra_keywords = user_scope.get("keywords") or []
    if extra_keywords:
        kw_str = " OR ".join(extra_keywords)
        result.append({
            "query": kw_str,
            "purpose": "user_keywords",
            "max_results": 50,
            "priority": "high",
        })

    return result


# ── 邮件扫描器（Thread-based, 50 threads per invoke）─────────────────

async def run_mail_scan(
    mailbox: str,
    max_threads: int = 100,
    *,
    progress_callback: Any = None,
) -> list[MessageLite]:
    """Paginate Gmail threads newest-first, 50 per page, until max_threads unique threads.

    Each page of 50 threads = one Gmail API call = one invoke-safe unit.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ..mail_providers.gmail.adapter import (
        _clear_aps_cache_errors,
        _normalize_message,
        extract_messages_from_thread,
        fetch_thread_full,
        get_aps_cache_errors,
        list_messages,
        list_threads_page,
        normalize_mailbox,
        _to_message_lite,
        write_message,
    )
    _clear_aps_cache_errors()

    normalized = normalize_mailbox(mailbox)
    all_messages: list[MessageLite] = []
    seen_threads: set[str] = set()
    page_token: str | None = None
    gmail_errors: list[str] = []
    fallback_used = False
    _errors_lock = threading.Lock()

    while len(all_messages) < max_threads:
        # 1. List one page of threads (50 per call = 1 invoke)
        try:
            page = list_threads_page(normalized, page_token=page_token)
        except Exception as exc:
            # Fall back to local cache if Gmail API is unreachable
            fallback_used = True
            _logger.warning("threads.list failed, falling back to cache: %s", exc)
            cached = list_messages(normalized)
            for m in cached:
                if isinstance(m, dict) and m.get("id"):
                    try:
                        all_messages.append(_to_message_lite(m))
                    except Exception:
                        pass
                if len(all_messages) >= max_threads:
                    break
            break

        thread_refs = page.get("threads") if isinstance(page, dict) else []
        if not thread_refs:
            break

        thread_ids: list[str] = []
        for t in thread_refs:
            if isinstance(t, dict) and t.get("id"):
                tid = str(t["id"])
                if tid not in seen_threads:
                    thread_ids.append(tid)
                    seen_threads.add(tid)

        if not thread_ids:
            page_token = page.get("nextPageToken")
            if not page_token:
                break
            continue

        # 2. Fetch full thread details concurrently within this page
        def _fetch_one_thread(tid: str) -> list[dict[str, Any]]:
            try:
                full = fetch_thread_full(normalized, tid)
                raw_msgs = extract_messages_from_thread(full)
                normalized_msgs: list[dict[str, Any]] = []
                for msg in raw_msgs:
                    try:
                        n = _normalize_message(normalized, msg)
                        write_message(normalized, n)
                        normalized_msgs.append(n)
                    except Exception:
                        pass
                return normalized_msgs
            except Exception as exc:
                with _errors_lock:
                    gmail_errors.append(f"fetch_thread({tid}): {exc}")
                return []

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(_fetch_one_thread, tid): tid for tid in thread_ids}
            for future in as_completed(futures):
                msgs = future.result()
                for msg in msgs:
                    try:
                        all_messages.append(_to_message_lite(msg))
                    except Exception:
                        pass
                if len(all_messages) >= max_threads:
                    break

        if progress_callback:
            progress_callback("scan", {
                "threads_fetched": len(all_messages),
                "max_threads": max_threads,
                "page_size": len(thread_ids),
            })

        page_token = page.get("nextPageToken")
        if not page_token:
            break

    # Persist index for future cache fallback
    if all_messages:
        from ..mail_providers.gmail.adapter import write_index, read_message
        try:
            summaries: list[dict[str, Any]] = []
            for lite in all_messages:
                try:
                    msg = read_message(normalized, lite.message_id)
                    summaries.append({
                        "id": msg.get("id", lite.message_id),
                        "thread_id": msg.get("thread_id", lite.thread_id),
                        "internal_date": str(msg.get("internal_date") or lite.internal_date or ""),
                        "from_addr": msg.get("from", lite.from_addr) or "",
                        "subject": msg.get("subject", lite.subject) or "",
                        "snippet": msg.get("snippet", lite.snippet) or "",
                    })
                except Exception:
                    pass
            if summaries:
                write_index(normalized, summaries)
        except Exception:
            pass

    if fallback_used:
        if progress_callback:
            progress_callback("scan_fallback", {
                "gmail_api_failed": bool(gmail_errors),
                "cached_count": len(all_messages),
                "errors": gmail_errors[-3:],
            })
    if not all_messages and gmail_errors and not fallback_used:
        _logger.error("Gmail API failed: %s", gmail_errors[:3])
        if progress_callback:
            progress_callback("scan_fallback_empty", {
                "gmail_api_failed": True,
                "cached_count": 0,
                "errors": gmail_errors[:3],
                "hint": "Check Gmail token and network",
            })

    return all_messages[:max_threads]
