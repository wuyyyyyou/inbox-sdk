"""Host Agent 细粒度工具的安全边界测试。"""

from __future__ import annotations

import logging
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
    from anna_inbox_executa.ai_agent_tools_flow import AI_AGENT_SESSION_TOOL_NAMES

    forbidden = {"apply_proposed_actions", "reply_now", "start_ai_turn", "send_mail", "trash"}
    assert not (AI_AGENT_TOOL_NAMES & forbidden)
    assert "query_mail_evidence" in AI_AGENT_TOOL_NAMES
    assert "propose_inbox_actions" in AI_AGENT_TOOL_NAMES
    assert "query_mail_evidence" in AI_AGENT_SESSION_TOOL_NAMES
    assert "search_email" not in AI_AGENT_SESSION_TOOL_NAMES
    assert "read_email" not in AI_AGENT_SESSION_TOOL_NAMES
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

    assert len(AI_AGENT_DEFAULT_TOOLS) == 10
    total = len(json.dumps(AI_AGENT_DEFAULT_TOOLS, ensure_ascii=False))
    # describe 仍保留前端显式调用的兼容工具；Host Session 实际白名单已收敛。
    assert total <= 7300, f"AI_AGENT_DEFAULT_TOOLS too large: {total}"
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


def test_public_evidence_omits_raw_coverage_note() -> None:
    """P3 不得把旧缓存边界文案交给 Host 直接复述。"""
    from anna_inbox_executa.ai_agent_tools_flow import _public_outcome

    public = _public_outcome({
        "kind": "evidence",
        "coverage_note": "Local index is fully backfilled (~3033).",
        "sync_boundary": {"earliest_indexed_at": "2026-02-06T00:00:00Z"},
    })
    assert "coverage_note" not in public
    assert public["sync_boundary"]["earliest_indexed_at"] == "2026-02-06T00:00:00Z"
    print("[PASS] test_public_evidence_omits_raw_coverage_note")


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


def test_search_email_uses_full_cache_and_workflow_ids() -> None:
    """AI 搜索只读全量缓存，并按前端 Todo 快照筛选，不实时访问 Gmail。"""
    messages = [
        {
            "id": "todo-1", "thread_id": "thread-1", "from": "Alice <alice@example.com>",
            "to": "me@example.com", "subject": "Todo", "internal_date": "1783814400000", "label_ids": ["INBOX"],
        },
        {
            "id": "other-1", "thread_id": "thread-2", "from": "Alice <alice@example.com>",
            "to": "me@example.com", "subject": "Other", "internal_date": "1783814300000", "label_ids": ["INBOX"],
        },
    ]
    boundary = {
        "earliest_indexed_at": "2026-01-01T00:00:00Z", "latest_indexed_at": "2026-07-22T00:00:00Z",
        "initial_sync_complete": True, "backfill_complete": False, "cache_total": 2, "priority_days": 180,
    }
    with patch(
        "mail_agent.mail_providers.gmail.adapter.list_messages",
        return_value=messages,
    ), patch(
        "mail_agent.mail_providers.gmail.mailbox_sync.get_mailbox_sync_boundary",
        return_value=boundary,
    ), patch(
        "mail_agent.mail_providers.gmail.mailbox_sync.boundary_honesty_note",
        return_value="缓存边界说明",
    ):
        result = _search_email(
            {"mailbox": "me@example.com", "about": "from:alice", "filter": "is:todo", "limit": 10},
            {"todo_message_ids": ["todo-1"]},
        )
    assert result["scan_source"] == "cache"
    assert result["gmail_query"] == "from:alice"
    assert result["local_workflow_query"] == "is:todo"
    assert result["cache_candidates_scanned"] == 2
    assert result["sync_boundary"] == boundary
    assert [row["message_id"] for row in result["results"]] == ["todo-1"]
    print("[PASS] test_search_email_uses_full_cache_and_workflow_ids")


def test_plain_search_only_fetches_requested_candidates() -> None:
    """AI 搜索不继承 display_range，空缓存返回同步边界而非 Gmail 查询。"""
    with patch(
        "mail_agent.mail_providers.gmail.adapter.list_messages",
        return_value=[],
    ), patch(
        "mail_agent.mail_providers.gmail.mailbox_sync.get_mailbox_sync_boundary",
        return_value={"cache_total": 0, "initial_sync_complete": False, "priority_days": 180},
    ), patch(
        "mail_agent.mail_providers.gmail.mailbox_sync.boundary_honesty_note",
        return_value="180 天优先同步尚未完成",
    ):
        result = _search_email(
            {"mailbox": "me@example.com", "about": "from:alice", "limit": 7},
            {"display_range_days": 30},
        )
    assert result["scan_source"] == "cache"
    assert result["gmail_query"] == "from:alice"
    assert result["result_limit"] == 7
    assert result["truncated"] is False
    assert result["coverage_note"] == "180 天优先同步尚未完成"
    print("[PASS] test_plain_search_only_fetches_requested_candidates")


