"""High-level local-persistence operations for the mail agent.

All functions are async and use the shared storage singleton.
Callers must be running inside the asyncio event loop.
"""

from __future__ import annotations

import json
import uuid
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
    InboxCustomCategory,
    InboxSettings,
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


def _conversation_state_key(mailbox: str, conversation_id: str) -> str:
    """按邮箱与会话隔离 P3 active_scope/evidence 状态，禁止跨线程串线。"""
    return f"{_mailbox_prefix(mailbox)}/conversation-state/{sanitize_key_part(conversation_id)}"


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
            existing.display_name = str(raw.get("display_name") or raw.get("name") or existing.display_name or "")
            existing.provider = str(raw.get("provider") or existing.provider or "gmail")
            existing.avatar_url = str(raw.get("avatar_url") or raw.get("picture") or existing.avatar_url or "")
            existing.auth_source = str(raw.get("auth_source") or raw.get("source") or existing.auth_source or "")
            existing.authorized = bool(raw.get("authorized", existing.authorized))
            existing.last_auth_checked_at = str(raw.get("last_auth_checked_at") or existing.last_auth_checked_at or "")
            existing.updated_at = _now()
        else:
            by_email[email] = MailboxRegistryEntry(
                email=email,
                display_name=str(raw.get("display_name") or raw.get("name") or ""),
                avatar_url=str(raw.get("avatar_url") or raw.get("picture") or ""),
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


def _inbox_settings_key(mailbox: str) -> str:
    """构造按邮箱隔离的 Inbox 设置 key，避免不同账号共享偏好。"""
    return f"{_mailbox_prefix(mailbox)}/inbox_settings"


def _inbox_workflow_state_key(mailbox: str) -> str:
    """构造按邮箱隔离的 Todo/Done/Snoozed 状态 key。"""
    return f"{_mailbox_prefix(mailbox)}/inbox_workflow_state"


def _ask_history_key(mailbox: str) -> str:
    """构造按邮箱隔离的 AI Ask 会话历史 key。"""
    return f"{_mailbox_prefix(mailbox)}/ask_history"


def _normalize_message_ids(value: Any, *, limit: int = 500) -> list[str]:
    """规范化 Gmail message_id 列表，去重并限制单邮箱工作流状态体积。"""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    ids: list[str] = []
    for item in value:
        message_id = str(item or "").strip()
        if not message_id or message_id in seen:
            continue
        seen.add(message_id)
        ids.append(message_id[:256])
        if len(ids) >= limit:
            break
    return ids


def _normalize_inbox_workflow_state(value: Any) -> dict[str, Any]:
    """规范化互斥工作流状态，Todo 优先级高于 Done，避免同一邮件出现在两个分组。"""
    raw = value if isinstance(value, dict) else {}
    todos = _normalize_message_ids(raw.get("todos"))
    done = [item for item in _normalize_message_ids(raw.get("done")) if item not in set(todos)]
    snoozed = [item for item in _normalize_message_ids(raw.get("snoozed")) if item not in set(todos) and item not in set(done)]
    until_raw = raw.get("snoozedUntil") if isinstance(raw.get("snoozedUntil"), dict) else {}
    snoozed_until = {
        message_id: str(until_raw.get(message_id) or "")[:64]
        for message_id in snoozed
        if str(until_raw.get(message_id) or "").strip()
    }
    return {
        "todos": todos,
        "done": done,
        "snoozed": snoozed,
        "snoozedUntil": snoozed_until,
        "version": 1,
        "updated_at": _now(),
    }


async def get_inbox_workflow_state(mailbox: str) -> dict[str, Any]:
    """读取邮箱级工作流状态；不存在时返回空状态和空 etag。"""
    result = await get_storage().get(_inbox_workflow_state_key(mailbox), scope=default_scope())
    raw = result.get("value") if result.get("exists") and isinstance(result.get("value"), dict) else {}
    return {
        "exists": bool(result.get("exists")),
        "state": _normalize_inbox_workflow_state(raw),
        "etag": str(result.get("etag") or ""),
    }


async def set_inbox_workflow_state(
    mailbox: str,
    state: dict[str, Any],
    *,
    if_match: str | None = None,
) -> dict[str, Any]:
    """保存完整工作流状态；调用方必须传递最新 etag 以避免跨端覆盖。"""
    normalized = _normalize_inbox_workflow_state(state)
    result = await get_storage().set(
        _inbox_workflow_state_key(mailbox),
        normalized,
        scope=default_scope(),
        if_match=if_match,
    )
    return {"state": normalized, "etag": str(result.get("etag") or "")}


async def get_ai_ask_history(mailbox: str) -> dict[str, Any]:
    """读取按邮箱保存的 AI Ask 会话索引，不返回其他邮箱历史。"""
    result = await get_storage().get(_ask_history_key(mailbox), scope=default_scope())
    raw = result.get("value") if result.get("exists") and isinstance(result.get("value"), dict) else {}
    entries = raw.get("entries") if isinstance(raw.get("entries"), list) else []
    return {
        "exists": bool(result.get("exists")),
        "entries": entries[:30],
        "etag": str(result.get("etag") or ""),
    }


async def set_ai_ask_history(
    mailbox: str,
    entries: list[dict[str, Any]],
    *,
    if_match: str | None = None,
) -> dict[str, Any]:
    """保存按邮箱隔离的 AI Ask 会话索引，保留最多 30 条由前端裁剪后的记录。"""
    safe_entries = [item for item in entries if isinstance(item, dict)][:30]
    payload = {"entries": safe_entries, "updated_at": _now(), "version": 1}
    result = await get_storage().set(_ask_history_key(mailbox), payload, scope=default_scope(), if_match=if_match)
    return {"entries": safe_entries, "etag": str(result.get("etag") or "")}


async def get_conversation_state(mailbox: str, conversation_id: str) -> dict[str, Any]:
    """读取 P3 结构化会话状态；不存在时返回空 active_scope。"""
    cid = str(conversation_id or "").strip()
    if not cid:
        return {"exists": False, "state": {}, "etag": ""}
    result = await get_storage().get(_conversation_state_key(mailbox, cid), scope=default_scope())
    value = result.get("value") if result.get("exists") else None
    return {
        "exists": bool(result.get("exists")),
        "state": dict(value) if isinstance(value, dict) else {},
        "etag": str(result.get("etag") or ""),
    }


async def set_conversation_state(
    mailbox: str,
    conversation_id: str,
    state: dict[str, Any],
    *,
    if_match: str | None = None,
) -> dict[str, Any]:
    """保存 P3 会话 scope/evidence 指针；使用 etag 避免并发覆盖。"""
    cid = str(conversation_id or "").strip()
    if not cid:
        return {"state": {}, "etag": ""}
    safe = dict(state or {})
    safe["version"] = 1
    safe["updated_at"] = _now()
    result = await get_storage().set(
        _conversation_state_key(mailbox, cid),
        safe,
        scope=default_scope(),
        if_match=if_match,
    )
    return {"state": safe, "etag": str(result.get("etag") or "")}


def _normalize_inbox_custom_categories(value: Any) -> list[InboxCustomCategory]:
    """规范化 Split 列表，防止非法条目、重复 ID 或超长输入进入邮箱设置。"""
    if not isinstance(value, list):
        return []
    categories: list[InboxCustomCategory] = []
    seen_ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict) or len(categories) >= 20:
            continue
        category_id = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        query = str(raw.get("query") or "").strip()
        # ID、显示名称和查询缺一不可；ID 重复时保留先保存的 Split。
        if not category_id or not name or not query or category_id in seen_ids:
            continue
        seen_ids.add(category_id)
        bundling_behavior = str(raw.get("bundling_behavior") or "default")
        if bundling_behavior not in ("default", "by_sender", "none"):
            bundling_behavior = "default"
        categories.append(InboxCustomCategory(
            id=category_id[:120],
            name=name[:60],
            query=query[:500],
            hide_when_empty=raw.get("hide_when_empty") is True,
            bundling_behavior=bundling_behavior,
        ))
    return categories


