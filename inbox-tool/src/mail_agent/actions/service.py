"""Handle Panel service — thread summary, draft reply, and revise draft LLM calls.

§1.2 of PRD-V2: when the user clicks "Handle" on an attention card, the panel
shows thread metadata, latest email, thread summary (on-demand LLM), and
draft reply (LLM-generated, user-editable).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from ..domain.types import CandidateItem, MailboxProfile, MailStrategy
from ..storage.types import PersistentCard


# ── Thread context fetch ────────────────────────────────────────────

_ON_WROTE_RE = re.compile(r"^\s*On\s+(.{1,700}?)\s+wrote:\s*$", re.IGNORECASE)
_CHINESE_WROTE_RE = re.compile(r"^\s*(?:在\s*)?.{1,700}?写道[:：]\s*$")
_ORIGINAL_MESSAGE_RE = re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE)
_FORWARDED_MESSAGE_RE = re.compile(r"^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE)
_OUTLOOK_FROM_RE = re.compile(r"^\s*From:\s+.+", re.IGNORECASE)
_OUTLOOK_SENT_RE = re.compile(r"^\s*Sent:\s+.+", re.IGNORECASE)
_QUOTE_DATE_OR_ADDR_RE = re.compile(
    r"(@|<[^>]+@[^>]+>|\b\d{4}\b|\b\d{1,2}:\d{2}\b|"
    r"\b(?:mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
    r"january|february|march|april|june|july|august|september|october|november|december)\b)",
    re.IGNORECASE,
)


def _looks_like_quote_header(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _ORIGINAL_MESSAGE_RE.match(stripped) or _FORWARDED_MESSAGE_RE.match(stripped):
        return True
    if _CHINESE_WROTE_RE.match(stripped):
        return stripped.startswith("在") or bool(_QUOTE_DATE_OR_ADDR_RE.search(stripped))
    match = _ON_WROTE_RE.match(stripped)
    if not match:
        return False
    # Avoid cutting normal prose such as "On the proposal, Alice wrote:".
    return bool(_QUOTE_DATE_OR_ADDR_RE.search(match.group(1)))


def _looks_like_outlook_header(lines: list[str], index: int) -> bool:
    if not _OUTLOOK_FROM_RE.match(lines[index].strip()):
        return False
    lookahead = lines[index + 1:index + 4]
    return any(_OUTLOOK_SENT_RE.match(item.strip()) for item in lookahead)


def _strip_quoted_reply(text: str) -> str:
    """Keep the newly written part of an email body for thread-history display."""
    if not text:
        return ""

    kept: list[str] = []
    seen_content = False
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    for index, line in enumerate(lines):
        stripped = line.strip()
        if _looks_like_quote_header(stripped) or _looks_like_outlook_header(lines, index):
            break

        if stripped:
            seen_content = True
        kept.append(line.rstrip())

    cleaned = "\n".join(kept).strip()
    # 部分客户端把引用正文序列化为每行的 >> 前缀，而不是 Gmail 的 > 前缀。
    cleaned = re.sub(r"(?m)^[ \t]*>{2,}[ \t]?", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


_THREAD_CONTEXT_PAGE_SIZE = 5
_THREAD_CONTEXT_MAX_PAGE_SIZE = 50


def _clamp_thread_page_limit(value: int | None) -> int:
    try:
        raw = int(value or _THREAD_CONTEXT_PAGE_SIZE)
    except Exception:
        raw = _THREAD_CONTEXT_PAGE_SIZE
    return max(1, min(_THREAD_CONTEXT_MAX_PAGE_SIZE, raw))


def _summary_time(summary: dict[str, Any]) -> int:
    try:
        return int(summary.get("internal_date") or 0)
    except Exception:
        return 0


def _thread_summaries(mailbox: str, thread_id: str) -> list[dict[str, Any]]:
    from ..mail_providers.gmail.adapter import list_messages

    summaries = [
        item for item in list_messages(mailbox)
        if str(item.get("thread_id") or "") == str(thread_id or "")
    ]
    summaries.sort(key=_summary_time)
    return summaries


def _message_from_summary(mailbox: str, summary: dict[str, Any]) -> Any:
    from ..mail_providers.gmail.adapter import get_message_detail

    message_id = str(summary.get("id") or "")
    if not message_id:
        return None
    try:
        return get_message_detail(mailbox, message_id)
    except Exception:
        return None


def _serialize_thread_message(message: Any, summary: dict[str, Any]) -> dict[str, Any]:
    if message:
        return {
            "message_id": message.message_id,
            "from": message.from_addr,
            "to": message.to_addr,
            "cc": message.cc or "",
            "date": message.internal_date,
            "subject": message.subject,
            "body": _strip_quoted_reply(message.body_text or "")[:2000],
        }
    return {
        "message_id": str(summary.get("id") or ""),
        "from": str(summary.get("from") or ""),
        "to": str(summary.get("to") or ""),
        "cc": str(summary.get("cc") or ""),
        "date": str(summary.get("internal_date") or ""),
        "subject": str(summary.get("subject") or ""),
        "body": "",
    }


def _fetch_thread_context_sync(
    mailbox: str,
    card: PersistentCard,
    *,
    before_index: int | None = None,
    page_limit: int = _THREAD_CONTEXT_PAGE_SIZE,
    preview_edges: bool = False,
) -> dict[str, Any]:
    """Fetch one bounded page of thread messages for the Handle Panel."""
    try:
        summaries = _thread_summaries(mailbox, card.thread_id)
    except Exception:
        summaries = []

    if not summaries:
        return {
            "thread_id": card.thread_id,
            "message_count": 0,
            "from": card.original.from_addr,
            "to": card.original.to_addr,
            "cc": "",
            "subject": card.original.thread,
            "latest_time": card.original.time,
            "messages": [],
            "returned_count": 0,
            "has_more_messages": False,
            "next_before_index": None,
        }

    total = len(summaries)
    latest = summaries[-1]
    if preview_edges and before_index is None:
        page = [summaries[0], summaries[-1]] if total > 1 else [summaries[0]]
        messages = [
            _serialize_thread_message(_message_from_summary(mailbox, summary), summary)
            for summary in page
        ]
        return {
            "thread_id": card.thread_id,
            "message_count": total,
            "from": str(latest.get("from") or ""),
            "to": str(latest.get("to") or ""),
            "cc": str(latest.get("cc") or ""),
            "subject": card.original.thread,
            "latest_time": str(latest.get("internal_date") or ""),
            "messages": messages,
            "returned_count": len(messages),
            "has_more_messages": total > 2,
            "next_before_index": total - 1 if total > 2 else None,
        }

    limit = _clamp_thread_page_limit(page_limit)
    try:
        end_index = int(before_index) if before_index is not None else total
    except Exception:
        end_index = total
    end_index = max(0, min(total, end_index))
    start_index = max(0, end_index - limit)
    page = summaries[start_index:end_index]
    messages = [
        _serialize_thread_message(_message_from_summary(mailbox, summary), summary)
        for summary in page
    ]
    return {
        "thread_id": card.thread_id,
        "message_count": total,
        "from": str(latest.get("from") or ""),
        "to": str(latest.get("to") or ""),
        "cc": str(latest.get("cc") or ""),
        "subject": card.original.thread,
        "latest_time": str(latest.get("internal_date") or ""),
        "messages": messages,
        "returned_count": len(messages),
        "has_more_messages": start_index > 0,
        "next_before_index": start_index if start_index > 0 else None,
    }


# ── Thread summary ──────────────────────────────────────────────────

THREAD_SUMMARY_SYSTEM = """You are Anna's thread context summarizer. Help the user decide how to handle the email without repeating the card copy.

