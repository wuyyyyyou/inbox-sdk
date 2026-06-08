"""联系人记忆写入。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from email.utils import parseaddr
from html import unescape
from typing import Any

from ..llm_runtime.service import call_llm_json_safe
from ..storage.types import PersistentCard, _now
from .store import get_contact_memory, normalize_email, save_contact_memory
from .types import (
    ContactMemoryEvidence,
    ContactMemoryFile,
    ContactMessageSummary,
    ContactThreadMemory,
    ContactThreadSummary,
)

MAX_THREAD_MEMORIES = 30
MAX_MESSAGE_SUMMARIES = 24


def parse_contact(raw: str) -> tuple[str, str]:
    name, email = parseaddr(str(raw or ""))
    return normalize_email(email or raw), name.strip().strip('"')


async def ingest_card_event(
    mailbox: str,
    card: PersistentCard,
    *,
    event_type: str = "card_created",
    source: str = "gmail_scan",
    user_action: str = "",
    draft_excerpt: str = "",
    sampling_create_message: Any = None,
) -> None:
    contact_email, contact_name = parse_contact(card.original.from_addr)
    if not mailbox or not contact_email:
        return

    messages = _thread_messages(mailbox, card.thread_id)
    if not messages:
        messages = [_card_as_message(card)]
    if draft_excerpt:
        messages.append(_synthetic_reply_message(mailbox, card.thread_id, draft_excerpt))

    await update_contact_thread_memory(
        mailbox=mailbox,
        contact_header=card.original.from_addr,
        thread_id=card.thread_id or card.message_id,
        subject=card.original.thread or card.title,
        messages=messages,
        sampling_create_message=sampling_create_message,
        display_name=contact_name,
        thread_summary_hint=_card_thread_summary_hint(card),
        source=source,
        user_action=user_action or event_type,
    )


async def ingest_thread_observation(
    mailbox: str,
    thread_id: str,
    fallback_contact_header: str,
    subject: str = "",
    *,
    sampling_create_message: Any = None,
    source: str = "gmail_scan",
    user_action: str = "owner_replied",
) -> None:
    messages = _thread_messages(mailbox, thread_id)
    contact_header = _latest_non_owner_header(messages, mailbox) or fallback_contact_header
    if not messages:
        messages = [{
            "message_id": thread_id,
            "thread_id": thread_id,
            "from_addr": fallback_contact_header,
            "to_addr": mailbox,
            "subject": subject,
            "body": "",
            "date": _now(),
        }]
    await update_contact_thread_memory(
        mailbox=mailbox,
        contact_header=contact_header,
        thread_id=thread_id,
        subject=subject or _first_non_empty(*(str(m.get("subject") or "") for m in messages)),
        messages=messages,
        sampling_create_message=sampling_create_message,
        thread_summary_hint=_subject_thread_summary_hint(subject, messages),
        source=source,
        user_action=user_action,
    )


async def update_contact_thread_memory(
    *,
    mailbox: str,
    contact_header: str,
    thread_id: str,
    subject: str = "",
    messages: list[dict[str, Any]] | None = None,
    sampling_create_message: Any = None,
    display_name: str = "",
    thread_summary_hint: str = "",
    source: str = "gmail_scan",
    user_action: str = "",
) -> None:
    contact_email, parsed_name = parse_contact(contact_header)
    mailbox_key = normalize_email(mailbox)
    if not mailbox_key or not contact_email or not thread_id:
        return
    memory = await get_contact_memory(mailbox_key, contact_email)
    if memory is None:
        memory = ContactMemoryFile(mailbox=mailbox_key, contact_email=contact_email, display_name=display_name or parsed_name)
    elif (display_name or parsed_name) and not memory.display_name:
        memory.display_name = display_name or parsed_name

    existing = next((item for item in memory.threads if item.thread_id == thread_id), None)
    normalized_messages = _normalize_messages(messages or [], mailbox_key, thread_id)
    generated = await _build_thread_memory(
        mailbox=mailbox_key,
        contact_email=contact_email,
        thread_id=thread_id,
        subject=subject,
        messages=normalized_messages,
        existing=existing,
        sampling_create_message=sampling_create_message,
        thread_summary_hint=thread_summary_hint,
        source=source,
        user_action=user_action,
    )
    _upsert_thread(memory, generated)
    _update_stats(memory, generated, user_action)
    await save_contact_memory(memory)


async def _build_thread_memory(
    *,
    mailbox: str,
    contact_email: str,
    thread_id: str,
    subject: str,
    messages: list[dict[str, Any]],
    existing: ContactThreadMemory | None,
    sampling_create_message: Any,
    thread_summary_hint: str,
    source: str,
    user_action: str,
) -> ContactThreadMemory:
    fallback = _fallback_thread_memory(mailbox, contact_email, thread_id, subject, messages, existing, source, user_action, thread_summary_hint)
    if sampling_create_message is None:
        return fallback

    payload = {
        "mailbox": mailbox,
        "contact_email": contact_email,
        "thread_id": thread_id,
        "subject": subject,
        "thread_summary_hint": _limit(thread_summary_hint, 320),
        "user_action": user_action,
        "existing_memory": asdict(existing) if existing else {},
        "messages": [
            {
                "message_id": item.get("message_id", ""),
                "from_addr": item.get("from_addr", ""),
                "direction": item.get("direction", ""),
                "date": item.get("date", ""),
                "body": _limit(item.get("body", ""), 1200),
            }
            for item in messages[-12:]
        ],
    }
    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=(
            "You update a mailbox-scoped contact memory file. Summarize only this Gmail thread. "
            "Do not invent facts. Keep summaries short and specific. "
            "The thread_summary.summary must describe the thread-level event or topic, not quote the email body. "
            "Use message_summaries for per-message content."
        ),
        user_message=f"""Update one contact thread memory from the provided messages.

