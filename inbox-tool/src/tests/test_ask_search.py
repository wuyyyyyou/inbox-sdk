"""Test ask/search.py: query building + broadening logic.

Run:
  cd inbox-tool/src && py -3 tests/test_ask_search.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _make_plan(**overrides):
    from mail_agent.ask.planner import AskPlan
    defaults = {
        "plan_id": "test",
        "user_request": "test",
        "direction": "inbox",
        "timeframe": "30d",
        "goal": "general_qa",
    }
    return AskPlan(**(defaults | overrides))


# ── build_queries tests ────────────────────────────────────────────────

async def test_build_queries_topic_only():
    """Topics with search_terms → OR group in query."""
    from mail_agent.ask.search import build_queries

    plan = _make_plan(
        topics=[{"concept": "test", "search_terms": ["resume", "CV", "求职"], "relevance_hint": "hint"}],
    )
    queries = await build_queries(plan, "test@gmail.com")
    assert len(queries) >= 1
    q = queries[0]["query"]
    assert "resume" in q and "CV" in q and "求职" in q
    assert "in:inbox" in q
    assert "newer_than:30d" in q
    print("[PASS] test_build_queries_topic_only")


async def test_build_queries_no_topics_no_people():
    """Empty topics and people → broad sweep query."""
    from mail_agent.ask.search import build_queries

    plan = _make_plan(topics=[], people=[])
    queries = await build_queries(plan, "test@gmail.com")
    assert len(queries) >= 1
    q = queries[0]["query"]
    assert "in:inbox" in q
    assert "newer_than:30d" in q
    print("[PASS] test_build_queries_no_topics_no_people")


async def test_build_queries_direction_sent():
    """Direction=sent → in:sent filter."""
    from mail_agent.ask.search import build_queries

    plan = _make_plan(direction="sent", topics=[])
    queries = await build_queries(plan, "test@gmail.com")
    assert "in:sent" in queries[0]["query"]
    print("[PASS] test_build_queries_direction_sent")


async def test_build_queries_direction_all():
    """Direction=all → no direction filter."""
    from mail_agent.ask.search import build_queries

    plan = _make_plan(direction="all", topics=[])
    queries = await build_queries(plan, "test@gmail.com")
    assert "in:inbox" not in queries[0]["query"]
    assert "in:sent" not in queries[0]["query"]
    print("[PASS] test_build_queries_direction_all")


async def test_build_queries_timeframe():
    """Custom timeframe → newer_than filter."""
    from mail_agent.ask.search import build_queries

    plan = _make_plan(timeframe="7d", topics=[])
    queries = await build_queries(plan, "test@gmail.com")
    assert "newer_than:7d" in queries[0]["query"]
    print("[PASS] test_build_queries_timeframe")


async def test_build_queries_people_without_contact_memory():
    """People with name_hints but no contact memory → partial name in query."""
    from mail_agent.ask.search import build_queries

    plan = _make_plan(
        people=[{"name_hint": "christopher", "role": "sender"}],
        topics=[],
    )
    queries = await build_queries(plan, "test@gmail.com")
    q = queries[0]["query"]
    assert "from:christopher" in q or "christopher" in q
    print("[PASS] test_build_queries_people_without_contact_memory")


async def test_build_queries_role_recipient():
    """role=recipient → uses to: (not from:)."""
    from mail_agent.ask.search import build_queries

    plan = _make_plan(
        people=[{"name_hint": "bob", "role": "recipient"}],
        topics=[],
    )
    queries = await build_queries(plan, "test@gmail.com")
    q = queries[0]["query"]
    assert "to:bob" in q
    print("[PASS] test_build_queries_role_recipient")


async def test_build_queries_role_either():
    """role=either → uses both from: and to:."""
    from mail_agent.ask.search import build_queries

    plan = _make_plan(
        people=[{"name_hint": "alice", "role": "either"}],
        topics=[],
    )
    queries = await build_queries(plan, "test@gmail.com")
    q = queries[0]["query"]
    assert "from:alice" in q
    assert "to:alice" in q
    print("[PASS] test_build_queries_role_either")


async def test_build_queries_combined():
    """Both people and topics → combined query."""
    from mail_agent.ask.search import build_queries

    plan = _make_plan(
        people=[{"name_hint": "alice", "role": "sender"}],
        topics=[{"concept": "c", "search_terms": ["invoice"], "relevance_hint": "h"}],
    )
    queries = await build_queries(plan, "test@gmail.com")
    q = queries[0]["query"]
    assert "alice" in q
    assert "invoice" in q
    print("[PASS] test_build_queries_combined")


async def test_build_queries_gmail_flags():
    """gmail_flags like is:unread → appended to query."""
    from mail_agent.ask.search import build_queries

    plan = _make_plan(topics=[], gmail_flags=["is:unread", "has:attachment"])
    queries = await build_queries(plan, "test@gmail.com")
    q = queries[0]["query"]
    assert "is:unread" in q
    assert "has:attachment" in q
    print("[PASS] test_build_queries_gmail_flags")


async def test_build_queries_fallback_empty():
    """If queries somehow empty → fallback broad query."""
    from mail_agent.ask.search import build_queries

    plan = _make_plan(topics=[], people=[], direction="all", timeframe="")
    queries = await build_queries(plan, "test@gmail.com")
    assert len(queries) >= 1
    assert queries[0]["query"]
    print("[PASS] test_build_queries_fallback_empty")


# ── _broaden_query tests ───────────────────────────────────────────────

def test_broaden_level_1_removes_person():
    """Level 1 broadening removes from:/to: patterns and OR groups."""
    from mail_agent.ask.search import _broaden_query

    q = {"query": "from:alice {invoice receipt} in:inbox newer_than:30d", "max_results": 100}
    broad = _broaden_query(q, 1)
    bq = broad["query"]
    assert "from:alice" not in bq
    assert "{" not in bq
    assert "in:inbox" in bq
    assert "newer_than:30d" in bq
    print("[PASS] test_broaden_level_1_removes_person")


def test_broaden_level_2_minimal():
    """Level 2 broadening keeps only direction + timeframe."""
    from mail_agent.ask.search import _broaden_query

    q = {"query": "from:alice invoice in:inbox newer_than:30d", "max_results": 100}
    broad = _broaden_query(q, 2)
    bq = broad["query"]
    assert "alice" not in bq
    assert "invoice" not in bq
    assert "in:inbox" in bq
    assert "newer_than:30d" in bq
    print("[PASS] test_broaden_level_2_minimal")


def test_broaden_level_1_removes_quoted_name():
    """Level 1 removes from:'Alice Smith' correctly."""
    from mail_agent.ask.search import _broaden_query

    q = {"query": 'from:"Alice Smith" invoice in:inbox newer_than:7d', "max_results": 50}
    broad = _broaden_query(q, 1)
    bq = broad["query"]
    assert "Alice" not in bq
    assert "Smith" not in bq
    assert "in:inbox" in bq
    assert "newer_than:7d" in bq
    print("[PASS] test_broaden_level_1_removes_quoted_name")


def test_broaden_level_2_preserves_exclusion():
    """Level 2 preserves '-' prefix on exclusion filters."""
    from mail_agent.ask.search import _broaden_query

    q = {"query": "alice newer_than:30d -in:sent -in:draft", "max_results": 50}
    broad = _broaden_query(q, 2)
    bq = broad["query"]
    assert "-in:sent" in bq
    assert "-in:draft" in bq
    print("[PASS] test_broaden_level_2_preserves_exclusion")


def test_broaden_level_2_no_filters():
    """Level 2 with nothing extractable → safe fallback."""
    from mail_agent.ask.search import _broaden_query

    q = {"query": "alice invoice", "max_results": 50}
    broad = _broaden_query(q, 2)
    bq = broad["query"]
    assert bq
    print("[PASS] test_broaden_level_2_no_filters")


# ── _resolve_people tests ──────────────────────────────────────────────

async def test_resolve_people_empty():
    """Empty name_hints → empty result."""
    from mail_agent.ask.search import _resolve_people

    result = await _resolve_people([], "test@gmail.com")
    assert result == {}
    print("[PASS] test_resolve_people_empty")


async def test_resolve_people_no_matches():
    """No contact memory → empty email lists."""
    from mail_agent.ask.search import _resolve_people

    result = await _resolve_people(
        [{"name_hint": "nobody_xyz_123", "role": "sender"}],
        "test@gmail.com",
    )
    assert result["nobody_xyz_123"] == []
    print("[PASS] test_resolve_people_no_matches")


# ── Main ────────────────────────────────────────────────────────────────

async def main_async():
    print("=" * 60)
    print("Ask Search Tests")
    print("=" * 60)

    print("\n--- build_queries ---\n")
    await test_build_queries_topic_only()
    await test_build_queries_no_topics_no_people()
    await test_build_queries_direction_sent()
    await test_build_queries_direction_all()
    await test_build_queries_timeframe()
    await test_build_queries_people_without_contact_memory()
    await test_build_queries_role_recipient()
    await test_build_queries_role_either()
    await test_build_queries_combined()
    await test_build_queries_gmail_flags()
    await test_build_queries_fallback_empty()

    print("\n--- _broaden_query ---\n")
    test_broaden_level_1_removes_person()
    test_broaden_level_1_removes_quoted_name()
    test_broaden_level_2_minimal()
    test_broaden_level_2_preserves_exclusion()
    test_broaden_level_2_no_filters()

    print("\n--- _resolve_people ---\n")
    await test_resolve_people_empty()
    await test_resolve_people_no_matches()

    print(f"\n[ALL TESTS PASSED]")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