def test_search_email_includes_attachment_filenames() -> None:
    """search_email 应暴露附件文件名，供发票/附件类问答。"""
    messages = [{
        "id": "m1", "thread_id": "t1", "from": "billing@fal.ai", "to": "me@example.com",
        "subject": "New invoice", "internal_date": "1783814400000", "label_ids": ["INBOX"],
        "attachments": [{"filename": "invoice-LWTZJX-00001.pdf", "attachmentId": "att1"}],
    }]
    with patch(
        "mail_agent.mail_providers.gmail.adapter.list_messages",
        return_value=messages,
    ), patch(
        "mail_agent.mail_providers.gmail.mailbox_sync.get_mailbox_sync_boundary",
        return_value={"cache_total": 1, "initial_sync_complete": True, "backfill_complete": False, "priority_days": 180},
    ), patch(
        "mail_agent.mail_providers.gmail.mailbox_sync.boundary_honesty_note",
        return_value="缓存边界说明",
    ):
        result = _search_email(
            {"mailbox": "me@example.com", "about": "fal invoice", "limit": 5},
            {"display_range_days": 60},
        )
    assert result["results"][0]["attachmentFilenames"] == ["invoice-LWTZJX-00001.pdf"]
    assert result["results"][0]["hasAttachment"] is True
    print("[PASS] test_search_email_includes_attachment_filenames")


def test_search_email_order_oldest_uses_full_cached_range() -> None:
    """最早邮件必须在完整缓存排序后取值，不能从 newest 前 20 条猜测。"""
    messages = [
        {
            "id": "new", "thread_id": "t-new", "from": "sender@example.com", "to": "me@example.com",
            "subject": "Yesterday", "internal_date": "1783814400000", "label_ids": ["INBOX"],
        },
        {
            "id": "old", "thread_id": "t-old", "from": "sender@example.com", "to": "me@example.com",
            "subject": "Earliest indexed", "internal_date": "1770000000000", "label_ids": ["INBOX"],
        },
    ]
    with patch(
        "mail_agent.mail_providers.gmail.adapter.list_messages",
        return_value=messages,
    ), patch(
        "mail_agent.mail_providers.gmail.mailbox_sync.get_mailbox_sync_boundary",
        return_value={"cache_total": 2, "initial_sync_complete": True, "backfill_complete": False, "priority_days": 180},
    ), patch(
        "mail_agent.mail_providers.gmail.mailbox_sync.boundary_honesty_note",
        return_value="缓存边界说明",
    ):
        oldest = _search_email(
            {"mailbox": "me@example.com", "about": "in:anywhere", "order": "oldest", "limit": 1},
            {"display_range_days": 7},
        )
        newest = _search_email(
            {"mailbox": "me@example.com", "about": "in:anywhere", "order": "newest", "limit": 1},
            {"display_range_days": 60},
        )
    assert oldest["order"] == "oldest"
    assert oldest["cache_candidates_scanned"] == 2
    assert oldest["results"][0]["message_id"] == "old"
    assert newest["results"][0]["message_id"] == "new"
    print("[PASS] test_search_email_order_oldest_uses_full_cached_range")


def test_merge_ui_context_accepts_thread_ref_for_draft() -> None:
    """关闭详情抽屉时仍可用 search 命中的 THREAD_REF 写回复。"""
    from anna_inbox_executa.ai_agent_tools_flow import _merge_ui_context, _remember_thread_ref

    _remember_thread_ref("me@example.com", "thread-9", "message-9")
    context = _merge_ui_context({
        "mailbox": "me@example.com",
        "thread_ref": "THREAD_REF_thread-9",
        "user_text": "帮我回复",
    })
    assert context["current_thread"]["thread_id"] == "thread-9"
    assert context["current_thread"]["message_id"] == "message-9"
    assert context["current_thread"]["kind"] == "thread"
    print("[PASS] test_merge_ui_context_accepts_thread_ref_for_draft")


def test_gmail_search_failure_logs_safe_diagnostics() -> None:
    """Gmail 搜索失败日志应保留传输诊断字段，且不泄漏邮箱或查询条件。"""
    from mail_agent.mail_providers.gmail import adapter

    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("mail_agent.gmail")
    handler = CaptureHandler()
    logger.addHandler(handler)
    try:
        with patch.object(adapter, "gmail_request", side_effect=adapter.GmailApiError(503, "upstream unavailable")):
            assert adapter.search_gmail(
                "private@example.com",
                "from:private@example.com invoice",
                7,
                request_timeout_seconds=12,
            ) == []
    finally:
        logger.removeHandler(handler)
    message = "\n".join(record.getMessage() for record in records)
    assert "gmail_search_failed" in message
    assert "http_status=503" in message
    assert "private@example.com" not in message
    assert "invoice" not in message
    print("[PASS] test_gmail_search_failure_logs_safe_diagnostics")


if __name__ == "__main__":
    test_host_agent_tool_whitelist_excludes_mutations()
    test_flat_thread_fields_merge_into_readonly_context()
    test_host_agent_tool_schemas_are_decision_compact()
    test_public_outcome_strips_internal_fields()
    test_public_evidence_omits_raw_coverage_note()
    test_search_limit_applies_per_call_only()
    test_search_query_keeps_gmail_syntax_and_separates_workflow_filters()
    test_search_email_uses_full_cache_and_workflow_ids()
    test_plain_search_only_fetches_requested_candidates()
    test_search_email_includes_attachment_filenames()
    test_search_email_order_oldest_uses_full_cached_range()
    test_merge_ui_context_accepts_thread_ref_for_draft()
    test_gmail_search_failure_logs_safe_diagnostics()
    print("[ALL TESTS PASSED]")
