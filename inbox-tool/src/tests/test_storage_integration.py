"""Local integration test for storage + card system.

Uses a fake in-memory StorageClient that mimics the APS wire protocol
(get/set/list/delete), so tests run without the Anna platform.

Run:  cd src && py -3 tests/test_storage_integration.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

# Ensure src/ is on path
SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ── Fake StorageClient ──────────────────────────────────────────────

class FakeStorageClient:
    """In-memory APS-compatible storage for local testing."""

    def __init__(self):
        self._store: dict[str, dict[str, Any]] = {}
        self._generation: int = 1

    async def get(self, key: str, *, scope: str = "user", timeout: float = 30) -> dict:
        if key in self._store:
            return {"value": self._store[key]["value"], "etag": self._store[key]["etag"], "exists": True}
        return {"value": None, "etag": "", "exists": False}

    async def set(
        self, key: str, value: Any, *,
        scope: str = "user", if_match: str | None = None,
        ttl_seconds: int | None = None, timeout: float = 30,
    ) -> dict:
        if if_match and key in self._store and self._store[key]["etag"] != if_match:
            from executa_sdk.storage import StorageError, STORAGE_ERR_PRECONDITION_FAILED
            raise StorageError(STORAGE_ERR_PRECONDITION_FAILED, "etag mismatch")
        etag = f"etag_{self._generation}"
        self._generation += 1
        self._store[key] = {"value": value, "etag": etag}
        return {"etag": etag, "generation": self._generation, "size_bytes": len(str(value))}

    async def delete(self, key: str, *, scope: str = "user", if_match: str | None = None, timeout: float = 30) -> dict:
        self._store.pop(key, None)
        return {"deleted": True}

    async def list(
        self, *, prefix: str | None = None, cursor: str | None = None,
        limit: int | None = None, kind: str | None = None,
        scope: str = "user", timeout: float = 30,
    ) -> dict:
        items: list[dict] = []
        for key in self._store:
            if prefix and not key.startswith(prefix):
                continue
            items.append({"key": key})
        return {"items": items, "next_cursor": None}


# ── Test runner ─────────────────────────────────────────────────────

def _green(s: str) -> str:
    return f"\033[92m{s}\033[0m"


def _red(s: str) -> str:
    return f"\033[91m{s}\033[0m"


passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  {_green('PASS')} {label}")
    else:
        failed += 1
        print(f"  {_red('FAIL')} {label}  {detail}")


async def main():
    global passed, failed

    # 1. Wire fake storage into the singleton
    print("\n── Setup ──")
    fake = FakeStorageClient()
    fake_files = FakeStorageClient()  # type: ignore[assignment]

    from mail_agent.storage_client import init
    init(fake, fake_files, scope="user")  # type: ignore[arg-type]
    print("  Fake StorageClient wired into singleton")

    # 2. Test storage_types
    print("\n── storage_types ──")
    from mail_agent.storage_types import (
        ScanState, ProcessedMessage, PersistentCard, CardDetails,
        OriginalEmail, CardAction, ActiveCards, RunRecord,
        RunHistoryEntry, UserPreferences, SnoozePrefs, LearningRecord,
    )

    state = ScanState.empty("test@example.com")
    check("ScanState.empty()", state.mailbox == "test@example.com" and state.total_scans == 0)

    msg = ProcessedMessage(message_id="msg_001", thread_id="t1", from_addr="alice@x.com",
                           subject="Hello", snippet="Hi there", internal_date="2026-05-21")
    check("ProcessedMessage fields", msg.message_id == "msg_001" and msg.from_addr == "alice@x.com")

    card = PersistentCard(
        card_id="card_1", message_id="msg_001", thread_id="thread_alice",
        title="Alice needs a reply",
        summary="Alice asked about the project timeline.",
        recommendation="Suggested: Send a quick update on the timeline.",
        label="Needs reply",
        details=CardDetails(needs="Your reply", latest_activity="Alice · today", reviewed="Full thread", mailbox="test@example.com"),
        original=OriginalEmail(from_addr="alice@x.com", thread="Project timeline", time="today"),
        actions=[CardAction(id="draft", label="Prepare reply", primary=True)],
    )
    check("PersistentCard built", card.card_id == "card_1" and len(card.actions) == 1)

    # 3. Test storage_ops — scan state
    print("\n── storage_ops: scan state ──")
    from mail_agent.storage_ops import (
        get_scan_state, set_scan_state, mark_message_processed,
        mark_messages_processed_batch, get_processed_message_ids,
        filter_unprocessed, get_active_cards, set_active_cards,
        update_card_status, save_run_record, get_run_record,
        get_run_history, append_run_history,
        get_user_prefs, set_snooze_prefs, append_learning,
    )

    initial = await get_scan_state("test@example.com")
    check("get_scan_state (empty)", initial.total_scans == 0)

    initial.last_scan_ts = "2026-05-21T10:00:00"
    initial.total_scans = 1
    await set_scan_state("test@example.com", initial)
    loaded = await get_scan_state("test@example.com")
    check("set_scan_state + get roundtrip", loaded.total_scans == 1 and loaded.last_scan_ts == "2026-05-21T10:00:00")

    # 4. Test storage_ops — processed messages
    print("\n── storage_ops: processed messages ──")
    await mark_message_processed("test@example.com", msg)
    check("is processed (single)", await _is_processed("test@example.com", "msg_001"))

    ids = await get_processed_message_ids("test@example.com")
    check("get_processed_message_ids", "msg_001" in ids)

    new = await filter_unprocessed("test@example.com", ["msg_001", "msg_002", "msg_003"])
    check("filter_unprocessed", set(new) == {"msg_002", "msg_003"})

    msgs = [
        ProcessedMessage(message_id="msg_002", thread_id="t2", run_id="run_1"),
        ProcessedMessage(message_id="msg_003", thread_id="t3", run_id="run_1"),
    ]
    await mark_messages_processed_batch("test@example.com", msgs)
    ids2 = await get_processed_message_ids("test@example.com")
    check("batch mark processed", "msg_002" in ids2 and "msg_003" in ids2)

    # 5. Test card_service
    print("\n── card_service ──")
    from mail_agent.card_service import (
        build_card, merge_cards, cards_to_frontend, build_action_memo,
     )
    from mail_agent.types import CandidateItem, JudgmentResult, FinalDecision, BaseJudgment

    # Build a mock judgment for use with build_card
    # (we already tested PersistentCard construction; focus on merge/format)
    cards = ActiveCards(cards=[card])
    frontend = cards_to_frontend(cards)
    check("cards_to_frontend count", len(frontend) == 1)
    check("cards_to_frontend title", frontend[0]["title"] == "Alice needs a reply")
    check("cards_to_frontend details", frontend[0]["details"]["mailbox"] == "test@example.com")
    check("cards_to_frontend actions", frontend[0]["actions"][0]["label"] == "Prepare reply")

    # Test merge: new card with same thread should replace old
    card_v2 = PersistentCard(
        card_id="card_2", message_id="msg_001", thread_id="thread_alice",
        title="Alice needs a reply (updated)", status="pending",
    )
    merged = merge_cards(cards, [card_v2])
    check("merge replaces by thread", len(merged.cards) == 1 and merged.cards[0].title == "Alice needs a reply (updated)")

    # Test merge: resolved card dropped
    resolved = PersistentCard(card_id="card_3", thread_id="t_resolved", title="Old", status="resolved")
    existing = ActiveCards(cards=[resolved])
    merged2 = merge_cards(existing, [])
    check("merge drops resolved", len(merged2.cards) == 0)

    # 6. Test storage_ops — active cards
    print("\n── storage_ops: active cards ──")
    await set_active_cards("test@example.com", merged)
    loaded_cards = await get_active_cards("test@example.com")
    check("set/get active cards roundtrip", len(loaded_cards.cards) == 1
          and loaded_cards.cards[0].title == "Alice needs a reply (updated)")

    updated = await update_card_status("test@example.com", "card_2", "resolved", "handled_manually")
    check("update_card_status", updated is not None and updated.status == "resolved")

    loaded2 = await get_active_cards("test@example.com")
    check("resolved card still in active (until merge)", loaded2.cards[0].status == "resolved")

    # 7. Test run records
    print("\n── storage_ops: run records ──")
    run = RunRecord(
        run_id="run_test_1", mailbox="test@example.com",
        strategy_mode="default_secretary", user_request="test request",
        scanned_count=100, candidate_count=5, main_count=3, lower_count=2,
        summary=["Found 3 items"], strategy=["Strategy: secretary"],
    )
    await save_run_record("test@example.com", run)
    loaded_run = await get_run_record("test@example.com", "run_test_1")
    check("save/get run record", loaded_run is not None and loaded_run.scanned_count == 100)

    entry = RunHistoryEntry(
        run_id="run_test_1", mailbox="test@example.com", ts="2026-05-21",
        request="test request", mode="auto", strategy="default_secretary",
        result="3 main, 2 lower", summary="All good",
    )
    await append_run_history(entry)
    history = await get_run_history()
    check("append/get run history", len(history) >= 1 and history[0].run_id == "run_test_1")

    # 8. Test user preferences
    print("\n── storage_ops: user preferences ──")
    prefs = await get_user_prefs()
    check("get_user_prefs (empty)", len(prefs.snooze.senders) == 0)

    from mail_agent.storage_ops import add_snooze_sender, add_snooze_thread
    await add_snooze_sender("newsletter@spam.com")
    await add_snooze_thread("Weekly digest")
    prefs2 = await get_user_prefs()
    check("add_snooze_sender", "newsletter@spam.com" in prefs2.snooze.senders)
    check("add_snooze_thread", "Weekly digest" in prefs2.snooze.threads)

    await append_learning("LinkedIn notifications", "Keep quiet")
    prefs3 = await get_user_prefs()
    check("append_learning", len(prefs3.learning) == 1 and prefs3.learning[0].pattern == "LinkedIn notifications")

    # ── Summary ──
    print(f"\n{'='*50}")
    print(f"  {_green('PASSED')}: {passed}  {_red('FAILED')}: {failed}")
    print(f"{'='*50}\n")
    if failed > 0:
        sys.exit(1)


async def _is_processed(mailbox: str, msg_id: str) -> bool:
    from mail_agent.storage_ops import is_message_processed
    return await is_message_processed(mailbox, msg_id)


if __name__ == "__main__":
    asyncio.run(main())
