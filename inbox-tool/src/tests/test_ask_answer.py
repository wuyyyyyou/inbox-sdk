"""Test ask/answer.py: guard, rendering, filter prompt.

Run:
  cd inbox-tool/src && py -3 tests/test_ask_answer.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

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


# ── Filter prompt ──────────────────────────────────────────────────────

def test_filter_prompt_format():
    """Filter prompt template renders correctly."""
    from mail_agent.ask.answer import _FILTER_SYSTEM_PROMPT, _FILTER_USER_TEMPLATE

    user = _FILTER_USER_TEMPLATE.format(
        user_request="find candidates",
        relevance_hint="unknown senders about jobs",
        count=5,
        headers='[{"i":0,"f":"a@x.com","s":"test"}]',
    )
    assert "find candidates" in user
    assert "unknown senders about jobs" in user
    assert "5 total" in user
    assert _FILTER_SYSTEM_PROMPT.startswith("You are Anna's relevance filter")
    print("[PASS] test_filter_prompt_format")


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

    print("\n--- Rendering ---\n")
    test_render_candidates_basic()
    test_render_candidates_body_truncation()
    test_render_empty_candidates()

    print("\n--- Filter prompt ---\n")
    test_filter_prompt_format()

    print(f"\n[ALL TESTS PASSED]")


if __name__ == "__main__":
    main()
