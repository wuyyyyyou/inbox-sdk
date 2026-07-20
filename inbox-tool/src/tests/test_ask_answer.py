"""Test ask/answer.py: guard, rendering, filter prompt.

Run:
  cd inbox-tool/src && py -3 tests/test_ask_answer.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ── Guard tests ────────────────────────────────────────────────────────

def test_guard_flags_forbidden_suggestion():
    """Guard flags (does not destroy) 'send' in suggestion."""
    from mail_agent.ask.answer import _apply_guard

    result = {
        "sections": [{
            "heading": "Test",
            "items": [{
                "subject": "Hi",
                "suggestion": "You should send a reply to Alice",
                "message_id": "123",
                "thread_id": "456",
            }],
        }],
    }
    valid_ids = {"123"}
    valid_thread_ids = {"456"}
    guarded = _apply_guard(result, valid_ids, valid_thread_ids)
    item = guarded["sections"][0]["items"][0]
    # Suggestion text is preserved
    assert "send" in item["suggestion"]
    # Warning flag is added
    assert "_guard_warning" in item
    assert "send" in item["_guard_warning"]
    print("[PASS] test_guard_flags_forbidden_suggestion")


def test_guard_flags_forbidden_draft():
    """Guard flags (does not destroy) 'delete' in draft."""
    from mail_agent.ask.answer import _apply_guard

    result = {
        "sections": [{
            "heading": "Test",
            "items": [{
                "subject": "Spam",
                "draft": "Please delete all these emails",
                "message_id": "abc",
                "thread_id": "def",
            }],
        }],
    }
    guarded = _apply_guard(result, {"abc"}, {"def"})
    item = guarded["sections"][0]["items"][0]
    assert "delete" in item["draft"]
    assert "_guard_warning" in item
    print("[PASS] test_guard_flags_forbidden_draft")


def test_guard_strips_invalid_message_id():
    """Guard clears message_id that doesn't exist in valid_ids."""
    from mail_agent.ask.answer import _apply_guard

    result = {
        "sections": [{
            "heading": "Test",
            "items": [{
                "subject": "Test",
                "message_id": "hallucinated_id",
                "thread_id": "real_tid",
            }],
        }],
    }
    guarded = _apply_guard(result, {"real_id"}, {"real_tid"})
    item = guarded["sections"][0]["items"][0]
    assert item["message_id"] == ""
    assert item["thread_id"] == "real_tid"  # valid, should survive
    print("[PASS] test_guard_strips_invalid_message_id")


def test_guard_strips_invalid_thread_id():
    """Guard clears thread_id that doesn't exist in valid_thread_ids."""
    from mail_agent.ask.answer import _apply_guard

    result = {
        "sections": [{
            "heading": "Test",
            "items": [{
                "subject": "Test",
                "message_id": "real_mid",
                "thread_id": "hallucinated_tid",
            }],
        }],
    }
    guarded = _apply_guard(result, {"real_mid"}, {"real_tid"})
    item = guarded["sections"][0]["items"][0]
    assert item["message_id"] == "real_mid"
    assert item["thread_id"] == ""
    print("[PASS] test_guard_strips_invalid_thread_id")


def test_guard_false_positive_sender_not_flagged():
    """'sender' contains 'send' substring but must NOT be flagged."""
    from mail_agent.ask.answer import _apply_guard

    result = {
        "sections": [{
            "heading": "Safe",
            "items": [{
                "subject": "Test",
                "suggestion": "The sender is Alice from marketing",
                "message_id": "ok", "thread_id": "ok",
            }],
        }],
    }
    guarded = _apply_guard(result, {"ok"}, {"ok"})
    item = guarded["sections"][0]["items"][0]
    assert "_guard_warning" not in item
    print("[PASS] test_guard_false_positive_sender_not_flagged")


