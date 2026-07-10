"""Cumulative Anna sampling budget for one Ask tool invocation."""

from __future__ import annotations

import asyncio
from typing import Any


# Anna applies this quota across all sampling/createMessage calls sharing an
# invoke_id. Keep the Ask workflow within the 8192-token grant currently
# exposed by the host, including retries and JSON repair calls.
ASK_SAMPLING_TOTAL_TOKENS = 8192
ASK_PLANNER_MAX_TOKENS = 1024
ASK_FILTER_MAX_TOKENS = 512
ASK_ANSWER_MAX_TOKENS = 6144
ASK_JSON_REPAIR_MAX_TOKENS = 512

_MAX_TOKENS_BY_TOOL = {
    "ask_planner": ASK_PLANNER_MAX_TOKENS,
    "ask_filter": ASK_FILTER_MAX_TOKENS,
    "ask_answer": ASK_ANSWER_MAX_TOKENS,
    "json_repair": ASK_JSON_REPAIR_MAX_TOKENS,
}
_DEFAULT_MAX_TOKENS = 512


class AskSamplingBudgetExceeded(RuntimeError):
    """Raised before sending a sampling request that exceeds the Ask budget."""


def with_ask_sampling_budget(sampling_create_message: Any) -> Any:
    """Wrap one Ask run's sampler with an invoke-wide token reservation cap.

    The host accounts for requested ``maxTokens`` cumulatively, including
    failed attempts. Reserve before each call so concurrent mailbox filters,
    retries, and JSON repair cannot overrun the host quota.
    """
    remaining = ASK_SAMPLING_TOTAL_TOKENS
    lock = asyncio.Lock()

    async def _budgeted_sampling(**kwargs: Any) -> dict[str, Any]:
        nonlocal remaining
        metadata = kwargs.get("metadata")
        tool = str(metadata.get("tool") or "unknown") if isinstance(metadata, dict) else "unknown"
        requested = kwargs.get("max_tokens")
        if not isinstance(requested, int) or requested <= 0:
            raise ValueError("max_tokens must be a positive integer")

        tool_cap = _MAX_TOKENS_BY_TOOL.get(tool, _DEFAULT_MAX_TOKENS)
        async with lock:
            approved = min(requested, tool_cap, remaining)
            if approved <= 0:
                raise AskSamplingBudgetExceeded(
                    f"Ask sampling budget exhausted after reserving {ASK_SAMPLING_TOTAL_TOKENS} tokens"
                )
            remaining -= approved

        kwargs["max_tokens"] = approved
        return await sampling_create_message(**kwargs)

    return _budgeted_sampling
