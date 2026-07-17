"""cancel_mail_agent_run 取消 Brief 扫描回归测试。

运行：uv --directory inbox-tool/src run python tests/test_cancel_mail_agent_run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_cancel_stops_continue() -> None:
    from anna_inbox_executa.brief_flow import cancel_mail_agent_run, start_mail_agent_run, _continue_mail_agent_run_async
    from anna_inbox_executa.common import MAIL_AGENT_RUNS
    import asyncio

    started = start_mail_agent_run(
        {
            "run_id": "bg_canceltest01",
            "user_request": "scan",
            "mailbox": "user@example.com",
            "mode": "auto",
        },
        invoke_id="invoke-1",
    )
    assert started["run_id"] == "bg_canceltest01"
    assert MAIL_AGENT_RUNS["bg_canceltest01"]["needs_continue"] is True

    cancelled = cancel_mail_agent_run("bg_canceltest01")
    assert cancelled["cancelled"] is True
    assert cancelled["stage"] == "cancelled"
    assert MAIL_AGENT_RUNS["bg_canceltest01"]["cancel_requested"] is True

    view = asyncio.run(_continue_mail_agent_run_async({"run_id": "bg_canceltest01"}, "invoke-2"))
    assert view["stage"] == "cancelled"
    assert view["needs_continue"] is False
    assert view["status"] == "failed"

    # 幂等
    again = cancel_mail_agent_run("bg_canceltest01")
    assert again["cancelled"] is True
    assert again["success"] is True

    MAIL_AGENT_RUNS.pop("bg_canceltest01", None)


def test_cancel_missing_run() -> None:
    from anna_inbox_executa.brief_flow import cancel_mail_agent_run

    result = cancel_mail_agent_run("bg_does_not_exist")
    assert result["success"] is False
    assert "not found" in result["error"]


if __name__ == "__main__":
    test_cancel_stops_continue()
    test_cancel_missing_run()
    print("cancel_mail_agent_run: OK")
