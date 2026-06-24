"""Focused regression tests for Handle draft prompt construction."""

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
    from mail_agent.actions.service import _DRAFT_SYSTEM, _build_draft_prompt
    from mail_agent.storage.types import OriginalEmail, PersistentCard

    card = PersistentCard(
        card_id="card_1",
        thread_id="thread_1",
        title="Partnership reply",
        summary="A brand asked whether you want to move forward next month.",
        recommendation="Decide whether to move forward or ask for more details.",
        original=OriginalEmail(
            from_addr="brand@example.com",
            thread="Creator partnership",
            body="We'd love to collaborate next month. Are you interested?",
        ),
    )
    thread_context = {
        "subject": "Creator partnership",
        "message_count": 1,
        "messages": [
            {
                "from": "brand@example.com",
                "body": "We'd love to collaborate next month. Are you interested?",
            }
        ],
    }

    prompt = _build_draft_prompt(
        card,
        thread_context,
        "reply_to_sender",
        revision_input="User reply intent:\n- Reply goal: Decline\n- Your take: Keep the door open for July.",
    )
    check("generation label", "User reply intent and instructions:" in prompt, prompt)
    check("generation task", "Generate a new draft incorporating these instructions" in prompt, prompt)

    revise_prompt = _build_draft_prompt(
        card,
        thread_context,
        "reply_to_sender",
        current_draft="Hi, thanks for reaching out.",
        revision_input="Make it shorter and more direct.",
    )
    check("revision label", "User revision request:" in revise_prompt, revise_prompt)
    check("revision task", "Revise the existing draft" in revise_prompt, revise_prompt)
    check("system mentions current intent", "current reply intent/instructions" in _DRAFT_SYSTEM, _DRAFT_SYSTEM)

    print("PASS handle draft prompt tests")


if __name__ == "__main__":
    main()
