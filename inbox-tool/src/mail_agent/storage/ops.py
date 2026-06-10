"""High-level local-persistence operations for the mail agent.

All functions are async and use the shared storage singleton.
Callers must be running inside the asyncio event loop.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from .client import get_storage, get_files, scope as default_scope
from .types import (
    ActiveCards,
    CardAction,
    CardDetails,
    CardStatus,
    LearningRecord,
    MailboxRegistry,
    MailboxRegistryEntry,
    OriginalEmail,
    PersistentCard,
    ProcessedMessage,
    RunHistoryEntry,
    RunRecord,
    ScanPlan,
    ScanState,
    SnoozePrefs,
    UserPreferences,
    _now,
)
from .keys import app_key, sanitize_key_part


# ── Key builders ────────────────────────────────────────────────────

def _sanitize(email: str) -> str:
    """Sanitize email address for use in storage keys."""
    return sanitize_key_part(email.strip())


def _mailbox_prefix(mailbox: str) -> str:
    return app_key(f"mailbox/{_sanitize(mailbox)}")


# ── 邮箱注册表 ────────────────────────────────────────────────

MAILBOX_REGISTRY_KEY = app_key("mailboxes/registry")


def _normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def _dict_to_mailbox_registry_entry(raw: dict[str, Any]) -> MailboxRegistryEntry:
    allowed = MailboxRegistryEntry.__dataclass_fields__
    data = {key: raw.get(key) for key in allowed if key in raw}
    email = _normalize_email(str(data.get("email") or ""))
    return MailboxRegistryEntry(
        **{
            **data,
            "email": email,
            "provider": str(data.get("provider") or "gmail"),
            "authorized": bool(data.get("authorized", True)),
            "selected": bool(data.get("selected", True)),
        }
    )


async def get_mailbox_registry() -> MailboxRegistry:
    result = await get_storage().get(MAILBOX_REGISTRY_KEY, scope=default_scope())
    if result.get("exists") and isinstance(result.get("value"), dict):
        raw = result["value"]
        entries = []
        for item in raw.get("mailboxes", []) if isinstance(raw.get("mailboxes"), list) else []:
            if isinstance(item, dict) and _normalize_email(str(item.get("email") or "")):
                entries.append(_dict_to_mailbox_registry_entry(item))
        return MailboxRegistry(mailboxes=entries, updated_at=str(raw.get("updated_at") or _now()))
    return MailboxRegistry()


async def set_mailbox_registry(registry: MailboxRegistry) -> dict:
    registry.updated_at = _now()
    return await get_storage().set(MAILBOX_REGISTRY_KEY, _dataclass_to_dict(registry), scope=default_scope())


async def upsert_mailbox_registry_entry(entry: MailboxRegistryEntry) -> MailboxRegistry:
    email = _normalize_email(entry.email)
    registry = await get_mailbox_registry()
    found = False
    for idx, existing in enumerate(registry.mailboxes):
        if _normalize_email(existing.email) == email:
            entry.added_at = existing.added_at or entry.added_at
            entry.updated_at = _now()
            registry.mailboxes[idx] = entry
            found = True
            break
    if not found:
        entry.email = email
        entry.updated_at = _now()
        registry.mailboxes.append(entry)
    await set_mailbox_registry(registry)
    return registry


async def merge_discovered_mailboxes(discovered: list[dict[str, Any]]) -> MailboxRegistry:
    registry = await get_mailbox_registry()
    by_email: dict[str, MailboxRegistryEntry] = {
        _normalize_email(entry.email): entry for entry in registry.mailboxes if _normalize_email(entry.email)
    }
    changed = False
    for raw in discovered:
        email = _normalize_email(str(raw.get("email") or raw.get("mailbox") or ""))
        if not email:
            continue
        existing = by_email.get(email)
        if existing:
            existing.provider = str(raw.get("provider") or existing.provider or "gmail")
            existing.auth_source = str(raw.get("auth_source") or raw.get("source") or existing.auth_source or "")
            existing.authorized = bool(raw.get("authorized", existing.authorized))
            existing.last_auth_checked_at = str(raw.get("last_auth_checked_at") or existing.last_auth_checked_at or "")
            existing.updated_at = _now()
        else:
            by_email[email] = MailboxRegistryEntry(
                email=email,
                provider=str(raw.get("provider") or "gmail"),
                auth_source=str(raw.get("auth_source") or raw.get("source") or ""),
                authorized=bool(raw.get("authorized", True)),
                selected=True,
                last_auth_checked_at=str(raw.get("last_auth_checked_at") or ""),
            )
        changed = True

    # 将不在当前 discovered 列表中的已有条目标记为 unauthorized。
    # 平台 token 从邮箱 A 换到 B 时，A 仍在注册表中但 token 已消失；
    # multi-token 被删除时同理。只有 discovered 中的邮箱才是"拿得到 token"的。
    discovered_emails = {_normalize_email(str(r.get("email") or "")) for r in discovered}
    for email, entry in list(by_email.items()):
        if email not in discovered_emails and entry.authorized:
            entry.authorized = False
            entry.updated_at = _now()
            changed = True

    if changed:
        registry.mailboxes = sorted(by_email.values(), key=lambda item: item.email)
        await set_mailbox_registry(registry)
    return registry


async def set_mailbox_selected(mailbox: str, selected: bool) -> MailboxRegistry:
    email = _normalize_email(mailbox)
    registry = await get_mailbox_registry()
    for entry in registry.mailboxes:
        if _normalize_email(entry.email) == email:
            entry.selected = bool(selected)
            entry.updated_at = _now()
            await set_mailbox_registry(registry)
            return registry
    entry = MailboxRegistryEntry(email=email, selected=bool(selected), provider="gmail")
    registry.mailboxes.append(entry)
    await set_mailbox_registry(registry)
    return registry


async def remove_mailbox_from_registry(mailbox: str) -> MailboxRegistry:
    email = _normalize_email(mailbox)
    registry = await get_mailbox_registry()
    registry.mailboxes = [entry for entry in registry.mailboxes if _normalize_email(entry.email) != email]
    await set_mailbox_registry(registry)
    return registry


# ── 多邮箱 token 持久化 ──────────────────────────────────────────

MULTI_TOKENS_KEY = app_key("mailboxes/multi_tokens")


async def get_multi_tokens() -> list[dict[str, Any]]:
    result = await get_storage().get(MULTI_TOKENS_KEY, scope=default_scope())
    if result.get("exists") and isinstance(result.get("value"), list):
        return result["value"]
    return []


async def set_multi_tokens(tokens: list[dict[str, Any]]) -> dict:
    return await get_storage().set(MULTI_TOKENS_KEY, tokens, scope=default_scope())


async def update_mailbox_registry_fields(mailbox: str, **fields: Any) -> MailboxRegistry:
    email = _normalize_email(mailbox)
    if not email:
        return await get_mailbox_registry()
    registry = await get_mailbox_registry()
    for entry in registry.mailboxes:
        if _normalize_email(entry.email) == email:
            for key, value in fields.items():
                if key in MailboxRegistryEntry.__dataclass_fields__:
                    setattr(entry, key, value)
            entry.updated_at = _now()
            await set_mailbox_registry(registry)
            return registry
    entry = MailboxRegistryEntry(email=email, provider="gmail")
    for key, value in fields.items():
        if key in MailboxRegistryEntry.__dataclass_fields__:
            setattr(entry, key, value)
    entry.updated_at = _now()
    registry.mailboxes.append(entry)
    await set_mailbox_registry(registry)
    return registry


async def aggregate_active_cards(mailboxes: list[str] | None = None) -> ActiveCards:
    registry = await get_mailbox_registry()
    allowed = {_normalize_email(mailbox) for mailbox in (mailboxes or []) if _normalize_email(mailbox)}
    if not allowed:
        allowed = {_normalize_email(entry.email) for entry in registry.mailboxes if _normalize_email(entry.email)}
    cards: list[PersistentCard] = []
    latest_updated = ""
    for mailbox in sorted(allowed):
        active = await get_active_cards(mailbox)
        latest_updated = max(latest_updated, active.updated_at or "")
        for card in active.cards:
            if not card.details.mailbox:
                card.details.mailbox = mailbox
            cards.append(card)
    cards.sort(key=lambda card: (_priority_rank(card.priority), card.created_at or ""), reverse=True)
    return ActiveCards(cards=cards, updated_at=latest_updated or _now())


def _priority_rank(priority: str) -> int:
    return {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(str(priority or "").lower(), 0)


# ── Scan state ──────────────────────────────────────────────────────

async def get_scan_state(mailbox: str) -> ScanState:
    key = f"{_mailbox_prefix(mailbox)}/scan_state"
    result = await get_storage().get(key, scope=default_scope())
    if result.get("exists") and result.get("value"):
        raw = dict(result["value"])
        # 兼容旧持久化数据：历史记录里可能没有精确到邮件的最新时间。
        raw.setdefault("last_message_internal_date", "")
        return ScanState(**raw)
    return ScanState.empty(mailbox)


async def set_scan_state(mailbox: str, state: ScanState) -> dict:
    key = f"{_mailbox_prefix(mailbox)}/scan_state"
    result = await get_storage().set(key, _dataclass_to_dict(state), scope=default_scope())
    await update_mailbox_registry_fields(mailbox, last_scan_at=state.last_scan_ts or _now(), last_scan_status="ok", last_error="")
    return result


# ── Scan plan ────────────────────────────────────────────────────────


async def get_scan_plan(mailbox: str) -> ScanPlan:
    key = f"{_mailbox_prefix(mailbox)}/scan_plan"
    result = await get_storage().get(key, scope=default_scope())
    if result.get("exists") and result.get("value"):
        raw = dict(result["value"])
        raw.setdefault("mailbox", mailbox)
        return ScanPlan(**{k: v for k, v in raw.items() if k in ScanPlan.__dataclass_fields__})
    return ScanPlan.empty(mailbox)


async def set_scan_plan(mailbox: str, plan: ScanPlan) -> dict:
    key = f"{_mailbox_prefix(mailbox)}/scan_plan"
    plan.updated_at = _now()
    return await get_storage().set(key, _dataclass_to_dict(plan), scope=default_scope())


# ── Processed message index ─────────────────────────────────────────

def _msg_key(mailbox: str, message_id: str) -> str:
    return f"{_mailbox_prefix(mailbox)}/processed/{message_id}"


async def get_processed_message_ids(mailbox: str) -> set[str]:
    """Return all already-processed Gmail message IDs for a mailbox.

    Uses storage.list with prefix so we avoid N individual GET calls.
    """
    prefix = f"{_mailbox_prefix(mailbox)}/processed/"
    processed: set[str] = set()
    cursor: str | None = None
    while True:
        result = await get_storage().list(prefix=prefix, cursor=cursor, limit=200, scope=default_scope())
        items = result.get("items") or []
        for item in items:
            key = item.get("key") if isinstance(item, dict) else item
            if key and key.startswith(prefix):
                msg_id = key[len(prefix):]
                if msg_id:
                    processed.add(msg_id)
        cursor = result.get("next_cursor")
        if not cursor:
            break
    return processed


async def filter_unprocessed(mailbox: str, message_ids: list[str]) -> list[str]:
    """Given a list of message IDs, return only the unprocessed ones."""
    processed = await get_processed_message_ids(mailbox)
    return [mid for mid in message_ids if mid not in processed]


async def mark_message_processed(mailbox: str, msg: ProcessedMessage) -> dict:
    """Mark a single message as processed."""
    key = _msg_key(mailbox, msg.message_id)
    return await get_storage().set(key, _dataclass_to_dict(msg), scope=default_scope())


async def mark_messages_processed_batch(mailbox: str, msgs: list[ProcessedMessage]) -> None:
    """Write processed-message markers for a batch (no concurrency control needed)."""
    for msg in msgs:
        await mark_message_processed(mailbox, msg)


async def is_message_processed(mailbox: str, message_id: str) -> bool:
    result = await get_storage().get(_msg_key(mailbox, message_id), scope=default_scope())
    return bool(result.get("exists"))


# ── Active cards ────────────────────────────────────────────────────

def _cards_key(mailbox: str) -> str:
    return f"{_mailbox_prefix(mailbox)}/cards/active"


async def \
        get_active_cards(mailbox: str) -> ActiveCards:
    result = await get_storage().get(_cards_key(mailbox), scope=default_scope())
    if result.get("exists") and result.get("value"):
        raw = result["value"]
        cards = [_dict_to_persistent_card(c) for c in raw.get("cards", [])]
        cards.sort(key=lambda c: (_priority_rank(c.priority), c.created_at or ""), reverse=True)
        return ActiveCards(cards=cards, updated_at=raw.get("updated_at", ""))
    return ActiveCards()


async def set_active_cards(mailbox: str, cards: ActiveCards) -> dict:
    cards.updated_at = _now()
    result = await get_storage().set(_cards_key(mailbox), _dataclass_to_dict(cards), scope=default_scope())
    await update_mailbox_registry_fields(mailbox, card_count=len(cards.cards))
    return result


async def update_card_status(
    mailbox: str, card_id: str, status: CardStatus, resolution: str = ""
) -> PersistentCard | None:
    """Update a single card's status in the active cards list."""
    active = await get_active_cards(mailbox)
    for card in active.cards:
        if card.card_id == card_id:
            card.status = status
            card.updated_at = _now()
            if status in ("resolved", "dismissed"):
                card.resolved_at = _now()
                card.resolution = resolution
            await set_active_cards(mailbox, active)
            return card
    return None


