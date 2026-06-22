"""Brief short-invoke scan should use persisted scan state incrementally."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


async def _run_case(*, total_scans: int, last_internal_date: str) -> dict[str, Any]:
    from anna_inbox_executa import brief_flow
    from mail_agent.core import pipeline, scan
    from mail_agent.domain.types import MailTaskPlan, MessageLite
    from mail_agent.storage import ops
    from mail_agent.storage.types import ScanState

    original_storage_ready = pipeline._storage_ready
    original_get_scan_plan_config = pipeline._get_scan_plan_config
    original_run_mail_scan = scan.run_mail_scan
    original_get_scan_state = ops.get_scan_state
    original_filter_unprocessed = ops.filter_unprocessed
    original_save_run_checkpoint = brief_flow._save_run_checkpoint

    captured: dict[str, Any] = {}
    run_id = f"test_incremental_{total_scans}"
    brief_flow.MAIL_AGENT_RUNS[run_id] = {
        "run_id": run_id,
        "status": "queued",
        "stage": "queued",
        "progress": {},
        "warnings": [],
        "started_at": "2026-06-22T00:00:00+08:00",
        "updated_at": "2026-06-22T00:00:00+08:00",
        "result": None,
        "error": "",
        "partial": {"_args": {}},
        "needs_continue": True,
        "cards_added": 0,
        "brief": {},
    }

    async def fake_get_scan_plan_config(mailbox: str) -> Any:
        class Config:
            max_messages = 25
            scan_window_days = 3

        return Config()

    async def fake_run_mail_scan(
        mailbox: str,
        max_threads: int,
        *,
        newer_than_days: int | None = None,
        after_timestamp: str = "",
        progress_callback: Any = None,
    ) -> list[MessageLite]:
        captured.update({
            "mailbox": mailbox,
            "max_threads": max_threads,
            "newer_than_days": newer_than_days,
            "after_timestamp": after_timestamp,
        })
        return [
            MessageLite(
                message_id="msg-1",
                thread_id="thread-1",
                from_addr="sender@example.com",
                to_addr=mailbox,
                subject="Hello",
                snippet="Can you review this?",
                internal_date="1770000000001",
                label_ids=["INBOX"],
                unread=True,
            )
        ]

    async def fake_get_scan_state(mailbox: str) -> ScanState:
        return ScanState(
            mailbox=mailbox,
            last_scan_ts="2026-06-21T00:00:00+08:00",
            last_message_internal_date=last_internal_date,
            total_scans=total_scans,
            total_processed=10,
        )

    try:
        pipeline._storage_ready = lambda: True
        pipeline._get_scan_plan_config = fake_get_scan_plan_config
        scan.run_mail_scan = fake_run_mail_scan
        ops.get_scan_state = fake_get_scan_state
        ops.filter_unprocessed = lambda mailbox, ids: asyncio.sleep(0, result=ids)
        brief_flow._save_run_checkpoint = lambda run_id: None

        await brief_flow._brief_prepare_scan(
            run_id,
            {
                "user_request": "scan my inbox",
                "mailbox": "user@example.com",
                "mode": "default_secretary",
                "max_messages": 25,
            },
        )
        captured["progress"] = brief_flow.MAIL_AGENT_RUNS[run_id]["progress"]
        return captured
    finally:
        pipeline._storage_ready = original_storage_ready
        pipeline._get_scan_plan_config = original_get_scan_plan_config
        scan.run_mail_scan = original_run_mail_scan
        ops.get_scan_state = original_get_scan_state
        ops.filter_unprocessed = original_filter_unprocessed
        brief_flow._save_run_checkpoint = original_save_run_checkpoint
        brief_flow.MAIL_AGENT_RUNS.pop(run_id, None)


def main() -> None:
    first = asyncio.run(_run_case(total_scans=0, last_internal_date="1770000000000"))
    check("first scan after_timestamp", first["after_timestamp"], "")
    check("first scan incremental flag", first["progress"]["incremental"], False)

    later = asyncio.run(_run_case(total_scans=2, last_internal_date="1770000000000"))
    check("incremental after_timestamp", later["after_timestamp"], "1770000000000")
    check("incremental flag", later["progress"]["incremental"], True)
    check("scan window", later["newer_than_days"], 3)
    check("scan max threads", later["max_threads"], 25)

    print("PASS brief incremental scan tests")


if __name__ == "__main__":
    main()
