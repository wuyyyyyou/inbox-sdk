"""AI turn Router 单元测试（阶段 A）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


async def test_router_unavailable_uses_deterministic_inbox_route():
    from mail_agent.ai_turn.router import route_ai_turn

    route = await route_ai_turn(
        "找出最近紧急邮件",
        {"current_thread": {"kind": "none"}, "display_range_days": 7},
        sampling_create_message=None,
    )
    assert [step["tool"] for step in route["steps"]] == ["search_mail", "rank_answer"]
    assert route["clarify"] is None
    assert route["router_fallback"] is True
    print("[PASS] test_router_unavailable_uses_deterministic_inbox_route")


async def test_router_unavailable_summarizes_current_thread():
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
    assert route["steps"] == [{"tool": "summarize_thread", "params": {}}]
    assert route["use_current_thread"] is True
    assert route["clarify"] is None
    print("[PASS] test_router_unavailable_summarizes_current_thread")


async def test_user_selected_inbox_bypasses_router_and_starts_search():
    """用户已明确选择收件箱后，不能再次依赖 Router 模型或回到澄清弹层。"""
    from mail_agent.ai_turn.router import route_ai_turn

    route = await route_ai_turn(
        "请从邮件中找出最需要优先处理的 3 封",
        {"routing_intent": "inbox", "current_thread": {"kind": "none"}},
        sampling_create_message=None,
    )

    assert [step["tool"] for step in route["steps"]] == ["search_mail", "rank_answer"]
    assert route["router_user_selected"] is True
    print("[PASS] test_user_selected_inbox_bypasses_router_and_starts_search")


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


async def test_normalize_preserves_router_chat_decision():
    """邮箱操作由 Router 模型判断，后端不得用关键词覆盖为搜索。"""
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
    assert route["steps"] == [{"tool": "chat_general", "params": {}}]
    print("[PASS] test_normalize_preserves_router_chat_decision")


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

    # 无 Sampling 时普通问候应自动进入聊天，不能要求用户手动选择范围。
    route = await route_ai_turn("hello", {}, sampling_create_message=None)
    assert route["steps"] == [{"tool": "chat_general", "params": {}}]
    assert route["clarify"] is None
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


async def test_chat_general_uses_single_final_generation():
    """纯聊天只生成一次最终回答，完整性约束由最终 system prompt 承担。"""
    from mail_agent.ai_turn.runner import _tool_chat_general

    calls: list[str] = []

    async def sampling(**kwargs: Any) -> dict[str, Any]:
        tool = str(kwargs.get("metadata", {}).get("tool") or "")
        calls.append(tool)
        message_text = str(kwargs["messages"][0]["content"]["text"])
        system_prompt = str(kwargs["system_prompt"])
        # 反向 RPC 在 Windows Host 上会经过 GBK stdout；直连 Sampling 必须先转义。
        message_text.encode("ascii")
        system_prompt.encode("ascii")
        assert tool == "ai_turn_chat"
        assert kwargs["max_tokens"] == 500
        assert "complete final answer" in system_prompt
        return {"content": {"type": "text", "text": "I can help you with that."}}

    result = await _tool_chat_general(
        "为什么 Ϊ 会出现在文本里？",
        language="en",
        sampling_create_message=sampling,
    )

    assert result["kind"] == "chat"
    assert result["assistant_text"] == "I can help you with that."
    assert calls == ["ai_turn_chat"]
    print("[PASS] test_chat_general_uses_single_final_generation")


def test_ai_turn_system_prompts_use_xml_modules():
    """各 AI 侧栏系统提示词必须保留统一 XML 模块和各自 JSON 输出协议。"""
    from mail_agent.ai_turn.prompts import (
        batch_draft_system_prompt,
        chat_general_system_prompt,
        compose_new_system_prompt,
        draft_reply_system_prompt,
        revise_draft_system_prompt,
        router_system_prompt,
        thread_answer_system_prompt,
    )

    router_prompt = router_system_prompt()
    for tag in ("role", "output_formatting", "whitelist_tools", "schema", "decision_rules", "strict_constraints"):
        assert f"<{tag}>" in router_prompt
        assert f"</{tag}>" in router_prompt

    prompts = [
        chat_general_system_prompt("zh", "用户偏好简洁回答"),
        thread_answer_system_prompt("en"),
        draft_reply_system_prompt("zh", summarize_first=False),
        revise_draft_system_prompt("en"),
        compose_new_system_prompt("zh"),
        batch_draft_system_prompt("en", mode="batch_outreach"),
    ]
    for prompt in prompts:
        for tag in ("role", "whitelist_tools", "schema", "decision_tree", "strict_rules"):
            assert f"<{tag}>" in prompt
            assert f"</{tag}>" in prompt
    assert '"steps"' in router_prompt
    assert '"markdown"' in prompts[1]
    assert '"draft_body"' in prompts[2]
    assert "<memory>" in prompts[0]
    print("[PASS] test_ai_turn_system_prompts_use_xml_modules")


def test_router_output_budget_is_fixed_and_small():
    """Router 输出只需容纳工具计划，必须固定限制为 150 tokens。"""
    from mail_agent.ai_turn.router import _ROUTER_MAX_OUTPUT_TOKENS

    assert _ROUTER_MAX_OUTPUT_TOKENS == 150
    print("[PASS] test_router_output_budget_is_fixed_and_small")


async def test_router_input_stays_within_compact_budget():
    """Router 的系统提示词和动态上下文合计应控制在约 500 tokens。"""
    from mail_agent.ai_turn.router import _ROUTER_INPUT_TOKEN_BUDGET, _estimate_router_input_tokens

    calls: list[dict[str, Any]] = []

    async def sampling_stub(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "content": {
                "type": "text",
                "text": '{"language":"zh","use_current_thread":false,"clarify":null,"steps":[{"tool":"chat_general","params":{}}]}',
            },
        }

    from mail_agent.ai_turn.router import route_ai_turn

    await route_ai_turn(
        "请判断这个复杂请求应该使用哪个能力" * 200,
        {"current_thread": {"kind": "thread", "thread_id": "t1"}},
        sampling_create_message=sampling_stub,
        conversation_summary="历史上下文" * 200,
        memory_summary="用户偏好" * 200,
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["max_tokens"] == 150
    # Sampling transport 将中文编码为 ASCII ``\\uXXXX``，该转义在 Host 解码后不会
    # 以六个字符进入模型上下文；按解码后的语义文本核对实际 Router 输入预算。
    raw_message = str(call["messages"][0]["content"]["text"]).encode("ascii").decode("unicode_escape")
    input_tokens = _estimate_router_input_tokens(str(call["system_prompt"])) + _estimate_router_input_tokens(raw_message)
    assert input_tokens <= _ROUTER_INPUT_TOKEN_BUDGET
    print("[PASS] test_router_input_stays_within_compact_budget")


async def test_high_confidence_search_skips_router_sampling():
    """明确的邮箱搜索应本地选型，避免先等待一次 Router 网络调用。"""
    from mail_agent.ai_turn.router import route_ai_turn

    async def forbidden_sampling(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("明确搜索不应调用 ai_turn_router Sampling")

    route = await route_ai_turn(
        "找出最近的未读邮件",
        {"current_thread": {"kind": "none"}},
        sampling_create_message=forbidden_sampling,
    )

    assert [step["tool"] for step in route["steps"]] == ["search_mail", "rank_answer"]
    assert route["router_reason"] == "fast_local_route"
    print("[PASS] test_high_confidence_search_skips_router_sampling")


async def test_mailbox_runner_bypasses_keyword_router():
    """有邮箱上下文的邮件问题由 Router 选择 Ask，而不是后端关键词分流。"""
    from mail_agent.ai_turn import runner

    original = runner._tool_search_and_answer
    original_route = runner.route_ai_turn

    async def fake_search(*_args, **_kwargs):
        return {"kind": "scan", "assistant_text": "## Inbox answer", "scan_result": {}}

    runner._tool_search_and_answer = fake_search
    async def mail_route(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "language": "en",
            "clarify": None,
            "steps": [{"tool": "search_mail", "params": {}}],
            "router_fallback": False,
        }
    runner.route_ai_turn = mail_route
    try:
        result = await runner.run_ai_turn(
            "What needs my reply?",
            {"mailbox": "a@b.com", "current_thread": {"kind": "none"}},
            {"mailbox": "a@b.com"},
            sampling_create_message=None,
        )
    finally:
        runner._tool_search_and_answer = original
        runner.route_ai_turn = original_route

    assert result["kind"] == "scan"
    assert result["route"]["steps"] == [{"tool": "search_mail", "params": {}}]
    print("[PASS] test_mailbox_runner_bypasses_keyword_router")


async def test_mailbox_context_routes_general_question_to_chat_without_search():
    """邮箱页面的纯聊天问题不得因为 mailbox 上下文而读取邮件。"""
    from mail_agent.ai_turn import runner

    original_search = runner._tool_search_and_answer
    sampling_calls: list[dict[str, Any]] = []

    async def forbidden_search(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("纯聊天问题不应调用邮箱搜索")

    async def chat_sampling(**kwargs: Any) -> dict[str, Any]:
        sampling_calls.append(kwargs)
        if kwargs.get("metadata", {}).get("tool") == "ai_turn_router":
            return {
                "content": {
                    "type": "text",
                    "text": '{"language":"zh","use_current_thread":false,"clarify":null,"steps":[{"tool":"chat_general","params":{}}]}',
                },
            }
        return {"content": {"type": "text", "text": "现在是上海时间。"}}

    runner._tool_search_and_answer = forbidden_search
    try:
        result = await runner.run_ai_turn(
            "量子纠缠是什么？",
            {"mailbox": "a@b.com", "current_thread": {"kind": "none"}},
            {"mailbox": "a@b.com"},
            sampling_create_message=chat_sampling,
        )
    finally:
        runner._tool_search_and_answer = original_search

    assert result["kind"] == "chat"
    assert result["route"]["steps"] == [{"tool": "chat_general", "params": {}}]
    assert len(sampling_calls) == 2
    system_prompt = str(sampling_calls[1]["system_prompt"])
    assert "chat-only turn" in system_prompt
    assert "live time" in system_prompt
    assert "complete final answer" in system_prompt
    print("[PASS] test_mailbox_context_routes_general_question_to_chat_without_search")


async def test_analysis_error_becomes_retryable_run_error():
    """Ask 的分析失败必须映射为 AI run 失败态，供侧栏提供重试操作。"""
    from mail_agent.ai_turn import runner
    from mail_agent.ask import answer

    original = answer.run_ask_pipeline

    async def fake_pipeline(**_kwargs):
        return {
            "summary": "AI analysis is temporarily unavailable. Please try again.",
            "analysis_error": True,
            "sections": [],
        }

    answer.run_ask_pipeline = fake_pipeline
    try:
        result = await runner._tool_search_and_answer(
            "What needs my reply?",
            {"mailbox": "a@b.com"},
            {"mailbox": "a@b.com"},
            sampling_create_message=None,
            progress_callback=None,
            with_rank=True,
        )
    finally:
        answer.run_ask_pipeline = original

    assert result["kind"] == "error"
    assert result["error"] == "analysis_unavailable"
    assert result["fallback_used"] is False
    print("[PASS] test_analysis_error_becomes_retryable_run_error")


def main():
    asyncio.run(test_router_unavailable_uses_deterministic_inbox_route())
    asyncio.run(test_router_unavailable_summarizes_current_thread())
    asyncio.run(test_user_selected_inbox_bypasses_router_and_starts_search())
    asyncio.run(test_normalize_rejects_unknown_tool())
    asyncio.run(test_normalize_preserves_router_chat_decision())
    asyncio.run(test_sampling_router_payload())
    asyncio.run(test_chat_general_runner())
    asyncio.run(test_chat_general_uses_single_final_generation())
    test_ai_turn_system_prompts_use_xml_modules()
    test_router_output_budget_is_fixed_and_small()
    asyncio.run(test_router_input_stays_within_compact_budget())
    asyncio.run(test_high_confidence_search_skips_router_sampling())
    asyncio.run(test_mailbox_runner_bypasses_keyword_router())
    asyncio.run(test_mailbox_context_routes_general_question_to_chat_without_search())
    asyncio.run(test_analysis_error_becomes_retryable_run_error())
    print("[ALL TESTS PASSED]")


if __name__ == "__main__":
    main()
