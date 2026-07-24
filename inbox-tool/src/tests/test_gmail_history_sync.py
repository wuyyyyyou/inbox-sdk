"""Gmail History 增量同步回归测试。

运行：uv --directory inbox-tool/src run python tests/test_gmail_history_sync.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"PASS {label}")


def main() -> None:
    from mail_agent.mail_providers.gmail import adapter

    mailbox = "user@example.com"
    scan_version = adapter.ATTACHMENT_SCAN_VERSION
    existing = [
        {"id": "changed", "internal_date": "2", "label_ids": ["INBOX", "UNREAD"], "subject": "Old", "attachment_scan_version": scan_version},
        {"id": "deleted", "internal_date": "1", "label_ids": ["INBOX"], "subject": "Deleted", "attachment_scan_version": scan_version},
    ]
    summaries = {
        "changed": {"id": "changed", "internal_date": "2", "label_ids": ["INBOX", "STARRED"], "subject": "Updated", "attachment_scan_version": scan_version},
        "added": {"id": "added", "internal_date": "3", "label_ids": ["INBOX", "UNREAD"], "subject": "New", "attachment_scan_version": scan_version},
    }
    history_page = {
        "historyId": "20",
        "history": [
            {"labelsAdded": [{"message": {"id": "changed"}}]},
            {"messagesDeleted": [{"message": {"id": "deleted"}}]},
            {"messagesAdded": [{"message": {"id": "added"}}]},
        ],
    }

    with tempfile.TemporaryDirectory() as temp_dir:
        with (
            patch.object(adapter, "cache_dir", return_value=Path(temp_dir)),
            patch.object(adapter, "gmail_request", return_value=history_page) as history_request,
            patch.object(adapter, "get_access_token", return_value="test-token"),
            patch.object(adapter, "fetch_message_summary", side_effect=lambda _mailbox, message_id, **_kwargs: summaries[message_id]),
        ):
            adapter.write_index(mailbox, existing)
            adapter.set_cached_mailbox_history_cursor(mailbox, "10", scope_days=30)
            result = adapter.sync_cached_mailbox_history(mailbox)

            merged = {item["id"]: item for item in adapter.list_messages(mailbox)}
            state = adapter._read_history_sync_state(mailbox)

        check("history mode", result["mode"] == "history", str(result))
        check("adds new mail", "added" in merged)
        check("updates labels", merged["changed"]["label_ids"] == ["INBOX", "STARRED"], str(merged["changed"]))
        check("removes deleted mail", "deleted" not in merged)
        check("advances cursor after cache write", state.get("history_id") == "20", str(state))
        params = history_request.call_args.args[2]
        check("uses stored cursor", params["startHistoryId"] == "10", str(params))

    # 本地已有缓存但 cursor 丢失：锚定 profile.historyId，禁止全量 messages.list 重扫。
    with tempfile.TemporaryDirectory() as temp_dir:
        with (
            patch.object(adapter, "cache_dir", return_value=Path(temp_dir)),
            patch.object(adapter, "gmail_request", return_value={"historyId": "99"}) as profile_request,
            patch.object(adapter, "live_search_metadata_and_cache", return_value=[]) as catchup,
        ):
            adapter.write_index(mailbox, existing)
            result = adapter.sync_cached_mailbox_history(mailbox)
            state = adapter._read_history_sync_state(mailbox)

        check("missing cursor seeds from profile", result.get("mode") == "history_cursor_seeded", str(result))
        check("seeded cursor does not require resync", result.get("resync_required") is False, str(result))
        check("seeded cursor persisted", state.get("history_id") == "99", str(state))
        check("seeded cursor only hits profile", profile_request.call_count == 1, str(profile_request.call_count))
        check("seeded cursor does short catch-up not full resync", catchup.call_count == 1, str(catchup.call_count))
        catchup_query = str(catchup.call_args.args[1] if catchup.call_args and catchup.call_args.args else "")
        check("seeded catch-up is recent window", "newer_than:2d" in catchup_query, catchup_query)

    # 摘要读超时：软失败重试，不推进 cursor，不炸成 internal error。
    with tempfile.TemporaryDirectory() as temp_dir:
        with (
            patch.object(adapter, "cache_dir", return_value=Path(temp_dir)),
            patch.object(adapter, "gmail_request", return_value=history_page),
            patch.object(adapter, "get_access_token", return_value="test-token"),
            patch.object(adapter, "fetch_message_summary", side_effect=TimeoutError("The read operation timed out")),
        ):
            adapter.write_index(mailbox, existing)
            adapter.set_cached_mailbox_history_cursor(mailbox, "10", scope_days=30)
            result = adapter.sync_cached_mailbox_history(mailbox)
            state = adapter._read_history_sync_state(mailbox)

        check("timeout returns history_retry", result.get("mode") == "history_retry", str(result))
        check("timeout does not require full resync", result.get("resync_required") is False, str(result))
        check("timeout keeps old cursor", state.get("history_id") == "10", str(state))

    # 旧摘要没有附件扫描版本时，自动同步应要求一次完整基线同步，不能等详情页补全。
    with tempfile.TemporaryDirectory() as temp_dir:
        legacy = [{"id": "legacy", "internal_date": "1", "label_ids": ["INBOX"], "attachment_scan_version": 0}]
        with (
            patch.object(adapter, "cache_dir", return_value=Path(temp_dir)),
            patch.object(adapter, "gmail_request") as history_request,
        ):
            adapter.write_index(mailbox, legacy)
            adapter.set_cached_mailbox_history_cursor(mailbox, "10", scope_days=30)
            result = adapter.sync_cached_mailbox_history(mailbox)
            state = adapter._read_history_sync_state(mailbox)

        check("attachment upgrade requires resync", result.get("resync_required") is True and result.get("resync_reason") == "attachment_metadata_upgrade", str(result))
        check("attachment upgrade skips History request", history_request.call_count == 0)
        check("attachment upgrade keeps old cursor", state.get("history_id") == "10", str(state))

    with tempfile.TemporaryDirectory() as temp_dir:
        with (
            patch.object(adapter, "cache_dir", return_value=Path(temp_dir)),
            patch.object(adapter, "gmail_request", side_effect=adapter.GmailApiError(404, "history expired")),
        ):
            adapter.write_index(mailbox, existing)
            adapter.set_cached_mailbox_history_cursor(mailbox, "10", scope_days=30)
            result = adapter.sync_cached_mailbox_history(mailbox)
            state = adapter._read_history_sync_state(mailbox)

        check("history expiry requires resync", result.get("resync_required") is True and result.get("resync_reason") == "history_expired", str(result))
        check("history expiry keeps old cursor", state.get("history_id") == "10", str(state))


if __name__ == "__main__":
    main()
