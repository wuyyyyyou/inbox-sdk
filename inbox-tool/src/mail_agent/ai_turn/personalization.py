"""AI 侧栏 Saved prompts 与 Memory（偏好）持久化。

存储走 ops 统一层（APS/local），仅短偏好句与 prompt 文本，不写邮件正文。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from mail_agent.storage.client import get_storage, scope as default_scope
from mail_agent.storage.keys import app_key

# 应用级 KV：与邮箱无关的 AI 个性化数据
_SAVED_PROMPTS_KEY = app_key("ai/saved_prompts")
_MEMORIES_KEY = app_key("ai/memories")

_MAX_PROMPTS = 50
_MAX_MEMORIES = 40
_MAX_PROMPT_BODY = 4000
_MAX_MEMORY_TEXT = 500


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def list_saved_prompts() -> dict[str, Any]:
    """列出全部 Saved prompts（按更新时间倒序）。"""
    result = await get_storage().get(_SAVED_PROMPTS_KEY, scope=default_scope())
    raw = result.get("value") if result.get("exists") else None
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        items = []
    prompts = [item for item in items if isinstance(item, dict)]
    prompts.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return {"success": True, "prompts": prompts, "etag": result.get("etag") or ""}


async def save_saved_prompt(
    *,
    prompt_id: str = "",
    title: str = "",
    body: str = "",
) -> dict[str, Any]:
    """新建或更新一条 Saved prompt。"""
    body_text = str(body or "").strip()[:_MAX_PROMPT_BODY]
    if not body_text:
        return {"success": False, "error": "empty_body"}
    title_text = str(title or "").strip()[:120] or body_text[:40]
    result = await get_storage().get(_SAVED_PROMPTS_KEY, scope=default_scope())
    raw = result.get("value") if result.get("exists") else {"items": []}
    if not isinstance(raw, dict):
        raw = {"items": []}
    items = [item for item in (raw.get("items") or []) if isinstance(item, dict)]
    pid = str(prompt_id or "").strip()
    now = _now_iso()
    if pid:
        found = False
        for item in items:
            if str(item.get("id") or "") == pid:
                item["title"] = title_text
                item["body"] = body_text
                item["updated_at"] = now
                found = True
                break
        if not found:
            items.append({
                "id": pid,
                "title": title_text,
                "body": body_text,
                "created_at": now,
                "updated_at": now,
            })
    else:
        pid = _new_id("sp")
        items.append({
            "id": pid,
            "title": title_text,
            "body": body_text,
            "created_at": now,
            "updated_at": now,
        })
    if len(items) > _MAX_PROMPTS:
        items = sorted(items, key=lambda item: str(item.get("updated_at") or ""), reverse=True)[:_MAX_PROMPTS]
    raw["items"] = items
    etag = str(result.get("etag") or "") or None
    saved = await get_storage().set(_SAVED_PROMPTS_KEY, raw, scope=default_scope(), if_match=etag)
    return {
        "success": True,
        "prompt": next((item for item in items if str(item.get("id")) == pid), None),
        "etag": saved.get("etag") or "",
    }


async def delete_saved_prompt(prompt_id: str) -> dict[str, Any]:
    """删除一条 Saved prompt。"""
    pid = str(prompt_id or "").strip()
    if not pid:
        return {"success": False, "error": "missing_id"}
    result = await get_storage().get(_SAVED_PROMPTS_KEY, scope=default_scope())
    raw = result.get("value") if result.get("exists") else {"items": []}
    if not isinstance(raw, dict):
        raw = {"items": []}
    before = raw.get("items") if isinstance(raw.get("items"), list) else []
    items = [item for item in before if isinstance(item, dict) and str(item.get("id") or "") != pid]
    raw["items"] = items
    etag = str(result.get("etag") or "") or None
    saved = await get_storage().set(_SAVED_PROMPTS_KEY, raw, scope=default_scope(), if_match=etag)
    return {"success": True, "deleted": len(before) - len(items), "etag": saved.get("etag") or ""}


async def get_saved_prompt(prompt_id: str) -> dict[str, Any] | None:
    """按 id 取单条 prompt。"""
    data = await list_saved_prompts()
    for item in data.get("prompts") or []:
        if str(item.get("id") or "") == str(prompt_id or "").strip():
            return item
    return None


async def list_ai_memories() -> dict[str, Any]:
    """列出 AI Memory 偏好条目。"""
    result = await get_storage().get(_MEMORIES_KEY, scope=default_scope())
    raw = result.get("value") if result.get("exists") else None
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        items = []
    memories = [item for item in items if isinstance(item, dict)]
    memories.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return {"success": True, "memories": memories, "etag": result.get("etag") or ""}


async def add_ai_memory(text: str, *, source: str = "user") -> dict[str, Any]:
    """追加一条偏好记忆（去重近似：相同正文不重复）。"""
    body = str(text or "").strip()[:_MAX_MEMORY_TEXT]
    if not body:
        return {"success": False, "error": "empty_text"}
    result = await get_storage().get(_MEMORIES_KEY, scope=default_scope())
    raw = result.get("value") if result.get("exists") else {"items": []}
    if not isinstance(raw, dict):
        raw = {"items": []}
    items = [item for item in (raw.get("items") or []) if isinstance(item, dict)]
    lowered = body.casefold()
    for item in items:
        if str(item.get("text") or "").strip().casefold() == lowered:
            item["updated_at"] = _now_iso()
            item["source"] = str(source or "user")[:32]
            raw["items"] = items
            etag = str(result.get("etag") or "") or None
            saved = await get_storage().set(_MEMORIES_KEY, raw, scope=default_scope(), if_match=etag)
            return {"success": True, "memory": item, "deduped": True, "etag": saved.get("etag") or ""}
    memory = {
        "id": _new_id("mem"),
        "text": body,
        "source": str(source or "user")[:32],
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    items.insert(0, memory)
    if len(items) > _MAX_MEMORIES:
        items = items[:_MAX_MEMORIES]
    raw["items"] = items
    etag = str(result.get("etag") or "") or None
    saved = await get_storage().set(_MEMORIES_KEY, raw, scope=default_scope(), if_match=etag)
    return {"success": True, "memory": memory, "deduped": False, "etag": saved.get("etag") or ""}


async def delete_ai_memory(memory_id: str) -> dict[str, Any]:
    """删除一条 Memory。"""
    mid = str(memory_id or "").strip()
    if not mid:
        return {"success": False, "error": "missing_id"}
    result = await get_storage().get(_MEMORIES_KEY, scope=default_scope())
    raw = result.get("value") if result.get("exists") else {"items": []}
    if not isinstance(raw, dict):
        raw = {"items": []}
    before = raw.get("items") if isinstance(raw.get("items"), list) else []
    items = [item for item in before if isinstance(item, dict) and str(item.get("id") or "") != mid]
    raw["items"] = items
    etag = str(result.get("etag") or "") or None
    saved = await get_storage().set(_MEMORIES_KEY, raw, scope=default_scope(), if_match=etag)
    return {"success": True, "deleted": len(before) - len(items), "etag": saved.get("etag") or ""}


async def format_memory_summary_for_prompt(*, max_items: int = 8) -> str:
    """生成注入 Router/Generator 的短偏好摘要（无邮件正文）。"""
    data = await list_ai_memories()
    memories = data.get("memories") or []
    if not memories:
        return ""
    lines = []
    for item in memories[: max(1, min(int(max_items or 8), 12))]:
        text = str(item.get("text") or "").strip()
        if text:
            lines.append(f"- {text[:_MAX_MEMORY_TEXT]}")
    if not lines:
        return ""
    return "User preferences (AI Memory):\n" + "\n".join(lines)


__all__ = [
    "add_ai_memory",
    "delete_ai_memory",
    "delete_saved_prompt",
    "format_memory_summary_for_prompt",
    "get_saved_prompt",
    "list_ai_memories",
    "list_saved_prompts",
    "save_saved_prompt",
]