# ── Run records ─────────────────────────────────────────────────────

def _run_key(mailbox: str, run_id: str) -> str:
    return f"{_mailbox_prefix(mailbox)}/run/{run_id}"


async def save_run_record(mailbox: str, run: RunRecord) -> dict:
    return await get_storage().set(_run_key(mailbox, run.run_id), _dataclass_to_dict(run), scope=default_scope())


async def get_run_record(mailbox: str, run_id: str) -> RunRecord | None:
    result = await get_storage().get(_run_key(mailbox, run_id), scope=default_scope())
    if result.get("exists") and result.get("value"):
        return RunRecord(**result["value"])
    return None


# ── Run history (cross-mailbox) ─────────────────────────────────────

RUN_HISTORY_KEY = app_key("runs/history")


async def get_run_history(limit: int = 20) -> list[RunHistoryEntry]:
    result = await get_storage().get(RUN_HISTORY_KEY, scope=default_scope())
    if result.get("exists") and result.get("value"):
        raw = result["value"]
        entries = [_dict_to_run_history_entry(e) for e in raw.get("entries", [])]
        return entries[:limit]
    return []


async def append_run_history(entry: RunHistoryEntry) -> dict:
    result = await get_storage().get(RUN_HISTORY_KEY, scope=default_scope())
    raw = result.get("value") if result.get("exists") else {"entries": []}
    if not isinstance(raw, dict):
        raw = {"entries": []}
    entries: list[dict] = raw.get("entries", [])
    entries.insert(0, _dataclass_to_dict(entry))
    # Keep last 50
    if len(entries) > 50:
        entries = entries[:50]
    raw["entries"] = entries
    return await get_storage().set(RUN_HISTORY_KEY, raw, scope=default_scope())