def test_guard_safe_content_passes_through():
    """Guard does not modify safe suggestions."""
    from mail_agent.ask.answer import _apply_guard

    result = {
        "sections": [{
            "heading": "Safe",
            "items": [{
                "subject": "Meeting notes",
                "suggestion": "Review the agenda before Friday",
                "context": "Team sync about Q3 goals",
                "message_id": "ok_id",
                "thread_id": "ok_tid",
            }],
        }],
    }
    guarded = _apply_guard(result, {"ok_id"}, {"ok_tid"})
    item = guarded["sections"][0]["items"][0]
    assert item["suggestion"] == "Review the agenda before Friday"
    assert item["context"] == "Team sync about Q3 goals"
    assert item["message_id"] == "ok_id"
    print("[PASS] test_guard_safe_content_passes_through")


def test_guard_empty_sections():
    """Guard handles empty sections gracefully."""
    from mail_agent.ask.answer import _apply_guard

    result = {"sections": []}
    guarded = _apply_guard(result, set(), set())
    assert guarded["sections"] == []
    print("[PASS] test_guard_empty_sections")


def test_guard_validates_and_materializes_mail_links():
    """Only candidate-backed links survive and display metadata comes from Gmail data."""
    from mail_agent.ask.answer import _apply_guard

    result = {"sections": [{"items": [{
        "subject": "Group",
        "mail_links": [
            {"label": "Invented", "mailbox": "me@example.com", "thread_id": "t1", "message_id": "m1"},
            {"label": "Fake", "mailbox": "me@example.com", "thread_id": "fake", "message_id": "m1"},
            {"label": "Fake", "mailbox": "me@example.com", "thread_id": "t2", "message_id": "fake"},
        ],
    }]}]}
    sources = {"m1": {
        "mailbox": "me@example.com", "thread_id": "t1", "subject": "Real subject",
        "from": "Alice <alice@example.com>", "date": "Jul 07", "snippet": "Preview",
    }}
    guarded = _apply_guard(result, {"m1"}, {"t1"}, sources)
    links = guarded["sections"][0]["items"][0]["mail_links"]
    assert links == [{
        "label": "Real subject", "mailbox": "me@example.com", "thread_id": "t1", "message_id": "m1",
        "from": "Alice <alice@example.com>", "date": "Jul 07", "snippet": "Preview",
    }]
    print("[PASS] test_guard_validates_and_materializes_mail_links")


def test_guard_backfills_mail_link_from_item_id():
    """A cited result item remains navigable when the model omits mail_links."""
    from mail_agent.ask.answer import _apply_guard

    result = {"sections": [{"items": [{
        "subject": "Model-generated label",
        "context": "This thread needs your reply.",
        "message_id": "m1",
        "thread_id": "t1",
    }]}]}
    sources = {"m1": {
        "mailbox": "me@example.com", "thread_id": "t1", "subject": "Partnership Opportunities",
        "from": "Bri <bri@example.com>", "date": "Jul 07", "snippet": "Preview",
    }}
    guarded = _apply_guard(result, {"m1"}, {"t1"}, sources)
    item = guarded["sections"][0]["items"][0]
    assert item["subject"] == "Partnership Opportunities"
    assert item["mail_links"][0]["message_id"] == "m1"
    print("[PASS] test_guard_backfills_mail_link_from_item_id")


def test_guard_backfills_mail_link_from_exact_subject_mention():
    """An exact candidate subject in narrative text is linked without fuzzy guessing."""
    from mail_agent.ask.answer import _apply_guard

    result = {"sections": [{"items": [{
        "subject": "No Action Required",
        "context": "The thread regarding 'Exploring Browserless as a creative engine partner' is not awaiting your reply.",
    }]}]}
    sources = {
        "m1": {
            "mailbox": "me@example.com", "thread_id": "t1",
            "subject": "Exploring Browserless as a creative engine partner",
            "from": "Joel <joel@example.com>", "date": "Jul 06", "snippet": "Pilot recap",
        },
        "m2": {
            "mailbox": "me@example.com", "thread_id": "t2", "subject": "Unrelated subject",
            "from": "Other <other@example.com>", "date": "Jul 05", "snippet": "Other",
        },
    }
    guarded = _apply_guard(result, {"m1", "m2"}, {"t1", "t2"}, sources)
    links = guarded["sections"][0]["items"][0]["mail_links"]
    assert [link["message_id"] for link in links] == ["m1"]
    print("[PASS] test_guard_backfills_mail_link_from_exact_subject_mention")


