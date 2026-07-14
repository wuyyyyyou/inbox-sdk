"""Ask pipeline answer stage: filter → context → answer LLM → guard.

run_ask_pipeline() is the orchestrator: plan → search → filter → context → answer → guard.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any

from ..domain.types import MessageLite
from .planner import AskPlan
from .sampling_budget import ASK_ANSWER_MAX_TOKENS

_logger = logging.getLogger(__name__)
_BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# 中文注释：反向 JSON-RPC 不能使用文件传输；筛选请求需远低于 512 KiB 协议帧上限。
# 中文注释：全部命中用于扫描统计，只有排序靠前的有限候选才能读取正文和线程。
_MAX_CONTEXT_CANDIDATES = 8
# 中文注释：正文/线程 Gmail 读限并发，避免 8 封串行放大墙钟时间。
_CONTEXT_READ_CONCURRENCY = 4
# 中文注释：即使有必要阅读片段，回答也必须综合事实，避免退化为邮件正文复制。
_ASK_SYNTHESIS_INSTRUCTION = (
    "Summarize and synthesize the evidence in your own words. "
    "Do not copy email body verbatim, except for a short necessary quote or an exact subject."
)
_ASK_ANSWER_SYSTEM_PROMPT = """You are Anna, an executive email assistant. Return one valid JSON object only, with no markdown or analysis.
Schema: {"title": string, "summary": string, "sections": [{"heading": string, "body": string?, "items": [{"subject": string?, "context": string?, "suggestion": string?, "draft": string?, "mailbox": string?, "message_id": string?, "thread_id": string?, "from": string?, "mail_links": array?, "reply_gaps": object?}]}]}.
Use only the supplied email evidence. Keep the answer concise, factual, and in the user's language. A reply draft and reply_gaps.needs_user_input cannot both appear for one item."""


def _answer_language_instruction(user_request: str) -> str:
    """Keep generated answer copy aligned with the user's language."""
    if re.search(r"[\u3400-\u9fff]", user_request):
        return (
            "Write all generated natural-language fields in Simplified Chinese. "
            "Keep email subjects, names, addresses, and quoted source text in their original language."
        )
    return (
        "Write all generated natural-language fields in the same language as the user's request. "
        "Keep email subjects, names, addresses, and quoted source text in their original language."
    )


