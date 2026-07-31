"""Executa 调用链的安全时序诊断。

诊断对象只保存在当前 invoke 或其关联的后台 run 内存中。它不记录邮件、
邮箱地址、提示词、token、URL 查询参数或 Host 返回正文，因此可以回传前端
用于定位平台反向 RPC 与上游服务的延迟边界。
"""

from __future__ import annotations

import time
import uuid
from contextvars import ContextVar, Token
from typing import Any


_CURRENT_TRACE: ContextVar[dict[str, Any] | None] = ContextVar("anna_runtime_trace", default=None)
_MAX_SPANS = 40
_SAFE_FIELD_NAMES = {
    "api_calls",
    "cached",
    "code",
    "endpoint",
    "error_type",
    "http_status",
    "index_build_ms",
    "index_reused",
    "fts_query_ms",
    "path",
    "result_count",
    "rows_indexed",
    "rows_scanned",
    "source",
}


def create_trace(*, operation: str, invoke_id: str = "") -> dict[str, Any]:
    """创建单次调用的内存 trace，invoke_id 不对外暴露以避免关联宿主内部数据。"""
    _ = invoke_id
    return {
        "trace_id": f"rt_{uuid.uuid4().hex[:12]}",
        "operation": str(operation or "invoke")[:80],
        "started": time.monotonic(),
        "spans": [],
    }


def activate_trace(trace: dict[str, Any] | None) -> Token[dict[str, Any] | None]:
    """将 trace 绑定到当前线程/协程上下文，asyncio.to_thread 会继承该上下文。"""
    return _CURRENT_TRACE.set(trace)


def deactivate_trace(token: Token[dict[str, Any] | None]) -> None:
    """恢复上层 invoke 的上下文，避免并发请求互相写入诊断记录。"""
    _CURRENT_TRACE.reset(token)


def current_trace() -> dict[str, Any] | None:
    """返回当前调用链的 trace；没有诊断上下文时保持无副作用。"""
    return _CURRENT_TRACE.get()


def record_span(
    stage: str,
    started: float,
    *,
    outcome: str = "ok",
    **fields: Any,
) -> None:
    """记录一个已结束阶段，仅接收允许的非敏感枚举或数值字段。"""
    trace = current_trace()
    if not trace:
        return
    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    span: dict[str, Any] = {
        "stage": str(stage or "unknown")[:80],
        "elapsed_ms": elapsed_ms,
        "outcome": "ok" if outcome == "ok" else "error",
    }
    for name, value in fields.items():
        if name not in _SAFE_FIELD_NAMES or value is None:
            continue
        if isinstance(value, bool):
            span[name] = value
        elif isinstance(value, int):
            span[name] = value
        elif isinstance(value, str):
            span[name] = value[:80]
    spans = trace.setdefault("spans", [])
    if isinstance(spans, list):
        spans.append(span)
        if len(spans) > _MAX_SPANS:
            del spans[:-_MAX_SPANS]


def snapshot(trace: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """生成可返回前端的只读摘要，不暴露内部 monotonic 起点或调用参数。"""
    selected = trace or current_trace()
    if not selected:
        return None
    spans = selected.get("spans")
    return {
        "trace_id": str(selected.get("trace_id") or ""),
        "operation": str(selected.get("operation") or "invoke"),
        "elapsed_ms": max(0, int((time.monotonic() - float(selected.get("started") or time.monotonic())) * 1000)),
        "spans": [dict(item) for item in spans if isinstance(item, dict)] if isinstance(spans, list) else [],
    }


__all__ = [
    "activate_trace",
    "create_trace",
    "current_trace",
    "deactivate_trace",
    "record_span",
    "snapshot",
]