async def clear_run_history() -> dict:
    """Delete all run history entries."""
    return await get_storage().set(RUN_HISTORY_KEY, {"entries": []}, scope=default_scope())


async def clear_cards_by_category(mailbox: str, category: str) -> int:
    """Remove cards matching a given category from active cards. Returns count removed.

    category: "reply" | "review" | "cleanup" | "all"
    """
    active = await get_active_cards(mailbox)
    original_count = len(active.cards)

    if category == "all":
        active.cards = []
    elif category == "cleanup":
        active.cards = [
            c for c in active.cards
            if not (c.user_action == "cleanup" or c.card_type == "cleanup_bundle")
        ]
    elif category == "reply":
        active.cards = [c for c in active.cards if c.user_action != "reply"]
    elif category == "review":
        active.cards = [c for c in active.cards if c.user_action != "review"]
    else:
        return 0

    removed = original_count - len(active.cards)
    if removed > 0:
        await set_active_cards(mailbox, active)
        await update_mailbox_registry_fields(mailbox, card_count=len(active.cards))
    return removed


async def reset_all_data() -> dict:
    """Reset all persistent data via storage API.

    For local file storage the caller should also delete the data directory
    after this returns (handled by main.py's data_root() cleanup).
    """
    storage = get_storage()
    # 1. Clear mailbox-level data for all known mailboxes
    registry = await get_mailbox_registry()
    for entry in registry.mailboxes:
        mbox = entry.email
        prefix = _mailbox_prefix(mbox)
        # Use correct key suffixes matching _cards_key, _scan_key etc
        for sub in ("cards/active", "scan_state", "scan_plan", "processed"):
            try:
                await storage.delete(f"{prefix}/{sub}", scope=default_scope())
            except Exception:
                pass
        # Contact memories
        try:
            from ..contact_memory.store import list_contact_memories, clear_contact_memories
            await clear_contact_memories(mbox)
        except Exception:
            pass
        # Run records
        try:
            result = await storage.list(f"{prefix}/run/", scope=default_scope())
            for item in result.get("items", []):
                try:
                    await storage.delete(item.get("key", ""), scope=default_scope())
                except Exception:
                    pass
        except Exception:
            pass

    # 2. Clear cross-mailbox data
    for key in (RUN_HISTORY_KEY, _CUSTOM_PLANS_KEY, MAILBOX_REGISTRY_KEY):
        try:
            await storage.delete(key, scope=default_scope())
        except Exception:
            pass

    # 3. Clear Gmail cache storage keys (APS)
    try:
        result = await storage.list(app_key("gmail_cache/mailboxes/"), scope=default_scope())
        for item in result.get("items", []):
            try:
                await storage.delete(item.get("key", ""), scope=default_scope())
            except Exception:
                pass
    except Exception:
        pass

    return {"ok": True}
