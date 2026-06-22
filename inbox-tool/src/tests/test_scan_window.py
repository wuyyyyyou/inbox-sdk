"""扫描时间窗口的最小回归测试。"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


async def _collect_scan_queries(newer_than_days: int | None) -> list[str]:
    from mail_agent.core.scan import run_mail_scan
    from mail_agent.mail_providers.gmail import adapter

    original_list_threads_page = adapter.list_threads_page
    original_normalize_mailbox = adapter.normalize_mailbox
    queries: list[str] = []

    def fake_list_threads_page(
        mailbox: str,
        page_token: str | None = None,
        max_results: int = adapter.THREAD_PAGE_SIZE,
        query: str = "-in:chats",
    ) -> dict[str, Any]:
        queries.append(query)
        return {"threads": [], "nextPageToken": ""}

    try:
        adapter.normalize_mailbox = lambda mailbox: mailbox
        adapter.list_threads_page = fake_list_threads_page
        await run_mail_scan("hr@anna.partners", 20, newer_than_days=newer_than_days)
        return queries
    finally:
        adapter.list_threads_page = original_list_threads_page
        adapter.normalize_mailbox = original_normalize_mailbox


async def _collect_fallback_message_ids() -> list[str]:
    from mail_agent.core.scan import run_mail_scan
    from mail_agent.domain.types import MessageLite
    from mail_agent.mail_providers.gmail import adapter

    original_list_threads_page = adapter.list_threads_page
    original_list_messages = adapter.list_messages
    original_normalize_mailbox = adapter.normalize_mailbox
    original_to_message_lite = adapter._to_message_lite
    now = datetime.now(timezone(timedelta(hours=8)))
    recent_ms = int((now - timedelta(days=1)).timestamp() * 1000)
    old_ms = int((now - timedelta(days=8)).timestamp() * 1000)

    def fake_list_threads_page(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("offline")

    def fake_to_message_lite(message: dict[str, Any]) -> MessageLite:
        return MessageLite(
            message_id=str(message["id"]),
            thread_id=str(message.get("thread_id") or message["id"]),
            from_addr="sender@example.com",
            to_addr="hr@anna.partners",
            cc="",
            subject="subject",
            snippet="snippet",
            internal_date=str(message["internal_date"]),
            label_ids=[],
            unread=False,
            starred=False,
            important=False,
            has_attachment=False,
            headers={},
        )

    try:
        adapter.normalize_mailbox = lambda mailbox: mailbox
        adapter.list_threads_page = fake_list_threads_page
        adapter.list_messages = lambda mailbox: [
            {"id": "recent", "thread_id": "recent-thread", "internal_date": str(recent_ms)},
            {"id": "old", "thread_id": "old-thread", "internal_date": str(old_ms)},
        ]
        adapter._to_message_lite = fake_to_message_lite
        messages = await run_mail_scan("hr@anna.partners", 20, newer_than_days=7)
        return [message.message_id for message in messages]
    finally:
        adapter._to_message_lite = original_to_message_lite
        adapter.list_messages = original_list_messages
        adapter.list_threads_page = original_list_threads_page
        adapter.normalize_mailbox = original_normalize_mailbox


async def _collect_thread_message_ids_with_limit() -> list[str]:
    from mail_agent.core.scan import run_mail_scan
    from mail_agent.domain.types import MessageLite
    from mail_agent.mail_providers.gmail import adapter

    original_list_threads_page = adapter.list_threads_page
    original_fetch_thread_full = adapter.fetch_thread_full
    original_extract_messages_from_thread = adapter.extract_messages_from_thread
    original_normalize_message = adapter._normalize_message
    original_normalize_mailbox = adapter.normalize_mailbox
    original_to_message_lite = adapter._to_message_lite
    original_write_message = adapter.write_message
    original_read_message = adapter.read_message
    original_write_index = adapter.write_index

    raw_messages = [
        {"id": "m1", "thread_id": "t1", "internal_date": "1770000000001"},
        {"id": "m2", "thread_id": "t1", "internal_date": "1770000000002"},
        {"id": "m3", "thread_id": "t1", "internal_date": "1770000000003"},
    ]
    cache: dict[str, dict[str, Any]] = {}

    def fake_to_message_lite(message: dict[str, Any]) -> MessageLite:
        return MessageLite(
            message_id=str(message["id"]),
            thread_id=str(message.get("thread_id") or message["id"]),
            from_addr="sender@example.com",
            to_addr="hr@anna.partners",
            cc="",
            subject="subject",
            snippet="snippet",
            internal_date=str(message["internal_date"]),
            label_ids=[],
            unread=False,
            starred=False,
            important=False,
            has_attachment=False,
            headers={},
        )

    try:
        adapter.normalize_mailbox = lambda mailbox: mailbox
        adapter.list_threads_page = lambda *args, **kwargs: {"threads": [{"id": "t1"}], "nextPageToken": ""}
        adapter.fetch_thread_full = lambda mailbox, thread_id: {"id": thread_id, "messages": raw_messages}
        adapter.extract_messages_from_thread = lambda full: list(full["messages"])
        adapter._normalize_message = lambda mailbox, message: dict(message)
        adapter.write_message = lambda mailbox, message: cache.setdefault(str(message["id"]), dict(message))
        adapter.read_message = lambda mailbox, message_id: cache[message_id]
        adapter.write_index = lambda mailbox, summaries: None
        adapter._to_message_lite = fake_to_message_lite
        messages = await run_mail_scan("hr@anna.partners", 2, newer_than_days=None)
        return [message.message_id for message in messages]
    finally:
        adapter.write_index = original_write_index
        adapter.read_message = original_read_message
        adapter.write_message = original_write_message
        adapter._to_message_lite = original_to_message_lite
        adapter.normalize_mailbox = original_normalize_mailbox
        adapter._normalize_message = original_normalize_message
        adapter.extract_messages_from_thread = original_extract_messages_from_thread
        adapter.fetch_thread_full = original_fetch_thread_full
        adapter.list_threads_page = original_list_threads_page


def main() -> None:
    logging.getLogger("mail_agent.core.scan").setLevel(logging.CRITICAL)

    recent_queries = asyncio.run(_collect_scan_queries(7))
    check("recent scan query", recent_queries, ["-in:chats newer_than:7d"])
    if any("unread" in query.lower() for query in recent_queries):
        raise AssertionError(f"first scan query must include read and unread mail: {recent_queries!r}")

    default_queries = asyncio.run(_collect_scan_queries(None))
    check("default scan query", default_queries, ["-in:chats"])

    fallback_ids = asyncio.run(_collect_fallback_message_ids())
    check("fallback scan window", fallback_ids, ["recent"])

    limited_ids = asyncio.run(_collect_thread_message_ids_with_limit())
    check("message hard limit", limited_ids, ["m1", "m2"])

    print("PASS scan window tests")


if __name__ == "__main__":
    main()
