"""将 APS 历史全量存储收敛到白名单同步数据。"""

from __future__ import annotations

from typing import Any

from .keys import app_key
from .selective import is_aps_sync_key

_APP_PREFIX = f"{app_key('')}/"
_LEGACY_WORKFLOW_SUFFIX = "/inbox_workflow_state"


def _is_known_local_key(key: str) -> bool:
    """仅识别本工具已发布过的非同步命名空间，未知键绝不删除。"""
    normalized = str(key or "").replace("\\", "/").strip("/")
    if not normalized.startswith(_APP_PREFIX):
        return False
    relative = normalized[len(_APP_PREFIX):]
    return relative.startswith((
        "gmail_cache/",
        "mailboxes/",
        "runs/",
        "prefs/",
        "custom/",
        "ai/",
        "mailbox/",
    ))


def _workflow_sync_payload(value: Any) -> dict[str, Any]:
    """从旧的完整工作流状态提取允许跨端同步的分类字段。"""
    raw = value if isinstance(value, dict) else {}
    todos = raw.get("todos") if isinstance(raw.get("todos"), list) else []
    done = raw.get("done") if isinstance(raw.get("done"), list) else []
    snoozed = raw.get("snoozed") if isinstance(raw.get("snoozed"), list) else []
    snoozed_until = raw.get("snoozedUntil") if isinstance(raw.get("snoozedUntil"), dict) else {}
    return {
        "todos": todos,
        "done": done,
        "snoozed": snoozed,
        "snoozedUntil": snoozed_until,
        "version": 1,
        "updated_at": str(raw.get("updated_at") or ""),
    }


async def migrate_and_cleanup_aps(storage: Any, *, scope: str) -> dict[str, int]:
    """迁移旧工作流同步字段，并删除已知的 APS 非白名单业务键。

    本函数只接收 APS 原始客户端，绝不经过选择性路由，确保旧缓存、邮件内容和
    联系人等数据确实从 APS 删除，而未知命名空间保持原样。
    """
    counts = {"migrated": 0, "deleted": 0}
    cursor: str | None = None
    while True:
        result = await storage.list(prefix=_APP_PREFIX, cursor=cursor, limit=200, scope=scope)
        items = result.get("items") or []
        for item in items:
            key = str(item.get("key") or "") if isinstance(item, dict) else str(item or "")
            if not key or is_aps_sync_key(key):
                continue
            if key.endswith(_LEGACY_WORKFLOW_SUFFIX):
                legacy = await storage.get(key, scope=scope)
                sync_key = f"{key[:-len(_LEGACY_WORKFLOW_SUFFIX)]}/inbox_workflow_sync"
                current = await storage.get(sync_key, scope=scope)
                if legacy.get("exists") and not current.get("exists"):
                    await storage.set(sync_key, _workflow_sync_payload(legacy.get("value")), scope=scope)
                    counts["migrated"] += 1
            if _is_known_local_key(key):
                await storage.delete(key, scope=scope)
                counts["deleted"] += 1
        cursor = result.get("next_cursor") or result.get("cursor")
        if not cursor or not items:
            break
    return counts
