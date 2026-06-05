"""Gmail 查询规范化的最小回归测试。"""

from __future__ import annotations

import sys
from pathlib import Path


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check(label: str, actual: str, expected: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def main() -> None:
    from mail_agent.core.scan import _normalize_gmail_query

    check("draft label", _normalize_gmail_query("in:draft newer_than:7d"), "in:drafts newer_than:7d")
    check(
        "grouped or",
        _normalize_gmail_query("in:inbox (newer_than:3d OR is:important OR is:starred)"),
        "in:inbox {newer_than:3d is:important is:starred}",
    )
    check("bare keywords", _normalize_gmail_query("Brief OR mailbox OR surface"), "{Brief mailbox surface}")
    check(
        "multi word term",
        _normalize_gmail_query("(interested OR follow up OR partnership) -in:draft"),
        '{interested "follow up" partnership} -in:drafts',
    )
    print("PASS gmail query normalize tests")


if __name__ == "__main__":
    main()
