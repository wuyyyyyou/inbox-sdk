"""Host Agent 细粒度工具的安全边界测试。"""

from __future__ import annotations

from unittest.mock import patch

from anna_inbox_executa.ai_agent_tools_flow import (
    AI_AGENT_TOOL_NAMES,
    _merge_ui_context,
    _reserve_search_limit,
    _search_email,
    _split_gmail_and_local_workflow_query,
)
from mail_agent.domain.types import MessageLite


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


def test_search_limit_applies_per_call_only() -> None:
    """每次搜索独立限制返回量，同一会话可以持续发起新搜索。"""
    assert [_reserve_search_limit(7) for _ in range(10)] == [7] * 10
    assert _reserve_search_limit(999) == 20
    assert _reserve_search_limit(None) == 12
    assert _reserve_search_limit(0) == 12
    print("[PASS] test_search_limit_applies_per_call_only")


def test_search_query_keeps_gmail_syntax_and_separates_workflow_filters() -> None:
    """Gmail 条件保持原样，本地工作流条件不发送给 Gmail。"""
    gmail_query, local_query = _split_gmail_and_local_workflow_query(
        "newer_than:7d from:alice AND is:todo",
    )
    assert gmail_query == "newer_than:7d from:alice"
    assert local_query == "is:todo"
    try:
        _split_gmail_and_local_workflow_query("from:alice OR is:todo")
    except ValueError as exc:
        assert "AND" in str(exc)
    else:
        raise AssertionError("mixed OR query should be rejected")
    print("[PASS] test_search_query_keeps_gmail_syntax_and_separates_workflow_filters")


def test_search_email_uses_live_gmail_and_workflow_ids() -> None:
    """search_email 必须实时调用 Gmail，并只在实时命中集上筛选前端 Todo 标记。"""
    messages = [
        MessageLite("todo-1", "thread-1", "Alice <alice@example.com>", "me@example.com", subject="Todo"),
        MessageLite("other-1", "thread-2", "Alice <alice@example.com>", "me@example.com", subject="Other"),
    ]
    with patch(
        "mail_agent.mail_providers.gmail.adapter.live_search_metadata_and_cache",
        return_value=["todo-1", "other-1"],
    ) as live_search, patch(
        "mail_agent.mail_providers.gmail.adapter.get_messages_lite",
        return_value=messages,
    ):
        result = _search_email(
            {"mailbox": "me@example.com", "about": "from:alice", "filter": "is:todo", "limit": 10},
            {"todo_message_ids": ["todo-1"]},
        )
    assert live_search.call_args.args[:3] == ("me@example.com", "from:alice", 50)
    assert live_search.call_args.kwargs == {"force_refresh": True, "strict": True}
    assert result["scan_source"] == "gmail"
    assert result["gmail_query"] == "from:alice"
    assert result["local_workflow_query"] == "is:todo"
    assert [row["message_id"] for row in result["results"]] == ["todo-1"]
    print("[PASS] test_search_email_uses_live_gmail_and_workflow_ids")


if __name__ == "__main__":
    test_host_agent_tool_whitelist_excludes_mutations()
    test_flat_thread_fields_merge_into_readonly_context()
    test_host_agent_tool_schemas_are_decision_compact()
    test_public_outcome_strips_internal_fields()
    test_search_limit_applies_per_call_only()
    test_search_query_keeps_gmail_syntax_and_separates_workflow_filters()
    test_search_email_uses_live_gmail_and_workflow_ids()
    print("[ALL TESTS PASSED]")
