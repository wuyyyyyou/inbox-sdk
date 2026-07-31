"""Local integration test for storage + card system.

Uses a fake in-memory StorageClient that mimics the APS wire protocol
(get/set/list/delete), so tests run without the Anna platform.

Run:  cd src && py -3 tests/test_storage_integration.py
"""

from __future__ import annotations

import asyncio
import json
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

    # APS 本地 Runtime 在 GBK stdout 下只能安全转发 ASCII reverse-RPC。
    # SDK 必须在写入前包装 Unicode，并在读取后无损还原业务值。
    from executa_sdk.storage import StorageClient

    transport_frames: list[dict[str, Any]] = []
    transport_client = StorageClient(write_frame=transport_frames.append)
    stored_value = {"subject": "\U0001f4c5 \u4f1a\u8bae\u5b89\u6392", "nested": ["\u5f20\u4e09"]}
    set_task = asyncio.create_task(transport_client.set("test/unicode", stored_value, scope="user"))
    await asyncio.sleep(0)
    set_frame = transport_frames.pop()
    check("APS storage/set transport is ASCII", json.dumps(set_frame, ensure_ascii=False).isascii())
    transport_client.dispatch_response({"id": set_frame["id"], "result": {"etag": "unicode-etag"}})
    await set_task

    get_task = asyncio.create_task(transport_client.get("test/unicode", scope="user"))
    await asyncio.sleep(0)
    get_frame = transport_frames.pop()
    encoded_value = set_frame["params"]["value"]
    transport_client.dispatch_response({"id": get_frame["id"], "result": {"exists": True, "etag": "unicode-etag", "value": encoded_value}})
    restored = await get_task
    check("APS storage/get restores Unicode", restored.get("value") == stored_value)

    # 1. Wire fake storage into the singleton
    print("\n── Setup ──")
    fake = FakeStorageClient()
    fake_files = FakeStorageClient()  # type: ignore[assignment]

    from mail_agent.storage.client import init
    init(fake, fake_files, scope="user")  # type: ignore[arg-type]
    print("  Fake StorageClient wired into singleton")

    # 2. Test storage_types
    print("\n── storage_types ──")
    from mail_agent.storage.types import (
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
        user_action="reply",
    )
    check("PersistentCard built", card.card_id == "card_1" and len(card.actions) == 1)

    # 3. Test storage_ops — scan state
    print("\n── storage_ops: scan state ──")
    from mail_agent.storage.ops import (
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
    from mail_agent.cards.service import (
        build_card, merge_cards, cards_to_frontend, build_action_memo,
     )
    from mail_agent.domain.types import CandidateItem, JudgmentResult, FinalDecision, BaseJudgment

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
        title="Alice needs a reply (updated)", status="pending", user_action="reply",
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

    print("\n── storage_tools: mark_card_read ──")
    review_card = PersistentCard(
        card_id="card_review", message_id="msg_review", thread_id="thread_review",
        title="Review this notice", status="pending", user_action="review",
        details=CardDetails(mailbox="test@example.com"),
    )
    await set_active_cards("test@example.com", ActiveCards(cards=[review_card]))
    import anna_inbox_executa.storage_tools as storage_tools
    init(fake, fake_files, scope="user")  # storage_tools import wires common.py; restore fake singleton
    original_mark_read = storage_tools._mark_gmail_messages_read

    async def fake_mark_read(mailbox: str, message_ids: list[str]):
        return {"ok": True, "mailbox": mailbox, "message_ids": message_ids}, "", ""

    storage_tools._mark_gmail_messages_read = fake_mark_read
    try:
        result = await storage_tools._handle_mark_card_read({"mailbox": "test@example.com", "card_id": "card_review"})
        updated_review_cards = await get_active_cards("test@example.com")
        updated_review = next((c for c in updated_review_cards.cards if c.card_id == "card_review"), None)
        check("mark_card_read resolves review card", bool(result.get("ok")) and updated_review is not None and updated_review.resolution == "read")

        reply_card = PersistentCard(
            card_id="card_reply", message_id="msg_reply", thread_id="thread_reply",
            title="Reply to Alice", status="pending", user_action="reply",
            details=CardDetails(mailbox="test@example.com"),
        )
        await set_active_cards("test@example.com", ActiveCards(cards=[reply_card]))
        reply_result = await storage_tools._handle_mark_card_read({"mailbox": "test@example.com", "card_id": "card_reply"})
        check("mark_card_read rejects non-review card", "only supports review cards" in str(reply_result.get("error", "")))
    finally:
        storage_tools._mark_gmail_messages_read = original_mark_read

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
    updated_entry = RunHistoryEntry(
        run_id="run_test_1", mailbox="test@example.com", ts="2026-05-21",
        request="test request", mode="auto", strategy="default_secretary",
        result="Scanned 100 emails, 3 needs reply, 2 needs review", summary="Updated",
    )
    await append_run_history(updated_entry)
    history = await get_run_history()
    same_run_entries = [h for h in history if h.run_id == "run_test_1"]
    check(
        "append run history updates same run",
        len(same_run_entries) == 1 and history[0].result == "Scanned 100 emails, 3 needs reply, 2 needs review",
    )

    # 8. Test user preferences
    print("\n── storage_ops: user preferences ──")
    prefs = await get_user_prefs()
    check("get_user_prefs (empty)", len(prefs.snooze.senders) == 0)

    from mail_agent.storage.ops import add_snooze_sender, add_snooze_thread
    await add_snooze_sender("newsletter@spam.com")
    await add_snooze_thread("Weekly digest")
    prefs2 = await get_user_prefs()
    check("add_snooze_sender", "newsletter@spam.com" in prefs2.snooze.senders)
    check("add_snooze_thread", "Weekly digest" in prefs2.snooze.threads)

    await append_learning("LinkedIn notifications", "Keep quiet")
    prefs3 = await get_user_prefs()
    check("append_learning", len(prefs3.learning) == 1 and prefs3.learning[0].pattern == "LinkedIn notifications")

    print("\n-- contact memory --")
    from mail_agent.contact_memory.indexer import ingest_card_event
    from mail_agent.contact_memory.manager import backfill_active_card_memories, clear_memory, delete_memory, get_memory_detail, list_memory_summaries
    from mail_agent.contact_memory.retriever import retrieve_contact_context
    from mail_agent.contact_memory.store import get_contact_memory
    from mail_agent.contact_memory.types import ContactMemoryQuery

    async def fake_selector(**kwargs):
        return {"content": {"text": '{"selected":[{"thread_id":"thread_memory_old","relevance":0.91,"reason":"same candidate follow-up context","include_message_ids":["msg_memory_old"]}],"none_reason":""}'}}

    memory_card = PersistentCard(
        card_id="card_memory_old", message_id="msg_memory_old", thread_id="thread_memory_old",
        title="Alice follows up on Priya",
        summary="Alice asks whether Priya should move forward.",
        recommendation="Confirm whether to advance Priya.",
        original=OriginalEmail(from_addr="Alice <alice@x.com>", thread="Priya next round", body="Can we move faster on Priya?"),
    )
    current_card = PersistentCard(
        card_id="card_memory_current", message_id="msg_memory_current", thread_id="thread_memory_current",
        title="Alice asks for an update",
        summary="Alice asks whether there is a decision.",
        recommendation="Reply with the decision status.",
        original=OriginalEmail(from_addr="Alice <alice@x.com>", thread="Priya decision", body="Any update?"),
    )
    await ingest_card_event("test@example.com", memory_card)
    await ingest_card_event("test@example.com", current_card)
    memory_file = await get_contact_memory("test@example.com", "alice@x.com")
    contact_ctx = await retrieve_contact_context(ContactMemoryQuery(
        mailbox="test@example.com",
        contact_email="alice@x.com",
        current_subject="Priya decision",
        current_body="Any update?",
        current_thread_id="thread_memory_current",
        purpose="thread_summary",
    ), sampling_create_message=fake_selector)
    same_thread_ctx = await retrieve_contact_context(ContactMemoryQuery(
        mailbox="test@example.com",
        contact_email="alice@x.com",
        current_subject="Priya next round",
        current_body="Priya",
        current_thread_id="thread_memory_old",
        purpose="thread_summary",
    ), sampling_create_message=fake_selector)
    isolated_ctx = await retrieve_contact_context(ContactMemoryQuery(
        mailbox="other@example.com",
        contact_email="alice@x.com",
        current_subject="Priya next round",
        current_body="Priya",
        current_thread_id="thread_memory",
        purpose="thread_summary",
    ), sampling_create_message=fake_selector)
    check("contact memory stores thread memories", memory_file is not None and len(memory_file.threads) == 2)
    old_thread_memory = next((item for item in (memory_file.threads if memory_file else []) if item.thread_id == "thread_memory_old"), None)
    check(
        "contact memory thread summary uses card summary",
        old_thread_memory is not None
        and old_thread_memory.thread_summary.summary == "Alice asks whether Priya should move forward."
        and old_thread_memory.thread_summary.summary != "Can we move faster on Priya?",
    )
    check("contact memory selector retrieves prior thread", len(contact_ctx.relevant_topics) == 1 and contact_ctx.relevant_topics[0].thread_id == "thread_memory_old")
    check("contact memory excludes current thread", len(same_thread_ctx.relevant_topics) == 0)
    check("contact memory mailbox isolation", len(isolated_ctx.relevant_topics) == 0)
    memory_list = await list_memory_summaries(["test@example.com"])
    memory_detail = await get_memory_detail("test@example.com", "alice@x.com")
    check("contact memory manager lists contacts", memory_list.get("count") == 1 and memory_list["contacts"][0]["contact_email"] == "alice@x.com")
    check("contact memory manager gets detail", "memory" in memory_detail and len(memory_detail["memory"]["threads"]) == 2)
    await delete_memory("test@example.com", "alice@x.com")
    deleted_memory = await get_contact_memory("test@example.com", "alice@x.com")
    check("contact memory manager deletes one contact", deleted_memory is None)
    await ingest_card_event("test@example.com", memory_card)
    cleared = await clear_memory(["test@example.com"])
    check("contact memory manager clears mailbox", cleared.get("deleted") == 1 and await get_contact_memory("test@example.com", "alice@x.com") is None)
    await set_active_cards("test@example.com", ActiveCards(cards=[memory_card]))
    backfilled = await backfill_active_card_memories(["test@example.com"])
    backfilled_memory = await get_contact_memory("test@example.com", "alice@x.com")
    check("contact memory backfills active cards", backfilled.get("backfilled") == 1 and backfilled_memory is not None)
    await clear_memory(["test@example.com"])
    # Brief pipeline / _persist_run_results 已下线，contact memory 改由侧栏与线程事件写入。

    # 选择性 APS 同步：工作流分类走 APS，邮件缓存只留在本地。
    print("\n── selective APS storage ──")
    from mail_agent.storage.aps_cleanup import migrate_and_cleanup_aps
    from mail_agent.storage.client import get_aps_storage, get_local_storage, get_storage, init_selective
    from mail_agent.storage.keys import app_key
    from mail_agent.storage.ops import get_inbox_workflow_state, set_inbox_workflow_state

    local_sync = FakeStorageClient()
    aps_sync = FakeStorageClient()
    init_selective(local_sync, local_sync, aps_sync, aps_sync)
    saved_workflow = await set_inbox_workflow_state(
        "one@example.com",
        {"todos": ["todo-1"], "done": ["done-1"], "snoozed": ["later-1"], "snoozedUntil": {"later-1": "2030-01-01"}},
    )
    synced_workflow = await get_inbox_workflow_state("one@example.com")
    local_workflow = await get_local_storage().get(app_key("mailbox/one_example.com/inbox_workflow_state"))
    aps_workflow = await get_aps_storage().get(app_key("mailbox/one_example.com/inbox_workflow_sync"))
    check(
        "selective workflow synchronizes all classifications",
        synced_workflow["state"]["done"] == ["done-1"]
        and local_workflow["value"]["done"] == ["done-1"]
        and aps_workflow["value"]["done"] == ["done-1"]
        and synced_workflow["etag"] == saved_workflow["etag"],
    )
    cache_key = app_key("gmail_cache/mailboxes/one_example.com/index")
    await get_storage().set(cache_key, {"messages": []})
    check(
        "selective storage keeps cache local",
        (await get_local_storage().get(cache_key))["exists"] is True
        and (await get_aps_storage().get(cache_key))["exists"] is False,
    )
    sync_keys = [
        app_key("mailbox/one_example.com/ask_history"),
        app_key("mailbox/one_example.com/inbox_settings"),
        app_key("mailbox/one_example.com/inbox-drafts/thread-1"),
        app_key("mailbox/one_example.com/compose-drafts/draft-1"),
    ]
    for key in sync_keys:
        await get_storage().set(key, {"synced": key})
    aps_sync_results = [await get_aps_storage().get(key) for key in sync_keys]
    local_sync_results = [await get_local_storage().get(key) for key in sync_keys]
    check(
        "selective storage routes all approved data to APS",
        all(result["exists"] for result in aps_sync_results)
        and not any(result["exists"] for result in local_sync_results),
    )

    legacy_workflow = app_key("mailbox/legacy_example.com/inbox_workflow_state")
    unknown_key = app_key("future-feature/value")
    await aps_sync.set(legacy_workflow, {"todos": ["todo-2"], "done": ["done-2"], "snoozed": ["later-2"]})
    await aps_sync.set(unknown_key, {"keep": True})
    cleanup = await migrate_and_cleanup_aps(aps_sync, scope="user")
    migrated_key = app_key("mailbox/legacy_example.com/inbox_workflow_sync")
    migrated = await aps_sync.get(migrated_key)
    check(
        "APS cleanup migrates workflow and preserves unknown keys",
        cleanup["migrated"] == 1
        and (await aps_sync.get(legacy_workflow))["exists"] is False
        and migrated["value"]["todos"] == ["todo-2"]
        and migrated["value"]["done"] == ["done-2"]
        and (await aps_sync.get(unknown_key))["exists"] is True,
    )

    # ── Summary ──
    print(f"\n{'='*50}")
    print(f"  {_green('PASSED')}: {passed}  {_red('FAILED')}: {failed}")
    print(f"{'='*50}\n")
    if failed > 0:
        sys.exit(1)


async def _is_processed(mailbox: str, msg_id: str) -> bool:
    from mail_agent.storage.ops import is_message_processed
    return await is_message_processed(mailbox, msg_id)


if __name__ == "__main__":
    asyncio.run(main())