Status rules:
- open: the contact is waiting for the user.
- waiting_for_them: the user already replied and is waiting for the contact.
- closed: no follow-up is needed.
- unknown: unclear.

Return JSON with this shape:
{{
  "thread_summary": {{
    "summary": "one concise sentence about the thread-level event or topic",
    "current_state": "current state in one concise sentence",
    "open_loop": "specific unresolved ask, or empty",
    "status": "open|waiting_for_them|closed|unknown",
    "importance": "high|medium|low"
  }},
  "message_summaries": [
    {{
      "message_id": "...",
      "from_addr": "...",
      "direction": "inbound|outbound",
      "date": "...",
      "summary": "one concise sentence",
      "action_signal": "ask|reply|decision|FYI|none"
    }}
  ]
}}

Input:
{json.dumps(payload, ensure_ascii=False)}""",
        fallback={
            "thread_summary": asdict(fallback.thread_summary),
            "message_summaries": [asdict(item) for item in fallback.message_summaries],
        },
        temperature=0.0,
        max_tokens=1200,
        timeout=90.0,
        metadata={"tool": "contact_memory_update", "thread_id": thread_id},
    )
    raw = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    generated = _thread_from_payload(mailbox, thread_id, subject, raw, fallback)
    generated.source_refs = _merge_evidence(fallback.source_refs, generated.source_refs)
    return generated


def _fallback_thread_memory(
    mailbox: str,
    contact_email: str,
    thread_id: str,
    subject: str,
    messages: list[dict[str, Any]],
    existing: ContactThreadMemory | None,
    source: str,
    user_action: str,
    thread_summary_hint: str,
) -> ContactThreadMemory:
    summaries = _merge_message_summaries(
        existing.message_summaries if existing else [],
        [
            ContactMessageSummary(
                message_id=str(item.get("message_id") or ""),
                from_addr=str(item.get("from_addr") or ""),
                direction=item.get("direction") if item.get("direction") in ("inbound", "outbound") else "inbound",
                date=str(item.get("date") or _now()),
                summary=_limit(_first_non_empty(item.get("body"), item.get("snippet"), subject), 220),
                action_signal=_action_signal(user_action),
            )
            for item in messages
        ],
    )
    latest = summaries[-1] if summaries else None
    status = _status_from_action(user_action, latest.direction if latest else "inbound")
    existing_summary = existing.thread_summary.summary if existing and not thread_summary_hint else ""
    summary_text = _limit(_first_non_empty(
        thread_summary_hint,
        existing_summary,
        _subject_thread_summary_hint(subject, messages),
        subject,
        "Conversation with contact",
    ), 260)
    source_refs = _merge_evidence(
        existing.source_refs if existing else [],
        [
            ContactMemoryEvidence(
                mailbox=mailbox,
                thread_id=thread_id,
                message_id=str(item.get("message_id") or ""),
                date=str(item.get("date") or _now()),
                source=source,
            )
            for item in messages
        ],
    )
    return ContactThreadMemory(
        thread_id=thread_id,
        subject=subject or (existing.subject if existing else ""),
        thread_summary=ContactThreadSummary(
            summary=summary_text,
            current_state=_current_state(status),
            open_loop="" if status in ("waiting_for_them", "closed") else _limit(_first_non_empty(summary_text, subject), 200),
            status=status,  # type: ignore[arg-type]
            importance="medium",
        ),
        message_summaries=summaries,
        source_refs=source_refs,
        updated_at=_now(),
    )


def _thread_from_payload(
    mailbox: str,
    thread_id: str,
    subject: str,
    payload: dict[str, Any],
    fallback: ContactThreadMemory,
) -> ContactThreadMemory:
    summary_raw = payload.get("thread_summary") if isinstance(payload.get("thread_summary"), dict) else {}
    status = str(summary_raw.get("status") or fallback.thread_summary.status)
    if status not in ("open", "waiting_for_them", "closed", "unknown"):
        status = fallback.thread_summary.status
    importance = str(summary_raw.get("importance") or fallback.thread_summary.importance)
    if importance not in ("high", "medium", "low"):
        importance = fallback.thread_summary.importance
    generated_messages = [
        ContactMessageSummary(
            message_id=str(item.get("message_id") or ""),
            from_addr=str(item.get("from_addr") or ""),
            direction=str(item.get("direction") or "inbound") if str(item.get("direction") or "") in ("inbound", "outbound") else "inbound",
            date=str(item.get("date") or ""),
            summary=_limit(str(item.get("summary") or ""), 260),
            action_signal=_limit(str(item.get("action_signal") or ""), 80),
        )
        for item in payload.get("message_summaries", [])
        if isinstance(item, dict)
    ]
    return ContactThreadMemory(
        thread_id=thread_id,
        subject=subject or fallback.subject,
        thread_summary=ContactThreadSummary(
            summary=_limit(str(summary_raw.get("summary") or fallback.thread_summary.summary), 320),
            current_state=_limit(str(summary_raw.get("current_state") or fallback.thread_summary.current_state), 260),
            open_loop=_limit(str(summary_raw.get("open_loop") or fallback.thread_summary.open_loop), 240),
            status=status,  # type: ignore[arg-type]
            importance=importance,  # type: ignore[arg-type]
        ),
        message_summaries=_merge_message_summaries(fallback.message_summaries, generated_messages),
        source_refs=fallback.source_refs,
        updated_at=_now(),
    )


def _thread_messages(mailbox: str, thread_id: str) -> list[dict[str, Any]]:
    if not thread_id:
        return []
    try:
        from ..mail_providers.gmail.adapter import get_thread_context
        thread = get_thread_context(mailbox, thread_id, max_messages=12)
        return [
            {
                "message_id": msg.message_id,
                "thread_id": msg.thread_id,
                "from_addr": msg.from_addr,
                "to_addr": msg.to_addr,
                "subject": msg.subject,
                "body": msg.body_text or msg.snippet,
                "snippet": msg.snippet,
                "date": msg.internal_date,
            }
            for msg in thread.messages
        ]
    except Exception:
        return []


def _normalize_messages(messages: list[dict[str, Any]], mailbox: str, thread_id: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    owner = normalize_email(mailbox)
    for item in messages:
        from_addr = str(item.get("from_addr") or item.get("from") or "")
        from_email, _ = parse_contact(from_addr)
        direction = "outbound" if from_email == owner else "inbound"
        normalized.append({
            "message_id": str(item.get("message_id") or item.get("id") or ""),
            "thread_id": str(item.get("thread_id") or thread_id),
            "from_addr": from_addr,
            "to_addr": str(item.get("to_addr") or item.get("to") or ""),
            "subject": str(item.get("subject") or ""),
            "body": str(item.get("body") or item.get("body_text") or item.get("snippet") or ""),
            "snippet": str(item.get("snippet") or ""),
            "date": str(item.get("date") or item.get("internal_date") or _now()),
            "direction": direction,
        })
    normalized.sort(key=lambda item: item.get("date") or "")
    return normalized


def _card_thread_summary_hint(card: PersistentCard) -> str:
    return _limit(_first_non_empty(
        card.summary,
        card.title,
        getattr(card.details, "latest_activity", ""),
        card.original.thread,
        card.recommendation,
    ), 320)


def _subject_thread_summary_hint(subject: str, messages: list[dict[str, Any]] | None = None) -> str:
    clean_subject = _limit(subject, 220)
    if clean_subject:
        return clean_subject
    subjects = [str(item.get("subject") or "") for item in (messages or [])]
    return _limit(_first_non_empty(*subjects), 220)


def _card_as_message(card: PersistentCard) -> dict[str, Any]:
    return {
        "message_id": card.message_id,
        "thread_id": card.thread_id,
        "from_addr": card.original.from_addr,
        "to_addr": card.original.to_addr,
        "subject": card.original.thread or card.title,
        "body": card.original.body or card.summary,
        "date": card.original.time or card.updated_at or _now(),
    }


def _synthetic_reply_message(mailbox: str, thread_id: str, body: str) -> dict[str, Any]:
    return {
        "message_id": f"app_reply:{_now()}",
        "thread_id": thread_id,
        "from_addr": mailbox,
        "to_addr": "",
        "subject": "",
        "body": body,
        "date": _now(),
    }


def _latest_non_owner_header(messages: list[dict[str, Any]], owner_email: str) -> str:
    owner = normalize_email(owner_email)
    for item in reversed(messages):
        from_addr = str(item.get("from_addr") or "")
        from_email, _ = parse_contact(from_addr)
        if from_email and from_email != owner:
            return from_addr
    return ""


def _upsert_thread(memory: ContactMemoryFile, thread: ContactThreadMemory) -> None:
    memory.threads = [item for item in memory.threads if item.thread_id != thread.thread_id]
    memory.threads.insert(0, thread)
    memory.threads = sorted(memory.threads, key=lambda item: item.updated_at or "", reverse=True)[:MAX_THREAD_MEMORIES]


def _update_stats(memory: ContactMemoryFile, thread: ContactThreadMemory, user_action: str) -> None:
    memory.stats.messages_seen = len({item.message_id for t in memory.threads for item in t.message_summaries if item.message_id})
    memory.stats.threads_seen = len(memory.threads)
    memory.stats.user_replies = len({
        item.message_id
        for t in memory.threads
        for item in t.message_summaries
        if item.direction == "outbound" and item.message_id
    })
    if user_action == "handled_manually":
        memory.stats.handled_manually_count += 1
    elif user_action == "no_action_needed":
        memory.stats.no_action_count += 1
    elif user_action == "dismissed":
        memory.stats.dismissed_count += 1


def _merge_message_summaries(
    existing: list[ContactMessageSummary],
    incoming: list[ContactMessageSummary],
) -> list[ContactMessageSummary]:
    by_id: dict[str, ContactMessageSummary] = {item.message_id: item for item in existing if item.message_id}
    no_id = [item for item in existing if not item.message_id]
    for item in incoming:
        if item.message_id:
            by_id[item.message_id] = item
        else:
            no_id.append(item)
    merged = no_id + list(by_id.values())
    merged.sort(key=lambda item: item.date or "")
    return merged[-MAX_MESSAGE_SUMMARIES:]


def _merge_evidence(
    existing: list[ContactMemoryEvidence],
    incoming: list[ContactMemoryEvidence],
) -> list[ContactMemoryEvidence]:
    seen: set[tuple[str, str, str]] = set()
    merged: list[ContactMemoryEvidence] = []
    for item in incoming + existing:
        key = (item.mailbox, item.thread_id, item.message_id)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged[:12]


def _status_from_action(user_action: str, latest_direction: str) -> str:
    if user_action in ("handled_manually", "no_action_needed", "dismissed"):
        return "closed"
    if user_action in ("reply", "user_replied", "owner_replied") or latest_direction == "outbound":
        return "waiting_for_them"
    if latest_direction == "inbound":
        return "open"
    return "unknown"


def _current_state(status: str) -> str:
    if status == "open":
        return "The contact may be waiting for the user."
    if status == "waiting_for_them":
        return "The user has replied and is waiting for the contact."
    if status == "closed":
        return "No follow-up is currently needed."
    return "The current state is unclear."


def _action_signal(user_action: str) -> str:
    if user_action in ("reply", "user_replied", "owner_replied"):
        return "reply"
    if user_action in ("handled_manually", "no_action_needed", "dismissed"):
        return "decision"
    return "none"


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _limit(value: Any, size: int) -> str:
    text = _plain_memory_text(value)
    return text[:size].rstrip()


def _plain_memory_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    tag_match = re.search(r"<\s*[a-z][^>]*>", text, flags=re.IGNORECASE)
    if tag_match:
        if tag_match.start() > 24:
            text = text[:tag_match.start()]
        else:
            text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
            text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
            text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
            text = re.sub(r"</(?:p|div|li|tr)>", " ", text, flags=re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(unescape(text).split())
