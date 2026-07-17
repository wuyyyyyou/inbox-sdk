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


def test_bare_recent_defers_to_scan_plan() -> None:
    """裸「最近/recent」不是明确窗口，必须 defer 到 Scan Plan。"""
    from mail_agent.ask.planner import resolve_effective_timeframe

    assert resolve_effective_timeframe("找找最近需要浏览的邮件", 30, "7d") == "30d"
    assert resolve_effective_timeframe("Show me recent emails to review", 30, "7d") == "30d"
    print("[PASS] test_bare_recent_defers_to_scan_plan")


def test_explicit_request_time_overrides_scan_plan() -> None:
    """用户明确写出最近 30 天时，必须覆盖 Scan Plan 的默认范围。"""
    from mail_agent.ask.planner import resolve_effective_timeframe

    assert resolve_effective_timeframe("Show emails from the last 30 days", 7, "7d") == "30d"
    assert resolve_effective_timeframe("最近 7 天未读邮件", 30, "30d") == "7d"
    assert resolve_effective_timeframe("帮我看看本周邮件", 30, "30d") == "7d"
    print("[PASS] test_explicit_request_time_overrides_scan_plan")


def test_actionable_browse_plan_clears_search_terms() -> None:
    """「需要浏览/处理」必须 broad sweep，不能带着无效关键词去 Gmail。"""
    from mail_agent.ask.planner import AskPlan, is_actionable_browse_request, normalize_actionable_browse_plan

    assert is_actionable_browse_request("找找最近需要浏览的邮件") is True
    plan = normalize_actionable_browse_plan(
        AskPlan(
            user_request="找找最近需要浏览的邮件",
            title="查找浏览邮件",
            topics=[{"concept": "browse", "search_terms": ["浏览", "review"], "relevance_hint": "x"}],
            direction="sent",
            goal="general_qa",
        )
    )
    assert plan.topics and plan.topics[0].get("search_terms") == []
    assert plan.direction == "inbox"
    assert plan.goal == "find_emails"
    print("[PASS] test_actionable_browse_plan_clears_search_terms")


def test_needs_reply_plan_uses_direction_all() -> None:
    """「What needs my reply?」必须 direction=all 且清空 search_terms。"""
    from mail_agent.ask.planner import AskPlan, is_needs_reply_request, normalize_actionable_browse_plan

    assert is_needs_reply_request("What needs my reply?") is True
    plan = normalize_actionable_browse_plan(
        AskPlan(
            user_request="What needs my reply?",
            topics=[{"concept": "reply", "search_terms": ["reply", "needs"], "relevance_hint": "x"}],
            direction="inbox",
            goal="general_qa",
        )
    )
    assert plan.direction == "all"
    assert plan.goal == "draft_replies"
    assert plan.topics and plan.topics[0].get("search_terms") == []
    print("[PASS] test_needs_reply_plan_uses_direction_all")


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
    test_bare_recent_defers_to_scan_plan()
    test_explicit_request_time_overrides_scan_plan()
    test_actionable_browse_plan_clears_search_terms()
    test_needs_reply_plan_uses_direction_all()
    await test_execute_search_never_reads_more_than_passed_cap()
    print("[ALL TESTS PASSED]")


if __name__ == "__main__":
    asyncio.run(main())
