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

    def do_PUT(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
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


async def main_async() -> None:
    from anna_inbox_executa import v2_tools
    from executa_sdk.host_upload import HostUploadClient
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
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PutHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        etag = v2_tools._put_presigned_url_sync(
            f"http://127.0.0.1:{server.server_port}/upload",
            {},
            b"helper-bytes",
            "application/octet-stream",
        )
    finally:
        server.shutdown()
        server.server_close()
    check("presigned helper returns etag", etag == "etag-local")
    check("presigned helper sends body", PutHandler.received_body == b"helper-bytes")

    original_host_upload = v2_tools.host_upload
    original_put = v2_tools._put_presigned_url_sync
    fake_host_upload = FakeInlineOnlyHostUpload()
    v2_tools.host_upload = fake_host_upload
    v2_tools._put_presigned_url_sync = lambda *args, **kwargs: "etag-1"
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
        v2_tools.host_upload = original_host_upload
        v2_tools._put_presigned_url_sync = original_put

    check("download url comes from host upload", result["download_url"].startswith("https://files.example.test/"))
    check("host inline upload is not used", not fake_host_upload.inline_called)
    check("host negotiate was called once", len(fake_host_upload.negotiate_calls) == 1)
    check("attachment purpose is set", fake_host_upload.negotiate_calls[0]["purpose"] == "user_artifact")
    check("declared bytes is set", fake_host_upload.negotiate_calls[0]["size_bytes"] == len(b"pdf-bytes"))
    check("host confirm was called", fake_host_upload.confirmed)
    check("content bytes stay out of JSON result", "content" not in result and "content_b64" not in result)

    fake_presigned_upload = FakePresignedHostUpload()
    v2_tools.host_upload = fake_presigned_upload

    def _raise_ssl_eof(*args: Any, **kwargs: Any) -> str:
        raise OSError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")

    v2_tools._put_presigned_url_sync = _raise_ssl_eof
    try:
        presigned_result = await v2_tools._upload_attachment_for_download(
            "user@example.com",
            "card-1",
            {
                "filename": "large.pdf",
                "mime_type": "application/pdf",
                "message_id": "msg-1",
            },
            b"pdf-bytes",
        )
    finally:
        v2_tools.host_upload = original_host_upload
        v2_tools._put_presigned_url_sync = original_put

    check("presigned upload confirms after put eof", fake_presigned_upload.confirmed)
    check("presigned download url is returned", presigned_result["download_url"].endswith("/presigned.pdf"))

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
    check("loopback payload serves bytes", loopback_body == b"loopback-bytes")
    check("loopback payload serves attachment header", "loopback.txt" in loopback_disposition)


def main() -> None:
    asyncio.run(main_async())
    print("PASS attachment download host upload tests")


if __name__ == "__main__":
    main()
