"""Regression tests for generated mail-prompt draft formatting."""

from __future__ import annotations

import sys
from pathlib import Path


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> None:
    from anna_inbox_executa.v2_tools import (
        MAIL_PROMPT_SYSTEM,
        _normalize_generated_draft_body,
    )

    assert _normalize_generated_draft_body("Hi Sahra,\n\nThanks.\n\nBest,\n\nKate") == (
        "Hi Sahra,\n\nThanks.\n\nBest,\nKate"
    )
    assert _normalize_generated_draft_body("First paragraph.\n\nFinal paragraph.") == (
        "First paragraph.\n\nFinal paragraph."
    )
    assert _normalize_generated_draft_body("Thanks,\r\n\r\nKate\r\n") == "Thanks,\nKate"
    assert "no blank line between them" in MAIL_PROMPT_SYSTEM

    print("PASS mail prompt draft format tests")


if __name__ == "__main__":
    main()
