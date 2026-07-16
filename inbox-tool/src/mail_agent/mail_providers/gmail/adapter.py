"""Mail adapter — reads from local Gmail cache and live Gmail API."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import hashlib
import html as html_lib
from html.parser import HTMLParser
import json
import logging
import os
import re
import shutil
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ...domain.types import MessageDetail, MessageLite, ThreadContext
from ...storage.keys import app_key

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
TOKEN_URI = "https://oauth2.googleapis.com/token"
_gmail_request_token: ContextVar[str | None] = ContextVar("gmail_request_token", default=None)
_history_sync_locks: dict[str, threading.Lock] = {}
_history_sync_locks_guard = threading.Lock()


class GmailApiError(ValueError):
    """保留安全 HTTP 状态码，供增量同步区分游标失效与单封邮件删除。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@contextmanager
def _gmail_request_token_scope(access_token: str | None = None):
    """仅在当前业务调用链内复用短期 token，退出作用域立即清除。"""
    marker = _gmail_request_token.set(access_token)
    try:
        yield
    finally:
        _gmail_request_token.reset(marker)


def beijing_now() -> str:
    return datetime.now(BEIJING_TZ).isoformat()


def _tool_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _repo_root() -> Path:
    # adapter.py lives at inbox-tool/src/mail_agent/mail_providers/gmail/adapter.py.
    return Path(__file__).resolve().parents[5]


def _is_platform() -> bool:
    """检测是否运行在 Anna 平台（非本地 dev）。"""
    if getattr(sys, "_MEIPASS", ""):
        return True
    if os.environ.get("GMAIL_ACCESS_TOKEN") or os.environ.get("GOOGLE_ACCESS_TOKEN"):
        return True
    if globals().get("_platform_account_map"):
        return True
    return False


def _data_root() -> Path:
    """返回统一的数据根目录。"""
    if _is_platform():
        return Path("./.data/").resolve()
    return _tool_root() / ".data"


def sanitize_mailbox_id(mailbox: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in mailbox.strip())
    return safe.strip("._") or "default"


_discovered_email: str = ""

# 多邮箱 token 映射表 {email: {access_token, refresh_token, ...}}
_multi_token_map: dict[str, dict[str, Any]] = {}
_multi_token_lock = threading.RLock()
_multi_token_refresh_locks: dict[str, threading.Lock] = {}
_platform_account_map: dict[str, dict[str, Any]] = {}
_platform_account_lock = threading.RLock()
_platform_account_lister: Callable[[], list[dict[str, Any]]] | None = None
_platform_token_resolver: Callable[[str, float], str] | None = None
_display_name_cache: dict[str, str] = {}
_avatar_url_cache: dict[str, str] = {}
_contact_avatar_cache: dict[str, dict[str, str]] = {}
_contact_avatar_loaded: set[str] = set()
GRAVATAR_AVATAR_BASE = "https://www.gravatar.com/avatar"


def _avatar_debug(message: str, **fields: Any) -> None:
    return


def _looks_like_email(value: str) -> bool:
    return "@" in value and "." in value.split("@")[-1]


def configure_platform_accounts(
    account_lister: Callable[[], list[dict[str, Any]]] | None,
    token_resolver: Callable[[str, float], str] | None,
) -> None:
    """Configure the Anna Credentials reverse-RPC bridge.

    The bridge exposes account metadata plus an on-demand short-lived access
    token. It intentionally never persists the returned token.
    """
    global _platform_account_lister, _platform_token_resolver
    with _platform_account_lock:
        _platform_account_lister = account_lister
        _platform_token_resolver = token_resolver


def set_platform_accounts(accounts: list[dict[str, Any]]) -> None:
    """Replace the in-memory platform account metadata snapshot."""
    next_map: dict[str, dict[str, Any]] = {}
    for raw in accounts:
        email = str(raw.get("email") or "").strip().lower()
        # 不同 Anna runtime 对同一账户主键分别使用 account_id 或 id。
        # 统一为 account_id，避免非默认账户因字段名差异被静默过滤掉。
        account_id = str(raw.get("account_id") or raw.get("id") or "").strip()
        if _looks_like_email(email) and account_id:
            next_map[email] = {
                "email": email,
                "account_id": account_id,
                "label": str(raw.get("label") or raw.get("name") or "").strip(),
                "is_default": bool(raw.get("is_default")),
                "status": str(raw.get("status") or "active").strip().lower(),
                "scopes": list(raw.get("scopes") or []) if isinstance(raw.get("scopes"), list) else [],
            }
    with _platform_account_lock:
        _platform_account_map.clear()
        _platform_account_map.update(next_map)


def get_platform_accounts() -> list[dict[str, Any]]:
    with _platform_account_lock:
        accounts = [dict(account) for account in _platform_account_map.values()]
    return sorted(accounts, key=lambda item: (not bool(item.get("is_default")), str(item.get("email") or "")))


def get_platform_account(mailbox: str) -> dict[str, Any]:
    normalized = str(mailbox or "").strip().lower()
    with _platform_account_lock:
        return dict(_platform_account_map.get(normalized) or {})


def _ensure_platform_accounts() -> None:
    with _platform_account_lock:
        lister = _platform_account_lister
    if lister is not None:
        lister()


def set_multi_tokens(tokens: list[dict[str, Any]]) -> None:
    global _multi_token_map
    next_map: dict[str, dict[str, Any]] = {}
    for record in tokens:
        email = str(record.get("email") or "").strip().lower()
        if _looks_like_email(email):
            next_map[email] = dict(record)
    # Runtime credentials are a snapshot, not an append-only stream. Replacing
    # the map ensures a token removed in Anna disappears from this process too.
    with _multi_token_lock:
        _multi_token_map = next_map


def get_multi_token_map() -> dict[str, dict[str, Any]]:
    with _multi_token_lock:
        return {email: dict(record) for email, record in _multi_token_map.items()}


def get_multi_token_emails() -> list[str]:
    with _multi_token_lock:
        return sorted(_multi_token_map.keys())


def _get_multi_token_refresh_lock(email: str) -> threading.Lock:
    with _multi_token_lock:
        lock = _multi_token_refresh_locks.get(email)
        if lock is None:
            lock = threading.Lock()
            _multi_token_refresh_locks[email] = lock
        return lock


def _schedule_aps_persist() -> None:
    """调度异步 APS 写入，刷新后的 token 通过 event loop 持久化。"""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            with _multi_token_lock:
                tokens = [{"email": e, **{k: v for k, v in r.items() if not k.startswith("_")}}
                          for e, r in _multi_token_map.items()]
            asyncio.run_coroutine_threadsafe(_async_persist_multi_tokens(tokens), loop)
    except RuntimeError:
        pass


async def _async_persist_multi_tokens(tokens: list[dict[str, Any]]) -> None:
    try:
        from ...storage.ops import set_multi_tokens as set_mt
        await set_mt(tokens)
    except Exception:
        pass


def normalize_mailbox(mailbox: str) -> str:
    global _discovered_email
    raw = str(mailbox or "").strip().lower()

    if not get_platform_account(raw):
        _ensure_platform_accounts()
    if get_platform_account(raw):
        return raw

    # Legacy multi-token path: accept any registered multi-token email.
    with _multi_token_lock:
        if raw in _multi_token_map:
            return raw

    # Platform path: if a token is available, discover the authorized email once.
    if os.environ.get("GMAIL_ACCESS_TOKEN") or os.environ.get("GOOGLE_ACCESS_TOKEN"):
        if not _discovered_email:
            _discovered_email = get_authorized_email().lower()
        if not _discovered_email:
            raise ValueError("Gmail token is present but could not resolve authorized email from profile")
        if raw == _discovered_email:
            return _discovered_email
        if _looks_like_email(raw):
            return raw
        return _discovered_email

    if _looks_like_email(raw):
        return raw
    raise ValueError(f"Unsupported mailbox: {mailbox}")


# ── Cache paths ───────────────────────────────────────────────────

def cache_dir() -> Path:
    override = os.environ.get("ZHAOPY_MAIL_AGENT_DATA_DIR")
    base = Path(override).expanduser().resolve() if override else _data_root() / "anna-inbox" / "gmail_cache"
    path = base / "mailboxes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _mailbox_cache_dir(mailbox: str) -> Path:
    path = cache_dir() / sanitize_mailbox_id(mailbox)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _index_path(mailbox: str) -> Path:
    return _mailbox_cache_dir(mailbox) / "index.json"


def _message_path(mailbox: str, message_id: str) -> Path:
    return _mailbox_cache_dir(mailbox) / f"{sanitize_mailbox_id(message_id)}.json"


def _feed_pages_dir(mailbox: str) -> Path:
    path = _mailbox_cache_dir(mailbox) / "feed_pages"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _feed_meta_path(mailbox: str) -> Path:
    return _feed_pages_dir(mailbox) / "meta.json"


def _feed_page_path(mailbox: str, page_number: int) -> Path:
    return _feed_pages_dir(mailbox) / f"page-{page_number:04d}.json"


def _sync_state_path(mailbox: str) -> Path:
    return _mailbox_cache_dir(mailbox) / "sync_state.json"


def _storage_cache_enabled() -> bool:
    try:
        from ...storage.client import backend, is_ready
        return is_ready() and backend() == "aps"
    except Exception:
        return False


def _storage_cache_prefix(mailbox: str) -> str:
    return app_key(f"gmail_cache/mailboxes/{sanitize_mailbox_id(mailbox)}")


def _storage_index_key(mailbox: str) -> str:
    return f"{_storage_cache_prefix(mailbox)}/index"


def _storage_message_key(mailbox: str, message_id: str) -> str:
    return f"{_storage_cache_prefix(mailbox)}/messages/{sanitize_mailbox_id(message_id)}"


def _storage_feed_meta_key(mailbox: str) -> str:
    return f"{_storage_cache_prefix(mailbox)}/feed_pages/meta"


def _storage_feed_page_key(mailbox: str, page_number: int) -> str:
    return f"{_storage_cache_prefix(mailbox)}/feed_pages/page/{page_number:04d}"


def _storage_sync_state_key(mailbox: str) -> str:
    return f"{_storage_cache_prefix(mailbox)}/sync_state"


def _storage_get_value_sync(key: str, *, timeout: float = 30.0) -> Any:
    from ...storage.client import get_storage, scope as default_scope
    from ...storage.sync_bridge import run as run_storage_sync
    result = run_storage_sync(get_storage().get(key, scope=default_scope()), timeout=timeout)
    return result.get("value") if result.get("exists") else None


