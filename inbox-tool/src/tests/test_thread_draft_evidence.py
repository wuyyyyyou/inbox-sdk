"""回复草稿必须读取完整线程证据的回归测试。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> None:
    from mail_agent.ai_turn.tools import _load_thread_excerpt

    summaries = [
        {"id": "m3", "thread_id": "thread-1", "internal_date": "300", "subject": "Re: Deal"},
        {"id": "m1", "thread_id": "thread-1", "internal_date": "100", "subject": "Deal"},
        {"id": "m2", "thread_id": "thread-1", "internal_date": "200", "subject": "Re: Deal"},
    ]
    details = {
        "m1": {"id": "m1", "thread_id": "thread-1", "date": "2026-04-15", "from": "Alice <alice@example.com>", "to": "Kate", "subject": "Deal", "snippet": "Opening", "body_text": "Opening terms", "content_analysis": {"body": "Opening terms", "attachment_analysis": []}},
        "m2": {"id": "m2", "thread_id": "thread-1", "date": "2026-04-16", "from": "Kate", "to": "Alice", "subject": "Re: Deal", "snippet": "Reply", "body_text": "We need approval", "content_analysis": {"body": "We need approval", "attachment_analysis": []}},
        "m3": {"id": "m3", "thread_id": "thread-1", "date": "2026-04-17", "from": "Alice <alice@example.com>", "to": "Kate", "subject": "Re: Deal", "snippet": "Latest", "body_text": "Please confirm", "content_analysis": {"body": "Please confirm", "attachment_analysis": []}},
    }

    async def run() -> dict:
        with patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="kate@example.com"), patch(
            "mail_agent.mail_providers.gmail.adapter.refresh_thread_cache", return_value=list(details.values())
        ) as refresh, patch(
            "mail_agent.mail_providers.gmail.adapter.list_messages", return_value=summaries
        ), patch(
            "mail_agent.mail_providers.gmail.adapter.read_message", side_effect=lambda _mailbox, mid: details[mid]
        ):
            result = await _load_thread_excerpt("kate@example.com", "m3", "thread-1")
            assert refresh.call_count == 1
            return result

    evidence = asyncio.run(run())
    assert evidence["thread_message_count"] == 3
    assert evidence["thread_messages_included"] == 3
    assert evidence["thread_evidence_partial"] is False
    assert evidence["body"].index("Opening terms") < evidence["body"].index("We need approval") < evidence["body"].index("Please confirm")
    print("PASS thread draft evidence")


if __name__ == "__main__":
    main()
