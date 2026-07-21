"""Host Agent 细粒度工具的安全边界测试。"""

from __future__ import annotations

from anna_inbox_executa.ai_agent_tools_flow import (
    AI_AGENT_TOOL_NAMES,
    _merge_ui_context,
    _reserve_search_budget,
)


def test_host_agent_tool_whitelist_excludes_mutations() -> None:
    """Host 自动环不得获得确认执行或发送类工具。"""
    forbidden = {"apply_proposed_actions", "reply_now", "start_ai_turn", "send_mail", "trash"}
    assert not (AI_AGENT_TOOL_NAMES & forbidden)
    assert "search_email" in AI_AGENT_TOOL_NAMES
    assert "read_email" in AI_AGENT_TOOL_NAMES
    assert "propose_inbox_actions" in AI_AGENT_TOOL_NAMES
    print("[PASS] test_host_agent_tool_whitelist_excludes_mutations")


def test_flat_thread_fields_merge_into_readonly_context() -> None:
    """Host 可传扁平 thread 字段，但执行器统一只读取 ui_context。"""
    context = _merge_ui_context({
        "mailbox": "mail@example.com",
        "message_id": "message-1",
        "thread_id": "thread-1",
        "subject": "Quarterly update",
    })
    assert context["mailbox"] == "mail@example.com"
    assert context["current_thread"] == {
        "kind": "thread",
        "mailbox": "mail@example.com",
        "message_id": "message-1",
        "thread_id": "thread-1",
        "subject": "Quarterly update",
        "snippet": "",
    }
    print("[PASS] test_flat_thread_fields_merge_into_readonly_context")


def test_host_agent_tool_schemas_are_decision_compact() -> None:
    """Host 选型 schema 必须短且含 when/never 约束，减少 session 犹豫与 input tokens。"""
    from anna_inbox_executa.common import AI_AGENT_DEFAULT_TOOLS, load_manifest
    import json

    assert len(AI_AGENT_DEFAULT_TOOLS) == 9
    total = len(json.dumps(AI_AGENT_DEFAULT_TOOLS, ensure_ascii=False))
    assert total <= 6500, f"AI_AGENT_DEFAULT_TOOLS too large: {total}"
    for tool in AI_AGENT_DEFAULT_TOOLS:
        desc = str(tool.get("description") or "")
        assert 20 <= len(desc) <= 480, f"{tool['name']} description length={len(desc)}"
        # Host 侧不暴露内部 provider/storage 旋钮，避免选型干扰。
        param_names = {str(p.get("name")) for p in (tool.get("parameters") or [])}
        assert "ai_provider" not in param_names
        assert "storage_provider" not in param_names
    search = next(tool for tool in AI_AGENT_DEFAULT_TOOLS if tool["name"] == "search_email")
    assert "bodySnippet" in search["description"]
    assert "bodyFull" in search["description"]
    # describe 权威 manifest 必须与 DEFAULT 工具定义一致。
    manifest = load_manifest()
    by_name = {t["name"]: t for t in manifest.get("tools") or [] if isinstance(t, dict)}
    for tool in AI_AGENT_DEFAULT_TOOLS:
        assert by_name[tool["name"]]["description"] == tool["description"]
        assert by_name[tool["name"]]["parameters"] == tool["parameters"]
    print("[PASS] test_host_agent_tool_schemas_are_decision_compact")


def test_public_outcome_strips_internal_fields() -> None:
    """回传 Host 的 outcome 不得带 route/sampling/diagnostics 等内部字段。"""
    from anna_inbox_executa.ai_agent_tools_flow import _public_outcome

    public = _public_outcome({
        "kind": "draft",
        "assistant_text": "Draft ready",
        "artifact": {"type": "draft_reply", "body": "Hi"},
        "mail_context": {"kind": "thread", "thread_id": "t1"},
        "route": {"steps": [{"tool": "draft_reply"}]},
        "sampling": {"input_tokens": 9999},
        "diagnostics": {"trace_id": "rt_x"},
        "fallback_used": True,
    })
    assert public == {
        "kind": "draft",
        "assistant_text": "Draft ready",
        "artifact": {"type": "draft_reply", "body": "Hi"},
        "mail_context": {"kind": "thread", "thread_id": "t1"},
    }
    print("[PASS] test_public_outcome_strips_internal_fields")


def test_search_budget_caps_calls_and_candidates_per_conversation() -> None:
    """多轮 Host 调用也不能超过 6 次搜索和 45 个候选额度。"""
    context = {"mailbox": "budget@example.com", "conversation_id": "budget-test"}
    used = []
    for _ in range(6):
        limit, budget = _reserve_search_budget({}, context, 7)
        used.append(limit)
    assert used == [7, 7, 7, 7, 7, 7]
    assert budget["searches_used"] == 6
    assert budget["candidates_reserved"] == 42
    try:
        _reserve_search_budget({}, context, 1)
    except ValueError as exc:
        assert "budget exhausted" in str(exc)
    else:
        raise AssertionError("seventh search must be rejected")
    full_context = {"mailbox": "budget@example.com", "conversation_id": "candidate-cap-test"}
    assert [_reserve_search_budget({}, full_context, 12)[0] for _ in range(4)] == [12, 12, 12, 9]
    try:
        _reserve_search_budget({}, full_context, 1)
    except ValueError as exc:
        assert "budget exhausted" in str(exc)
    else:
        raise AssertionError("forty-sixth candidate must be rejected")
    print("[PASS] test_search_budget_caps_calls_and_candidates_per_conversation")


if __name__ == "__main__":
    test_host_agent_tool_whitelist_excludes_mutations()
    test_flat_thread_fields_merge_into_readonly_context()
    test_host_agent_tool_schemas_are_decision_compact()
    test_public_outcome_strips_internal_fields()
    test_search_budget_caps_calls_and_candidates_per_conversation()
    print("[ALL TESTS PASSED]")
