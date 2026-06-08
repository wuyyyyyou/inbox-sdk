"""Handle Panel service — thread summary, draft reply, and revise draft LLM calls.

§1.2 of PRD-V2: when the user clicks "Handle" on an attention card, the panel
shows thread metadata, latest email, thread summary (on-demand LLM), and
draft reply (LLM-generated, user-editable).
"""

from __future__ import annotations

import json
from typing import Any

from ..domain.types import CandidateItem, MailboxProfile, MailStrategy
from ..storage.types import PersistentCard


# ── Thread context fetch ────────────────────────────────────────────

def _fetch_thread_context_sync(
    mailbox: str,
    card: PersistentCard,
) -> dict[str, Any]:
    """Fetch full thread messages for a card (sync, using cached Gmail data).

    Returns structured thread data for the Handle Panel header.
    """
    from ..mail_providers.gmail.adapter import get_thread_context

    try:
        thread = get_thread_context(mailbox, card.thread_id, max_messages=10)
        messages = thread.messages if thread else []
    except Exception:
        messages = []

    if not messages:
        return {
            "thread_id": card.thread_id,
            "message_count": 0,
            "from": card.original.from_addr,
            "to": card.original.to_addr,
            "cc": "",
            "subject": card.original.thread,
            "latest_time": card.original.time,
            "messages": [],
        }

    latest = messages[-1]
    return {
        "thread_id": card.thread_id,
        "message_count": len(messages),
        "from": latest.from_addr or "",
        "to": latest.to_addr or "",
        "cc": latest.cc or "",
        "subject": card.original.thread,
        "latest_time": latest.internal_date or "",
        "messages": [
            {
                "from": m.from_addr,
                "to": m.to_addr,
                "cc": m.cc or "",
                "date": m.internal_date,
                "subject": m.subject,
                "body": (m.body_text or "")[:2000],
            }
            for m in messages[-5:]
        ],
    }


# ── Thread summary ──────────────────────────────────────────────────

THREAD_SUMMARY_SYSTEM = """You are Anna's thread context summarizer. Help the user decide how to handle the email without repeating the card copy.

Output JSON only:
{
  "thread_kind": "single_short | single_normal | multi_thread | long_thread",
  "headline": "one concise sentence",
  "what_happened": ["only for multi-message threads"],
  "open_questions": ["explicit unresolved points only"],
  "reply_focus": "what the reply should cover, not a repeat of headline",
  "related_context": ["relevant contact-memory context only"],
  "should_show": true,
  "confidence": "high | medium | low"
}

Rules:
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
        if text:
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


def _dedupe_lines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        text = _limit_line(line, 240)
        key = text.lower()
        if text and key not in seen:
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
1. User's explicit answers (highest priority — these are FACTS from the user)
2. Original email thread content
3. Contact memory context (for tone, relationship, past context only — NOT for facts)

- NEVER invent facts, numbers, dates, prices, commitments, or opinions
- Use the user's OWN WORDS wherever possible — keep their casual/formal level
- If the user's answers are insufficient to write a meaningful reply, say so in "note" and write a placeholder body asking the user for more details
- Be concise — reply length proportional to original message
- Do not include email headers (To, From, CC) in the body
- If the user provided revision instructions, apply them precisely
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
        parts.append(f"\nUser instruction:\n{revision_input}")
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

    thread_ctx = _fetch_thread_context_sync(mailbox, card)
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

    thread_ctx = _fetch_thread_context_sync(mailbox, card)
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
        max_tokens=20480,
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
        result = send_reply(
            mailbox=mailbox,
            thread_id=card.thread_id,
            to_addr=card.original.from_addr,
            body=draft_body,
            reply_mode=reply_mode,
        )
        return {"ok": True, "dry_run": False, "result": result}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "dry_run": False}
