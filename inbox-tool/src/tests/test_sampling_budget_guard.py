"""Anna Sampling 公共预算守卫的脚本式测试。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class SamplingStub:
    """记录 Host 请求，避免测试依赖真实 Anna 平台。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {"content": {"type": "text", "text": "{}"}}


class FailingSamplingStub:
    """模拟 Host 失败，用于验证错误观测不会泄露 metadata。"""

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        raise TimeoutError("host timeout")


async def test_budget_guard_caps_requests_and_stops_before_host() -> None:
    from anna_inbox_executa.sampling_tools import (
        SamplingBudgetExceeded,
        build_budgeted_sampling,
    )

    stub = SamplingStub()
    logs: list[str] = []
    with patch("anna_inbox_executa.sampling_tools.log", logs.append):
        sampling = build_budgeted_sampling(stub, invoke_id="run-unit-test")
        await sampling(
            max_tokens=8000,
            timeout=180.0,
            metadata={"tool": "ask_answer", "mailbox": "secret@example.com"},
            system_prompt="Never log this private prompt.",
            messages=[{"role": "user", "content": {"type": "text", "text": "private email body"}}],
        )
        await sampling(
            max_tokens=4096,
            timeout=180.0,
            metadata={"tool": "ask_answer"},
        )
        try:
            await sampling(max_tokens=1, metadata={"tool": "ask_answer"})
        except SamplingBudgetExceeded:
            pass
        else:
            raise AssertionError("预算耗尽后必须在本地拒绝请求")

    assert [call["max_tokens"] for call in stub.calls] == [4096, 1904]
    assert all(call["timeout"] == 60.0 for call in stub.calls)
    joined_logs = "\n".join(logs)
    assert "requested_tokens=8000" in joined_logs
    assert "granted_tokens=4096" in joined_logs
    assert "remaining_tokens=1904" in joined_logs
    assert "prompt_bytes=" in joined_logs
    assert "private email body" not in joined_logs
    assert "Never log this private prompt" not in joined_logs
    assert "secret@example.com" not in joined_logs
    print("[PASS] test_budget_guard_caps_requests_and_stops_before_host")


async def test_budget_guard_logs_safe_failure_metrics() -> None:
    from anna_inbox_executa.sampling_tools import build_budgeted_sampling

    logs: list[str] = []
    with patch("anna_inbox_executa.sampling_tools.log", logs.append):
        sampling = build_budgeted_sampling(FailingSamplingStub(), invoke_id="run-unit-test")
        try:
            await sampling(
                max_tokens=512,
                metadata={"tool": "ask_planner", "mailbox": "secret@example.com"},
            )
        except TimeoutError:
            pass
        else:
            raise AssertionError("Host 异常必须向调用方传播")

    joined_logs = "\n".join(logs)
    assert "anna sampling failed" in joined_logs
    assert "elapsed_ms=" in joined_logs
    assert "error_type=TimeoutError" in joined_logs
    assert "secret@example.com" not in joined_logs
    print("[PASS] test_budget_guard_logs_safe_failure_metrics")


async def main() -> None:
    await test_budget_guard_caps_requests_and_stops_before_host()
    await test_budget_guard_logs_safe_failure_metrics()
    print("[ALL TESTS PASSED]")


if __name__ == "__main__":
    asyncio.run(main())
