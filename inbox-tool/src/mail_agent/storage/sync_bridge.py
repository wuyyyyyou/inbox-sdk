"""Synchronous bridge for code paths that cannot await storage calls."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Awaitable

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


def bind(loop: asyncio.AbstractEventLoop, loop_thread: threading.Thread) -> None:
    """绑定 Executa 后台事件循环，供同步 Gmail cache 代码调度 APS 调用。"""
    global _loop, _loop_thread
    _loop = loop
    _loop_thread = loop_thread


def run(coro: Awaitable[Any], *, timeout: float = 30.0) -> Any:
    """在非 event-loop 线程里同步等待一个 async storage 调用。

    跨线程提交时把当前 invoke_id 重绑到 loop 任务，保证 APS reverse RPC
    在多 invoke 并发下能带上 ``params.context.invoke_id``。
    """
    if _loop is None or not _loop.is_running():
        raise RuntimeError("storage sync bridge is not bound to a running event loop")
    if _loop_thread is not None and threading.get_ident() == _loop_thread.ident:
        raise RuntimeError("storage sync bridge cannot block the storage event loop thread")
    try:
        from executa_sdk.context import get_current_invoke_id, run_with_invoke_id
        scheduled: Any = run_with_invoke_id(get_current_invoke_id(), coro)
    except Exception:
        scheduled = coro  # type: ignore[assignment]
    future = asyncio.run_coroutine_threadsafe(scheduled, _loop)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"storage sync bridge timed out after {timeout}s") from exc
