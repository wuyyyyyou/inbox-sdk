"""P0 邮箱同步状态与可续跑分页回归测试。"""

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


def run_metadata_gap_regression(check, sync, adapter, mailbox: str) -> None:
    """单封 metadata 失败、404 与后续补齐都不得阻塞分页或要求清缓存。"""
    from mail_agent.mail_providers.gmail.adapter import GmailApiError

    with tempfile.TemporaryDirectory() as temp_dir:
        with patch.object(adapter, "cache_dir", return_value=Path(temp_dir)):
            def fake_request(_mailbox: str, _path: str, _params: dict, **_kwargs):
                return {"messages": [{"id": "ok"}, {"id": "retry"}, {"id": "gone"}], "nextPageToken": "next-page"}

            def fake_summary(_mailbox: str, message_id: str, **_kwargs):
                if message_id == "retry":
                    raise TimeoutError("temporary timeout")
                if message_id == "gone":
                    raise GmailApiError(404, "deleted")
                return {"id": message_id, "internal_date": "2"}

            with (
                patch.object(adapter, "get_access_token", return_value="token"),
                patch.object(adapter, "gmail_request", side_effect=fake_request),
                patch.object(adapter, "fetch_message_summary", side_effect=fake_summary),
            ):
                page_ids, next_token = sync._fetch_metadata_page(
                    mailbox,
                    query="in:anywhere",
                    page_token="",
                    batch_limit=100,
                )

            cached_ids = {str(item.get("id") or "") for item in adapter.list_messages(mailbox)}
            pending = sync._pending_metadata_entries(sync.read_sync_state(mailbox))
            boundary = sync.get_mailbox_sync_boundary(mailbox)
            check("metadata page advances after single failure", page_ids == ["ok", "retry", "gone"] and next_token == "next-page", str((page_ids, next_token)))
            check("metadata page persists successful records", cached_ids == {"ok"}, str(cached_ids))
            check("temporary metadata failure enters repair queue", [item["id"] for item in pending] == ["retry"], str(pending))
            check("metadata gap marks boundary incomplete", boundary.get("pending_metadata_count") == 1 and boundary.get("sync_stage") == "priority_metadata", str(boundary))

            with (
                patch.object(adapter, "get_access_token", return_value="token"),
                patch.object(adapter, "fetch_message_summary", return_value={"id": "retry", "internal_date": "3"}),
            ):
                repair = sync.repair_pending_metadata(mailbox, force=True)

            repaired_ids = {str(item.get("id") or "") for item in adapter.list_messages(mailbox)}
            check("manual repair restores missing record without clearing cache", repair["repaired"] == 1 and repaired_ids == {"ok", "retry"}, str((repair, repaired_ids)))
            check("successful repair clears pending queue", not sync._pending_metadata_entries(sync.read_sync_state(mailbox)), str(sync.read_sync_state(mailbox)))


