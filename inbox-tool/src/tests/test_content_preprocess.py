"""正文、签名与附件派生内容的回归测试。"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"PASS {label}")


def main() -> None:
    from mail_agent.content_preprocess import parse_attachment_content, preprocess_message, split_message_body

    parts = split_message_body(
        "Please review the invoice.\n\nBest regards,\nJane Doe\nFinance Director\nAcme Inc\njane@acme.example\n+1 415 555 1234\n"
        "\nOn Tue, sender@example.com wrote:\nOld quoted message"
    )
    check("quote is separated", parts["quoted_text"].startswith("On Tue"), str(parts))
    check("signature is separated", "Jane Doe" in parts["signature"], str(parts))
    check("body excludes quote", "Old quoted" not in parts["body"], str(parts))

    message = {
        "body_text": "Invoice INV-1001 total USD 11.00 has been paid.\n\n-- \nJane Doe\nCEO\nAcme\njane@acme.example",
        "attachments": [{"filename": "invoice.txt", "mimeType": "text/plain", "attachmentId": "att-1", "size": 32}],
    }
    preprocess_message(message, fetch_attachment=lambda attachment_id: b"Invoice INV-2002 total USD 22.00 overdue")
    analysis = message["content_analysis"]
    check("signature contact extracted", analysis["contacts"][0]["emails"] == "jane@acme.example", str(analysis))
    check("body invoice remains body sourced", analysis["body_invoice_facts"][0]["source"] == "body", str(analysis))
    check("attachment invoice remains attachment sourced", analysis["attachment_analysis"][0]["facts"][0]["source"] == "attachment", str(analysis))
    check("amount sources are separate", "11.00" in analysis["body_invoice_facts"][0]["amounts"] and "22.00" in analysis["attachment_analysis"][0]["facts"][0]["amounts"], str(analysis))

    office = io.BytesIO()
    with zipfile.ZipFile(office, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Proposal amount USD 99.00</w:t></w:r></w:p></w:body></w:document>',
        )
    office_result = parse_attachment_content(office.getvalue(), filename="proposal.docx", mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    check("docx text extracted without Office runtime", "Proposal amount" in office_result["text"], str(office_result))
    check("docx facts extracted", office_result["facts"][0]["source"] == "attachment", str(office_result))

    image_result = parse_attachment_content(b"not-a-real-image", filename="scan.png", mime_type="image/png")
    check("ocr failure is explicit", str(image_result["status"]).startswith(("ocr_", "parse_error", "unsupported")), str(image_result))


if __name__ == "__main__":
    main()
