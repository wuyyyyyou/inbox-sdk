from __future__ import annotations

from anna_inbox_executa.common import *

def _registry_to_frontend(registry: Any) -> list[dict[str, Any]]:
    return [
        {
            "email": entry.email,
            "avatar_url": entry.avatar_url,
            "provider": entry.provider,
            "auth_source": entry.auth_source,
            "authorized": entry.authorized,
            "selected": entry.selected,
            "last_auth_checked_at": entry.last_auth_checked_at,
            "last_scan_at": entry.last_scan_at,
            "last_scan_status": entry.last_scan_status,
            "last_error": entry.last_error,
            "card_count": entry.card_count,
        }
        for entry in getattr(registry, "mailboxes", [])
    ]


async def _merge_multi_tokens_seed(seed_tokens: list[dict[str, Any]]) -> None:
    """按当前 credential 快照同步 APS，同时保留同一 refresh token 的刷新结果。"""
    from mail_agent.storage.ops import get_multi_tokens, set_multi_tokens

    existing = await get_multi_tokens()
    existing_by_email: dict[str, dict[str, Any]] = {
        str(e.get("email", "")).strip().lower(): e for e in existing if e.get("email")
    }
    by_email: dict[str, dict[str, Any]] = {}
    for seed in seed_tokens:
        email = str(seed.get("email") or "").strip().lower()
        if not email or "@" not in email:
            continue
        current = existing_by_email.get(email)
        record = dict(seed)
        if current and seed.get("refresh_token") and seed.get("refresh_token") == current.get("refresh_token"):
            # APS may hold an access token refreshed after the credential JSON
            # was saved. Keep that volatile pair while accepting seed metadata.
            for key in ("access_token", "expires_at"):
                if current.get(key):
                    record[key] = current[key]
        by_email[email] = record

    await set_multi_tokens(list(by_email.values()))


async def _get_all_multi_tokens() -> list[dict[str, Any]]:
    from mail_agent.storage.ops import get_multi_tokens
    return await get_multi_tokens()


def _discover_mailboxes() -> list[dict[str, Any]]:
    from mail_agent.mail_providers.gmail.adapter import get_authorized_email, list_available_mailboxes_from_tokens, get_multi_token_emails, get_multi_token_map, get_account_avatar_url

    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1. 平台单 token — 最高优先级，auth_source="platform"
    email = get_authorized_email().strip().lower()
    if email and email not in seen:
        seen.add(email)
        results.append({
            "email": email, "provider": "gmail",
            "auth_source": "platform", "authorized": True,
            "avatar_url": get_account_avatar_url(email),
            "last_auth_checked_at": beijing_now(),
        })

    # 2. 多 token 邮箱 — auth_source="platform_multi"，去重跳过 platform 已覆盖的
    multi_token_map = get_multi_token_map()
    for multi_email in get_multi_token_emails():
        if multi_email not in seen:
            seen.add(multi_email)
            results.append({
                "email": multi_email, "provider": "gmail",
                "auth_source": "platform_multi", "authorized": True,
                "avatar_url": str(multi_token_map.get(multi_email, {}).get("avatar_url") or multi_token_map.get(multi_email, {}).get("picture") or ""),
                "last_auth_checked_at": beijing_now(),
            })

    # 3. 本地 dev token 文件兜底
    for local in list_available_mailboxes_from_tokens():
        local_email = str(local.get("email", "")).strip().lower()
        if local_email and local_email not in seen:
            seen.add(local_email)
            local["avatar_url"] = str(local.get("avatar_url") or get_account_avatar_url(local_email) or "")
            results.append(local)

    return results


def _sync_list_mailboxes() -> dict[str, Any]:
    from mail_agent.storage.ops import merge_discovered_mailboxes

    discovered = _discover_mailboxes()
    registry = _run_storage_query(merge_discovered_mailboxes(discovered))
    mailboxes = _registry_to_frontend(registry)
    return {
        "mailboxes": mailboxes,
        "selected": [item["email"] for item in mailboxes if item.get("selected")],
        "discovered": discovered,
    }


def _sync_get_mailbox_registry() -> dict[str, Any]:
    from mail_agent.storage.ops import get_mailbox_registry

    registry = _run_storage_query(get_mailbox_registry())
    mailboxes = _registry_to_frontend(registry)
    return {
        "mailboxes": mailboxes,
        "selected": [item["email"] for item in mailboxes if item.get("selected")],
        "discovered": [],
    }


def _sync_set_mailbox_selected(arguments: dict[str, Any]) -> dict[str, Any]:
    mailbox = str(arguments.get("mailbox", "")).strip().lower()
    if not mailbox:
        return {"error": "mailbox is required"}
    selected = arguments.get("selected", True)
    if not isinstance(selected, bool):
        selected = str(selected).lower() in ("1", "true", "yes", "on")
    from mail_agent.storage.ops import set_mailbox_selected

    registry = _run_storage_query(set_mailbox_selected(mailbox, selected))
    mailboxes = _registry_to_frontend(registry)
    return {"ok": True, "mailboxes": mailboxes, "selected": [item["email"] for item in mailboxes if item.get("selected")]}


def _sync_remove_mailbox(arguments: dict[str, Any]) -> dict[str, Any]:
    mailbox = str(arguments.get("mailbox", "")).strip().lower()
    if not mailbox:
        return {"error": "mailbox is required"}
    from mail_agent.storage.ops import remove_mailbox_from_registry

    registry = _run_storage_query(remove_mailbox_from_registry(mailbox))
    mailboxes = _registry_to_frontend(registry)
    return {"ok": True, "mailboxes": mailboxes, "selected": [item["email"] for item in mailboxes if item.get("selected")]}

__all__ = [name for name in globals() if not name.startswith("__")]
