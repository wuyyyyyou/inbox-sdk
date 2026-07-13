"""Regression tests for the read-only Compose sidebar prompt contract."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> None:
    from anna_inbox_executa.common import MAIL_AGENT_RUNS
    from anna_inbox_executa import v2_tools

    assert v2_tools._compose_response_mode(
        body="", visible_prompt="Write a first draft about a partnership", expected_artifact="compose_draft"
    ) == "draft"
    assert v2_tools._compose_response_mode(
        body="Hello", visible_prompt="Suggest changes to improve my draft", expected_artifact="compose_draft"
    ) == "analysis"
    assert v2_tools._compose_response_mode(
        body="Hello", visible_prompt="Rewrite this as a concise partnership email", expected_artifact="compose_draft"
    ) == "revision"
    assert v2_tools._compose_response_mode(
        body="你好", visible_prompt="请给我的草稿提改进建议", expected_artifact="compose_draft"
    ) == "analysis"
    assert v2_tools._compose_response_mode(
        body="你好", visible_prompt="请重写一版，更正式", expected_artifact="compose_draft"
    ) == "revision"

    run_id = "retry_compose_prompt_123"
    original = MAIL_AGENT_RUNS.get(run_id)
    MAIL_AGENT_RUNS[run_id] = {
        "run_id": run_id,
        "status": "running",
        "stage": "compose_mail_prompt",
        "result": None,
        "error": "",
    }
    try:
        result = asyncio.run(v2_tools._handle_v2_tool("start_compose_mail_prompt", {
            "run_id": run_id,
            "mailbox": "me@example.com",
            "draft": {"recipients": ["recipient@example.com"], "subject": "Hello", "body": ""},
            "visible_prompt": "Write a first draft about hello",
            "expected_artifact": "compose_draft",
        }, "invoke-1"))
        assert result["run_id"] == run_id
        assert result["status"] == "running"
    finally:
        if original is None:
            MAIL_AGENT_RUNS.pop(run_id, None)
        else:
            MAIL_AGENT_RUNS[run_id] = original

    print("PASS Compose mail prompt tests")


if __name__ == "__main__":
    main()