def test_guard_backfills_mail_link_from_summary_subject():
    """模型只在摘要列出候选主题时，仍输出可供前端渲染的安全链接。"""
    from mail_agent.ask.answer import _apply_guard

    result = {
        "title": "未读邮件",
        "summary": "你需要查看 Project milestone confirmation 并决定下一步。",
        "sections": [],
    }
    sources = {
        "m1": {
            "mailbox": "me@example.com", "thread_id": "t1",
            "subject": "Project milestone confirmation",
            "from": "Alice <alice@example.com>", "date": "Jul 07", "snippet": "Please confirm",
        },
    }
    guarded = _apply_guard(result, {"m1"}, {"t1"}, sources)
    item = guarded["sections"][0]["items"][0]
    assert item["mail_links"][0]["message_id"] == "m1"
    assert item["mail_links"][0]["thread_id"] == "t1"
    print("[PASS] test_guard_backfills_mail_link_from_summary_subject")


# ── Rendering tests ────────────────────────────────────────────────────

def test_render_candidates_basic():
    """Rendering includes from, subject, date, snippet."""
    from mail_agent.ask.answer import _render_candidates_for_llm

    enriched = [{
        "message_id": "m1",
        "thread_id": "t1",
        "from": "alice@x.com",
        "to": "me@x.com",
        "subject": "Hello",
        "date": "Jun 09, 2026, 14:30",
        "snippet": "Hi there",
        "body": "Full body text here.",
        "label_ids": ["INBOX"],
        "unread": True,
        "thread": [],
        "contact_context": "",
    }]
    rendered = _render_candidates_for_llm(enriched, body_limit=4000)
    assert "alice@x.com" in rendered
    assert "Hello" in rendered
    assert "UNREAD" in rendered
    assert "Full body text here" in rendered
    assert "Message ID: m1" in rendered
    print("[PASS] test_render_candidates_basic")


def test_render_candidates_body_truncation():
    """Body is truncated to the specified limit."""
    from mail_agent.ask.answer import _render_candidates_for_llm

    long_body = "x" * 5000
    enriched = [{
        "message_id": "m1", "thread_id": "t1",
        "from": "a@x.com", "to": "b@x.com",
        "subject": "Long", "date": "", "snippet": "",
        "body": long_body, "label_ids": [], "unread": False,
        "thread": [], "contact_context": "",
    }]
    rendered = _render_candidates_for_llm(enriched, body_limit=500)
    # The body text should not exceed 500 chars + some marker overhead
    assert long_body[:500] in rendered
    assert long_body[:501] not in rendered  # truncated at limit
    print("[PASS] test_render_candidates_body_truncation")


def test_render_empty_candidates():
    """Empty candidate list → empty string."""
    from mail_agent.ask.answer import _render_candidates_for_llm

    rendered = _render_candidates_for_llm([])
    assert rendered == ""
    print("[PASS] test_render_empty_candidates")


def test_context_selection_limits_body_and_thread_reads():
    """筛选后的全部候选仍保留统计，但最多有限条进入正文与线程读取。"""
    from mail_agent.ask.answer import (
        _select_candidates_for_context,
        _DEFAULT_CONTEXT_CANDIDATES,
        resolve_context_candidate_limit,
    )
    from mail_agent.ask.planner import AskPlan
    from mail_agent.domain.types import MessageLite

    candidates = [
        MessageLite(
            message_id=f"m{index}", thread_id=f"t{index}", from_addr="sender@example.com", to_addr="owner@example.com",
            subject=f"Invoice {index}", snippet="billing update", unread=index == 99,
            internal_date=str(index),
        )
        for index in range(100)
    ]
    plan = AskPlan(user_request="Find invoice emails", topics=[{"search_terms": ["invoice"]}])
    selected = _select_candidates_for_context(candidates, plan)

    assert resolve_context_candidate_limit(plan.user_request) == _DEFAULT_CONTEXT_CANDIDATES
    assert len(selected) == _DEFAULT_CONTEXT_CANDIDATES
    assert selected[0].message_id == "m99"
    print("[PASS] test_context_selection_limits_body_and_thread_reads")


