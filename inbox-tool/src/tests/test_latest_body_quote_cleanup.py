"""Regression tests for top-level Handle email body quote cleanup."""

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
    from anna_inbox_executa.v2_tools import _strip_quoted_reply_html

    html = (
        "<div>Latest update with <a href=\"https://anna.partners\">link</a>.</div>"
        "<div class=\"gmail_attr\">On Wed, Jun 24, 2026 at 1:18 PM Kate wrote:</div>"
        "<blockquote><div>Old quoted content</div></blockquote>"
    )
    cleaned = _strip_quoted_reply_html(html)
    check("keeps latest content", "Latest update" in cleaned, cleaned)
    check("keeps links", "https://anna.partners" in cleaned, cleaned)
    check("drops gmail attr", "wrote:" not in cleaned, cleaned)
    check("drops blockquote", "Old quoted content" not in cleaned, cleaned)

    nested = (
        "<div>Fresh answer <a href=\"https://anna.partners/thread\">thread</a>.</div>"
        "<div class=\"gmail_quote\">"
        "<div dir=\"ltr\">On Wed, Jun 24, 2026 at 1:18 PM Kate wrote:</div>"
        "<div>Nested old content</div>"
        "<blockquote><div>Older quoted content</div></blockquote>"
        "</div>"
    )
    nested_cleaned = _strip_quoted_reply_html(nested)
    check("keeps nested latest content", "Fresh answer" in nested_cleaned, nested_cleaned)
    check("keeps nested latest link", "https://anna.partners/thread" in nested_cleaned, nested_cleaned)
    check("drops nested gmail quote", "Nested old content" not in nested_cleaned, nested_cleaned)
    check("drops nested blockquote", "Older quoted content" not in nested_cleaned, nested_cleaned)
    check("does not leave broken closing div", nested_cleaned.count("</div>") <= nested_cleaned.count("<div"), nested_cleaned)

    ordinary_quote = (
        "<p>Please keep this excerpt:</p>"
        "<blockquote><p>The quoted contract term stays visible.</p></blockquote>"
        "<p>Then my reply continues.</p>"
    )
    ordinary_cleaned = _strip_quoted_reply_html(ordinary_quote)
    check("keeps ordinary blockquote", "quoted contract term stays visible" in ordinary_cleaned, ordinary_cleaned)
    check("keeps content after ordinary blockquote", "reply continues" in ordinary_cleaned, ordinary_cleaned)

    plain_header = (
        "<p>Current reply stays.</p>"
        "<br>On Wed, Jun 24, 2026 at 1:18 PM Kate wrote:"
        "<p>Old body without a quote wrapper</p>"
    )
    plain_cleaned = _strip_quoted_reply_html(plain_header)
    check("keeps content before plain header", "Current reply stays" in plain_cleaned, plain_cleaned)
    check("drops content after plain header", "Old body without a quote wrapper" not in plain_cleaned, plain_cleaned)

    paragraph_header = (
        "<p>Current paragraph reply stays.</p>"
        "<p>On Wed, Jun 24, 2026 at 1:18 PM Kate wrote:</p>"
        "<p>Old paragraph body</p>"
    )
    paragraph_cleaned = _strip_quoted_reply_html(paragraph_header)
    check("keeps content before paragraph header", "Current paragraph reply stays" in paragraph_cleaned, paragraph_cleaned)
    check("drops content after paragraph header", "Old paragraph body" not in paragraph_cleaned, paragraph_cleaned)

    ordinary_on_wrote = (
        "<p>On the proposal, Alice wrote: keep the budget unchanged.</p>"
        "<p>This is still current content.</p>"
    )
    ordinary_on_cleaned = _strip_quoted_reply_html(ordinary_on_wrote)
    check("keeps prose that only resembles quote header", "keep the budget unchanged" in ordinary_on_cleaned, ordinary_on_cleaned)
    check("keeps following prose", "still current content" in ordinary_on_cleaned, ordinary_on_cleaned)

    chinese_prose = "<p>孔子写道：学而时习之。</p><p>This note should remain.</p>"
    chinese_prose_cleaned = _strip_quoted_reply_html(chinese_prose)
    check("keeps non-email chinese quote prose", "学而时习之" in chinese_prose_cleaned, chinese_prose_cleaned)

    prefixed_quotes = _strip_quoted_reply_html("<div>&gt;&gt; Old quoted line<br>&gt;&gt; Another quoted line</div>")
    check("removes repeated quote prefixes", ">>" not in prefixed_quotes, prefixed_quotes)

    print("PASS latest body quote cleanup tests")


if __name__ == "__main__":
    main()