Output JSON only:
{
  "thread_kind": "single_short | single_normal | multi_thread | long_thread",
  "headline": "the main point in a short phrase",
  "what_happened": ["one brief outcome, only for multi-message threads"],
  "open_questions": ["one explicit unresolved point, if any"],
  "reply_focus": "the most important reply action, not a repeat of headline",
  "related_context": ["one relevant contact-memory fact, if useful"],
  "should_show": true,
  "confidence": "high | medium | low"
}

Rules:
- The combined human-readable text in headline, what_happened, open_questions, reply_focus, and related_context MUST be 30 words or fewer. JSON keys and enum values do not count.
- Count whitespace-delimited words before responding; shorten or omit lower-priority fields until the total is at most 30 words.
- Prefer one short phrase per field and leave optional arrays empty when they add no essential information.
- Do not repeat the same fact across fields.
- For single-message threads, do not explain current progress.
- For short single-message threads, only summarize if contact memory adds useful context.
- Keep every sentence short and factual.
- Do not invent details not present in the thread or contact memory.
CRITICAL: Time format: NEVER use relative time words. ALWAYS use "Mon DD, YYYY" format. Examples: "May 28, 2026", "Jan 3, 2026". If time of day matters, append it: "May 28, 2026, 2:30 PM"."""


def build_thread_summary_prompt(thread_context: dict[str, Any], card: PersistentCard, contact_context_text: str, thread_kind: str) -> str:
    """构建 thread summary 提示词，按线程复杂度控制摘要形态。"""
    messages_text = ""
    for i, m in enumerate(thread_context.get("messages", []), start=1):
        messages_text += f"\n--- Message {i} ---\n"
        messages_text += f"From: {m.get('from', '')}\n"
        messages_text += f"Date: {m.get('date', '')}\n"
        messages_text += f"Body: {m.get('body', '')[:800]}\n"

    return f"""Card context:
