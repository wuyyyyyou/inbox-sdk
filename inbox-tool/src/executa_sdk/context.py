"""Invoke context — gives plugin tools a typed view of the per-call
``params.context`` payload sent by the host Agent.

Example
-------

.. code-block:: python

    from executa_sdk import InvokeContext

    async def handle_invoke(request):
        ctx = InvokeContext.from_params(request["params"])
        if ctx.remaining_s() <= 0:
            return error("subcall_timeout", "no time left in budget")
        # Optionally tighten the next reverse-RPC. The host loader will
        # auto-inject ``_clientTimeoutS`` from ``ctx.deadline_ms`` if you
        # don't, but explicit is friendlier when the plugin wants a
        # shorter slice than "all remaining time".
        await storage.set(key, value, timeout=min(5.0, ctx.remaining_s()))

The host now propagates ``deadline_ms`` (a Unix epoch milliseconds
absolute deadline derived from the invoke ``timeoutMs``) into
``params.context.deadline_ms``. Older hosts will simply omit it, in
which case :meth:`remaining_s` returns :data:`math.inf`.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional


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
        ctx = params.get("context") if isinstance(params.get("context"), Mapping) else {}
        deadline = ctx.get("deadline_ms")
        try:
            deadline_int = int(deadline) if deadline is not None else None
        except (TypeError, ValueError):
            deadline_int = None
        return cls(
            invoke_id=ctx.get("invoke_id") or params.get("invoke_id"),
            plugin_name=ctx.get("plugin_name"),
            deadline_ms=deadline_int,
            credentials=ctx.get("credentials") if isinstance(ctx.get("credentials"), Mapping) else None,
            raw=ctx or None,
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


__all__ = ["InvokeContext"]