def test_resolve_answer_item_limit_parses_custom_count():
    """用户指定条数（默认 3，最高 8）应驱动 Answer items 与上下文上限。"""
    from mail_agent.ask.answer import (
        resolve_answer_item_limit,
        resolve_context_candidate_limit,
        _build_answer_system_prompt,
        _DEFAULT_ANSWER_ITEMS,
        _MAX_ANSWER_ITEMS,
        _MAX_CONTEXT_CANDIDATES,
    )

    assert resolve_answer_item_limit("整理收件箱") == _DEFAULT_ANSWER_ITEMS
    assert resolve_answer_item_limit("找 5 封邮件") == 5
    assert resolve_answer_item_limit("请从邮件中找出最需要优先处理的 5 封") == 5
    assert resolve_answer_item_limit("find top 5 emails that need reply") == 5
    assert resolve_answer_item_limit("list 12 messages") == _MAX_ANSWER_ITEMS
    assert resolve_answer_item_limit("0 emails") == 1
    assert resolve_context_candidate_limit("找 5 封邮件") == 5
    assert resolve_context_candidate_limit("找 8 封邮件") == _MAX_CONTEXT_CANDIDATES
    assert resolve_context_candidate_limit("Find invoice emails") == 5
    prompt = _build_answer_system_prompt(5)
    assert "at most 5 priority items" in prompt
    assert "at most 3 priority items" not in prompt
    print("[PASS] test_resolve_answer_item_limit_parses_custom_count")


async def test_filter_candidates_does_not_make_a_second_sampling_call():
    """Ask 已有本地排序时，筛选不能额外调用或重试 Sampling。"""
    from mail_agent.ask.answer import _filter_candidates
    from mail_agent.ask.planner import AskPlan
    from mail_agent.domain.types import MessageLite

    calls: list[dict[str, Any]] = []

    async def sampling_stub(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"content": {"type": "text", "text": "{}"}}

    messages = [
        MessageLite(
            message_id=f"m{index}", thread_id=f"t{index}", from_addr="sender@example.com", to_addr="owner@example.com",
            subject=f"Invoice {index}", snippet="billing update",
        )
        for index in range(11)
    ]
    filtered = await _filter_candidates(messages, AskPlan(user_request="Find invoices"), sampling_create_message=sampling_stub)

    assert filtered == messages
    assert calls == []
    print("[PASS] test_filter_candidates_does_not_make_a_second_sampling_call")


def test_answer_language_instruction():
    """Chinese requests require Chinese answer copy while preserving source text."""
    from mail_agent.ask.answer import _answer_language_instruction

    chinese = _answer_language_instruction("整理收件箱")
    english = _answer_language_instruction("Organize my inbox")
    assert "Simplified Chinese" in chinese
    assert "original language" in chinese
    assert "in English" in english
    assert "Do not output Chinese" in english
    print("[PASS] test_answer_language_instruction")


def test_english_generated_copy_rejects_chinese():
    """源邮件字段以外的英文回答文案不得混入中文；context 允许源语言摘录。"""
    from mail_agent.ask.answer import _generated_copy_contains_chinese

    assert _generated_copy_contains_chinese({"title": "查找紧急邮件", "summary": "Found one urgent email."})
    assert not _generated_copy_contains_chinese({
        "title": "Urgent emails",
        "summary": "Found one urgent email.",
        "sections": [{"heading": "Immediate action", "items": [{"subject": "紧急通知", "suggestion": "Reply today."}]}],
    })
    # 中文邮件的 context 摘录不得触发整份答案丢弃。
    assert not _generated_copy_contains_chinese({
        "title": "Emails that need your reply",
        "summary": "One human thread looks open.",
        "sections": [{
            "heading": "Needs reply",
            "items": [{
                "subject": "问候一下",
                "context": "你好，最近方便通话吗？",
                "suggestion": "Reply with your availability.",
            }],
        }],
    })
    print("[PASS] test_english_generated_copy_rejects_chinese")


