"""本地 Inbox 查询与 cache-only 过滤测试。"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mail_agent.domain.types import MessageLite
from mail_agent.local_query import (
    build_local_query_from_plan,
    filter_cached_messages,
    match_local_query,
    normalize_to_local_query,
    parse_local_query,
)


def _msg(**kwargs: object) -> MessageLite:
    defaults = dict(
        message_id="m1",
        thread_id="t1",
        from_addr="Alice <alice@example.com>",
        to_addr="me@example.com",
        cc="",
        subject="Invoice ready",
        snippet="Please pay soon",
        internal_date="1783814400000",
        label_ids=["INBOX", "UNREAD"],
        unread=True,
        starred=False,
        important=False,
        has_attachment=False,
        attachments=[],
        headers={},
    )
    defaults.update(kwargs)
    return MessageLite(**defaults)  # type: ignore[arg-type]


def test_parse_and_exclude_and_todo() -> None:
    parsed = parse_local_query("is:inbox AND invoice AND -from:noreply")
    assert not parsed.error
    assert match_local_query(_msg(), parsed)
    assert not match_local_query(_msg(from_addr="Bot <noreply@x.com>"), parsed)
    todo_parsed = parse_local_query("is:todo")
    assert match_local_query(_msg(message_id="todo-1"), todo_parsed, todo_ids=["todo-1"])
    assert not match_local_query(_msg(message_id="todo-1"), todo_parsed, todo_ids=[])
    print("[PASS] test_parse_and_exclude_and_todo")


def test_workflow_status_ids_override_gmail_labels() -> None:
    """实时 Gmail 命中可按前端工作流快照筛选，不依赖 Gmail 中不存在的标签。"""
    done = parse_local_query("is:done")
    snoozed = parse_local_query("is:snoozed")
    assert match_local_query(_msg(message_id="done-1"), done, done_ids=["done-1"])
    assert not match_local_query(_msg(message_id="done-1"), done, done_ids=[])
    assert match_local_query(_msg(message_id="snoozed-1"), snoozed, snoozed_ids=["snoozed-1"])
    assert not match_local_query(_msg(message_id="snoozed-1"), snoozed, snoozed_ids=[])
    print("[PASS] test_workflow_status_ids_override_gmail_labels")


def test_normalize_gmail_fragments() -> None:
    assert "is:inbox" in normalize_to_local_query("in:inbox newer_than:7d is:unread")
    assert not parse_local_query(normalize_to_local_query("in:inbox is:unread")).error
    print("[PASS] test_normalize_gmail_fragments")


def test_filter_cached_messages() -> None:
    messages = [
        _msg(message_id="a", subject="Hello", label_ids=["INBOX"]),
        _msg(message_id="b", subject="Invoice", label_ids=["INBOX"], from_addr="bot@x.com"),
        _msg(message_id="c", subject="Other", label_ids=["SENT"]),
    ]
    hits, parsed = filter_cached_messages(messages, "is:inbox AND invoice AND -from:bot", limit=10)
    assert not parsed.error
    assert [m.message_id for m in hits] == []
    hits2, _ = filter_cached_messages(messages, "is:inbox AND invoice", limit=10)
    assert [m.message_id for m in hits2] == ["b"]
    print("[PASS] test_filter_cached_messages")


def test_build_local_query_from_plan() -> None:
    class Plan:
        direction = "inbox"
        gmail_flags = ["is:unread"]
        people = [{"name_hint": "Alice", "role": "sender"}]
        topics = [{"search_terms": ["invoice"]}]
        timeframe = "7d"

    query = build_local_query_from_plan(Plan())
    assert "is:inbox" in query
    assert "from:Alice" in query or "from:alice" in query.lower()
    assert "invoice" in query.lower()
    assert not parse_local_query(query).error
    print("[PASS] test_build_local_query_from_plan")


if __name__ == "__main__":
    test_parse_and_exclude_and_todo()
    test_workflow_status_ids_override_gmail_labels()
    test_normalize_gmail_fragments()
    test_filter_cached_messages()
    test_build_local_query_from_plan()
    print("[ALL TESTS PASSED]")
