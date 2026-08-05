"""Regression tests for thread summary sanitization."""

from __future__ import annotations

import sys
from pathlib import Path


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label} failed{': ' + detail if detail else ''}")


def test_thread_ref_token_variants_are_removed() -> None:
    """截图中常见的两种内部引用写法都不能进入用户可见回答。"""
    from mail_agent.ai_turn.runner import _sanitize_thread_answer_markdown

    cleaned = _sanitize_thread_answer_markdown(
        "Summary [THREAD_REF_abc-123_x]  continues\n\n[THREADREF_Z9]"
    )
    assert cleaned == "Summary continues"


def test_ordinary_bracket_text_is_preserved() -> None:
    """普通 Markdown 方括号文本不是内部 token，必须原样保留。"""
    from mail_agent.ai_turn.runner import _sanitize_thread_answer_markdown

    markdown = "See [the project plan](https://example.com/plan) and [TODO]."
    assert _sanitize_thread_answer_markdown(markdown) == markdown


def test_thread_answer_prompt_forbids_internal_markers() -> None:
    """线程回答提示词应禁止复述和生成内部引用标记。"""
    from mail_agent.ai_turn.prompts import thread_answer_system_prompt

    prompt = thread_answer_system_prompt("en")
    assert "THREAD_REF" in prompt
    assert "THREADREF" in prompt
    assert "never repeat, quote, or generate" in prompt


def main() -> None:
    from mail_agent.actions.service import _dedupe_lines, _looks_like_summary_payload

    payload = (
        '{"thread_kind":"long_thread","headline":"Judge fee","what_happened":[],'
        '"open_questions":[],"reply_focus":"Confirm budget","related_context":[],'
        '"should_show":true,"confidence":"low"}'
    )

    check("detects summary payload", _looks_like_summary_payload(payload), payload)
    check(
        "drops summary payload from visible lines",
        _dedupe_lines(["Natural language summary", payload]) == ["Natural language summary"],
        str(_dedupe_lines(["Natural language summary", payload])),
    )

    print("PASS thread summary sanitization tests")


if __name__ == "__main__":
    main()
