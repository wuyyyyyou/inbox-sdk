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


if __name__ == "__main__":
    test_ask_uses_only_current_mailbox()
    test_ask_falls_back_to_tool_mailbox()
    print("AI turn current mailbox: OK")