def _uses_chinese(user_request: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", user_request))


def _answer_fallback(plan: AskPlan, detail: str = "") -> dict[str, Any]:
    # 中文注释：用户侧只展示可行动摘要；解析器/stack 细节只写日志，避免把 excerpt 甩到侧栏。
    if detail:
        _logger.warning("ask answer fallback: detail=%s", str(detail)[:300])
    if _uses_chinese(plan.user_request):
        summary = "Anna 暂时无法生成可用的回答，请换个说法重试，或打开具体邮件后再问。"
        title = plan.title or "扫描未完成"
    else:
        summary = "Anna could not produce a usable answer. Try rephrasing, or open a specific email first."
        title = plan.title or "Scan incomplete"
    return {"title": title, "summary": summary, "sections": []}


async def _filter_candidates(
    messages: list[MessageLite],
    plan: AskPlan,
    *,
    sampling_create_message: Any = None,
) -> list[MessageLite]:
    """保留全部搜索命中，相关度筛选统一由本地排序在正文读取前完成。"""
    # 中文注释：不再为筛选额外调用或重试 Sampling，避免空响应消耗预算并拖长 Answer 阶段。
    _ = plan, sampling_create_message
    return list(messages)


# ── Context reader ─────────────────────────────────────────────────────

def _fmt_ts(epoch_ms: str) -> str:
    if not epoch_ms:
        return ""
    try:
        dt = datetime.fromtimestamp(int(epoch_ms) / 1000.0, tz=_BEIJING_TZ)
        return dt.strftime("%b %d, %Y, %H:%M")
    except (ValueError, TypeError, OSError):
        return str(epoch_ms)[:20]


def _select_candidates_for_context(candidates: list[MessageLite], plan: AskPlan) -> list[MessageLite]:
    """按请求相关度排序并选择有限候选，避免将所有正文交给模型。"""
    # 中文注释：这里是确定性本地排序，不新增 Sampling 调用；关键词仅用于缩小正文读取集合。
    terms = [str(term).casefold().strip() for topic in plan.topics for term in topic.get("search_terms", [])]
    terms.extend(re.findall(r"[\w\u3400-\u9fff]{2,}", plan.user_request.casefold()))
    terms = [term for term in terms if term]

    def score(item: tuple[int, MessageLite]) -> tuple[int, int, int]:
        index, message = item
        subject = (message.subject or "").casefold()
        snippet = (message.snippet or "").casefold()
        relevance = sum(8 for term in terms if term in subject)
        relevance += sum(3 for term in terms if term in snippet)
        if getattr(message, "unread", False):
            relevance += 4
        try:
            timestamp = int(message.internal_date or 0)
        except (TypeError, ValueError):
            timestamp = 0
        return (relevance, timestamp, -index)

    ranked = sorted(enumerate(candidates), key=score, reverse=True)
    return [message for _index, message in ranked[:_MAX_CONTEXT_CANDIDATES]]


async def _read_candidate_context(
    candidates: list[MessageLite],
    mailbox: str,
    *,
    mailbox_by_message_id: dict[str, str] | None = None,
    sampling_create_message: Any = None,
    progress_callback: Any = None,
) -> list[dict[str, Any]]:
    """Read message bodies and contact memory for each candidate.

    Returns enriched candidate dicts ready for Answer LLM rendering.
    """
    from ..mail_providers.gmail.adapter import normalize_mailbox, get_message_detail, get_thread_context

    normalized = normalize_mailbox(mailbox)
    # 中文注释：即使未来有其他调用方绕过编排层，也不能读取无限量邮件正文。
    candidates = candidates[:_MAX_CONTEXT_CANDIDATES]

    # Collect unique senders for contact memory retrieval
    sender_emails: list[str] = []
    seen_senders: set[str] = set()
    for msg in candidates:
        addr = (msg.from_addr or "").strip()
        if addr and addr not in seen_senders:
            seen_senders.add(addr)
            sender_emails.append(addr)

    # Retrieve contact memory (best-effort, concurrent)
    contact_contexts: dict[str, str] = {}
    try:
        from ..contact_memory.retriever import retrieve_contact_context, format_contact_context_for_prompt
        from ..contact_memory.types import ContactMemoryQuery

        async def _fetch_one(addr: str) -> tuple[str, str]:
            try:
                ctx = await retrieve_contact_context(ContactMemoryQuery(
                    mailbox=normalized,
                    contact_email=addr,
                    purpose="thread_summary",
                ), sampling_create_message)
                return (addr, format_contact_context_for_prompt(ctx))
            except Exception:
                return (addr, "")

        import asyncio as _asyncio
        results = await _asyncio.gather(*[_fetch_one(a) for a in sender_emails[:5]])
        for addr, ctx_text in results:
            contact_contexts[addr] = ctx_text
    except ImportError:
        pass

    import asyncio as _asyncio

    # 中文注释：Gmail adapter 为同步 I/O，放入线程池并行读取，用信号量限制并发。
    semaphore = _asyncio.Semaphore(_CONTEXT_READ_CONCURRENCY)
    total = len(candidates)

    async def _read_one(idx: int, msg: MessageLite) -> dict[str, Any]:
        source_mailbox = (mailbox_by_message_id or {}).get(msg.message_id or "", mailbox)
        source_mailbox = normalize_mailbox(source_mailbox)
        entry: dict[str, Any] = {
            "mailbox": source_mailbox,
            "message_id": msg.message_id or "",
            "thread_id": msg.thread_id or "",
            "from": msg.from_addr or "",
            "to": msg.to_addr or "",
            "subject": msg.subject or "",
            "date": _fmt_ts(msg.internal_date or ""),
            "snippet": msg.snippet or "",
            "unread": getattr(msg, "unread", False),
            "label_ids": getattr(msg, "label_ids", None) or [],
            "body": "",
            "thread": [],
            "contact_context": contact_contexts.get(msg.from_addr or "", ""),
        }
        async with semaphore:
            try:
                detail = await _asyncio.to_thread(get_message_detail, source_mailbox, msg.message_id)
                if detail:
                    entry["body"] = (getattr(detail, "body_text", "") or "")[:1200]
            except Exception:
                pass
            try:
                thread_ctx = await _asyncio.to_thread(
                    get_thread_context, source_mailbox, msg.thread_id or msg.message_id
                )
                if thread_ctx and thread_ctx.messages:
                    entry["thread"] = []
                    for tm in thread_ctx.messages[:3]:
                        entry["thread"].append({
                            "from": getattr(tm, "from_addr", "") or "",
                            "to": getattr(tm, "to_addr", "") or "",
                            "subject": getattr(tm, "subject", "") or "",
                            "date": _fmt_ts(getattr(tm, "internal_date", "") or ""),
                            "body": (getattr(tm, "body_text", "") or "")[:400],
                        })
            except Exception:
                entry["thread"] = []
        if progress_callback and (idx % 2 == 0 or idx == total):
            progress_callback("read_context", {"current": idx, "total": total})
        return entry

    return list(await _asyncio.gather(*[
        _read_one(idx, msg) for idx, msg in enumerate(candidates, 1)
    ]))


# ── Answer LLM ─────────────────────────────────────────────────────────
# _EXECUTION_SYSTEM_PROMPT imported from core.pipeline at module top


def _render_candidates_for_llm(
    enriched: list[dict[str, Any]],
    *,
    body_limit: int = 4000,
    thread_body_limit: int = 2000,
    max_thread_messages: int = 20,
) -> str:
    """Render enriched candidates as compact text for the Answer LLM."""
    parts: list[str] = []
    for i, e in enumerate(enriched, 1):
        unread_label = " (UNREAD)" if e.get("unread") else ""
        labels = [str(l) for l in (e.get("label_ids") or []) if str(l) not in ("UNREAD",)]
        labels_str = f"  Labels: {', '.join(labels)}" if labels else ""
        parts.append(
            f"### Email {i}\n"
            f"From: {e.get('from', '')}\n"
            f"Subject: {e.get('subject', '')}{unread_label}\n"
            f"Date: {e.get('date', '')}\n"
            f"Mailbox: {e.get('mailbox', '')}\n"
            f"Thread ID: {e.get('thread_id', '')}\n"
            f"Message ID: {e.get('message_id', '')}{labels_str}"
        )
        if e.get("body") and body_limit > 0:
            parts.append(f"Snippet: {e.get('snippet', '')}")
            parts.append(f"Body:\n{e['body'][:body_limit]}")
        else:
            parts.append(f"Snippet: {e.get('snippet', '')}")
        if e.get("thread") and thread_body_limit > 0 and max_thread_messages > 0:
            thread_msgs = e["thread"][:max_thread_messages]
            parts.append(f"\nThread history ({len(thread_msgs)} messages):")
            for tm in thread_msgs:
                parts.append(
                    f"  [{tm.get('date', '')}] {tm.get('from', '')}: "
                    f"{tm.get('subject', '')}\n"
                    f"    {tm.get('body', '')[:thread_body_limit]}"
                )
        # Contact context
        if e.get("contact_context"):
            parts.append(f"Contact context: {e['contact_context']}")
        parts.append("")
    return "\n".join(parts)


async def _generate_answer(
    plan: AskPlan,
    enriched: list[dict[str, Any]],
    mailbox: str,
    *,
    sampling_create_message: Any = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """Run the Answer LLM on filtered, context-enriched candidates.

    Progressive truncation fallback: full → compact → short → headers.
    """
    from ..llm_runtime.service import call_llm_json_safe

    # Build the stable part of the user prompt (doesn't change between variants)
    def _build_user_prompt(rendered: str) -> str:
        return (
            f"## Your Identity\n"
            f"You are Anna, executive assistant to {mailbox}.\n"
            f"In all output text, address your principal directly as 'you' / 'your'.\n"
            f"Match by EMAIL ADDRESS (between < >), not by display name.\n\n"
            f"## User request\n"
            f"{plan.user_request}\n\n"
            f"## Response language\n"
            f"{_answer_language_instruction(plan.user_request)}\n\n"
            f"## Task\n"
            f"{plan.task_prompt}\n\n"
            f"## Two-phase reply generation\n"
            f"For EVERY item that needs a reply, decide between two paths:\n"
            f"PATH A — You have enough context → write the draft in the 'draft' field.\n"
            f"PATH B — You need user clarification → OMIT 'draft', set reply_gaps.needs_user_input=true "
            f"with specific questions. The user will answer, and a draft will be generated later.\n"
            f"CRITICAL: Never include both draft AND reply_gaps.needs_user_input on the same item.\n"
            f"When in doubt, choose Path B. A bad guess is worse than asking.\n\n"
            f"## Structured mail references\n"
            f"When an item cites one or more provided emails, include mail_links (maximum 5):\n"
            f"[{{\"label\": \"exact email subject\", \"mailbox\": \"provided mailbox\", "
            f"\"thread_id\": \"provided thread id\", \"message_id\": \"provided message id\"}}]\n"
            f"Use only IDs and subjects shown below. Never emit href, URLs, or invented references.\n"
            f"For a single-email item, also include its mailbox, thread_id, and message_id fields.\n\n"
            f"## Relevant emails ({len(enriched)} total)\n"
            f"{rendered}\n\n"
            f"## Important\n"
            f"- Base your answer ONLY on the emails provided below.\n"
            f"- {_ASK_SYNTHESIS_INSTRUCTION}\n"
            f"- If the emails below do not contain what the user is looking for, say so honestly."
        )

    # 中文注释：Anna invoke 只尝试一次紧凑回答；失败直接返回安全 fallback，不能再消耗多轮 token。
    variants = [{"name": "compact", "body_limit": 700, "thread_body_limit": 240, "max_thread_messages": 2}]

    result: dict[str, Any] | None = None
    last_error = ""
    for variant in variants:
        rendered = _render_candidates_for_llm(
            enriched,
            body_limit=int(variant["body_limit"]),
            thread_body_limit=int(variant["thread_body_limit"]),
            max_thread_messages=int(variant["max_thread_messages"]),
        )
        try:
            result = await call_llm_json_safe(
                sampling_create_message,
                system_prompt=_ASK_ANSWER_SYSTEM_PROMPT,
                user_message=_build_user_prompt(rendered),
                fallback={"title": "Scan failed", "summary": "Unable to analyze emails.", "sections": []},
                temperature=0.2,
                max_tokens=ASK_ANSWER_MAX_TOKENS,
                timeout=60.0,
                metadata={"tool": "ask_answer", "email_count": str(len(enriched)), "variant": variant["name"]},
                allow_fallback=sampling_create_message is None,
                allow_sampling_provider_fallback=True,
                max_attempts=1 if sampling_create_message is not None else None,
            )
            break
        except Exception as exc:
            last_error = str(exc)
            if progress_callback:
                progress_callback("evaluate", {"variant": variant["name"], "reason": last_error[:200]})

    if result is None:
        # 中文注释：不再把本地邮件列表伪装成 AI 回答；失败时返回明确错误摘要。
        return _answer_fallback(plan, last_error or "Anna sampling failed")

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    if not payload:
        return _answer_fallback(plan, "Anna returned empty analysis")
    payload["llm_meta"] = {
        "provider": result.get("provider"),
        "model": result.get("model"),
        "usage": result.get("usage"),
    }
    return payload


# ── Guard ───────────────────────────────────────────────────────────────

_FORBIDDEN_ACTIONS: list[str] = ["send", "delete", "forward", "unsubscribe"]


def _normalize_reference_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _mail_link_from_source(message_id: str, source: dict[str, str]) -> dict[str, str]:
    return {
        "label": str(source.get("subject") or "(no subject)")[:120],
        "mailbox": str(source.get("mailbox") or ""),
        "thread_id": str(source.get("thread_id") or ""),
        "message_id": message_id,
        "from": str(source.get("from") or "")[:240],
        "date": str(source.get("date") or "")[:80],
        "snippet": str(source.get("snippet") or "")[:240],
    }


def _apply_guard(
    result: dict[str, Any],
    valid_ids: set[str],
    valid_thread_ids: set[str],
    valid_sources: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Apply safety guards to the Answer LLM output.

    1. Block forbidden action language in suggestions and drafts
    2. Strip message_id / thread_id references that don't exist in source emails
    """
    sections = result.get("sections")
    if not isinstance(sections, list):
        return result

    for section in sections:
        if not isinstance(section, dict):
            continue
        items = section.get("items")
        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue

            # Guard 1: flag (don't destroy) forbidden action language.
            # "send" can appear in legitimate advice ("you should send a reply")
            # vs. automated action ("I'll send the reply for you"). We can't
            # reliably distinguish these with regex, so we flag instead of block.
            for field in ("suggestion", "draft"):
                text = str(item.get(field, ""))
                if not text:
                    continue
                lowered = text.lower()
                for action in _FORBIDDEN_ACTIONS:
                    pattern = r"\b" + re.escape(action) + r"\b"
                    if re.search(pattern, lowered):
                        item["_guard_warning"] = f"contains '{action}' — review before acting"
                        _logger.warning("Ask guard flagged '%s' in %s", action, field)
                        break

            # Guard 2: validate message_id / thread_id references
            mid = str(item.get("message_id", ""))
            if mid and mid not in valid_ids:
                item["message_id"] = ""
                _logger.warning("Ask guard stripped invalid message_id: %s", mid[:40])

            tid = str(item.get("thread_id", ""))
            if tid and tid not in valid_thread_ids:
                item["thread_id"] = ""
                _logger.warning("Ask guard stripped invalid thread_id: %s", tid[:40])

            sources = valid_sources or {}
            source = sources.get(mid)
            item_source_is_valid = False
            if source:
                expected_thread = source.get("thread_id", "")
                if tid and tid != expected_thread:
                    item["thread_id"] = ""
                else:
                    item_source_is_valid = True
                    item["thread_id"] = expected_thread
                    item["mailbox"] = source.get("mailbox", "")
                    item["subject"] = source.get("subject", item.get("subject", ""))
                    item["from"] = source.get("from", item.get("from", ""))

            raw_links = item.get("mail_links")
            safe_links: list[dict[str, str]] = []
            seen_threads: set[tuple[str, str]] = set()

            def add_source_link(link_mid: str, link_source: dict[str, str]) -> None:
                if len(safe_links) >= 5:
                    return
                link_mailbox = str(link_source.get("mailbox") or "").strip().lower()
                link_tid = str(link_source.get("thread_id") or "").strip()
                if not link_mid or not link_mailbox or not link_tid:
                    return
                thread_key = (link_mailbox, link_tid)
                if thread_key in seen_threads:
                    return
                seen_threads.add(thread_key)
                safe_links.append(_mail_link_from_source(link_mid, link_source))

            if isinstance(raw_links, list):
                for raw_link in raw_links:
                    if not isinstance(raw_link, dict):
                        continue
                    link_mid = str(raw_link.get("message_id") or "").strip()
                    link_tid = str(raw_link.get("thread_id") or "").strip()
                    link_mailbox = str(raw_link.get("mailbox") or "").strip().lower()
                    link_source = sources.get(link_mid)
                    if not link_source:
                        continue
                    if link_tid != link_source.get("thread_id") or link_mailbox != link_source.get("mailbox", "").lower():
                        continue
                    add_source_link(link_mid, link_source)

            # 中文注释：模型可能漏掉 mail_links；条目自身的合法 ID 仍应确定性补成链接。
            if source and item_source_is_valid:
                add_source_link(mid, source)

            # 中文注释：若模型连 ID 也漏掉，只接受结果文本中完整出现的候选邮件主题，避免模糊匹配误跳转。
            reference_text = _normalize_reference_text(" ".join(
                str(item.get(field) or "") for field in ("subject", "context", "suggestion", "draft")
            ))
            item_subject = _normalize_reference_text(item.get("subject"))
            for source_mid, candidate_source in sources.items():
                candidate_subject = _normalize_reference_text(candidate_source.get("subject"))
                if not candidate_subject:
                    continue
                exact_subject_mentioned = candidate_subject in reference_text
                if len(candidate_subject) < 6:
                    exact_subject_mentioned = item_subject == candidate_subject
                if exact_subject_mentioned:
                    add_source_link(source_mid, candidate_source)

            item["mail_links"] = safe_links

    return result


# ── Orchestrator ────────────────────────────────────────────────────────

async def run_ask_pipeline(
    user_request: str = "",
    mailboxes: list[str] | None = None,
    *,
    plan: AskPlan | None = None,
    scan_window_days: int | None = None,
    max_messages: int | None = None,
    sampling_create_message: Any = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """Full Ask pipeline: plan → search → filter → context → answer → guard.

    Supports multiple mailboxes: plan once, search+filter concurrently per mailbox,
    merge candidates, then single answer pass.

    If `plan` is provided, skips the Planner LLM and uses the given plan directly
    (for re-running saved plans without re-planning).
    """
    from .planner import plan_ask_request, resolve_effective_timeframe
    from .search import build_queries, execute_search

    if not mailboxes:
        return {"title": "Error", "summary": "No mailbox selected.", "sections": []}

    primary_mailbox = mailboxes[0]

    # ── 1. Plan (once, skipped if plan provided) ───────────────────
    if plan is None:
        if not user_request:
            return {"title": "Error", "summary": "No user request provided.", "sections": []}
        if progress_callback:
            progress_callback("plan", {"stage": "plan"})
        plan = await plan_ask_request(user_request, primary_mailbox, sampling_create_message=sampling_create_message)

    # 中文注释：模型计划只能决定检索意图，默认扫描时间必须服从用户当前 Scan Plan。
    plan.timeframe = resolve_effective_timeframe(plan.user_request or user_request, scan_window_days, plan.timeframe)

    timeframe_match = re.fullmatch(r"(\d{1,3})d", plan.timeframe)
    timeframe_days = int(timeframe_match.group(1)) if timeframe_match else 0
    if progress_callback:
        progress_callback("plan_done", {
            "title": plan.title, "goal": plan.goal,
            "direction": plan.direction, "timeframe": plan.timeframe,
            "scan_window_days": timeframe_days,
        })

    # ── 2. Search + filter per mailbox (concurrent) ──────────────────
    import asyncio

    primary_queries: list[dict[str, Any]] = []
    all_sources: list[dict[str, str]] = []

    async def _search_one(mbox: str) -> tuple[str, list[MessageLite], list[MessageLite]]:
        """Search + filter for a single mailbox. Returns (mailbox, all_messages, candidates)."""
        nonlocal primary_queries
        queries = await build_queries(plan, mbox)
        if not primary_queries and mbox == primary_mailbox:
            primary_queries = queries
        if progress_callback:
            progress_callback("search", {
                "mailbox": mbox,
                "query_total": len(queries),
                "scan_window_days": timeframe_days,
            })

        # 中文注释：多邮箱并发时最多 broaden 1 次，避免 0 结果时 Gmail 调用成倍放大。
        messages = await execute_search(
            mbox,
            queries,
            progress_callback=progress_callback,
            max_broaden_attempts=1 if len(mailboxes) > 1 else 2,
            max_messages=max_messages if max_messages is not None else 200,
        )
        if not messages:
            return (mbox, [], [])

        # Capture source previews for frontend progress display
        for msg in messages[:8]:
            all_sources.append({
                "subject": getattr(msg, "subject", "") or "",
                "from": getattr(msg, "from_addr", "") or "",
                "date": _fmt_ts(getattr(msg, "internal_date", "") or ""),
                "thread_id": getattr(msg, "thread_id", "") or "",
            })

        if progress_callback:
            thread_count = len({message.thread_id for message in messages if message.thread_id})
            progress_callback("search_done", {
                "mailbox": mbox, "scanned": len(messages), "threads": thread_count,
                "scan_window_days": timeframe_days,
                "partial": {"sources": all_sources[-8:]},
            })

        filtered = await _filter_candidates(messages, plan, sampling_create_message=sampling_create_message)
        return (mbox, messages, filtered)

    per_mailbox = await asyncio.gather(*[_search_one(m) for m in mailboxes])

    # ── 3. Merge candidates ─────────────────────────────────────────
    all_candidates: list[MessageLite] = []
    candidate_mailboxes: dict[str, str] = {}
    seen_ids: set[str] = set()
    total_scanned = 0

    for _mbox, messages, candidates in per_mailbox:
        total_scanned += len(messages)
        for c in candidates:
            cid = (c.message_id or "", c.thread_id or "")
            if cid not in seen_ids:
                seen_ids.add(cid)
                all_candidates.append(c)
                if c.message_id:
                    candidate_mailboxes[c.message_id] = _mbox

    if not all_candidates:
        if _uses_chinese(plan.user_request):
            return {
                "title": plan.title or "未找到结果",
                "summary": f"已扫描 {len(mailboxes)} 个邮箱中的 {total_scanned} 封邮件，但没有找到符合你要求的内容。",
                "sections": [],
            }
        return {
            "title": plan.title or "No results",
            "summary": f"Scanned {total_scanned} emails across {len(mailboxes)} mailbox(es) but none matched your request.",
            "sections": [],
        }

    if progress_callback:
        progress_callback("filter_done", {"candidates": len(all_candidates), "scanned": total_scanned})

    # ── 4. Context ──────────────────────────────────────────────────
    context_candidates = _select_candidates_for_context(all_candidates, plan)
    if progress_callback:
        # 中文注释：只展示安全数量，明确告知用户模型仅打开必要的有限上下文。
        progress_callback("read_context", {"current": 0, "total": len(context_candidates)})
    enriched = await _read_candidate_context(
        context_candidates, primary_mailbox,
        mailbox_by_message_id=candidate_mailboxes,
        # 中文注释：联系人记忆会对每个联系人再发起 LLM 选择；Ask Session 每轮只允许规划与回答两次调用。
        sampling_create_message=None,
        progress_callback=progress_callback,
    )
    if progress_callback:
        progress_callback("read_context_done", {"total": len(enriched)})

    # ── 5. Answer LLM ───────────────────────────────────────────────
    if progress_callback:
        progress_callback("answer", {"candidates": len(enriched)})
    result = await _generate_answer(plan, enriched, primary_mailbox,
                                     sampling_create_message=sampling_create_message,
                                     progress_callback=progress_callback)

    # ── 6. Guard ────────────────────────────────────────────────────
    valid_ids = {c.message_id or "" for c in context_candidates if c.message_id}
    valid_thread_ids = {c.thread_id or "" for c in context_candidates if c.thread_id}
    valid_sources = {
        str(entry.get("message_id") or ""): {
            "mailbox": str(entry.get("mailbox") or ""),
            "thread_id": str(entry.get("thread_id") or ""),
            "subject": str(entry.get("subject") or ""),
            "from": str(entry.get("from") or ""),
            "date": str(entry.get("date") or ""),
            "snippet": str(entry.get("snippet") or ""),
        }
        for entry in enriched
        if entry.get("message_id")
    }
    result = _apply_guard(result, valid_ids, valid_thread_ids, valid_sources)

    result.setdefault("plan_id", plan.plan_id)
    result.setdefault("plan_title", plan.title)
    result.setdefault("plan_description", plan.description)
    result.setdefault("plan_timeframe", plan.timeframe)
    result.setdefault("plan_direction", plan.direction)
    result.setdefault("plan_goal", plan.goal)
    result.setdefault("plan_gmail_flags", plan.gmail_flags)
    result.setdefault("plan_topics", plan.topics)
    result.setdefault("plan_queries", primary_queries)
    result.setdefault("planner_llm", plan.llm_meta)
    result.setdefault("messages_scanned", total_scanned)
    result.setdefault("candidates_found", len(all_candidates))

    return result


# ── Ask item draft generation ─────────────────────────────────────────

_ASK_DRAFT_SYSTEM = """You are Anna, an executive email assistant. Generate a professional, concise email reply. Use the user's answers to the clarifying questions to fill in the details they provided. Do NOT make up information beyond what the user told you."""


async def generate_ask_item_draft(
    message_id: str,
    thread_id: str,
    mailbox: str,
    from_addr: str,
    subject: str,
    user_answers: dict[str, str],
    *,
    sampling_create_message: Any = None,
) -> dict[str, Any]:
    """Generate a draft reply for an Ask result item, incorporating user answers.

    Reads the original email body from Gmail cache, then calls the LLM
    with the user's answers to produce a draft.
    """
    from ..llm_runtime.service import call_llm_json_safe
    from ..mail_providers.gmail.adapter import normalize_mailbox, get_message_detail

    normalized = normalize_mailbox(mailbox)

    # Read original email body
    body_text = ""
    try:
        detail = get_message_detail(normalized, message_id)
        if detail:
            body_text = (getattr(detail, "body_text", "") or "")[:3000]
    except Exception:
        pass

    # Format user answers
    answers_text = "\n".join(f"Q: {q}\nA: {a}" for q, a in user_answers.items() if a.strip())

    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=_ASK_DRAFT_SYSTEM,
        user_message=(
            f"## Original email\n"
            f"From: {from_addr}\n"
            f"Subject: {subject}\n\n"
            f"{body_text}\n\n"
            f"## User's answers to clarifying questions\n"
            f"{answers_text}\n\n"
            f"## Instructions\n"
            f"Write a professional reply email that incorporates the user's answers above. "
            f"Do NOT add information the user didn't provide. "
            f"Output JSON: {{\"subject\": \"Reply subject\", \"body\": \"Reply body text\"}}"
        ),
        fallback={"subject": "", "body": "", "note": "Draft generation failed"},
        temperature=0.3,
        max_tokens=4096,
        timeout=120.0,
        metadata={"tool": "ask_draft", "message_id": message_id},
        allow_fallback=True,
        allow_sampling_provider_fallback=True,
    )

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    return {
        "subject": str(payload.get("subject") or ""),
        "body": str(payload.get("body") or ""),
        "fallback_used": result.get("fallback_used", False),
    }
