"""AI turn 多轮会话摘要 registry（进程内，不落盘、不存正文）。"""

from __future__ import annotations

import threading
import time
from typing import Any

# 每个 conversation_id 最多保留最近 N 条摘要
_MAX_TURNS = 8
# 进程内条目上限，防止泄漏
_MAX_CONVERSATIONS = 64

_lock = threading.Lock()
_registry: dict[str, list[dict[str, Any]]] = {}


def _trim_registry() -> None:
    """超过容量时丢弃最旧会话。"""
    if len(_registry) <= _MAX_CONVERSATIONS:
        return
    ordered = sorted(
        _registry.items(),
        key=lambda item: float((item[1][-1] if item[1] else {}).get("ts") or 0),
    )
    for key, _ in ordered[: max(0, len(_registry) - _MAX_CONVERSATIONS)]:
        _registry.pop(key, None)


def get_conversation_summary(conversation_id: str) -> list[dict[str, Any]]:
    """读取会话最近摘要列表（副本）。"""
    cid = str(conversation_id or "").strip()
    if not cid:
        return []
    with _lock:
        turns = _registry.get(cid) or []
        return [dict(item) for item in turns]


def record_turn_summary(
    conversation_id: str,
    *,
    tool: str,
    success: bool,
    candidate_count: int = 0,
    has_draft: bool = False,
    search_intent: str = "",
    kind: str = "",
) -> None:
    """写入一轮非敏感摘要：tool 名、是否成功、候选数、是否有 draft 等。"""
    cid = str(conversation_id or "").strip()
    if not cid:
        return
    entry = {
        "ts": time.time(),
        "tool": str(tool or "")[:64],
        "success": bool(success),
        "candidate_count": max(0, int(candidate_count or 0)),
        "has_draft": bool(has_draft),
        "search_intent": str(search_intent or "")[:120],
        "kind": str(kind or "")[:32],
    }
    with _lock:
        turns = list(_registry.get(cid) or [])
        turns.append(entry)
        _registry[cid] = turns[-_MAX_TURNS:]
        _trim_registry()


def format_summary_for_router(conversation_id: str) -> str:
    """将 registry 摘要格式化为 Router 可注入的短文本（无邮件正文）。"""
    turns = get_conversation_summary(conversation_id)
    if not turns:
        return ""
    lines: list[str] = []
    for index, turn in enumerate(turns[-4:], start=1):
        lines.append(
            f"{index}. tool={turn.get('tool')} ok={turn.get('success')} "
            f"candidates={turn.get('candidate_count')} has_draft={turn.get('has_draft')} "
            f"kind={turn.get('kind')} intent={turn.get('search_intent') or '-'}"
        )
    return "Recent turns (no bodies):\n" + "\n".join(lines)


__all__ = [
    "format_summary_for_router",
    "get_conversation_summary",
    "record_turn_summary",
]
