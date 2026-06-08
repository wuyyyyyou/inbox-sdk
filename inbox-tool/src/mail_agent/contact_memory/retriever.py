"""为提示词检索联系人记忆。"""

from __future__ import annotations

import json
from dataclasses import asdict
from email.utils import parseaddr
from typing import Any

from ..llm_runtime.service import call_llm_json_safe, dashscope_available
from .store import get_contact_memory, normalize_email
from .types import ContactContext, ContactContextTopic, ContactMemoryQuery, ContactThreadMemory

MAX_SELECTOR_CANDIDATES = 20
MAX_SELECTED_THREADS = 3


async def retrieve_contact_context(query: ContactMemoryQuery, sampling_create_message: Any = None) -> ContactContext:
    contact_email = normalize_email(query.contact_email)
    if not query.mailbox or not contact_email:
        return ContactContext(contact_email=contact_email)
    memory = await get_contact_memory(query.mailbox, contact_email)
    if memory is None:
        return ContactContext(contact_email=contact_email)

    candidates = [
        thread
        for thread in memory.threads
        if thread.thread_id and thread.thread_id != query.current_thread_id
    ]
    candidates = sorted(candidates, key=lambda item: item.updated_at or "", reverse=True)[:MAX_SELECTOR_CANDIDATES]
    if not candidates or (sampling_create_message is None and not dashscope_available()):
        return ContactContext(contact_email=contact_email)

    selected = await _select_threads_with_llm(query, candidates, sampling_create_message)
    by_id = {thread.thread_id: thread for thread in candidates}
    topics: list[ContactContextTopic] = []
    source_refs: list[dict[str, Any]] = []
    for item in selected[:MAX_SELECTED_THREADS]:
        thread_id = str(item.get("thread_id") or "")
        thread = by_id.get(thread_id)
        if not thread:
            continue
        relevance = _safe_float(item.get("relevance"))
        topics.append(ContactContextTopic(
            thread_id=thread.thread_id,
            title=thread.subject,
            summary=_thread_summary_text(thread),
            open_loop=thread.thread_summary.open_loop,
            status=thread.thread_summary.status,
            confidence=relevance,
            reason=str(item.get("reason") or ""),
        ))
        include_ids = {str(mid) for mid in item.get("include_message_ids", []) if str(mid).strip()}
        refs = thread.source_refs[:2]
        if include_ids:
            refs = [ref for ref in thread.source_refs if ref.message_id in include_ids] or refs
        source_refs.extend(asdict(ref) for ref in refs[:2])

    return ContactContext(
        contact_email=memory.contact_email,
        relationship_hint=_relationship_hint(memory.display_name, memory.stats.user_replies),
        relevant_topics=topics,
        reply_guidance=_reply_guidance(topics),
        source_refs=source_refs[:5],
    )


def contact_email_from_header(value: str) -> str:
    _, email = parseaddr(str(value or ""))
    return normalize_email(email or value)


def format_contact_context_for_prompt(context: ContactContext) -> str:
    if not context.relevant_topics:
        return "No relevant contact memory."
    lines: list[str] = []
    if context.relationship_hint:
        lines.append(f"Relationship: {context.relationship_hint}")
    for index, topic in enumerate(context.relevant_topics, start=1):
        parts = [topic.title or topic.summary]
        if topic.summary and topic.summary not in parts:
            parts.append(topic.summary)
        if topic.open_loop:
            parts.append(f"Open loop: {topic.open_loop}")
        if topic.reason:
            parts.append(f"Why relevant: {topic.reason}")
        lines.append(f"{index}. " + " | ".join(part for part in parts if part))
    if context.reply_guidance:
        lines.append(f"Reply guidance: {context.reply_guidance}")
    return "\n".join(lines)


async def _select_threads_with_llm(
    query: ContactMemoryQuery,
    candidates: list[ContactThreadMemory],
    sampling_create_message: Any,
) -> list[dict[str, Any]]:
    candidate_payload = [
        {
            "thread_id": thread.thread_id,
            "subject": thread.subject,
            "summary": thread.thread_summary.summary,
            "current_state": thread.thread_summary.current_state,
            "open_loop": thread.thread_summary.open_loop,
            "status": thread.thread_summary.status,
            "updated_at": thread.updated_at,
            "messages": [
                {
                    "message_id": msg.message_id,
                    "direction": msg.direction,
                    "date": msg.date,
                    "summary": msg.summary,
                    "action_signal": msg.action_signal,
                }
                for msg in thread.message_summaries[-4:]
            ],
        }
        for thread in candidates
    ]
    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=(
            "You are a contact-memory selector. Select only previous threads that help understand "
            "the current email. Do not select the current thread. Do not select merely because it is recent."
        ),
        user_message=f"""Choose relevant prior contact-memory threads for the current task.

Hard constraints:
- Candidates are already limited to the same mailbox and same contact.
- The current thread has already been excluded.
- Return zero selections when prior threads do not add useful context.

Current task:
{{
  "purpose": {json.dumps(query.purpose)},
  "current_subject": {json.dumps(query.current_subject, ensure_ascii=False)},
  "current_body": {json.dumps(query.current_body, ensure_ascii=False)},
  "current_thread_id": {json.dumps(query.current_thread_id)}
}}

Candidate memories:
{json.dumps(candidate_payload, ensure_ascii=False)}

Return JSON:
{{
  "selected": [
    {{
      "thread_id": "...",
      "relevance": 0.0,
      "reason": "why this prior thread helps",
      "include_message_ids": ["..."]
    }}
  ],
  "none_reason": "why none were selected, if selected is empty"
}}""",
        fallback={"selected": [], "none_reason": "LLM selector unavailable."},
        temperature=0.0,
        max_tokens=900,
        timeout=90.0,
        metadata={"tool": "contact_memory_selector", "purpose": query.purpose},
    )
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    selected = payload.get("selected") if isinstance(payload.get("selected"), list) else []
    return [item for item in selected if isinstance(item, dict)]


def _thread_summary_text(thread: ContactThreadMemory) -> str:
    parts = [
        thread.thread_summary.summary,
        thread.thread_summary.current_state,
    ]
    return " ".join(part.strip() for part in parts if part and part.strip())


def _relationship_hint(display_name: str, user_replies: int) -> str:
    if display_name and user_replies:
        return f"{display_name}; user has replied {user_replies} time(s)."
    if display_name:
        return display_name
    if user_replies:
        return f"User has replied {user_replies} time(s)."
    return ""


def _reply_guidance(topics: list[ContactContextTopic]) -> str:
    for topic in topics:
        if topic.status == "open" and topic.open_loop:
            return topic.open_loop
    return ""


def _safe_float(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(score, 0.0), 1.0)
