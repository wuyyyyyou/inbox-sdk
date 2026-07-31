"""AI 侧栏 Ask 邮箱范围的回归测试。

运行：uv --directory inbox-tool/src run python tests/test_ai_turn_current_mailbox.py
"""

from __future__ import annotations

import sys
from pathlib import Path


_SRC = str(Path(__file__).resolve().parents[1])
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def test_ask_uses_only_current_mailbox() -> None:
    """多账户选择状态不得让侧栏 Ask 跨邮箱搜索。"""
    from mail_agent.ai_turn.runner import _mailboxes_from_context

    mailboxes = _mailboxes_from_context(
        {
            "mailbox": "active@example.com",
            "selected_mailboxes": ["active@example.com", "other@example.com"],
        },
        {"mailbox": "argument@example.com"},
    )

    assert mailboxes == ["active@example.com"]


def test_ask_falls_back_to_tool_mailbox() -> None:
    """旧调用未传 ui_context.mailbox 时仍可使用工具参数指定的当前邮箱。"""
    from mail_agent.ai_turn.runner import _mailboxes_from_context

    assert _mailboxes_from_context({}, {"mailbox": "argument@example.com"}) == ["argument@example.com"]


def test_local_sidebar_exposes_only_confirmed_evidence_threads() -> None:
    """Local 轮询结果仅让严格命中的 THREAD_REF 成为可打开详情的入口。"""
    from anna_inbox_executa.ai_turn_flow import (
        _confirmed_evidence_thread_ids,
        _confirmed_evidence_thread_labels,
    )

    outcome = {
        "match_status": "confirmed",
        "results": [
            {"thread_id": "thread-1", "subject": "Invoice due"},
            {"thread_ref": "THREAD_REF_thread-2", "subject": "  Demo   Day  "},
            {"thread_id": "thread-1"},
        ],
    }
    assert _confirmed_evidence_thread_ids(outcome) == ["thread-1", "thread-2"]
    assert _confirmed_evidence_thread_labels(outcome) == {
        "thread-1": "Invoice due",
        "thread-2": "Demo Day",
    }
    assert _confirmed_evidence_thread_ids({
        "match_status": "no_confirmed_match",
        "results": [{"thread_ref": "THREAD_REF_nearby"}],
    }) == []
    assert _confirmed_evidence_thread_labels({
        "match_status": "no_confirmed_match",
        "results": [{"thread_ref": "THREAD_REF_nearby", "subject": "Similar only"}],
    }) == {}


def test_local_empty_search_describes_cache_coverage_not_search_range() -> None:
    """未命中时必须说明全量当前缓存，日期边界不能伪装成检索范围。"""
    from anna_inbox_executa.local_agent_session import (
        _LOCAL_AGENT_SYSTEM_PROMPT,
        _force_final_text,
    )

    assert "earliest/latest indexed dates the search range" in _LOCAL_AGENT_SYSTEM_PROMPT
    assert "180-day priority metadata sync is running" in _LOCAL_AGENT_SYSTEM_PROMPT
    source = _force_final_text.__code__.co_consts
    assert any(
        isinstance(value, str) and "never as the search range" in value
        for value in source
    )


if __name__ == "__main__":
    test_ask_uses_only_current_mailbox()
    test_ask_falls_back_to_tool_mailbox()
    test_local_sidebar_exposes_only_confirmed_evidence_threads()
    test_local_empty_search_describes_cache_coverage_not_search_range()
    print("AI turn current mailbox: OK")
