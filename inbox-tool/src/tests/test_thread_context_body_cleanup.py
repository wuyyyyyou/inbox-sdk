"""Regression tests for Handle thread-context body cleanup."""

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
    from mail_agent.actions.service import _strip_quoted_reply

    body = (
        "Hi Naveen,\n\n"
        "Hope you're having a great week. I'll keep this short.\n\n"
        "On Thu, May 21, 2026 at 10:00 PM kateqh zhou <kateqh@anna.partners> wrote:\n"
        "> Hi Naveen,\n"
        "> Previous content"
    )
    cleaned = _strip_quoted_reply(body)
    check("keeps new content", "I'll keep this short" in cleaned, cleaned)
    check("drops wrote header", "wrote:" not in cleaned, cleaned)
    check("drops quoted line", "> Previous content" not in cleaned, cleaned)

    forwarded = "Thanks, I'll review it.\n\n-----Original Message-----\nFrom: A\nOld content"
    cleaned_forwarded = _strip_quoted_reply(forwarded)
    check("drops original message", cleaned_forwarded == "Thanks, I'll review it.", cleaned_forwarded)

    quoted_only = "On Thu, May 21, 2026 at 10:00 PM kateqh zhou wrote:\n> Old content"
    check("quoted only becomes empty", _strip_quoted_reply(quoted_only) == "", _strip_quoted_reply(quoted_only))

    markdown_quote = "Here is the part I want to preserve:\n\n> The contract quote stays.\n> So does this line."
    cleaned_markdown = _strip_quoted_reply(markdown_quote)
    check("keeps markdown quote lines", "> The contract quote stays." in cleaned_markdown, cleaned_markdown)
    check("keeps consecutive quote lines", "> So does this line." in cleaned_markdown, cleaned_markdown)

    prose_wrote = "On the proposal, Alice wrote: keep the budget unchanged.\nThis is current content."
    cleaned_prose = _strip_quoted_reply(prose_wrote)
    check("keeps prose that resembles quote header", "keep the budget unchanged" in cleaned_prose, cleaned_prose)
    check("keeps following prose", "This is current content." in cleaned_prose, cleaned_prose)

    chinese_prose = "孔子写道：学而时习之。\nThis note should remain."
    cleaned_chinese_prose = _strip_quoted_reply(chinese_prose)
    check("keeps non-email chinese quote prose", "学而时习之" in cleaned_chinese_prose, cleaned_chinese_prose)

    print("PASS thread context body cleanup tests")


if __name__ == "__main__":
    main()
