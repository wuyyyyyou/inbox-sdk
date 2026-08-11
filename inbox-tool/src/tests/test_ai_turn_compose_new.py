"""ai_turn_compose_new 返回结构兼容性回归测试。"""

from __future__ import annotations

import sys
import asyncio
import json
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


async def test_compose_new_normalizes_array_sampling_response(container: str, body: str) -> None:
    from mail_agent.ai_turn.tools import tool_compose_new

    async def sampling_stub(**_kwargs: Any) -> dict[str, Any]:
        return {
            "content": {
                "type": "text",
                "text": json.dumps(
                    {
                        "assistant_text": "Draft ready",
                        container: [{"body": body, "subject": "Array subject", "recipients": ["to@example.com"]}],
                    }
                ),
            }
        }

    result = await tool_compose_new(
        "Write a new email",
        {"mailbox": "sender@example.com"},
        {},
        language="en",
        sampling_create_message=sampling_stub,
    )

    assert result["kind"] == "draft"
    artifact = result["artifact"]
    assert artifact["type"] == "compose_draft"
    assert artifact["body"] == body
    assert artifact["subject"] == "Array subject"
    assert artifact["recipients"] == ["to@example.com"]


async def test_compose_new_without_usable_body_keeps_compose_empty() -> None:
    from mail_agent.ai_turn.tools import tool_compose_new

    async def sampling_stub(**_kwargs: Any) -> dict[str, Any]:
        return {
            "content": {
                "type": "text",
                "text": '{"drafts":[{"subject":"No body","recipients":["to@example.com"]}],"emails":[{"body":"   "}]}',
            }
        }

    result = await tool_compose_new(
        "Write a new email",
        {"mailbox": "sender@example.com"},
        {},
        language="en",
        sampling_create_message=sampling_stub,
    )

    assert result["kind"] == "error"
    assert result["error"] == "compose_empty"


async def main() -> None:
    for container, body in (
        ("drafts", "Draft body from drafts"),
        ("emails", "Draft body from emails"),
    ):
        await test_compose_new_normalizes_array_sampling_response(container, body)
    await test_compose_new_without_usable_body_keeps_compose_empty()
    print("[PASS] test_ai_turn_compose_new")


if __name__ == "__main__":
    asyncio.run(main())
