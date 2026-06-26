"""Regression tests for bounded Handle thread-context pagination."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label} failed{': ' + detail if detail else ''}")


def main() -> None:
    from mail_agent.actions import service
    from mail_agent.storage.types import OriginalEmail, PersistentCard

    summaries = [
        {
            "id": f"msg-{i}",
            "thread_id": "thread-1",
            "from": f"sender-{i}@example.com",
            "to": "me@example.com",
            "cc": "",
            "subject": "Thread subject",
            "internal_date": str(i),
        }
        for i in range(8)
    ]
    read_ids: list[str] = []

    def fake_summaries(mailbox: str, thread_id: str):
        check("mailbox forwarded", mailbox == "me@example.com", mailbox)
        check("thread id forwarded", thread_id == "thread-1", thread_id)
        return list(summaries)

    def fake_detail(mailbox: str, summary: dict):
        read_ids.append(str(summary.get("id") or ""))
        index = int(str(summary["id"]).split("-")[-1])
        return SimpleNamespace(
            message_id=summary["id"],
            from_addr=summary["from"],
            to_addr=summary["to"],
            cc="",
            internal_date=summary["internal_date"],
            subject=summary["subject"],
            body_text=f"Body {index}",
        )

    original_summaries = service._thread_summaries
    original_detail = service._message_from_summary
    service._thread_summaries = fake_summaries
    service._message_from_summary = fake_detail
    try:
        card = PersistentCard(
            card_id="card-1",
            message_id="msg-7",
            thread_id="thread-1",
            original=OriginalEmail(thread="Thread subject", from_addr="sender-7@example.com", to_addr="me@example.com", time="7"),
        )
        preview = service._fetch_thread_context_sync("me@example.com", card, preview_edges=True)
        check("preview size", len(preview["messages"]) == 2, str(preview))
        check("preview ids", [m["message_id"] for m in preview["messages"]] == ["msg-0", "msg-7"], str(preview))
        check("preview cursor", preview["next_before_index"] == 7, str(preview))
        check("preview has more", preview["has_more_messages"] is True, str(preview))
        read_ids.clear()

        first = service._fetch_thread_context_sync("me@example.com", card, page_limit=5)
        check("first page size", len(first["messages"]) == 5, str(first))
        check("first page ids", [m["message_id"] for m in first["messages"]] == ["msg-3", "msg-4", "msg-5", "msg-6", "msg-7"], str(first))
        check("first page cursor", first["next_before_index"] == 3, str(first))
        check("first page has more", first["has_more_messages"] is True, str(first))
        check("only first page details read", read_ids == ["msg-3", "msg-4", "msg-5", "msg-6", "msg-7"], str(read_ids))

        read_ids.clear()
        second = service._fetch_thread_context_sync("me@example.com", card, before_index=3, page_limit=5)
        check("second page size", len(second["messages"]) == 3, str(second))
        check("second page ids", [m["message_id"] for m in second["messages"]] == ["msg-0", "msg-1", "msg-2"], str(second))
        check("second page no cursor", second["next_before_index"] is None, str(second))
        check("second page has no more", second["has_more_messages"] is False, str(second))
        check("only second page details read", read_ids == ["msg-0", "msg-1", "msg-2"], str(read_ids))
    finally:
        service._thread_summaries = original_summaries
        service._message_from_summary = original_detail

    print("PASS thread context pagination tests")


if __name__ == "__main__":
    main()
