"""Focused tests for Gmail -> active-card status reconciliation.

Run: cd inbox-tool/src && uv run python tests/test_gmail_status_sync.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class FakeStorageClient:
    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}
        self._generation = 1

    async def get(self, key: str, *, scope: str = "user", timeout: float = 30) -> dict[str, Any]:
        if key in self._store:
            return {"value": self._store[key]["value"], "etag": self._store[key]["etag"], "exists": True}
        return {"value": None, "etag": "", "exists": False}

    async def set(
        self,
        key: str,
        value: Any,
        *,
        scope: str = "user",
        if_match: str | None = None,
        ttl_seconds: int | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        etag = f"etag_{self._generation}"
        self._generation += 1
        self._store[key] = {"value": value, "etag": etag}
        return {"etag": etag, "generation": self._generation}

    async def delete(self, key: str, *, scope: str = "user", if_match: str | None = None, timeout: float = 30) -> dict[str, Any]:
        self._store.pop(key, None)
        return {"deleted": True}

    async def list(
        self,
        *,
        prefix: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
        kind: str | None = None,
        scope: str = "user",
        timeout: float = 30,
    ) -> dict[str, Any]:
        return {"items": [{"key": key} for key in self._store if not prefix or key.startswith(prefix)], "next_cursor": None}


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"PASS {label}")


def card(card_id: str, thread_id: str):
    from mail_agent.storage.types import CardDetails, OriginalEmail, PersistentCard

    return PersistentCard(
        card_id=card_id,
        message_id=f"msg_{card_id}",
        thread_id=thread_id,
        title=f"Card {card_id}",
        user_action="reply",
        details=CardDetails(mailbox="user@example.com"),
        original=OriginalEmail(from_addr="alice@example.com", thread=f"Thread {thread_id}"),
    )


async def main() -> None:
    from mail_agent.storage.client import init
    from mail_agent.storage.ops import get_active_cards, get_run_history, set_active_cards
    from mail_agent.storage.types import ActiveCards
    from mail_agent.sync import gmail_status
    from mail_agent.sync.gmail_status import GmailThreadState, reconcile_active_cards_with_gmail

    init(FakeStorageClient(), FakeStorageClient(), scope="user")  # type: ignore[arg-type]

    original_fetch = gmail_status.fetch_gmail_thread_state
    states = {
        "t_reply": GmailThreadState(
            thread_id="t_reply",
            latest_message_id="sent_1",
            latest_internal_date="1770000000000",
            latest_from_addr="User <user@example.com>",
            latest_from_owner=True,
            unread_message_ids=[],
            sent_message_ids=["sent_1"],
            history_id="20",
        ),
        "t_missing": GmailThreadState(thread_id="t_missing", missing=True),
        "t_open": GmailThreadState(
            thread_id="t_open",
            latest_message_id="in_1",
            latest_internal_date="1770000000001",
            latest_from_addr="Alice <alice@example.com>",
            latest_from_owner=False,
            unread_message_ids=["in_1"],
            history_id="30",
        ),
    }

    def fake_fetch(mailbox: str, thread_id: str, *, owner_aliases: list[str] | None = None) -> GmailThreadState:
        return states[thread_id]

    gmail_status.fetch_gmail_thread_state = fake_fetch
    try:
        await set_active_cards("user@example.com", ActiveCards(cards=[
            card("reply", "t_reply"),
            card("missing", "t_missing"),
            card("open", "t_open"),
        ]))

        summary = await reconcile_active_cards_with_gmail("user@example.com", reason="test")
        active = await get_active_cards("user@example.com")
        by_id = {item.card_id: item for item in active.cards}

        check("checked all threads", summary["checked_threads"] == 3, str(summary))
        check("resolved replied", by_id["reply"].status == "resolved" and by_id["reply"].resolution == "replied_in_gmail")
        check("dismissed missing", by_id["missing"].status == "dismissed" and by_id["missing"].resolution == "gmail_removed")
        check("keeps open pending", by_id["open"].status == "pending")
        check("writes gmail state", by_id["reply"].gmail_state.get("latest_from_owner") is True)
        check("summary history id", summary["last_history_id"] == "30", str(summary))

        history = await get_run_history(limit=10)
        actions = {entry.action for entry in history}
        check("writes action history", {"replied_in_gmail", "gmail_removed"}.issubset(actions), str(actions))
    finally:
        gmail_status.fetch_gmail_thread_state = original_fetch


if __name__ == "__main__":
    asyncio.run(main())