async def append_card_action(
    mailbox: str,
    card_id: str,
    card_title: str,
    action: str,
    detail: str = "",
    *,
    card_summary: str = "",
    card_from: str = "",
    card_subject: str = "",
    card_body: str = "",
) -> dict:
    """Record a card-level action (snooze, reply, handle, etc.) in run history."""
    from uuid import uuid4
    from .types import _now

    entry = RunHistoryEntry(
        run_id=f"act_{uuid4().hex[:12]}",
        mailbox=mailbox,
        ts=_now(),
        entry_type="card_action",
        card_id=card_id,
        card_title=card_title,
        action=action,
        detail=detail,
        result=f"{action}: {card_title}",
        summary=detail or action,
        card_summary=card_summary,
        card_from=card_from,
        card_subject=card_subject,
        card_body=card_body,
    )
    return await append_run_history(entry)


# ── User preferences ────────────────────────────────────────────────

SNOOZE_KEY = app_key("prefs/snooze")
LEARNING_KEY = app_key("prefs/learning")


async def get_user_prefs() -> UserPreferences:
    prefs = UserPreferences()
    snooze_result = await get_storage().get(SNOOZE_KEY, scope=default_scope())
    if snooze_result.get("exists") and snooze_result.get("value"):
        prefs.snooze = SnoozePrefs(**snooze_result["value"])
    learning_result = await get_storage().get(LEARNING_KEY, scope=default_scope())
    if learning_result.get("exists") and learning_result.get("value"):
        raw = learning_result["value"]
        prefs.learning = [LearningRecord(**r) for r in raw.get("records", [])]
    return prefs