async def get_inbox_settings(mailbox: str) -> dict[str, Any]:
    """读取邮箱设置；不存在时返回约定默认值和空 etag。"""
    result = await get_storage().get(_inbox_settings_key(mailbox), scope=default_scope())
    raw = result.get("value") if result.get("exists") and isinstance(result.get("value"), dict) else {}
    allowed = {key: raw[key] for key in InboxSettings.__dataclass_fields__ if key in raw and key != "custom_categories"}
    allowed["custom_categories"] = _normalize_inbox_custom_categories(raw.get("custom_categories"))
    allowed["mailbox"] = mailbox
    return {
        "settings": InboxSettings(**allowed),
        "etag": str(result.get("etag") or ""),
    }


async def set_inbox_settings(
    mailbox: str,
    patch: dict[str, Any],
    *,
    if_match: str | None = None,
) -> dict[str, Any]:
    """合并并保存当前邮箱设置，使用 etag 防止并发覆盖。"""
    current = await get_inbox_settings(mailbox)
    settings = current["settings"]
    values = dict(patch) if isinstance(patch, dict) else {}
    if values.get("display_range_days") in (7, 30, 60):
        settings.display_range_days = int(values["display_range_days"])
    if values.get("time_section_mode") in ("detailed", "recent_then_months", "months_only"):
        settings.time_section_mode = str(values["time_section_mode"])
    # 仅允许 Settings 中暴露的轮询档位，非法值忽略以保留原配置
    if values.get("llm_status_poll_seconds") in (0, 30, 60, 120, 300):
        settings.llm_status_poll_seconds = int(values["llm_status_poll_seconds"])
    # 前台 Gmail History 增量同步频率；低于 15 秒会放大平台 Reverse RPC 压力。
    if values.get("auto_sync_seconds") in (0, 15, 30, 60, 120):
        settings.auto_sync_seconds = int(values["auto_sync_seconds"])
    for field_name in ("stars_enabled", "todos_enabled"):
        if isinstance(values.get(field_name), bool):
            setattr(settings, field_name, values[field_name])
    for field_name in ("stars_limit", "todos_limit"):
        if values.get(field_name) is not None:
            try:
                setattr(settings, field_name, max(1, min(50, int(values[field_name]))))
            except (TypeError, ValueError):
                pass
    if "custom_categories" in values:
        # 分类只随当前 mailbox 的 InboxSettings 保存，沿用同一 etag 并发保护。
        settings.custom_categories = _normalize_inbox_custom_categories(values["custom_categories"])
    settings.updated_at = _now()
    result = await get_storage().set(
        _inbox_settings_key(mailbox),
        _dataclass_to_dict(settings),
        scope=default_scope(),
        if_match=if_match,
    )
    return {"settings": settings, "etag": str(result.get("etag") or "")}


