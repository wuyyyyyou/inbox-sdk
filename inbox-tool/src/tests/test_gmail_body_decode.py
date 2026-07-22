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
    links = adapter.extract_external_links_from_message(html_only)
    check("html link metadata is extracted", any(link["url"] == "https://billing.example.com/invoice/123" for link in links))

    unsafe_links = _message([
        _part("text/html", '<a href="javascript:alert(1)">bad</a><a href="https://safe.example.com/path">safe</a>'),
    ])
    safe_links = adapter.extract_external_links_from_message(unsafe_links)
    check("unsafe links are filtered", all(not link["url"].startswith("javascript:") for link in safe_links))
    check("safe links still render", any(link["host"] == "safe.example.com" for link in safe_links))

    attachment_msg = _message([_part("text/plain", "Please review the attachment.")])
    attachment_msg["attachments"] = [
        {"filename": "offer.pdf", "mimeType": "application/pdf", "size": 1234, "attachmentId": "att-1"},
        {"filename": "", "mimeType": "image/png", "size": 10, "attachmentId": "inline"},
    ]
    attachments = adapter.attachment_metadata_from_message(attachment_msg)
    check("attachment metadata filters non-downloadable parts", len(attachments) == 1)
    check("attachment metadata has opaque id", attachments[0]["id"] and "att-1" not in attachments[0]["id"])
    found = adapter.find_attachment_for_token(attachment_msg, attachments[0]["id"])
    check("attachment token resolves to gmail attachment id", found["gmail_attachment_id"] == "att-1")

    inline_image = {
        "mimeType": "image/png",
        "filename": "icon.png",
        "headers": [
            {"name": "Content-ID", "value": "<icon@example.com>"},
            {"name": "Content-Disposition", "value": "inline; filename=icon.png"},
        ],
        "body": {"attachmentId": "inline-image", "size": 5600},
    }
    explicit_attachment = {
        "mimeType": "image/png",
        "filename": "photo.png",
        "headers": [{"name": "Content-Disposition", "value": "attachment; filename=photo.png"}],
        "body": {"attachmentId": "photo-attachment", "size": 1200},
    }
    extracted = adapter._extract_attachments({"parts": [inline_image, explicit_attachment]})
    check("inline CID image is not listed as attachment", [item["filename"] for item in extracted] == ["photo.png"])

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
