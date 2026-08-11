"""本地侧栏 Agent Session：与 Host 共用工具白名单。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from anna_inbox_executa.ai_agent_tools_flow import AI_AGENT_SESSION_TOOL_NAMES
from anna_inbox_executa.local_agent_session import (
    _assemble_outcome,
    _force_final_text,
    _normalize_plan,
    _planner_system_prompt,
    run_local_agent_session,
)
from anna_inbox_executa.ai_turn_flow import _confirmed_evidence_thread_ids


def test_normalize_plan_accepts_host_tool_names_only():
    plan = _normalize_plan({"action": "tool_call", "tool": "query_mail_evidence", "arguments": {"user_text": "find mail"}})
    assert plan["action"] == "tool_call"
    assert plan["tool"] == "query_mail_evidence"
    assert "search_email" not in AI_AGENT_SESSION_TOOL_NAMES

    # Sampling 可能把工具名直接放进 action，仍应兼容为标准工具调用。
    direct_action = _normalize_plan({
        "action": "query_mail_evidence",
        "arguments": {"user_text": "find mail"},
    })
    assert direct_action["action"] == "tool_call"
    assert direct_action["tool"] == "query_mail_evidence"
    assert direct_action["arguments"]["user_text"] == "find mail"

    bad = _normalize_plan({"action": "tool_call", "tool": "search_mail", "arguments": {}})
    assert bad["action"] == "final"
    assert bad["tool"] == ""


def test_planner_system_prompt_stays_within_route_budget():
    assert len(_planner_system_prompt()) <= 7_000
    prompt = _planner_system_prompt()
    assert "Never include or echo ui_context" in prompt
    assert "recent_conversation" in prompt
    assert "screen" in prompt


def test_local_session_confirmation_generates_batch_compose_without_search() -> None:
    captured: dict[str, object] = {}

    async def fake_batch_compose(user_text, ui_context, arguments, **kwargs):
        captured["user_text"] = user_text
        captured["ui_context"] = ui_context
        captured["arguments"] = arguments
        captured["confirmed"] = kwargs["confirmed"]
        return {
            "kind": "draft",
            "assistant_text": "已生成 3 封新邮件草稿。",
            "compose_artifacts": [{"type": "compose_draft", "body": "Hello", "recipients": ["one@example.com"]}],
        }

    async def unexpected_sampling(**_kwargs):
        raise AssertionError("Batch compose confirmation must not enter the local planner")

    context = {
        "mailbox": "owner@example.com",
        "recent_conversation": [
            {"role": "user", "content": "分别写给 one@example.com、two@example.com 和 three@example.com。模板 1 和模板 2 都在这里。"},
            {"role": "assistant", "content": "我会分别匹配模板。是否确认按此方案生成这三封邮件草稿？"},
            {"role": "user", "content": "确认"},
        ],
    }
    with patch("mail_agent.ai_turn.tools.tool_batch_compose_new", new=fake_batch_compose):
        outcome = asyncio.run(run_local_agent_session(
            "确认",
            context,
            {"run_id": "local-batch-compose-confirm"},
            sampling_create_message=unexpected_sampling,
        ))

    assert outcome["kind"] == "draft"
    assert captured["confirmed"] is True
    assert "one@example.com" in str(captured["user_text"])
    assert "分别匹配模板" in str(captured["user_text"])


def test_local_session_continue_generates_next_batch_from_same_request() -> None:
    captured: dict[str, object] = {}

    async def fake_batch_compose(user_text, _ui_context, _arguments, **kwargs):
        captured["user_text"] = user_text
        captured["offset"] = kwargs["batch_offset"]
        return {"kind": "draft", "assistant_text": "已生成下一批。", "compose_artifacts": []}

    source = "Write to " + ", ".join(f"user{index}@example.com" for index in range(12))
    context = {
        "language_hint": "zh",
        "recent_conversation": [
            {"role": "user", "content": source},
            {"role": "assistant", "content": "已生成 10 封新邮件草稿。还有 2 位收件人未生成。回复“继续”后再生成下一批。"},
        ],
    }
    async def unexpected_sampling(**_kwargs):
        raise AssertionError("Continuation must not enter the planner")

    with patch("mail_agent.ai_turn.tools.tool_batch_compose_new", new=fake_batch_compose):
        outcome = asyncio.run(run_local_agent_session(
            "继续",
            context,
            {},
            sampling_create_message=unexpected_sampling,
        ))

    assert outcome["kind"] == "draft"
    assert captured["offset"] == 10
    assert str(captured["user_text"]).count("@example.com") == 12


def test_selected_batch_draft_skips_router_and_mail_search() -> None:
    """收件箱多选的批量回复必须直接进入批量草稿预览。"""
    captured: dict[str, object] = {}

    async def fake_batch_draft(user_text, ui_context, **kwargs):
        captured["user_text"] = user_text
        captured["ui_context"] = ui_context
        captured["confirmed"] = kwargs["confirmed"]
        return {"kind": "batch_draft_preview", "assistant_text": "请确认后生成草稿。"}

    async def unexpected_sampling(**_kwargs):
        raise AssertionError("Selected batch draft must not enter the local planner")

    context = {
        "mailbox": "owner@example.com",
        "selected_threads": [
            {"mailbox": "owner@example.com", "message_id": "m1", "thread_id": "t1"},
            {"mailbox": "owner@example.com", "message_id": "m2", "thread_id": "t2"},
        ],
    }
    with patch("mail_agent.ai_turn.tools.tool_batch_draft", new=fake_batch_draft):
        outcome = asyncio.run(run_local_agent_session(
            "为每封选中的邮件起草简短回复",
            context,
            {"run_id": "local-selected-batch-draft"},
            sampling_create_message=unexpected_sampling,
        ))

    assert outcome["kind"] == "batch_draft_preview"
    assert captured["confirmed"] is False


def test_local_session_keeps_full_template_until_contact_details_arrive() -> None:
    """模板录入不写长期 Memory；联系人资料到达后带完整模板直接起草。"""
    template = (
        "先不要生成draft，把以下模板加入上下文\n"
        "Subject: Loved your project [Repo Name]\n"
        "Body: Hi [Developer Name],\n\n"
        "This is the complete Founding Builder Program template."
    )
    captured: dict[str, object] = {}

    async def unexpected_sampling(**_kwargs):
        raise AssertionError("Template capture and template compose must not enter the planner")

    async def fake_tool(tool, arguments, _invoke_id):
        captured["tool"] = tool
        captured["user_text"] = arguments["user_text"]
        return {
            "success": True,
            "data": {
                "kind": "draft",
                "assistant_text": "草稿已生成。",
                "artifact": {"type": "compose_draft", "body": "Hi OrbitApp,"},
            },
        }

    captured_outcome = asyncio.run(run_local_agent_session(
        template,
        {"mailbox": "owner@example.com"},
        {},
        sampling_create_message=unexpected_sampling,
    ))
    assert captured_outcome["kind"] == "chat"
    assert "暂不生成" in captured_outcome["assistant_text"]

    context = {
        "mailbox": "owner@example.com",
        "local_draft_template": template,
        "local_draft_template_awaiting_details": True,
    }
    wait_outcome = asyncio.run(run_local_agent_session(
        "接下来我会输入邀请人、GitHub 仓库链接、备注说明和联系方式，输入后再生成 draft 卡片。",
        context,
        {},
        sampling_create_message=unexpected_sampling,
    ))
    assert wait_outcome["kind"] == "chat"
    assert "等待" in wait_outcome["assistant_text"]

    with patch("anna_inbox_executa.local_agent_session.handle_ai_agent_tool", new=fake_tool):
        outcome = asyncio.run(run_local_agent_session(
            "OrbitApp https://example.com/orbit request@example.com 使用我最开始给你的模板，并且将方括号里面的内容替换。",
            context,
            {},
            sampling_create_message=unexpected_sampling,
        ))

    assert outcome["kind"] == "draft"
    assert captured["tool"] == "ai_compose_new"
    assert "[full_local_template]" in str(captured["user_text"])
    assert template in str(captured["user_text"])


def test_assemble_prefers_structured_tool_outcome():
    out = _assemble_outcome(
        final_text="Here is the draft.",
        tool_records=[
            {
                "success": True,
                "data": {
                    "kind": "draft",
                    "assistant_text": "Draft body note",
                    "artifact": {"type": "draft_reply", "body": "Hi"},
                },
            }
        ],
        language="en",
    )
    assert out["kind"] == "draft"
    assert out["artifact"]["body"] == "Hi"
    assert out["assistant_text"] == "Here is the draft."


def test_assemble_draft_boundary_forces_unsent_status():
    """local session 的最终边界不得传播模型的已发送语义。"""
    out = _assemble_outcome(
        final_text="已发送成功，请查看。",
        tool_records=[
            {
                "tool": "ai_draft_reply",
                "success": True,
                "data": {
                    "kind": "draft",
                    "assistant_text": "已发送成功。",
                    "artifact": {"type": "draft_reply", "body": "Hi"},
                },
            }
        ],
        language="zh",
    )
    assert out["artifact"]["delivery_status"] == "not_sent"
    assert out["artifact"]["requires_user_review"] is True
    assert "未发送" in out["assistant_text"]
    assert "已发送" not in out["assistant_text"]


def test_confirmed_evidence_thread_ids_only_exposes_strict_matches():
    """Local 轮询结果只为严格命中的邮件开放详情引用。"""
    assert _confirmed_evidence_thread_ids({
        "match_status": "confirmed",
        "results": [
            {"thread_id": "thread-1"},
            {"thread_ref": "THREAD_REF_thread-2"},
            {"thread_id": "thread-1"},
        ],
    }) == ["thread-1", "thread-2"]
    assert _confirmed_evidence_thread_ids({
        "match_status": "no_confirmed_match",
        "results": [{"thread_ref": "THREAD_REF_nearby"}],
    }) == []


def test_assemble_falls_back_to_search_results_when_final_empty():
    """工具已有搜索命中但模型未给 final 时，不得返回笼统空失败文案。"""
    from anna_inbox_executa.local_agent_session import _fallback_text_from_tools

    text = _fallback_text_from_tools(
        [
            {
                "success": True,
                "data": {
                    "kind": "search",
                    "gmail_query": "from:fal invoice",
                    "display_range_days": 60,
                    "count": 1,
                    "results": [
                        {
                            "subject": "New invoice from fal",
                            "from": "fal@example.com",
                            "date": "2026-07-20",
                            "thread_ref": "THREAD_REF_abc",
                            "attachmentFilenames": ["invoice.pdf"],
                        }
                    ],
                },
            }
        ],
        "zh",
    )
    assert "invoice.pdf" in text
    assert "THREAD_REF_abc" in text
    assert "暂时无法完成" not in text

    out = _assemble_outcome(final_text="", tool_records=[
        {
            "success": True,
            "data": {
                "kind": "search",
                "count": 0,
                "results": [],
                "display_range_days": 60,
                "gmail_query": "ssl certificate",
            },
        }
    ], language="zh")
    assert out["assistant_text"]
    assert "暂时无法完成" not in out["assistant_text"]
    assert "0" in out["assistant_text"] or "未找到" in out["assistant_text"]


def test_evidence_fallback_clarifies_empty_strict_result_with_nearby_thread():
    """本地模式无精确命中时也应说明相近线程并索取关键条件，不能只报搜索失败。"""
    from anna_inbox_executa.local_agent_session import _fallback_text_from_tools

    text = _fallback_text_from_tools(
        [
            {
                "success": True,
                "data": {
                    "kind": "evidence",
                    "match_status": "no_confirmed_match",
                    "results": [],
                    "nearby_results": [
                        {
                            "subject": "Re: Collaboration: Putting Inbox to the test",
                            "from": "Mira <mira@contact.example>",
                            "date": "2026-07-14",
                            "thread_ref": "THREAD_REF_nearby",
                        },
                    ],
                },
            },
        ],
        "zh",
    )
    assert "没有确认" in text
    assert "Putting Inbox" in text
    assert "完整发件人" in text


def test_run_local_agent_session_force_final_when_empty_after_tools():
    """选型在工具后未产出 final 时，应强制合成而非直接空失败。"""
    call_count = {"n": 0}

    async def _llm_json_safe(sampling_fn, **kwargs):
        call_count["n"] += 1
        meta = kwargs.get("metadata") or {}
        if meta.get("step") == "force_final":
            return {
                "payload": {
                    "action": "final",
                    "tool": "",
                    "arguments": {},
                    "text": "强制合成后的回答，含 [THREAD_REF_x]。",
                },
                "fallback_used": False,
            }
        if call_count["n"] == 1:
            return {
                "payload": {
                    "action": "tool_call",
                    "tool": "query_mail_evidence",
                    "arguments": {"user_text": "找 alice 的邮件"},
                    "text": "",
                },
                "fallback_used": False,
            }
        # 第二步：错误地给出空 final
        return {
            "payload": {"action": "final", "tool": "", "arguments": {}, "text": ""},
            "fallback_used": False,
        }

    async def _fake_tool(tool, arguments, invoke_id, **_kwargs):
        return {
            "success": True,
            "tool": tool,
            "data": {
                "kind": "evidence",
                "query_plan": {"query": "from:alice"},
                "results": [{"subject": "Hi", "thread_ref": "THREAD_REF_x"}],
                "count": 1,
            },
        }

    async def _sampling(**kwargs):
        return {"content": {"type": "text", "text": "{}"}}

    async def _run():
        with patch(
            "anna_inbox_executa.local_agent_session.call_llm_json_safe",
            new=AsyncMock(side_effect=_llm_json_safe),
        ), patch(
            "anna_inbox_executa.local_agent_session.handle_ai_agent_tool",
            new=AsyncMock(side_effect=_fake_tool),
        ):
            return await run_local_agent_session(
                "找 alice 的邮件",
                {"mailbox": "a@b.com", "display_range_days": 60},
                {"mailbox": "a@b.com", "ai_provider": "anna-llm"},
                sampling_create_message=_sampling,
                invoke_id="test-force-final",
            )

    outcome = asyncio.run(_run())
    assert "强制合成" in outcome["assistant_text"]
    assert "暂时无法完成" not in outcome["assistant_text"]


def test_force_final_truncation_uses_evidence_fallback():
    """force-final 截断或运行时 fallback 时，必须返回证据摘要而不是半截答案。"""
    async def _llm_json_safe(_sampling_fn, **_kwargs):
        return {
            "payload": {"action": "final", "text": "答案被截断"},
            "text": "",
            "fallback_used": True,
            "truncated": True,
            "fallback_kind": "truncated_json",
        }

    result = asyncio.run(_force_final_text(
        sampling_create_message=object(),
        trail_parts=["[tool_result name=query_mail_evidence]\n{}"],
        language="zh",
        tool_records=[{
            "success": True,
            "data": {
                "kind": "evidence",
                "results": [{"subject": "付款确认", "from": "a@example.com", "thread_ref": "THREAD_REF_pay"}],
            },
        }],
    ))

    assert "付款确认" in result
    assert "THREAD_REF_pay" in result
    assert "答案被截断" not in result


def test_force_final_accepts_completed_retry_with_terminal_sentence():
    """运行时重试成功后，force-final 接受有明确终止句的完整回答。"""
    async def _llm_json_safe(_sampling_fn, **_kwargs):
        return {
            "payload": {"action": "final", "text": "已确认付款安排，详见 [THREAD_REF_pay]。"},
            "fallback_used": False,
            "truncated": False,
            "fallback_kind": "",
        }

    result = asyncio.run(_force_final_text(
        sampling_create_message=object(),
        trail_parts=["[tool_result name=query_mail_evidence]\n{}"],
        language="zh",
        tool_records=[],
    ))

    assert result.endswith("。")
    assert "已确认付款安排" in result


def test_run_local_agent_session_calls_handle_ai_agent_tool():
    async def _sampling(**kwargs):
        return {
            "content": {
                "type": "text",
                "text": '{"action":"tool_call","tool":"query_mail_evidence","arguments":{"user_text":"search inbox"},"text":""}',
            }
        }

    # 第二步 final
    call_count = {"n": 0}
    call_metadata: list[dict] = []

    async def _llm_json_safe(sampling_fn, **kwargs):
        call_count["n"] += 1
        call_metadata.append(dict(kwargs.get("metadata") or {}))
        if call_count["n"] == 1:
            return {
                "payload": {
                    "action": "tool_call",
                    "tool": "query_mail_evidence",
                    "arguments": {
                        "user_text": "search inbox",
                        "query_plan": {
                            "intent": "find",
                            "query": "in:inbox",
                            "order": "newest",
                            "answer_mode": "llm",
                            "needs": ["metadata"],
                        },
                    },
                    "text": "",
                },
                "fallback_used": False,
            }
        return {
            "payload": {"action": "final", "tool": "", "arguments": {}, "text": "Found 1 email."},
            "fallback_used": False,
        }

    async def _fake_tool(tool, arguments, invoke_id):
        assert tool in AI_AGENT_SESSION_TOOL_NAMES
        assert tool == "query_mail_evidence"
        assert arguments.get("mailbox") == "a@b.com"
        assert arguments["query_plan"]["query"] == "in:inbox"
        return {
            "success": True,
            "tool": tool,
            "data": {
                "kind": "evidence",
                "query_plan": {"query": "in:inbox"},
                "results": [],
                "count": 0,
            },
        }

    async def _run():
        with patch(
            "anna_inbox_executa.local_agent_session.call_llm_json_safe",
            new=AsyncMock(side_effect=_llm_json_safe),
        ), patch(
            "anna_inbox_executa.local_agent_session.handle_ai_agent_tool",
            new=AsyncMock(side_effect=_fake_tool),
        ):
            return await run_local_agent_session(
                "search inbox",
                {"mailbox": "a@b.com", "display_range_days": 7},
                {"mailbox": "a@b.com", "ai_provider": "anna-llm"},
                sampling_create_message=_sampling,
                invoke_id="test-invoke",
            )

    outcome = asyncio.run(_run())
    assert outcome["kind"] == "chat"
    assert "Found 1 email" in outcome["assistant_text"]
    assert outcome.get("scan_query") == "in:inbox"
    assert call_metadata[0]["tool"] == "local_agent_session.route.sample"


def test_run_local_agent_session_strips_model_context_before_tool_call():
    """模型回显的大上下文不得传给工具，但权威 ui_context 必须仍被注入。"""
    seen: list[dict] = []
    model_context = {
        "ui_context": {"mailbox": "wrong@example.com", "huge": "x" * 10_000},
        "recent_conversation": [{"role": "user", "content": "full email"}],
        "screen": {"html": "x" * 10_000},
        "query_plan": {
            "query": "in:inbox",
            "nested": {"ui_context": {"bad": True}, "keep": "yes"},
        },
    }

    async def fake_plan(*_args, **kwargs):
        if (kwargs.get("metadata") or {}).get("step") == "force_final":
            return {"payload": {"action": "final", "text": "已完成。"}}
        return {
            "payload": {
                "action": "tool_call",
                "tool": "query_mail_evidence",
                "arguments": model_context,
            }
        }

    async def fake_tool(tool, arguments, _invoke_id):
        seen.append(dict(arguments))
        return {
            "success": True,
            "tool": tool,
            "data": {
                "kind": "evidence",
                "assistant_text": "已完成。",
                "results": [],
                "query_plan": {"query": "in:inbox"},
            },
        }

    async def run():
        with patch(
            "anna_inbox_executa.local_agent_session.call_llm_json_safe",
            new=AsyncMock(side_effect=fake_plan),
        ), patch(
            "anna_inbox_executa.local_agent_session.handle_ai_agent_tool",
            new=AsyncMock(side_effect=fake_tool),
        ):
            return await run_local_agent_session(
                "查收件箱",
                {"mailbox": "authoritative@example.com", "conversation_id": "chat-1"},
                {"mailbox": "authoritative@example.com"},
                sampling_create_message=object(),
                invoke_id="test-strip-model-context",
            )

    outcome = asyncio.run(run())
    assert outcome["assistant_text"] == "已完成。"
    assert len(seen) == 1
    arguments = seen[0]
    assert arguments["ui_context"] == {
        "mailbox": "authoritative@example.com",
        "conversation_id": "chat-1",
    }
    assert "recent_conversation" not in arguments
    assert "screen" not in arguments
    assert "ui_context" not in arguments["query_plan"]["nested"]
    assert arguments["query_plan"]["nested"]["keep"] == "yes"


def test_run_local_agent_session_retries_truncated_route_with_larger_budget() -> None:
    """选型 JSON 截断时，第二次 Sampling 应使用更高预算并返回完整回答。"""
    requests: list[dict] = []
    responses = iter([
        '{"action":"final","tool":"","arguments":{},"text":"回答尚未完成',
        '{"action":"final","tool":"","arguments":{},"text":"完整回答已生成。"}',
    ])

    async def _sampling(**kwargs):
        requests.append(kwargs)
        return {"content": {"type": "text", "text": next(responses)}}

    async def _run():
        with patch(
            "anna_inbox_executa.local_agent_session.handle_ai_agent_tool",
            new=AsyncMock(),
        ):
            return await run_local_agent_session(
                "请给出完整回答",
                {"mailbox": "a@b.com"},
                {"mailbox": "a@b.com"},
                sampling_create_message=_sampling,
                invoke_id="test-truncated-route-retry",
            )

    outcome = asyncio.run(_run())
    assert [request["max_tokens"] for request in requests] == [1600, 2400]
    assert outcome["assistant_text"] == "完整回答已生成。"
    assert outcome["kind"] == "chat"
    assert not outcome.get("fallback_used")


def test_run_local_agent_session_does_not_accept_truncated_route_final() -> None:
    """route 返回运行时截断的 final 时，不得把半截文本当作正常聊天回答。"""
    async def fake_plan(*_args, **_kwargs):
        return {
            "payload": {"action": "final", "text": "半截回答。"},
            "fallback_used": True,
            "truncated": True,
            "fallback_kind": "truncated_json",
            "fallback_reason": "runtime_stop_length",
        }

    async def run():
        with patch("anna_inbox_executa.local_agent_session.call_llm_json_safe", new=AsyncMock(side_effect=fake_plan)):
            return await run_local_agent_session(
                "请回答",
                {"mailbox": "a@b.com"},
                {"mailbox": "a@b.com"},
                sampling_create_message=object(),
                invoke_id="test-truncated-route-final",
            )

    outcome = asyncio.run(run())
    assert outcome["kind"] == "error"
    assert outcome["fallback_used"] is True
    assert outcome["error"] == "planner_unavailable"
    assert "半截回答" not in outcome["assistant_text"]


def test_run_local_agent_session_propagates_force_final_degradation() -> None:
    """force-final 失败后使用证据摘要时，结果必须显式标记降级及原因。"""
    calls = {"n": 0}

    async def fake_plan(*_args, **kwargs):
        calls["n"] += 1
        if (kwargs.get("metadata") or {}).get("step") == "force_final":
            return {
                "payload": {"action": "final", "text": ""},
                "fallback_used": True,
                "fallback_kind": "truncated_json",
                "fallback_reason": "runtime_stop_length",
                "truncated": True,
            }
        return {"payload": {"action": "tool_call", "tool": "query_mail_evidence", "arguments": {}}}

    async def fake_tool(tool, _arguments, _invoke_id):
        return {
            "success": True,
            "tool": tool,
            "data": {
                "kind": "evidence",
                "results": [{"thread_ref": "THREAD_REF_t1", "subject": "Payment"}],
            },
        }

    async def run():
        with patch("anna_inbox_executa.local_agent_session.call_llm_json_safe", new=AsyncMock(side_effect=fake_plan)), patch(
            "anna_inbox_executa.local_agent_session.handle_ai_agent_tool", new=AsyncMock(side_effect=fake_tool),
        ):
            return await run_local_agent_session(
                "付款怎么安排？",
                {"mailbox": "a@b.com"},
                {"mailbox": "a@b.com"},
                sampling_create_message=object(),
                invoke_id="test-force-final-degraded",
            )

    outcome = asyncio.run(run())
    assert calls["n"] == 2
    assert outcome["fallback_used"] is True
    assert outcome["fallback_reason"] == "runtime_stop_length"
    assert "THREAD_REF_t1" in outcome["assistant_text"]


def test_run_local_agent_session_executes_evidence_once_before_final() -> None:
    """P3 Evidence 返回后不允许本地外层规划器再次调用同一复合工具。"""
    calls = {"tool": 0, "plan": 0}

    async def fake_plan(*_args, **kwargs):
        calls["plan"] += 1
        if (kwargs.get("metadata") or {}).get("step") == "force_final":
            return {"payload": {"action": "final", "text": "根据邮件正文，已确认付款安排。"}}
        return {"payload": {"action": "tool_call", "tool": "query_mail_evidence", "arguments": {}}}

    async def fake_tool(tool, _arguments, _invoke_id):
        calls["tool"] += 1
        return {"success": True, "tool": tool, "data": {"kind": "evidence", "results": [{"thread_ref": "THREAD_REF_t1", "subject": "Payment"}]}}

    async def run():
        with patch("anna_inbox_executa.local_agent_session.call_llm_json_safe", new=AsyncMock(side_effect=fake_plan)), patch(
            "anna_inbox_executa.local_agent_session.handle_ai_agent_tool", new=AsyncMock(side_effect=fake_tool),
        ):
            return await run_local_agent_session(
                "付款怎么安排的？",
                {"mailbox": "a@b.com", "conversation_id": "chat-1"},
                {"mailbox": "a@b.com"},
                sampling_create_message=object(),
                invoke_id="test-evidence-once",
            )

    outcome = asyncio.run(run())
    assert calls["tool"] == 1
    assert "付款安排" in outcome["assistant_text"]


def test_run_local_agent_session_stops_after_new_compose_draft() -> None:
    """新邮件工具已返回草稿时，不得继续选型并误触发邮件检索。"""
    tools: list[str] = []

    async def fake_plan(*_args, **_kwargs):
        return {"payload": {"action": "tool_call", "tool": "ai_compose_new", "arguments": {}}}

    async def fake_tool(tool, _arguments, _invoke_id):
        tools.append(tool)
        return {
            "success": True,
            "tool": tool,
            "data": {
                "kind": "draft",
                "assistant_text": "已生成新邮件草稿。",
                "artifact": {
                    "type": "compose_draft",
                    "recipients": ["one@example.com"],
                    "subject": "Hello",
                    "body": "Hi there,",
                },
            },
        }

    async def run():
        with patch("anna_inbox_executa.local_agent_session.call_llm_json_safe", new=AsyncMock(side_effect=fake_plan)), patch(
            "anna_inbox_executa.local_agent_session.handle_ai_agent_tool", new=AsyncMock(side_effect=fake_tool),
        ):
            return await run_local_agent_session(
                "给 one@example.com 写一封新邮件",
                {"mailbox": "a@b.com", "conversation_id": "chat-compose"},
                {"mailbox": "a@b.com"},
                sampling_create_message=object(),
                invoke_id="test-compose-stops-after-draft",
            )

    outcome = asyncio.run(run())
    assert tools == ["ai_compose_new"]
    assert outcome["artifact"]["type"] == "compose_draft"
    assert outcome["assistant_text"] == "已生成新邮件草稿。"


def test_run_local_agent_session_keeps_all_batch_compose_artifacts() -> None:
    """批量新邮件的所有 compose_artifacts 必须透传给前端，且不得转入检索。"""
    tools: list[str] = []

    async def fake_plan(*_args, **_kwargs):
        return {"payload": {"action": "tool_call", "tool": "ai_compose_new", "arguments": {}}}

    async def fake_tool(tool, _arguments, _invoke_id):
        tools.append(tool)
        return {
            "success": True,
            "tool": tool,
            "data": {
                "kind": "draft",
                "assistant_text": "已生成 2 封新邮件草稿。",
                "artifact": {"type": "compose_draft", "recipients": ["one@example.com"], "body": "One"},
                "compose_artifacts": [
                    {"type": "compose_draft", "recipients": ["one@example.com"], "body": "One"},
                    {"type": "compose_draft", "recipients": ["two@example.com"], "body": "Two"},
                ],
            },
        }

    async def run():
        with patch("anna_inbox_executa.local_agent_session.call_llm_json_safe", new=AsyncMock(side_effect=fake_plan)), patch(
            "anna_inbox_executa.local_agent_session.handle_ai_agent_tool", new=AsyncMock(side_effect=fake_tool),
        ):
            return await run_local_agent_session(
                "给 one@example.com 和 two@example.com 分别写新邮件",
                {"mailbox": "a@b.com", "conversation_id": "chat-batch-compose"},
                {"mailbox": "a@b.com"},
                sampling_create_message=object(),
                invoke_id="test-batch-compose-stops-after-draft",
            )

    outcome = asyncio.run(run())
    assert tools == ["ai_compose_new"]
    assert len(outcome["compose_artifacts"]) == 2
    assert outcome["compose_artifacts"][1]["recipients"] == ["two@example.com"]


def test_run_local_agent_session_previews_draft_before_card() -> None:
    tools: list[str] = []

    async def fake_plan(*_args, **kwargs):
        if (kwargs.get("metadata") or {}).get("step") == "force_final":
            return {
                "payload": {
                    "action": "final",
                    "text": (
                        "草稿正文：\nHi Pervaiz,\nThanks for waiting.\n\n"
                        "请确认后我再生成可插入的草稿卡片。"
                    ),
                }
            }
        return {"payload": {"action": "final", "text": "请确认草稿。"}}

    async def fake_tool(tool, _arguments, _invoke_id):
        tools.append(tool)
        return {
            "success": True,
            "tool": tool,
            "data": {
                "kind": "evidence",
                "match_status": "confirmed",
                "results": [{"thread_ref": "THREAD_REF_t1", "subject": "Re: Collaboration"}],
            },
        }

    async def run():
        with patch("anna_inbox_executa.local_agent_session.call_llm_json_safe", new=AsyncMock(side_effect=fake_plan)), patch(
            "anna_inbox_executa.local_agent_session.handle_ai_agent_tool", new=AsyncMock(side_effect=fake_tool),
        ):
            return await run_local_agent_session(
                "请帮我回复这封邮件",
                {
                    "mailbox": "a@b.com",
                    "current_thread": {"mailbox": "a@b.com", "thread_id": "thread-1", "message_id": "message-1"},
                },
                {"mailbox": "a@b.com", "requested_artifact": "draft_reply"},
                sampling_create_message=object(),
                invoke_id="test-thread-draft-preview",
            )

    outcome = asyncio.run(run())
    assert tools == ["query_mail_evidence"]
    assert outcome.get("artifact") in (None, {})
    assert "请确认是否生成回复草稿" in outcome["assistant_text"]
    assert "草稿正文" not in outcome["assistant_text"]
    assert "Hi Pervaiz" not in outcome["assistant_text"]


def test_run_local_agent_session_creates_card_after_user_confirms_draft() -> None:
    """用户确认上一轮草稿正文后，才调用 ai_draft_reply 生成 draft 卡片。

    两步确认流的确认语必须同时明确指向当前线程（如「为这封邮件确认生成草稿」），
    仅说「开始吧」不会强制出卡片。
    """
    tools: list[str] = []

    async def fake_plan(*_args, **_kwargs):
        return {"payload": {"action": "final", "text": "已生成草稿卡片。"}}

    async def fake_tool(tool, _arguments, _invoke_id):
        tools.append(tool)
        return {
            "success": True,
            "tool": tool,
            "data": {
                "kind": "draft",
                "assistant_text": "我已根据邮件内容整理回复草稿，请核对邮件内容、收件人和主题。",
                "artifact": {
                    "type": "draft_reply",
                    "mailbox": "a@b.com",
                    "thread_id": "thread-1",
                    "message_id": "message-1",
                    "body": "Hi Pervaiz,\nThanks for waiting.",
                    "delivery_status": "not_sent",
                    "requires_user_review": True,
                },
            },
        }

    async def run():
        with patch("anna_inbox_executa.local_agent_session.call_llm_json_safe", new=AsyncMock(side_effect=fake_plan)), patch(
            "anna_inbox_executa.local_agent_session.handle_ai_agent_tool", new=AsyncMock(side_effect=fake_tool),
        ):
            return await run_local_agent_session(
                "为这封邮件确认生成草稿",
                {
                    "mailbox": "a@b.com",
                    "current_thread": {"mailbox": "a@b.com", "thread_id": "thread-1", "message_id": "message-1"},
                    "recent_conversation": [
                        {
                            "role": "assistant",
                            "content": "草稿正文：\nHi Pervaiz,\nThanks for waiting.\n请确认后我再生成卡片。",
                        }
                    ],
                },
                {"mailbox": "a@b.com"},
                sampling_create_message=object(),
                invoke_id="test-thread-draft-confirm",
            )

    outcome = asyncio.run(run())
    assert tools == ["ai_draft_reply"]
    assert outcome["kind"] == "draft"
    assert outcome["artifact"]["type"] == "draft_reply"
    assert outcome["artifact"]["delivery_status"] == "not_sent"


def test_run_local_agent_session_stops_after_search_field_clarification() -> None:
    """Evidence 要求选择搜索字段时，本地 Agent 不得再次选型或继续搜索。"""
    calls = {"plan": 0, "tool": 0}

    async def fake_plan(*_args, **_kwargs):
        calls["plan"] += 1
        return {"payload": {"action": "tool_call", "tool": "query_mail_evidence", "arguments": {}}}

    async def fake_tool(tool, _arguments, _invoke_id):
        calls["tool"] += 1
        return {
            "success": True,
            "tool": tool,
            "data": {
                "kind": "clarify",
                "assistant_text": "要按什么条件搜索这段信息？",
                "clarification": {
                    "kind": "search_field",
                    "original_input": '"launch" 邮件',
                    "question": "要按什么条件搜索这段信息？",
                    "actions": [{"id": "search_body", "label": "正文内容"}],
                    "freeform_enabled": True,
                },
            },
        }

    async def run():
        with patch("anna_inbox_executa.local_agent_session.call_llm_json_safe", new=AsyncMock(side_effect=fake_plan)), patch(
            "anna_inbox_executa.local_agent_session.handle_ai_agent_tool", new=AsyncMock(side_effect=fake_tool),
        ):
            return await run_local_agent_session(
                '"launch" 邮件在聊什么？',
                {"mailbox": "a@b.com", "conversation_id": "chat-1"},
                {"mailbox": "a@b.com"},
                sampling_create_message=object(),
                invoke_id="test-search-field-clarify",
            )

    outcome = asyncio.run(run())
    assert calls == {"plan": 1, "tool": 1}
    assert outcome["kind"] == "clarify"
    assert outcome["clarification"]["kind"] == "search_field"


def test_run_local_agent_session_forces_evidence_when_route_final_skips_search() -> None:
    """找邮类首轮若直接 final（含伪造字段澄清），必须强制一次 query_mail_evidence。"""
    tools: list[str] = []
    tool_args: list[dict] = []

    async def fake_plan(*_args, **_kwargs):
        return {
            "payload": {
                "action": "final",
                "text": "要按什么条件搜索这段信息？",
            }
        }

    async def fake_tool(tool, arguments, _invoke_id):
        tools.append(tool)
        tool_args.append(dict(arguments or {}))
        return {
            "success": True,
            "tool": tool,
            "data": {
                "kind": "evidence_template",
                "assistant_text": "已在当前全部本地缓存中检索，未找到与您条件匹配的相关邮件。",
                "match_status": "no_confirmed_match",
                "results": [],
            },
        }

    async def run():
        with patch("anna_inbox_executa.local_agent_session.call_llm_json_safe", new=AsyncMock(side_effect=fake_plan)), patch(
            "anna_inbox_executa.local_agent_session.handle_ai_agent_tool", new=AsyncMock(side_effect=fake_tool),
        ):
            return await run_local_agent_session(
                "找一下关于'区块链质押收益'的邮件",
                {"mailbox": "a@b.com", "conversation_id": "chat-j10"},
                {"mailbox": "a@b.com"},
                sampling_create_message=object(),
                invoke_id="test-force-evidence-j10",
            )

    outcome = asyncio.run(run())
    assert tools == ["query_mail_evidence"]
    assert "区块链质押收益" in str((tool_args[0].get("query_plan") or {}).get("query") or "")
    assert "未找到" in outcome["assistant_text"]
    assert "要按什么条件" not in outcome["assistant_text"]


def test_run_local_agent_session_keeps_authoritative_user_text_for_evidence() -> None:
    """模型改写 query_mail_evidence.user_text 时，必须以外层原话为准，避免误澄清。"""
    seen: list[str] = []

    async def fake_plan(*_args, **_kwargs):
        return {
            "payload": {
                "action": "tool_call",
                "tool": "query_mail_evidence",
                "arguments": {
                    "user_text": '找一下关于"区块链质押收益"的邮件',
                    "query_plan": {"intent": "find", "query": "body:区块链质押收益"},
                },
            }
        }

    async def fake_tool(tool, arguments, _invoke_id):
        seen.append(str(arguments.get("user_text") or ""))
        return {
            "success": True,
            "tool": tool,
            "data": {
                "kind": "evidence_template",
                "assistant_text": "已在当前全部本地缓存中检索，未找到与您条件匹配的相关邮件。",
                "match_status": "no_confirmed_match",
                "results": [],
            },
        }

    async def run():
        with patch("anna_inbox_executa.local_agent_session.call_llm_json_safe", new=AsyncMock(side_effect=fake_plan)), patch(
            "anna_inbox_executa.local_agent_session.handle_ai_agent_tool", new=AsyncMock(side_effect=fake_tool),
        ):
            return await run_local_agent_session(
                "找一下关于'区块链质押收益'的邮件",
                {"mailbox": "a@b.com", "conversation_id": "chat-j10b"},
                {"mailbox": "a@b.com"},
                sampling_create_message=object(),
                invoke_id="test-auth-user-text-j10",
            )

    outcome = asyncio.run(run())
    assert seen == ["找一下关于'区块链质押收益'的邮件"]
    assert "未找到" in outcome["assistant_text"]


def test_force_final_marks_primary_sampling_step() -> None:
    """最终回答代理不得改变 Sampling 调用，并为主调用写入安全阶段标记。"""
    calls: list[dict] = []

    async def fake_sampling(**kwargs):
        calls.append(kwargs)
        return {
            "content": {"type": "text", "text": '{"action":"final","text":"已完成。"}'},
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }

    result = asyncio.run(_force_final_text(
        sampling_create_message=fake_sampling,
        trail_parts=["[tool_result name=query_mail_evidence]\\n{}"],
        language="zh",
    ))

    assert result == "已完成。"
    assert len(calls) == 1
    metadata = calls[0].get("metadata") or {}
    assert metadata["tool"] == "local_agent_session.final.sample"
    assert metadata["sampling_stage"] == "primary"
    assert metadata["sampling_attempt"] == "1"


def test_force_final_missing_colon_is_repaired_locally() -> None:
    """{"action" "final"} 漏冒号由本地 salvage 修好，不再二次 sampling repair。"""
    calls: list[dict] = []

    async def fake_sampling(**kwargs):
        calls.append(kwargs)
        return {
            "content": {"type": "text", "text": '{"action" "final", "text": "已修复。"}'},
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }

    result = asyncio.run(_force_final_text(
        sampling_create_message=fake_sampling,
        trail_parts=["[tool_result name=query_mail_evidence]\\n{}"],
        language="zh",
    ))

    assert result == "已修复。"
    assert len(calls) == 1
    assert (calls[0].get("metadata") or {}).get("sampling_stage") == "primary"


def test_force_final_invalid_json_falls_back_without_repair() -> None:
    """最终回答 JSON 无法本地闭合时不再二次 sampling repair，走 fallback。"""
    calls: list[dict] = []

    async def fake_sampling(**kwargs):
        calls.append(kwargs)
        return {
            "content": {"type": "text", "text": '{"a": true true, "b": [1,2,}'},
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }

    result = asyncio.run(_force_final_text(
        sampling_create_message=fake_sampling,
        trail_parts=["[tool_result name=query_mail_evidence]\\n{}"],
        language="zh",
    ))

    # fallback 空文本；重要的是不出现 json_repair 二次调用。
    assert result == ""
    assert len(calls) == 2  # max_attempts=2 的主任务重试
    assert all((c.get("metadata") or {}).get("tool") == "local_agent_session.final.sample" for c in calls)
    assert all((c.get("metadata") or {}).get("sampling_stage") == "primary" for c in calls)


if __name__ == "__main__":
    test_normalize_plan_accepts_host_tool_names_only()
    test_planner_system_prompt_stays_within_route_budget()
    test_local_session_confirmation_generates_batch_compose_without_search()
    test_local_session_continue_generates_next_batch_from_same_request()
    test_selected_batch_draft_skips_router_and_mail_search()
    test_local_session_keeps_full_template_until_contact_details_arrive()
    test_assemble_prefers_structured_tool_outcome()
    test_confirmed_evidence_thread_ids_only_exposes_strict_matches()
    test_assemble_falls_back_to_search_results_when_final_empty()
    test_evidence_fallback_clarifies_empty_strict_result_with_nearby_thread()
    test_run_local_agent_session_force_final_when_empty_after_tools()
    test_run_local_agent_session_calls_handle_ai_agent_tool()
    test_run_local_agent_session_strips_model_context_before_tool_call()
    test_run_local_agent_session_previews_draft_before_card()
    test_run_local_agent_session_creates_card_after_user_confirms_draft()
    test_run_local_agent_session_retries_truncated_route_with_larger_budget()
    test_run_local_agent_session_executes_evidence_once_before_final()
    test_run_local_agent_session_stops_after_search_field_clarification()
    test_run_local_agent_session_forces_evidence_when_route_final_skips_search()
    test_run_local_agent_session_keeps_authoritative_user_text_for_evidence()
    test_force_final_marks_primary_sampling_step()
    test_force_final_missing_colon_is_repaired_locally()
    test_force_final_invalid_json_falls_back_without_repair()
    print("ok")