def test_scrub_chinese_generated_copy_keeps_structure():
    """英文请求混入中文生成字段时，scrub 后应仍可用。"""
    from mail_agent.ask.answer import _generated_copy_contains_chinese, _scrub_chinese_generated_copy
    from mail_agent.ask.planner import AskPlan

    payload = {
        "title": "需要回复的邮件",
        "summary": "有一封需要你回复。",
        "sections": [{
            "heading": "待回复",
            "items": [{
                "subject": "hello",
                "context": "你好",
                "suggestion": "请尽快回复",
                "message_id": "m1",
            }],
        }],
    }
    cleaned = _scrub_chinese_generated_copy(
        payload,
        AskPlan(user_request="What needs my reply?", title="Emails that need your reply"),
    )
    assert not _generated_copy_contains_chinese(cleaned)
    assert cleaned["sections"][0]["items"][0]["message_id"] == "m1"
    assert cleaned["sections"][0]["items"][0]["context"] == "你好"
    print("[PASS] test_scrub_chinese_generated_copy_keeps_structure")


def test_answer_requires_synthesis_instead_of_copying_email_body():
    """总结提示词必须要求综合邮件证据，不能直接逐字复述正文。"""
    from mail_agent.ask.answer import _ASK_SYNTHESIS_INSTRUCTION

    assert "own words" in _ASK_SYNTHESIS_INSTRUCTION
    assert "Do not copy email body verbatim" in _ASK_SYNTHESIS_INSTRUCTION
    print("[PASS] test_answer_requires_synthesis_instead_of_copying_email_body")


def test_answer_fallback_uses_request_language():
    from mail_agent.ask.answer import _answer_fallback
    from mail_agent.ask.planner import AskPlan

    base = dict(plan_id="p1", title="", description="", people=[], topics=[], timeframe="7d",
                direction="inbox", goal="general_qa", task_prompt="", gmail_flags=[])
    chinese = _answer_fallback(AskPlan(user_request="整理收件箱", **base))
    english = _answer_fallback(AskPlan(user_request="Organize my inbox", **base))
    assert chinese["title"] == ""
    assert chinese["summary"] == "AI 分析暂时不可用，请稍后重试。"
    assert english["summary"] == "AI analysis is temporarily unavailable. Please try again."
    assert chinese["analysis_error"] is True
    assert chinese["fallback_used"] is False
    print("[PASS] test_answer_fallback_uses_request_language")


def test_empty_sampling_uses_error_fallback_not_local_mail_list():
    """无扫描证据时返回错误摘要，不编造邮件列表。"""
    from mail_agent.ask.answer import _answer_fallback
    from mail_agent.ask.planner import AskPlan

    result = _answer_fallback(
        AskPlan(user_request="Find urgent emails", title="Urgent emails"),
        "Anna sampling failed",
    )

    assert result["title"] == ""
    assert result["sections"] == []
    assert "matching emails" not in result["summary"].lower()
    assert "相关邮件" not in result["summary"]
    print("[PASS] test_empty_sampling_uses_error_fallback_not_local_mail_list")


def test_answer_fallback_never_exposes_enriched_candidates():
    """已有扫描证据时，Answer 失败也必须返回可重试错误而非候选邮件。"""
    from mail_agent.ask.answer import _answer_fallback
    from mail_agent.ask.planner import AskPlan

    result = _answer_fallback(
        AskPlan(user_request="What needs my reply?", title="Emails that need your reply", goal="draft_replies"),
        "Expecting value: schema echo",
    )

    assert result["analysis_error"] is True
    assert result["sections"] == []
    assert result["fallback_used"] is False
    assert "temporarily unavailable" in result["summary"].lower()
    print("[PASS] test_answer_fallback_never_exposes_enriched_candidates")


