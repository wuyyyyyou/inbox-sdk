"""扫描计划构建器和邮件扫描器。

使用真实 Gmail API 进行邮件搜索和获取。
设计文档 §10。

主要流程：
  build_scan_plan() → 根据策略和用户请求构建扫描计划
  run_mail_scan()  → 执行扫描计划，获取并缓存邮件，返回 MessageLite 列表
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


# ── 邮件扫描器（使用 Gmail API）───────────────────────────────────

async def run_mail_scan(
    mailbox: str,
    scan_plan: dict[str, Any],
    *,
    progress_callback: Any = None,
) -> list[MessageLite]:
    """使用真实 Gmail API 执行邮件扫描。

    对扫描计划中的每条查询：
    1. 调用 Gmail API users.messages.list 搜索邮件
    2. 获取新邮件并缓存到本地
    3. 返回去重后的 MessageLite 列表

    参数：
        mailbox: 邮箱地址
        scan_plan: build_scan_plan 返回的扫描计划

    返回：
        MessageLite 列表，按 internalDate 排序
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ..mail_providers.gmail.adapter import (
        cache_debug_info,
        get_messages_lite_async,
        live_search_and_cache,
        list_messages,
        normalize_mailbox,
    )

    normalized_mailbox = normalize_mailbox(mailbox)
    budget = scan_plan.get("budget", {})
    max_messages = int(budget.get("max_messages", 250))

    queries = scan_plan.get("queries", [])

    # Phase 1: concurrent Gmail search across all queries
    gmail_errors: list[str] = []
    fallback_used = False
    _errors_lock = threading.Lock()

    def _run_one_query(q: dict[str, Any]) -> tuple[list[str], str]:
        nonlocal fallback_used
        query = _normalize_gmail_query(str(q.get("query", "")))
        max_results = min(int(q.get("max_results", 100)), 500)
        stop_at = str(q.get("stop_at_internal_date") or "")
        try:
            ids = live_search_and_cache(normalized_mailbox, query, max_results, stop_at_internal_date=stop_at)
            return ids, ""
        except Exception as exc:
            fallback_used = True
            err_msg = f"Gmail API failed for query \"{query}\": {exc}"
            _logger.warning("fallback to cache: %s", err_msg)
            with _errors_lock:
                gmail_errors.append(err_msg)
            all_cached = list_messages(normalized_mailbox)
            ids = [str(m.get("id")) for m in all_cached if m.get("id")]
            return ids[:max_results], str(exc)

    matched_id_set: set[str] = set()
    matched_ids: list[str] = []
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        futures = {pool.submit(_run_one_query, q): q for q in queries}
        for future in as_completed(futures):
            if len(matched_ids) >= max_messages:
                break
            ids, _err = future.result()
            for msg_id in ids:
                if len(matched_ids) >= max_messages:
                    break
                if msg_id not in matched_id_set:
                    matched_id_set.add(msg_id)
                    matched_ids.append(msg_id)

    # 从缓存中将消息 ID 转换为 MessageLite 对象
    limited_ids = matched_ids[:max_messages]
    messages = await get_messages_lite_async(normalized_mailbox, limited_ids)
    if progress_callback:
        progress_callback("scan_cache", {
            "matched_ids": len(limited_ids),
            "lite_count": len(messages),
            "cache": cache_debug_info(normalized_mailbox),
            "partial": {
                "brief_debug": {
                    "gmail_cache": cache_debug_info(normalized_mailbox),
                    "matched_ids": len(limited_ids),
                    "lite_count": len(messages),
                }
            },
        })

    if fallback_used:
        _logger.warning("Gmail API 不可用，回退到本地缓存：%d 封缓存邮件", len(messages))
        if progress_callback:
            progress_callback("scan_fallback", {
                "gmail_api_failed": True,
                "cached_count": len(messages),
                "errors": gmail_errors[-3:],  # 最多返回最后3条错误
            })

    if fallback_used and not messages:
        _logger.error(
            "Gmail API 不可用且本地缓存为空，无法获取任何邮件。请检查网络连接和 token 是否有效。\n错误详情: %s",
            "\n".join(gmail_errors[-3:]),
        )
        if progress_callback:
            progress_callback("scan_fallback_empty", {
                "gmail_api_failed": True,
                "cached_count": 0,
                "errors": gmail_errors[-3:],
                "hint": "请检查网络连接和 Gmail token 是否有效",
            })

    return messages
