"""Judgment priority calibration regression tests."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def _judgment_for(action_reason: str, priority: str = "low") -> Any:
    from mail_agent.judgment_engine.service import _parse_compact_batch_item
    from mail_agent.planning.strategies import get as get_strategy

    strategy = get_strategy("default_secretary")
    assert strategy is not None
    return _parse_compact_batch_item(
        {
            "candidate_id": f"cand-{action_reason}",
            "priority": priority,
            "surface": priority in ("critical", "high", "medium"),
            "user_action": "review",
            "action_reason": action_reason,
            "title": "Review item",
            "context": "Context",
            "suggestion": "Review when convenient.",
            "action": "do_nothing",
            "confidence": 0.7,
        },
        strategy,
    )


def main() -> None:
    upcoming = _judgment_for("upcoming_event", "low")
    check("upcoming_event priority", upcoming.final_decision.priority, "medium")
    check("upcoming_event surfaces", upcoming.final_decision.should_show_in_main_result, True)
    check("upcoming_event not lower", upcoming.final_decision.should_show_in_lower_priority, False)

    deal = _judgment_for("deal_or_pipeline", "ignore")
    check("deal_or_pipeline priority", deal.final_decision.priority, "medium")
    check("deal_or_pipeline surfaces", deal.final_decision.should_show_in_main_result, True)

    cleanup = _judgment_for("cleanup", "medium")
    check("cleanup priority", cleanup.final_decision.priority, "low")

    receipt = _judgment_for("receipt_or_notice", "low")
    check("receipt priority remains low", receipt.final_decision.priority, "low")

    print("PASS judgment priority calibration tests")


if __name__ == "__main__":
    main()