async def test_truncated_answer_uses_reserved_retry_then_returns_error():
    """Sampling 截断后使用预留重试额度，仍失败则返回可重试错误。"""
    from mail_agent.ask.answer import _generate_answer
    from mail_agent.ask.planner import AskPlan

    calls: list[dict[str, Any]] = []

    async def truncated_sampling(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"content": {"type": "text", "text": '{"summary":"partial'}}

    plan = AskPlan(user_request="What needs my reply?", title="Emails that need your reply", goal="draft_replies")
    result = await _generate_answer(
        plan,
        [{
            "subject": "Project update",
            "from": "Kate <kate@example.com>",
            "snippet": "Can you confirm the schedule?",
            "mailbox": "owner@example.com",
            "message_id": "m1",
            "thread_id": "t1",
        }],
        "owner@example.com",
        sampling_create_message=truncated_sampling,
    )

    assert result["analysis_error"] is True
    assert result["fallback_used"] is False
    assert result["sections"] == []
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == 4096
    assert calls[1]["max_tokens"] == 4096
    print("[PASS] test_truncated_answer_uses_reserved_retry_then_returns_error")


def test_answer_system_prompt_avoids_typescript_schema_tokens():
    """System prompt 不得用 string/string? 类型注解，否则模型会原样回显导致 JSON 失败。"""
    from mail_agent.ask.answer import _ASK_ANSWER_SYSTEM_PROMPT, _build_answer_system_prompt

    assert "string?" not in _ASK_ANSWER_SYSTEM_PROMPT
    assert '"title": string' not in _ASK_ANSWER_SYSTEM_PROMPT
    assert "valid JSON" in _ASK_ANSWER_SYSTEM_PROMPT
    assert "at most 3 priority items" in _ASK_ANSWER_SYSTEM_PROMPT
    assert "at most 8 priority items" in _build_answer_system_prompt(8)
    print("[PASS] test_answer_system_prompt_avoids_typescript_schema_tokens")


def test_answer_sampling_budget_uses_phase_weights():
    """Ask 无预算 sampler 也按 v1 总额度的阶段比例，而非旧固定值。"""
    from mail_agent.ask.sampling_budget import ASK_SAMPLING_PHASE_WEIGHTS, ask_sampling_tokens

    assert ASK_SAMPLING_PHASE_WEIGHTS == {
        "planner": 0.10,
        "answer": 0.60,
        "answer_retry": 0.25,
        "json_repair": 0.05,
    }
    assert ask_sampling_tokens(None, "planner") == 600
    assert ask_sampling_tokens(None, "answer") == 4096
    assert ask_sampling_tokens(None, "answer_retry") == 4096
    assert ask_sampling_tokens(None, "json_repair") == 800
    print("[PASS] test_answer_sampling_budget_uses_phase_weights")


# ── Main ────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Ask Answer Tests")
    print("=" * 60)

    print("\n--- Guard ---\n")
    test_guard_flags_forbidden_suggestion()
    test_guard_flags_forbidden_draft()
    test_guard_strips_invalid_message_id()
    test_guard_strips_invalid_thread_id()
    test_guard_false_positive_sender_not_flagged()
    test_guard_safe_content_passes_through()
    test_guard_empty_sections()
    test_guard_validates_and_materializes_mail_links()
    test_guard_backfills_mail_link_from_item_id()
    test_guard_backfills_mail_link_from_exact_subject_mention()
    test_guard_backfills_mail_link_from_summary_subject()

    print("\n--- Rendering ---\n")
    test_render_candidates_basic()
    test_render_candidates_body_truncation()
    test_render_empty_candidates()

    print("\n--- Candidate selection ---\n")
    test_context_selection_limits_body_and_thread_reads()
    test_resolve_answer_item_limit_parses_custom_count()
    asyncio.run(test_filter_candidates_does_not_make_a_second_sampling_call())
    test_answer_language_instruction()
    test_english_generated_copy_rejects_chinese()
    test_scrub_chinese_generated_copy_keeps_structure()
    test_answer_requires_synthesis_instead_of_copying_email_body()
    test_answer_fallback_uses_request_language()
    test_empty_sampling_uses_error_fallback_not_local_mail_list()
    test_answer_fallback_never_exposes_enriched_candidates()
    asyncio.run(test_truncated_answer_uses_reserved_retry_then_returns_error())
    test_answer_system_prompt_avoids_typescript_schema_tokens()
    test_answer_sampling_budget_uses_phase_weights()

    print(f"\n[ALL TESTS PASSED]")


if __name__ == "__main__":
    main()
