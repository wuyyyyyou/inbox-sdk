"""Gmail body decoding should feed clean text to downstream LLM prompts."""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _part(mime_type: str, text: str) -> dict[str, Any]:
    return {"mimeType": mime_type, "body": {"data": _b64(text)}}


def _message(parts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "msg-1",
        "threadId": "thread-1",
        "internalDate": "1770000000000",
        "labelIds": ["INBOX"],
        "snippet": "snippet",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "sender@example.com"},
                {"name": "To", "value": "user@example.com"},
                {"name": "Subject", "value": "HTML test"},
            ],
            "parts": parts,
        },
    }


def main() -> None:
    from mail_agent.mail_providers.gmail import adapter

    mixed = _message([
        _part("text/plain", "Thanks for reviewing the proposal."),
        _part("text/html", "<html><body><style>.x{color:red}</style><p>HTML duplicate</p></body></html>"),
    ])
    text = adapter._decode_body(mixed)
    check("effective text/plain is preferred", text == "Thanks for reviewing the proposal.")

    html_only = _message([
        _part(
            "text/html",
            """
            <html><head><style>.hidden{display:none}</style><script>alert(1)</script></head>
            <body><table><tr><td>Invoice ready</td></tr></table>
            <a href="https://billing.example.com/invoice/123">Review invoice</a></body></html>
            """,
        )
    ])
    html_text = adapter._decode_body(html_only)
    display_body = adapter.decode_body_for_display(html_only)
    check("html text is preserved", "Invoice ready" in html_text)
    check("link destination is preserved", "https://billing.example.com/invoice/123" in html_text)
    check("style tag is removed", "<style" not in html_text.lower())
    check("script tag is removed", "<script" not in html_text.lower())
    check("table tag is removed", "<table" not in html_text.lower())
    check("display html keeps original table markup", "<table" in display_body["html"].lower())
    check("display html keeps original link markup", "href=\"https://billing.example.com/invoice/123\"" in display_body["html"])

    placeholder_plain = _message([
        _part("text/plain", "View this email in your browser."),
        _part("text/html", "<html><body><p>Security alert for your account.</p></body></html>"),
    ])
    fallback_text = adapter._decode_body(placeholder_plain)
    check("placeholder plain falls back to html", "Security alert" in fallback_text)
    check("placeholder plain is not used", "View this email" not in fallback_text)

    normalized = adapter._normalize_message("user@example.com", html_only)
    normalized["body_text"] = "<html><body><style>noise</style><p>Cached invoice</p></body></html>"
    detail = adapter._to_message_detail(normalized)
    check("cached raw html is re-decoded", "Invoice ready" in detail.body_text)
    check("cached raw html tags are not returned", "<html" not in detail.body_text.lower())

    print("PASS gmail body decode tests")


if __name__ == "__main__":
    main()
