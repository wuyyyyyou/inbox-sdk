"""外发附件 stage 校验与路径安全。"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)


def main() -> None:
    from mail_agent.mail_providers.gmail import outgoing_attachments as oa

    check("blocks exe", oa.is_blocked_outgoing_filename("setup.exe"))
    check("allows pdf", not oa.is_blocked_outgoing_filename("report.pdf"))
    check("allows png", not oa.is_blocked_outgoing_filename("photo.PNG"))

    meta = oa.validate_outgoing_attachment_meta(filename="a.pdf", mime_type="application/pdf", size=1024)
    check("normalized filename", meta["filename"] == "a.pdf")
    check("size kept", meta["size"] == 1024)

    try:
        oa.validate_outgoing_attachment_meta(filename="x.exe", mime_type="application/octet-stream", size=10)
        raise AssertionError("exe should raise")
    except ValueError:
        pass

    try:
        oa.validate_outgoing_attachment_meta(
            filename="big.bin",
            mime_type="application/octet-stream",
            size=oa.OUTGOING_ATTACHMENT_TOTAL_MAX_BYTES + 1,
        )
        raise AssertionError("oversize should raise")
    except ValueError:
        pass

    try:
        oa.validate_outgoing_attachment_meta(
            filename="more.bin",
            mime_type="application/octet-stream",
            size=10 * 1024 * 1024,
            existing_total_bytes=20 * 1024 * 1024,
        )
        raise AssertionError("total oversize should raise")
    except ValueError:
        pass

    slot = oa.create_stage_upload_slot(
        "user@example.com",
        filename="note.txt",
        mime_type="text/plain",
        size=5,
    )
    check("slot has attachment id", bool(slot["attachment_id"]))
    check("slot has upload token", bool(slot["upload_token"]))
    committed = oa.commit_stage_upload(slot["upload_token"], b"hello")
    check("commit ok", committed["ok"] is True)
    check("commit size", committed["size"] == 5)
    data = oa.read_staged_attachment("user@example.com", committed["storage_key"])
    check("read bytes", data == b"hello")
    deleted = oa.delete_staged_attachment("user@example.com", committed["storage_key"])
    check("deleted", deleted is True)

    print("PASS outgoing attachment tests")


if __name__ == "__main__":
    main()
