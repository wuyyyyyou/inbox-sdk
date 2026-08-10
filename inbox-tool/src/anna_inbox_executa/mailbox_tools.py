from __future__ import annotations

from anna_inbox_executa.common import *

def _registry_to_frontend(registry: Any) -> list[dict[str, Any]]:
    return [
        {
            "email": entry.email,
            "display_name": getattr(entry, "display_name", ""),
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
    from mail_agent.mail_providers.gmail.adapter import (
        get_account_avatar_url,
        get_account_display_name,
        get_authorized_email,
        get_multi_token_emails,
        get_multi_token_map,
        get_platform_accounts,
        list_available_mailboxes_from_tokens,
    )

    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1. Anna Credentials API is the platform multi-account source of truth.
    # It returns metadata only; Gmail tokens are fetched per account on demand.
    refresh_platform_google_accounts()
    platform_accounts = get_platform_accounts()
    for account in platform_accounts:
        email = str(account.get("email") or "").strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        status = str(account.get("status") or "active").lower()
        authorized = status in {"", "active", "connected"}
        # 平台账号列表通常只提供邮箱和 label，不保证 label 是 Google 账户真实姓名，
        # 也不会直接携带头像地址。通过当前账号的短期 token 查询 Google profile，
        # 让平台运行时与本地 OAuth token 的账户展示保持一致；查询失败时保留 label，
        # 避免 profile scope 或网络问题影响邮箱账号发现本身。
        platform_label = str(account.get("label") or "").strip()
        display_name = get_account_display_name(email) or platform_label
        avatar_url = get_account_avatar_url(email)
        results.append({
            "email": email,
            "provider": "gmail",
            "auth_source": "platform_credentials",
            "authorized": authorized,
            "display_name": display_name,
            "avatar_url": avatar_url,
            "last_auth_checked_at": beijing_now(),
        })

    # 2. Compatibility fallback for legacy single-account injection.
    # 已明确收到平台“未授予 Connected accounts”错误时，默认 token
    # 只能代表一个账户，不能再伪装成多账户发现成功；其它旧 runtime 仍保留兼容。
    credentials_status = get_platform_credentials_status()
    if not results and credentials_status.get("code") != "not_granted":
        email = get_authorized_email().strip().lower()
        if email and email not in seen:
            seen.add(email)
            results.append({
                "email": email, "provider": "gmail",
                "auth_source": "platform", "authorized": True,
                "display_name": get_account_display_name(email),
                "avatar_url": get_account_avatar_url(email),
                "last_auth_checked_at": beijing_now(),
            })

    # 3. Legacy injected multi-token data, retained only for local migration.
    # 3. Legacy injected multi-token data is only a local migration fallback.
    # 一旦平台已返回 Connected accounts，平台快照就是唯一来源；否则旧 token
    # 会把平台刚删除的邮箱重新发现出来，导致其缓存永远无法自动清理。
    if not platform_accounts:
        multi_token_map = get_multi_token_map()
        for multi_email in get_multi_token_emails():
            if multi_email not in seen:
                seen.add(multi_email)
                results.append({
                    "email": multi_email, "provider": "gmail",
                    "auth_source": "platform_multi", "authorized": True,
                    "display_name": str(multi_token_map.get(multi_email, {}).get("display_name") or multi_token_map.get(multi_email, {}).get("name") or get_account_display_name(multi_email) or ""),
                    "avatar_url": str(multi_token_map.get(multi_email, {}).get("avatar_url") or multi_token_map.get(multi_email, {}).get("picture") or ""),
                    "last_auth_checked_at": beijing_now(),
                })

    # 4. 本地 dev token 文件兜底。平台已有账号快照时不能混入本地旧 token，
    # 否则被平台删除的邮箱仍会被发现并阻止其数据清理。
    if not platform_accounts:
        for local in list_available_mailboxes_from_tokens():
            local_email = str(local.get("email", "")).strip().lower()
            if local_email and local_email not in seen:
                seen.add(local_email)
                local["avatar_url"] = str(local.get("avatar_url") or get_account_avatar_url(local_email) or "")
                results.append(local)

    return results


def _sync_list_mailboxes() -> dict[str, Any]:
    from mail_agent.storage.ops import delete_mailbox_data, merge_discovered_mailboxes

    discovered = _discover_mailboxes()
    registry = _run_storage_query(merge_discovered_mailboxes(discovered))
    credentials_status = get_platform_credentials_status()
    if credentials_status.get("code") == "ok":
        # 只有 Credentials API 明确返回成功时，才能把“本次未发现”解释为
        # 平台已删除邮箱。临时网络故障会返回 unavailable，必须保留旧数据，
        # 避免一次 discovery 抖动造成不可逆的数据删除。
        discovered_emails = {
            str(item.get("email") or "").strip().lower()
            for item in discovered
            if str(item.get("email") or "").strip()
        }
        stale_mailboxes = [
            entry.email
            for entry in registry.mailboxes
            if entry.email not in discovered_emails and not entry.authorized
        ]
        for mailbox in stale_mailboxes:
            # delete_mailbox_data 同时清理 APS、本地 Gmail 缓存、工作流、草稿、
            # 联系人记忆和注册表，避免旧邮箱在更换账号后继续出现在首页。
            _run_storage_query(delete_mailbox_data(mailbox))
        if stale_mailboxes:
            from mail_agent.storage.ops import get_mailbox_registry
            registry = _run_storage_query(get_mailbox_registry())
    mailboxes = _registry_to_frontend(registry)
    return {
        "mailboxes": mailboxes,
        "selected": [item["email"] for item in mailboxes if item.get("selected")],
        "discovered": discovered,
        # 仅透传可安全展示的授权分类和下一步动作，绝不含 token。
        "credentials_status": credentials_status,
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
