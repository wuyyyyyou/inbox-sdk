"""Regression tests for thread summary sanitization."""

from __future__ import annotations

import sys
from pathlib import Path


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label} failed{': ' + detail if detail else ''}")


def main() -> None:
    from mail_agent.actions.service import _dedupe_lines, _looks_like_summary_payload

    payload = (
        '{"thread_kind":"long_thread","headline":"Judge fee","what_happened":[],'
        '"open_questions":[],"reply_focus":"Confirm budget","related_context":[],'
        '"should_show":true,"confidence":"low"}'
    )

    check("detects summary payload", _looks_like_summary_payload(payload), payload)
    check(
        "drops summary payload from visible lines",
        _dedupe_lines(["Natural language summary", payload]) == ["Natural language summary"],
        str(_dedupe_lines(["Natural language summary", payload])),
    )

    print("PASS thread summary sanitization tests")


if __name__ == "__main__":
    main()