Title: {card.title}
Summary: {card.summary}
Recommendation: {card.recommendation}

Thread kind: {thread_kind}
Thread has {thread_context.get('message_count', 0)} messages. Latest {len(thread_context.get('messages', []))} shown below:
{messages_text}

Relevant contact memory:
{contact_context_text}

Return the compact context JSON."""


def classify_thread_kind(thread_context: dict[str, Any]) -> str:
    """根据消息数量和正文长度给 summary 选择展示形态。"""
    messages = thread_context.get("messages", []) if isinstance(thread_context.get("messages"), list) else []
    count = int(thread_context.get("message_count") or len(messages) or 0)
    latest_body = str(messages[-1].get("body", "") if messages else "")
    if count <= 1:
        return "single_short" if len(latest_body.strip()) <= 280 else "single_normal"
    return "long_thread" if count >= 6 or sum(len(str(m.get("body", ""))) for m in messages) > 2500 else "multi_thread"


def _summary_max_tokens(thread_kind: str) -> int:
    """按线程复杂度限制摘要输出长度。"""
    return {"single_normal": 256, "multi_thread": 512, "long_thread": 768}.get(thread_kind, 256)


def _summary_thread_context(thread_ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_count": thread_ctx.get("message_count", 0),
        "latest_from": thread_ctx.get("from", ""),
        "latest_time": thread_ctx.get("latest_time", ""),
    }


def _contact_context_payload(contact_context: Any) -> dict[str, Any]:
    from dataclasses import asdict
    try:
        return asdict(contact_context)
    except Exception:
        return {}


def _related_context_lines(contact_context: Any) -> list[str]:
    lines: list[str] = []
    for topic in getattr(contact_context, "relevant_topics", []) or []:
        text = " | ".join(part for part in [getattr(topic, "title", ""), getattr(topic, "summary", ""), getattr(topic, "open_loop", "")] if part)
        if text and not _looks_like_summary_payload(text):
            lines.append(text[:240])
    return _dedupe_lines(lines)[:3]


def normalize_thread_summary_payload(payload: dict[str, Any], thread_kind: str, related_context: list[str], card: PersistentCard) -> dict[str, Any]:
    """清理 LLM 输出，避免重复和过长。"""
    headline = _limit_line(str(payload.get("headline") or card.title or card.summary or ""), 180)
    what_happened = _dedupe_lines(_as_list(payload.get("what_happened")))[:3]
    open_questions = _dedupe_lines(_as_list(payload.get("open_questions")))[:3]
    reply_focus = _limit_line(str(payload.get("reply_focus") or card.recommendation or ""), 180)
    merged_related = _dedupe_lines(_as_list(payload.get("related_context")) + related_context)[:3]
    if thread_kind.startswith("single"):
        what_happened = []
    should_show = bool(payload.get("should_show", True)) and bool(headline or what_happened or open_questions or reply_focus or merged_related)
    confidence = str(payload.get("confidence") or "medium")
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"
    return {
        "thread_kind": thread_kind,
        "headline": headline,
        "what_happened": what_happened,
        "open_questions": open_questions,
        "reply_focus": reply_focus if reply_focus != headline else "",
        "related_context": merged_related,
        "should_show": should_show,
        "confidence": confidence,
    }


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _looks_like_summary_payload(value: str) -> bool:
    text = str(value or "").strip()
    if not (text.startswith("{") and text.endswith("}")):
        return False
    try:
        payload = json.loads(text)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    keys = {
        "thread_kind",
        "headline",
        "what_happened",
        "open_questions",
        "reply_focus",
        "related_context",
        "should_show",
        "confidence",
    }
    return any(key in payload for key in keys)


def _dedupe_lines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        text = _limit_line(line, 240)
        key = text.lower()
        if text and key not in seen and not _looks_like_summary_payload(text):
            seen.add(key)
            result.append(text)
    return result


def _limit_line(value: str, size: int) -> str:
    return " ".join(str(value or "").split())[:size].rstrip()


# ── Draft reply & revise (unified) ───────────────────────────────────

_DRAFT_SYSTEM = """You are Anna, writing a reply ON BEHALF OF the user.

