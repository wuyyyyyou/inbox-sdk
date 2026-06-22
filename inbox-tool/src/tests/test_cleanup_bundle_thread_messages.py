"""Regression test: cleanup bundles keep multiple messages from one Gmail thread."""

from __future__ import annotations

import sys
from pathlib import Path


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")


def main() -> None:
    from mail_agent.cards.service import build_cleanup_bundle
    from mail_agent.domain.types import MessageLite

    mailbox = "user@example.com"
    messages = [
        MessageLite(
            message_id=f"msg_{i}",
            thread_id="thread_digest",
            from_addr=f"Digest <digest{i}@example.com>",
            to_addr=mailbox,
            subject=f"Digest item {i}",
            snippet=f"Digest snippet {i}",
            internal_date=str(1780000000000 + i),
        )
        for i in range(4)
    ]
    low_value_items = [
        {
            "message_id": message.message_id,
            "reason": "Low-priority automated digest",
            "confidence": 0.9,
        }
        for message in messages
    ]

    card, full_bundled = build_cleanup_bundle("run_thread_bundle", mailbox, low_value_items, messages)

    check("cleanup card exists", card is not None)
    check("full bundled keeps every message", len(full_bundled) == 4, f"got {len(full_bundled)}")
    check("card total count", card is not None and card.bundled_count == 4, f"got {getattr(card, 'bundled_count', None)}")
    check("card preview remains compact", card is not None and len(card.bundled_messages) == 3)
    check("all message ids are represented", {item["message_id"] for item in full_bundled} == {f"msg_{i}" for i in range(4)})

    print("PASS cleanup bundle thread messages")


if __name__ == "__main__":
    main()