async def set_snooze_prefs(prefs: SnoozePrefs) -> dict:
    prefs.updated_at = _now()
    return await get_storage().set(SNOOZE_KEY, _dataclass_to_dict(prefs), scope=default_scope())


async def add_snooze_sender(sender: str) -> dict:
    """Add a sender to the snooze preference list."""
    prefs = await get_user_prefs()
    if sender not in prefs.snooze.senders:
        prefs.snooze.senders.append(sender)
    return await set_snooze_prefs(prefs.snooze)


async def add_snooze_thread(thread: str) -> dict:
    prefs = await get_user_prefs()
    if thread not in prefs.snooze.threads:
        prefs.snooze.threads.append(thread)
    return await set_snooze_prefs(prefs.snooze)


async def remove_snooze_sender(sender: str) -> dict:
    prefs = await get_user_prefs()
    prefs.snooze.senders = [s for s in prefs.snooze.senders if s != sender]
    return await set_snooze_prefs(prefs.snooze)


async def remove_snooze_thread(thread: str) -> dict:
    prefs = await get_user_prefs()
    prefs.snooze.threads = [t for t in prefs.snooze.threads if t != thread]
    return await set_snooze_prefs(prefs.snooze)


async def append_learning(pattern: str, action: str) -> dict:
    result = await get_storage().get(LEARNING_KEY, scope=default_scope())
    raw = result.get("value") if result.get("exists") else {"records": []}
    if not isinstance(raw, dict):
        raw = {"records": []}
    records: list[dict] = raw.get("records", [])
    records.append(_dataclass_to_dict(LearningRecord(pattern=pattern, action=action)))
    if len(records) > 100:
        records = records[-100:]
    raw["records"] = records
    return await get_storage().set(LEARNING_KEY, raw, scope=default_scope())


