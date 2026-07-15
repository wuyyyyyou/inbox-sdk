"""Ask 扫描范围与邮件数量上限的回归测试。"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_scan_plan_timeframe_is_used_without_explicit_request_time() -> None:
    """未写时间的请求必须使用当前 Scan Plan 的天数，而不是 Planner 猜测值。"""
    from mail_agent.ask.planner import resolve_effective_timeframe

    assert resolve_effective_timeframe("帮我找需要处理的邮件", 7, "30d") == "7d"
    print("[PASS] test_scan_plan_timeframe_is_used_without_explicit_request_time")


def test_explicit_request_time_overrides_scan_plan() -> None:
    """用户明确写出最近 30 天时，必须覆盖 Scan Plan 的默认范围。"""
    from mail_agent.ask.planner import resolve_effective_timeframe

    assert resolve_effective_timeframe("Show emails from the last 30 days", 7, "7d") == "30d"
    print("[PASS] test_explicit_request_time_overrides_scan_plan")


async def test_execute_search_never_reads_more_than_passed_cap() -> None:
    """多个查询合并后仍不得超过前端传入的最大邮件数。"""
    from mail_agent.ask.search import execute_search

    calls: list[int] = []

    def fake_live_search(_mailbox: str, _query: str, limit: int) -> list[str]:
        calls.append(limit)
        return [f"message-{index}" for index in range(limit)]

    async def fake_get_messages(_mailbox: str, message_ids: list[str]) -> list[str]:
        return message_ids

    fake_adapter = types.SimpleNamespace(
        live_search_and_cache=fake_live_search,
        get_messages_lite_async=fake_get_messages,
    )
    with patch.dict(sys.modules, {"mail_agent.mail_providers.gmail.adapter": fake_adapter}):
        messages = await execute_search(
            "owner@example.com",
            [
                {"query": "newer_than:7d", "max_results": 100},
                {"query": "newer_than:7d in:inbox", "max_results": 100},
            ],
            max_messages=50,
            max_broaden_attempts=0,
        )

    assert len(messages) == 50
    assert calls == [50]
    print("[PASS] test_execute_search_never_reads_more_than_passed_cap")


async def main() -> None:
    test_scan_plan_timeframe_is_used_without_explicit_request_time()
    test_explicit_request_time_overrides_scan_plan()
    await test_execute_search_never_reads_more_than_passed_cap()
    print("[ALL TESTS PASSED]")


if __name__ == "__main__":
    asyncio.run(main())