# ── Inbox thread assist / draft ─────────────────────────────────────


def _inbox_thread_assist_key(mailbox: str, thread_id: str, latest_message_id: str) -> str:
    return (
        f"{_mailbox_prefix(mailbox)}/inbox-thread-assist/"
        f"{sanitize_key_part(str(thread_id or 'thread'))}/"
        f"{sanitize_key_part(str(latest_message_id or 'latest'))}"
    )


def _inbox_thread_draft_key(mailbox: str, thread_id: str) -> str:
    return (
        f"{_mailbox_prefix(mailbox)}/inbox-drafts/"
        f"{sanitize_key_part(str(thread_id or 'thread'))}"
    )


async def get_inbox_thread_assist(mailbox: str, thread_id: str, latest_message_id: str) -> dict[str, Any]:
    key = _inbox_thread_assist_key(mailbox, thread_id, latest_message_id)
    result = await get_storage().get(key, scope=default_scope())
    value = result.get("value") if result.get("exists") and isinstance(result.get("value"), dict) else {}
    return {
        "exists": bool(result.get("exists")),
        "etag": str(result.get("etag") or ""),
        "value": value,
    }


async def set_inbox_thread_assist(
    mailbox: str,
    thread_id: str,
    latest_message_id: str,
    payload: dict[str, Any],
    *,
    if_match: str | None = None,
) -> dict[str, Any]:
    key = _inbox_thread_assist_key(mailbox, thread_id, latest_message_id)
    stored = {
        **(payload if isinstance(payload, dict) else {}),
        "thread_id": str(thread_id or ""),
        "latest_message_id": str(latest_message_id or ""),
        "updated_at": _now(),
    }
    return await get_storage().set(key, stored, scope=default_scope(), if_match=if_match)


async def get_inbox_thread_draft(mailbox: str, thread_id: str) -> dict[str, Any]:
    key = _inbox_thread_draft_key(mailbox, thread_id)
    result = await get_storage().get(key, scope=default_scope())
    value = result.get("value") if result.get("exists") and isinstance(result.get("value"), dict) else {}
    return {
        "exists": bool(result.get("exists")),
        "etag": str(result.get("etag") or ""),
        "value": value,
    }


