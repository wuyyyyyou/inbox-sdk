"""Ask pipeline answer stage: filter → context → answer LLM → guard.

run_ask_pipeline() is the orchestrator: plan → search → filter → context → answer → guard.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any

from ..core.pipeline import _EXECUTION_SYSTEM_PROMPT
from ..domain.types import MessageLite
from .planner import AskPlan

_logger = logging.getLogger(__name__)
_BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# 匹配时过滤，≤此值跳过
_SKIP_FILTER_THRESHOLD = 10


# ── Filter ─────────────────────────────────────────────────────────────

# 中文注释：候选数较少时跳过过滤 LLM，避免多消耗一次 sampling 调用。
_FILTER_SYSTEM_PROMPT = """You are Anna's relevance filter. For each email header, answer one question:
"Is this email relevant to the user's request?"

A relevant email helps answer the user's question. If in doubt, mark it relevant — the next stage will do deeper analysis.

## Output format
Output a single JSON object. First character MUST be `{`.
{"items": [{"i": <index>, "relevant": true|false, "reason": "brief reason"}]}

Include EVERY email in the items array. Set "relevant": false for emails to exclude.
Omitted items default to "relevant": true (pass through)."""

_FILTER_USER_TEMPLATE = """## User request
{user_request}

## What to look for
{relevance_hint}

