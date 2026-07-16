"""AI turn 阶段 C：batch_draft / batch_outreach、多选校验、白名单。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _multi_select_context(n: int = 3) -> dict:
    return {
        "mailbox": "a@b.com",
        "current_thread": {"kind": "none"},
        "selected_threads": [
            {
                "mailbox": "a@b.com",
                "message_id": f"m{i}",
                "thread_id": f"t{i}",
                "subject": f"Subject {i}",
            }
            for i in range(1, n + 1)
        ],
    }


async def test_batch_tools_in_whitelist():
    from mail_agent.ai_turn.router import AI_TURN_ALLOWED_TOOLS

    assert "batch_draft" in AI_TURN_ALLOWED_TOOLS
    assert "batch_outreach" in AI_TURN_ALLOWED_TOOLS
    assert "send_mail" not in AI_TURN_ALLOWED_TOOLS
    print("[PASS] test_batch_tools_in_whitelist")


async def test_fallback_batch_draft_with_selection():
    from mail_agent.ai_turn.router import route_ai_turn

    route = await route_ai_turn(
        "为勾选的这些邮件批量起草回复",
        _multi_select_context(3),
        sampling_create_message=None,
    )
    assert route["steps"][0]["tool"] == "batch_draft"
    print("[PASS] test_fallback_batch_draft_with_selection")


async def test_fallback_batch_outreach():
    from mail_agent.ai_turn.router import route_ai_turn

    route = await route_ai_turn(
        "Write personalized outreach for each of these emails",
        _multi_select_context(2),
        sampling_create_message=None,
    )
    assert route["steps"][0]["tool"] == "batch_outreach"
    print("[PASS] test_fallback_batch_outreach")


async def test_normalize_batch_without_selection_clarify():
    from mail_agent.ai_turn.router import _normalize_route

    route = _normalize_route(
        {
            "language": "zh",
            "use_current_thread": False,
            "clarify": None,
            "steps": [{"tool": "batch_draft", "params": {}}],
        },
        "批量起草",
        {"selected_threads": []},
    )
    assert route["steps"] == []
    assert route.get("clarify")
    print("[PASS] test_normalize_batch_without_selection_clarify")


async def test_normalize_rejects_unknown_batch_mutation():
    from mail_agent.ai_turn.router import _normalize_route

    route = _normalize_route(
        {
            "language": "en",
            "use_current_thread": False,
            "clarify": None,
            "steps": [
                {"tool": "batch_send", "params": {}},
                {"tool": "batch_draft", "params": {}},
            ],
        },
        "batch draft",
        _multi_select_context(2),
    )
    tools = [s["tool"] for s in route["steps"]]
    assert "batch_send" not in tools
    assert "batch_draft" in tools
    print("[PASS] test_normalize_rejects_unknown_batch_mutation")


async def test_batch_draft_isolation_offline():
    """无 Sampling 时仍按封产出 artifact，thread_id 互不串。"""
    from mail_agent.ai_turn.tools import tool_batch_draft

    outcome = await tool_batch_draft(
        "Draft short replies for each",
        _multi_select_context(3),
        language="en",
        sampling_create_message=None,
    )
    assert outcome["kind"] == "draft"
    artifacts = outcome.get("artifacts") or []
    assert len(artifacts) == 3
    thread_ids = [a["thread_id"] for a in artifacts]
    assert thread_ids == ["t1", "t2", "t3"]
    assert outcome["artifact"]["thread_id"] == "t1"
    # 每封 body 独立（至少都非空）
    assert all(str(a.get("body") or "").strip() for a in artifacts)
    print("[PASS] test_batch_draft_isolation_offline")


async def test_batch_draft_clarify_when_empty_selection():
    from mail_agent.ai_turn.tools import tool_batch_draft

    outcome = await tool_batch_draft(
        "批量起草",
        {"selected_threads": []},
        language="zh",
        sampling_create_message=None,
    )
    assert outcome["kind"] == "clarify"
    print("[PASS] test_batch_draft_clarify_when_empty_selection")


async def test_batch_outreach_mode_offline():
    from mail_agent.ai_turn.tools import tool_batch_outreach

    outcome = await tool_batch_outreach(
        "Personalized outreach for selected",
        _multi_select_context(2),
        language="en",
        sampling_create_message=None,
    )
    assert outcome["kind"] == "draft"
    assert len(outcome.get("artifacts") or []) == 2
    print("[PASS] test_batch_outreach_mode_offline")


async def test_batch_caps_at_five():
    from mail_agent.ai_turn.tools import tool_batch_draft, _selected_thread_refs

    ctx = _multi_select_context(8)
    refs = _selected_thread_refs(ctx)
    assert len(refs) == 5
    outcome = await tool_batch_draft(
        "batch draft",
        ctx,
        language="en",
        sampling_create_message=None,
    )
    assert len(outcome.get("artifacts") or []) == 5
    print("[PASS] test_batch_caps_at_five")


async def test_runner_primary_batch():
    from mail_agent.ai_turn.runner import _primary_tool

    assert _primary_tool(["batch_draft"]) == "batch_draft"
    assert _primary_tool(["batch_outreach", "search_mail"]) == "batch_outreach"
    assert _primary_tool(["draft_reply", "batch_draft"]) == "batch_draft"
    print("[PASS] test_runner_primary_batch")


async def main():
    await test_batch_tools_in_whitelist()
    await test_fallback_batch_draft_with_selection()
    await test_fallback_batch_outreach()
    await test_normalize_batch_without_selection_clarify()
    await test_normalize_rejects_unknown_batch_mutation()
    await test_batch_draft_isolation_offline()
    await test_batch_draft_clarify_when_empty_selection()
    await test_batch_outreach_mode_offline()
    await test_batch_caps_at_five()
    await test_runner_primary_batch()
    print("\nAll phase C tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
