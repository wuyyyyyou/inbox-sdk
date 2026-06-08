"""Storage key helpers for Anna Inbox."""

from __future__ import annotations

APP_STORAGE_PREFIX = "anna-inbox"


def app_key(key: str) -> str:
    """给所有 APS/local 业务 key 加上应用命名空间。"""
    clean = str(key or "").replace("\\", "/").strip("/")
    return f"{APP_STORAGE_PREFIX}/{clean}" if clean else APP_STORAGE_PREFIX


def sanitize_key_part(value: str) -> str:
    text = str(value or "").strip()
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in text).strip("._") or "default"
