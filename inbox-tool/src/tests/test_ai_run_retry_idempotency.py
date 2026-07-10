"""Regression tests for retrying sidebar AI runs without duplicate work."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label} failed{': ' + detail if detail else ''}")


def main() -> None:
    from anna_inbox_executa import ask_flow
    from anna_inbox_executa.common import MAIL_AGENT_RUNS

    run_id = "retry_scan_123"
    original = MAIL_AGENT_RUNS.get(run_id)
    MAIL_AGENT_RUNS[run_id] = {
        "run_id": run_id,
        "status": "running",
        "stage": "searching",
        "progress": {"current": 2},
        "partial": {"sources": [{"id": "message-1"}]},
        "started_at": "2026-07-10T00:00:00+08:00",
        "updated_at": "2026-07-10T00:00:01+08:00",
        "result": None,
        "error": "",
    }
    try:
        result = ask_flow.start_custom_scan({"run_id": run_id, "user_request": "Find follow-ups"}, "invoke-1")
        check("reused run id", result["run_id"] == run_id, str(result))
        check("reused running task", result["status"] == "running", str(result))
        check("kept progress", result["progress"] == {"current": 2}, str(result))
        check("did not replace stored run", MAIL_AGENT_RUNS[run_id]["stage"] == "searching", str(MAIL_AGENT_RUNS[run_id]))
    finally:
        if original is None:
            MAIL_AGENT_RUNS.pop(run_id, None)
        else:
            MAIL_AGENT_RUNS[run_id] = original

    from anna_inbox_executa import v2_tools

    prompt_run_id = "retry_prompt_123"
    original_prompt = MAIL_AGENT_RUNS.get(prompt_run_id)
    MAIL_AGENT_RUNS[prompt_run_id] = {
        "run_id": prompt_run_id,
        "status": "running",
        "stage": "inbox_mail_prompt",
        "result": None,
        "error": "",
    }
    try:
        prompt_result = asyncio.run(v2_tools._handle_v2_tool("start_inbox_mail_prompt", {
            "run_id": prompt_run_id,
            "mailbox": "me@example.com",
            "thread_id": "thread-1",
            "anchor_message_id": "message-1",
            "latest_message_id": "message-2",
            "visible_prompt": "Summarize this thread",
        }, "invoke-2"))
        check("reused mail prompt run id", prompt_result["run_id"] == prompt_run_id, str(prompt_result))
        check("reused mail prompt task", prompt_result["status"] == "running", str(prompt_result))
    finally:
        if original_prompt is None:
            MAIL_AGENT_RUNS.pop(prompt_run_id, None)
        else:
            MAIL_AGENT_RUNS[prompt_run_id] = original_prompt

    print("PASS AI run retry idempotency tests")


if __name__ == "__main__":
    main()