Output a JSON object with:
- subject: reply subject line (keep original subject, add "Re: " only if needed)
- body: the draft email body (plain text)
- tone: the tone of the reply (e.g. "warm and professional", "brief confirmation")
- note: a short internal note about what you did (English, <=50 chars)

CRITICAL — Source priority (never violate):
1. User's explicit answers and current reply intent/instructions (highest priority — these are the user's current direction)
2. Original email thread content
3. Contact memory context (for tone, relationship, past context only — NOT for facts)
4. Draft preferences such as tone, style, mood, and length

- NEVER invent facts, numbers, dates, prices, commitments, or opinions
- Use the user's OWN WORDS wherever possible — keep their casual/formal level
- If the user's answers are insufficient to write a meaningful reply, say so in "note" and write a placeholder body asking the user for more details
- Be concise — reply length proportional to original message
- Do not include email headers (To, From, CC) in the body
- If the user provided current reply intent or revision instructions, apply them precisely
- If current user intent conflicts with generic politeness, contact memory, or default style preferences, follow the current user intent
- CRITICAL — Time format: NEVER use relative time words. ALWAYS use "Mon DD, YYYY" format."""


def _build_draft_prompt(
    card: PersistentCard,
    thread_context: dict[str, Any],
    reply_mode: str,
    current_draft: str = "",
    revision_input: str = "",
    contact_context_text: str = "",
    user_answers: dict[str, str] | None = None,
) -> str:
    """构建草稿生成/修改提示词，并注入联系人上下文。"""
    latest_msg = None
    msgs = thread_context.get("messages", [])
    if msgs:
        latest_msg = msgs[-1]

    has_draft = bool(current_draft.strip())
    has_instruction = bool(revision_input.strip())
    has_answers = bool(user_answers)

    parts = [
        f"Card: {card.title}",
        f"Context: {card.summary}",
        f"Action needed: {card.recommendation}",
        "",
        "Latest email:",
        f"From: {latest_msg.get('from', '') if latest_msg else card.original.from_addr}",
        f"Subject: {thread_context.get('subject', card.original.thread)}",
        f"Body: {latest_msg.get('body', '')[:1200] if latest_msg else 'Not available'}",
    ]

    if has_answers:
        parts.append("\nUser's answers (AUTHORITATIVE — base the reply on these):")
        for qid, answer in user_answers.items():
            if answer.strip():
                parts.append(f"- {answer}")

    if contact_context_text and contact_context_text != "No relevant contact memory.":
        parts.extend(["", "Relevant contact memory:", contact_context_text])

    if has_draft:
        parts.append(f"\nExisting draft:\n{current_draft}")

    if has_instruction:
        task = "Revise the existing draft" if has_draft else "Generate a new draft incorporating these instructions"
        label = "User revision request" if has_draft else "User reply intent and instructions"
        parts.append(f"\n{label}:\n{revision_input}")
    elif has_answers:
        task = "Draft a reply based on the user's answers above"
    else:
        task = "Revise the existing draft considering the thread context" if has_draft else "Draft a reply based on the thread above"

    parts.append(f"\nReply mode: {reply_mode}")
    parts.append(f"Thread has {thread_context.get('message_count', 0)} total messages.")
    parts.append(f"\n{task}. Return JSON only.")

    return "\n".join(parts)


# ── LLM call wrappers ───────────────────────────────────────────────

async def summarize_thread(
    card: PersistentCard,
    mailbox: str,
    sampling_create_message: Any = None,
) -> dict[str, Any]:
    """生成 thread 摘要，并兼容联系人记忆上下文。"""
    from ..llm_runtime.service import call_llm_json_safe
    from ..contact_memory.retriever import contact_email_from_header, format_contact_context_for_prompt, retrieve_contact_context
    from ..contact_memory.types import ContactMemoryQuery

    thread_ctx = await asyncio.to_thread(_fetch_thread_context_sync, mailbox, card)
    contact_email = contact_email_from_header(card.original.from_addr)
    contact_context = await retrieve_contact_context(ContactMemoryQuery(
        mailbox=mailbox,
        contact_email=contact_email,
        current_subject=card.original.thread or card.title,
        current_body=card.original.body,
        current_thread_id=card.thread_id,
        purpose="thread_summary",
    ), sampling_create_message=sampling_create_message)
    contact_context_text = format_contact_context_for_prompt(contact_context)
    thread_kind = classify_thread_kind(thread_ctx)
    related_context = _related_context_lines(contact_context)

    if thread_kind == "single_short" and not related_context:
        payload = {
            "thread_kind": thread_kind,
            "headline": "",
            "what_happened": [],
            "open_questions": [],
            "reply_focus": "",
            "related_context": [],
            "should_show": False,
            "confidence": "high",
        }
        return {
            "summary": payload,
            "thread_context": _summary_thread_context(thread_ctx),
            "contact_context": _contact_context_payload(contact_context),
            "fallback_used": False,
            "fallback_reason": "",
        }

    if thread_kind == "single_short":
        payload = {
            "thread_kind": thread_kind,
            "headline": "",
            "what_happened": [],
            "open_questions": [],
            "reply_focus": contact_context.reply_guidance,
            "related_context": related_context,
            "should_show": True,
            "confidence": "medium",
        }
        return {
            "summary": payload,
            "thread_context": _summary_thread_context(thread_ctx),
            "contact_context": _contact_context_payload(contact_context),
            "fallback_used": False,
            "fallback_reason": "",
        }

    prompt = build_thread_summary_prompt(thread_ctx, card, contact_context_text, thread_kind)
    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=THREAD_SUMMARY_SYSTEM,
        user_message=prompt,
        fallback={
            "thread_kind": thread_kind,
            "headline": card.title or card.summary or "Unable to summarize",
            "what_happened": [],
            "open_questions": [],
            "reply_focus": card.recommendation,
            "related_context": related_context,
            "should_show": True,
            "confidence": "low",
        },
        temperature=0.2,
        max_tokens=_summary_max_tokens(thread_kind),
        timeout=90.0,
        metadata={"tool": "summarize_thread", "card_id": card.card_id},
    )

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    payload = normalize_thread_summary_payload(payload, thread_kind, related_context, card)
    return {
        "summary": payload,
        "thread_context": _summary_thread_context(thread_ctx),
        "contact_context": _contact_context_payload(contact_context),
        "fallback_used": result.get("fallback_used", False),
        "fallback_reason": result.get("fallback_reason", ""),
    }


async def generate_draft_reply(
    card: PersistentCard,
    mailbox: str,
    reply_mode: str = "reply_to_sender",
    sampling_create_message: Any = None,
    *,
    current_draft: str = "",
    revision_input: str = "",
    user_answers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Generate or revise a draft reply. If current_draft is non-empty, revises it."""
    from ..llm_runtime.service import call_llm_json_safe
    from ..contact_memory.retriever import contact_email_from_header, format_contact_context_for_prompt, retrieve_contact_context
    from ..contact_memory.types import ContactMemoryQuery

    thread_ctx = await asyncio.to_thread(_fetch_thread_context_sync, mailbox, card)
    contact_email = contact_email_from_header(card.original.from_addr)
    contact_context = await retrieve_contact_context(ContactMemoryQuery(
        mailbox=mailbox,
        contact_email=contact_email,
        current_subject=card.original.thread or card.title,
        current_body=card.original.body,
        current_thread_id=card.thread_id,
        purpose="draft_generation",
    ), sampling_create_message=sampling_create_message)
    prompt = _build_draft_prompt(
        card,
        thread_ctx,
        reply_mode,
        current_draft,
        revision_input,
        format_contact_context_for_prompt(contact_context),
        user_answers=user_answers,
    )

    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=_DRAFT_SYSTEM,
        user_message=prompt,
        fallback={"subject": "", "body": current_draft or "", "tone": "", "note": "Draft generation failed"},
        temperature=0.3,
        max_tokens=8000,
        timeout=150.0,
        metadata={"tool": "generate_draft", "card_id": card.card_id, "reply_mode": reply_mode},
    )

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    return {
        "draft": payload,
        "reply_mode": reply_mode,
        "fallback_used": result.get("fallback_used", False),
        "fallback_reason": result.get("fallback_reason", ""),
    }


