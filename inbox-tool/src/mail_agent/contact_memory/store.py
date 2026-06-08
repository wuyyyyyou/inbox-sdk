"""按邮箱隔离的联系人记忆存储。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..storage.client import get_storage, scope as default_scope
from ..storage.types import _now
from .types import (
    ContactMemoryEvidence,
    ContactMemoryFile,
    ContactMemoryStats,
    ContactMessageSummary,
    ContactThreadMemory,
    ContactThreadSummary,
)


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def sanitize_key_part(value: str) -> str:
    text = normalize_email(value)
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in text).strip("._") or "default"


def contact_memory_key(mailbox: str, contact_email: str) -> str:
    return f"mailbox/{sanitize_key_part(mailbox)}/contacts/{sanitize_key_part(contact_email)}/memory"


def contact_memory_prefix(mailbox: str) -> str:
    return f"mailbox/{sanitize_key_part(mailbox)}/contacts/"


async def get_contact_memory(mailbox: str, contact_email: str) -> ContactMemoryFile | None:
    mailbox_key = normalize_email(mailbox)
    contact_key = normalize_email(contact_email)
    if not mailbox_key or not contact_key:
        return None
    result = await get_storage().get(contact_memory_key(mailbox_key, contact_key), scope=default_scope())
    if not result.get("exists") or not isinstance(result.get("value"), dict):
        return None
    return dict_to_contact_memory(result["value"], mailbox_key, contact_key)


async def save_contact_memory(memory: ContactMemoryFile) -> dict:
    memory.mailbox = normalize_email(memory.mailbox)
    memory.contact_email = normalize_email(memory.contact_email)
    memory.updated_at = _now()
    return await get_storage().set(
        contact_memory_key(memory.mailbox, memory.contact_email),
        asdict(memory),
        scope=default_scope(),
    )


async def delete_contact_memory(mailbox: str, contact_email: str) -> dict:
    return await get_storage().delete(
        contact_memory_key(normalize_email(mailbox), normalize_email(contact_email)),
        scope=default_scope(),
    )


async def list_contact_memories(mailbox: str) -> list[ContactMemoryFile]:
    mailbox_key = normalize_email(mailbox)
    if not mailbox_key:
        return []
    prefix = contact_memory_prefix(mailbox_key)
    memories: list[ContactMemoryFile] = []
    cursor: str | None = None
    while True:
        result = await get_storage().list(prefix=prefix, cursor=cursor, limit=200, scope=default_scope())
        for item in result.get("items") or []:
            key = str(item.get("key") or "")
            if not key.endswith("/memory"):
                continue
            raw = await get_storage().get(key, scope=default_scope())
            value = raw.get("value") if raw.get("exists") else None
            if isinstance(value, dict):
                contact_email = normalize_email(str(value.get("contact_email") or ""))
                if contact_email:
                    memories.append(dict_to_contact_memory(value, mailbox_key, contact_email))
        cursor = result.get("next_cursor")
        if not cursor:
            break
    memories.sort(key=lambda item: item.updated_at or "", reverse=True)
    return memories


async def clear_contact_memories(mailbox: str) -> int:
    mailbox_key = normalize_email(mailbox)
    if not mailbox_key:
        return 0
    prefix = contact_memory_prefix(mailbox_key)
    deleted = 0
    cursor: str | None = None
    while True:
        result = await get_storage().list(prefix=prefix, cursor=cursor, limit=200, scope=default_scope())
        for item in result.get("items") or []:
            key = str(item.get("key") or "")
            if key.endswith("/memory"):
                await get_storage().delete(key, scope=default_scope())
                deleted += 1
        cursor = result.get("next_cursor")
        if not cursor:
            break
    return deleted


def dict_to_contact_memory(raw: dict[str, Any], mailbox: str, contact_email: str) -> ContactMemoryFile:
    threads = [
        _dict_to_thread(item)
        for item in raw.get("threads", [])
        if isinstance(item, dict)
    ]
    stats_raw = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
    return ContactMemoryFile(
        mailbox=normalize_email(str(raw.get("mailbox") or mailbox)),
        contact_email=normalize_email(str(raw.get("contact_email") or contact_email)),
        display_name=str(raw.get("display_name") or ""),
        threads=threads,
        stats=ContactMemoryStats(
            messages_seen=int(stats_raw.get("messages_seen") or 0),
            threads_seen=int(stats_raw.get("threads_seen") or len(threads)),
            user_replies=int(stats_raw.get("user_replies") or 0),
            dismissed_count=int(stats_raw.get("dismissed_count") or 0),
            handled_manually_count=int(stats_raw.get("handled_manually_count") or 0),
            no_action_count=int(stats_raw.get("no_action_count") or 0),
        ),
        created_at=str(raw.get("created_at") or _now()),
        updated_at=str(raw.get("updated_at") or _now()),
    )


def _dict_to_thread(raw: dict[str, Any]) -> ContactThreadMemory:
    summary_raw = raw.get("thread_summary") if isinstance(raw.get("thread_summary"), dict) else {}
    return ContactThreadMemory(
        thread_id=str(raw.get("thread_id") or ""),
        subject=str(raw.get("subject") or ""),
        thread_summary=ContactThreadSummary(
            summary=str(summary_raw.get("summary") or ""),
            current_state=str(summary_raw.get("current_state") or ""),
            open_loop=str(summary_raw.get("open_loop") or ""),
            status=_coerce_status(summary_raw.get("status")),
            importance=_coerce_importance(summary_raw.get("importance")),
        ),
        message_summaries=[
            _dict_to_message(item)
            for item in raw.get("message_summaries", [])
            if isinstance(item, dict)
        ],
        source_refs=[
            ContactMemoryEvidence(
                mailbox=str(item.get("mailbox") or ""),
                thread_id=str(item.get("thread_id") or ""),
                message_id=str(item.get("message_id") or ""),
                date=str(item.get("date") or ""),
                source=str(item.get("source") or ""),
            )
            for item in raw.get("source_refs", [])
            if isinstance(item, dict)
        ],
        updated_at=str(raw.get("updated_at") or _now()),
    )


def _dict_to_message(raw: dict[str, Any]) -> ContactMessageSummary:
    direction = str(raw.get("direction") or "inbound")
    if direction not in ("inbound", "outbound"):
        direction = "inbound"
    return ContactMessageSummary(
        message_id=str(raw.get("message_id") or ""),
        from_addr=str(raw.get("from_addr") or raw.get("from") or ""),
        direction=direction,  # type: ignore[arg-type]
        date=str(raw.get("date") or ""),
        summary=str(raw.get("summary") or ""),
        action_signal=str(raw.get("action_signal") or ""),
    )


def _coerce_status(value: Any) -> str:
    text = str(value or "unknown")
    return text if text in ("open", "waiting_for_them", "closed", "unknown") else "unknown"


def _coerce_importance(value: Any) -> str:
    text = str(value or "medium")
    return text if text in ("high", "medium", "low") else "medium"


# 兼容旧调用名，后续没有调用方后可以删除。
get_contact_profile = get_contact_memory
save_contact_profile = save_contact_memory
delete_contact_profile = delete_contact_memory
dict_to_contact_profile = dict_to_contact_memory
