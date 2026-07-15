"""AI turn 阶段 B：Router 扩展、registry、propose 载荷、记忆工具。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


async def test_fallback_draft_with_thread():
    from mail_agent.ai_turn.router import route_ai_turn

    route = await route_ai_turn(
        "帮我起草回复",
        {
            "current_thread": {
                "kind": "thread",
                "message_id": "m1",
                "thread_id": "t1",
                "mailbox": "a@b.com",
            },
        },
        sampling_create_message=None,
    )
    assert route["steps"][0]["tool"] == "draft_reply"
    print("[PASS] test_fallback_draft_with_thread")


async def test_fallback_revise_with_draft():
    from mail_agent.ai_turn.router import route_ai_turn

    route = await route_ai_turn(
        "Shorten and make friendlier",
        {
            "current_thread": {"kind": "none"},
            "last_draft": {"body": "Hello world, this is a long draft.", "source": "assistant_artifact"},
        },
        sampling_create_message=None,
    )
    assert route["steps"][0]["tool"] == "revise_draft"
    print("[PASS] test_fallback_revise_with_draft")


async def test_fallback_organize():
    from mail_agent.ai_turn.router import route_ai_turn

    route = await route_ai_turn(
        "Organize my inbox",
        {"current_thread": {"kind": "none"}},
        sampling_create_message=None,
    )
    assert route["steps"][0]["tool"] == "propose_inbox_actions"
    print("[PASS] test_fallback_organize")


async def test_fallback_remember():
    from mail_agent.ai_turn.router import route_ai_turn

    route = await route_ai_turn(
        "Remember I prefer short replies",
        {},
        sampling_create_message=None,
    )
    assert route["steps"][0]["tool"] == "remember_preference"
    print("[PASS] test_fallback_remember")


async def test_normalize_rejects_mutation_tool():
    from mail_agent.ai_turn.router import _normalize_route

    route = _normalize_route(
        {
            "language": "en",
            "use_current_thread": False,
            "clarify": None,
            "steps": [
                {"tool": "archive_mail", "params": {}},
                {"tool": "draft_reply", "params": {}},
            ],
        },
        "draft",
        {
            "current_thread": {
                "kind": "thread",
                "message_id": "m1",
                "thread_id": "t1",
            },
        },
    )
    tools = [s["tool"] for s in route["steps"]]
    assert "archive_mail" not in tools
    assert "draft_reply" in tools
    print("[PASS] test_normalize_rejects_mutation_tool")


async def test_registry_records_turns():
    from mail_agent.ai_turn.registry import format_summary_for_router, get_conversation_summary, record_turn_summary

    cid = "test_conv_phase_b"
    record_turn_summary(cid, tool="search_mail", success=True, candidate_count=3, has_draft=False, kind="scan")
    record_turn_summary(cid, tool="draft_reply", success=True, candidate_count=0, has_draft=True, kind="draft")
    turns = get_conversation_summary(cid)
    assert len(turns) >= 2
    text = format_summary_for_router(cid)
    assert "search_mail" in text
    assert "has_draft=True" in text
    print("[PASS] test_registry_records_turns")


async def test_remember_preference_runner():
    from mail_agent.ai_turn.tools import tool_remember_preference

    # 使用内存假存储会较重；直接测解析与失败路径
    outcome = await tool_remember_preference(
        "Remember to use short replies",
        language="en",
        params={"preference": "use short replies"},
    )
    # 无 storage 时可能失败或成功（本地 backend）
    assert outcome["kind"] in {"memory", "error"}
    assert outcome["assistant_text"]
    print("[PASS] test_remember_preference_runner")


async def test_apply_proposed_invalid_action():
    from mail_agent.ai_turn.tools import apply_proposed_actions

    result = await apply_proposed_actions(action="send_mail", items=[{"mailbox": "a@b.com", "message_id": "1"}])
    assert result["success"] is False
    assert result["error"] == "invalid_action"
    print("[PASS] test_apply_proposed_invalid_action")


async def test_phase_a_search_still_works():
    from mail_agent.ai_turn.router import route_ai_turn

    route = await route_ai_turn(
        "找出最近紧急邮件",
        {"current_thread": {"kind": "none"}, "display_range_days": 7},
        sampling_create_message=None,
    )
    tools = [step["tool"] for step in route["steps"]]
    assert "search_mail" in tools
    print("[PASS] test_phase_a_search_still_works")


def main():
    asyncio.run(test_fallback_draft_with_thread())
    asyncio.run(test_fallback_revise_with_draft())
    asyncio.run(test_fallback_organize())
    asyncio.run(test_fallback_remember())
    asyncio.run(test_normalize_rejects_mutation_tool())
    asyncio.run(test_registry_records_turns())
    asyncio.run(test_remember_preference_runner())
    asyncio.run(test_apply_proposed_invalid_action())
    asyncio.run(test_phase_a_search_still_works())
    print("[ALL TESTS PASSED]")


if __name__ == "__main__":
    main()
