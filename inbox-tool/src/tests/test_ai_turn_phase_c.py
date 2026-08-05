"""AI turn 阶段 C：batch_draft / batch_outreach、多选校验、白名单。"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

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


async def test_batch_draft_requires_confirmation_offline():
    """首轮仅返回逐封摘要，不得返回正文或 artifact。"""
    from mail_agent.ai_turn.tools import tool_batch_draft

    outcome = await tool_batch_draft(
        "Draft short replies for each",
        _multi_select_context(3),
        language="en",
        sampling_create_message=None,
    )
    assert outcome["kind"] == "batch_draft_preview"
    assert "artifact" not in outcome and "artifacts" not in outcome
    assert all(not str(item.get("body") or "").strip() for item in outcome["batch_preview"])
    print("[PASS] test_batch_draft_requires_confirmation_offline")


async def test_batch_confirmation_generates_only_after_explicit_confirmation():
    """同一会话明确确认后才允许 artifact；原始请求不能绕过预览。"""
    from mail_agent.ai_turn.runner import run_ai_turn

    context = _multi_select_context(2)
    context["conversation_id"] = "phase-c-batch-confirm"
    first = await run_ai_turn(
        "Draft short replies for each",
        context,
        {},
        sampling_create_message=None,
    )
    assert first["kind"] == "batch_draft_preview"
    assert "artifact" not in first and "artifacts" not in first

    second = await run_ai_turn("Draft short replies for each", context, {}, sampling_create_message=None)
    assert second["kind"] == "batch_draft_preview"
    assert "artifact" not in second and "artifacts" not in second

    confirmed = await run_ai_turn("确认生成草稿", context, {}, sampling_create_message=None)
    assert confirmed["kind"] == "draft"
    assert len(confirmed.get("artifacts") or []) == 2
    print("[PASS] test_batch_confirmation_generates_only_after_explicit_confirmation")


async def test_batch_preview_structured_no_action_is_skipped():
    """逐封分类返回 skipped_no_action 时不生成任何正文或 artifact。"""
    from unittest.mock import AsyncMock, patch
    from mail_agent.ai_turn.tools import tool_batch_draft

    classify = AsyncMock(return_value={
        "payload": {"skipped_no_action": True, "action_summary": "FYI only", "draft_body": "must drop"},
    })

    with patch("mail_agent.ai_turn.tools._load_thread_excerpt", new=AsyncMock(return_value={"subject": "FYI", "body": "notice"})), patch(
        "mail_agent.ai_turn.tools.call_llm_json_safe", new=classify,
    ):
        outcome = await tool_batch_draft(
            "Draft short replies for each",
            _multi_select_context(2),
            language="en",
            sampling_create_message=object(),
        )
    assert outcome["kind"] == "batch_draft_preview"
    assert len(outcome["skipped_no_action"]) == 2
    assert "artifact" not in outcome and "artifacts" not in outcome
    print("[PASS] test_batch_preview_structured_no_action_is_skipped")


async def test_batch_outreach_confirmation_reports_skipped_subjects():
    """确认外联时透传跳过主题，并在无可操作邮件时不生成 artifact。"""
    from mail_agent.ai_turn.tools import tool_batch_outreach

    outcome = await tool_batch_outreach(
        "Confirm outreach drafts",
        _multi_select_context(2),
        language="en",
        sampling_create_message=None,
        confirmed=True,
        skip_thread_keys={"a@b.com|t1", "a@b.com|t2"},
        skipped_subjects=["FYI update", "Newsletter"],
    )
    assert outcome["kind"] == "batch_draft"
    assert outcome.get("artifacts") == []
    assert "FYI update" in outcome["assistant_text"]
    assert "Newsletter" in outcome["assistant_text"]
    print("[PASS] test_batch_outreach_confirmation_reports_skipped_subjects")


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
    assert outcome["kind"] == "batch_draft_preview"
    assert "artifact" not in outcome and "artifacts" not in outcome
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
    assert outcome["kind"] == "batch_draft_preview"
    print("[PASS] test_batch_caps_at_five")


async def test_batch_draft_reports_each_completed_artifact():
    from mail_agent.ai_turn.tools import tool_batch_draft

    events: list[tuple[str, dict[str, Any]]] = []
    outcome = await tool_batch_draft(
        "batch draft",
        _multi_select_context(3),
        language="en",
        sampling_create_message=None,
        confirmed=True,
        progress_callback=lambda stage, progress: events.append((stage, progress)),
    )

    assert len(outcome["artifacts"]) == 3
    assert len(events) == 3
    assert [event[1]["batch_completed"] for event in events] == [1, 2, 3]
    assert all(event[1]["batch_total"] == 3 for event in events)
    assert len(events[-1][1]["partial"]["batch_drafts"]) == 3
    print("[PASS] test_batch_draft_reports_each_completed_artifact")


async def test_batch_draft_generates_in_parallel_and_reports_each_completion():
    """确认轮应在每封完成时立即上报，不能等待前一封草稿完成。"""
    from mail_agent.ai_turn import tools

    original_draft_one_thread = tools._draft_one_thread
    events: list[tuple[str, dict]] = []

    async def delayed_draft_one_thread(*args, **kwargs):
        thread_id = str(kwargs["thread_id"])
        await asyncio.sleep({"t1": 0.06, "t2": 0.04, "t3": 0.02}[thread_id])
        return {
            "ok": True,
            "artifact": {
                "type": "draft_reply",
                "mailbox": str(kwargs["mailbox"]),
                "thread_id": thread_id,
                "message_id": str(kwargs["message_id"]),
                "subject": str(kwargs["subject_hint"]),
                "body": f"Draft for {thread_id}",
            },
        }

    tools._draft_one_thread = delayed_draft_one_thread
    started_at = time.monotonic()
    try:
        outcome = await tools.tool_batch_draft(
            "batch draft",
            _multi_select_context(3),
            language="en",
            sampling_create_message=object(),
            confirmed=True,
            progress_callback=lambda stage, progress: events.append((stage, progress)),
        )
    finally:
        tools._draft_one_thread = original_draft_one_thread

    assert time.monotonic() - started_at < 0.1
    assert len(outcome["artifacts"]) == 3
    assert [event[1]["batch_completed"] for event in events] == [1, 2, 3]
    assert events[0][1]["partial"]["batch_drafts"][0]["thread_id"] == "t3"
    print("[PASS] test_batch_draft_generates_in_parallel_and_reports_each_completion")


async def test_batch_compose_reports_each_completed_artifact():
    from mail_agent.ai_turn.tools import tool_batch_compose_new

    events: list[tuple[str, dict[str, Any]]] = []
    outcome = await tool_batch_compose_new(
        "Write to one@example.com, two@example.com, and three@example.com about our partnership.",
        {"mailbox": "owner@example.com"},
        {},
        language="en",
        sampling_create_message=None,
        confirmed=True,
        progress_callback=lambda stage, progress: events.append((stage, progress)),
    )

    assert len(outcome["compose_artifacts"]) == 3
    assert [event[1]["batch_completed"] for event in events] == [1, 2, 3]
    # 每个 partial 事件只携带当前一封，避免累计正文导致载荷随批次增长。
    for _stage, progress in events:
        partial = progress["partial"]
        assert "batch_compose_drafts" not in partial
        assert isinstance(partial.get("batch_compose_draft"), dict)
        assert isinstance(partial["batch_compose_draft"].get("artifact"), dict)
    print("[PASS] test_batch_compose_reports_each_completed_artifact")


async def test_batch_compose_limits_each_batch_to_five_and_continues_the_last_recipient():
    from unittest.mock import AsyncMock, patch
    from mail_agent.ai_turn.tools import tool_batch_compose_new

    addresses = [f"user{index}@example.com" for index in range(6)]
    request = "Write to " + ", ".join(addresses)

    async def compose_one(user_text, *_args, **_kwargs):
        recipient = next(item for item in user_text.split() if "@" in item).rstrip(",.")
        return {"artifact": {"type": "compose_draft", "body": recipient, "recipients": [recipient]}}

    with patch("mail_agent.ai_turn.tools.tool_compose_new", new=AsyncMock(side_effect=compose_one)):
        outcome = await tool_batch_compose_new(
            request,
            {"mailbox": "owner@example.com"},
            {},
            language="zh",
            sampling_create_message=object(),
            confirmed=True,
        )

    assert len(outcome["compose_artifacts"]) == 5
    assert outcome["batch_compose_continuation"] == {"source_prompt": request, "offset": 5, "remaining": 1}
    assert "还有 1 位收件人未生成" in outcome["assistant_text"]

    with patch("mail_agent.ai_turn.tools.tool_compose_new", new=AsyncMock(side_effect=compose_one)):
        continued = await tool_batch_compose_new(
            request,
            {"mailbox": "owner@example.com"},
            {},
            language="zh",
            sampling_create_message=object(),
            confirmed=True,
            batch_offset=5,
        )

    assert len(continued["compose_artifacts"]) == 1
    assert continued["compose_artifacts"][0]["recipients"] == ["user5@example.com"]
    assert continued["batch_compose_continuation"] is None
    print("[PASS] test_batch_compose_limits_each_batch_to_five_and_continues_the_last_recipient")


async def test_batch_compose_continuation_uses_remaining_offset_and_original_language():
    """续批须识别英文提示的常见词序，避免从第一个收件人重复开始。"""
    from anna_inbox_executa.local_agent_session import _continuation_batch_request

    request = "Write to " + ", ".join(f"user{index}@example.com" for index in range(12))
    continuation = _continuation_batch_request("继续", {
        "recent_conversation": [
            {"role": "user", "content": request},
            {"role": "assistant", "content": "Prepared 5 new email drafts. 7 recipients remain."},
        ],
    })

    assert continuation == (request, 5)
    print("[PASS] test_batch_compose_continuation_uses_remaining_offset_and_original_language")


async def test_batch_compose_continuation_prefers_structured_state_over_chat_text():
    """结构化续批状态必须优先，避免最近消息含搜索结果时被错误重路由。"""
    from anna_inbox_executa.local_agent_session import _continuation_batch_request

    request = "Write to " + ", ".join(f"user{index}@example.com" for index in range(14))
    continuation = _continuation_batch_request("继续", {
        "batch_compose_continuation": {"source_prompt": request, "offset": 5, "remaining": 9},
        "recent_conversation": [{"role": "assistant", "content": "邮件搜索结果"}],
    })

    assert continuation == (request, 5)
    print("[PASS] test_batch_compose_continuation_prefers_structured_state_over_chat_text")


async def test_local_session_preserves_batch_compose_continuation_in_final_outcome():
    """本地 Planner 汇总工具结果时，续批游标必须到达 start_ai_turn 最终结果。"""
    from anna_inbox_executa.local_agent_session import _assemble_outcome

    request = "Write to " + ", ".join(f"user{index}@example.com" for index in range(12))
    outcome = _assemble_outcome(
        final_text="Prepared 5 new email drafts. 7 recipients remain.",
        tool_records=[{
            "tool": "ai_compose_new",
            "data": {
                "kind": "draft",
                "compose_artifacts": [{
                    "type": "compose_draft",
                    "body": "Draft",
                    "recipients": ["user0@example.com"],
                }],
                "batch_compose_continuation": {
                    "source_prompt": request,
                    "offset": 5,
                    "remaining": 7,
                },
            },
        }],
        language="en",
    )

    assert outcome["batch_compose_continuation"] == {
        "source_prompt": request,
        "offset": 5,
        "remaining": 7,
    }
    print("[PASS] test_local_session_preserves_batch_compose_continuation_in_final_outcome")


async def test_public_tool_outcome_keeps_batch_compose_continuation():
    """本地会话使用的公开工具结果不得剥离续批游标。"""
    from anna_inbox_executa.ai_agent_tools_flow import _public_outcome

    continuation = {"source_prompt": "Write to a@example.com and b@example.com", "offset": 5, "remaining": 2}
    public = _public_outcome({"kind": "draft", "batch_compose_continuation": continuation})

    assert public["batch_compose_continuation"] == continuation
    print("[PASS] test_public_tool_outcome_keeps_batch_compose_continuation")


async def test_local_session_routes_multiple_recipients_without_planner_sampling():
    """多个外部收件人必须直达批量新邮件，不能进入 route.sample 再误选邮件批量回复。"""
    from unittest.mock import AsyncMock, patch
    from anna_inbox_executa.local_agent_session import run_local_agent_session

    request = "Write to a@example.com, b@example.com, and c@example.com."
    batch_result = {
        "kind": "draft",
        "assistant_text": "已生成 3 封新邮件草稿。",
        "compose_artifacts": [],
    }
    with patch(
        "mail_agent.ai_turn.tools.tool_batch_compose_new",
        new=AsyncMock(return_value=batch_result),
    ) as compose, patch(
        "anna_inbox_executa.local_agent_session.call_llm_json_safe",
        new=AsyncMock(side_effect=AssertionError("route.sample must not run")),
    ):
        outcome = await run_local_agent_session(
            request,
            {"mailbox": "owner@example.com", "language_hint": "en"},
            {},
            sampling_create_message=object(),
        )

    assert outcome == batch_result
    assert compose.await_count == 1
    assert compose.await_args.args[0] == request
    assert compose.await_args.kwargs["confirmed"] is True
    print("[PASS] test_local_session_routes_multiple_recipients_without_planner_sampling")


async def test_batch_compose_partial_accumulates_without_exceeding_five():
    """轮询间隔内完成多封草稿时，partial 仍必须保留整批已完成项。"""
    from anna_inbox_executa.common import MAIL_AGENT_RUNS, _merge_partial

    run_id = "phase-c-partial-accumulate"
    MAIL_AGENT_RUNS[run_id] = {"partial": {}}
    try:
        for index in range(12):
            _merge_partial(run_id, {"batch_compose_draft": {
                "recipient": f"user{index}@example.com",
                "artifact": {"body": f"Draft {index}"},
            }})
        partial = MAIL_AGENT_RUNS[run_id]["partial"]
        assert len(partial["batch_compose_drafts"]) == 5
    finally:
        MAIL_AGENT_RUNS.pop(run_id, None)
    print("[PASS] test_batch_compose_partial_accumulates_without_exceeding_five")


async def test_batch_compose_keeps_failed_recipients_out_of_next_batch_count():
    """本批失败不属于下一批未开始目标，续批数必须按已调度的 5 位计算。"""
    from unittest.mock import AsyncMock, patch
    from mail_agent.ai_turn.tools import tool_batch_compose_new

    addresses = [f"user{index}@example.com" for index in range(14)]

    async def compose_one(user_text, *_args, **_kwargs):
        recipient = next(item for item in user_text.split() if "@" in item).rstrip(",.")
        if recipient in {"user2@example.com", "user7@example.com"}:
            return {"kind": "error", "error": "compose_empty"}
        return {"artifact": {"type": "compose_draft", "body": recipient, "recipients": [recipient]}}

    with patch("mail_agent.ai_turn.tools.tool_compose_new", new=AsyncMock(side_effect=compose_one)):
        outcome = await tool_batch_compose_new(
            "Write to " + ", ".join(addresses),
            {"mailbox": "owner@example.com"}, {}, language="zh",
            sampling_create_message=object(), confirmed=True,
        )

    assert len(outcome["compose_artifacts"]) == 4
    assert outcome["batch_compose_continuation"] == {
        "source_prompt": "Write to " + ", ".join(addresses),
        "offset": 5,
        "remaining": 9,
    }
    # 首批只调度前 5 位收件人；第 7 位的失败属于后续批次，不应计入本批。
    assert len(outcome["batch_failures"]) == 1
    assert "还有 9 位收件人未生成" in outcome["assistant_text"]
    print("[PASS] test_batch_compose_keeps_failed_recipients_out_of_next_batch_count")


async def test_host_tool_uses_ui_context_language_hint():
    """Host 工具把语言提示嵌在 ui_context 时，续批短词也必须输出中文。"""
    from anna_inbox_executa.ai_agent_tools_flow import _language

    assert _language({"ui_context": {"language_hint": "zh"}}, "continue") == "zh"
    print("[PASS] test_host_tool_uses_ui_context_language_hint")


async def test_batch_compose_isolates_each_recipient_and_runs_in_parallel():
    """批量新邮件不得把其它地址透传给子任务，也不能逐封串行等待。"""
    from unittest.mock import AsyncMock, patch
    from mail_agent.ai_turn.tools import tool_batch_compose_new

    calls: list[str] = []

    async def compose_one(user_text, *_args, **_kwargs):
        calls.append(user_text)
        recipient = next(item for item in user_text.split() if "@" in item).rstrip(",.")
        await asyncio.sleep({"one@example.com": 0.06, "two@example.com": 0.04, "three@example.com": 0.02}[recipient])
        return {"artifact": {"type": "compose_draft", "body": recipient, "recipients": [recipient]}}

    started_at = time.monotonic()
    with patch("mail_agent.ai_turn.tools.tool_compose_new", new=AsyncMock(side_effect=compose_one)):
        outcome = await tool_batch_compose_new(
            "Write to one@example.com, two@example.com, and three@example.com.",
            {"mailbox": "owner@example.com"},
            {},
            language="en",
            sampling_create_message=object(),
            confirmed=True,
        )

    assert time.monotonic() - started_at < 0.1
    assert [item["recipients"] for item in outcome["compose_artifacts"]] == [
        ["one@example.com"], ["two@example.com"], ["three@example.com"],
    ]
    assert all(sum(email in call for email in ("one@example.com", "two@example.com", "three@example.com")) == 1 for call in calls)
    print("[PASS] test_batch_compose_isolates_each_recipient_and_runs_in_parallel")


async def test_batch_compose_progress_sends_one_artifact_at_a_time():
    from unittest.mock import AsyncMock, patch
    from mail_agent.ai_turn.tools import tool_batch_compose_new

    events: list[dict[str, Any]] = []

    async def compose_one(user_text, *_args, **_kwargs):
        recipient = next(item for item in user_text.split() if "@" in item).rstrip(",.")
        return {"artifact": {"type": "compose_draft", "body": recipient, "recipients": [recipient]}}

    with patch("mail_agent.ai_turn.tools.tool_compose_new", new=AsyncMock(side_effect=compose_one)):
        await tool_batch_compose_new(
            "Write to one@example.com, two@example.com, and three@example.com.",
            {"mailbox": "owner@example.com"},
            {},
            language="en",
            sampling_create_message=object(),
            confirmed=True,
            progress_callback=lambda _stage, progress: events.append(progress),
        )

    partials = [event["partial"] for event in events]
    assert all("batch_compose_draft" in partial and "batch_compose_drafts" not in partial for partial in partials)
    assert all("artifact" in partial["batch_compose_draft"] for partial in partials)
    print("[PASS] test_batch_compose_progress_sends_one_artifact_at_a_time")


async def test_compose_tool_routes_multiple_recipients_to_batch_compose():
    """模型误选 ai_compose_new 时，多收件人仍必须生成多份 compose artifact。"""
    from unittest.mock import AsyncMock, patch
    from anna_inbox_executa.ai_agent_tools_flow import handle_ai_agent_tool

    batch_compose = AsyncMock(return_value={
        "kind": "draft",
        "assistant_text": "Prepared 3 new email drafts.",
        "compose_artifacts": [
            {"type": "compose_draft", "body": "One", "recipients": ["one@example.com"]},
            {"type": "compose_draft", "body": "Two", "recipients": ["two@example.com"]},
            {"type": "compose_draft", "body": "Three", "recipients": ["three@example.com"]},
        ],
    })
    with patch("mail_agent.ai_turn.tools.tool_batch_compose_new", new=batch_compose):
        result = await handle_ai_agent_tool(
            "ai_compose_new",
            {
                "user_text": "Write to one@example.com, two@example.com, and three@example.com.",
                "mailbox": "owner@example.com",
            },
            "test-invoke",
        )

    assert result["success"] is True
    assert len(result["data"]["compose_artifacts"]) == 3
    assert batch_compose.await_count == 1
    print("[PASS] test_compose_tool_routes_multiple_recipients_to_batch_compose")


async def test_batch_draft_tool_routes_multiple_new_recipients_to_batch_compose():
    """模型误选 ai_batch_draft 时，多个显式收件人仍只能生成新邮件草稿。"""
    from unittest.mock import AsyncMock, patch
    from anna_inbox_executa.ai_agent_tools_flow import handle_ai_agent_tool

    batch_compose = AsyncMock(return_value={
        "kind": "draft",
        "assistant_text": "Prepared 2 new email drafts.",
        "compose_artifacts": [
            {"type": "compose_draft", "body": "One", "recipients": ["one@example.com"]},
            {"type": "compose_draft", "body": "Two", "recipients": ["two@example.com"]},
        ],
    })
    with patch("mail_agent.ai_turn.tools.tool_batch_compose_new", new=batch_compose):
        result = await handle_ai_agent_tool(
            "ai_batch_draft",
            {
                "user_text": "Write to one@example.com and two@example.com.",
                "mailbox": "owner@example.com",
            },
            "test-invoke",
        )

    assert result["success"] is True
    assert len(result["data"]["compose_artifacts"]) == 2
    assert batch_compose.await_count == 1
    print("[PASS] test_batch_draft_tool_routes_multiple_new_recipients_to_batch_compose")


async def test_batch_preview_embeds_thread_refs_as_confirmed_evidence():
    """预览摘要逐封携带 [THREAD_REF_xxx]，并暴露已确认证据供前端渲染跳转入口。"""
    from mail_agent.ai_turn.tools import tool_batch_draft

    outcome = await tool_batch_draft(
        "为每封选中的邮件起草回复",
        _multi_select_context(3),
        language="zh",
        sampling_create_message=None,
    )
    assert outcome["kind"] == "batch_draft_preview"
    assert outcome["match_status"] == "confirmed"
    assert "[THREAD_REF_t1]" in outcome["assistant_text"]
    assert "[THREAD_REF_t2]" in outcome["assistant_text"]
    results = outcome["results"]
    assert len(results) == 3
    assert all(row["thread_ref"] == f"THREAD_REF_{row['thread_id']}" for row in results)
    assert {row["thread_id"] for row in results} == {"t1", "t2", "t3"}
    assert all(row["subject"] for row in results)
    assert "artifact" not in outcome and "artifacts" not in outcome
    print("[PASS] test_batch_preview_embeds_thread_refs_as_confirmed_evidence")


async def test_batch_confirmed_reports_thread_refs_as_confirmed_evidence():
    """确认后的草稿同样携带已确认证据，前端据此保留 THREAD_REF 入口。"""
    from mail_agent.ai_turn.runner import run_ai_turn

    context = _multi_select_context(2)
    context["conversation_id"] = "phase-c-confirm-refs"
    first = await run_ai_turn("为每封选中的邮件起草回复", context, {}, sampling_create_message=None)
    assert first["kind"] == "batch_draft_preview"
    confirmed = await run_ai_turn("确认生成草稿", context, {}, sampling_create_message=None)
    assert confirmed["kind"] == "draft"
    assert confirmed["match_status"] == "confirmed"
    results = confirmed["results"]
    assert len(results) == len(confirmed.get("artifacts") or []) == 2
    assert {row["thread_id"] for row in results} == {"t1", "t2"}
    assert all("[THREAD_REF_" + row["thread_id"] + "]" in confirmed["assistant_text"] for row in results)
    print("[PASS] test_batch_confirmed_reports_thread_refs_as_confirmed_evidence")


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
    await test_batch_draft_requires_confirmation_offline()
    await test_batch_confirmation_generates_only_after_explicit_confirmation()
    await test_batch_preview_structured_no_action_is_skipped()
    await test_batch_outreach_confirmation_reports_skipped_subjects()
    await test_batch_draft_clarify_when_empty_selection()
    await test_batch_outreach_mode_offline()
    await test_batch_caps_at_five()
    await test_batch_draft_reports_each_completed_artifact()
    await test_batch_draft_generates_in_parallel_and_reports_each_completion()
    await test_batch_compose_reports_each_completed_artifact()
    await test_batch_compose_limits_each_batch_to_five_and_continues_the_last_recipient()
    await test_batch_compose_continuation_uses_remaining_offset_and_original_language()
    await test_batch_compose_continuation_prefers_structured_state_over_chat_text()
    await test_local_session_preserves_batch_compose_continuation_in_final_outcome()
    await test_public_tool_outcome_keeps_batch_compose_continuation()
    await test_local_session_routes_multiple_recipients_without_planner_sampling()
    await test_batch_compose_partial_accumulates_without_exceeding_five()
    await test_batch_compose_keeps_failed_recipients_out_of_next_batch_count()
    await test_host_tool_uses_ui_context_language_hint()
    await test_batch_compose_isolates_each_recipient_and_runs_in_parallel()
    await test_batch_compose_progress_sends_one_artifact_at_a_time()
    await test_compose_tool_routes_multiple_recipients_to_batch_compose()
    await test_batch_draft_tool_routes_multiple_new_recipients_to_batch_compose()
    await test_batch_preview_embeds_thread_refs_as_confirmed_evidence()
    await test_batch_confirmed_reports_thread_refs_as_confirmed_evidence()
    await test_runner_primary_batch()
    print("\nAll phase C tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