def _storage_get_record_sync(key: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """读取 APS 值及 etag；增量 cursor 写回必须使用同一版本条件提交。"""
    from ...storage.client import get_storage, scope as default_scope
    from ...storage.sync_bridge import run as run_storage_sync
    result = run_storage_sync(get_storage().get(key, scope=default_scope()), timeout=timeout)
    return result if isinstance(result, dict) else {}


def _storage_set_value_sync(key: str, value: Any, *, timeout: float = 30.0) -> None:
    from ...storage.client import get_storage, scope as default_scope
    from ...storage.sync_bridge import run as run_storage_sync
    run_storage_sync(get_storage().set(key, value, scope=default_scope()), timeout=timeout)


def _storage_delete_value_sync(key: str, *, timeout: float = 30.0) -> None:
    from ...storage.client import get_storage, scope as default_scope
    from ...storage.sync_bridge import run as run_storage_sync
    run_storage_sync(get_storage().delete(key, scope=default_scope()), timeout=timeout)


def _storage_clear_prefix_sync(prefix: str, *, timeout: float = 30.0) -> int:
    """Delete every APS KV entry under ``prefix`` from a worker thread."""
    from ...storage.client import get_storage, scope as default_scope
    from ...storage.sync_bridge import run as run_storage_sync

    storage = get_storage()
    deleted = 0
    while True:
        result = run_storage_sync(
            storage.list(prefix=prefix, limit=200, scope=default_scope()),
            timeout=timeout,
        )
        items = result.get("items") or []
        if not items:
            break
        for item in items:
            key = str(item.get("key") or "") if isinstance(item, dict) else ""
            if not key:
                continue
            run_storage_sync(storage.delete(key, scope=default_scope()), timeout=timeout)
            deleted += 1
    return deleted


async def _storage_get_value_async(key: str, *, timeout: float = 30.0) -> Any:
    from ...storage.client import get_storage, scope as default_scope
    result = await get_storage().get(key, scope=default_scope(), timeout=timeout)
    return result.get("value") if result.get("exists") else None


def cache_debug_info(mailbox: str) -> dict[str, Any]:
    if _storage_cache_enabled():
        return {
            "backend": "aps",
            "prefix": _storage_cache_prefix(mailbox),
            "index_key": _storage_index_key(mailbox),
        }
    return {
        "backend": "local",
        "cache_dir": str(_mailbox_cache_dir(mailbox)),
        "index_file": str(_index_path(mailbox)),
    }


def clear_mailbox_cache(mailbox: str) -> dict[str, Any]:
    """Clear only one mailbox's Gmail feed/body cache.

    OAuth credentials, mailbox registration, Brief cards, scan state, drafts,
    and preferences are intentionally outside this cache boundary.
    """
    normalized = normalize_mailbox(mailbox)
    deleted_aps_keys = 0
    if _storage_cache_enabled():
        deleted_aps_keys = _storage_clear_prefix_sync(f"{_storage_cache_prefix(normalized)}/")

    local_dir = cache_dir() / sanitize_mailbox_id(normalized)
    deleted_local = local_dir.exists()
    if deleted_local:
        shutil.rmtree(local_dir)

    return {
        "mailbox": normalized,
        "deleted_aps_keys": deleted_aps_keys,
        "deleted_local_cache": deleted_local,
    }


def _history_sync_lock(mailbox: str) -> threading.Lock:
    """同一 Executa 进程内串行化单邮箱增量同步，避免旧快照覆盖新标签。"""
    normalized = normalize_mailbox(mailbox)
    with _history_sync_locks_guard:
        lock = _history_sync_locks.get(normalized)
        if lock is None:
            lock = threading.Lock()
            _history_sync_locks[normalized] = lock
        return lock


def _read_history_sync_state(mailbox: str) -> dict[str, Any]:
    if _storage_cache_enabled():
        record = _storage_get_record_sync(_storage_sync_state_key(mailbox))
        payload = record.get("value") if record.get("exists") else None
        if not isinstance(payload, dict):
            return {}
        return {**payload, "_etag": str(record.get("etag") or "")}
    path = _sync_state_path(mailbox)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _write_history_sync_state(mailbox: str, state: dict[str, Any], *, if_match: str | None = None) -> None:
    """同步游标只在 index 成功写入后更新，失败时保留旧 cursor 以便安全重试。"""
    payload = {
        "schema_version": 1,
        "history_id": str(state.get("history_id") or ""),
        "scope_days": int(state.get("scope_days") or 30),
        "updated_at": beijing_now(),
    }
    if _storage_cache_enabled():
        from ...storage.client import get_storage, scope as default_scope
        from ...storage.sync_bridge import run as run_storage_sync
        run_storage_sync(
            get_storage().set(
                _storage_sync_state_key(mailbox),
                payload,
                scope=default_scope(),
                if_match=if_match,
            ),
        )
        return
    path = _sync_state_path(mailbox)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def set_cached_mailbox_history_cursor(mailbox: str, history_id: str, *, scope_days: int) -> None:
    """在 All-mail 全量快照完成后建立 Gmail History cursor。"""
    if not str(history_id or "").strip():
        return
    with _history_sync_lock(mailbox):
        previous = _read_history_sync_state(mailbox)
        _write_history_sync_state(
            mailbox,
            {"history_id": history_id, "scope_days": scope_days},
            if_match=str(previous.get("_etag") or "") or None,
        )


# ── Cache read / write ────────────────────────────────────────────

def list_messages(mailbox: str) -> list[dict[str, Any]]:
    if _storage_cache_enabled():
        payload = _storage_get_value_sync(_storage_index_key(mailbox))
        if not isinstance(payload, dict):
            return []
        messages = payload.get("messages")
        return messages if isinstance(messages, list) else []

    path = _index_path(mailbox)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and payload.get("messages"):
            return payload["messages"]

    # Fallback: rebuild from individual message files when index is missing
    msg_dir = _mailbox_cache_dir(mailbox)
    if not msg_dir.is_dir():
        return []
    messages: list[dict[str, Any]] = []
    for f in sorted(msg_dir.iterdir()):
        if not f.name.endswith(".json"):
            continue
        try:
            msg = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(msg, dict):
                messages.append(msg)
        except (json.JSONDecodeError, OSError):
            pass
    return sorted(messages, key=lambda m: int(m.get("internal_date") or 0), reverse=True)


def read_cache(mailbox: str) -> dict[str, Any]:
    if _storage_cache_enabled():
        try:
            payload = _storage_get_value_sync(_storage_index_key(mailbox))
        except Exception as exc:
            _aps_cache_errors.append(f"read_cache({mailbox}): {exc}")
            return {"mailbox": mailbox, "messages": [], "updated_at": None}
        if not isinstance(payload, dict):
            return {"mailbox": mailbox, "messages": [], "updated_at": None}
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        return {"mailbox": mailbox, "messages": messages, "updated_at": payload.get("updated_at")}

    path = _index_path(mailbox)
    if not path.exists():
        return {"mailbox": mailbox, "messages": [], "updated_at": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"mailbox": mailbox, "messages": [], "updated_at": None}
    if not isinstance(payload, dict):
        return {"mailbox": mailbox, "messages": [], "updated_at": None}
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    return {"mailbox": mailbox, "messages": messages, "updated_at": payload.get("updated_at")}


_aps_cache_errors: list[str] = []


def get_aps_cache_errors() -> list[str]:
    return list(_aps_cache_errors)


def _clear_aps_cache_errors() -> None:
    _aps_cache_errors.clear()


def write_index(mailbox: str, messages: list[dict[str, Any]]) -> None:
    ordered_messages = sorted(messages, key=_internal_date_sort_key, reverse=True)
    updated_at = beijing_now()
    payload = {
        "mailbox": mailbox,
        "updated_at": updated_at,
        "message_count": len(ordered_messages),
        "messages": ordered_messages,
    }
    if _storage_cache_enabled():
        try:
            _storage_set_value_sync(_storage_index_key(mailbox), payload)
        except Exception as exc:
            _aps_cache_errors.append(f"write_index({mailbox}): {exc}")
        _write_cached_feed_pages(mailbox, ordered_messages, updated_at)
        return
    _index_path(mailbox).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_cached_feed_pages(mailbox, ordered_messages, updated_at)


def write_message(mailbox: str, message: dict[str, Any]) -> None:
    message_id = str(message.get("id") or "")
    if not message_id:
        raise ValueError("Cannot cache Gmail message without id")
    if _storage_cache_enabled():
        try:
            _storage_set_value_sync(_storage_message_key(mailbox, message_id), message)
        except Exception as exc:
            _aps_cache_errors.append(f"write_message({mailbox}, {message_id[:20]}): {exc}")
        return
    _message_path(mailbox, message_id).write_text(
        json.dumps(message, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def read_message(mailbox: str, message_id: str) -> dict[str, Any]:
    if _storage_cache_enabled():
        payload = _storage_get_value_sync(_storage_message_key(mailbox, message_id))
        if isinstance(payload, dict):
            return payload
        raise ValueError(f"Cached message not found: {message_id}")

    path = _message_path(mailbox, message_id)
    if not path.exists():
        raise ValueError(f"Cached message not found: {message_id}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Cached message is invalid: {message_id}")
    return payload


async def read_message_async(mailbox: str, message_id: str) -> dict[str, Any]:
    if not _storage_cache_enabled():
        return read_message(mailbox, message_id)
    payload = await _storage_get_value_async(_storage_message_key(mailbox, message_id))
    if not isinstance(payload, dict):
        raise ValueError(f"Cached message not found: {message_id}")
    return payload


def message_summary(message: dict[str, Any]) -> dict[str, Any]:
    body_text = str(message.get("body_text") or "")
    headers = message.get("raw_headers") if isinstance(message.get("raw_headers"), dict) else {}
    summary_keys = [
        "id", "thread_id", "mailbox", "history_id", "internal_date",
        "date", "from", "to", "cc", "bcc", "subject", "message_id",
        "in_reply_to", "references", "label_ids", "snippet",
        "size_estimate", "mime_type", "attachments", "fetched_at",
    ]
    summary = {key: message.get(key) for key in summary_keys}
    summary["body_preview"] = body_text[:500]
    summary["body_length"] = len(body_text)
    summary["raw_header_count"] = len(headers)
    summary["body_cached"] = bool(message.get("_body_cached", True))
    mailbox = str(message.get("mailbox") or "")
    message_id = str(message.get("id") or "")
    if summary["body_cached"]:
        if _storage_cache_enabled():
            summary["cache_key"] = _storage_message_key(mailbox, message_id)
        else:
            summary["json_file"] = str(_message_path(mailbox, message_id))
    return summary


def _internal_date_sort_key(item: dict[str, Any]) -> int:
    try:
        return int(item.get("internal_date") or 0)
    except (TypeError, ValueError):
        return 0


def _thread_original_subjects(messages: list[dict[str, Any]]) -> dict[str, str]:
    subjects: dict[str, str] = {}
    for item in sorted(messages, key=_internal_date_sort_key):
        thread_id = str(item.get("thread_id") or "").strip()
        subject = str(item.get("subject") or "").strip()
        if thread_id and subject and thread_id not in subjects:
            subjects[thread_id] = subject
    return subjects


def _compact_feed_message(message: dict[str, Any], mailbox: str, thread_subjects: dict[str, str] | None = None) -> dict[str, Any]:
    labels = [str(label)[:80] for label in (message.get("label_ids") or [])][:32]
    attachments = message.get("attachments") if isinstance(message.get("attachments"), list) else []
    try:
        stored_attachment_count = int(message.get("attachment_count") or 0)
    except (TypeError, ValueError):
        stored_attachment_count = 0
    attachment_count = max(len(attachments), stored_attachment_count)
    has_attachment = bool(attachments) or bool(message.get("has_attachment")) or attachment_count > 0
    latest_subject = str(message.get("subject") or "").strip()
    thread_id = str(message.get("thread_id") or "").strip()
    subject = str((thread_subjects or {}).get(thread_id) or message.get("original_subject") or latest_subject)
    return {
        "id": str(message.get("id") or "")[:128],
        "thread_id": thread_id[:128],
        "mailbox": mailbox,
        "internal_date": str(message.get("internal_date") or "")[:32],
        "date": str(message.get("date") or "")[:128],
        "from": str(message.get("from") or "")[:512],
        "to": str(message.get("to") or "")[:512],
        "subject": subject[:512],
        "latest_subject": latest_subject[:512],
        "snippet": str(message.get("snippet") or "")[:HOME_FEED_SNIPPET_MAX_CHARS],
        "body_preview": str(message.get("body_preview") or "")[:HOME_FEED_BODY_PREVIEW_MAX_CHARS],
        "label_ids": labels,
        "unread": "UNREAD" in labels,
        "important": "IMPORTANT" in labels,
        "starred": "STARRED" in labels,
        "has_attachment": has_attachment,
        "attachment_count": attachment_count,
        "body_cached": bool(message.get("body_cached")),
    }


def _build_feed_page_metadata(mailbox: str, messages: list[dict[str, Any]], updated_at: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ordered = sorted(messages, key=_internal_date_sort_key, reverse=True)
    thread_subjects = _thread_original_subjects(messages)
    compact_messages = [_compact_feed_message(message, mailbox, thread_subjects) for message in ordered]
    pages: list[dict[str, Any]] = []
    page_payloads: list[dict[str, Any]] = []
    for page_number, start in enumerate(range(0, len(compact_messages), CACHED_FEED_PAGE_SIZE)):
        page_messages = compact_messages[start:start + CACHED_FEED_PAGE_SIZE]
        pages.append({
            "page": page_number,
            "count": len(page_messages),
            "start_offset": start,
            "newest_internal_date": str(page_messages[0].get("internal_date") or "") if page_messages else "",
            "oldest_internal_date": str(page_messages[-1].get("internal_date") or "") if page_messages else "",
        })
        page_payloads.append({
            "mailbox": mailbox,
            "page": page_number,
            "updated_at": updated_at,
            "count": len(page_messages),
            "messages": page_messages,
        })
    meta = {
        "mailbox": mailbox,
        "updated_at": updated_at,
        # 分页内容字段变化时递增版本。云端 APS 每次读取都需要一次反向 RPC，
        # 已确认版本的元数据不应再额外读取第一页进行字段探测。
        "schema_version": 2,
        "page_size": CACHED_FEED_PAGE_SIZE,
        "message_count": len(compact_messages),
        "page_count": len(page_payloads),
        "pages": pages,
    }
    return meta, page_payloads


def _write_cached_feed_pages(mailbox: str, messages: list[dict[str, Any]], updated_at: str | None = None) -> None:
    stamp = str(updated_at or beijing_now())
    meta, page_payloads = _build_feed_page_metadata(mailbox, messages, stamp)
    if _storage_cache_enabled():
        previous = _storage_get_value_sync(_storage_feed_meta_key(mailbox))
        previous_count = int(previous.get("page_count") or 0) if isinstance(previous, dict) else 0
        _storage_set_value_sync(_storage_feed_meta_key(mailbox), meta)
        for page_payload in page_payloads:
            _storage_set_value_sync(_storage_feed_page_key(mailbox, int(page_payload["page"])), page_payload)
        for page_number in range(len(page_payloads), previous_count):
            try:
                _storage_delete_value_sync(_storage_feed_page_key(mailbox, page_number))
            except Exception:
                pass
        return

    meta_path = _feed_meta_path(mailbox)
    pages_dir = _feed_pages_dir(mailbox)
    previous_count = 0
    if meta_path.exists():
        try:
            previous = json.loads(meta_path.read_text(encoding="utf-8"))
            previous_count = int(previous.get("page_count") or 0) if isinstance(previous, dict) else 0
        except Exception:
            previous_count = 0
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for page_payload in page_payloads:
        _feed_page_path(mailbox, int(page_payload["page"])).write_text(
            json.dumps(page_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    for page_number in range(len(page_payloads), previous_count):
        stale_path = _feed_page_path(mailbox, page_number)
        if stale_path.exists():
            stale_path.unlink()


def _read_cached_feed_meta(mailbox: str) -> dict[str, Any] | None:
    if _storage_cache_enabled():
        payload = _storage_get_value_sync(_storage_feed_meta_key(mailbox))
        return payload if isinstance(payload, dict) else None
    path = _feed_meta_path(mailbox)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _read_cached_feed_page(mailbox: str, page_number: int) -> dict[str, Any] | None:
    if _storage_cache_enabled():
        payload = _storage_get_value_sync(_storage_feed_page_key(mailbox, page_number))
        return payload if isinstance(payload, dict) else None
    path = _feed_page_path(mailbox, page_number)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def ensure_cached_feed_index(mailbox: str) -> dict[str, Any]:
    meta = _read_cached_feed_meta(mailbox)
    if isinstance(meta, dict) and int(meta.get("schema_version") or 0) >= 2:
        return meta
    # 旧缓存缺少 schema_version，直接从索引重建一次。不能只返回旧元数据，
    # 否则每一次云端分页都会继续额外读取第一页做历史字段探测。
    cached = read_cache(mailbox)
    messages = cached.get("messages") if isinstance(cached, dict) and isinstance(cached.get("messages"), list) else []
    updated_at = str(cached.get("updated_at") or beijing_now())
    _write_cached_feed_pages(mailbox, messages, updated_at)
    return _read_cached_feed_meta(mailbox) or {
        "mailbox": mailbox,
        "updated_at": updated_at,
        "schema_version": 2,
        "page_size": CACHED_FEED_PAGE_SIZE,
        "message_count": 0,
        "page_count": 0,
        "pages": [],
    }


def get_cached_feed_page(mailbox: str, page_number: int) -> dict[str, Any]:
    payload = _read_cached_feed_page(mailbox, page_number)
    if isinstance(payload, dict):
        return payload
    return {"mailbox": mailbox, "page": page_number, "updated_at": None, "count": 0, "messages": []}


SUMMARY_METADATA_REFRESH_SECONDS = 30 * 60
SUMMARY_FETCH_MAX_WORKERS = 20
GMAIL_PAGE_SUMMARY_FETCH_MAX_WORKERS = 20
CACHED_FEED_PAGE_SIZE = 100
HOME_FEED_SNIPPET_MAX_CHARS = 120
HOME_FEED_BODY_PREVIEW_MAX_CHARS = 120


# ── Gmail API token management ────────────────────────────────────

def _token_dir() -> Path:
    override = os.environ.get("ANNA_INBOX_TOKEN_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return _repo_root() / "scripts" / "google_token" / ".secrets" / "gmail_tokens"


def _load_token_record(mailbox: str) -> dict[str, Any]:
    candidates = [
        _token_dir() / f"{sanitize_mailbox_id(mailbox)}.json",
        _token_dir() / "default.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            record["_token_file"] = str(path)
            return record
    raise ValueError(f"Local Gmail token file not found for {mailbox}")


def list_available_mailboxes_from_tokens() -> list[dict[str, Any]]:
    """发现本地 Gmail token 文件，供多邮箱开发模式使用。"""
    token_root = _token_dir()
    if not token_root.exists():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(token_root.glob("*.json")):
        if path.name == "default.json":
            continue
        email = path.stem.replace("_", "@", 1) if "_" in path.stem else path.stem
        display_name = ""
        avatar_url = ""
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(record, dict):
                email = str(record.get("email") or record.get("mailbox") or record.get("emailAddress") or email)
                display_name = str(record.get("display_name") or record.get("name") or "")
                avatar_url = str(record.get("avatar_url") or record.get("picture") or "")
        except Exception:
            pass
        email = email.strip().lower()
        if _looks_like_email(email):
            results.append({
                "email": email,
                "display_name": display_name,
                "provider": "gmail",
                "auth_source": "local_file",
                "authorized": True,
                "avatar_url": avatar_url,
                "token_file": str(path),
                "last_auth_checked_at": beijing_now(),
            })
    return results


def _should_refresh_token(record: dict[str, Any]) -> bool:
    if not record.get("refresh_token"):
        return False
    try:
        return float(record.get("expires_at") or 0) <= time.time() + 60
    except (TypeError, ValueError):
        return False


def _refresh_access_token(record: dict[str, Any], *, timeout_seconds: float | None = None) -> None:
    """刷新本地 OAuth token；传入预算时，全部重试共用同一个截止时间。"""
    client_id = record.get("client_id")
    client_secret = record.get("client_secret")
    refresh_token = record.get("refresh_token")
    if not client_id or not client_secret or not refresh_token:
        raise ValueError("Gmail refresh token is missing client metadata")
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URI, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    deadline = time.monotonic() + max(0.1, float(timeout_seconds)) if timeout_seconds is not None else None
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            request_timeout = 30.0
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Gmail token refresh timed out")
                request_timeout = min(request_timeout, remaining)
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except (urllib.error.URLError, ssl.SSLError, TimeoutError) as exc:
            last_error = exc
            if attempt >= 2:
                raise
            if deadline is not None and deadline - time.monotonic() <= 0:
                raise
            logging.getLogger("mail_agent.gmail").warning(
                "token refresh retry %s/3 for %s after %s",
                attempt + 2,
                str(record.get("email") or "<local-token>"),
                exc,
            )
            time.sleep(0.35 * (attempt + 1))
    else:
        if last_error:
            raise last_error
        raise RuntimeError("token refresh failed without a captured exception")
    record["access_token"] = payload["access_token"]
    if "expires_in" in payload:
        record["expires_at"] = int(time.time()) + int(payload["expires_in"])
    record["updated_at"] = beijing_now()
    token_file = record.get("_token_file")
    if token_file:
        clean_record = {key: value for key, value in record.items() if not key.startswith("_")}
        Path(str(token_file)).write_text(json.dumps(clean_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return

def get_access_token(
    mailbox: str,
    *,
    platform_token_timeout_seconds: float = 35.0,
    token_refresh_timeout_seconds: float | None = None,
    refresh_platform_accounts: bool = True,
) -> str:
    normalized = str(mailbox or "").strip().lower()

    # 延迟探测已在调用方使用受限预算刷新过账号，禁止 adapter 再开启一轮默认 12 秒的发现。
    if refresh_platform_accounts and not get_platform_account(normalized):
        _ensure_platform_accounts()
    platform_account = get_platform_account(normalized)
    if platform_account:
        with _platform_account_lock:
            resolver = _platform_token_resolver
        if resolver is None:
            raise ValueError("Platform account is available but credentials/getToken is unavailable")
        # 连通性探测传入剩余预算；正常 Gmail 业务沿用 35 秒的默认凭据预算。
        token = resolver(str(platform_account["account_id"]), platform_token_timeout_seconds)
        if token:
            return token
        raise ValueError(f"Platform returned no Gmail access token for {mailbox}")

    # Legacy multi-token path, retained for existing local development data.
    with _multi_token_lock:
        has_multi_token = normalized in _multi_token_map
    if has_multi_token:
        refresh_lock = _get_multi_token_refresh_lock(normalized)
        with refresh_lock:
            with _multi_token_lock:
                current = _multi_token_map.get(normalized)
                if current is None:
                    raise ValueError(f"Gmail mailbox was unbound while resolving its token: {mailbox}")
                record = dict(current)
            if _should_refresh_token(record):
                try:
                    _refresh_access_token(record, timeout_seconds=token_refresh_timeout_seconds)
                except (urllib.error.HTTPError, urllib.error.URLError, ssl.SSLError, TimeoutError) as exc:
                    # Anna may inject a fresh access token together with stale refresh
                    # metadata. Try the access token once instead of failing before the
                    # Gmail request; Gmail will still reject it if it is actually expired.
                    if not record.get("access_token"):
                        if isinstance(exc, urllib.error.HTTPError):
                            raise ValueError(f"Gmail token refresh failed for {mailbox}: HTTP {exc.code}") from exc
                        raise ValueError(f"Gmail token refresh failed for {mailbox}: {exc}") from exc
                    logging.getLogger("mail_agent.gmail").warning("refresh failed for %s; trying current access token (%s)", mailbox, exc)
                else:
                    should_persist = False
                    with _multi_token_lock:
                        latest = _multi_token_map.get(normalized)
                        if latest is None:
                            raise ValueError(f"Gmail mailbox was unbound while refreshing its token: {mailbox}")
                        # A newly bound credential wins over an older refresh result.
                        if latest.get("refresh_token") == current.get("refresh_token"):
                            updated = {
                                **latest,
                                "access_token": record.get("access_token"),
                                "expires_at": record.get("expires_at"),
                                "updated_at": record.get("updated_at"),
                            }
                            _multi_token_map[normalized] = updated
                            record = dict(updated)
                            should_persist = True
                        else:
                            record = dict(latest)
                    if should_persist:
                        _schedule_aps_persist()
            token = record.get("access_token")
            if not token:
                raise ValueError(f"Gmail access token is missing for multi-token mailbox {mailbox}")
            return str(token)

    # Legacy single-account fallback. Platform refreshes this credential for us.
    platform_token = os.environ.get("GMAIL_ACCESS_TOKEN") or os.environ.get("GOOGLE_ACCESS_TOKEN")
    if platform_token and platform_token.strip():
        global _discovered_email
        if not _discovered_email or normalized != _discovered_email:
            _discovered_email = get_authorized_email().lower()
        if normalized == _discovered_email or not _discovered_email:
            return str(platform_token).strip()

    # Local dev — read from JSON token file with refresh support.
    record = _load_token_record(mailbox)
    if _should_refresh_token(record):
        try:
            _refresh_access_token(record, timeout_seconds=token_refresh_timeout_seconds)
        except (urllib.error.HTTPError, urllib.error.URLError, ssl.SSLError, TimeoutError) as exc:
            if not record.get("access_token"):
                if isinstance(exc, urllib.error.HTTPError):
                    raise ValueError(f"Gmail token refresh failed for {mailbox}: HTTP {exc.code}") from exc
                raise ValueError(f"Gmail token refresh failed for {mailbox}: {exc}") from exc
            logging.getLogger("mail_agent.gmail").warning("local refresh failed for %s; trying current access token (%s)", mailbox, exc)
    token = record.get("access_token")
    if not token:
        raise ValueError(f"Local Gmail access token is missing for {mailbox}")
    return str(token)


def get_authorized_email() -> str:
    """Discover the authorized Gmail account from a platform-injected token.

    Calls Gmail users/me/profile with the token from os.environ.
    Returns the authorized email address, or empty string if not available.
    """
    token = os.environ.get("GMAIL_ACCESS_TOKEN") or os.environ.get("GOOGLE_ACCESS_TOKEN")
    if not token or not token.strip():
        return ""

    req = urllib.request.Request(
        GMAIL_API_BASE + "/users/me/profile",
        headers={"Authorization": f"Bearer {token.strip()}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            profile = json.loads(response.read().decode("utf-8"))
    except Exception:
        return ""

    return str(profile.get("emailAddress") or "").strip()


def _load_stored_account_profile(mailbox: str) -> tuple[str, str]:
    normalized = str(mailbox or "").strip().lower()
    record = get_multi_token_map().get(normalized) or {}
    display_name = str(record.get("display_name") or record.get("name") or "")
    avatar_url = str(record.get("avatar_url") or record.get("picture") or "")
    if display_name or avatar_url:
        return display_name, avatar_url
    try:
        local = _load_token_record(normalized)
        return (
            str(local.get("display_name") or local.get("name") or ""),
            str(local.get("avatar_url") or local.get("picture") or ""),
        )
    except Exception:
        return "", ""


def _fetch_account_profile(mailbox: str) -> tuple[str, str]:
    normalized = str(mailbox or "").strip().lower()
    token = get_access_token(normalized)
    request = urllib.request.Request(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return str(payload.get("name") or ""), str(payload.get("picture") or "")


def get_account_display_name(mailbox: str) -> str:
    normalized = str(mailbox or "").strip().lower()
    if normalized in _display_name_cache:
        return _display_name_cache[normalized]
    stored_name, stored_avatar = _load_stored_account_profile(normalized)
    if stored_name:
        _display_name_cache[normalized] = stored_name
        if stored_avatar:
            _avatar_url_cache[normalized] = stored_avatar
        return stored_name
    try:
        fetched_name, fetched_avatar = _fetch_account_profile(normalized)
    except Exception:
        fetched_name = ""
        fetched_avatar = stored_avatar
    _display_name_cache[normalized] = fetched_name
    if fetched_avatar:
        _avatar_url_cache[normalized] = fetched_avatar
    return fetched_name


def get_account_avatar_url(mailbox: str) -> str:
    """Best-effort Google profile image lookup; returns empty without profile scope."""
    normalized = str(mailbox or "").strip().lower()
    if normalized in _avatar_url_cache:
        return _avatar_url_cache[normalized]
    stored_name, stored = _load_stored_account_profile(normalized)
    if stored_name and normalized not in _display_name_cache:
        _display_name_cache[normalized] = stored_name
    if stored:
        _avatar_url_cache[normalized] = stored
        return stored
    try:
        fetched_name, stored = _fetch_account_profile(normalized)
        if fetched_name:
            _display_name_cache[normalized] = fetched_name
    except Exception:
        stored = ""
    _avatar_url_cache[normalized] = stored
    return stored


def _people_api_get(token: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
    url = f"https://people.googleapis.com/v1/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _cache_people_photo_urls(cached: dict[str, str], people: list[Any]) -> None:
    for person in people:
        if not isinstance(person, dict):
            continue
        photos = [item for item in (person.get("photos") or []) if isinstance(item, dict) and item.get("url")]
        photo_url = str(photos[0].get("url") or "") if photos else ""
        if not photo_url:
            continue
        for item in person.get("emailAddresses") or []:
            email = str(item.get("value") or "").strip().lower() if isinstance(item, dict) else ""
            if email:
                cached[email] = photo_url
                _avatar_debug(
                    "people_photo_cached",
                    email_hash=hashlib.md5(email.encode("utf-8")).hexdigest()[:8],
                    url_present=True,
                    url_host=urllib.parse.urlparse(photo_url).netloc or "unknown",
                )


def _gravatar_avatar_url(email: str) -> str:
    normalized = str(email or "").strip().lower()
    if not _looks_like_email(normalized):
        _avatar_debug("gravatar_url_skipped", reason="invalid_email")
        return ""
    digest = hashlib.md5(normalized.encode("utf-8")).hexdigest()
    url = f"{GRAVATAR_AVATAR_BASE}/{digest}?s=96&d=404"
    _avatar_debug("gravatar_url_generated", email_hash=digest[:8], url_present=bool(url), url_host="www.gravatar.com")
    return url


def _gravatar_avatar_exists(url: str) -> bool:
    if not url:
        _avatar_debug("gravatar_head_skipped", reason="empty_url")
        return False
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Anna-Inbox/2.0"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            exists = 200 <= int(response.status) < 400
            _avatar_debug("gravatar_head_result", status=int(response.status), exists=exists)
            return exists
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            _avatar_debug("gravatar_head_result", status=404, exists=False)
            return False
        _avatar_debug("gravatar_head_error", status=exc.code)
        raise


def _resolve_gravatar_avatar_url(email: str) -> str:
    url = _gravatar_avatar_url(email)
    if not url:
        return ""
    try:
        return url if _gravatar_avatar_exists(url) else ""
    except Exception as exc:
        _avatar_debug("gravatar_resolve_failed", error=type(exc).__name__)
        return ""


def _cache_gravatar_photo_urls(cached: dict[str, str], requested: set[str]) -> None:
    for email in sorted(requested):
        if email in cached:
            continue
        gravatar_url = _resolve_gravatar_avatar_url(email)
        if gravatar_url:
            cached[email] = gravatar_url
            _avatar_debug("gravatar_cached", email_hash=hashlib.md5(email.encode("utf-8")).hexdigest()[:8])


def _http_error_json(exc: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        body = exc.read().decode("utf-8")
        return json.loads(body) if body else {}
    except Exception:
        return {}


def resolve_contact_avatar_urls(mailbox: str, emails: list[str]) -> dict[str, Any]:
    """Resolve Google Contact photos, then Gravatar photos, for a bounded email set."""
    normalized_mailbox = normalize_mailbox(mailbox)
    requested = {
        str(email or "").strip().lower()
        for email in emails[:200]
        if "@" in str(email or "")
    }
    cached = _contact_avatar_cache.setdefault(normalized_mailbox, {})
    _avatar_debug(
        "adapter_invoked",
        mailbox_hash=hashlib.md5(normalized_mailbox.encode("utf-8")).hexdigest()[:8],
        requested=len(requested),
        cached=len(cached),
        loaded=normalized_mailbox in _contact_avatar_loaded,
    )
    if normalized_mailbox not in _contact_avatar_loaded:
        token = get_access_token(normalized_mailbox)
        try:
            page_token = ""
            while True:
                params = {
                    "resourceName": "people/me",
                    "pageSize": 1000,
                    "personFields": "names,emailAddresses,photos",
                }
                if page_token:
                    params["pageToken"] = page_token
                payload = _people_api_get(token, "people/me/connections", params)
                _avatar_debug(
                    "people_connections_page",
                    count=len(payload.get("connections") or []),
                    next=bool(payload.get("nextPageToken")),
                )
                _cache_people_photo_urls(cached, payload.get("connections") or [])
                page_token = str(payload.get("nextPageToken") or "")
                if not page_token:
                    break

            page_token = ""
            while True:
                params = {
                    "pageSize": 1000,
                    "readMask": "names,emailAddresses,photos",
                    "sources": ["READ_SOURCE_TYPE_CONTACT", "READ_SOURCE_TYPE_PROFILE"],
                }
                if page_token:
                    params["pageToken"] = page_token
                payload = _people_api_get(token, "otherContacts", params)
                _avatar_debug(
                    "people_other_contacts_page",
                    count=len(payload.get("otherContacts") or []),
                    next=bool(payload.get("nextPageToken")),
                )
                _cache_people_photo_urls(cached, payload.get("otherContacts") or [])
                page_token = str(payload.get("nextPageToken") or "")
                if not page_token:
                    break
            _contact_avatar_loaded.add(normalized_mailbox)
        except urllib.error.HTTPError as exc:
            error_payload = _http_error_json(exc)
            error_info = (((error_payload.get("error") or {}).get("details") or []) if isinstance(error_payload, dict) else [])
            service_disabled = next(
                (
                    item for item in error_info
                    if isinstance(item, dict)
                    and item.get("@type") == "type.googleapis.com/google.rpc.ErrorInfo"
                    and item.get("reason") == "SERVICE_DISABLED"
                ),
                None,
            )
            if service_disabled:
                metadata = service_disabled.get("metadata") if isinstance(service_disabled.get("metadata"), dict) else {}
                _cache_gravatar_photo_urls(cached, requested)
                result_count = len([email for email in requested if email in cached])
                _avatar_debug("adapter_return", path="service_disabled", returned=result_count)
                return {
                    "avatars": {email: cached[email] for email in requested if email in cached},
                    "permission_required": False,
                    "service_disabled": True,
                    "service": str(metadata.get("service") or "people.googleapis.com"),
                    "activation_url": str(metadata.get("activationUrl") or ""),
                    "warning": str(((error_payload.get("error") or {}).get("message") or "People API is disabled.")),
                }
            if exc.code in (401, 403):
                _cache_gravatar_photo_urls(cached, requested)
                result_count = len([email for email in requested if email in cached])
                _avatar_debug("adapter_return", path="permission_required", returned=result_count)
                return {
                    "avatars": {email: cached[email] for email in requested if email in cached},
                    "permission_required": True,
                    "required_scopes": [
                        "https://www.googleapis.com/auth/contacts.readonly",
                        "https://www.googleapis.com/auth/contacts.other.readonly",
                    ],
                }
            raise ValueError(f"Google People API request failed: HTTP {exc.code}") from exc
        except Exception as exc:
            _cache_gravatar_photo_urls(cached, requested)
            result_count = len([email for email in requested if email in cached])
            _avatar_debug("adapter_return", path="warning", returned=result_count, error=type(exc).__name__)
            return {"avatars": {email: cached[email] for email in requested if email in cached}, "warning": str(exc)}

    own_avatar = get_account_avatar_url(normalized_mailbox)
    if own_avatar:
        cached[normalized_mailbox] = own_avatar
    _cache_gravatar_photo_urls(cached, requested)
    result_count = len([email for email in requested if email in cached])
    _avatar_debug("adapter_return", path="ok", returned=result_count, cached=len(cached))
    return {
        "avatars": {email: cached[email] for email in requested if email in cached},
        "permission_required": False,
    }


# ── Gmail API request ─────────────────────────────────────────────

def gmail_request(
    mailbox: str,
    path: str,
    query: dict[str, Any] | None = None,
    *,
    access_token: str | None = None,
) -> dict[str, Any]:
    # 调用方可在一个受限业务请求内复用已取得的短期 token，减少平台
    # credentials/getToken 的反向 RPC 数量；未传入时保留原有按请求解析行为。
    scoped_token = _gmail_request_token.get()
    token = access_token or scoped_token or get_access_token(mailbox)
    # 第一个 Gmail 请求取得 token 后写入当前受限上下文，后续同批请求及复制出的
    # 摘要 worker 都可复用；外层 scope 退出时会恢复，不会残留在进程全局状态。
    if not access_token and not scoped_token:
        _gmail_request_token.set(token)
    url = GMAIL_API_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
            detail_json = json.loads(detail) if detail else {"status_code": exc.code}
        except Exception:
            detail_json = {"status_code": exc.code}
        raise GmailApiError(exc.code, f"Gmail API request failed: {exc.code} {detail_json}") from exc
    return json.loads(raw) if raw else {}


# ── Gmail message parsing ─────────────────────────────────────────

def _header_map(message: dict[str, Any]) -> dict[str, str]:
    headers = ((message.get("payload") or {}).get("headers") or [])
    result: dict[str, str] = {}
    for header in headers:
        if isinstance(header, dict) and header.get("name"):
            result[str(header["name"]).lower()] = str(header.get("value") or "")
    return result


_BODY_TEXT_LIMIT = 30000
_DISPLAY_BODY_LIMIT = 200000
_HTML_RE = re.compile(r"<\s*(?:!doctype|html|head|body|table|style|script|div|span|p|br|a)\b", re.IGNORECASE)
_PLACEHOLDER_PLAIN_RE = re.compile(
    r"\b(?:view|open|see|read)\b.{0,40}\b(?:html|browser|web version|online)\b|"
    r"\b(?:html version|browser version)\b",
    re.IGNORECASE | re.DOTALL,
)


def _decode_base64_body(data: Any) -> str:
    try:
        raw = str(data or "")
        if not raw:
            return ""
        return base64.urlsafe_b64decode(
            raw + "=" * (-len(raw) % 4)
        ).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _collapse_body_whitespace(text: str) -> str:
    text = html_lib.unescape(str(text or "")).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _is_effective_plain_body(text: str) -> bool:
    cleaned = _collapse_body_whitespace(text)
    if not cleaned:
        return False
    if len(cleaned) < 40 and _PLACEHOLDER_PLAIN_RE.search(cleaned):
        return False
    return True


def _strip_html_tags_regex(html: str) -> str:
    """Best-effort HTML to readable text when optional parsers are unavailable."""
    html = str(html or "")
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)
    html = re.sub(r"<(style|script|noscript|head|meta|link)\b[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(
        r"<([a-z0-9]+)\b[^>]*style=[\"'][^\"']*display\s*:\s*none[^\"']*[\"'][^>]*>.*?</\1>",
        " ",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    def _replace_anchor(match: re.Match[str]) -> str:
        attrs = match.group(1) or ""
        body = match.group(2) or ""
        href_match = re.search(r"href\s*=\s*(['\"])(.*?)\1", attrs, flags=re.IGNORECASE | re.DOTALL)
        label = re.sub(r"<[^>]+>", " ", body)
        label = _collapse_body_whitespace(label)
        href = _collapse_body_whitespace(href_match.group(2)) if href_match else ""
        if href and label and href not in label:
            return f" {label} ({href}) "
        return f" {label or href} "

    html = re.sub(r"<a\b([^>]*)>(.*?)</a>", _replace_anchor, html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"</(?:p|div|tr|li|h[1-6]|blockquote|section|article|br)\s*>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<[^>]+>", " ", html)
    return _collapse_body_whitespace(html)


class _HTMLTextParser(HTMLParser):
    """Small stdlib HTML-to-text fallback that preserves link destinations."""

    _BLOCK_TAGS = {"p", "div", "br", "li", "tr", "table", "section", "article", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}
    _SKIP_CONTAINER_TAGS = {"style", "script", "noscript", "head"}
    _DROP_TAGS = {"meta", "link"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.anchor_href_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._DROP_TAGS:
            return
        if self.skip_depth:
            self.skip_depth += 1
            return
        if tag in self._SKIP_CONTAINER_TAGS:
            self.skip_depth += 1
            return
        attr_map = {name.lower(): value or "" for name, value in attrs}
        if "display:none" in re.sub(r"\s+", "", attr_map.get("style", "").lower()):
            self.skip_depth += 1
            return
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a":
            self.anchor_href_stack.append(attr_map.get("href", ""))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if tag == "a" and self.anchor_href_stack:
            href = self.anchor_href_stack.pop().strip()
            if href:
                self.parts.append(f" ({href})")
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth and data:
            self.parts.append(data)

    def text(self) -> str:
        return _collapse_body_whitespace("".join(self.parts))


def _html_to_text(html: str) -> str:
    html = str(html or "")
    if not html.strip():
        return ""

    cleaned_html = html
    try:
        from bs4 import BeautifulSoup  # type: ignore[import-not-found]

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(["style", "script", "noscript", "head", "meta", "link"]):
            tag.decompose()
        for tag in soup.find_all(style=re.compile(r"display\s*:\s*none", re.IGNORECASE)):
            tag.decompose()
        cleaned_html = str(soup)
    except Exception:
        cleaned_html = html

    try:
        import html2text  # type: ignore[import-not-found]

        converter = html2text.HTML2Text()
        converter.ignore_links = False
        converter.ignore_images = True
        converter.ignore_emphasis = False
        converter.body_width = 0
        converter.skip_internal_anchors = True
        converter.protect_links = True
        return _collapse_body_whitespace(converter.handle(cleaned_html))
    except Exception:
        pass

    try:
        parser = _HTMLTextParser()
        parser.feed(cleaned_html)
        parser.close()
        parsed = parser.text()
        if parsed:
            return parsed
    except Exception:
        pass
    return _strip_html_tags_regex(cleaned_html)


def _looks_like_html_body(text: str) -> bool:
    return bool(_HTML_RE.search(str(text or "")))


def decode_body_for_display(message: dict[str, Any]) -> dict[str, str]:
    """Decode original Gmail body parts for user display, separate from LLM-cleaned body_text."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType") or "")
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        data = body.get("data")
        if data and mime_type in {"text/plain", "text/html"}:
            decoded = _decode_base64_body(data)
            if decoded:
                if mime_type == "text/html":
                    html_parts.append(decoded)
                else:
                    plain_parts.append(decoded)
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    walk(payload)
    # Display HTML is budgeted later at the JSON-RPC response boundary. Keep a
    # larger source window here so newsletter layouts (Cloudflare, GitHub,
    # billing providers, etc.) are not cut mid-document and downgraded to text.
    # LLM-facing body_text remains independently capped by _BODY_TEXT_LIMIT.
    html_body = "\n\n".join(part.strip() for part in html_parts if part.strip())[:_DISPLAY_BODY_LIMIT]
    text_body = "\n\n".join(_collapse_body_whitespace(part) for part in plain_parts if part.strip())[:_DISPLAY_BODY_LIMIT]
    return {"html": html_body, "text": text_body}


def _safe_external_url(raw_url: str) -> str:
    url = html_lib.unescape(str(raw_url or "").strip())
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "mailto"}:
        return ""
    if scheme in {"http", "https"} and not parsed.netloc:
        return ""
    return urllib.parse.urlunparse(parsed._replace(scheme=scheme))


class _HTMLLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self._current_href = ""
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a" or self._current_href:
            return
        attr_map = {name.lower(): value or "" for name, value in attrs}
        href = _safe_external_url(attr_map.get("href", ""))
        if href:
            self._current_href = href
            self._current_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._current_href:
            return
        self.links.append({"url": self._current_href, "text": _collapse_body_whitespace("".join(self._current_text))})
        self._current_href = ""
        self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href and data:
            self._current_text.append(data)


_TEXT_URL_RE = re.compile(r"\bhttps?://[^\s<>\"]+|\bmailto:[^\s<>\"]+", re.IGNORECASE)


def extract_external_links_from_message(message: dict[str, Any], *, limit: int = 20) -> list[dict[str, str]]:
    """Return safe external links from display HTML/text as lightweight metadata."""
    display = decode_body_for_display(message)
    raw_html = str(display.get("html") or "")
    raw_text = str(display.get("text") or message.get("body_text") or "")
    seen: set[str] = set()
    results: list[dict[str, str]] = []

    def add(url: str, text: str, source: str) -> None:
        safe_url = _safe_external_url(url)
        if not safe_url or safe_url in seen or len(results) >= limit:
            return
        seen.add(safe_url)
        parsed = urllib.parse.urlparse(safe_url)
        host = parsed.netloc or parsed.scheme
        label = _collapse_body_whitespace(text)[:120] or host or safe_url[:120]
        digest = hashlib.sha1(safe_url.encode("utf-8", errors="ignore")).hexdigest()[:12]
        results.append({
            "id": f"link_{digest}",
            "url": safe_url,
            "text": label,
            "host": host,
            "source": source,
        })

    if raw_html.strip():
        try:
            parser = _HTMLLinkParser()
            parser.feed(raw_html)
            parser.close()
            for item in parser.links:
                add(item.get("url", ""), item.get("text", ""), "html")
        except Exception:
            pass

    for match in _TEXT_URL_RE.finditer(raw_text):
        url = match.group(0).rstrip(").,;!?'\"")
        add(url, url, "text")

    return results


def _attachment_token(message_id: str, attachment_id: str, index: int) -> str:
    raw = json.dumps({"m": message_id, "a": attachment_id, "i": index}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_attachment_token(token: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(str(token or "") + "=" * (-len(str(token or "")) % 4))
        payload = json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def attachment_metadata_from_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Return downloadable Gmail attachment metadata with opaque frontend IDs."""
    message_id = str(message.get("id") or "")
    result: list[dict[str, Any]] = []
    attachments = message.get("attachments") if isinstance(message.get("attachments"), list) else []
    for index, item in enumerate(attachments):
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "").strip()
        attachment_id = str(item.get("attachmentId") or "").strip()
        if not filename or not attachment_id:
            continue
        result.append({
            "id": _attachment_token(message_id, attachment_id, index),
            "message_id": message_id,
            "filename": filename[:240],
            "mime_type": str(item.get("mimeType") or "application/octet-stream")[:120],
            "size": int(item.get("size") or 0),
            "source": "gmail",
            "downloadable": True,
        })
    return result


def find_attachment_for_token(message: dict[str, Any], token: str) -> dict[str, Any]:
    payload = _decode_attachment_token(token)
    message_id = str(message.get("id") or "")
    if not payload or str(payload.get("m") or "") != message_id:
        raise ValueError("Attachment does not belong to this message")
    attachment_id = str(payload.get("a") or "")
    for item in attachment_metadata_from_message(message):
        decoded = _decode_attachment_token(str(item.get("id") or ""))
        if str(decoded.get("a") or "") == attachment_id:
            return {**item, "gmail_attachment_id": attachment_id}
    raise ValueError("Attachment not found")


def fetch_attachment_bytes(mailbox: str, message_id: str, gmail_attachment_id: str) -> bytes:
    """Fetch and decode one Gmail attachment body."""
    path = (
        f"/users/me/messages/{urllib.parse.quote(message_id, safe='')}"
        f"/attachments/{urllib.parse.quote(gmail_attachment_id, safe='')}"
    )
    payload = gmail_request(mailbox, path)
    data = str(payload.get("data") or "")
    if not data:
        raise ValueError("Gmail attachment response did not include data")
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode_body(message: dict[str, Any]) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType") or "")
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        data = body.get("data")
        if data and mime_type in {"text/plain", "text/html"}:
            decoded = _decode_base64_body(data)
            if not decoded:
                return
            if mime_type == "text/plain":
                plain_parts.append(decoded)
            else:
                html_parts.append(decoded)
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    walk(payload)

    effective_plain = [_collapse_body_whitespace(part) for part in plain_parts if _is_effective_plain_body(part)]
    if effective_plain:
        return "\n\n".join(part for part in effective_plain if part)[:_BODY_TEXT_LIMIT]

    html_text = _html_to_text("\n\n".join(html_parts))
    if html_text:
        return html_text[:_BODY_TEXT_LIMIT]

    fallback_plain = [_collapse_body_whitespace(part) for part in plain_parts if part.strip()]
    return "\n\n".join(part for part in fallback_plain if part)[:_BODY_TEXT_LIMIT]


def _extract_attachments(part: dict[str, Any]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        filename = str(node.get("filename") or "")
        body = node.get("body") if isinstance(node.get("body"), dict) else {}
        if filename:
            attachments.append({
                "filename": filename,
                "mimeType": node.get("mimeType"),
                "size": body.get("size"),
                "attachmentId": body.get("attachmentId"),
            })
        for child in node.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(part)
    return attachments


def _normalize_message(mailbox: str, message: dict[str, Any]) -> dict[str, Any]:
    headers = _header_map(message)
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "mailbox": mailbox,
        "history_id": message.get("historyId"),
        "internal_date": message.get("internalDate"),
        "date": headers.get("date", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "bcc": headers.get("bcc", ""),
        "subject": headers.get("subject", ""),
        "message_id": headers.get("message-id", ""),
        "in_reply_to": headers.get("in-reply-to", ""),
        "references": headers.get("references", ""),
        "label_ids": message.get("labelIds") or [],
        "snippet": message.get("snippet") or "",
        "size_estimate": message.get("sizeEstimate"),
        "mime_type": payload.get("mimeType"),
        "attachments": _extract_attachments(payload),
        "body_text": _decode_body(message),
        "raw_headers": headers,
        "payload": payload,
        "fetched_at": beijing_now(),
    }


# ── Gmail live search ─────────────────────────────────────────────

def search_gmail(
    mailbox: str,
    query: str,
    max_results: int = 100,
    *,
    access_token: str | None = None,
) -> list[str]:
    """Search Gmail with a query string, return list of message IDs."""
    target = max(1, min(int(max_results or 100), 500))
    message_ids: list[str] = []
    page_token = ""
    while len(message_ids) < target:
        params: dict[str, Any] = {
            "q": query,
            "maxResults": min(100, target - len(message_ids)),
            "fields": "messages/id,nextPageToken",
        }
        if "in:anywhere" in query.lower():
            params["includeSpamTrash"] = "true"
        if page_token:
            params["pageToken"] = page_token
        try:
            payload = gmail_request(mailbox, "/users/me/messages", params, access_token=access_token)
        except ValueError as exc:
            logging.getLogger("mail_agent.gmail").warning("search_gmail failed for %s: %s", mailbox, exc)
            return message_ids
        refs = payload.get("messages") if isinstance(payload, dict) else []
        for ref in refs or []:
            if isinstance(ref, dict) and ref.get("id"):
                message_ids.append(str(ref["id"]))
        page_token = str(payload.get("nextPageToken") or "") if isinstance(payload, dict) else ""
        if not page_token or not refs:
            break
    return message_ids[:target]


def _is_at_or_before_stop_time(message: dict[str, Any], stop_internal_date: str) -> bool:
    if not stop_internal_date:
        return False
    try:
        return int(message.get("internal_date") or 0) <= int(stop_internal_date)
    except (TypeError, ValueError):
        return False


# ── Thread-based fetch (paginated, 50 threads per invoke) ────────────

THREAD_PAGE_SIZE = 50


def list_threads_page(
    mailbox: str,
    page_token: str | None = None,
    max_results: int = THREAD_PAGE_SIZE,
    query: str = "-in:chats",
) -> dict[str, Any]:
    """List one page of threads from Gmail, newest first. 50 threads per page."""
    query_params: dict[str, Any] = {
        "q": query or "-in:chats",
        "maxResults": min(max_results, 100),
        "fields": "threads(id,snippet,historyId),nextPageToken,resultSizeEstimate",
    }
    if page_token:
        query_params["pageToken"] = page_token
    return gmail_request(mailbox, "/users/me/threads", query_params)


def fetch_thread_full(mailbox: str, thread_id: str) -> dict[str, Any]:
    """Fetch the full Gmail thread with all messages."""
    import urllib.parse as _up
    return gmail_request(
        mailbox,
        f"/users/me/threads/{_up.quote(str(thread_id), safe='')}",
        {"format": "full", "fields": "messages(id,threadId,historyId,labelIds,internalDate,payload(parts,headers,body,mimeType),snippet)"},
    )


def extract_messages_from_thread(thread: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract message dicts from a full Gmail thread response, newest first."""
    msgs = thread.get("messages") if isinstance(thread, dict) else []
    if not isinstance(msgs, list):
        return []
    # Sort newest first
    return sorted(msgs, key=lambda m: int(m.get("internalDate") or 0), reverse=True)


def fetch_and_cache_message(mailbox: str, message_id: str) -> dict[str, Any] | None:
    """Fetch a full Gmail message and cache it locally. Returns the normalized message dict."""
    try:
        full = gmail_request(
            mailbox,
            f"/users/me/messages/{urllib.parse.quote(message_id, safe='')}",
            {"format": "full"},
        )
    except ValueError:
        return None
    normalized = _normalize_message(mailbox, full)
    write_message(mailbox, normalized)
    return normalized


def fetch_message_summary(
    mailbox: str,
    message_id: str,
    *,
    access_token: str | None = None,
    strict: bool = False,
) -> dict[str, Any] | None:
    """Fetch headers, labels and attachment metadata without downloading bodies."""
    metadata_headers = [
        "Date",
        "From",
        "To",
        "Cc",
        "Bcc",
        "Subject",
        "Message-Id",
        "In-Reply-To",
        "References",
    ]
    try:
        payload = gmail_request(
            mailbox,
            f"/users/me/messages/{urllib.parse.quote(message_id, safe='')}",
            {
                "format": "metadata",
                "metadataHeaders": metadata_headers,
                "fields": (
                    "id,threadId,historyId,labelIds,internalDate,snippet,sizeEstimate,"
                    "payload(headers(name,value),mimeType,filename,body(attachmentId,size),parts(filename,mimeType,body(attachmentId,size)))"
                ),
            },
            access_token=access_token,
        )
    except GmailApiError as exc:
        if strict:
            raise
        if exc.status_code != 404:
            logging.getLogger("mail_agent.gmail").warning("metadata fetch failed for %s: HTTP %s", message_id, exc.status_code)
        return None
    except ValueError:
        if strict:
            raise
        return None
    headers = _header_map(payload)
    message_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    return {
        "id": payload.get("id"),
        "thread_id": payload.get("threadId"),
        "mailbox": mailbox,
        "history_id": payload.get("historyId"),
        "internal_date": payload.get("internalDate"),
        "date": headers.get("date", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "bcc": headers.get("bcc", ""),
        "subject": headers.get("subject", ""),
        "message_id": headers.get("message-id", ""),
        "in_reply_to": headers.get("in-reply-to", ""),
        "references": headers.get("references", ""),
        "label_ids": payload.get("labelIds") or [],
        "snippet": payload.get("snippet") or "",
        "size_estimate": payload.get("sizeEstimate"),
        "mime_type": message_payload.get("mimeType"),
        "attachments": _extract_attachments(message_payload),
        "fetched_at": beijing_now(),
        "body_preview": "",
        "body_length": 0,
        "raw_header_count": len(headers),
        "body_cached": False,
        "headers_complete": True,
        "metadata_refreshed_at": int(time.time()),
    }


def live_search_metadata_and_cache(
    mailbox: str,
    query: str,
    max_results: int = 100,
    *,
    access_token: str | None = None,
) -> list[str]:
    """Search Gmail and cache compact summaries, leaving full bodies on demand."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 正式环境的 getToken 是 Host 反向 RPC。首页最多并发拉取数百封摘要，
    # 若每个 Gmail 请求各自取 token，会把一次刷新放大为数百次跨进程往返。
    # token 仅保留在当前函数及其 worker 上下文中，函数返回即释放，绝不写入缓存或日志。
    with _gmail_request_token_scope(access_token):
        # 使用 ContextVar 而不是给公开 helper 新增必填参数；线程池任务通过 copy_context
        # 显式继承本次 token，既避免凭据 RPC 风暴，也不影响其他并发 mailbox 请求。
        msg_ids = search_gmail(mailbox, query, max_results)
        if not msg_ids:
            return []

        existing = read_cache(mailbox)
        existing_by_id = {
            str(item.get("id")): item
            for item in existing.get("messages") or []
            if isinstance(item, dict) and item.get("id")
        }
        refresh_before = int(time.time()) - SUMMARY_METADATA_REFRESH_SECONDS
        missing_ids = [
            message_id
            for message_id in msg_ids
            if message_id not in existing_by_id
            or (not existing_by_id[message_id].get("from") and not existing_by_id[message_id].get("headers_complete"))
            or int(existing_by_id[message_id].get("metadata_refreshed_at") or 0) <= refresh_before
        ]
        if missing_ids:
            worker_context = copy_context()
            with ThreadPoolExecutor(max_workers=SUMMARY_FETCH_MAX_WORKERS) as pool:
                futures = {
                    pool.submit(worker_context.copy().run, fetch_message_summary, mailbox, message_id): message_id
                    for message_id in missing_ids
                }
                for future in as_completed(futures):
                    try:
                        summary = future.result()
                    except Exception:
                        summary = None
                    if summary:
                        existing_by_id[str(summary.get("id") or futures[future])] = summary

        returned_ids = [message_id for message_id in msg_ids if message_id in existing_by_id]
        merged = sorted(existing_by_id.values(), key=lambda item: int(item.get("internal_date") or 0), reverse=True)
        write_index(mailbox, merged)
        return returned_ids


def _delete_cached_message(mailbox: str, message_id: str) -> None:
    """删除已永久不存在邮件的正文缓存；索引删除由调用方集中提交。"""
    if _storage_cache_enabled():
        _storage_delete_value_sync(_storage_message_key(mailbox, message_id))
        return
    path = _message_path(mailbox, message_id)
    if path.exists():
        path.unlink()


def sync_cached_mailbox_history(mailbox: str) -> dict[str, Any]:
    """用 Gmail History API 增量合并第三方客户端产生的邮件状态变化。

    此函数只读取 Gmail 并更新本邮箱缓存。任意 History 或 metadata 请求失败时
    不写新 cursor，下一次会从同一个已确认 cursor 重试，不能把半同步状态标为成功。
    """
    normalized = normalize_mailbox(mailbox)
    with _history_sync_lock(normalized):
        state = _read_history_sync_state(normalized)
        start_history_id = str(state.get("history_id") or "")
        cache = read_cache(normalized)
        cached_messages = cache.get("messages") if isinstance(cache.get("messages"), list) else []
        if not start_history_id or not cached_messages:
            return {
                "mailbox": normalized,
                "mode": "baseline_required",
                "added": 0,
                "updated": 0,
                "deleted": 0,
                "cache_total": len(cached_messages),
                "resync_required": True,
                "resync_reason": "cursor_missing",
                "updated_at": beijing_now(),
            }

        changed_ids: set[str] = set()
        deleted_ids: set[str] = set()
        page_token = ""
        final_history_id = ""
        pages = 0
        try:
            while True:
                pages += 1
                if pages > 100:
                    raise RuntimeError("Gmail History pagination exceeded the 100-page safety limit")
                params: dict[str, Any] = {
                    "startHistoryId": start_history_id,
                    "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
                    "fields": "history(id,messagesAdded(message/id),messagesDeleted(message/id),labelsAdded(message/id),labelsRemoved(message/id)),nextPageToken,historyId",
                }
                if page_token:
                    params["pageToken"] = page_token
                payload = gmail_request(normalized, "/users/me/history", params)
                final_history_id = str(payload.get("historyId") or final_history_id)
                history_items = payload.get("history") if isinstance(payload.get("history"), list) else []
                for item in history_items:
                    if not isinstance(item, dict):
                        continue
                    for key in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                        for entry in item.get(key) or []:
                            message = entry.get("message") if isinstance(entry, dict) else {}
                            message_id = str(message.get("id") or "") if isinstance(message, dict) else ""
                            if message_id:
                                changed_ids.add(message_id)
                                deleted_ids.discard(message_id)
                    for entry in item.get("messagesDeleted") or []:
                        message = entry.get("message") if isinstance(entry, dict) else {}
                        message_id = str(message.get("id") or "") if isinstance(message, dict) else ""
                        if message_id:
                            deleted_ids.add(message_id)
                            changed_ids.discard(message_id)
                page_token = str(payload.get("nextPageToken") or "")
                if not page_token:
                    break
        except GmailApiError as exc:
            if exc.status_code == 404:
                return {
                    "mailbox": normalized,
                    "mode": "history_expired",
                    "added": 0,
                    "updated": 0,
                    "deleted": 0,
                    "cache_total": len(cached_messages),
                    "resync_required": True,
                    "resync_reason": "history_expired",
                    "updated_at": beijing_now(),
                }
            raise

        by_id = {
            str(item.get("id") or ""): dict(item)
            for item in cached_messages
            if isinstance(item, dict) and item.get("id")
        }
        added = 0
        updated = 0
        for message_id in sorted(changed_ids):
            try:
                summary = fetch_message_summary(normalized, message_id, strict=True)
            except GmailApiError as exc:
                if exc.status_code == 404:
                    deleted_ids.add(message_id)
                    continue
                raise
            if not summary:
                raise RuntimeError(f"Gmail returned no metadata for changed message {message_id}")
            if message_id in by_id:
                # 保留已有摘要中正文缓存、原始主题等本地字段，只覆盖 Gmail 权威 metadata。
                by_id[message_id] = {**by_id[message_id], **summary}
                updated += 1
            else:
                by_id[message_id] = summary
                added += 1

        for message_id in deleted_ids:
            by_id.pop(message_id, None)
            _delete_cached_message(normalized, message_id)

        merged = sorted(by_id.values(), key=_internal_date_sort_key, reverse=True)
        cache_error_count = len(_aps_cache_errors)
        write_index(normalized, merged)
        if len(_aps_cache_errors) != cache_error_count:
            raise RuntimeError("Gmail cache index write failed; History cursor was not advanced")
        _write_history_sync_state(normalized, {
            "history_id": final_history_id or start_history_id,
            "scope_days": int(state.get("scope_days") or 30),
        }, if_match=str(state.get("_etag") or "") or None)
        return {
            "mailbox": normalized,
            "mode": "history",
            "history_id": final_history_id or start_history_id,
            "added": added,
            "updated": updated,
            "deleted": len(deleted_ids),
            "cache_total": len(merged),
            "resync_required": False,
            "updated_at": beijing_now(),
        }


def refresh_thread_cache(mailbox: str, thread_id: str) -> list[dict[str, Any]]:
    """Fetch a Gmail thread, normalize/cache all messages, and update the index."""
    normalized_mailbox = normalize_mailbox(mailbox)
    full = fetch_thread_full(normalized_mailbox, thread_id)
    raw_msgs = extract_messages_from_thread(full)
    normalized_msgs: list[dict[str, Any]] = []
    for msg in raw_msgs:
        n = _normalize_message(normalized_mailbox, msg)
        write_message(normalized_mailbox, n)
        normalized_msgs.append(n)

    if normalized_msgs:
        cache = read_cache(normalized_mailbox)
        by_id: dict[str, dict[str, Any]] = {}
        for item in cache.get("messages") or []:
            if isinstance(item, dict) and item.get("id"):
                by_id[str(item["id"])] = item
        for msg in normalized_msgs:
            by_id[str(msg.get("id") or "")] = message_summary(msg)
        merged = sorted(by_id.values(), key=lambda item: int(item.get("internal_date") or 0), reverse=True)
        write_index(normalized_mailbox, merged)

    return normalized_msgs


def patch_cached_messages_read(mailbox: str, message_ids: list[str]) -> None:
    """Best-effort local cache patch after Gmail UNREAD labels are removed."""
    normalized_mailbox = normalize_mailbox(mailbox)
    target_ids = {str(mid) for mid in message_ids if str(mid)}
    if not target_ids:
        return

    changed_summaries: dict[str, dict[str, Any]] = {}
    for mid in target_ids:
        try:
            msg = read_message(normalized_mailbox, mid)
        except Exception:
            continue
        labels = [str(label) for label in (msg.get("label_ids") or []) if str(label).upper() != "UNREAD"]
        msg["label_ids"] = labels
        write_message(normalized_mailbox, msg)
        changed_summaries[mid] = message_summary(msg)

    if not changed_summaries:
        return

    cache = read_cache(normalized_mailbox)
    summaries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in cache.get("messages") or []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "")
        if not mid:
            continue
        summaries.append(changed_summaries.get(mid, item))
        seen.add(mid)
    for mid, summary in changed_summaries.items():
        if mid not in seen:
            summaries.append(summary)
    summaries.sort(key=lambda item: int(item.get("internal_date") or 0), reverse=True)
    write_index(normalized_mailbox, summaries)


def patch_cached_message_labels(
    mailbox: str,
    message_ids: list[str],
    *,
    add_label_ids: list[str] | None = None,
    remove_label_ids: list[str] | None = None,
) -> None:
    """Best-effort local cache patch after Gmail label modification."""
    normalized_mailbox = normalize_mailbox(mailbox)
    target_ids = {str(mid).strip() for mid in message_ids if str(mid).strip()}
    if not target_ids:
        return

    add_set = {str(label).strip().upper() for label in (add_label_ids or []) if str(label).strip()}
    remove_set = {str(label).strip().upper() for label in (remove_label_ids or []) if str(label).strip()}
    changed_summaries: dict[str, dict[str, Any]] = {}

    for mid in target_ids:
        try:
            msg = read_message(normalized_mailbox, mid)
        except Exception:
            continue
        labels = {str(label).strip().upper() for label in (msg.get("label_ids") or []) if str(label).strip()}
        labels.update(add_set)
        labels.difference_update(remove_set)
        msg["label_ids"] = sorted(labels)
        write_message(normalized_mailbox, msg)
        changed_summaries[mid] = message_summary(msg)

    if not changed_summaries:
        return

    cache = read_cache(normalized_mailbox)
    summaries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in cache.get("messages") or []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "")
        if not mid:
            continue
        summaries.append(changed_summaries.get(mid, item))
        seen.add(mid)
    for mid, summary in changed_summaries.items():
        if mid not in seen:
            summaries.append(summary)
    summaries.sort(key=lambda item: int(item.get("internal_date") or 0), reverse=True)
    write_index(normalized_mailbox, summaries)


_FETCH_WORKERS = 6


def live_search_and_cache(
    mailbox: str,
    query: str,
    max_results: int = 100,
    *,
    stop_at_internal_date: str = "",
) -> list[str]:
    """Search Gmail, fetch+cache new messages, return list of message IDs.

    Uses ThreadPoolExecutor to fetch uncached messages concurrently.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    msg_ids = search_gmail(mailbox, query, max_results)
    if not msg_ids:
        return []

    existing = read_cache(mailbox)
    existing_by_id: dict[str, dict[str, Any]] = {
        str(item.get("id")): item for item in existing["messages"] if item.get("id")
    }
    cached_ids = set(existing_by_id.keys())

    # Split into cached (process in order) and uncached (fetch concurrently)
    uncached_to_fetch: list[str] = []
    ordered: list[dict[str, Any]] = []
    for msg_id in msg_ids:
        if msg_id in cached_ids:
            ordered.append(existing_by_id[msg_id])
        else:
            uncached_to_fetch.append(msg_id)
            ordered.append({})  # placeholder, filled after concurrent fetch

    # Fetch uncached messages concurrently
    if uncached_to_fetch:
        fetched: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
            future_to_mid = {pool.submit(fetch_and_cache_message, mailbox, mid): mid for mid in uncached_to_fetch}
            for future in as_completed(future_to_mid):
                mid = future_to_mid[future]
                try:
                    result = future.result()
                    if result:
                        fetched[mid] = message_summary(result)
                except Exception:
                    pass

        # Fill placeholders with fetched results
        for i, entry in enumerate(ordered):
            if isinstance(entry, dict) and not entry:
                mid = msg_ids[i]
                if mid in fetched:
                    ordered[i] = fetched[mid]

    # Build returned IDs respecting stop time boundary
    returned_ids: list[str] = []
    for mid, msg in zip(msg_ids, ordered):
        if isinstance(msg, dict) and msg:
            if _is_at_or_before_stop_time(msg, stop_at_internal_date):
                break
            returned_ids.append(mid)

    # Update index
    all_cached = dict(existing_by_id)
    for msg in ordered:
        if isinstance(msg, dict) and msg:
            all_cached[str(msg.get("id"))] = msg

    merged = sorted(all_cached.values(), key=lambda item: int(item.get("internal_date") or 0), reverse=True)
    write_index(mailbox, merged)
    return returned_ids


# ── MessageLite / MessageDetail / ThreadContext ────────────────────

def _to_message_lite(msg: dict[str, Any]) -> MessageLite:
    """Convert cached message summary to MessageLite."""
    return MessageLite(
        message_id=str(msg.get("id") or ""),
        thread_id=str(msg.get("thread_id") or ""),
        from_addr=str(msg.get("from") or ""),
        to_addr=str(msg.get("to") or ""),
        cc=str(msg.get("cc") or ""),
        subject=str(msg.get("subject") or ""),
        snippet=str(msg.get("snippet") or ""),
        internal_date=str(msg.get("internal_date") or ""),
        label_ids=[str(l) for l in (msg.get("label_ids") or [])],
        unread="UNREAD" in str(msg.get("label_ids") or "").upper(),
        starred="STARRED" in str(msg.get("label_ids") or "").upper(),
        important="IMPORTANT" in str(msg.get("label_ids") or "").upper(),
        has_attachment=bool(msg.get("attachments") and len(msg.get("attachments") or []) > 0),
        attachments=attachment_metadata_from_message(msg),
        headers={
            "list_unsubscribe": str(msg.get("raw_headers", {}).get("list-unsubscribe", "")),
            "list_id": str(msg.get("raw_headers", {}).get("list-id", "")),
            "auto_submitted": str(msg.get("raw_headers", {}).get("auto-submitted", "")),
            "precedence": str(msg.get("raw_headers", {}).get("precedence", "")),
        },
    )


def get_messages_lite(mailbox: str, message_ids: list[str]) -> list[MessageLite]:
    results: list[MessageLite] = []
    for msg_id in message_ids:
        try:
            msg = read_message(mailbox, msg_id)
        except (ValueError, RuntimeError, TimeoutError, json.JSONDecodeError):
            continue
        if isinstance(msg, dict):
            results.append(_to_message_lite(msg))
    return results


async def get_messages_lite_async(mailbox: str, message_ids: list[str]) -> list[MessageLite]:
    if not _storage_cache_enabled():
        return get_messages_lite(mailbox, message_ids)

    results: list[MessageLite] = []
    for msg_id in message_ids:
        try:
            msg = await _storage_get_value_async(_storage_message_key(mailbox, msg_id))
        except Exception:
            msg = None
        if isinstance(msg, dict):
            results.append(_to_message_lite(msg))
    return results


def get_message_detail(mailbox: str, message_id: str) -> MessageDetail | None:
    try:
        msg = read_message(mailbox, message_id)
    except (ValueError, RuntimeError, TimeoutError, json.JSONDecodeError):
        return None
    if not isinstance(msg, dict):
        return None

    body_text = str(msg.get("body_text") or "")
    return _to_message_detail(msg, body_text)


def _to_message_detail(msg: dict[str, Any], body_text: str | None = None) -> MessageDetail:
    body_text = str(msg.get("body_text") or "") if body_text is None else body_text
    if _looks_like_html_body(body_text) and isinstance(msg.get("payload"), dict):
        decoded = _decode_body(msg)
        if decoded:
            body_text = decoded
    return MessageDetail(
        message_id=str(msg.get("id") or ""),
        thread_id=str(msg.get("thread_id") or ""),
        from_addr=str(msg.get("from") or ""),
        to_addr=str(msg.get("to") or ""),
        cc=str(msg.get("cc") or ""),
        subject=str(msg.get("subject") or ""),
        snippet=str(msg.get("snippet") or ""),
        internal_date=str(msg.get("internal_date") or ""),
        label_ids=[str(l) for l in (msg.get("label_ids") or [])],
        unread="UNREAD" in str(msg.get("label_ids") or "").upper(),
        starred="STARRED" in str(msg.get("label_ids") or "").upper(),
        important="IMPORTANT" in str(msg.get("label_ids") or "").upper(),
        has_attachment=bool(msg.get("attachments") and len(msg.get("attachments") or []) > 0),
        headers={
            "list_unsubscribe": str(msg.get("raw_headers", {}).get("list-unsubscribe", "")),
            "list_id": str(msg.get("raw_headers", {}).get("list-id", "")),
            "auto_submitted": str(msg.get("raw_headers", {}).get("auto-submitted", "")),
            "precedence": str(msg.get("raw_headers", {}).get("precedence", "")),
        },
        body_text=body_text,
    )


async def get_message_detail_async(mailbox: str, message_id: str) -> MessageDetail | None:
    try:
        msg = await read_message_async(mailbox, message_id)
    except (ValueError, RuntimeError, TimeoutError, json.JSONDecodeError):
        return None
    if not isinstance(msg, dict):
        return None
    return _to_message_detail(msg)


def get_thread_context(mailbox: str, thread_id: str, max_messages: int = 10) -> ThreadContext:
    all_msgs = list_messages(mailbox)
    thread_msgs: list[MessageDetail] = []
    for summary in all_msgs:
        if str(summary.get("thread_id") or "") == thread_id:
            detail = get_message_detail(mailbox, str(summary.get("id") or ""))
            if detail:
                thread_msgs.append(detail)
        if len(thread_msgs) >= max(1, max_messages):
            break

    thread_msgs.sort(key=lambda m: m.internal_date)
    return ThreadContext(thread_id=thread_id, messages=thread_msgs)


async def get_thread_context_async(mailbox: str, thread_id: str, max_messages: int = 10) -> ThreadContext:
    if not _storage_cache_enabled():
        return get_thread_context(mailbox, thread_id, max_messages=max_messages)

    index = await _storage_get_value_async(_storage_index_key(mailbox))
    all_msgs = index.get("messages") if isinstance(index, dict) and isinstance(index.get("messages"), list) else []
    thread_msgs: list[MessageDetail] = []
    for summary in all_msgs:
        if str(summary.get("thread_id") or "") == thread_id:
            detail = await get_message_detail_async(mailbox, str(summary.get("id") or ""))
            if detail:
                thread_msgs.append(detail)
        if len(thread_msgs) >= max(1, max_messages):
            break

    thread_msgs.sort(key=lambda m: m.internal_date)
    return ThreadContext(thread_id=thread_id, messages=thread_msgs)


# ── Send reply via Gmail API ────────────────────────────────────────

def send_compose_email(mailbox: str, recipients: list[str], subject: str, body: str) -> dict[str, Any]:
    """Send a plain-text compose message after an explicit frontend confirmation."""
    from email.mime.text import MIMEText
    import base64 as b64

    normalized_recipients = [str(item).strip() for item in recipients if str(item).strip()]
    if not normalized_recipients:
        raise ValueError("At least one recipient is required")
    if not str(subject).strip() or not str(body).strip():
        raise ValueError("Subject and content are required")

    msg = MIMEText(str(body), "plain", "utf-8")
    msg["To"] = ", ".join(normalized_recipients)
    msg["Subject"] = str(subject)
    raw_b64 = b64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    request = urllib.request.Request(
        f"{GMAIL_API_BASE}/users/me/messages/send",
        data=json.dumps({"raw": raw_b64}).encode("utf-8"),
        headers={"Authorization": f"Bearer {get_access_token(mailbox)}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Gmail compose send failed: HTTP {exc.code}") from exc
    return {"id": str(result.get("id") or ""), "thread_id": str(result.get("threadId") or "")}


def search_contacts(mailbox: str, query: str, *, limit: int = 10) -> dict[str, Any]:
    """Search both Google contact collections without persisting the directory."""
    normalized_query = str(query or "").strip()
    if not normalized_query:
        return {"contacts": []}
    token = get_access_token(normalize_mailbox(mailbox))
    page_size = max(1, min(int(limit or 10), 25))
    params = {"query": normalized_query, "readMask": "names,emailAddresses,photos", "pageSize": page_size}
    contacts: list[dict[str, Any]] = []
    permission_required = False
    try:
        payload = _people_api_get(token, "people:searchContacts", params)
        candidates = payload.get("results") or []
        for item in candidates:
            person = item.get("person") if isinstance(item, dict) else {}
            if isinstance(person, dict):
                contacts.append(person)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            permission_required = True
        else:
            raise ValueError(f"Google contacts search failed: HTTP {exc.code}") from exc
    try:
        payload = _people_api_get(token, "otherContacts:search", params)
        candidates = payload.get("results") or []
        for item in candidates:
            person = item.get("person") if isinstance(item, dict) else {}
            if isinstance(person, dict):
                contacts.append(person)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            permission_required = True
        else:
            raise ValueError(f"Google other contacts search failed: HTTP {exc.code}") from exc

    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for person in contacts:
        names = person.get("names") if isinstance(person.get("names"), list) else []
        emails = person.get("emailAddresses") if isinstance(person.get("emailAddresses"), list) else []
        photos = person.get("photos") if isinstance(person.get("photos"), list) else []
        name = str((names[0] if names else {}).get("displayName") or "") if isinstance(names[0] if names else {}, dict) else ""
        photo_url = str((photos[0] if photos else {}).get("url") or "") if isinstance(photos[0] if photos else {}, dict) else ""
        for email_item in emails:
            email = str(email_item.get("value") or "").strip().lower() if isinstance(email_item, dict) else ""
            if not email or email in seen:
                continue
            seen.add(email)
            result.append({"email": email, "name": name, "avatar_url": photo_url})
            if len(result) >= page_size:
                break
        if len(result) >= page_size:
            break
    return {
        "contacts": result,
        "permission_required": permission_required,
        "required_scopes": [
            "https://www.googleapis.com/auth/contacts.readonly",
            "https://www.googleapis.com/auth/contacts.other.readonly",
        ] if permission_required else [],
    }

def send_reply(
    mailbox: str,
    thread_id: str,
    to_addr: str,
    body: str,
    *,
    reply_mode: str = "reply_to_sender",
    cc_addr: str = "",
) -> dict[str, Any]:
    """Send a reply email via Gmail API.

    WARNING: This performs a real send. Callers should default to dry_run=True
    and only call this function after explicit user confirmation.
    """
    from email.mime.text import MIMEText
    import base64 as b64

    # Fetch the original message to get Message-ID and subject for threading
    thread_ctx = get_thread_context(mailbox, thread_id, max_messages=1)
    original_msg_id = ""
    original_subject = "Re: "
    if thread_ctx.messages:
        latest = thread_ctx.messages[-1]
        original_subject = latest.subject or ""
        if not original_subject.lower().startswith("re:"):
            original_subject = f"Re: {original_subject}"
        # Try to get Message-ID from headers
        try:
            raw_msg = get_message_detail(mailbox, latest.message_id)
            if raw_msg and raw_msg.headers:
                for h in raw_msg.headers:
                    if h.lower() == "message-id":
                        original_msg_id = raw_msg.headers[h]
                        break
        except Exception:
            pass

    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to_addr
    if reply_mode == "reply_all" and cc_addr:
        msg["Cc"] = cc_addr
    msg["Subject"] = original_subject
    if original_msg_id:
        msg["In-Reply-To"] = original_msg_id
        msg["References"] = original_msg_id

    raw_bytes = msg.as_bytes()
    raw_b64 = b64.urlsafe_b64encode(raw_bytes).decode("ascii")

    token = get_access_token(mailbox)
    url = f"{GMAIL_API_BASE}/users/me/messages/send"
    payload = json.dumps({"raw": raw_b64, "threadId": thread_id}).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
            detail_json = json.loads(detail) if detail else {"status_code": exc.code}
        except Exception:
            detail_json = {"status_code": exc.code}
        import sys as _sys
        print(f"[send_reply] Gmail API error: {exc.code} {detail_json}", file=_sys.stderr)
        raise ValueError(f"Gmail API send failed: {exc.code} {detail_json}") from exc

    import sys as _sys
    result = json.loads(raw) if raw else {}
    print(f"[send_reply] Gmail API success: id={result.get('id', '?')[:20]} threadId={result.get('threadId', '?')}", file=_sys.stderr)
    return result


# ── Trash email via Gmail API ──────────────────────────────────────

def trash_email(mailbox: str, message_id: str) -> dict[str, Any]:
    """Move a message to trash via Gmail API."""
    import sys as _sys
    token = get_access_token(mailbox)
    url = f"{GMAIL_API_BASE}/users/me/messages/{urllib.parse.quote(message_id, safe='')}/trash"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
            detail_json = json.loads(detail) if detail else {"status_code": exc.code}
        except Exception:
            detail_json = {"status_code": exc.code}
        print(f"[trash_email] Gmail API error: {exc.code} {detail_json}", file=_sys.stderr)
        raise ValueError(f"Gmail trash failed: {exc.code} {detail_json}") from exc
    result = json.loads(raw) if raw else {}
    print(f"[trash_email] success: id={result.get('id', '?')[:20]}", file=_sys.stderr)
    return result


THREAD_STATE_OPERATIONS: dict[str, tuple[list[str], list[str]]] = {
    "mark_read": ([], ["UNREAD"]),
    "mark_unread": (["UNREAD"], []),
    "star": (["STARRED"], []),
    "unstar": ([], ["STARRED"]),
    "mark_important": (["IMPORTANT"], []),
    "mark_not_important": ([], ["IMPORTANT"]),
}


def update_thread_state(mailbox: str, thread_id: str, operation: str) -> dict[str, Any]:
    """Apply one guarded Gmail state transition to an entire thread."""
    normalized_operation = str(operation or "").strip().lower()
    if normalized_operation not in {*THREAD_STATE_OPERATIONS, "trash", "untrash"}:
        raise ValueError(f"Unsupported thread state operation: {normalized_operation}")

    token = get_access_token(mailbox)
    encoded_thread_id = urllib.parse.quote(str(thread_id).strip(), safe="")
    if not encoded_thread_id:
        raise ValueError("thread_id is required")

    if normalized_operation in {"trash", "untrash"}:
        url = f"{GMAIL_API_BASE}/users/me/threads/{encoded_thread_id}/{normalized_operation}"
        body = None
    else:
        add_labels, remove_labels = THREAD_STATE_OPERATIONS[normalized_operation]
        url = f"{GMAIL_API_BASE}/users/me/threads/{encoded_thread_id}/modify"
        body = json.dumps({
            "addLabelIds": add_labels,
            "removeLabelIds": remove_labels,
        }).encode("utf-8")

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
            detail_json = json.loads(detail) if detail else {"status_code": exc.code}
        except Exception:
            detail_json = {"status_code": exc.code}
        raise ValueError(f"Gmail thread {normalized_operation} failed: {exc.code} {detail_json}") from exc

    result = json.loads(raw) if raw else {"id": thread_id, "messages": []}
    message_ids = [
        str(item.get("id") or "")
        for item in (result.get("messages") or [])
        if isinstance(item, dict) and item.get("id")
    ]
    if normalized_operation in THREAD_STATE_OPERATIONS:
        add_labels, remove_labels = THREAD_STATE_OPERATIONS[normalized_operation]
    elif normalized_operation == "trash":
        add_labels, remove_labels = ["TRASH"], ["INBOX"]
    else:
        add_labels, remove_labels = [], ["TRASH"]
    patch_cached_message_labels(
        mailbox,
        message_ids,
        add_label_ids=add_labels,
        remove_label_ids=remove_labels,
    )
    return {
        "thread_id": thread_id,
        "operation": normalized_operation,
        "message_ids": message_ids,
    }


def modify_message_labels(
    mailbox: str,
    message_ids: list[str],
    *,
    add_label_ids: list[str] | None = None,
    remove_label_ids: list[str] | None = None,
    allowlist: set[str] | None = None,
) -> dict[str, Any]:
    """Modify Gmail system labels for one or more messages with an allowlist guard."""
    ids = [str(mid).strip() for mid in message_ids if str(mid).strip()]
    if not ids:
        raise ValueError("message_ids is required")

    add_set = {str(label).strip().upper() for label in (add_label_ids or []) if str(label).strip()}
    remove_set = {str(label).strip().upper() for label in (remove_label_ids or []) if str(label).strip()}
    # 收件箱批量栏需要 STARRED / INBOX / TRASH；自定义用户标签仍须显式传入 allowlist
    default_allow = {"UNREAD", "IMPORTANT", "STARRED", "INBOX", "TRASH"}
    effective_allowlist = {
        str(label).strip().upper()
        for label in (allowlist or default_allow)
        if str(label).strip()
    }
    disallowed = sorted((add_set | remove_set) - effective_allowlist)
    if disallowed:
        raise ValueError(f"Only these labels can be modified: {', '.join(sorted(effective_allowlist))}")

    token = get_access_token(mailbox)
    body = json.dumps({
        "ids": ids,
        "addLabelIds": sorted(add_set),
        "removeLabelIds": sorted(remove_set),
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{GMAIL_API_BASE}/users/me/messages/batchModify",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
            detail_json = json.loads(detail) if detail else {"status_code": exc.code}
        except Exception:
            detail_json = {"status_code": exc.code}
        raise ValueError(f"Gmail batchModify failed: {exc.code} {detail_json}") from exc

    patch_cached_message_labels(
        mailbox,
        ids,
        add_label_ids=sorted(add_set),
        remove_label_ids=sorted(remove_set),
    )
    return {
        "message_ids": ids,
        "add_label_ids": sorted(add_set),
        "remove_label_ids": sorted(remove_set),
        "result": json.loads(raw) if raw else {},
    }


def set_message_starred(mailbox: str, message_id: str, starred: bool) -> dict[str, Any]:
    """Add or remove Gmail's STARRED label and patch the compact cache index."""
    token = get_access_token(mailbox)
    body = json.dumps({
        "addLabelIds": ["STARRED"] if starred else [],
        "removeLabelIds": [] if starred else ["STARRED"],
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{GMAIL_API_BASE}/users/me/messages/{urllib.parse.quote(message_id, safe='')}/modify",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Gmail star update failed: HTTP {exc.code}") from exc

    cache = read_cache(mailbox)
    summaries: list[dict[str, Any]] = []
    for item in cache.get("messages") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "") == message_id:
            labels = {str(label) for label in (item.get("label_ids") or [])}
            if starred:
                labels.add("STARRED")
            else:
                labels.discard("STARRED")
            item = {**item, "label_ids": sorted(labels)}
        summaries.append(item)
    write_index(mailbox, summaries)
    return json.loads(raw) if raw else {"id": message_id, "labelIds": ["STARRED"] if starred else []}


# ── Batch mark read via Gmail API ──────────────────────────────────

def batch_mark_read(mailbox: str, message_ids: list[str]) -> dict[str, Any]:
    """Remove UNREAD label from messages via Gmail batchModify API."""
    import sys as _sys
    token = get_access_token(mailbox)
    body = json.dumps({"ids": list(message_ids), "removeLabelIds": ["UNREAD"]}).encode("utf-8")
    req = urllib.request.Request(
        f"{GMAIL_API_BASE}/users/me/messages/batchModify",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
            detail_json = json.loads(detail) if detail else {"status_code": exc.code}
        except Exception:
            detail_json = {"status_code": exc.code}
        print(f"[batch_mark_read] Gmail API error: {exc.code} {detail_json}", file=_sys.stderr)
        raise ValueError(f"Gmail batchModify failed: {exc.code} {detail_json}") from exc
    result = json.loads(raw) if raw else {}
    print(f"[batch_mark_read] success: {len(message_ids)} msg(s)", file=_sys.stderr)
    return result
