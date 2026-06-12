from __future__ import annotations

from anna_inbox_executa.common import *

async def _handle_mark_cleanup_read(arguments: dict[str, Any]) -> dict[str, Any]:
    """Mark cleanup-bundle messages as read in Gmail and update the card in storage."""
    mailbox = str(arguments.get("mailbox", "")).strip()
    card_id = str(arguments.get("card_id", "")).strip()
    raw_ids = arguments.get("message_ids") or []
    message_ids = [str(mid).strip() for mid in raw_ids if str(mid).strip()] if isinstance(raw_ids, list) else []
    if not mailbox or not card_id or not message_ids:
        return {"error": "mailbox, card_id, and message_ids (non-empty array) are required"}

    # 1. Gmail batchModify: remove UNREAD label
    gmail_result = None
    gmail_error = ""
    try:
        import json as _json2
        import urllib.request as _ur
        from mail_agent.mail_providers.gmail.adapter import get_access_token
        token = get_access_token(mailbox)
        body = _json2.dumps({
            "ids": message_ids,
            "removeLabelIds": ["UNREAD"],
        }).encode("utf-8")
        req = _ur.Request(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with _ur.urlopen(req, timeout=30) as resp:
            raw_body = resp.read().decode("utf-8")
            gmail_result = _json2.loads(raw_body) if raw_body.strip() else {"ok": True}
    except Exception as exc:
        gmail_error = str(exc)

    # 2. Keep card visible (pending) — read state is tracked frontend-side
    # Write history
    card_title = card_id
    try:
        from mail_agent.storage.ops import get_active_cards as _cards_for_title
        cards_obj = await _cards_for_title(mailbox)
        card_obj = next((c for c in cards_obj.cards if c.card_id == card_id), None)
        card_title = card_obj.title if card_obj else card_id
    except Exception:
        pass
    from mail_agent.storage.ops import append_card_action
    await append_card_action(mailbox, card_id, card_title, "cleanup_read", f"{len(message_ids)} emails")

    return {
        "ok": gmail_error == "",
        "marked_count": len(message_ids),
        "gmail_result": gmail_result,
        "gmail_error": gmail_error,
    }



async def run_aps_storage_smoke(arguments: dict[str, Any]) -> dict[str, Any]:
    """只验证 APS KV 的最小读写链路，不触碰业务邮箱数据。"""
    from mail_agent.storage.client import get_storage, scope as default_scope

    storage = get_storage()
    suffix = str(arguments.get("key_suffix") or uuid.uuid4().hex[:8]).strip()
    key = app_key(f"debug/aps_smoke/{suffix}")
    value = {
        "value": str(arguments.get("value") or "hello aps"),
        "ts": beijing_now(),
    }
    storage_scope = default_scope()

    set_result = await storage.set(key, value, scope=storage_scope)
    get_result = await storage.get(key, scope=storage_scope)
    list_result = await storage.list(prefix=app_key("debug/aps_smoke/"), limit=20, scope=storage_scope)
    list_all_result = await storage.list(limit=20, scope=storage_scope)
    delete_result = await storage.delete(key, scope=storage_scope)
    after_delete = await storage.get(key, scope=storage_scope)

    return {
        "success": bool(get_result.get("exists")) and get_result.get("value") == value,
        "backend": "aps" if _should_use_aps_storage() else "local-json",
        "scope": storage_scope,
        "key": key,
        "set": set_result,
        "get": get_result,
        "list": list_result,
        "list_all": list_all_result,
        "list_count": len(list_result.get("items") or []),
        "delete": delete_result,
        "exists_after_delete": bool(after_delete.get("exists")),
    }

__all__ = [name for name in globals() if not name.startswith("__")]