# ── Reply now ────────────────────────────────────────────────────────

async def reply_now(
    card: PersistentCard,
    mailbox: str,
    draft_body: str,
    reply_mode: str = "reply_to_sender",
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Send the draft reply via Gmail API (or mock if dry_run=True).

    dry_run=True (default): 仅验证参数并返回模拟结果，不真实发送邮件。
    dry_run=False: 通过 Gmail API 真实发送。
    """
    from ..mail_providers.gmail.adapter import send_reply
    import sys

    print(f"[handle_service.reply_now] mailbox={mailbox} thread_id={card.thread_id} to={card.original.from_addr} dry_run={dry_run} body_len={len(draft_body)}", file=sys.stderr)

    if not draft_body.strip():
        return {"ok": False, "error": "Draft body is empty", "dry_run": dry_run}

    if dry_run:
        print(f"[handle_service.reply_now] dry_run=True, returning mock result", file=sys.stderr)
        return {
            "ok": True,
            "dry_run": True,
            "message": "Mock: reply was NOT actually sent.",
            "detail": {
                "to": card.original.from_addr,
                "thread": card.original.thread,
                "reply_mode": reply_mode,
                "body_preview": draft_body[:200],
            },
        }

    try:
        result = await asyncio.to_thread(
            send_reply,
            mailbox=mailbox,
            thread_id=card.thread_id,
            to_addr=card.original.from_addr,
            body=draft_body,
            reply_mode=reply_mode,
        )
        return {"ok": True, "dry_run": False, "result": result}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "dry_run": False}
