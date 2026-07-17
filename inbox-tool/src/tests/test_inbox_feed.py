"""Homepage Gmail feed regression tests (no network or LLM)."""

from __future__ import annotations

import sys
import tempfile
import json
from pathlib import Path
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> None:
    from anna_inbox_executa.gmail_tools import (
        CACHED_EMAIL_BODY_MAX_CHARS,
        CACHED_FEED_RESPONSE_MAX_BYTES,
        _cached_rpc_frame_size,
        get_cached_email,
        list_cached_emails,
        list_gmail_emails_page,
        list_inbox_emails,
    )
    from mail_agent.mail_providers.gmail.adapter import (
        ATTACHMENT_SCAN_VERSION,
        SUMMARY_METADATA_REFRESH_SECONDS,
        clear_mailbox_cache,
        ensure_cached_feed_index,
        fetch_message_summary,
        get_cached_feed_page,
        live_search_metadata_and_cache,
        search_gmail,
        sync_cached_message_summaries,
        write_index,
        write_message,
        update_thread_state,
    )

    captured: dict[str, object] = {}

    def fake_search(mailbox: str, query: str, limit: int) -> list[str]:
        captured.update(mailbox=mailbox, query=query, limit=limit)
        return ["new", "old"]

    messages = [
        {
            "id": "old", "thread_id": "t-old", "internal_date": "1", "from": "Old <old@example.com>",
            "subject": "Old", "snippet": "Earlier", "label_ids": ["INBOX"], "attachments": [],
        },
        {
            "id": "new", "thread_id": "t-new", "internal_date": "2", "from": "New <new@example.com>",
            "to": "User <user@example.com>", "subject": "Important", "snippet": "Newest", "body_preview": "Preview", "label_ids": ["INBOX", "UNREAD", "IMPORTANT"],
            "attachments": [{"filename": "brief.pdf"}],
        },
    ]

    with (
        patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
        patch("mail_agent.mail_providers.gmail.adapter.gmail_request", return_value={"emailAddress": "user@example.com"}),
        patch("mail_agent.mail_providers.gmail.adapter.clear_mailbox_cache", return_value={"mailbox": "user@example.com"}) as clear_cache,
        patch("mail_agent.mail_providers.gmail.adapter.live_search_metadata_and_cache", side_effect=fake_search),
        patch("mail_agent.mail_providers.gmail.adapter.list_messages", return_value=messages),
    ):
        result = list_inbox_emails("USER@example.com", 7, 100)
        starred = list_inbox_emails("USER@example.com", 30, 50, "starred")
        all_mail = list_inbox_emails("USER@example.com", 7, 500, "all", True)

    # 刷新固定走 All mail query（含 days 窗口），与请求 category 无关
    assert captured == {"mailbox": "user@example.com", "query": "in:anywhere -in:chats newer_than:7d", "limit": 500}
    assert [item["id"] for item in result["messages"]] == ["new", "old"]
    assert result["messages"][0]["unread"] is True
    assert result["messages"][0]["important"] is True
    assert result["messages"][0]["attachment_count"] == 1
    assert result["messages"][0]["to"] == "User <user@example.com>"
    assert result["messages"][0]["snippet"] == "Newest"
    assert result["messages"][0]["body_preview"] == "Preview"
    assert "body_text" not in result["messages"][0]
    assert starred["category"] == "starred"
    assert starred["query"] == "in:anywhere -in:chats newer_than:30d"
    assert all_mail["category"] == "all"
    assert all_mail["cache_reset"] == {"mailbox": "user@example.com"}
    assert "has_more" in result
    assert "next_offset" in result
    assert result["cached_total"] == 2
    clear_cache.assert_called_once_with("user@example.com")

    default_query: dict[str, object] = {}

    def capture_default_query(mailbox: str, query: str, limit: int) -> list[str]:
        default_query.update(mailbox=mailbox, query=query, limit=limit)
        return ["new"]

    with (
        patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
        patch("mail_agent.mail_providers.gmail.adapter.gmail_request", return_value={"emailAddress": "user@example.com"}),
        patch("mail_agent.mail_providers.gmail.adapter.live_search_metadata_and_cache", side_effect=capture_default_query),
        patch("mail_agent.mail_providers.gmail.adapter.list_messages", return_value=messages),
    ):
        default_feed = list_inbox_emails("USER@example.com")
    assert default_feed["days"] == 30
    assert default_query == {"mailbox": "user@example.com", "query": "in:anywhere -in:chats newer_than:30d", "limit": 100}

    # list_inbox_emails 响应帧必须受 48KiB 预算约束（缓存仍可写入更多）
    oversized_messages = [
        {
            "id": f"big-{index}",
            "thread_id": f"thread-{index}",
            "internal_date": str(1_720_000_000_000 - index),
            "from": f"Sender {index} <sender-{index}@example.com> " + ("f" * 400),
            "to": "recipient@example.com " + ("t" * 400),
            "subject": "s" * 500,
            "snippet": "p" * 120,
            "body_preview": "b" * 120,
            "label_ids": ["INBOX", "UNREAD"],
            "attachments": [],
        }
        for index in range(100)
    ]
    oversized_ids = [item["id"] for item in oversized_messages]
    with (
        patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
        patch("mail_agent.mail_providers.gmail.adapter.gmail_request", return_value={"emailAddress": "user@example.com"}),
        patch("mail_agent.mail_providers.gmail.adapter.live_search_metadata_and_cache", return_value=oversized_ids),
        patch("mail_agent.mail_providers.gmail.adapter.list_messages", return_value=oversized_messages),
    ):
        bounded_feed = list_inbox_emails("USER@example.com", 30, 100, "all")
    assert 0 < bounded_feed["count"] < 100
    assert bounded_feed["has_more"] is True
    assert bounded_feed["cached_total"] == 100
    assert _cached_rpc_frame_size("list_inbox_emails", bounded_feed) <= CACHED_FEED_RESPONSE_MAX_BYTES

    thread_subject_messages = [
        {
            "id": "thread-original",
            "thread_id": "thread-title",
            "internal_date": "10",
            "from": "User <user@example.com>",
            "subject": "Original invite title",
            "snippet": "Original",
            "label_ids": ["SENT"],
            "attachments": [],
        },
        {
            "id": "thread-latest",
            "thread_id": "thread-title",
            "internal_date": "20",
            "from": "Sender <sender@example.com>",
            "subject": "Changed latest title",
            "snippet": "Latest",
            "label_ids": ["INBOX"],
            "has_attachment": True,
            "attachment_count": 1,
            "attachments": [],
        },
    ]
    with (
        patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
        patch("mail_agent.mail_providers.gmail.adapter.gmail_request", return_value={"emailAddress": "user@example.com"}),
        patch("mail_agent.mail_providers.gmail.adapter.live_search_metadata_and_cache", return_value=["thread-latest"]),
        patch("mail_agent.mail_providers.gmail.adapter.list_messages", return_value=thread_subject_messages),
    ):
        thread_title_feed = list_inbox_emails("USER@example.com", 7, 100)
    assert thread_title_feed["messages"][0]["subject"] == "Original invite title"
    assert thread_title_feed["messages"][0]["latest_subject"] == "Changed latest title"
    assert thread_title_feed["messages"][0]["has_attachment"] is True
    assert thread_title_feed["messages"][0]["attachment_count"] == 1

    # 同线程旧邮件有附件、最新回信无附件时，列表行仍应显示回形针
    thread_attachment_messages = [
        {
            "id": "thread-att-old",
            "thread_id": "thread-att",
            "internal_date": "10",
            "from": "Sender <sender@example.com>",
            "subject": "Invoice",
            "snippet": "See attached",
            "label_ids": ["INBOX"],
            "attachments": [{"filename": "invoice.pdf"}],
        },
        {
            "id": "thread-att-new",
            "thread_id": "thread-att",
            "internal_date": "20",
            "from": "User <user@example.com>",
            "subject": "Re: Invoice",
            "snippet": "Thanks",
            "label_ids": ["INBOX", "SENT"],
            "attachments": [],
        },
    ]
    with (
        patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
        patch("mail_agent.mail_providers.gmail.adapter.gmail_request", return_value={"emailAddress": "user@example.com"}),
        patch("mail_agent.mail_providers.gmail.adapter.live_search_metadata_and_cache", return_value=["thread-att-new"]),
        patch("mail_agent.mail_providers.gmail.adapter.list_messages", return_value=thread_attachment_messages),
    ):
        thread_attachment_feed = list_inbox_emails("USER@example.com", 7, 100)
    assert thread_attachment_feed["messages"][0]["id"] == "thread-att-new"
    assert thread_attachment_feed["messages"][0]["has_attachment"] is True
    assert thread_attachment_feed["messages"][0]["attachment_count"] == 1

    # 详情补全附件后必须同步更新旧的目录摘要与分页缓存。
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_root = Path(temp_dir)
        stale_summary = {
            "id": "detail-attachment",
            "thread_id": "thread-detail",
            "internal_date": "30",
            "from": "Sender <sender@example.com>",
            "subject": "Attachment from detail",
            "label_ids": ["INBOX"],
            "attachments": [],
        }
        detail_message = {
            **stale_summary,
            "attachments": [{"filename": "guide.pdf", "mimeType": "application/pdf", "attachmentId": "guide-1"}],
        }
        with (
            patch("mail_agent.mail_providers.gmail.adapter.cache_dir", return_value=cache_root),
            patch("mail_agent.mail_providers.gmail.adapter._storage_cache_enabled", return_value=False),
        ):
            write_index("user@example.com", [stale_summary])
            write_message("user@example.com", detail_message)
            assert get_cached_feed_page("user@example.com", 0)["messages"][0]["has_attachment"] is False
            assert sync_cached_message_summaries("user@example.com", [detail_message]) is True
            repaired = get_cached_feed_page("user@example.com", 0)["messages"][0]
        assert repaired["has_attachment"] is True
        assert repaired["attachment_count"] == 1

    with tempfile.TemporaryDirectory() as temp_dir:
        cache_root = Path(temp_dir)
        target = cache_root / "user_example.com"
        sibling = cache_root / "other_example.com"
        target.mkdir()
        sibling.mkdir()
        (target / "index.json").write_text("{}", encoding="utf-8")
        (sibling / "index.json").write_text("{}", encoding="utf-8")
        with (
            patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
            patch("mail_agent.mail_providers.gmail.adapter.cache_dir", return_value=cache_root),
            patch("mail_agent.mail_providers.gmail.adapter._storage_cache_enabled", return_value=False),
        ):
            cleared = clear_mailbox_cache("USER@example.com")
        assert cleared["deleted_local_cache"] is True
        assert not target.exists()
        assert sibling.exists()

    now_ms = 1_720_000_000_000
    recent_messages = [
        dict(messages[0], internal_date=str(now_ms - 20 * 24 * 60 * 60 * 1000), label_ids=["TRASH"]),
        dict(messages[1], internal_date=str(now_ms - 2 * 24 * 60 * 60 * 1000)),
        {
            "id": "star", "thread_id": "t-star", "internal_date": str(now_ms - 1 * 24 * 60 * 60 * 1000),
            "from": "Star <star@example.com>", "subject": "Flagged", "snippet": "Flagged", "label_ids": ["STARRED"], "attachments": [],
        },
    ]
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_root = Path(temp_dir)
        with (
            patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
            patch("mail_agent.mail_providers.gmail.adapter.read_cache", return_value={"updated_at": "now", "messages": recent_messages}),
            patch("mail_agent.mail_providers.gmail.adapter.cache_dir", return_value=cache_root),
            patch("mail_agent.mail_providers.gmail.adapter.cache_debug_info", return_value={"backend": "local"}),
            patch("anna_inbox_executa.gmail_tools.time.time", return_value=now_ms / 1000),
        ):
            cached_feed = list_cached_emails("USER@example.com", 7, 10, "all")
            starred_cached_feed = list_cached_emails("USER@example.com", 7, 1, "starred")
    cached_new = next(item for item in cached_feed["messages"] if item["id"] == "new")
    assert cached_feed["category"] == "all"
    assert starred_cached_feed["category"] == "starred"
    assert starred_cached_feed["count"] == 1
    assert starred_cached_feed["offset"] == 0
    assert starred_cached_feed["next_offset"] == 1
    assert starred_cached_feed["has_more"] is False
    assert starred_cached_feed["messages"][0]["id"] == "star"
    assert cached_new["important"] is True
    assert cached_new["unread"] is True
    assert cached_new["attachment_count"] == 1

    long_text = "x" * 300
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_root = Path(temp_dir)
        with (
            patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
            patch("mail_agent.mail_providers.gmail.adapter.read_cache", return_value={
                "updated_at": "now",
                "messages": [{
                    "id": "long",
                    "thread_id": "t-long",
                    "internal_date": str(now_ms),
                    "from": "Long <long@example.com>",
                    "subject": "Long",
                    "snippet": long_text,
                    "body_preview": long_text,
                    "label_ids": ["INBOX"],
                    "attachments": [],
                }],
            }),
            patch("mail_agent.mail_providers.gmail.adapter.cache_dir", return_value=cache_root),
            patch("mail_agent.mail_providers.gmail.adapter.cache_debug_info", return_value={"backend": "local"}),
            patch("anna_inbox_executa.gmail_tools.time.time", return_value=now_ms / 1000),
        ):
            compact_feed = list_cached_emails("USER@example.com", 7, 10, "all")
    assert compact_feed["messages"][0]["snippet"] == "x" * 120
    assert compact_feed["messages"][0]["body_preview"] == "x" * 120

    paged_messages = [
        {
            "id": f"page-{index}",
            "thread_id": f"thread-{index}",
            "internal_date": str(now_ms - index),
            "from": "Sender <sender@example.com>",
            "subject": f"Message {index}",
            "snippet": "Preview",
            "label_ids": ["INBOX"],
            "attachments": [],
        }
        for index in range(205)
    ]
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_root = Path(temp_dir)
    with (
        patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
        patch("mail_agent.mail_providers.gmail.adapter.read_cache", return_value={"updated_at": "now", "messages": paged_messages}),
        patch("mail_agent.mail_providers.gmail.adapter.cache_dir", return_value=cache_root),
        patch("mail_agent.mail_providers.gmail.adapter.cache_debug_info", return_value={"backend": "local"}),
        patch("anna_inbox_executa.gmail_tools.time.time", return_value=now_ms / 1000),
    ):
            first_page = list_cached_emails("USER@example.com", 30, 500, "inbox", 0)
            second_page = list_cached_emails("USER@example.com", 30, 100, "inbox", 100)
            final_page = list_cached_emails("USER@example.com", 30, 100, "inbox", 200)
            all_time_page = list_cached_emails("USER@example.com", 0, 100, "inbox", 200)
    assert first_page["count"] == 100
    assert first_page["has_more"] is True
    assert first_page["next_offset"] == 100
    assert second_page["messages"][0]["id"] == "page-100"
    assert final_page["count"] == 5
    assert final_page["has_more"] is False
    assert final_page["next_offset"] == 205
    assert all_time_page["days"] == 0
    assert all_time_page["messages"][0]["id"] == "page-200"

    with tempfile.TemporaryDirectory() as temp_dir:
        cache_root = Path(temp_dir)
        with (
            patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
            patch("mail_agent.mail_providers.gmail.adapter.cache_dir", return_value=cache_root),
        ):
            write_index("user@example.com", paged_messages)
            feed_meta = ensure_cached_feed_index("user@example.com")
            feed_page = get_cached_feed_page("user@example.com", 1)
        assert feed_meta["page_count"] == 3
        assert feed_page["count"] == 100
        assert feed_page["messages"][0]["id"] == "page-100"

    large_page_messages = [
        {
            **item,
            "from": f"Sender {index} <sender-{index}@example.com> " + "f" * 400,
            "to": "recipient@example.com " + "t" * 400,
            "subject": "s" * 500,
            "snippet": "p" * 120,
            "body_preview": "b" * 120,
        }
        for index, item in enumerate(paged_messages[:100])
    ]
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_root = Path(temp_dir)
        with (
            patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
            patch("mail_agent.mail_providers.gmail.adapter.read_cache", return_value={"updated_at": "now", "messages": large_page_messages}),
            patch("mail_agent.mail_providers.gmail.adapter.cache_dir", return_value=cache_root),
            patch("mail_agent.mail_providers.gmail.adapter.cache_debug_info", return_value={"backend": "local"}),
            patch("anna_inbox_executa.gmail_tools.time.time", return_value=now_ms / 1000),
        ):
            byte_bounded_page = list_cached_emails("USER@example.com", 30, 100, "inbox", 0)
    assert 0 < byte_bounded_page["count"] < 100
    assert byte_bounded_page["next_offset"] == byte_bounded_page["count"]
    assert byte_bounded_page["has_more"] is True
    assert _cached_rpc_frame_size("list_cached_emails", byte_bounded_page) <= CACHED_FEED_RESPONSE_MAX_BYTES

    gmail_pages = [
        {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "page-2"},
        {"messages": [{"id": "m3"}]},
    ]
    with patch("mail_agent.mail_providers.gmail.adapter.gmail_request", side_effect=gmail_pages) as request:
        ids = search_gmail("user@example.com", "in:anywhere -in:chats newer_than:7d", 500)
    assert ids == ["m1", "m2", "m3"]
    assert request.call_args_list[0].args[2]["includeSpamTrash"] == "true"
    assert request.call_args_list[1].args[2]["pageToken"] == "page-2"

    metadata_message = {
        "id": "meta-1",
        "threadId": "thread-meta-1",
        "historyId": "history-meta-1",
        "internalDate": str(now_ms),
        "snippet": "Snippet text",
        "labelIds": ["INBOX", "UNREAD"],
        "sizeEstimate": 1234,
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Meta Sender <meta@example.com>"},
                {"name": "To", "value": "user@example.com"},
                {"name": "Subject", "value": "Metadata only"},
                {"name": "Date", "value": "Mon, 01 Jul 2024 10:00:00 +0000"},
            ],
            "parts": [
                {
                    "mimeType": "multipart/related",
                    "parts": [{
                        "mimeType": "multipart/alternative",
                        "parts": [{"filename": "brief.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "att-1", "size": 42}}],
                    }],
                },
            ],
        },
    }
    with patch("mail_agent.mail_providers.gmail.adapter.gmail_request", return_value=metadata_message) as metadata_request:
        summary = fetch_message_summary("user@example.com", "meta-1")
    assert summary is not None
    assert summary["from"] == "Meta Sender <meta@example.com>"
    assert summary["attachments"][0]["attachmentId"] == "att-1"
    assert summary["body_cached"] is False
    assert summary["headers_complete"] is True
    assert summary["attachment_scan_version"] == ATTACHMENT_SCAN_VERSION
    request_params = metadata_request.call_args.args[2]
    assert request_params["format"] == "full"
    assert "metadataHeaders" not in request_params
    assert "body(data)" not in request_params["fields"]
    assert "attachmentId" in request_params["fields"]

    stale_age = SUMMARY_METADATA_REFRESH_SECONDS - 60
    existing_summary = {
        "id": "cached-1",
        "from": "Cached <cached@example.com>",
        "headers_complete": True,
        "metadata_refreshed_at": int(now_ms / 1000) - stale_age,
        "attachment_scan_version": ATTACHMENT_SCAN_VERSION,
        "internal_date": str(now_ms),
    }
    with (
        patch("mail_agent.mail_providers.gmail.adapter.search_gmail", return_value=["cached-1"]),
        patch("mail_agent.mail_providers.gmail.adapter.read_cache", return_value={"messages": [existing_summary]}),
        patch("mail_agent.mail_providers.gmail.adapter.fetch_message_summary") as refresh_summary,
        patch("mail_agent.mail_providers.gmail.adapter.write_index"),
        patch("mail_agent.mail_providers.gmail.adapter.time.time", return_value=now_ms / 1000),
    ):
        ids = live_search_metadata_and_cache("user@example.com", "in:inbox", 10)
    assert ids == ["cached-1"]
    refresh_summary.assert_not_called()

    # 历史摘要没有附件扫描版本时，即使仍在普通刷新窗口内，也必须在首次同步补扫。
    legacy_summary = {**existing_summary, "id": "legacy-attachment", "attachment_scan_version": 0}
    refreshed_legacy = {
        **legacy_summary,
        "attachments": [{"filename": "legacy.pdf", "mimeType": "application/pdf", "attachmentId": "legacy-1"}],
        "attachment_scan_version": ATTACHMENT_SCAN_VERSION,
    }
    with (
        patch("mail_agent.mail_providers.gmail.adapter.search_gmail", return_value=["legacy-attachment"]),
        patch("mail_agent.mail_providers.gmail.adapter.read_cache", return_value={"messages": [legacy_summary]}),
        patch("mail_agent.mail_providers.gmail.adapter.fetch_message_summary", return_value=refreshed_legacy) as refresh_summary,
        patch("mail_agent.mail_providers.gmail.adapter.write_index") as write_index,
        patch("mail_agent.mail_providers.gmail.adapter.time.time", return_value=now_ms / 1000),
    ):
        ids = live_search_metadata_and_cache("user@example.com", "in:inbox", 10)
    assert ids == ["legacy-attachment"]
    refresh_summary.assert_called_once_with("user@example.com", "legacy-attachment")
    assert write_index.call_args.args[1][0]["attachments"][0]["filename"] == "legacy.pdf"

    fetched_summaries = {
        "gmail-2": {
            "id": "gmail-2",
            "thread_id": "thread-2",
            "internal_date": str(now_ms - 2),
            "from": "Two <two@example.com>",
            "subject": "Second",
            "snippet": "second",
            "label_ids": ["INBOX"],
            "attachments": [],
        },
        "gmail-3": {
            "id": "gmail-3",
            "thread_id": "thread-3",
            "internal_date": str(now_ms - 3),
            "from": "Three <three@example.com>",
            "subject": "Third",
            "snippet": "third",
            "label_ids": ["INBOX"],
            "attachments": [],
        },
    }
    with (
        patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
        patch("mail_agent.mail_providers.gmail.adapter.gmail_request", return_value={"messages": [{"id": "gmail-1"}, {"id": "gmail-2"}, {"id": "gmail-3"}], "nextPageToken": "next-page"}) as gmail_request,
        patch("mail_agent.mail_providers.gmail.adapter.fetch_message_summary", side_effect=lambda mailbox, message_id: fetched_summaries.get(message_id)),
        patch("mail_agent.mail_providers.gmail.adapter.read_cache", return_value={"messages": []}),
        patch("mail_agent.mail_providers.gmail.adapter.write_index") as write_index,
        patch("mail_agent.mail_providers.gmail.adapter.message_summary", side_effect=lambda item: item),
    ):
        all_mail_page = list_gmail_emails_page("USER@example.com", 30, 100, "all", "", 0, ["gmail-1"])
    assert [item["id"] for item in all_mail_page["messages"]] == ["gmail-2", "gmail-3"]
    assert all_mail_page["page_token"] == "next-page"
    assert all_mail_page["page_offset"] == 0
    assert all_mail_page["has_more"] is True
    assert gmail_request.call_args.args[2]["q"] == "in:anywhere -in:chats"
    assert gmail_request.call_args.args[2]["includeSpamTrash"] == "true"
    write_index.assert_called()

    with (
        patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
        patch("mail_agent.mail_providers.gmail.adapter.gmail_request", return_value={"messages": [{"id": "gmail-2"}]}) as gmail_request,
        patch("mail_agent.mail_providers.gmail.adapter.fetch_message_summary", return_value=fetched_summaries["gmail-2"]),
        patch("mail_agent.mail_providers.gmail.adapter.read_cache", return_value={"messages": []}),
        patch("mail_agent.mail_providers.gmail.adapter.write_index"),
        patch("mail_agent.mail_providers.gmail.adapter.message_summary", side_effect=lambda item: item),
    ):
        # category=inbox 仅诊断字段；Gmail query 仍固定 All mail
        inbox_page = list_gmail_emails_page("USER@example.com", 30, 100, "inbox")
    assert [item["id"] for item in inbox_page["messages"]] == ["gmail-2"]
    assert inbox_page["has_more"] is False
    assert gmail_request.call_args.args[2]["q"] == "in:anywhere -in:chats"

    with (
        patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
        patch("mail_agent.mail_providers.gmail.adapter.gmail_request", return_value={"messages": [{"id": "gmail-2"}]}) as gmail_request,
        patch("mail_agent.mail_providers.gmail.adapter.fetch_message_summary", return_value=fetched_summaries["gmail-2"]),
        patch("mail_agent.mail_providers.gmail.adapter.read_cache", return_value={"messages": []}),
        patch("mail_agent.mail_providers.gmail.adapter.write_index"),
        patch("mail_agent.mail_providers.gmail.adapter.message_summary", side_effect=lambda item: item),
    ):
        all_time_inbox_page = list_gmail_emails_page("USER@example.com", 0, 100, "inbox")
    assert [item["id"] for item in all_time_inbox_page["messages"]] == ["gmail-2"]
    assert all_time_inbox_page["has_more"] is False
    assert gmail_request.call_args.args[2]["q"] == "in:anywhere -in:chats"

    out_of_order_summaries = {
        "gmail-new": {
            "id": "gmail-new",
            "thread_id": "thread-new",
            "internal_date": str(now_ms),
            "from": "New <new@example.com>",
            "subject": "Newest",
            "snippet": "new",
            "label_ids": ["INBOX"],
            "attachments": [],
        },
        "gmail-old": {
            "id": "gmail-old",
            "thread_id": "thread-old",
            "internal_date": str(now_ms - 10),
            "from": "Old <old@example.com>",
            "subject": "Older",
            "snippet": "old",
            "label_ids": ["INBOX"],
            "attachments": [],
        },
    }
    with (
        patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
        patch("mail_agent.mail_providers.gmail.adapter.gmail_request", return_value={"messages": [{"id": "gmail-old"}, {"id": "gmail-new"}]}),
        patch("mail_agent.mail_providers.gmail.adapter.fetch_message_summary", side_effect=lambda mailbox, message_id: out_of_order_summaries.get(message_id)),
        patch("mail_agent.mail_providers.gmail.adapter.read_cache", return_value={"messages": []}),
        patch("mail_agent.mail_providers.gmail.adapter.write_index"),
        patch("mail_agent.mail_providers.gmail.adapter.message_summary", side_effect=lambda item: item),
    ):
        ordered_page = list_gmail_emails_page("USER@example.com", 30, 100, "all")
    assert [item["id"] for item in ordered_page["messages"]] == ["gmail-new", "gmail-old"]

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    thread_payload = {"id": "thread-1", "messages": [{"id": "m1"}, {"id": "m2"}]}
    with (
        patch("mail_agent.mail_providers.gmail.adapter.get_access_token", return_value="test-token"),
        patch("mail_agent.mail_providers.gmail.adapter.urllib.request.urlopen", return_value=FakeResponse(thread_payload)) as urlopen,
        patch("mail_agent.mail_providers.gmail.adapter.patch_cached_message_labels") as patch_labels,
    ):
        updated = update_thread_state("user@example.com", "thread-1", "mark_unread")
    request_object = urlopen.call_args.args[0]
    assert request_object.full_url.endswith("/users/me/threads/thread-1/modify")
    assert json.loads(request_object.data) == {"addLabelIds": ["UNREAD"], "removeLabelIds": []}
    assert updated["message_ids"] == ["m1", "m2"]
    patch_labels.assert_called_once_with("user@example.com", ["m1", "m2"], add_label_ids=["UNREAD"], remove_label_ids=[])

    with (
        patch("mail_agent.mail_providers.gmail.adapter.get_access_token", return_value="test-token"),
        patch("mail_agent.mail_providers.gmail.adapter.urllib.request.urlopen", return_value=FakeResponse(thread_payload)) as urlopen,
        patch("mail_agent.mail_providers.gmail.adapter.patch_cached_message_labels") as patch_labels,
    ):
        update_thread_state("user@example.com", "thread-1", "untrash")
    requests = [call.args[0] for call in urlopen.call_args_list]
    assert len(requests) == 1
    assert requests[0].full_url.endswith("/users/me/threads/thread-1/untrash")
    patch_labels.assert_called_once_with("user@example.com", ["m1", "m2"], add_label_ids=[], remove_label_ids=["TRASH"])

    fetched = {"id": "missing", "body_text": "Fetched body"}
    with (
        patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
        patch("mail_agent.mail_providers.gmail.adapter.read_message", side_effect=ValueError("not cached")),
        patch("mail_agent.mail_providers.gmail.adapter.fetch_and_cache_message", return_value=fetched),
        patch("mail_agent.mail_providers.gmail.adapter.cache_debug_info", return_value={"backend": "local"}),
    ):
        detail = get_cached_email("USER@example.com", "missing")
    assert detail["message"]["body_text"] == "Fetched body"
    assert detail["message"]["body_truncated"] is False

    oversized = {
        "id": "oversized",
        "thread_id": "thread-oversized",
        "body_text": "x" * (CACHED_EMAIL_BODY_MAX_CHARS + 5000),
        "payload": {"body": {"data": "y" * 100_000}},
        "raw_headers": {"received": "z" * 10_000},
    }
    with (
        patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
        patch("mail_agent.mail_providers.gmail.adapter.read_message", return_value=oversized),
    ):
        bounded_detail = get_cached_email("USER@example.com", "oversized")
    assert len(bounded_detail["message"]["body_text"]) == CACHED_EMAIL_BODY_MAX_CHARS
    assert bounded_detail["message"]["body_truncated"] is True
    assert "payload" not in bounded_detail["message"]
    assert "raw_headers" not in bounded_detail["message"]
    assert _cached_rpc_frame_size("get_cached_email", bounded_detail) <= CACHED_FEED_RESPONSE_MAX_BYTES

    gmail_refs = {
        "messages": [{"id": "gmail-1"}, {"id": "gmail-2"}, {"id": "gmail-3"}],
        "nextPageToken": "gmail-next",
    }

    def fake_gmail_summary(_mailbox: str, message_id: str) -> dict[str, object]:
        return {
            "id": message_id,
            "thread_id": f"thread-{message_id}",
            "internal_date": str(now_ms),
            "from": "Sender <sender@example.com>",
            "subject": message_id,
            "label_ids": ["INBOX"],
        }

    with (
        patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
        patch("mail_agent.mail_providers.gmail.adapter.gmail_request", return_value=gmail_refs) as gmail_page_request,
        patch("mail_agent.mail_providers.gmail.adapter.fetch_message_summary", side_effect=fake_gmail_summary) as fetch_summary,
        patch("mail_agent.mail_providers.gmail.adapter.read_cache", return_value={"messages": []}),
        patch("mail_agent.mail_providers.gmail.adapter.write_index") as write_index,
        patch("mail_agent.mail_providers.gmail.adapter.message_summary", side_effect=lambda item: item),
    ):
        transient_page = list_gmail_emails_page(
            "USER@example.com", 30, 100, "all", "", 0, ["gmail-1"],
        )
    assert [message["id"] for message in transient_page["messages"]] == ["gmail-2", "gmail-3"]
    assert transient_page["page_token"] == "gmail-next"
    assert transient_page["page_offset"] == 0
    assert transient_page["has_more"] is True
    assert fetch_summary.call_count == 2
    assert gmail_page_request.call_args.args[2]["q"] == "in:anywhere -in:chats"
    assert gmail_page_request.call_args.args[2]["includeSpamTrash"] == "true"
    write_index.assert_called()
    print("PASS inbox feed tests")


if __name__ == "__main__":
    main()
