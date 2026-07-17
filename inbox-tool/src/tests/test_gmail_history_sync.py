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