# ── Serialization helpers ───────────────────────────────────────────

def _dataclass_to_dict(obj: Any) -> dict:
    """Convert a dataclass instance to a JSON-safe dict."""
    from dataclasses import asdict
    return asdict(obj)


def _dict_to_persistent_card(d: dict) -> PersistentCard:
    details = CardDetails(**d.get("details", {})) if isinstance(d.get("details"), dict) else CardDetails()
    original = OriginalEmail(**d.get("original", {})) if isinstance(d.get("original"), dict) else OriginalEmail()
    actions = [_dict_to_card_action(a) for a in d.get("actions", [])] if isinstance(d.get("actions"), list) else []
    return PersistentCard(
        card_id=d.get("card_id", ""),
        message_id=d.get("message_id", ""),
        thread_id=d.get("thread_id", ""),
        title=d.get("title", ""),
        summary=d.get("summary", ""),
        recommendation=d.get("recommendation", ""),
        label=d.get("label", ""),
        priority=d.get("priority", "medium"),
        item_type=d.get("item_type", ""),
        draft_reply=d.get("draft_reply", ""),
        thread_summary=d.get("thread_summary", ""),
        display_section=d.get("display_section", "main"),
        details=details,
        original=original,
        actions=actions,
        status=d.get("status", "pending"),
        snooze_until=d.get("snooze_until", ""),
        created_at=d.get("created_at", ""),
        updated_at=d.get("updated_at", ""),
        resolved_at=d.get("resolved_at", ""),
        resolution=d.get("resolution", ""),
        card_type=d.get("card_type", ""),
        bundled_messages=d.get("bundled_messages", []),
        user_action=d.get("user_action", ""),
        reply_gaps=d.get("reply_gaps", {}) if isinstance(d.get("reply_gaps"), dict) else {},
    )


def _dict_to_card_action(d: dict) -> CardAction:
    return CardAction(
        id=d.get("id", ""),
        label=d.get("label", ""),
        button_label=d.get("button_label", ""),
        primary=d.get("primary", False),
        status_title=d.get("status_title", ""),
        status=d.get("status", ""),
    )


def _dict_to_run_history_entry(d: dict) -> RunHistoryEntry:
    return RunHistoryEntry(
        run_id=d.get("run_id", ""),
        mailbox=d.get("mailbox", ""),
        ts=d.get("ts", ""),
        request=d.get("request", ""),
        mode=d.get("mode", ""),
        strategy=d.get("strategy", ""),
        plan_id=d.get("plan_id", ""),
        result=d.get("result", ""),
        summary=d.get("summary", ""),
        entry_type=d.get("entry_type", "scan"),
        card_id=d.get("card_id", ""),
        card_title=d.get("card_title", ""),
        action=d.get("action", ""),
        detail=d.get("detail", ""),
        card_summary=d.get("card_summary", ""),
        card_from=d.get("card_from", ""),
        card_subject=d.get("card_subject", ""),
        card_body=d.get("card_body", ""),
    )


# ── Custom scan plans ──────────────────────────────────────────────

_CUSTOM_PLANS_KEY = app_key("custom/scan_plans")
_MAX_CUSTOM_PLANS = 20


