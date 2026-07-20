"""调用链安全诊断的回归测试。

运行：uv --directory inbox-tool/src run python tests/test_runtime_diagnostics.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch


_SRC = str(Path(__file__).resolve().parents[1])
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def test_span_redacts_unapproved_fields() -> None:
    """邮箱等未授权字段不能进入前端可见诊断。"""
    from anna_inbox_executa.diagnostics import activate_trace, create_trace, deactivate_trace, record_span, snapshot

    trace = create_trace(operation="list_gmail_emails", invoke_id="private-host-invoke")
    token = activate_trace(trace)
    try:
        started = time.monotonic()
        record_span(
            "gmail.http",
            started,
            endpoint="messages",
            http_status=200,
            mailbox="secret@example.com",
            token="never-recorded",
        )
        payload = snapshot(trace)
    finally:
        deactivate_trace(token)

    assert payload is not None
    assert payload["operation"] == "list_gmail_emails"
    assert payload["spans"][0]["endpoint"] == "messages"
    assert "mailbox" not in payload["spans"][0]
    assert "secret@example.com" not in str(payload)
    assert "never-recorded" not in str(payload)


def test_invoke_response_includes_diagnostics() -> None:
    """同步 Gmail/收件箱工具成功时，前端可以获得对应 invoke 的分段摘要。"""
    from anna_inbox_executa import main

    with patch.object(
        main,
        "handle_invoke",
        return_value={"success": True, "tool": "list_gmail_emails_page", "data": {"messages": []}},
    ):
        response = main.handle_request({
            "jsonrpc": "2.0",
            "id": "diagnostic-test",
            "method": "invoke",
            "params": {"tool": "list_gmail_emails_page", "arguments": {}, "context": {}},
        })

    diagnostic = response["result"]["data"]["diagnostics"]
    assert diagnostic["operation"] == "list_gmail_emails_page"
    assert diagnostic["trace_id"].startswith("rt_")
    assert diagnostic["spans"][-1]["stage"] == "executa.invoke"


def test_existing_run_diagnostics_are_not_duplicated() -> None:
    """后台 run 已有 diagnostics 时不得再附加 invoke_diagnostics。"""
    from anna_inbox_executa import main
    from anna_inbox_executa.diagnostics import create_trace

    trace = create_trace(operation="start_ai_turn", invoke_id="run-test")
    result = main._attach_diagnostics(
        {"success": True, "data": {"diagnostics": {"operation": "start_ai_turn", "spans": []}}},
        trace,
    )

    assert set(result["data"]) == {"diagnostics"}


def test_gmail_401_without_candidates_is_not_presented_as_empty_search() -> None:
    """401/403 且没有候选时必须返回授权修复提示。"""
    from anna_inbox_executa.ai_turn_flow import (
        _gmail_access_failed_without_results,
        _gmail_access_unavailable_message,
    )
    from anna_inbox_executa.diagnostics import create_trace

    trace = create_trace(operation="start_ai_turn", invoke_id="run-test")
    trace["spans"] = [{"stage": "gmail.http", "http_status": 401, "outcome": "error"}]
    assert _gmail_access_failed_without_results(trace, 0) is True
    assert _gmail_access_failed_without_results(trace, 1) is False
    assert "重新连接 Google" in _gmail_access_unavailable_message("帮我看邮件")


def test_running_ai_turn_is_a_successful_async_start() -> None:
    """running 状态代表可轮询的异步启动，不能被前端当作工具调用失败。"""
    from anna_inbox_executa.ai_turn_flow import MAIL_AGENT_RUNS, _public_ai_turn_state

    run_id = "at_running_state_test"
    MAIL_AGENT_RUNS[run_id] = {"status": "running", "needs_continue": True}
    try:
        state = _public_ai_turn_state(run_id)
    finally:
        MAIL_AGENT_RUNS.pop(run_id, None)

    assert state["success"] is True
    assert state["status"] == "running"
    assert state["needs_continue"] is True


if __name__ == "__main__":
    test_span_redacts_unapproved_fields()
    test_invoke_response_includes_diagnostics()
    test_existing_run_diagnostics_are_not_duplicated()
    test_gmail_401_without_candidates_is_not_presented_as_empty_search()
    test_running_ai_turn_is_a_successful_async_start()
    print("Runtime diagnostics: OK")
