"""Focused storage test for Inbox thread assist and draft helpers."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class FakeStorageClient:
    def __init__(self):
        self._store: dict[str, dict[str, Any]] = {}
        self._generation = 1

    async def get(self, key: str, *, scope: str = "user", timeout: float = 30) -> dict[str, Any]:
        if key in self._store:
            stored = self._store[key]
            return {"exists": True, "value": stored["value"], "etag": stored["etag"]}
        return {"exists": False, "value": None, "etag": ""}

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
        if if_match and key in self._store and self._store[key]["etag"] != if_match:
            raise RuntimeError("etag mismatch")
        etag = f"etag_{self._generation}"
        self._generation += 1
        self._store[key] = {"etag": etag, "value": value}
        return {"etag": etag}

    async def delete(self, key: str, *, scope: str = "user", if_match: str | None = None, timeout: float = 30) -> dict[str, Any]:
        existed = key in self._store
        self._store.pop(key, None)
        return {"deleted": existed}

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
        items = [{"key": key} for key in self._store if not prefix or key.startswith(prefix)]
        return {"items": items, "next_cursor": None}


async def main() -> None:
    from mail_agent.storage.client import init
    from mail_agent.storage.ops import (
        delete_inbox_thread_draft,
        get_inbox_thread_assist,
        get_inbox_thread_draft,
        set_inbox_thread_assist,
        set_inbox_thread_draft,
    )

    fake = FakeStorageClient()
    init(fake, fake, scope="user")  # type: ignore[arg-type]

    mailbox = "test@example.com"
    thread_id = "thread-123"
    latest_message_id = "msg-999"

    assist = await get_inbox_thread_assist(mailbox, thread_id, latest_message_id)
    assert assist["exists"] is False

    saved_assist = await set_inbox_thread_assist(
        mailbox,
        thread_id,
        latest_message_id,
        {
            "overview": "Project update is waiting on confirmation.",
            "quick_replies": [{"id": "confirm", "label": "Confirm plan", "intent": "Confirm the timing."}],
        },
    )
    assert saved_assist.get("etag")

    loaded_assist = await get_inbox_thread_assist(mailbox, thread_id, latest_message_id)
    assert loaded_assist["exists"] is True
    assert loaded_assist["value"]["overview"] == "Project update is waiting on confirmation."
    assert loaded_assist["value"]["thread_id"] == thread_id
    assert loaded_assist["value"]["latest_message_id"] == latest_message_id

    saved_draft = await set_inbox_thread_draft(mailbox, thread_id, "Draft reply body")
    assert saved_draft.get("etag")
    loaded_draft = await get_inbox_thread_draft(mailbox, thread_id)
    assert loaded_draft["exists"] is True
    assert loaded_draft["value"]["body"] == "Draft reply body"

    deleted = await delete_inbox_thread_draft(mailbox, thread_id)
    assert deleted["deleted"] is True
    missing_draft = await get_inbox_thread_draft(mailbox, thread_id)
    assert missing_draft["exists"] is False

    print("PASS inbox thread storage helpers")


if __name__ == "__main__":
    asyncio.run(main())
