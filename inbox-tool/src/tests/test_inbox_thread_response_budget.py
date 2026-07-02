"""Focused tests for bounded Inbox thread-page responses."""

from __future__ import annotations

import base64
from unittest.mock import patch


def _message(index: int, **overrides: object) -> dict[str, object]:
    message: dict[str, object] = {
        "id": f"m{index}",
        "thread_id": "thread-1",
        "internal_date": str(index),
        "from": "sender@example.com",
        "to": "user@example.com",
        "subject": "Subject",
        "label_ids": ["INBOX"],
        "attachments": [],
        "body_text": "Body",
    }
    message.update(overrides)
    return message


def main() -> None:
    import anna_inbox_executa.common as common
    import anna_inbox_executa.v2_tools as tools

    assert tools.INBOX_THREAD_PAGE_SIZE == 5

    with patch(
        "mail_agent.mail_providers.gmail.adapter.decode_body_for_display",
        return_value={"html": "<p>Hello</p>", "text": "Hello"},
    ):
        html_payload = tools._display_body_payload(_message(1), limit=24000)
    assert html_payload["body_html"] == "<p>Hello</p>"
    assert "body_text" not in html_payload

    with patch(
        "mail_agent.mail_providers.gmail.adapter.decode_body_for_display",
        return_value={"html": "<p>Hello</p>", "text": "Hello"},
    ):
        prompt_payload = tools._display_body_payload(_message(1), limit=24000, prefer_html=False)
    assert prompt_payload["body_text"] == "Hello"
    assert "body_html" not in prompt_payload

    # Newsletter HTML larger than the old 30k/24k thresholds must retain its
    # layout when the complete response still fits under 256 KiB.
    newsletter_html = "<html><body><table>" + ("<tr><td>Cloud update</td></tr>" * 1200) + "</table></body></html>"
    encoded_html = base64.urlsafe_b64encode(newsletter_html.encode("utf-8")).decode("ascii")
    newsletter_message = _message(2, payload={
        "mimeType": "text/html",
        "body": {"data": encoded_html},
    })
    from mail_agent.mail_providers.gmail.adapter import decode_body_for_display
    decoded_newsletter = decode_body_for_display(newsletter_message)
    assert len(decoded_newsletter["html"]) > 30_000
    newsletter_display = tools._build_inbox_message_display_response("user@example.com", newsletter_message)
    assert newsletter_display.get("body_html")
    assert "body_text" not in newsletter_display
    assert tools._inbox_message_display_rpc_frame_size(newsletter_display) <= tools.INBOX_THREAD_RESPONSE_MAX_BYTES

    with patch(
        "mail_agent.mail_providers.gmail.adapter.decode_body_for_display",
        return_value={"html": "", "text": "Plain body"},
    ):
        text_payload = tools._display_body_payload(_message(1), limit=24000)
    assert text_payload["body_text"] == "Plain body"
    assert "body_html" not in text_payload

    messages = [_message(index) for index in range(12)]
    with (
        patch.object(tools, "_load_thread_messages", return_value=messages),
        patch.object(
            tools,
            "_display_body_payload",
            side_effect=lambda _message, *, limit: {
                "body_text": "汉" * limit,
                "body_truncated": True,
            },
        ),
    ):
        bounded = tools._build_inbox_thread_page("user@example.com", "thread-1")
    assert bounded["returned_count"] == 5
    assert [item["id"] for item in bounded["messages"]] == ["m7", "m8", "m9", "m10", "m11"]
    assert tools._inbox_thread_rpc_frame_size(bounded) <= tools.INBOX_THREAD_RESPONSE_MAX_BYTES
    assert all("body_html" not in item for item in bounded["messages"])

    metadata_heavy = [_message(index, **{"from": "x" * 100_000}) for index in range(12)]
    with patch.object(tools, "_load_thread_messages", return_value=metadata_heavy):
        reduced_page = tools._build_inbox_thread_page(
            "user@example.com",
            "thread-1",
            include_display_body=False,
        )
    assert 0 < reduced_page["returned_count"] < 5
    assert reduced_page["messages"][-1]["id"] == "m11"
    assert reduced_page["next_before_index"] == 12 - reduced_page["returned_count"]
    assert tools._inbox_thread_rpc_frame_size(reduced_page) <= tools.INBOX_THREAD_RESPONSE_MAX_BYTES

    frame = {
        "jsonrpc": "2.0",
        "id": "request-id",
        "result": {
            "success": True,
            "tool": "get_inbox_thread_page",
            "data": {
                "mailbox": "user@example.com",
                "thread_id": "thread-1",
                "messages": [
                    {"id": f"m{index}", "body_text": "汉" * 24_000, "body_truncated": False}
                    for index in range(5)
                ],
                "returned_count": 5,
                "has_earlier": False,
                "next_before_index": None,
                "latest_message_id": "m4",
            },
        },
    }
    fitted = common._limit_inbox_thread_response_frame(frame)
    assert common._encoded_frame_size(fitted) <= common.MAX_INBOX_THREAD_RESPONSE_BYTES
    assert all(not item.get("body_html") for item in fitted["result"]["data"]["messages"])

    print("PASS inbox thread response budget tests")


if __name__ == "__main__":
    main()
