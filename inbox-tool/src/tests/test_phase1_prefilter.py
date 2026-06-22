"""Phase 1 low-value prefilter regression tests."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)


def _msg(
    message_id: str,
    *,
    subject: str,
    snippet: str = "",
    from_addr: str = "news@example.com",
    unread: bool = False,
    starred: bool = False,
    important: bool = False,
    has_attachment: bool = False,
    headers: dict[str, str] | None = None,
) -> Any:
    from mail_agent.domain.types import MessageLite

    return MessageLite(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        from_addr=from_addr,
        to_addr="user@example.com",
        subject=subject,
        snippet=snippet,
        internal_date="1770000000000",
        label_ids=["INBOX"],
        unread=unread,
        starred=starred,
        important=important,
        has_attachment=has_attachment,
        headers=headers or {},
    )


async def _run() -> None:
    from mail_agent.core.phase1 import _run_phase1_single_batch, prefilter_phase1_messages
    from mail_agent.domain.types import MailboxProfile
    from mail_agent.planning.strategies import get as get_strategy

    strategy = get_strategy("default_secretary")
    assert strategy is not None
    profile = MailboxProfile(mailbox_id="user@example.com", owner="user@example.com", important_contacts=["ceo@example.com"])

    bulk = _msg(
        "bulk-1",
        subject="Weekly newsletter digest",
        snippet="Unsubscribe from this promotion",
        headers={"list_unsubscribe": "<mailto:unsubscribe@example.com>"},
    )
    security = _msg("security-1", subject="Security login verification required")
    starred = _msg("starred-1", subject="Newsletter digest", starred=True)
    request = _msg("request-1", subject="Can you review this proposal?", from_addr="ceo@example.com")
    ambiguous = _msg("ambiguous-1", subject="Team update", snippet="Sharing the latest note from the team")

    pref = prefilter_phase1_messages([bulk, security, starred, request, ambiguous], strategy, profile)
    llm_ids = {m.message_id for m in pref["llm_messages"]}
    rule_candidate_ids = {m.message_id for m in pref["rule_candidate_messages"]}
    low_ids = {item["message_id"] for item in pref["low_value_items"]}
    check("bulk message is prefiltered", low_ids == {"bulk-1"})
    check("clear protected messages use rule fast path", rule_candidate_ids == {"security-1", "starred-1", "request-1"})
    check("only ambiguous message stays in LLM path", llm_ids == {"ambiguous-1"})

    calls: list[str] = []

    async def fake_sampling_create_message(**kwargs: Any) -> dict[str, Any]:
        text = kwargs["messages"][0]["content"]["text"]
        calls.append(text)
        payload = {
            "classifications": [
                {
                    "message_id": "ambiguous-1",
                    "user_action": "review",
                    "priority_hint": "low",
                    "read_depth": "message_detail",
                    "confidence": 0.7,
                    "reason": "Useful update worth a quick review.",
                }
            ]
        }
        return {"content": json.dumps(payload)}

    result = await _run_phase1_single_batch([bulk, security, ambiguous], strategy, profile, fake_sampling_create_message)
    check("sampling called once for remaining LLM messages", len(calls) == 1)
    check("prefiltered bulk not included in prompt", "bulk-1" not in calls[0])
    check("rule fast-path security not included in prompt", "security-1" not in calls[0])
    check("ambiguous message included in prompt", "ambiguous-1" in calls[0])
    check("rule low value retained for cleanup", any(item["message_id"] == "bulk-1" for item in result["low_value_items"]))
    check("rule and llm candidates retained", len(result["candidates"]) == 2)

    async def forbidden_sampling_create_message(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("sampling should not run when every message is prefiltered")

    only_bulk = await _run_phase1_single_batch([bulk], strategy, profile, forbidden_sampling_create_message)
    check("all-prefiltered batch has no candidates", only_bulk["candidates"] == [])
    check("all-prefiltered batch still returns cleanup item", [item["message_id"] for item in only_bulk["low_value_items"]] == ["bulk-1"])

    only_rule = await _run_phase1_single_batch([security], strategy, profile, forbidden_sampling_create_message)
    check("all-rule-fast-path batch has candidate", len(only_rule["candidates"]) == 1)


def main() -> None:
    asyncio.run(_run())
    print("PASS phase1 prefilter tests")


if __name__ == "__main__":
    main()
