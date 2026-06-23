"""Synchronize persisted cards with Gmail thread state.

Gmail does not expose a mutable "replied" flag.  A thread is considered
replied only when the latest Gmail message is from the mailbox owner.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any
import re
import sys

from ..storage.types import PersistentCard, _now


@dataclass
class GmailThreadState:
    thread_id: str
    latest_message_id: str = ""
    latest_internal_date: str = ""
    latest_from_addr: str = ""
    latest_from_owner: bool = False
    unread_message_ids: list[str] | None = None
    sent_message_ids: list[str] | None = None
    trashed: bool = False
    history_id: str = ""
    missing: bool = False

    def to_card_state(self) -> dict[str, Any]:
        return {
            "last_synced_at": _now(),
            "latest_message_id": self.latest_message_id,
            "latest_internal_date": self.latest_internal_date,
            "latest_from_owner": self.latest_from_owner,
            "unread": bool(self.unread_message_ids),
            "history_id": self.history_id,
            "missing": self.missing,
        }


def _extract_email(header_value: str) -> str:
    value = str(header_value or "").strip()
    match = re.search(r"<([^>]+)>", value)
    if match:
        return match.group(1).strip().lower()
    return value.strip().lower()


def _owner_emails(mailbox: str, aliases: list[str] | None = None) -> set[str]:
    emails = {_extract_email(mailbox)}
    for alias in aliases or []:
        parsed = _extract_email(alias)
        if parsed:
            emails.add(parsed)
    return {email for email in emails if email}


def _max_history_id(messages: list[dict[str, Any]]) -> str:
    values: list[int] = []
    for msg in messages:
        try:
            values.append(int(msg.get("history_id") or 0))
        except (TypeError, ValueError):
            pass
    return str(max(values)) if values else ""


def _max_internal_date(messages: list[dict[str, Any]]) -> str:
    values: list[int] = []
    for msg in messages:
        try:
            values.append(int(msg.get("internal_date") or 0))
        except (TypeError, ValueError):
            pass
    return str(max(values)) if values else ""


def fetch_gmail_thread_state(mailbox: str, thread_id: str, *, owner_aliases: list[str] | None = None) -> GmailThreadState:
    """Fetch and cache one Gmail thread, returning a compact state summary."""
    from ..mail_providers.gmail.adapter import refresh_thread_cache

    messages = refresh_thread_cache(mailbox, thread_id)
    if not messages:
        return GmailThreadState(thread_id=thread_id, missing=True)

    latest = max(messages, key=lambda msg: int(msg.get("internal_date") or 0))
    owner_set = _owner_emails(mailbox, owner_aliases)
    latest_from = str(latest.get("from") or "")
    latest_email = _extract_email(latest_from)
    unread_ids: list[str] = []
    sent_ids: list[str] = []
    trashed = False
    for msg in messages:
        labels = {str(label).upper() for label in (msg.get("label_ids") or [])}
        mid = str(msg.get("id") or "")
        if mid and "UNREAD" in labels:
            unread_ids.append(mid)
        if mid and "SENT" in labels:
            sent_ids.append(mid)
        if "TRASH" in labels:
            trashed = True

    return GmailThreadState(
        thread_id=thread_id,
        latest_message_id=str(latest.get("id") or ""),
        latest_internal_date=str(latest.get("internal_date") or ""),
        latest_from_addr=latest_from,
        latest_from_owner=bool(latest_email and latest_email in owner_set),
        unread_message_ids=unread_ids,
        sent_message_ids=sent_ids,
        trashed=trashed,
        history_id=_max_history_id(messages),
    )


def _card_key(card: PersistentCard) -> str:
    return card.thread_id or card.card_id


async def reconcile_active_cards_with_gmail(
    mailbox: str,
    *,
    reason: str = "scan_start",
    max_cards: int = 200,
) -> dict[str, Any]:
    """Update persisted active cards from current Gmail thread state."""
    from ..storage.ops import append_card_action, get_active_cards, set_active_cards

    active = await get_active_cards(mailbox)
    checked_threads = 0
    resolved_replied = 0
    marked_read = 0
    removed_missing = 0
    warnings: list[str] = []
    changed = False
    seen_threads: set[str] = set()
    history_values: list[int] = []

    for card in active.cards[:max_cards]:
        if not card.thread_id or card.status == "dismissed":
            continue
        if card.thread_id in seen_threads:
            continue
        seen_threads.add(card.thread_id)
        try:
            state = fetch_gmail_thread_state(mailbox, card.thread_id)
        except Exception as exc:
            warnings.append(f"{card.thread_id}: {type(exc).__name__}: {exc}")
            continue

        checked_threads += 1
        card.gmail_state = state.to_card_state()
        try:
            history_values.append(int(state.history_id or 0))
        except (TypeError, ValueError):
            pass

        if state.missing or state.trashed:
            if card.status != "dismissed":
                card.status = "dismissed"
                card.resolved_at = _now()
                card.resolution = "gmail_removed"
                card.updated_at = _now()
                removed_missing += 1
                changed = True
                await append_card_action(mailbox, card.card_id, card.title, "gmail_removed", reason)
            continue

        if state.latest_from_owner:
            if card.status != "resolved" or card.resolution not in ("replied", "replied_in_gmail"):
                card.status = "resolved"
                card.resolution = "replied_in_gmail"
                card.resolved_at = _now()
                resolved_replied += 1
                changed = True
                await append_card_action(mailbox, card.card_id, card.title, "replied_in_gmail", reason)
            card.updated_at = _now()
            continue

        if not state.unread_message_ids:
            marked_read += 1
        card.updated_at = _now()
        changed = True

    if changed:
        await set_active_cards(mailbox, active)

    summary = {
        "ok": True,
        "mailbox": mailbox,
        "checked_threads": checked_threads,
        "resolved_replied": resolved_replied,
        "marked_read": marked_read,
        "removed_missing": removed_missing,
        "last_history_id": str(max(history_values)) if history_values else "",
        "warnings": warnings[-5:],
    }
    if warnings:
        print(f"[gmail_status_sync] warnings: {warnings[-3:]}", file=sys.stderr)
    return summary


async def mark_card_thread_read_in_gmail(mailbox: str, card: PersistentCard) -> dict[str, Any]:
    """Mark unread messages in a card's thread as read and patch local cache."""
    from ..mail_providers.gmail.adapter import batch_mark_read, patch_cached_messages_read
    import asyncio

    if not card.thread_id:
        return {"ok": True, "marked": 0}
    state = fetch_gmail_thread_state(mailbox, card.thread_id)
    unread_ids = list(state.unread_message_ids or [])
    if not unread_ids:
        card.gmail_state = state.to_card_state()
        return {"ok": True, "marked": 0, "state": asdict(state)}
    result = await asyncio.to_thread(batch_mark_read, mailbox, unread_ids)
    patch_cached_messages_read(mailbox, unread_ids)
    refreshed = fetch_gmail_thread_state(mailbox, card.thread_id)
    card.gmail_state = refreshed.to_card_state()
    return {"ok": True, "marked": len(unread_ids), "result": result, "state": asdict(refreshed)}


async def refresh_card_gmail_state(mailbox: str, card: PersistentCard) -> dict[str, Any]:
    """Refresh one card's Gmail state and store it on the card object."""
    state = fetch_gmail_thread_state(mailbox, card.thread_id) if card.thread_id else GmailThreadState(thread_id="")
    card.gmail_state = state.to_card_state()
    return asdict(state)