def main() -> None:
    from mail_agent.mail_providers.gmail import adapter
    from mail_agent.mail_providers.gmail import mailbox_sync as sync
    from tests.test_mailbox_sync_logging import (
        run_backfill_sync_silence_regression,
        run_background_repair_regression,
        run_content_sync_silence_regression,
        run_priority_sync_silence_regression,
        run_sync_failure_error_log_regression,
    )

    mailbox = "user@example.com"
    messages = [
        {
            "id": "new", "thread_id": "t-new", "internal_date": "1783814400000",
            "label_ids": ["INBOX"], "attachment_scan_version": adapter.ATTACHMENT_SCAN_VERSION,
        },
        {
            "id": "old", "thread_id": "t-old", "internal_date": "1770000000000",
            "label_ids": ["INBOX"], "attachment_scan_version": adapter.ATTACHMENT_SCAN_VERSION,
        },
    ]

    with tempfile.TemporaryDirectory() as temp_dir:
        with patch.object(adapter, "cache_dir", return_value=Path(temp_dir)):
            adapter.write_index(mailbox, messages)
            sync.write_sync_state(mailbox, {
                "history_id": "h-1",
                "last_history_id": "h-1",
                "initial_sync_complete": True,
                "body_sync_complete": False,
                "attachment_sync_complete": False,
                "backfill_complete": False,
                "configured_sync_range": sync.configured_sync_range(),
            })
            boundary = sync.get_mailbox_sync_boundary(mailbox)
            state = sync.read_sync_state(mailbox)

            check("boundary earliest persisted", bool(boundary["earliest_indexed_at"]), str(boundary))
            check("boundary latest persisted", bool(boundary["latest_indexed_at"]), str(boundary))
            check("history cursor persisted", state.get("last_history_id") == "h-1", str(state))
            check("initial sync state persisted", state.get("initial_sync_complete") is True, str(state))
            check("unbounded backfill configured", state.get("configured_sync_range", {}).get("backfill") == "unbounded", str(state))

            # 清缓存后必须同步复位，否则 History 会误判有完整基线。
            sync.reset_mailbox_sync_state(mailbox)
            reset = sync.read_sync_state(mailbox)
            check("reset clears cursor", not reset.get("history_id"), str(reset))
            check("reset clears priority completion", reset.get("initial_sync_complete") is False, str(reset))

    with tempfile.TemporaryDirectory() as temp_dir:
        with patch.object(adapter, "cache_dir", return_value=Path(temp_dir)):
            adapter.write_index(mailbox, [])

            empty = sync.empty_boundary(mailbox)
            check("empty boundary exposes sync stage", empty.get("sync_stage") == "priority_metadata", str(empty))
            check("empty boundary omits priority page token", "priority_page_token" not in empty, str(empty))
            check("empty boundary omits backfill page token", "backfill_page_token" not in empty, str(empty))

            cases = [
                (
                    "priority incomplete",
                    {
                        "initial_sync_complete": False,
                        "backfill_complete": False,
                        "body_sync_complete": False,
                        "attachment_sync_complete": False,
                        "watch_status": "unknown",
                    },
                    "priority_metadata",
                ),
                (
                    "backfill incomplete",
                    {
                        "initial_sync_complete": True,
                        "backfill_complete": False,
                        "body_sync_complete": False,
                        "attachment_sync_complete": False,
                        "watch_status": "unknown",
                    },
                    "backfill_metadata",
                ),
                (
                    "content incomplete",
                    {
                        "initial_sync_complete": True,
                        "backfill_complete": True,
                        "body_sync_complete": False,
                        "attachment_sync_complete": False,
                        "watch_status": "unknown",
                    },
                    "content_preprocess",
                ),
                (
                    "watch unknown",
                    {
                        "initial_sync_complete": True,
                        "backfill_complete": True,
                        "body_sync_complete": True,
                        "attachment_sync_complete": True,
                        "watch_status": "unknown",
                    },
                    "watch_setup",
                ),
                (
                    "watch active",
                    {
                        "initial_sync_complete": True,
                        "backfill_complete": True,
                        "body_sync_complete": True,
                        "attachment_sync_complete": True,
                        "watch_status": "active",
                    },
                    "ready",
                ),
                (
                    "watch skipped no topic",
                    {
                        "initial_sync_complete": True,
                        "backfill_complete": True,
                        "body_sync_complete": True,
                        "attachment_sync_complete": True,
                        "watch_status": "skipped_no_topic",
                    },
                    "ready",
                ),
                (
                    "watch error",
                    {
                        "initial_sync_complete": True,
                        "backfill_complete": True,
                        "body_sync_complete": True,
                        "attachment_sync_complete": True,
                        "watch_status": "error:quota_exceeded",
                    },
                    "watch_error",
                ),
            ]

            for label, patch_data, expected_stage in cases:
                sync.write_sync_state(mailbox, patch_data)
                boundary = sync.get_mailbox_sync_boundary(mailbox)
                check(f"{label} exposes sync stage", boundary.get("sync_stage") == expected_stage, str(boundary))
                check(f"{label} omits priority page token", "priority_page_token" not in boundary, str(boundary))
                check(f"{label} omits backfill page token", "backfill_page_token" not in boundary, str(boundary))

    with tempfile.TemporaryDirectory() as temp_dir:
        with patch.object(adapter, "cache_dir", return_value=Path(temp_dir)):
            # 部分缓存不能被误认为已完成 180 天优先基线，否则 History 锚定会跳过历史邮件。
            adapter.write_index(mailbox, [{"id": "partial", "internal_date": "1780000000000"}])
            sync.write_sync_state(mailbox, {
                "initial_sync_complete": False,
                "history_id": "h-partial",
                "last_history_id": "h-partial",
            })
            with patch.object(adapter, "gmail_request") as gmail_request:
                result = adapter.sync_cached_mailbox_history(mailbox)
            check("partial cache requires priority resync", result.get("resync_required") is True, str(result))
            check("partial cache reports incomplete priority", result.get("resync_reason") == "priority_sync_incomplete", str(result))
            gmail_request.assert_not_called()

    with tempfile.TemporaryDirectory() as temp_dir:
        with patch.object(adapter, "cache_dir", return_value=Path(temp_dir)):
            # 只验证分页状态机；Gmail 页内容由 mock 提供。
            pages: list[str] = []

            def priority_page(_mailbox: str, *, query: str, page_token: str, batch_limit: int):
                pages.append(page_token or "first")
                if not page_token:
                    return [f"p{index}" for index in range(100)], "token-2"
                return ["p2"], ""

            with patch.object(sync, "_fetch_metadata_page", side_effect=priority_page):
                first = sync.progress_priority_metadata_sync(mailbox, batch_limit=100, profile_history_id="h-priority")
                second = sync.progress_priority_metadata_sync(mailbox, batch_limit=100, profile_history_id="h-priority")
            state = sync.read_sync_state(mailbox)

            check("priority saves continuation token", first["initial_sync_complete"] is False, str(first))
            check("priority resumes from saved token", pages == ["first", "token-2"], str(pages))
            check("priority completes only after Gmail pages end", second["initial_sync_complete"] is True, str(second))
            check("priority cursor cleared after completion", not state.get("priority_page_token"), str(state))

            sync.write_sync_state(mailbox, {
                "history_id": "h-backfill",
                "last_history_id": "h-backfill",
                "initial_sync_complete": True,
                "backfill_complete": False,
            })
            backfill_pages: list[str] = []

            def backfill_page(_mailbox: str, *, query: str, page_token: str, batch_limit: int):
                backfill_pages.append(page_token or "first")
                if not page_token:
                    return [f"b{index}" for index in range(100)], "token-old"
                return ["b2"], ""

            with patch.object(sync, "_fetch_metadata_page", side_effect=backfill_page):
                first = sync.progress_backfill_metadata_sync(mailbox, batch_limit=100)
                second = sync.progress_backfill_metadata_sync(mailbox, batch_limit=100)
            state = sync.read_sync_state(mailbox)

            check("backfill saves continuation token", first["backfill_complete"] is False, str(first))
            check("backfill resumes from saved token", backfill_pages == ["first", "token-old"], str(backfill_pages))
            check("backfill has no count cap", second["backfill_complete"] is True, str(second))
            check("backfill cursor cleared after completion", not state.get("backfill_page_token"), str(state))

    run_priority_sync_silence_regression(check, sync, adapter)
    run_backfill_sync_silence_regression(check, sync, adapter)
    run_content_sync_silence_regression(check, sync, adapter)
    run_sync_failure_error_log_regression(check, sync, adapter)
    run_background_repair_regression(check, sync, adapter)
    run_metadata_gap_regression(check, sync, adapter, mailbox)


if __name__ == "__main__":
    main()
