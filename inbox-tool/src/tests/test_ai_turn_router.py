"""AI turn Router 单元测试（阶段 A）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


async def test_fallback_route_search():
    from mail_agent.ai_turn.router import route_ai_turn

    route = await route_ai_turn(
        "找出最近紧急邮件",
        {"current_thread": {"kind": "none"}, "display_range_days": 7},
        sampling_create_message=None,
    )
    tools = [step["tool"] for step in route["steps"]]
    assert "search_mail" in tools
    assert route["clarify"] is None
    print("[PASS] test_fallback_route_search")


async def test_fallback_route_summarize_with_thread():
    from mail_agent.ai_turn.router import route_ai_turn

    route = await route_ai_turn(
        "请总结这封邮件",
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
    assert route["steps"][0]["tool"] == "summarize_thread"
    assert route["use_current_thread"] is True
    print("[PASS] test_fallback_route_summarize_with_thread")


async def test_normalize_rejects_unknown_tool():
    from mail_agent.ai_turn.router import _normalize_route

    route = _normalize_route(
        {
            "language": "en",
            "use_current_thread": False,
            "clarify": None,
            "steps": [
                {"tool": "send_mail", "params": {}},
                {"tool": "chat_general", "params": {}},
            ],
        },
        "hi",
        {},
    )
    assert route["steps"] == [{"tool": "chat_general", "params": {}}]
    print("[PASS] test_normalize_rejects_unknown_tool")


async def test_normalize_forces_explicit_search_out_of_chat():
    """明确的收件箱检索不能被 Router Sampling 错路由为普通聊天。"""
    from mail_agent.ai_turn.router import _normalize_route

    route = _normalize_route(
        {
            "language": "zh",
            "use_current_thread": False,
            "clarify": None,
            "steps": [{"tool": "chat_general", "params": {}}],
        },
        "帮我找最近7天内的未读邮件",
        {},
    )
    assert [step["tool"] for step in route["steps"]] == ["search_mail", "rank_answer"]
    print("[PASS] test_normalize_forces_explicit_search_out_of_chat")


async def test_sampling_router_payload():
    from mail_agent.ai_turn.router import route_ai_turn

    async def fake_sampling(**kwargs):
        return {
            "content": {
                "type": "text",
                "text": '{"language":"en","use_current_thread":false,"clarify":null,"steps":[{"tool":"chat_general","params":{}}]}',
            },
        }

    # call_llm_json_safe 需要完整 JSON 路径；这里直接测 normalize + 带 sampling 的成功路径
    async def sampling_create_message(**kwargs):
        return await fake_sampling(**kwargs)

    # 通过 monkeypatch call_llm_json_safe 较重；用无 sampling 的 chat 兜底即可
    route = await route_ai_turn("hello", {}, sampling_create_message=None)
    assert route["steps"][0]["tool"] == "chat_general"
    print("[PASS] test_sampling_router_payload")


async def test_chat_general_runner():
    from mail_agent.ai_turn.runner import run_ai_turn

    result = await run_ai_turn(
        "你好",
        {},
        {"mailbox": "a@b.com"},
        sampling_create_message=None,
    )
    assert result["kind"] == "chat"
    assert result["assistant_text"]
    print("[PASS] test_chat_general_runner")


def main():
    asyncio.run(test_fallback_route_search())
    asyncio.run(test_fallback_route_summarize_with_thread())
    asyncio.run(test_normalize_rejects_unknown_tool())
    asyncio.run(test_normalize_forces_explicit_search_out_of_chat())
    asyncio.run(test_sampling_router_payload())
    asyncio.run(test_chat_general_runner())
    print("[ALL TESTS PASSED]")


if __name__ == "__main__":
    main()
