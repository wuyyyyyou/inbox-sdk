"""Invoke context — typed view of host ``params.context`` plus reverse-RPC routing.

Host rule (Anna runtime): when multiple tool invokes are active on the same
Executa process, every plugin-initiated reverse RPC must carry
``params.context.invoke_id`` so the host can attribute the call to the correct
in-flight invoke. Omitting it yields JSON-RPC ``-32602``.

This module therefore:

1. Parses the per-invoke host payload (:class:`InvokeContext`).
2. Tracks the **current** ``invoke_id`` across worker threads and the shared
   asyncio loop (ContextVar + thread-local fallback).
3. Injects ``params.context.invoke_id`` into outbound reverse-RPC frames.
"""
from __future__ import annotations

import contextvars
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Optional

# 当前 in-flight invoke 的 id：asyncio 任务内优先走 ContextVar；
# 跨 ThreadPool / run_coroutine_threadsafe 时用 thread-local 兜底，
# 并在调度到 event loop 前显式 re-bind（见 run_with_invoke_id）。
_CURRENT_INVOKE_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "executa_current_invoke_id",
    default="",
)
_THREAD_INVOKE_ID = threading.local()


def resolve_invoke_id(params: Mapping[str, Any] | None) -> str:
    """从 Host invoke 参数解析 invoke_id（context 优先，兼容顶层字段）。"""
    if not isinstance(params, Mapping):
        return ""
    raw_ctx = params.get("context")
    ctx: Mapping[str, Any] = raw_ctx if isinstance(raw_ctx, Mapping) else {}
    raw = ctx.get("invoke_id")
    if raw is None or raw == "":
        raw = params.get("invoke_id")
    return str(raw or "").strip()


def get_current_invoke_id() -> str:
    """返回当前绑定的 invoke_id；未绑定时为空字符串。"""
    value = str(_CURRENT_INVOKE_ID.get() or "").strip()
    if value:
        return value
    return str(getattr(_THREAD_INVOKE_ID, "value", "") or "").strip()


def set_current_invoke_id(invoke_id: str) -> contextvars.Token:
    """绑定当前线程/任务的 invoke_id，返回可 reset 的 ContextVar token。"""
    normalized = str(invoke_id or "").strip()
    token = _CURRENT_INVOKE_ID.set(normalized)
    _THREAD_INVOKE_ID.value = normalized
    return token


def reset_current_invoke_id(token: contextvars.Token) -> None:
    """恢复 set_current_invoke_id 之前的 ContextVar，并清理 thread-local。"""
    try:
        _CURRENT_INVOKE_ID.reset(token)
    finally:
        _THREAD_INVOKE_ID.value = str(_CURRENT_INVOKE_ID.get() or "").strip()


@contextmanager
def invoke_id_scope(invoke_id: str) -> Iterator[str]:
    """同步作用域：在 worker / 连接性探测线程内绑定 invoke_id。"""
    token = set_current_invoke_id(invoke_id)
    try:
        yield str(invoke_id or "").strip()
    finally:
        reset_current_invoke_id(token)


class _AsyncInvokeIdScope:
    """async with 绑定当前任务的 invoke_id，确保 reverse RPC 能读到。"""

    def __init__(self, invoke_id: str) -> None:
        self._invoke_id = str(invoke_id or "").strip()
        self._token: contextvars.Token | None = None

    async def __aenter__(self) -> str:
        self._token = set_current_invoke_id(self._invoke_id)
        return self._invoke_id

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._token is not None:
            reset_current_invoke_id(self._token)
            self._token = None


def async_invoke_id_scope(invoke_id: str) -> _AsyncInvokeIdScope:
    """asyncio 任务内绑定 invoke_id 的异步上下文管理器。"""
    return _AsyncInvokeIdScope(invoke_id)


async def run_with_invoke_id(invoke_id: str, awaitable: Any) -> Any:
    """在绑定 invoke_id 的任务上下文中 await 给定 awaitable。

    用于 ``asyncio.run_coroutine_threadsafe``：worker 线程的 ContextVar
    不会自动传到 event loop 线程，必须在 loop 侧重新绑定。
    """
    async with async_invoke_id_scope(invoke_id):
        return await awaitable


def inject_reverse_rpc_context(
    params: Mapping[str, Any] | None,
    *,
    invoke_id: str | None = None,
) -> dict[str, Any]:
    """向 reverse-RPC params 注入 ``context.invoke_id``（不覆盖已有非空值）。

    Host 在多 invoke 并发时用该字段路由；单 invoke 时带上亦安全。
    """
    out: dict[str, Any] = dict(params) if isinstance(params, Mapping) else {}
    resolved = str(invoke_id or get_current_invoke_id() or "").strip()
    if not resolved:
        return out
    raw_ctx = out.get("context")
    context = dict(raw_ctx) if isinstance(raw_ctx, Mapping) else {}
    existing = str(context.get("invoke_id") or "").strip()
    if not existing:
        context["invoke_id"] = resolved
    out["context"] = context
    return out


@dataclass(frozen=True)
class InvokeContext:
    """Typed view of ``params.context`` for a single tool invocation."""

    invoke_id: Optional[str] = None
    plugin_name: Optional[str] = None
    deadline_ms: Optional[int] = None
    credentials: Mapping[str, Any] | None = None
    raw: Mapping[str, Any] | None = None

    @classmethod
    def from_params(cls, params: Mapping[str, Any] | None) -> "InvokeContext":
        """Build from the raw ``params`` dict of an ``invoke`` request."""
        if not isinstance(params, Mapping):
            return cls()
        raw_ctx = params.get("context")
        ctx: Mapping[str, Any] = raw_ctx if isinstance(raw_ctx, Mapping) else {}
        deadline = ctx.get("deadline_ms")
        try:
            deadline_int = int(deadline) if deadline is not None else None
        except (TypeError, ValueError):
            deadline_int = None
        invoke_id = resolve_invoke_id(params) or None
        credentials = ctx.get("credentials")
        return cls(
            invoke_id=invoke_id,
            plugin_name=ctx.get("plugin_name") if ctx else None,
            deadline_ms=deadline_int,
            credentials=credentials if isinstance(credentials, Mapping) else None,
            raw=dict(ctx) if ctx else None,
        )

    def remaining_s(self) -> float:
        """Seconds left in the invoke budget. ``math.inf`` if unknown."""
        if self.deadline_ms is None:
            return math.inf
        return max(0.0, (self.deadline_ms / 1000.0) - time.time())

    def has_deadline(self) -> bool:
        return self.deadline_ms is not None

    def expired(self) -> bool:
        """True iff a deadline is set and has already passed."""
        return self.has_deadline() and self.remaining_s() <= 0.0


__all__ = [
    "InvokeContext",
    "resolve_invoke_id",
    "get_current_invoke_id",
    "set_current_invoke_id",
    "reset_current_invoke_id",
    "invoke_id_scope",
    "async_invoke_id_scope",
    "run_with_invoke_id",
    "inject_reverse_rpc_context",
]
