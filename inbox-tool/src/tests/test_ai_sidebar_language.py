"""Regression tests for request-language AI sidebar copy."""

from __future__ import annotations

import sys
from pathlib import Path


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> None:
    from anna_inbox_executa.v2_tools import (
        COMPOSE_MAIL_PROMPT_SYSTEM,
        MAIL_PROMPT_SYSTEM,
        MAIL_SUMMARY_SYSTEM,
        _sidebar_fallback_text,
        _sidebar_language_instruction,
    )
    from mail_agent.ask.planner import _PLANNER_SYSTEM_PROMPT

    chinese_instruction = _sidebar_language_instruction("请总结这封邮件，并告诉我下一步怎么办")
    assert "Simplified Chinese" in chinese_instruction
    assert "reply_gaps/compose_gaps" in chinese_instruction
    assert "draft_reply and compose_draft" in chinese_instruction
    assert _sidebar_fallback_text("请总结这封邮件", "summary") == "我已查看该邮件线程，但暂时无法生成详细摘要。"
    assert _sidebar_fallback_text("请帮我写邮件", "compose_draft") == "在生成邮件草稿前，我还需要一些补充信息。"

    english_instruction = _sidebar_language_instruction("Summarize this thread")
    assert "same language as the visible prompt" in english_instruction
    assert _sidebar_fallback_text("Summarize this thread", "summary") == "I reviewed the thread, but could not generate a detailed summary."

    for prompt in (MAIL_PROMPT_SYSTEM, COMPOSE_MAIL_PROMPT_SYSTEM, MAIL_SUMMARY_SYSTEM):
        assert "Follow the Response language instruction" in prompt
    assert "use Simplified Chinese when the request contains Chinese" in _PLANNER_SYSTEM_PROMPT
    print("PASS AI sidebar language tests")


if __name__ == "__main__":
    main()