async def set_inbox_thread_draft(
    mailbox: str,
    thread_id: str,
    body: str,
    *,
    body_html: str | None = None,
    if_match: str | None = None,
    message: dict[str, Any] | None = None,
    updated_at: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """持久化线程草稿正文、经净化的富文本 HTML 与外发附件元数据（不含文件字节）。"""
    from mail_agent.mail_providers.gmail.outgoing_attachments import normalize_draft_attachment_meta
    from mail_agent.mail_providers.gmail.outgoing_html import sanitize_outgoing_html

    key = _inbox_thread_draft_key(mailbox, thread_id)
    # 未传 attachments 时保留已有附件列表，避免只改正文时丢失附件引用。
    if attachments is None:
        existing = await get_storage().get(key, scope=default_scope())
        existing_value = existing.get("value") if existing.get("exists") and isinstance(existing.get("value"), dict) else {}
        attachment_meta = normalize_draft_attachment_meta(existing_value.get("attachments"))
    else:
        attachment_meta = normalize_draft_attachment_meta(attachments)
    payload = {
        "thread_id": str(thread_id or ""),
        "body": str(body or ""),
        # 后端再次净化，防止旧客户端或直接 RPC 绕过浏览器侧的 DOMPurify。
        "body_html": sanitize_outgoing_html(body_html),
        "message": message if isinstance(message, dict) else {},
        "attachments": attachment_meta,
        "updated_at": str(updated_at or _now()),
    }
    return await get_storage().set(key, payload, scope=default_scope(), if_match=if_match)


async def delete_inbox_thread_draft(mailbox: str, thread_id: str) -> dict[str, Any]:
    key = _inbox_thread_draft_key(mailbox, thread_id)
    return await get_storage().delete(key, scope=default_scope())


async def list_inbox_thread_drafts(mailbox: str, *, limit: int = 100) -> dict[str, Any]:
    prefix = f"{_mailbox_prefix(mailbox)}/inbox-drafts/"
    result = await get_storage().list(prefix=prefix, limit=max(1, min(int(limit or 100), 500)), scope=default_scope())
    drafts: list[dict[str, Any]] = []
    for item in result.get("items") or []:
        key = item.get("key") if isinstance(item, dict) else item
        if not key:
            continue
        loaded = await get_storage().get(str(key), scope=default_scope())
        value = loaded.get("value") if loaded.get("exists") and isinstance(loaded.get("value"), dict) else {}
        body = str(value.get("body") or "")
        if not body.strip():
            continue
        drafts.append({
            "thread_id": str(value.get("thread_id") or str(key).rsplit("/", 1)[-1]),
            "body": body,
            "body_html": str(value.get("body_html") or ""),
            "message": value.get("message") if isinstance(value.get("message"), dict) else {},
            "updated_at": str(value.get("updated_at") or ""),
            "etag": str(loaded.get("etag") or ""),
        })
    drafts.sort(key=lambda draft: draft.get("updated_at") or "", reverse=True)
    return {"mailbox": mailbox, "count": len(drafts), "drafts": drafts, "updated_at": _now()}


# ── Compose drafts ──────────────────────────────────────────────────

def _compose_draft_key(mailbox: str, draft_id: str) -> str:
    return f"{_mailbox_prefix(mailbox)}/compose-drafts/{sanitize_key_part(draft_id)}"


async def get_compose_draft(mailbox: str, draft_id: str) -> dict[str, Any]:
    result = await get_storage().get(_compose_draft_key(mailbox, draft_id), scope=default_scope())
    value = result.get("value") if result.get("exists") and isinstance(result.get("value"), dict) else {}
    return {"exists": bool(result.get("exists")), "etag": str(result.get("etag") or ""), "draft": value}


async def set_compose_draft(
    mailbox: str,
    draft: dict[str, Any],
    *,
    if_match: str | None = None,
) -> dict[str, Any]:
    draft_id = str(draft.get("id") or uuid.uuid4().hex).strip()
    key = _compose_draft_key(mailbox, draft_id)
    existing = await get_storage().get(key, scope=default_scope())
    if if_match and existing.get("exists") and str(existing.get("etag") or "") != if_match:
        raise ValueError("Compose draft was changed elsewhere. Refresh and try again.")
    def _addr_list(key: str) -> list[str]:
        # 统一清洗 To / Cc / Bcc，最多各保留 100 个地址
        raw = draft.get(key) or []
        if isinstance(raw, str):
            items = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
        elif isinstance(raw, list):
            items = [str(item).strip() for item in raw if str(item).strip()]
        else:
            items = []
        seen: set[str] = set()
        result: list[str] = []
        for item in items:
            email = item.lower()
            if email in seen:
                continue
            seen.add(email)
            result.append(email)
            if len(result) >= 100:
                break
        return result

    # Compose Draft 既支持普通新邮件，也支持从线程详情保存的 Forward 草稿；
    # 路由元数据必须和正文一起持久化，前端才能在 Drafts 中回到原线程。
    # 外发附件只存元数据 + storage_key，字节在本地 stage 目录。
    from mail_agent.mail_providers.gmail.outgoing_attachments import normalize_draft_attachment_meta

    if "attachments" in draft:
        attachment_meta = normalize_draft_attachment_meta(draft.get("attachments"))
    else:
        existing_value = existing.get("value") if existing.get("exists") and isinstance(existing.get("value"), dict) else {}
        attachment_meta = normalize_draft_attachment_meta(existing_value.get("attachments"))
    payload = {
        "id": draft_id,
        "mailbox": str(mailbox or "").strip().lower(),
        "draft_mode": "forward" if str(draft.get("draft_mode") or "").strip().lower() == "forward" else "compose",
        "source_thread_id": str(draft.get("source_thread_id") or "").strip(),
        "source_message_id": str(draft.get("source_message_id") or "").strip(),
        "recipients": _addr_list("recipients"),
        "cc": _addr_list("cc"),
        "bcc": _addr_list("bcc"),
        "subject": str(draft.get("subject") or "")[:998],
        "body": str(draft.get("body") or ""),
        "body_html": str(draft.get("body_html") or ""),
        "attachments": attachment_meta,
        "created_at": str(draft.get("created_at") or (existing.get("value") or {}).get("created_at") or _now()),
        "updated_at": _now(),
    }
    result = await get_storage().set(key, payload, scope=default_scope(), if_match=str(existing.get("etag") or "") or None)
    return {"ok": True, "etag": str(result.get("etag") or ""), "draft": payload}


async def delete_compose_draft(mailbox: str, draft_id: str) -> dict[str, Any]:
    return await get_storage().delete(_compose_draft_key(mailbox, draft_id), scope=default_scope())


async def list_compose_drafts(mailbox: str, *, limit: int = 100) -> dict[str, Any]:
    prefix = f"{_mailbox_prefix(mailbox)}/compose-drafts/"
    result = await get_storage().list(prefix=prefix, limit=max(1, min(int(limit or 100), 500)), scope=default_scope())
    drafts: list[dict[str, Any]] = []
    for item in result.get("items") or []:
        key = item.get("key") if isinstance(item, dict) else item
        if not key:
            continue
        loaded = await get_storage().get(str(key), scope=default_scope())
        value = loaded.get("value") if loaded.get("exists") and isinstance(loaded.get("value"), dict) else {}
        if not value:
            continue
        drafts.append({**value, "etag": str(loaded.get("etag") or "")})
    drafts.sort(key=lambda draft: str(draft.get("updated_at") or ""), reverse=True)
    return {"mailbox": mailbox, "count": len(drafts), "drafts": drafts}


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


# ── Active cards (sharded) ───────────────────────────────────────────

ACTIVE_CARDS_SHARD_SIZE = 50


def _cards_index_key(mailbox: str) -> str:
    return f"{_mailbox_prefix(mailbox)}/cards/active_index"


def _cards_shard_key(mailbox: str, shard_index: int) -> str:
    return f"{_mailbox_prefix(mailbox)}/cards/active_{shard_index:04d}"


def _cards_legacy_key(mailbox: str) -> str:
    return f"{_mailbox_prefix(mailbox)}/cards/active"


async def _migrate_legacy_cards(mailbox: str) -> bool:
    """One-shot migration: read old single-file cards, write sharded format."""
    legacy = await get_storage().get(_cards_legacy_key(mailbox), scope=default_scope())
    if not legacy.get("exists") or not legacy.get("value"):
        return False
    raw = legacy["value"]
    cards = [_dict_to_persistent_card(c) for c in raw.get("cards", [])]
    cards.sort(key=lambda c: (_priority_rank(c.priority), c.created_at or ""), reverse=True)
    active = ActiveCards(cards=cards, updated_at=raw.get("updated_at", "") or _now())
    await _set_active_cards_sharded(mailbox, active)
    await get_storage().delete(_cards_legacy_key(mailbox), scope=default_scope())
    return True


async def _set_active_cards_sharded(mailbox: str, cards: ActiveCards) -> dict:
    """Write cards in shards + index."""
    cards.updated_at = _now()
    total = len(cards.cards)
    shard_count = max(1, (total + ACTIVE_CARDS_SHARD_SIZE - 1) // ACTIVE_CARDS_SHARD_SIZE)
    storage = get_storage()
    for i in range(shard_count):
        shard_cards = cards.cards[i * ACTIVE_CARDS_SHARD_SIZE:(i + 1) * ACTIVE_CARDS_SHARD_SIZE]
        await storage.set(
            _cards_shard_key(mailbox, i),
            _dataclass_to_dict(ActiveCards(cards=shard_cards, updated_at=cards.updated_at)),
            scope=default_scope(),
        )
    # Delete stale shards beyond current count
    idx_result = await storage.get(_cards_index_key(mailbox), scope=default_scope())
    old_count = 0
    if idx_result.get("exists") and isinstance(idx_result.get("value"), dict):
        old_count = int(idx_result["value"].get("shards", 0))
    for i in range(shard_count, old_count):
        try:
            await storage.delete(_cards_shard_key(mailbox, i), scope=default_scope())
        except Exception:
            pass
    await storage.set(
        _cards_index_key(mailbox),
        {"shards": shard_count, "total_cards": total, "updated_at": cards.updated_at},
        scope=default_scope(),
    )
    await update_mailbox_registry_fields(mailbox, card_count=total)
    return {"ok": True, "shards": shard_count, "total_cards": total}


async def get_active_cards(mailbox: str) -> ActiveCards:
    result = await get_storage().get(_cards_index_key(mailbox), scope=default_scope())
    if not result.get("exists") or not isinstance(result.get("value"), dict):
        # Try legacy migration
        if await _migrate_legacy_cards(mailbox):
            return await get_active_cards(mailbox)
        return ActiveCards()
    idx = result["value"]
    shard_count = int(idx.get("shards", 0))
    updated_at = str(idx.get("updated_at", ""))
    all_cards: list[PersistentCard] = []
    for i in range(shard_count):
        shard_result = await get_storage().get(_cards_shard_key(mailbox, i), scope=default_scope())
        if shard_result.get("exists") and shard_result.get("value"):
            raw = shard_result["value"]
            for c in raw.get("cards", []):
                all_cards.append(_dict_to_persistent_card(c))
    all_cards.sort(key=lambda c: (_priority_rank(c.priority), c.created_at or ""), reverse=True)
    return ActiveCards(cards=all_cards, updated_at=updated_at)


async def get_active_cards_page(mailbox: str, offset: int = 0, limit: int = 50) -> dict:
    """Return one page of active cards directly from a single shard when possible."""
    result = await get_storage().get(_cards_index_key(mailbox), scope=default_scope())
    if not result.get("exists") or not isinstance(result.get("value"), dict):
        if await _migrate_legacy_cards(mailbox):
            return await get_active_cards_page(mailbox, offset, limit)
        return {"active": ActiveCards(), "total": 0, "has_more": False}
    idx = result["value"]
    total = int(idx.get("total_cards", 0))
    shard_count = int(idx.get("shards", 0))
    updated_at = str(idx.get("updated_at", ""))
    page_cards: list[PersistentCard] = []
    # Each non-last shard has exactly ACTIVE_CARDS_SHARD_SIZE cards
    global_offset = 0
    for i in range(shard_count):
        shard_size = ACTIVE_CARDS_SHARD_SIZE if i < shard_count - 1 else total - (shard_count - 1) * ACTIVE_CARDS_SHARD_SIZE
        shard_start = global_offset
        shard_end = global_offset + shard_size
        global_offset = shard_end
        # Does this shard overlap with [offset, offset+limit)?
        if shard_end <= offset or shard_start >= offset + limit:
            continue
        shard_result = await get_storage().get(_cards_shard_key(mailbox, i), scope=default_scope())
        if shard_result.get("exists") and shard_result.get("value"):
            raw_cards = shard_result["value"].get("cards", [])
            for j, c in enumerate(raw_cards):
                card_pos = shard_start + j
                if offset <= card_pos < offset + limit:
                    page_cards.append(_dict_to_persistent_card(c))
                elif card_pos >= offset + limit:
                    break
        if global_offset >= offset + limit:
            break
    page_active = ActiveCards(cards=page_cards, updated_at=updated_at)
    return {
        "active": page_active,
        "total": total,
        "has_more": (offset + limit) < total,
    }


async def set_active_cards(mailbox: str, cards: ActiveCards) -> dict:
    return await _set_active_cards_sharded(mailbox, cards)


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


# ── Cleanup bundle (sharded, separate from active cards) ────────────

CLEANUP_SHARD_SIZE = 50


def _cleanup_index_key(mailbox: str) -> str:
    return f"{_mailbox_prefix(mailbox)}/cards/cleanup_index"


def _cleanup_shard_key(mailbox: str, shard_index: int) -> str:
    return f"{_mailbox_prefix(mailbox)}/cards/cleanup_{shard_index:04d}"


def _cleanup_legacy_key(mailbox: str) -> str:
    return f"{_mailbox_prefix(mailbox)}/cards/cleanup_bundle"


async def get_cleanup_bundle(mailbox: str) -> list[dict[str, Any]]:
    result = await get_storage().get(_cleanup_index_key(mailbox), scope=default_scope())
    messages: list[dict[str, Any]] = []
    if not result.get("exists") or not isinstance(result.get("value"), dict):
        # Try legacy single-file migration
        legacy = await get_storage().get(_cleanup_legacy_key(mailbox), scope=default_scope())
        if legacy.get("exists") and isinstance(legacy.get("value"), list):
            messages = legacy["value"]
            await set_cleanup_bundle(mailbox, messages)
            await get_storage().delete(_cleanup_legacy_key(mailbox), scope=default_scope())
        else:
            return []
    else:
        idx = result["value"]
        for i in range(int(idx.get("shards", 0))):
            shard = await get_storage().get(_cleanup_shard_key(mailbox, i), scope=default_scope())
            if shard.get("exists") and isinstance(shard.get("value"), list):
                messages.extend(shard["value"])
    # Ensure every item has a mailbox field (backfill for old data)
    for item in messages:
        if "mailbox" not in item:
            item["mailbox"] = mailbox
    return messages


async def get_cleanup_bundle_page(mailbox: str, offset: int = 0, limit: int = 100) -> dict:
    """Return one page of cleanup messages, reading only the needed shards."""
    result = await get_storage().get(_cleanup_index_key(mailbox), scope=default_scope())
    if not result.get("exists") or not isinstance(result.get("value"), dict):
        legacy = await get_storage().get(_cleanup_legacy_key(mailbox), scope=default_scope())
        if legacy.get("exists") and isinstance(legacy.get("value"), list):
            await set_cleanup_bundle(mailbox, legacy["value"])
            await get_storage().delete(_cleanup_legacy_key(mailbox), scope=default_scope())
            return await get_cleanup_bundle_page(mailbox, offset, limit)
        return {"items": [], "total": 0, "has_more": False}
    idx = result["value"]
    total = int(idx.get("total", 0))
    shard_count = int(idx.get("shards", 0))
    page: list[dict[str, Any]] = []
    global_offset = 0
    for i in range(shard_count):
        shard_size = CLEANUP_SHARD_SIZE if i < shard_count - 1 else total - (shard_count - 1) * CLEANUP_SHARD_SIZE
        shard_start = global_offset
        shard_end = global_offset + shard_size
        global_offset = shard_end
        if shard_end <= offset or shard_start >= offset + limit:
            continue
        shard = await get_storage().get(_cleanup_shard_key(mailbox, i), scope=default_scope())
        if shard.get("exists") and isinstance(shard.get("value"), list):
            for j, item in enumerate(shard["value"]):
                pos = shard_start + j
                if offset <= pos < offset + limit:
                    if "mailbox" not in item:
                        item["mailbox"] = mailbox
                    page.append(item)
                elif pos >= offset + limit:
                    break
        if global_offset >= offset + limit:
            break
    return {"items": page, "total": total, "has_more": (offset + limit) < total}


async def set_cleanup_bundle(mailbox: str, messages: list[dict[str, Any]], *, preserve_existing: bool = True) -> dict:
    """Persist a cleanup bundle, merging with any existing bundle by message_id.

    New items overwrite old items with the same message_id; old items not present
    in the new list are preserved.  This prevents incremental scans from losing
    cleanup cards accumulated across previous scans.
    """
    merged = list(messages)
    if preserve_existing:
        existing = await get_cleanup_bundle(mailbox)
        seen: set[str] = {str(m.get("message_id", "")) for m in messages if m.get("message_id")}
        for old in existing:
            mid = str(old.get("message_id", ""))
            if mid and mid not in seen:
                merged.append(old)
                seen.add(mid)

    total = len(merged)
    shard_count = max(1, (total + CLEANUP_SHARD_SIZE - 1) // CLEANUP_SHARD_SIZE) if total > 0 else 0
    storage = get_storage()
    if shard_count == 0:
        await storage.set(_cleanup_index_key(mailbox), {"shards": 0, "total": 0, "updated_at": _now()}, scope=default_scope())
        return {"ok": True, "shards": 0, "total": 0}
    for i in range(shard_count):
        s = merged[i * CLEANUP_SHARD_SIZE:(i + 1) * CLEANUP_SHARD_SIZE]
        await storage.set(_cleanup_shard_key(mailbox, i), s, scope=default_scope())
    # Delete stale shards
    idx_result = await storage.get(_cleanup_index_key(mailbox), scope=default_scope())
    old_count = 0
    if idx_result.get("exists") and isinstance(idx_result.get("value"), dict):
        old_count = int(idx_result["value"].get("shards", 0))
    for i in range(shard_count, old_count):
        try:
            await storage.delete(_cleanup_shard_key(mailbox, i), scope=default_scope())
        except Exception:
            pass
    await storage.set(
        _cleanup_index_key(mailbox),
        {"shards": shard_count, "total": total, "updated_at": _now()},
        scope=default_scope(),
    )
    return {"ok": True, "shards": shard_count, "total": total}


async def remove_cleanup_messages(mailbox: str, message_ids: list[str]) -> dict:
    """Remove messages from the persisted cleanup bundle after they are read."""
    remove_ids = {str(mid).strip() for mid in message_ids if str(mid).strip()}
    if not remove_ids:
        return {"ok": True, "removed": 0, "total": None}

    existing = await get_cleanup_bundle(mailbox)
    kept = [item for item in existing if str(item.get("message_id") or item.get("id") or "") not in remove_ids]
    removed = len(existing) - len(kept)
    result = await set_cleanup_bundle(mailbox, kept, preserve_existing=False)
    return {"ok": True, "removed": removed, "total": result.get("total")}


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
    entry_data = _dataclass_to_dict(entry)
    if entry.run_id:
        entries = [e for e in entries if not (isinstance(e, dict) and e.get("run_id") == entry.run_id)]
    entries.insert(0, entry_data)
    # Keep last 50
    if len(entries) > 50:
        entries = entries[:50]
    raw["entries"] = entries
    return await get_storage().set(RUN_HISTORY_KEY, raw, scope=default_scope())


async def clear_run_history() -> dict:
    """Delete all run history entries."""
    return await get_storage().set(RUN_HISTORY_KEY, {"entries": []}, scope=default_scope())


async def reset_mailbox_scan_history(mailbox: str) -> dict:
    """Clear Brief scan history for one mailbox while keeping auth/cache/plan.

    This removes active cards, cleanup bundle, processed-message markers, scan
    state, per-run records, and cross-mailbox run history entries for the
    mailbox. It intentionally keeps Gmail cache, contact memory, scan plan, and
    the mailbox registry entry.
    """
    storage = get_storage()
    mbox = _normalize_email(mailbox)
    prefix = _mailbox_prefix(mbox)
    counts: dict[str, int] = {"keys": 0, "history_entries": 0}

    async def delete_key(key: str) -> None:
        try:
            await storage.delete(key, scope=default_scope())
            counts["keys"] += 1
        except Exception:
            pass

    async def delete_prefix(prefix_key: str) -> None:
        cursor: str | None = None
        while True:
            try:
                result = await storage.list(prefix=prefix_key, cursor=cursor, limit=200, scope=default_scope())
            except TypeError:
                result = await storage.list(prefix_key, scope=default_scope())
            except Exception:
                return
            items = result.get("items") or []
            for item in items:
                key = item.get("key", "") if isinstance(item, dict) else ""
                if key:
                    await delete_key(key)
            cursor = result.get("cursor") or result.get("next_cursor")
            if not cursor or not items:
                break

    # Active card shards and legacy card key.
    try:
        idx = await storage.get(_cards_index_key(mbox), scope=default_scope())
        if idx.get("exists") and isinstance(idx.get("value"), dict):
            for i in range(int(idx["value"].get("shards", 0))):
                await delete_key(_cards_shard_key(mbox, i))
    except Exception:
        pass
    await delete_key(_cards_index_key(mbox))
    await delete_key(_cards_legacy_key(mbox))

    # Cleanup bundle shards and legacy bundle key.
    try:
        idx = await storage.get(_cleanup_index_key(mbox), scope=default_scope())
        if idx.get("exists") and isinstance(idx.get("value"), dict):
            for i in range(int(idx["value"].get("shards", 0))):
                await delete_key(_cleanup_shard_key(mbox, i))
    except Exception:
        pass
    await delete_key(_cleanup_index_key(mbox))
    await delete_key(_cleanup_legacy_key(mbox))

    await delete_key(f"{prefix}/scan_state")
    await delete_prefix(f"{prefix}/processed/")
    await delete_prefix(f"{prefix}/run/")

    try:
        hist_result = await storage.get(RUN_HISTORY_KEY, scope=default_scope())
        if hist_result.get("exists") and isinstance(hist_result.get("value"), dict):
            raw: dict = hist_result["value"]
            entries: list = raw.get("entries", [])
            before = len(entries)
            entries = [e for e in entries if not (isinstance(e, dict) and _normalize_email(str(e.get("mailbox", ""))) == mbox)]
            counts["history_entries"] = before - len(entries)
            raw["entries"] = entries
            await storage.set(RUN_HISTORY_KEY, raw, scope=default_scope())
    except Exception:
        pass

    await update_mailbox_registry_fields(mbox, card_count=0, last_scan_at="", last_scan_status="", last_error="")
    return {"ok": True, "mailbox": mbox, "deleted": counts}


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
        # Also clear the separate cleanup bundle if applicable
        if category in ("all", "cleanup"):
            try:
                # Delete cleanup shards + index + legacy key
                idx = await get_storage().get(_cleanup_index_key(mailbox), scope=default_scope())
                if idx.get("exists") and isinstance(idx.get("value"), dict):
                    for i in range(int(idx["value"].get("shards", 0))):
                        try:
                            await get_storage().delete(_cleanup_shard_key(mailbox, i), scope=default_scope())
                        except Exception:
                            pass
                await get_storage().delete(_cleanup_index_key(mailbox), scope=default_scope())
                await get_storage().delete(_cleanup_legacy_key(mailbox), scope=default_scope())
            except Exception:
                pass
    return removed


async def delete_mailbox_data(mailbox: str) -> dict:
    """Delete all persistent data for a single mailbox.

    Nukes every key under the mailbox prefix (cards, processed, scan state,
    run records, contact memories, etc.), plus Gmail cache, run-history
    entries for this mailbox, and the registry entry.

    Returns counts of deleted items.
    """
    storage = get_storage()
    mbox = _normalize_email(mailbox)
    prefix = _mailbox_prefix(mbox)
    counts: dict[str, int] = {"keys": 0, "cache_keys": 0, "history_entries": 0}

    # 1. Nuke all per-mailbox keys (covers cards, processed, scan state, scan plan, run records, contacts)
    try:
        result = await storage.list(f"{prefix}/", scope=default_scope())
        for item in result.get("items", []):
            try:
                await storage.delete(item.get("key", ""), scope=default_scope())
                counts["keys"] += 1
            except Exception:
                pass
    except Exception:
        pass

    # 2. Also delete the mailbox prefix key itself (some storages keep it as a directory marker)
    try:
        await storage.delete(prefix, scope=default_scope())
    except Exception:
        pass

    # 3. Nuke Gmail cache — APS keys
    from ..mail_providers.gmail.adapter import _storage_cache_prefix
    cache_prefix = _storage_cache_prefix(mbox)
    try:
        cache_result = await storage.list(f"{cache_prefix}/", scope=default_scope())
        for item in cache_result.get("items", []):
            try:
                await storage.delete(item.get("key", ""), scope=default_scope())
                counts["cache_keys"] += 1
            except Exception:
                pass
        await storage.delete(cache_prefix, scope=default_scope())
    except Exception:
        pass

    # 4. Nuke Gmail cache — local filesystem
    try:
        from ..mail_providers.gmail.adapter import _mailbox_cache_dir
        import shutil
        local_cache = _mailbox_cache_dir(mbox)
        if local_cache.exists():
            shutil.rmtree(str(local_cache))
    except Exception:
        pass

    # 5. Nuke local mailbox data directory (Files storage)
    try:
        from anna_inbox_executa.common import data_root as _common_data_root
        import shutil as _shutil
        local_mbox_dir = _common_data_root() / "anna-inbox" / "mailbox" / _sanitize(mbox)
        if local_mbox_dir.exists():
            _shutil.rmtree(str(local_mbox_dir))
    except Exception:
        pass

    # 6. Filter run-history entries for this mailbox (cross-mailbox key)
    try:
        hist_result = await storage.get(RUN_HISTORY_KEY, scope=default_scope())
        if hist_result.get("exists") and isinstance(hist_result.get("value"), dict):
            raw: dict = hist_result["value"]
            entries: list = raw.get("entries", [])
            before = len(entries)
            entries = [e for e in entries if not (isinstance(e, dict) and _normalize_email(str(e.get("mailbox", ""))) == mbox)]
            counts["history_entries"] = before - len(entries)
            raw["entries"] = entries
            await storage.set(RUN_HISTORY_KEY, raw, scope=default_scope())
    except Exception:
        pass

    # 7. Remove from mailbox registry
    try:
        await remove_mailbox_from_registry(mbox)
    except Exception:
        pass

    return {"ok": True, "mailbox": mbox, "deleted": counts}


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
        for sub in ("cards/active", "cards/active_index", "cards/cleanup_bundle", "cards/cleanup_index", "scan_state", "scan_plan", "processed"):
            try:
                await storage.delete(f"{prefix}/{sub}", scope=default_scope())
            except Exception:
                pass
        # Delete all card shards for this mailbox
        try:
            idx_result = await storage.get(f"{prefix}/cards/active_index", scope=default_scope())
            if idx_result.get("exists") and isinstance(idx_result.get("value"), dict):
                shard_count = int(idx_result["value"].get("shards", 0))
                for i in range(shard_count):
                    try:
                        await storage.delete(f"{prefix}/cards/active_{i:04d}", scope=default_scope())
                    except Exception:
                        pass
        except Exception:
            pass
        # Delete all cleanup shards for this mailbox
        try:
            cleanup_idx = await storage.get(f"{prefix}/cards/cleanup_index", scope=default_scope())
            if cleanup_idx.get("exists") and isinstance(cleanup_idx.get("value"), dict):
                for i in range(int(cleanup_idx["value"].get("shards", 0))):
                    try:
                        await storage.delete(f"{prefix}/cards/cleanup_{i:04d}", scope=default_scope())
                    except Exception:
                        pass
            await storage.delete(f"{prefix}/cards/cleanup_index", scope=default_scope())
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
        bundled_count=d.get("bundled_count", 0),
        user_action=d.get("user_action", ""),
        reply_gaps=d.get("reply_gaps", {}) if isinstance(d.get("reply_gaps"), dict) else {},
        gmail_state=d.get("gmail_state", {}) if isinstance(d.get("gmail_state"), dict) else {},
        attachments=d.get("attachments", []) if isinstance(d.get("attachments"), list) else [],
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
