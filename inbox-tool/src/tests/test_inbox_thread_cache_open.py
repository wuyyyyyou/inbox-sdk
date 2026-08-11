"""AI THREAD_REF 打开详情时的缓存优先回归。"""

from __future__ import annotations

from unittest.mock import patch


def test_index_summary_opens_thread_without_full_gmail_refresh() -> None:
    """只有索引摘要时也应返回线程首屏，不能阻塞到整线程 Gmail 请求。"""
    import anna_inbox_executa.v2_tools as tools

    summary = {
        "id": "message-1",
        "thread_id": "thread-1",
        "from": "gurru@example.com",
        "subject": "Collaboration",
        "internal_date": "1",
    }
    with patch("mail_agent.mail_providers.gmail.adapter.list_messages", return_value=[summary]), patch(
        "mail_agent.mail_providers.gmail.adapter.read_message", return_value=None,
    ), patch("mail_agent.mail_providers.gmail.adapter.refresh_thread_cache") as refresh, patch(
        "mail_agent.mail_providers.gmail.adapter.sync_cached_message_summaries",
    ):
        result = tools._load_thread_messages("mail@example.com", "thread-1")

    assert result == [summary]
    refresh.assert_not_called()
    print("[PASS] test_index_summary_opens_thread_without_full_gmail_refresh")


def test_incomplete_attachment_metadata_refreshes_thread() -> None:
    """摘要标记有附件但详情缓存为空时，必须刷新线程而非永久返回占位。"""
    import anna_inbox_executa.v2_tools as tools

    summary = {
        "id": "message-1",
        "thread_id": "thread-1",
        "from": "gurru@example.com",
        "subject": "Collaboration",
        "internal_date": "1",
        "has_attachment": True,
        "attachment_count": 2,
    }
    refreshed = [{
        **summary,
        "attachments": [
            {"id": "a-1", "filename": "agreement.pdf"},
            {"id": "a-2", "filename": "invoice.pdf"},
        ],
    }]
    with patch("mail_agent.mail_providers.gmail.adapter.list_messages", return_value=[summary]), patch(
        "mail_agent.mail_providers.gmail.adapter.read_message", return_value=summary,
    ), patch(
        "mail_agent.mail_providers.gmail.adapter.refresh_thread_cache", return_value=refreshed,
    ) as refresh:
        result = tools._load_thread_messages("mail@example.com", "thread-1")

    assert result == refreshed
    refresh.assert_called_once_with("mail@example.com", "thread-1")
    print("[PASS] test_incomplete_attachment_metadata_refreshes_thread")


if __name__ == "__main__":
    test_index_summary_opens_thread_without_full_gmail_refresh()
    test_incomplete_attachment_metadata_refreshes_thread()
