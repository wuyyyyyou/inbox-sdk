"""Attachment download should prefer presigned host upload over stdio payloads."""

from __future__ import annotations

import asyncio
import http.server
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)


class PutHandler(http.server.BaseHTTPRequestHandler):
    received_body = b""
    received_headers: dict[str, str] = {}

    def do_PUT(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        PutHandler.received_headers = {str(key): str(value) for key, value in self.headers.items()}
        PutHandler.received_body = self.rfile.read(length)
        self.send_response(200)
        self.send_header("ETag", '"etag-local"')
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return


class FakeInlineOnlyHostUpload:
    MAX_INLINE_BYTES = 8 * 1024 * 1024

    def __init__(self) -> None:
        self.inline_called = False
        self.negotiate_calls: list[dict[str, Any]] = []
        self.confirmed = False

    async def upload_inline(self, **kwargs: Any) -> dict[str, Any]:
        self.inline_called = True
        raise AssertionError("attachment download must not send file bytes through stdio inline upload")

    async def negotiate(self, **kwargs: Any) -> dict[str, Any]:
        self.negotiate_calls.append(kwargs)
        return {
            "put_url": "https://upload.example.test/object",
            "r2_key": "test/key",
            "headers": {"Content-Type": kwargs["mime_type"]},
        }

    async def confirm(self, **kwargs: Any) -> dict[str, Any]:
        self.confirmed = True
        return {
            "download_url": "https://files.example.test/attachment.pdf",
            "r2_key": kwargs["r2_key"],
            "expires_at": "2026-06-24T12:00:00Z",
            "size_bytes": 9,
        }


class FakePresignedHostUpload:
    MAX_INLINE_BYTES = 1

    def __init__(self) -> None:
        self.confirmed = False

    async def negotiate(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "put_url": "https://upload.example.test/object",
            "r2_key": "test/presigned-key",
            "headers": {"Content-Type": kwargs["mime_type"]},
        }

    async def confirm(self, **kwargs: Any) -> dict[str, Any]:
        self.confirmed = True
        return {
            "download_url": "https://files.example.test/presigned.pdf",
            "r2_key": kwargs["r2_key"],
            "expires_at": "2026-06-24T12:00:00Z",
            "size_bytes": 9,
        }


class FakeApsFiles:
    """收件附件下载迁移后的 APS Files fake。"""

    def __init__(self) -> None:
        self.begin_calls: list[dict[str, Any]] = []
        self.complete_calls: list[dict[str, Any]] = []

    async def upload_begin(self, **kwargs: Any) -> dict[str, Any]:
        self.begin_calls.append(kwargs)
        return {
            "put_url": "https://upload.example.test/aps-object",
            "headers": {"Content-Type": kwargs["content_type"]},
        }

    async def upload_complete(self, **kwargs: Any) -> dict[str, Any]:
        self.complete_calls.append(kwargs)
        return {"completed": True}

    async def download_url(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "url": "https://files.example.test/aps-attachment.pdf",
            "expires_at": "2026-06-24T12:00:00Z",
        }


class FakeRetryHostUpload(FakeInlineOnlyHostUpload):
    def __init__(self) -> None:
        super().__init__()
        self.confirm_calls = 0

    async def confirm(self, **kwargs: Any) -> dict[str, Any]:
        self.confirm_calls += 1
        if self.confirm_calls == 1:
            raise RuntimeError("UPLOAD_NOT_FOUND: head_object returned 404")
        return await super().confirm(**kwargs)


class FakeRetryApsFiles(FakeApsFiles):
    async def upload_complete(self, **kwargs: Any) -> dict[str, Any]:
        self.complete_calls.append(kwargs)
        if len(self.complete_calls) == 1:
            raise RuntimeError("-32029 pending_upload: R2 head_object failed (upload incomplete?)")
        return {"completed": True}


class FakeFieldsOnlyApsFiles(FakeApsFiles):
    async def upload_begin(self, **kwargs: Any) -> dict[str, Any]:
        self.begin_calls.append(kwargs)
        return {"fields": {"Content-Type": kwargs["content_type"]}}


async def main_async() -> None:
    from anna_inbox_executa import v2_tools
    from executa_sdk.host_upload import HostUploadClient
    from mail_agent.mail_providers.gmail import adapter as gmail_adapter
    import os

    frames: list[dict[str, Any]] = []
    client = HostUploadClient(write_frame=frames.append)
    negotiate_task = asyncio.create_task(
        client.negotiate(
            filename="invoice.pdf",
            mime_type="application/pdf",
            size_bytes=123,
            purpose="user_artifact",
            timeout=1.0,
        )
    )
    await asyncio.sleep(0)
    check("host upload negotiate wrote one frame", len(frames) == 1)
    params = frames[0]["params"]
    check("host upload negotiate sends expected_bytes", params["expected_bytes"] == 123)
    check("host upload negotiate keeps size_bytes compatibility", params["size_bytes"] == 123)
    client.dispatch_response({"id": frames[0]["id"], "result": {"put_url": "https://upload.example.test", "r2_key": "key", "headers": {}}})
    await negotiate_task

    PutHandler.received_body = b""
    PutHandler.received_headers = {}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PutHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        etag = v2_tools._put_presigned_url_sync(
            f"http://127.0.0.1:{server.server_port}/upload",
            {"Content-Length": str(len(b"helper-bytes")), "X-Signed": "yes"},
            b"helper-bytes",
            "application/octet-stream",
        )
    finally:
        server.shutdown()
        server.server_close()
    check("presigned helper returns etag", etag == "etag-local")
    check("presigned helper sends body", PutHandler.received_body == b"helper-bytes")
    check("presigned helper keeps signed headers only", "User-Agent" not in PutHandler.received_headers)
    check("presigned helper does not inject connection", "Connection" not in PutHandler.received_headers)
    check("presigned helper sends signed custom header", PutHandler.received_headers.get("X-Signed") == "yes")

    original_aps_files = v2_tools._aps_files
    original_host_upload = v2_tools.host_upload
    original_put = v2_tools._put_presigned_url_sync
    fake_aps_files = FakeApsFiles()
    v2_tools._aps_files = fake_aps_files
    v2_tools._put_presigned_url_sync = lambda *args, **kwargs: "etag-1"

    # Host 优先：协商成功时不应落到 APS Files。
    fake_host = FakeInlineOnlyHostUpload()
    v2_tools.host_upload = fake_host
    try:
        host_result = await v2_tools._upload_attachment_for_download(
            "user@example.com",
            "card-1",
            {
                "filename": "invoice.pdf",
                "mime_type": "application/pdf",
                "message_id": "msg-1",
            },
            b"pdf-bytes",
        )
    finally:
        v2_tools.host_upload = original_host_upload
    check("host preferred returns download url", host_result["download_url"].startswith("https://files.example.test/"))
    check("host preferred negotiates once", len(fake_host.negotiate_calls) == 1)
    check("host preferred confirms once", fake_host.confirmed is True)
    check("host preferred skips APS begin", len(fake_aps_files.begin_calls) == 0)

    # Host confirm 404 必须重新 negotiate，不能复用第一次的 r2_key。
    retry_host = FakeRetryHostUpload()
    v2_tools.host_upload = retry_host
    try:
        retry_host_result = await v2_tools._upload_attachment_for_download(
            "user@example.com", "card-retry-host", {"filename": "invoice.pdf", "mime_type": "application/pdf"}, b"pdf-bytes"
        )
    finally:
        v2_tools.host_upload = original_host_upload
    check("host confirm 404 retries with a second negotiate", len(retry_host.negotiate_calls) == 2)
    check("host retry confirms only the fresh attempt", retry_host.confirm_calls == 2)
    check("host retry still returns download url", retry_host_result["download_url"].startswith("https://files.example.test/"))

    # Host 协商不可用时回退 APS Files。
    class _HostUnavailable:
        async def negotiate(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("host upload disabled in test")

    v2_tools.host_upload = _HostUnavailable()
    try:
        result = await v2_tools._upload_attachment_for_download(
            "user@example.com",
            "card-1",
            {
                "filename": "invoice.pdf",
                "mime_type": "application/pdf",
                "message_id": "msg-1",
            },
            b"pdf-bytes",
        )
    finally:
        v2_tools._aps_files = original_aps_files
        v2_tools.host_upload = original_host_upload
        v2_tools._put_presigned_url_sync = original_put

    check("download url comes from APS Files fallback", result["url"].startswith("https://files.example.test/"))
    check("APS upload begins once after host fail", len(fake_aps_files.begin_calls) == 1)
    check("APS upload carries declared bytes", fake_aps_files.begin_calls[0]["size_bytes"] == len(b"pdf-bytes"))
    check("APS upload completes once", len(fake_aps_files.complete_calls) == 1)
    check("content bytes stay out of JSON result", "content" not in result and "content_b64" not in result)

    # APS complete 的 pending/404 必须重新 begin，不能重复 complete 原 pending attempt。
    retry_aps = FakeRetryApsFiles()
    v2_tools._aps_files = retry_aps
    v2_tools.host_upload = _HostUnavailable()
    v2_tools._put_presigned_url_sync = lambda *args, **kwargs: "etag-retry"
    try:
        retry_aps_result = await v2_tools._upload_attachment_for_download(
            "user@example.com", "card-retry-aps", {"filename": "invoice.pdf", "mime_type": "application/pdf"}, b"pdf-bytes"
        )
    finally:
        v2_tools._aps_files = original_aps_files
        v2_tools.host_upload = original_host_upload
        v2_tools._put_presigned_url_sync = original_put
    check("APS pending complete retries with a second begin", len(retry_aps.begin_calls) == 2)
    check("APS retries complete each fresh attempt once", len(retry_aps.complete_calls) == 2)
    check("APS retry returns download url after complete", retry_aps_result["url"].startswith("https://files.example.test/"))

    fields_only_aps = FakeFieldsOnlyApsFiles()
    v2_tools._aps_files = fields_only_aps
    v2_tools.host_upload = _HostUnavailable()
    v2_tools._put_presigned_url_sync = lambda *args, **kwargs: "etag-fields"
    try:
        try:
            await v2_tools._upload_attachment_for_download(
                "user@example.com", "card-fields-only", {"filename": "invoice.pdf", "mime_type": "application/pdf"}, b"pdf-bytes"
            )
        except RuntimeError as exc:
            check("fields-only APS response is a protocol error", "fields without put_url" in str(exc))
        else:
            raise AssertionError("fields-only APS response must fail")
    finally:
        v2_tools._aps_files = original_aps_files
        v2_tools.host_upload = original_host_upload
        v2_tools._put_presigned_url_sync = original_put

    # Gmail 的不透明附件令牌可能超过平台对对象路径段的 128 字符限制。
    long_attachment_token = "eyJ" + "a" * 256
    v2_tools._aps_files = fake_aps_files
    v2_tools.host_upload = _HostUnavailable()
    v2_tools._put_presigned_url_sync = lambda *args, **kwargs: "etag-2"
    try:
        long_token_result = await v2_tools._upload_attachment_for_download(
            "user@example.com",
            "message-1",
            {
                "id": long_attachment_token,
                "filename": "deck.pdf",
                "mime_type": "application/pdf",
                "message_id": "message-1",
            },
            b"pdf-bytes",
        )
    finally:
        v2_tools._aps_files = original_aps_files
        v2_tools.host_upload = original_host_upload
        v2_tools._put_presigned_url_sync = original_put
    long_token_path = str(fake_aps_files.begin_calls[-1]["path"])
    check(
        "long attachment token upload returns a URL",
        long_token_result["url"].startswith("https://files.example.test/"),
    )
    check("APS path does not expose opaque attachment token", long_attachment_token not in long_token_path)
    check(
        "APS path segments stay within platform limit",
        all(len(segment) <= 128 for segment in long_token_path.split("/")),
    )

    os.environ.pop("ANNA_INBOX_ATTACHMENT_DOWNLOAD_MODE", None)
    check("default attachment download mode is direct inline", v2_tools._attachment_download_mode() == "direct_inline")
    os.environ["ANNA_INBOX_ATTACHMENT_DOWNLOAD_MODE"] = "host_preferred"
    check("host preferred mode can be enabled", v2_tools._attachment_download_mode() == "host_preferred")
    os.environ.pop("ANNA_INBOX_ATTACHMENT_DOWNLOAD_MODE", None)

    inline_payload = v2_tools._inline_attachment_download_payload(
        {
            "filename": "tiny.txt",
            "mime_type": "text/plain",
        },
        b"tiny-bytes",
    )
    check("inline fallback marks ok", inline_payload["ok"] is True)
    check("inline fallback marks delivery mode", inline_payload["delivery"] == "inline")
    check("inline fallback carries base64", inline_payload["content_b64"] == "dGlueS1ieXRlcw==")
    check("inline fallback omits download url", "download_url" not in inline_payload)

    loopback_payload = v2_tools._loopback_attachment_download_payload(
        {
            "filename": "loopback.txt",
            "mime_type": "text/plain",
        },
        b"loopback-bytes",
    )
    check("loopback payload returns url", loopback_payload["download_url"].startswith("http://127.0.0.1:"))
    with urllib.request.urlopen(loopback_payload["download_url"], timeout=5) as response:
        loopback_body = response.read()
        loopback_disposition = str(response.headers.get("Content-Disposition") or "")
        loopback_cors = str(response.headers.get("Access-Control-Allow-Origin") or "")
    check("loopback payload serves bytes", loopback_body == b"loopback-bytes")
    check("loopback payload serves attachment header", "loopback.txt" in loopback_disposition)
    check("loopback payload serves cors header", loopback_cors == "*")

    preview_payload = v2_tools._loopback_attachment_download_payload(
        {
            "filename": "sponsor-brief.pdf",
            "mime_type": "application/pdf",
        },
        b"preview-pdf-bytes",
        disposition="inline",
    )
    preview_url = str(preview_payload["download_url"])
    check("loopback preview uses dedicated route", "/preview/" in preview_url)
    check("loopback preview omits filename", "sponsor-brief.pdf" not in preview_url)
    with urllib.request.urlopen(preview_url, timeout=5) as response:
        preview_body = response.read()
        preview_disposition = str(response.headers.get("Content-Disposition") or "")
        preview_type = str(response.headers.get("Content-Type") or "")
    check("loopback preview serves bytes", preview_body == b"preview-pdf-bytes")
    check("loopback preview serves inline header", preview_disposition.startswith("inline;"))
    check("loopback preview serves pdf type", preview_type == "application/pdf")

    serialized = v2_tools._serialize_inbox_thread_message(
        {
            "id": "msg-serialize",
            "thread_id": "thread-1",
            "internal_date": "123",
            "from": "A <a@example.com>",
            "to": "B <b@example.com>",
            "subject": "Subject",
            "label_ids": ["INBOX"],
            "attachments": [
                {
                    "filename": "thread.pdf",
                    "mimeType": "application/octet-stream",
                    "attachmentId": "gmail-att-serialize",
                    "size": 77,
                },
            ],
        },
        include_display_body=False,
        body_limit=0,
    )
    check("thread serialization rewrites attachment list", len(serialized["attachments"]) == 1)
    check("thread serialization injects opaque attachment id", bool(serialized["attachments"][0]["id"]))
    check("thread serialization keeps message id", serialized["attachments"][0]["message_id"] == "msg-serialize")

    original_read_message = gmail_adapter.read_message
    original_find_attachment_for_token = gmail_adapter.find_attachment_for_token
    original_fetch_attachment_bytes = gmail_adapter.fetch_attachment_bytes
    original_normalize_mailbox = gmail_adapter.normalize_mailbox
    original_should_use_aps_storage = v2_tools._should_use_aps_storage
    original_upload_for_download = v2_tools._upload_attachment_for_download
    original_download_mode = v2_tools._attachment_download_mode
    try:
        gmail_adapter.read_message = lambda mailbox, message_id: {"id": message_id, "attachments": []}
        gmail_adapter.find_attachment_for_token = lambda message, token: {
            "id": token,
            "message_id": str(message.get("id") or ""),
            "filename": "report.pdf",
            "mime_type": "application/octet-stream",
            "size": len(b"preview-bytes"),
            "gmail_attachment_id": "gmail-att-1",
        }
        gmail_adapter.fetch_attachment_bytes = lambda mailbox, message_id, gmail_attachment_id: b"preview-bytes"
        gmail_adapter.normalize_mailbox = lambda mailbox: mailbox.strip().lower()
        v2_tools._should_use_aps_storage = lambda: True
        v2_tools._attachment_download_mode = lambda: "direct_inline"

        preview_result = await v2_tools._handle_v2_tool(
            "prepare_inbox_attachment_access",
            {
                "mailbox": "User@Example.com",
                "message_id": "msg-77",
                "attachment_id": "token-77",
                "mode": "preview",
            },
            "invoke-1",
        )
        check("preview tool marks ok", preview_result["ok"] is True)
        check("preview tool returns url delivery", preview_result["delivery"] == "url")
        check("preview tool returns mode", preview_result["mode"] == "preview")
        check("preview tool normalizes mime from filename", preview_result["mime_type"] == "application/pdf")
        check("preview tool carries attachment id", preview_result["attachment_id"] == "token-77")
        check("preview tool returns preview url", str(preview_result["preview_url"]).startswith("http://127.0.0.1:"))
        check("preview tool uses dedicated preview route", "/preview/" in str(preview_result["preview_url"]))
        check("preview tool does not expose a download route", "/download/" not in str(preview_result["preview_url"]))

        gmail_adapter.find_attachment_for_token = lambda message, token: {
            "id": token,
            "message_id": str(message.get("id") or ""),
            "filename": "archive.zip",
            "mime_type": "application/zip",
            "size": len(b"archive-bytes"),
            "gmail_attachment_id": "gmail-att-unsupported",
        }
        unsupported_preview = await v2_tools._handle_v2_tool(
            "prepare_inbox_attachment_access",
            {
                "mailbox": "User@Example.com",
                "message_id": "msg-unsupported",
                "attachment_id": "token-unsupported",
                "mode": "preview",
            },
            "invoke-unsupported",
        )
        check("unsupported preview is rejected by backend", unsupported_preview["ok"] is False)
        check("unsupported preview explains download fallback", "Download" in unsupported_preview["error"])

        gmail_adapter.find_attachment_for_token = lambda message, token: {
            "id": token,
            "message_id": str(message.get("id") or ""),
            "filename": "report.pdf",
            "mime_type": "application/octet-stream",
            "size": len(b"preview-bytes"),
            "gmail_attachment_id": "gmail-att-1",
        }

        async def _raise_upload(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("host upload unavailable")

        v2_tools._attachment_download_mode = lambda: "host_preferred"
        v2_tools._upload_attachment_for_download = _raise_upload
        download_result = await v2_tools._handle_v2_tool(
            "prepare_inbox_attachment_access",
            {
                "mailbox": "user@example.com",
                "message_id": "msg-88",
                "attachment_id": "token-88",
                "mode": "download",
            },
            "invoke-2",
        )
        check("local download falls back to loopback", download_result["delivery"] == "url")
        check("local fallback stays on loopback", str(download_result["download_url"]).startswith("http://127.0.0.1:"))
        check("download tool returns mode", download_result["mode"] == "download")
        check("download tool returns message id", download_result["message_id"] == "msg-88")
        check("download tool normalizes mime", download_result["mime_type"] == "application/pdf")
    finally:
        gmail_adapter.read_message = original_read_message
        gmail_adapter.find_attachment_for_token = original_find_attachment_for_token
        gmail_adapter.fetch_attachment_bytes = original_fetch_attachment_bytes
        gmail_adapter.normalize_mailbox = original_normalize_mailbox
        v2_tools._should_use_aps_storage = original_should_use_aps_storage
        v2_tools._upload_attachment_for_download = original_upload_for_download
        v2_tools._attachment_download_mode = original_download_mode


def main() -> None:
    asyncio.run(main_async())
    print("PASS attachment download host upload tests")


if __name__ == "__main__":
    main()
