"""联系人记忆管理接口。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from .indexer import ingest_card_event, parse_contact
from .store import clear_contact_memories, delete_contact_memory, get_contact_memory, list_contact_memories, normalize_email
from .types import ContactMemoryFile, ContactThreadMemory


async def list_memory_summaries(mailboxes: list[str]) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    for mailbox in _normalize_mailboxes(mailboxes):
        for memory in await list_contact_memories(mailbox):
            summaries.append(_memory_summary(memory))
    summaries.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return {
        "contacts": summaries,
        "count": len(summaries),
        "mailboxes": sorted({str(item.get("mailbox") or "") for item in summaries if item.get("mailbox")}),
    }


async def backfill_active_card_memories(
    mailboxes: list[str],
    *,
    sampling_create_message: Any = None,
    source: str = "active_card_backfill",
    since: str = "",
) -> dict[str, Any]:
    from ..storage.ops import get_active_cards

    backfilled = 0
    skipped_old = 0
    since_dt = _parse_dt(since)
    for mailbox in _normalize_mailboxes(mailboxes):
        active = await get_active_cards(mailbox)
        for card in active.cards:
            if getattr(card, "card_type", "") == "cleanup_bundle":
                continue
            # 后台补写只处理本次扫描后的卡片，避免每次扫描重复触发旧卡片的 LLM 写入。
            card_dt = _parse_dt(getattr(card, "created_at", "") or getattr(card, "updated_at", ""))
            if since_dt and card_dt and card_dt < since_dt:
                skipped_old += 1
                continue
            contact_email, _ = parse_contact(getattr(card.original, "from_addr", ""))
            thread_id = getattr(card, "thread_id", "") or getattr(card, "message_id", "")
            if not contact_email or not thread_id:
                continue
            await ingest_card_event(
                mailbox,
                card,
                event_type="card_created",
                source=source,
                user_action="backfill",
                sampling_create_message=sampling_create_message,
            )
            backfilled += 1
    return {"ok": True, "backfilled": backfilled, "skipped_old": skipped_old}


async def get_memory_detail(mailbox: str, contact_email: str) -> dict[str, Any]:
    mailbox_key = normalize_email(mailbox)
    contact_key = normalize_email(contact_email)
    if not mailbox_key or not contact_key:
        return {"error": "mailbox and contact_email are required"}
    memory = await get_contact_memory(mailbox_key, contact_key)
    if memory is None:
        return {"error": f"Contact memory not found: {contact_key}"}
    return {"memory": asdict(memory), "summary": _memory_summary(memory)}


async def delete_memory(mailbox: str, contact_email: str) -> dict[str, Any]:
    mailbox_key = normalize_email(mailbox)
    contact_key = normalize_email(contact_email)
    if not mailbox_key or not contact_key:
        return {"error": "mailbox and contact_email are required"}
    await delete_contact_memory(mailbox_key, contact_key)
    return {"ok": True, "mailbox": mailbox_key, "contact_email": contact_key}


async def clear_memory(mailboxes: list[str]) -> dict[str, Any]:
    deleted_by_mailbox: dict[str, int] = {}
    for mailbox in _normalize_mailboxes(mailboxes):
        deleted_by_mailbox[mailbox] = await clear_contact_memories(mailbox)
    return {
        "ok": True,
        "deleted": sum(deleted_by_mailbox.values()),
        "deleted_by_mailbox": deleted_by_mailbox,
    }


def _memory_summary(memory: ContactMemoryFile) -> dict[str, Any]:
    latest = _latest_thread(memory)
    statuses = {"open": 0, "waiting_for_them": 0, "closed": 0, "unknown": 0}
    for thread in memory.threads:
        status = thread.thread_summary.status if thread.thread_summary.status in statuses else "unknown"
        statuses[status] += 1
    return {
        "mailbox": memory.mailbox,
        "contact_email": memory.contact_email,
        "display_name": memory.display_name,
        "thread_count": len(memory.threads),
        "message_count": sum(len(thread.message_summaries) for thread in memory.threads),
        "open_count": statuses["open"],
        "waiting_count": statuses["waiting_for_them"],
        "closed_count": statuses["closed"],
        "unknown_count": statuses["unknown"],
        "updated_at": memory.updated_at,
        "latest_subject": latest.subject if latest else "",
        "latest_summary": latest.thread_summary.summary if latest else "",
        "latest_status": latest.thread_summary.status if latest else "unknown",
    }


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or ""))
    except Exception:
        return None


def _latest_thread(memory: ContactMemoryFile) -> ContactThreadMemory | None:
    if not memory.threads:
        return None
    return sorted(memory.threads, key=lambda item: item.updated_at or "", reverse=True)[0]


def _normalize_mailboxes(mailboxes: list[str]) -> list[str]:
    result = [normalize_email(mailbox) for mailbox in mailboxes if normalize_email(mailbox)]
    return sorted(dict.fromkeys(result))