async def save_custom_plan(plan: Any) -> None:
    """保存或更新一个 CustomScanPlan。plan_id 已存在则覆盖，最多保留 20 条。"""
    storage = get_storage()
    raw = await storage.get(_CUSTOM_PLANS_KEY, scope=default_scope())
    data = raw.get("value") if raw.get("exists") else {"plans": []}
    if not isinstance(data, dict):
        data = {"plans": []}
    plans: list[dict[str, Any]] = data.get("plans", [])
    if not isinstance(plans, list):
        plans = []

    # Support both old CustomScanPlan and new AskPlan
    plan_dict: dict[str, Any] = {
        "plan_id": getattr(plan, "plan_id", ""),
        "user_request": getattr(plan, "user_request", ""),
        "title": getattr(plan, "title", ""),
        "description": getattr(plan, "description", ""),
        "task_prompt": getattr(plan, "task_prompt", ""),
        "created_at": getattr(plan, "created_at", ""),
        "last_used_at": getattr(plan, "last_used_at", ""),
        "use_count": getattr(plan, "use_count", 0),
        "last_result_summary": getattr(plan, "last_result_summary", ""),
        # Old CustomScanPlan fields (default for AskPlan)
        "gmail_queries": getattr(plan, "gmail_queries", []),
        "scan_budget": getattr(plan, "scan_budget", {}),
        "read_depth": getattr(plan, "read_depth", ""),
        # New AskPlan fields (default for old CustomScanPlan)
        "people": getattr(plan, "people", []),
        "topics": getattr(plan, "topics", []),
        "timeframe": getattr(plan, "timeframe", ""),
        "direction": getattr(plan, "direction", ""),
        "goal": getattr(plan, "goal", ""),
        "gmail_flags": getattr(plan, "gmail_flags", []),
        "confidence": getattr(plan, "confidence", 0.0),
        "_plan_type": "ask" if hasattr(plan, "direction") and not hasattr(plan, "scan_budget") else "custom",
    }

    # Replace existing or prepend
    existing_idx = next((i for i, p in enumerate(plans) if p.get("plan_id") == plan.plan_id), None)
    if existing_idx is not None:
        plans[existing_idx] = plan_dict
    else:
        plans.insert(0, plan_dict)
        if len(plans) > _MAX_CUSTOM_PLANS:
            plans = plans[:_MAX_CUSTOM_PLANS]

    data["plans"] = plans
    await storage.set(_CUSTOM_PLANS_KEY, data, scope=default_scope())


async def get_custom_plan(plan_id: str) -> dict[str, Any] | None:
    """按 ID 加载单个 plan 的完整字典。"""
    storage = get_storage()
    raw = await storage.get(_CUSTOM_PLANS_KEY, scope=default_scope())
    data = raw.get("value") if raw.get("exists") else {"plans": []}
    plans: list[dict[str, Any]] = data.get("plans", []) if isinstance(data, dict) else []
    for p in plans:
        if p.get("plan_id") == plan_id:
            return p
    return None


async def list_custom_plans() -> list[dict[str, Any]]:
    """返回所有 plan 的元数据列表（供前端展示）。"""
    storage = get_storage()
    raw = await storage.get(_CUSTOM_PLANS_KEY, scope=default_scope())
    data = raw.get("value") if raw.get("exists") else {"plans": []}
    plans: list[dict[str, Any]] = data.get("plans", []) if isinstance(data, dict) else []
    # Return lightweight metadata
    return [
        {
            "plan_id": p.get("plan_id", ""),
            "title": p.get("title", ""),
            "user_request": p.get("user_request", ""),
            "created_at": p.get("created_at", ""),
            "last_used_at": p.get("last_used_at", ""),
            "use_count": p.get("use_count", 0),
            "last_result_summary": p.get("last_result_summary", ""),
        }
        for p in plans
    ]


async def update_plan_result(plan_id: str, summary: str) -> None:
    """执行完成后更新 last_used_at、use_count 和 last_result_summary。"""
    from datetime import datetime, timedelta, timezone
    BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
    now = datetime.now(BEIJING_TZ).isoformat()

    storage = get_storage()
    raw = await storage.get(_CUSTOM_PLANS_KEY, scope=default_scope())
    data = raw.get("value") if raw.get("exists") else {"plans": []}
    plans: list[dict[str, Any]] = data.get("plans", []) if isinstance(data, dict) else []
    for p in plans:
        if p.get("plan_id") == plan_id:
            p["last_used_at"] = now
            p["use_count"] = p.get("use_count", 0) + 1
            p["last_result_summary"] = summary
            break
    data["plans"] = plans
    await storage.set(_CUSTOM_PLANS_KEY, data, scope=default_scope())


async def delete_custom_plan(plan_id: str) -> None:
    storage = get_storage()
    raw = await storage.get(_CUSTOM_PLANS_KEY, scope=default_scope())
    data = raw.get("value") if raw.get("exists") else {"plans": []}
    plans: list[dict[str, Any]] = data.get("plans", []) if isinstance(data, dict) else []
    data["plans"] = [p for p in plans if p.get("plan_id") != plan_id]
    await storage.set(_CUSTOM_PLANS_KEY, data, scope=default_scope())