## Emails ({count} total)
{headers}"""


async def _filter_candidates(
    messages: list[MessageLite],
    plan: AskPlan,
    *,
    sampling_create_message: Any = None,
) -> list[MessageLite]:
    """Filter messages through a lightweight relevance LLM call.

    Skips the LLM call entirely if there are ≤_SKIP_FILTER_THRESHOLD messages.
    On LLM failure, all messages pass through unfiltered.
    """
    if len(messages) <= _SKIP_FILTER_THRESHOLD:
        return list(messages)

    from ..core.phase1 import _compact_header
    from ..llm_runtime.service import call_llm_json_safe

    # Merge relevance_hints from all topics
    hints = [t.get("relevance_hint", "") for t in plan.topics if t.get("relevance_hint")]
    relevance_hint = "; ".join(hints) if hints else "Find emails relevant to the user's request"

    headers_json = json.dumps(
        [_compact_header(m, i) for i, m in enumerate(messages)],
        ensure_ascii=False,
    )

    user_message = _FILTER_USER_TEMPLATE.format(
        user_request=plan.user_request,
        relevance_hint=relevance_hint,
        count=len(messages),
        headers=headers_json,
    )

    try:
        result = await call_llm_json_safe(
            sampling_create_message,
            system_prompt=_FILTER_SYSTEM_PROMPT,
            user_message=user_message,
            fallback={"items": []},
            temperature=0.1,
            max_tokens=4096,
            timeout=120.0,
            metadata={"tool": "ask_filter"},
            allow_fallback=True,
            allow_sampling_provider_fallback=True,
            max_attempts=2 if sampling_create_message is not None else None,
        )
    except Exception:
        _logger.warning("Ask filter LLM failed, passing all %d messages through", len(messages))
        return list(messages)

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    items = payload.get("items") if isinstance(payload.get("items"), list) else []

    # Build set of indices to exclude
    exclude_indices: set[int] = set()
    for item in items:
        if isinstance(item, dict) and not item.get("relevant", True):
            try:
                exclude_indices.add(int(item.get("i", -1)))
            except (ValueError, TypeError):
                pass

    filtered = [m for i, m in enumerate(messages) if i not in exclude_indices]
    if len(filtered) < len(messages):
        _logger.info("Ask filter: %d → %d relevant", len(messages), len(filtered))
    return filtered


# ── Context reader ─────────────────────────────────────────────────────

def _fmt_ts(epoch_ms: str) -> str:
    if not epoch_ms:
        return ""
    try:
        dt = datetime.fromtimestamp(int(epoch_ms) / 1000.0, tz=_BEIJING_TZ)
        return dt.strftime("%b %d, %Y, %H:%M")
    except (ValueError, TypeError, OSError):
        return str(epoch_ms)[:20]


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

    enriched: list[dict[str, Any]] = []
    for idx, msg in enumerate(candidates, 1):
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

        # Read body
        try:
            detail = get_message_detail(source_mailbox, msg.message_id)
            if detail:
                entry["body"] = (getattr(detail, "body_text", "") or "")[:4000]
        except Exception:
            pass

        # Read thread context
        try:
            thread_ctx = get_thread_context(source_mailbox, msg.thread_id or msg.message_id)
            if thread_ctx and thread_ctx.messages:
                entry["thread"] = []
                for tm in thread_ctx.messages[:10]:
                    entry["thread"].append({
                        "from": getattr(tm, "from_addr", "") or "",
                        "to": getattr(tm, "to_addr", "") or "",
                        "subject": getattr(tm, "subject", "") or "",
                        "date": _fmt_ts(getattr(tm, "internal_date", "") or ""),
                        "body": (getattr(tm, "body_text", "") or "")[:3000],
                    })
        except Exception:
            entry["thread"] = []

        if progress_callback and idx % 5 == 0:
            progress_callback("read_context", {"current": idx, "total": len(candidates)})

        enriched.append(entry)

    return enriched


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
            f"- If the emails below do not contain what the user is looking for, say so honestly."
        )

    variants = (
        [
            {"name": "full", "body_limit": 4000, "thread_body_limit": 2000, "max_thread_messages": 20},
            {"name": "compact", "body_limit": 1600, "thread_body_limit": 900, "max_thread_messages": 8},
            {"name": "short", "body_limit": 800, "thread_body_limit": 500, "max_thread_messages": 5},
            {"name": "headers", "body_limit": 0, "thread_body_limit": 0, "max_thread_messages": 0},
        ]
        if sampling_create_message is not None
        else [{"name": "full", "body_limit": 4000, "thread_body_limit": 2000, "max_thread_messages": 20}]
    )

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
                system_prompt=_EXECUTION_SYSTEM_PROMPT,
                user_message=_build_user_prompt(rendered),
                fallback={"title": "Scan failed", "summary": "Unable to analyze emails.", "sections": []},
                temperature=0.2,
                max_tokens=20480,
                timeout=180.0,
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
        return {
            "title": plan.title or "Scan incomplete",
            "summary": f"Anna could not produce a usable answer. {last_error[:240]}",
            "sections": [],
        }

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    if not payload:
        payload = {"title": plan.title or "Scan complete", "summary": "No analysis produced.", "sections": []}
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
    sampling_create_message: Any = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """Full Ask pipeline: plan → search → filter → context → answer → guard.

    Supports multiple mailboxes: plan once, search+filter concurrently per mailbox,
    merge candidates, then single answer pass.

    If `plan` is provided, skips the Planner LLM and uses the given plan directly
    (for re-running saved plans without re-planning).
    """
    from .planner import plan_ask_request
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

    if progress_callback:
        progress_callback("plan_done", {
            "title": plan.title, "goal": plan.goal,
            "direction": plan.direction, "timeframe": plan.timeframe,
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
            progress_callback("search", {"mailbox": mbox, "query_total": len(queries)})

        messages = await execute_search(mbox, queries, progress_callback=progress_callback)
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
            progress_callback("search_done", {
                "mailbox": mbox, "scanned": len(messages),
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
        return {
            "title": plan.title or "No results",
            "summary": f"Scanned {total_scanned} emails across {len(mailboxes)} mailbox(es) but none matched your request.",
            "sections": [],
        }

    if progress_callback:
        progress_callback("filter_done", {"candidates": len(all_candidates), "scanned": total_scanned})

    # ── 4. Context ──────────────────────────────────────────────────
    enriched = await _read_candidate_context(
        all_candidates, primary_mailbox,
        mailbox_by_message_id=candidate_mailboxes,
        sampling_create_message=sampling_create_message,
        progress_callback=progress_callback,
    )

    # ── 5. Answer LLM ───────────────────────────────────────────────
    if progress_callback:
        progress_callback("answer", {"candidates": len(enriched)})
    result = await _generate_answer(plan, enriched, primary_mailbox,
                                     sampling_create_message=sampling_create_message,
                                     progress_callback=progress_callback)

    # ── 6. Guard ────────────────────────────────────────────────────
    valid_ids = {c.message_id or "" for c in all_candidates if c.message_id}
    valid_thread_ids = {c.thread_id or "" for c in all_candidates if c.thread_id}
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
