from __future__ import annotations

import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_sync_logs():
    logger = logging.getLogger("mail_agent.gmail.sync")
    handler = _CaptureHandler()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _assert_no_sync_logs(check, label: str, records: list[logging.LogRecord]) -> None:
    """正常同步不应输出进度、开始或完成日志。"""
    messages = [record.getMessage() for record in records]
    check(f"{label} does not emit sync logs", not records, str(messages))


class _ImmediateThread:
    def __init__(self, target, name=None, daemon=None):
        self._target = target

    def start(self) -> None:
        self._target()


def run_priority_sync_silence_regression(check, sync, adapter) -> None:
    mailbox = "priority.progress.sentinel@example.invalid"
    history_id = "priority-history-id-sentinel-do-not-log"
    message_id = "priority-message-id-sentinel-do-not-log"

    with tempfile.TemporaryDirectory() as temp_dir:
        with patch.object(adapter, "cache_dir", return_value=Path(temp_dir)):
            sync.write_sync_state(mailbox, {
                "history_id": history_id,
                "last_history_id": history_id,
                "initial_sync_complete": False,
                "backfill_complete": False,
            })
            with capture_sync_logs() as handler:
                with patch.object(sync, "_fetch_metadata_page", return_value=([message_id], "")):
                    result = sync.progress_priority_metadata_sync(mailbox, batch_limit=1, profile_history_id=history_id)

            check("priority sync completes", result["initial_sync_complete"] is True, str(result))
            _assert_no_sync_logs(check, "priority sync", handler.records)


def run_backfill_sync_silence_regression(check, sync, adapter) -> None:
    mailbox = "backfill.progress.sentinel@example.invalid"
    message_id = "backfill-message-id-sentinel-do-not-log"

    with tempfile.TemporaryDirectory() as temp_dir:
        with patch.object(adapter, "cache_dir", return_value=Path(temp_dir)):
            sync.write_sync_state(mailbox, {
                "initial_sync_complete": True,
                "backfill_complete": False,
            })
            with capture_sync_logs() as handler:
                with patch.object(sync, "_fetch_metadata_page", return_value=([message_id], "")):
                    result = sync.progress_backfill_metadata_sync(mailbox, batch_limit=1)

            check("backfill sync completes", result["backfill_complete"] is True, str(result))
            _assert_no_sync_logs(check, "backfill sync", handler.records)


def run_content_sync_silence_regression(check, sync, adapter) -> None:
    mailbox = "content.progress.sentinel@example.invalid"

    with tempfile.TemporaryDirectory() as temp_dir:
        with patch.object(adapter, "cache_dir", return_value=Path(temp_dir)):
            sync.write_sync_state(mailbox, {
                "initial_sync_complete": True,
                "backfill_complete": False,
                "body_sync_complete": False,
                "attachment_sync_complete": False,
            })
            with capture_sync_logs() as handler:
                with patch.object(sync.threading, "Thread", _ImmediateThread):
                    with patch.object(sync, "progress_backfill_metadata_sync", return_value={"backfill_complete": True, "fetched": 0, "new_count": 0}):
                        with patch.object(adapter, "preprocess_cached_content_batch", return_value={
                            "mailbox": mailbox,
                            "processed": 1,
                            "skipped": 0,
                            "body_pending": 3,
                            "attachment_pending": 1,
                        }):
                            started = sync.schedule_background_sync(mailbox)

            check("content background scheduler starts", started is True, mailbox)
            _assert_no_sync_logs(check, "content background sync", handler.records)


def run_sync_failure_error_log_regression(check, sync, adapter) -> None:
    """同步失败时仅保留不含敏感内容的 ERROR 日志。"""
    mailbox = "failure.sentinel@example.invalid"

    with tempfile.TemporaryDirectory() as temp_dir:
        with patch.object(adapter, "cache_dir", return_value=Path(temp_dir)):
            sync.write_sync_state(mailbox, {
                "initial_sync_complete": True,
                "backfill_complete": False,
            })
            with capture_sync_logs() as handler:
                with patch.object(sync.threading, "Thread", _ImmediateThread):
                    with patch.object(sync, "progress_backfill_metadata_sync", side_effect=RuntimeError("sentinel")):
                        started = sync.schedule_background_sync(mailbox)

            messages = [record.getMessage() for record in handler.records]
            check("failure background scheduler starts", started is True, mailbox)
            check("sync failure emits exactly one error log", len(handler.records) == 1 and handler.records[0].levelno == logging.ERROR, str(messages))
            check("sync failure log excludes mailbox", mailbox not in messages[0] if messages else False, str(messages))


def run_background_repair_regression(check, sync, adapter) -> None:
    """后台历史回填完成时，仍必须继续处理 metadata 缺口。"""
    mailbox = "background.repair.sentinel@example.invalid"
    with tempfile.TemporaryDirectory() as temp_dir:
        with patch.object(adapter, "cache_dir", return_value=Path(temp_dir)):
            sync.write_sync_state(mailbox, {
                "initial_sync_complete": True,
                "backfill_complete": False,
                "pending_metadata": [{"id": "missing", "attempts": 1, "next_retry_at": 0}],
            })
            repairs = [{"attempted": 1, "repaired": 1, "pending": 0}]

            with (
                patch.object(sync.threading, "Thread", _ImmediateThread),
                patch.object(sync, "repair_pending_metadata", side_effect=repairs),
                patch.object(sync, "progress_backfill_metadata_sync", return_value={
                    "backfill_complete": True,
                    "fetched": 0,
                    "new_count": 0,
                    "boundary": {"cache_total": 1},
                }),
                patch.object(sync, "get_mailbox_sync_boundary", side_effect=[
                    {"cache_total": 1, "pending_metadata_count": 1},
                    {"cache_total": 1, "pending_metadata_count": 0},
                ]),
                patch.object(adapter, "preprocess_cached_content_batch", return_value={
                    "body_pending": 0,
                    "attachment_pending": 0,
                }),
            ):
                started = sync.schedule_background_sync(mailbox)

            check("automatic background sync starts repair work", started is True, mailbox)
            check("automatic background sync invokes repair", len(repairs) == 1, str(repairs))
