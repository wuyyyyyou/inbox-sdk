"""search_indexed_emails 的 cache-only 后端测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from anna_inbox_executa.gmail_tools import search_indexed_emails
from anna_inbox_executa.gmail_tools import CACHED_FEED_RESPONSE_MAX_BYTES, _cached_rpc_frame_size
from mail_agent.domain.types import MessageLite
from mail_agent.mail_providers.gmail.adapter import _to_message_lite


def _message(message_id: str, subject: str) -> MessageLite:
    return MessageLite(
        message_id=message_id, thread_id="thread-" + message_id,
        from_addr="alice@example.com", to_addr="me@example.com", cc="",
        subject=subject, snippet="indexed snippet", internal_date="1",
        label_ids=["INBOX"], unread=False, starred=False, important=False,
        has_attachment=False, attachments=[], headers={},
    )


def test_search_indexed_emails_uses_cache_and_workflow_ids() -> None:
    cached = [_message("todo-1", "Project update"), _message("other", "Project old")]
    rows = [{"id": "todo-1", "thread_id": "t1", "subject": "Project update", "label_ids": ["INBOX"]},
            {"id": "other", "thread_id": "t2", "subject": "Project old", "label_ids": ["INBOX"]}]
    with patch("mail_agent.mail_providers.gmail.adapter.list_all_cached_messages_lite", return_value=cached), \
         patch("mail_agent.mail_providers.gmail.adapter.list_messages", return_value=rows), \
         patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="me@example.com"), \
         patch("anna_inbox_executa.gmail_tools.gmail_request", side_effect=AssertionError("must not call Gmail")):
        result = search_indexed_emails("me@example.com", "is:todo AND project", ["todo-1"], [], [], 10)

    assert result["error"] == ""
    assert result["total"] == 1
    assert result["has_more"] is False
    assert [item["id"] for item in result["messages"]] == ["todo-1"]


def test_search_indexed_emails_matches_attachments_only_without_gmail() -> None:
    """主页索引仅保留 attachments 时，has:attachment 仍应命中且不回源。"""
    rows = [
        {
            "id": "with-attachment",
            "thread_id": "thread-with-attachment",
            "subject": "Indexed attachment",
            "label_ids": ["INBOX"],
            "attachments": [{"filename": "invoice.pdf"}],
        },
        {
            "id": "without-attachment",
            "thread_id": "thread-without-attachment",
            "subject": "Indexed message",
            "label_ids": ["INBOX"],
        },
    ]
    with patch("mail_agent.mail_providers.gmail.adapter.list_messages", return_value=rows), \
         patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="me@example.com"), \
         patch("anna_inbox_executa.gmail_tools.gmail_request", side_effect=AssertionError("must not call Gmail")):
        result = search_indexed_emails("me@example.com", "has:attachment", [], [], [], 10)

    assert result["error"] == ""
    assert result["total"] == 1
    assert [item["id"] for item in result["messages"]] == ["with-attachment"]


def test_search_indexed_emails_bounds_large_pages_and_advances_offsets() -> None:
    """大字段索引必须受 48KiB 帧预算限制，并按实际返回条数连续分页。"""
    rows = [
        {
            "id": f"message-{index:03d}",
            "thread_id": f"thread-{index:03d}",
            "internal_date": str(200 - index),
            "from": "sender@example.com " + "f" * 500,
            "to": "recipient@example.com " + "t" * 500,
            "subject": "Project " + "s" * 500,
            "snippet": "n" * 120,
            "body_preview": "b" * 120,
            "label_ids": ["INBOX"],
        }
        for index in range(200)
    ]
    with patch("mail_agent.mail_providers.gmail.adapter.list_messages", return_value=rows), \
         patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="me@example.com"), \
         patch("anna_inbox_executa.gmail_tools.gmail_request", side_effect=AssertionError("must not call Gmail")):
        first_page = search_indexed_emails("me@example.com", "project", [], [], [], 200, 0)
        second_page = search_indexed_emails(
            "me@example.com", "project", [], [], [], 200, first_page["next_offset"]
        )

    assert 0 < len(first_page["messages"]) < 200
    assert first_page["total"] == 200
    assert first_page["offset"] == 0
    assert first_page["next_offset"] == len(first_page["messages"])
    assert first_page["has_more"] is True
    assert _cached_rpc_frame_size("search_indexed_emails", first_page) <= CACHED_FEED_RESPONSE_MAX_BYTES
    assert second_page["offset"] == first_page["next_offset"]
    assert second_page["messages"][0]["id"] == f"message-{second_page['offset']:03d}"
    assert second_page["messages"][0]["id"] != first_page["messages"][0]["id"]


def test_to_message_lite_preserves_legacy_attachment_flags() -> None:
    """旧缓存只有 has_attachment 或 attachment_count 时也应设置 Lite 标记。"""
    common = {
        "id": "legacy",
        "thread_id": "thread-legacy",
        "subject": "Legacy attachment",
        "label_ids": [],
    }
    assert _to_message_lite({**common, "has_attachment": True}).has_attachment is True
    assert _to_message_lite({**common, "attachment_count": 1}).has_attachment is True
