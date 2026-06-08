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
    """在非 event-loop 线程里同步等待一个 async storage 调用。"""
    if _loop is None or not _loop.is_running():
        raise RuntimeError("storage sync bridge is not bound to a running event loop")
    if _loop_thread is not None and threading.get_ident() == _loop_thread.ident:
        raise RuntimeError("storage sync bridge cannot block the storage event loop thread")
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"storage sync bridge timed out after {timeout}s") from exc
