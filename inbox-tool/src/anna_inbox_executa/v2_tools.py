from __future__ import annotations

import re
from html import escape as html_escape

from anna_inbox_executa.common import *
from anna_inbox_executa.card_tools import _handle_generate_draft_background, _handle_summarize_background, _serialize_card_for_frontend
from anna_inbox_executa.gmail_tools import _dedup_body, _resolve_cid_images, _sanitize_email_html
from anna_inbox_executa.sampling_tools import *
from anna_inbox_executa.storage_tools import *

INLINE_ATTACHMENT_DIRECT_MAX_BYTES = 4 * 1024 * 1024
# APS / Host reverse-RPC 单次协商超时：需明显短于前端 tools.invoke 60s，
# 以便失败时仍能返回可读错误，而不是被宿主整调用超时淹没。
APS_FILES_REVERSE_RPC_TIMEOUT_SECONDS = 20.0
HOST_UPLOAD_REVERSE_RPC_TIMEOUT_SECONDS = 20.0
_DOWNLOAD_SERVER_LOCK = threading.Lock()
_DOWNLOAD_SERVER: Any | None = None
_DOWNLOAD_SERVER_THREAD: threading.Thread | None = None
_DOWNLOAD_TOKENS: dict[str, dict[str, Any]] = {}
_DOWNLOAD_TOKEN_TTL_SECONDS = 15 * 60
_CID_IMAGE_RE = re.compile(r'''src\s*=\s*["']cid:([^"'\s]+)["']''', re.IGNORECASE)


def _strip_quoted_reply_html(html: str) -> str:
    """Remove common quoted-reply blocks from display HTML while preserving new content."""
    from html import escape as _html_escape
    from html import unescape as _html_unescape
    from html.parser import HTMLParser as _HTMLParser
    import re as _re

    raw_html = str(html or "")
    if not raw_html.strip():
        return ""

    quote_container_classes = {"gmail_quote", "yahoo_quoted", "protonmail_quote"}
    quote_boundary_classes = {"gmail_attr", "moz-cite-prefix"}
    quote_boundary_ids = {"divrplyfwdmsg"}
    void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    on_wrote_re = _re.compile(r"\bOn\s+(.{1,700}?)\s+wrote:\s*$", _re.IGNORECASE | _re.DOTALL)
    chinese_wrote_re = _re.compile(r"(?:在\s*)?.{1,700}?写道[:：]\s*$", _re.DOTALL)
    original_message_re = _re.compile(r"-{2,}\s*Original Message\s*-{2,}\s*$", _re.IGNORECASE)
    forwarded_message_re = _re.compile(r"-{2,}\s*Forwarded message\s*-{2,}\s*$", _re.IGNORECASE)
    quote_date_or_addr_re = _re.compile(
        r"(@|<[^>]+@[^>]+>|\b\d{4}\b|\b\d{1,2}:\d{2}\b|"
        r"\b(?:mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
        r"january|february|march|april|june|july|august|september|october|november|december)\b)",
        _re.IGNORECASE,
    )

    def _text_from_html(fragment: str) -> str:
        text = _re.sub(r"<[^>]+>", " ", fragment)
        return _re.sub(r"\s+", " ", _html_unescape(text)).strip()

    def _looks_like_quote_header_text(text: str) -> bool:
        normalized = _re.sub(r"\s+", " ", str(text or "")).strip()
        if not normalized:
            return False
        if original_message_re.search(normalized) or forwarded_message_re.search(normalized):
            return True
        if chinese_wrote_re.search(normalized):
            return normalized.startswith("在") or bool(quote_date_or_addr_re.search(normalized))
        match = on_wrote_re.search(normalized)
        if not match:
            return False
        return bool(quote_date_or_addr_re.search(match.group(1)))

    class _QuotedReplyHTMLStripper(_HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []
            self.skip_depth = 0
            self.done = False

        def _quote_action(self, attrs: list[tuple[str, str | None]]) -> str:
            for name, value in attrs:
                normalized = name.lower()
                if normalized == "class":
                    classes = {part.strip().lower() for part in str(value or "").split()}
                    if classes & quote_boundary_classes:
                        return "stop"
                    if classes & quote_container_classes:
                        return "skip"
                if normalized == "id" and str(value or "").strip().lower() in quote_boundary_ids:
                    return "stop"
            return ""

        def _attrs_html(self, attrs: list[tuple[str, str | None]]) -> str:
            rendered: list[str] = []
            for name, value in attrs:
                if value is None:
                    rendered.append(f" {name}")
                else:
                    rendered.append(f' {name}="{_html_escape(str(value), quote=True)}"')
            return "".join(rendered)

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if self.done:
                return
            normalized = tag.lower()
            if self.skip_depth:
                self.skip_depth += 1
                return
            action = self._quote_action(attrs)
            if action == "stop":
                self.done = True
                return
            if action == "skip":
                self.skip_depth = 1
                return
            self.parts.append(f"<{tag}{self._attrs_html(attrs)}>")

        def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if self.done or self.skip_depth or self._quote_action(attrs):
                return
            self.parts.append(f"<{tag}{self._attrs_html(attrs)} />")

        def handle_endtag(self, tag: str) -> None:
            if self.done:
                return
            if self.skip_depth:
                self.skip_depth -= 1
                return
            if tag.lower() not in void_tags:
                self.parts.append(f"</{tag}>")

        def handle_data(self, data: str) -> None:
            if not self.done and not self.skip_depth:
                # 清理纯文本引用前缀，避免详情页把历史引用显示成连续 >>。
                normalized = _re.sub(r"(?m)^[ \t]*>{2,}[ \t]?", "", data)
                self.parts.append(_html_escape(normalized, quote=False))

        def handle_entityref(self, name: str) -> None:
            if not self.done and not self.skip_depth:
                self.parts.append(f"&{name};")

        def handle_charref(self, name: str) -> None:
            if not self.done and not self.skip_depth:
                self.parts.append(f"&#{name};")

    parser = _QuotedReplyHTMLStripper()
    try:
        parser.feed(raw_html)
        parser.close()
        cleaned = "".join(parser.parts)
    except Exception:
        cleaned = raw_html

    boundary_re = _re.compile(
        r"(?P<prefix>^|<br\s*/?>\s*|</(?:p|div|li|tr|table|blockquote|section|article|h[1-6])>\s*)"
        r"(?P<open>(?:<(?:p|div|span|font|blockquote|section|article|li|td|th|center)\b[^>]*>\s*)*)"
        r"(?P<header>(?:On\s+.{1,700}?\s+wrote:|(?:在\s*)?.{1,700}?写道[:：]|-{2,}\s*(?:Original Message|Forwarded message)\s*-{2,}))",
        _re.IGNORECASE | _re.DOTALL,
    )

    for match in boundary_re.finditer(cleaned):
        header_text = _text_from_html(match.group("header"))
        if not _looks_like_quote_header_text(header_text):
            continue
        cut_at = match.start("open") if match.group("open") else match.start("prefix")
        return cleaned[:cut_at].strip()

    return cleaned.strip()


def _attachment_download_mode() -> str:
    raw = str(os.environ.get("ANNA_INBOX_ATTACHMENT_DOWNLOAD_MODE") or "").strip().lower()
    if raw in {"host", "host_preferred", "upload"}:
        return "host_preferred"
    if raw in {"loopback", "local_url", "local-http"}:
        return "loopback"
    return "direct_inline"


def _safe_attachment_filename(filename: str) -> str:
    import re
    name = str(filename or "attachment").strip().replace("\\", "_").replace("/", "_")
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return name[:160] or "attachment"


def _put_presigned_url_sync(url: str, headers: dict[str, Any], content: bytes, mime_type: str) -> str:
    import subprocess
    import tempfile

    normalized_headers = {str(k): str(v) for k, v in (headers or {}).items()}
    if not any(key.lower() == "content-type" for key in normalized_headers):
        normalized_headers["Content-Type"] = mime_type or "application/octet-stream"
    if not any(key.lower() == "content-length" for key in normalized_headers):
        normalized_headers["Content-Length"] = str(len(content))
    if not any(key.lower() == "user-agent" for key in normalized_headers):
        normalized_headers["User-Agent"] = "anna-inbox-attachment-upload/1.0"
    if not any(key.lower() == "connection" for key in normalized_headers):
        normalized_headers["Connection"] = "close"

    helper = r"""
import json
import sys
import urllib.request

payload = json.loads(sys.stdin.read())
with open(payload["path"], "rb") as handle:
    data = handle.read()
request = urllib.request.Request(
    payload["url"],
    data=data,
    headers=payload["headers"],
    method="PUT",
)
with urllib.request.urlopen(request, timeout=float(payload["timeout"])) as response:
    sys.stdout.write(str(response.headers.get("ETag") or "").strip('"'))
"""
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix="anna-inbox-upload-", delete=False) as handle:
            handle.write(content)
            temp_path = handle.name
        payload = json.dumps(
            {
                "url": url,
                "headers": normalized_headers,
                "path": temp_path,
                "timeout": 120,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        completed = subprocess.run(
            [sys.executable, "-c", helper],
            input=payload,
            text=True,
            capture_output=True,
            timeout=150,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"presigned PUT helper exited with code {completed.returncode}: {detail[-600:]}")
        return completed.stdout.strip()
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


async def _upload_attachment_for_download(mailbox: str, card_id: str, attachment: dict[str, Any], content: bytes) -> dict[str, Any]:
    """将收件附件上传到可访问的短期 URL。

    优先 Host transient upload（与 KV 后端选择无关，响应始终可路由）；
    Host 不可用时回退 APS Files。稳定 object path 仅作元数据，URL 不持久化。
    Cloud Agent 不可使用 loopback：浏览器与 Executa 不在同一台机器。
    """
    from mail_agent.mail_providers.gmail.adapter import sanitize_mailbox_id

    filename = _safe_attachment_filename(str(attachment.get("filename") or "attachment"))
    mime_type = _normalized_attachment_mime_type(attachment)
    meta = {
        "mailbox": mailbox,
        "card_id": card_id,
        "message_id": str(attachment.get("message_id") or ""),
        "filename": filename,
        "artifact_kind": "email_attachment",
    }

    # 1) Host transient upload（历史可用路径；不依赖 storage_provider）
    host_presign_started = False
    host_unavailable_error = ""
    try:
        negotiated = await host_upload.negotiate(
            filename=filename,
            mime_type=mime_type,
            size_bytes=len(content),
            purpose="user_artifact",
            metadata=meta,
            timeout=HOST_UPLOAD_REVERSE_RPC_TIMEOUT_SECONDS,
        )
        host_presign_started = True
        put_url = str(negotiated.get("put_url") or "")
        r2_key = str(negotiated.get("r2_key") or "")
        if not put_url or not r2_key:
            raise RuntimeError("Host upload did not return a presigned upload target.")
        put_error: Exception | None = None
        try:
            await asyncio.to_thread(
                _put_presigned_url_sync,
                put_url,
                negotiated.get("headers") or {},
                content,
                mime_type,
            )
        except Exception as exc:
            put_error = exc
            log(f"host attachment presigned PUT returned error before confirm: {type(exc).__name__}: {exc}")
        if put_error is None:
            result = await host_upload.confirm(r2_key=r2_key, timeout=HOST_UPLOAD_REVERSE_RPC_TIMEOUT_SECONDS)
        else:
            try:
                result = await host_upload.confirm(r2_key=r2_key, timeout=HOST_UPLOAD_REVERSE_RPC_TIMEOUT_SECONDS)
            except Exception as confirm_exc:
                raise RuntimeError(
                    f"Temporary attachment upload failed after presigned PUT error: {put_error}"
                ) from confirm_exc
        result["storage_key"] = r2_key
        return result
    except Exception as host_exc:
        # 已开始 PUT 的失败不再吞掉；仅「协商阶段不可用」时继续 APS 回退。
        if host_presign_started:
            raise
        host_unavailable_error = f"{type(host_exc).__name__}: {host_exc}"
        log(f"host attachment upload unavailable: {host_unavailable_error}")

    # 2) APS Files 回退
    path = (
        f"anna-inbox/mailbox/{sanitize_mailbox_id(mailbox)}/attachments/"
        f"{_safe_attachment_filename(card_id)}/{_safe_attachment_filename(str(attachment.get('id') or 'attachment'))}/{filename}"
    )
    begin = await _aps_files.upload_begin(
        path=path,
        size_bytes=len(content),
        content_type=mime_type,
        metadata={
            "mailbox": mailbox,
            "card_id": card_id,
            "message_id": str(attachment.get("message_id") or ""),
            "filename": filename,
        },
        scope="user",
        timeout=APS_FILES_REVERSE_RPC_TIMEOUT_SECONDS,
    )
    put_url = str(begin.get("put_url") or begin.get("presigned_url") or "")
    if not put_url:
        detail = f" Host upload failed first: {host_unavailable_error}" if host_unavailable_error else ""
        raise RuntimeError(f"Anna Files did not return an upload URL.{detail}")
    upload_headers = begin.get("headers") or begin.get("fields") or {}
    put_error = None
    etag = ""
    try:
        etag = await asyncio.to_thread(_put_presigned_url_sync, put_url, upload_headers, content, mime_type)
    except Exception as exc:
        put_error = exc
        log(f"aps files presigned PUT returned error before complete: {type(exc).__name__}: {exc}")
    try:
        await _aps_files.upload_complete(
            path=path,
            etag=etag or None,
            size_bytes=len(content),
            content_type=mime_type,
            scope="user",
            timeout=APS_FILES_REVERSE_RPC_TIMEOUT_SECONDS,
        )
    except Exception as complete_exc:
        if put_error is not None:
            raise RuntimeError(f"Attachment file upload failed after presigned PUT error: {put_error}") from complete_exc
        raise
    result = await _aps_files.download_url(
        path=path,
        expires_in=900,
        scope="user",
        timeout=APS_FILES_REVERSE_RPC_TIMEOUT_SECONDS,
    )
    result["storage_key"] = path
    return result


def _download_presigned_url_sync(url: str) -> bytes:
    """通过 APS 返回的短期 URL 下载字节，不把大内容放进 JSON-RPC。"""
    request = urllib.request.Request(url, headers={"User-Agent": "anna-inbox-aps-files/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def _aps_outgoing_attachment_path(mailbox: str, attachment_id: str, filename: str) -> str:
    from mail_agent.mail_providers.gmail.adapter import sanitize_mailbox_id
    return (
        f"anna-inbox/mailbox/{sanitize_mailbox_id(mailbox)}/outgoing/"
        f"{_safe_attachment_filename(attachment_id)}/{_safe_attachment_filename(filename)}"
    )


async def _load_outgoing_attachments_for_send(mailbox: str, items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """按当前存储后端读取外发附件；APS 模式通过短期 URL 读取对象。"""
    if not _should_use_aps_files():
        from mail_agent.mail_providers.gmail.outgoing_attachments import load_outgoing_attachments_for_send
        return await asyncio.to_thread(load_outgoing_attachments_for_send, mailbox, items)
    loaded: list[dict[str, Any]] = []
    total = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        storage_key = str(item.get("storage_key") or "").strip()
        if not storage_key:
            continue
        access = await _aps_files.download_url(
            path=storage_key,
            expires_in=300,
            scope="user",
            timeout=APS_FILES_REVERSE_RPC_TIMEOUT_SECONDS,
        )
        url = str(access.get("url") or access.get("download_url") or "")
        if not url:
            raise RuntimeError("APS Files did not return a download URL for outgoing attachment.")
        content = await asyncio.to_thread(_download_presigned_url_sync, url)
        total += len(content)
        if total > 25 * 1024 * 1024:
            raise ValueError("Total attachments exceed the 25 MB limit")
        loaded.append({
            "id": str(item.get("id") or ""),
            "filename": _safe_attachment_filename(str(item.get("filename") or "attachment")),
            "mime_type": str(item.get("mime_type") or "application/octet-stream"),
            "size": len(content),
            "content": content,
        })
    return loaded


async def _delete_outgoing_attachments(mailbox: str, items: list[dict[str, Any]] | None) -> None:
    """删除 APS object，local 模式仍调用本地 stage 清理逻辑。"""
    if not _should_use_aps_files():
        from mail_agent.mail_providers.gmail.outgoing_attachments import delete_staged_attachments
        await asyncio.to_thread(delete_staged_attachments, mailbox, items)
        return
    for item in items or []:
        if isinstance(item, dict) and str(item.get("storage_key") or "").strip():
            await _aps_files.delete(
                path=str(item["storage_key"]),
                scope="user",
                timeout=APS_FILES_REVERSE_RPC_TIMEOUT_SECONDS,
            )


def _inline_attachment_download_payload(attachment: dict[str, Any], content: bytes) -> dict[str, Any]:
    mime_type = _normalized_attachment_mime_type(attachment)
    return {
        "ok": True,
        "delivery": "inline",
        "filename": attachment.get("filename") or "attachment",
        "mime_type": mime_type,
        "size": len(content),
        "content_b64": base64.b64encode(content).decode("ascii"),
    }


def _attachment_download_dir() -> Path:
    path = data_root() / "anna-inbox" / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup_expired_download_tokens() -> None:
    now = time.time()
    expired = [token for token, meta in _DOWNLOAD_TOKENS.items() if float(meta.get("expires_at_ts") or 0) <= now]
    for token in expired:
        meta = _DOWNLOAD_TOKENS.pop(token, {})
        file_paths = [meta.get("path"), *(meta.get("cid_paths") or [])]
        for file_path in file_paths:
            if not file_path:
                continue
            try:
                Path(str(file_path)).unlink()
            except OSError:
                pass


def _ensure_loopback_download_server() -> str:
    import http.server
    import socketserver

    global _DOWNLOAD_SERVER, _DOWNLOAD_SERVER_THREAD
    with _DOWNLOAD_SERVER_LOCK:
        if _DOWNLOAD_SERVER is not None:
            return f"http://127.0.0.1:{_DOWNLOAD_SERVER.server_address[1]}"

        class AttachmentDownloadHandler(http.server.BaseHTTPRequestHandler):
            server_version = "AnnaInboxAttachment/1.0"

            def _send_cors_headers(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, PUT, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Content-Length")
                self.send_header("Access-Control-Expose-Headers", "Content-Disposition, Content-Type, Content-Length")

            def do_OPTIONS(self) -> None:
                self.send_response(204)
                self._send_cors_headers()
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

            def do_PUT(self) -> None:
                """接收前端外发附件 stage 上传，字节不经过 JSON-RPC。"""
                path = urllib.parse.urlsplit(self.path).path
                if not path.startswith("/upload/"):
                    self.send_error(404)
                    return
                token = path[len("/upload/"):].split("/", 1)[0].strip()
                try:
                    from mail_agent.mail_providers.gmail.outgoing_attachments import (
                        commit_stage_upload,
                        get_stage_upload_slot,
                    )
                    slot = get_stage_upload_slot(token)
                    if not slot:
                        self.send_error(404)
                        return
                    length = int(self.headers.get("Content-Length") or "0")
                    if length < 0 or length > 25 * 1024 * 1024:
                        self.send_error(413)
                        return
                    content = self.rfile.read(length) if length else b""
                    commit_stage_upload(token, content)
                except Exception:
                    self.send_error(400)
                    return
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                body = b'{"ok":true}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                _cleanup_expired_download_tokens()
                path = urllib.parse.urlsplit(self.path).path
                prefix = next((candidate for candidate in ("/download/", "/preview/", "/message/") if path.startswith(candidate)), "")
                if not prefix:
                    self.send_error(404)
                    return
                token = path[len(prefix):].split("/", 1)[0].strip()
                meta = _DOWNLOAD_TOKENS.get(token)
                if not meta:
                    self.send_error(404)
                    return
                if prefix == "/message/":
                    remainder = path[len(prefix) + len(token):].lstrip("/")
                    if remainder.startswith("cid/"):
                        cid = urllib.parse.unquote(remainder[4:]).strip()
                        cid_meta = (meta.get("cid_images") or {}).get(cid)
                        if not isinstance(cid_meta, dict):
                            self.send_error(404)
                            return
                        file_path = Path(str(cid_meta.get("path") or ""))
                        mime_type = str(cid_meta.get("mime_type") or "image/*")
                    else:
                        file_path = Path(str(meta.get("path") or ""))
                        mime_type = "text/html; charset=utf-8"
                    if not file_path.exists():
                        self.send_error(404)
                        return
                    data = file_path.read_bytes()
                    self.send_response(200)
                    self._send_cors_headers()
                    self.send_header("Content-Type", mime_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                file_path = Path(str(meta.get("path") or ""))
                if not file_path.exists():
                    _DOWNLOAD_TOKENS.pop(token, None)
                    self.send_error(404)
                    return
                filename = _safe_attachment_filename(str(meta.get("filename") or "attachment"))
                mime_type = str(meta.get("mime_type") or "application/octet-stream")
                disposition = "inline" if str(meta.get("disposition") or "").lower() == "inline" else "attachment"
                data = file_path.read_bytes()
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", f'{disposition}; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                return

        class ThreadedLocalServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        _DOWNLOAD_SERVER = ThreadedLocalServer(("127.0.0.1", 0), AttachmentDownloadHandler)
        _DOWNLOAD_SERVER_THREAD = threading.Thread(target=_DOWNLOAD_SERVER.serve_forever, daemon=True, name="anna-inbox-downloads")
        _DOWNLOAD_SERVER_THREAD.start()
        log(f"attachment loopback server listening on http://127.0.0.1:{_DOWNLOAD_SERVER.server_address[1]}")
        return f"http://127.0.0.1:{_DOWNLOAD_SERVER.server_address[1]}"


def _loopback_attachment_download_payload(
    attachment: dict[str, Any],
    content: bytes,
    *,
    disposition: str = "attachment",
) -> dict[str, Any]:
    _cleanup_expired_download_tokens()
    filename = _safe_attachment_filename(str(attachment.get("filename") or "attachment"))
    mime_type = _normalized_attachment_mime_type(attachment)
    token = uuid.uuid4().hex
    file_path = _attachment_download_dir() / f"{token}-{filename}"
    file_path.write_bytes(content)
    expires_at_ts = time.time() + _DOWNLOAD_TOKEN_TTL_SECONDS
    _DOWNLOAD_TOKENS[token] = {
        "path": str(file_path),
        "filename": filename,
        "mime_type": mime_type,
        "disposition": "inline" if disposition == "inline" else "attachment",
        "expires_at_ts": expires_at_ts,
    }
    base_url = _ensure_loopback_download_server()
    if disposition == "inline":
        access_url = f"{base_url}/preview/{token}"
    else:
        access_url = f"{base_url}/download/{token}/{urllib.parse.quote(filename, safe='')}"
    return {
        "ok": True,
        "delivery": "url",
        "filename": filename,
        "mime_type": mime_type,
        "size": len(content),
        "download_url": access_url,
        "expires_at": datetime.fromtimestamp(expires_at_ts, tz=timezone.utc).isoformat(),
    }


def _write_loopback_email_body(html: str, payload: dict[str, Any]) -> str:
    """把超大邮件 HTML 与 CID 图片写入本地临时文件，避免经过 JSON-RPC stdout。

    正文和每个 CID 图片都使用同一个随机 token 保护；HTML 内的 cid: 引用被替换为
    loopback 图片 URL。这样保留 newsletter 的内嵌图片，又不会将图片转成 data URI
    放大 JSON 响应。临时文件会在 token 过期后统一删除。
    """
    _cleanup_expired_download_tokens()
    token = uuid.uuid4().hex
    base_url = _ensure_loopback_download_server()
    download_dir = _attachment_download_dir()
    cid_images: dict[str, dict[str, str]] = {}
    cid_paths: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        headers = part.get("headers") if isinstance(part.get("headers"), list) else []
        cid = ""
        for header in headers:
            if isinstance(header, dict) and str(header.get("name") or "").lower() == "content-id":
                cid = str(header.get("value") or "").strip().strip("<>")
                break
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        encoded = body.get("data")
        mime_type = str(part.get("mimeType") or "").lower()
        if cid and mime_type.startswith("image/") and encoded:
            try:
                raw = base64.urlsafe_b64decode(str(encoded) + "=" * (-len(str(encoded)) % 4))
                image_path = download_dir / f"{token}-cid-{len(cid_images)}"
                image_path.write_bytes(raw)
                cid_images[cid] = {"path": str(image_path), "mime_type": mime_type}
                cid_paths.append(str(image_path))
            except Exception:
                # 图片不可解码时保留原 cid 引用；页面正文仍可安全显示。
                pass
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(payload)

    def replace_cid(match: re.Match[str]) -> str:
        cid = match.group(1)
        cid_key = cid.split("@", 1)[0] if "@" in cid else cid
        resolved = cid_key if cid_key in cid_images else cid
        if resolved not in cid_images:
            return match.group(0)
        return f'src="{base_url}/message/{token}/cid/{urllib.parse.quote(resolved, safe="")}"'

    rendered_html = _CID_IMAGE_RE.sub(replace_cid, html)
    body_path = download_dir / f"{token}-message.html"
    body_path.write_text(rendered_html, encoding="utf-8")
    _DOWNLOAD_TOKENS[token] = {
        "path": str(body_path),
        "cid_images": cid_images,
        "cid_paths": cid_paths,
        "expires_at_ts": time.time() + _DOWNLOAD_TOKEN_TTL_SECONDS,
    }
    return f"{base_url}/message/{token}"


def _normalized_attachment_mime_type(attachment: dict[str, Any]) -> str:
    import mimetypes

    raw = str(attachment.get("mime_type") or "").strip().lower()
    if raw and raw != "application/octet-stream":
        return raw[:120]
    filename = str(attachment.get("filename") or "").strip()
    guessed, _ = mimetypes.guess_type(filename)
    if guessed:
        return guessed[:120]
    return "application/octet-stream"


def _attachment_preview_kind(attachment: dict[str, Any]) -> str:
    import mimetypes

    mime_type = _normalized_attachment_mime_type(attachment)
    filename = str(attachment.get("filename") or "").strip().lower()
    guessed, _ = mimetypes.guess_type(filename)
    # Gmail 明确返回的 MIME 类型优先；只有通用二进制类型时，才用文件名推断，
    # 避免异常文件名覆盖服务端已经确认的真实类型。
    effective_mime = str(
        guessed if mime_type == "application/octet-stream" and guessed else mime_type
    ).lower()
    if effective_mime == "application/pdf":
        return "pdf"
    if effective_mime.startswith("image/"):
        return "image"
    if (
        effective_mime.startswith("text/")
        or effective_mime in {"application/json", "text/csv"}
    ):
        return "text"
    if effective_mime.startswith("audio/"):
        return "audio"
    if effective_mime.startswith("video/"):
        return "video"
    return "download"


def _finalize_attachment_access_payload(
    base_payload: dict[str, Any],
    *,
    mode: str,
    message_id: str,
    attachment_id: str,
    attachment: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(base_payload)
    payload["ok"] = bool(payload.get("ok", True))
    payload["delivery"] = str(payload.get("delivery") or ("inline" if payload.get("content_b64") else "url"))
    payload["mode"] = mode
    payload["message_id"] = message_id
    payload["attachment_id"] = attachment_id
    payload["filename"] = str(payload.get("filename") or attachment.get("filename") or "attachment")
    payload["mime_type"] = _normalized_attachment_mime_type(attachment)
    payload["size"] = int(payload.get("size") or attachment.get("size") or 0)
    if payload["delivery"] == "url":
        if mode == "preview":
            preview_url = str(payload.get("preview_url") or payload.get("download_url") or "")
            payload["preview_url"] = preview_url
            payload.setdefault("download_url", str(payload.get("download_url") or ""))
        else:
            download_url = str(payload.get("download_url") or payload.get("preview_url") or "")
            payload["download_url"] = download_url
    return payload


def _clamp_int(value: Any, fallback: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return min(max_value, max(min_value, parsed))


INBOX_THREAD_PAGE_SIZE = 5
INBOX_THREAD_PAGE_MAX = 50
INBOX_FULL_BODY_LIMIT = 200000
INBOX_PAGE_BODY_LIMIT = INBOX_FULL_BODY_LIMIT
# 必须与 common.py 的最终 stdout 防线保持一致。宿主约 64 KiB 即会硬结束
# 子进程，因此这里使用 48 KiB 的业务预算，提前切换到正文 loopback URL。
INBOX_THREAD_RESPONSE_MAX_BYTES = 48 * 1024
INBOX_PAGE_BODY_LIMIT_STEPS = (200000, 120000, 80000, 48000, 24000, 12000, 6000, 3000, 1500, 750, 320)
# 线程问答只保留最近消息的去重正文。旧值会将约 9600 字符正文连同提示词
# 一次送入 Sampling，容易挤占模型完成 JSON / Markdown 回答所需的上下文。
INBOX_PROMPT_MESSAGE_LIMIT = 4
INBOX_PROMPT_BODY_LIMIT = 600
THREAD_ASSIST_CACHE_VERSION = 2
# 同一线程概览在缓存落盘前只允许一个后台 run，避免详情页重渲染或重试重复 Sampling。
INBOX_THREAD_ASSIST_INFLIGHT: dict[str, str] = {}


def _uses_chinese_text(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", str(value or "")))


def _sidebar_language_instruction(visible_prompt: str) -> str:
    """Keep the AI conversation in the language used for the request.

    Email drafts intentionally remain outside this rule: a reply or new email should
    follow the language appropriate to its recipients and source conversation.
    """
    if _uses_chinese_text(visible_prompt):
        return (
            "The visible prompt is in Chinese. You MUST write every user-facing conversation "
            "field in Simplified Chinese: assistant_text, assistant_followup_text, "
            "and every reply_gaps/compose_gaps summary, question, and hint. Do not "
            "use English for those fields except untranslatable names, addresses, "
            "identifiers, or quoted source text. Keep draft_reply and compose_draft "
            "in the language appropriate to the recipients and source email."
        )
    return (
        "You MUST write every user-facing conversation field in the same language as the "
        "visible prompt: assistant_text, assistant_followup_text, and every "
        "reply_gaps/compose_gaps summary, question, and hint. Keep draft_reply and "
        "compose_draft in the language appropriate to the recipients and source email."
    )


def _sidebar_fallback_text(visible_prompt: str, kind: str) -> str:
    """Return deterministic, request-language fallback copy for the AI sidebar."""
    if _uses_chinese_text(visible_prompt):
        return {
            "summary": "我已查看该邮件线程，但暂时无法生成详细摘要。",
            "thread_draft": "我已为该邮件线程生成回复草稿。",
            "compose_analysis": "我已查看你的草稿，并可在改写前提出改进建议。",
            "compose_draft": "在生成邮件草稿前，我还需要一些补充信息。",
        }[kind]
    return {
        "summary": "I reviewed the thread, but could not generate a detailed summary.",
        "thread_draft": "I drafted a reply for this thread.",
        "compose_analysis": "I reviewed your draft and can suggest improvements before rewriting it.",
        "compose_draft": "I need a little more context before I can propose an email draft.",
    }[kind]


THREAD_ASSIST_SYSTEM = """Return JSON only:
{"overview":"one complete factual sentence","quick_replies":[{"id":"short_id","label":"2-5 words","intent":"short grounded instruction"}]}

Use the supplied thread only. overview: one complete sentence, <=24 words.
Return exactly 2 quick_replies. Keep each intent <=12 words.
Never invent facts, commitments, or HTML. Never leave a sentence unfinished."""

MAIL_PROMPT_SYSTEM = """You are Anna, an executive email assistant working with a Gmail thread.

Return JSON only:
{
  "assistant_text": "short assistant response for the sidebar",
  "assistant_followup_text": "optional short summary of the draft strategy and possible next adjustment",
  "draft_reply": {"body": "plain text reply body"} | null,
  "reply_gaps": {
    "needs_user_input": true | false,
    "summary": "one short sentence",
    "questions": [
      {"id": "q1", "question": "specific question", "hint": "short hint", "required": true}
    ]
  }
}

Rules:
- Follow the Response language instruction in the user message exactly.
- draft_reply and reply_gaps.needs_user_input=true are mutually exclusive.
- If the user prompt requires information that only the user would know, do not guess.
- In that case, omit draft_reply and return 1-3 specific reply_gaps questions.
- If enough information is available, return a concise plain-text draft_reply.body.
- assistant_text should briefly explain your understanding of the thread and the user's intent.
- assistant_followup_text should briefly summarize the draft strategy and invite a useful adjustment. Omit it when no reliable summary is possible.
- In assistant_text and assistant_followup_text, use only Markdown headings, bold text, ordered or unordered lists, and HTTP/HTTPS links in the form [label](https://example.com). Do not use HTML, tables, images, code blocks, or block quotes. If the user requests an unsupported format, say so and offer an equivalent using the supported formats.
- When referring to this Gmail thread, replace <Thread ID> with the Thread ID from the prompt and use the exact token [THREAD_REF_<Thread ID>] so the sidebar can open it.
- Do not repeat the draft body in either assistant text field.
- Do not include email headers in the draft body.
- Keep the sign-off and sender name on consecutive lines with no blank line between them.
- Do not invent dates, commitments, prices, or factual claims.
- Never include HTML.
"""

COMPOSE_MAIL_PROMPT_SYSTEM = """You are Anna, an executive email assistant helping the user compose a new email.

Return JSON only:
{
  "assistant_text": "clear response for the AI sidebar",
  "assistant_followup_text": "optional concise next step",
  "suggested_subject": "optional suggested subject line",
  "compose_draft": {"body": "plain text email body", "subject": "optional subject line"} | null,
  "compose_gaps": {
    "needs_user_input": true | false,
    "summary": "one short sentence",
    "questions": [
      {"id": "q1", "question": "specific question", "hint": "short hint", "required": true}
    ]
  }
}

Rules:
- Follow the Response language instruction in the user message exactly.
- This is a new Compose email, not a Gmail thread. Treat the supplied Compose snapshot as the only email context.
- Never send, save, delete, or modify the Compose email. You may only give advice or return a proposed body for the user to review.
- Recipient addresses are audience context only. Never reproduce addresses or email headers in assistant text or a proposed body, and never suggest changing recipients.
- When the requested response mode is analysis, provide concrete, prioritized improvement suggestions and any needed clarifying questions. Set compose_draft to null even if a rewrite would be possible.
- When the requested response mode is draft or revision, return a proposed compose_draft only if the prompt and snapshot provide enough information. Otherwise set compose_draft to null and ask one to three specific questions.
- A revision must be a complete replacement body, not a patch or a list of edits.
- Do not invent dates, commitments, prices, names, or factual claims. Do not silently turn unknown details into facts.
- Do not repeat the proposed email body in assistant_text or assistant_followup_text.
- In assistant_text and assistant_followup_text, use only Markdown headings, bold text, ordered or unordered lists, and HTTP/HTTPS links in the form [label](https://example.com). Do not use HTML, tables, images, code blocks, or block quotes.
- The proposed email body must be plain text without HTML or Markdown fences. Do not include To, Cc, Bcc, or Subject headers in it.
- Keep a sign-off and sender name on consecutive lines with no blank line between them.
"""

MAIL_SUMMARY_SYSTEM = """Summarize the provided Gmail thread using only its evidence.
Return one JSON object with exactly one field: {"markdown": string}.
markdown must be a complete concise Markdown summary with factual full sentences.
Do not draft, send, or suggest state changes. Follow the requested response language."""


_DRAFT_SIGNOFF_BLANK_LINE_RE = re.compile(
    r"(?im)^(?P<signoff>[ \t]*(?:all the best|best(?: regards)?|cheers|kind regards|"
    r"many thanks|regards|respectfully|sincerely|thanks|thank you|warm regards)"
    r"[,.!，。！]?[ \t]*)\n(?:[ \t]*\n)+(?=[^\n]+\Z)"
)


def _normalize_generated_draft_body(value: Any) -> str:
    """Remove an accidental blank line between a closing and final signature line."""
    body = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return _DRAFT_SIGNOFF_BLANK_LINE_RE.sub(r"\g<signoff>\n", body)


def _inbox_sort_key(message: dict[str, Any]) -> tuple[int, str]:
    try:
        internal_date = int(message.get("internal_date") or 0)
    except Exception:
        internal_date = 0
    return (internal_date, str(message.get("id") or ""))


def _normalize_label_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(label).strip().upper() for label in value if str(label).strip()]


def _is_gmail_draft_message(message: dict[str, Any]) -> bool:
    return "DRAFT" in _normalize_label_ids(message.get("label_ids"))


def _visible_thread_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [message for message in messages if not _is_gmail_draft_message(message)]


def _thread_original_subject(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        subject = str(message.get("subject") or "").strip()
        if subject:
            return subject
    return "(no subject)"


def _normalize_quick_replies(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    replies: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value[:4], start=1):
        if not isinstance(item, dict):
            continue
        label = re.sub(r"\s+", " ", str(item.get("label") or "")).strip()
        intent = re.sub(r"\s+", " ", str(item.get("intent") or "")).strip()
        if not label or not intent:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        raw_id = str(item.get("id") or label).strip().lower()
        reply_id = re.sub(r"[^a-z0-9]+", "_", raw_id).strip("_") or f"quick_reply_{index}"
        replies.append({
            "id": reply_id[:64],
            "label": label[:48],
            "intent": intent[:240],
        })
        if len(replies) >= 3:
            break
    return replies


def _compact_body_text(text: str, *, limit: int) -> tuple[str, bool]:
    body = str(text or "").strip()
    if len(body) <= limit:
        return body, False
    return body[:limit].rstrip(), True


def _compact_thread_metadata(value: Any, *, limit: int) -> str:
    """限制展示元数据长度，防止异常邮件头绕过正文响应预算。"""
    text = str(value or "")
    return text if len(text) <= limit else text[:limit].rstrip()


def _display_body_source(message: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """读取一次邮件展示源，避免在不同响应预算下反复解码超大 MIME 正文。"""
    from mail_agent.mail_providers.gmail.adapter import decode_body_for_display

    display = decode_body_for_display(message)
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    return str(display.get("html") or ""), str(display.get("text") or ""), payload


def _display_body_payload(
    message: dict[str, Any],
    *,
    limit: int,
    prefer_html: bool = True,
    source: tuple[str, str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造线程页的小型正文预览，绝不把 CID 二进制内联进 JSON-RPC。"""
    from mail_agent.actions.service import _strip_quoted_reply

    raw_html, raw_text, payload = source or _display_body_source(message)

    if prefer_html and raw_html.strip():
        sanitized_html = _resolve_cid_images(
            _sanitize_email_html(_strip_quoted_reply_html(raw_html)),
            payload,
        )
        # CID 图片转 data URI 会让几 KB HTML 膨胀为数 MB。线程页只给不含
        # CID 的完整 HTML；含 CID 的邮件交由单封 URL 正文链路加载。
        if not _CID_IMAGE_RE.search(sanitized_html) and len(sanitized_html) <= limit:
            return {
                "body_html": sanitized_html,
                "body_truncated": False,
            }

    if raw_text.strip():
        compact_text, text_truncated = _compact_body_text(_strip_quoted_reply(raw_text), limit=limit)
        return {
            "body_text": compact_text,
            "body_truncated": text_truncated or bool(raw_html.strip()),
        }

    fallback_text = _strip_quoted_reply(_dedup_body(str(message.get("body_text") or "")))
    compact_text, text_truncated = _compact_body_text(fallback_text, limit=limit)
    return {
        "body_text": compact_text,
        "body_truncated": text_truncated or bool(raw_html.strip()),
    }


def _serialize_inbox_thread_message(message: dict[str, Any], *, include_display_body: bool, body_limit: int) -> dict[str, Any]:
    from mail_agent.mail_providers.gmail.adapter import attachment_metadata_from_message

    payload = {
        "id": _compact_thread_metadata(message.get("id"), limit=256),
        "thread_id": _compact_thread_metadata(message.get("thread_id"), limit=256),
        "internal_date": _compact_thread_metadata(message.get("internal_date"), limit=64),
        "from": _compact_thread_metadata(message.get("from"), limit=4096),
        "to": _compact_thread_metadata(message.get("to"), limit=4096),
        "cc": _compact_thread_metadata(message.get("cc"), limit=4096),
        "bcc": _compact_thread_metadata(message.get("bcc"), limit=4096),
        "subject": _compact_thread_metadata(message.get("subject"), limit=4096),
        "label_ids": [_compact_thread_metadata(label, limit=256) for label in _normalize_label_ids(message.get("label_ids"))[:50]],
        "attachments": attachment_metadata_from_message(message),
    }
    if include_display_body:
        payload.update(_display_body_payload(message, limit=body_limit))
    return payload


def _inbox_thread_rpc_frame_size(data: dict[str, Any]) -> int:
    """Measure the complete JSON-RPC frame with a conservative request-id reserve."""
    frame = {
        "jsonrpc": JSONRPC_VERSION,
        "id": "x" * 128,
        "result": {"success": True, "tool": "get_inbox_thread_page", "data": data},
    }
    return len(json.dumps(frame, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))


def _inbox_message_display_rpc_frame_size(data: dict[str, Any]) -> int:
    frame = {
        "jsonrpc": JSONRPC_VERSION,
        "id": "x" * 128,
        "result": {"success": True, "tool": "get_inbox_message_display_body", "data": data},
    }
    return len(json.dumps(frame, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))


def _build_inbox_message_display_response(mailbox: str, message: dict[str, Any]) -> dict[str, Any]:
    base = {
        "mailbox": mailbox,
        "message_id": str(message.get("id") or ""),
        "thread_id": str(message.get("thread_id") or ""),
    }
    raw_html, raw_text, payload = _display_body_source(message)
    if raw_html.strip():
        sanitized_html = _resolve_cid_images(
            _sanitize_email_html(_strip_quoted_reply_html(raw_html)),
            payload,
        )
        # 只有不含 CID 的小 HTML 才直走 JSON-RPC。其余 HTML 一律通过本地
        # loopback 读取，确保在序列化响应之前就停止正文的协议膨胀。
        inline_data = {**base, "body_html": sanitized_html, "body_truncated": False}
        if not _CID_IMAGE_RE.search(sanitized_html) and _inbox_message_display_rpc_frame_size(inline_data) <= INBOX_THREAD_RESPONSE_MAX_BYTES:
            return inline_data
        return {
            **base,
            "body_url": _write_loopback_email_body(sanitized_html, payload),
            "body_truncated": False,
        }

    from mail_agent.actions.service import _strip_quoted_reply

    # 纯文本邮件也必须遵守“完整正文或 body_url”的约定：不能为了通过
    # JSON-RPC 上限而回退给前端一段被截断的 URL 列表式预览。
    plain_text = _strip_quoted_reply(raw_text) if raw_text.strip() else _strip_quoted_reply(
        _dedup_body(str(message.get("body_text") or "")),
    )
    inline_data = {**base, "body_text": plain_text, "body_truncated": False}
    if _inbox_message_display_rpc_frame_size(inline_data) <= INBOX_THREAD_RESPONSE_MAX_BYTES:
        return inline_data
    plain_html = (
        "<!doctype html><html><body><pre style=\"white-space:pre-wrap;word-break:break-word\">"
        f"{html_escape(plain_text)}"
        "</pre></body></html>"
    )
    return {
        **base,
        "body_url": _write_loopback_email_body(plain_html, payload),
        "body_truncated": False,
    }


def _inbox_thread_page_payload(
    *,
    mailbox: str,
    thread_id: str,
    subject: str,
    latest_subject: str,
    messages: list[dict[str, Any]],
    start_index: int,
    latest_message_id: str,
    include_display_body: bool,
    body_limit: int,
) -> dict[str, Any]:
    return {
        "mailbox": mailbox,
        "thread_id": thread_id,
        "subject": subject,
        "latest_subject": latest_subject,
        "messages": [
            _serialize_inbox_thread_message(
                message,
                include_display_body=include_display_body,
                body_limit=body_limit,
            )
            for message in messages
        ],
        "returned_count": len(messages),
        "has_earlier": start_index > 0,
        "next_before_index": start_index if start_index > 0 else None,
        "latest_message_id": latest_message_id,
    }


def _read_cached_thread_messages(mailbox: str, thread_id: str) -> list[dict[str, Any]]:
    from mail_agent.mail_providers.gmail.adapter import list_messages, normalize_mailbox, read_message

    normalized_mailbox = normalize_mailbox(mailbox)
    summaries = [
        item for item in list_messages(normalized_mailbox)
        if str(item.get("thread_id") or "") == str(thread_id or "")
    ]
    messages: list[dict[str, Any]] = []
    for summary in summaries:
        message_id = str(summary.get("id") or "")
        if not message_id:
            continue
        try:
            cached = read_message(normalized_mailbox, message_id)
        except Exception:
            cached = None
        if isinstance(cached, dict):
            messages.append(cached)
    messages.sort(key=_inbox_sort_key)
    return messages


def _load_thread_messages(mailbox: str, thread_id: str, *, force_refresh: bool = False) -> list[dict[str, Any]]:
    from mail_agent.mail_providers.gmail.adapter import (
        list_messages,
        normalize_mailbox,
        refresh_thread_cache,
        sync_cached_message_summaries,
    )

    cached_messages = _read_cached_thread_messages(mailbox, thread_id)
    normalized_mailbox = normalize_mailbox(mailbox)
    expected_count = sum(
        1
        for summary in list_messages(normalized_mailbox)
        if str(summary.get("thread_id") or "") == str(thread_id or "")
    )
    # 仅当该线程所有 index 消息都有完整缓存时才跳过 Gmail；长线程在只缓存
    # 部分正文时必须刷新，否则“加载更早邮件”会被错误地截断。
    summaries_by_id = {
        str(item.get("id") or ""): item
        for item in list_messages(normalized_mailbox)
        if isinstance(item, dict) and item.get("id")
    }
    cached_by_id = {str(item.get("id") or ""): item for item in cached_messages}

    def _attachment_count(item: dict[str, Any] | None) -> int:
        if not isinstance(item, dict):
            return 0
        attachments = item.get("attachments") if isinstance(item.get("attachments"), list) else []
        return max(len(attachments), int(item.get("attachment_count") or 0), 1 if item.get("has_attachment") else 0)

    attachment_metadata_complete = all(
        _attachment_count(cached_by_id.get(message_id)) >= _attachment_count(summary)
        for message_id, summary in summaries_by_id.items()
    )
    if cached_messages and len(cached_messages) == expected_count and attachment_metadata_complete and not force_refresh:
        try:
            # 完整详情可能在更早一次按需读取中补齐附件；回填目录摘要后，
            # 当前线程关闭或下次加载列表时即可正确显示附件图标。
            sync_cached_message_summaries(normalized_mailbox, cached_messages)
        except Exception as exc:
            # 摘要缓存更新失败不能影响用户打开邮件详情。
            log(f"thread summary cache sync failed for {thread_id}: {type(exc).__name__}: {exc}")
        return cached_messages
    try:
        messages = refresh_thread_cache(mailbox, thread_id)
        if messages:
            messages.sort(key=_inbox_sort_key)
            return messages
    except Exception as exc:
        log(f"refresh_thread_cache failed for {thread_id}: {type(exc).__name__}: {exc}")
    return cached_messages


def _build_inbox_thread_page(
    mailbox: str,
    thread_id: str,
    *,
    anchor_message_id: str = "",
    before_index: int | None = None,
    limit: int = INBOX_THREAD_PAGE_SIZE,
    include_display_body: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    messages = _visible_thread_messages(_load_thread_messages(mailbox, thread_id, force_refresh=force_refresh))
    if not messages:
        return {
            "mailbox": mailbox,
            "thread_id": thread_id,
            "subject": "",
            "messages": [],
            "returned_count": 0,
            "has_earlier": False,
            "next_before_index": None,
            "latest_message_id": "",
        }

    total = len(messages)
    page_limit = _clamp_int(limit, INBOX_THREAD_PAGE_SIZE, 1, INBOX_THREAD_PAGE_MAX)
    end_index = total if before_index is None else _clamp_int(before_index, total, 0, total)
    if before_index is None and anchor_message_id:
        anchor = str(anchor_message_id or "")
        for index, message in enumerate(messages):
            if str(message.get("id") or "") == anchor:
                end_index = index + 1
                break
    start_index = max(0, end_index - page_limit)
    latest = messages[-1]
    subject = _thread_original_subject(messages)
    latest_subject = str(latest.get("subject") or "") or subject
    latest_message_id = str(latest.get("id") or "")
    if end_index <= 0:
        return {
            "mailbox": mailbox,
            "thread_id": thread_id,
            "subject": subject,
            "latest_subject": latest_subject,
            "messages": [],
            "returned_count": 0,
            "has_earlier": False,
            "next_before_index": None,
            "latest_message_id": latest_message_id,
        }

    # First reduce every body progressively. Only after the smallest body
    # budget still exceeds the frame cap do we move the oldest message to the
    # previous page. This preserves the newest context and pagination cursor.
    for fitted_start in range(start_index, end_index):
        fitted_messages = messages[fitted_start:end_index]
        body_limits = INBOX_PAGE_BODY_LIMIT_STEPS if include_display_body else (0,)
        for body_limit in body_limits:
            data = _inbox_thread_page_payload(
                mailbox=mailbox,
                thread_id=thread_id,
                subject=subject,
                latest_subject=latest_subject,
                messages=fitted_messages,
                start_index=fitted_start,
                latest_message_id=latest_message_id,
                include_display_body=include_display_body,
                body_limit=body_limit,
            )
            if _inbox_thread_rpc_frame_size(data) <= INBOX_THREAD_RESPONSE_MAX_BYTES:
                return data

    # A single message with pathological metadata can still exceed the cap.
    # Return a bounded, actionable page instead of falling through to stdio
    # file transport or allowing the host to terminate the tool process.
    return {
        "mailbox": mailbox,
        "thread_id": thread_id,
        "subject": subject,
        "latest_subject": latest_subject,
        "messages": [],
        "returned_count": 0,
        "has_earlier": True,
        "next_before_index": end_index,
        "latest_message_id": latest_message_id,
        "error": "Thread metadata exceeds the 48 KiB response limit.",
    }


def _find_thread_message(messages: list[dict[str, Any]], message_id: str) -> dict[str, Any] | None:
    target = str(message_id or "")
    for message in messages:
        if str(message.get("id") or "") == target:
            return message
    return None


def _thread_prompt_excerpt(messages: list[dict[str, Any]], *, max_messages: int = INBOX_PROMPT_MESSAGE_LIMIT) -> str:
    excerpt = messages[-max_messages:] if len(messages) > max_messages else messages
    parts: list[str] = []
    for index, message in enumerate(excerpt, start=1):
        display = _display_body_payload(message, limit=INBOX_PROMPT_BODY_LIMIT, prefer_html=False)
        body = str(display.get("body_text") or "").strip()
        if not body and str(display.get("body_html") or "").strip():
            body = _dedup_body(str(display.get("body_html") or ""))
        parts.append(
            "\n".join(
                [
                    f"Message {index}",
                    f"From: {message.get('from', '')}",
                    f"To: {message.get('to', '')}",
                    f"Date: {message.get('internal_date', '')}",
                    f"Subject: {message.get('subject', '')}",
                    f"Body: {body[:INBOX_PROMPT_BODY_LIMIT]}",
                ]
            )
        )
    return "\n\n---\n\n".join(parts)


def _one_line_overview(value: Any, *, max_chars: int = 220) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars].rsplit(" ", 1)[0].strip()
    return clipped or text[:max_chars].strip()


async def _load_contact_context_for_thread(
    *,
    mailbox: str,
    thread_id: str,
    subject: str,
    latest_from: str,
    latest_body: str,
    purpose: str,
    sampling_create_message: Any,
) -> tuple[str, list[str]]:
    from mail_agent.contact_memory.retriever import (
        contact_email_from_header,
        format_contact_context_for_prompt,
        retrieve_contact_context,
    )
    from mail_agent.contact_memory.types import ContactMemoryQuery

    contact_email = contact_email_from_header(latest_from)
    if not contact_email:
        return "No relevant contact memory.", []

    try:
        contact_context = await retrieve_contact_context(
            ContactMemoryQuery(
                mailbox=mailbox,
                contact_email=contact_email,
                current_subject=subject,
                current_body=latest_body,
                current_thread_id=thread_id,
                purpose=purpose,
            ),
            sampling_create_message=sampling_create_message,
        )
    except Exception:
        return "No relevant contact memory.", []

    related_lines: list[str] = []
    for topic in getattr(contact_context, "relevant_topics", []) or []:
        text = " | ".join(
            part for part in [
                getattr(topic, "title", ""),
                getattr(topic, "summary", ""),
                getattr(topic, "open_loop", ""),
            ] if part
        ).strip()
        if text and text not in related_lines:
            related_lines.append(text[:240])
    return format_contact_context_for_prompt(contact_context), related_lines[:3]


def _thread_participants(messages: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for message in messages:
        for key in ("from", "to", "cc", "bcc"):
            value = str(message.get(key) or "").strip()
            if value and value not in seen:
                seen.append(value)
    return seen


def _build_pseudo_card_for_thread(mailbox: str, thread_id: str, messages: list[dict[str, Any]], anchor_message_id: str = "") -> Any:
    from mail_agent.storage.types import CardDetails, OriginalEmail, PersistentCard

    if not messages:
        raise ValueError("Thread messages are unavailable")
    latest = messages[-1]
    anchor_message = _find_thread_message(messages, anchor_message_id) if anchor_message_id else None
    target = anchor_message or latest
    latest_body = _display_body_payload(target, limit=INBOX_PROMPT_BODY_LIMIT, prefer_html=False).get("body_text") or str(target.get("snippet") or "")
    participants = _thread_participants(messages)
    title = _thread_original_subject(messages)
    summary = str(target.get("snippet") or latest_body or "")[:220]
    recommendation = f"Reply with context from {len(participants)} participant(s)." if participants else "Reply with context from the thread."
    return PersistentCard(
        card_id=f"inbox-thread-{thread_id}",
        message_id=str(target.get("id") or ""),
        thread_id=thread_id,
        title=title,
        summary=summary,
        recommendation=recommendation,
        label="inbox_thread",
        priority="medium",
        details=CardDetails(
            needs="Reply or review this thread.",
            latest_activity=str(latest.get("internal_date") or ""),
            reviewed=f"{len(messages)} message(s) in thread",
            mailbox=mailbox,
        ),
        original=OriginalEmail(
            thread=title,
            from_addr=str(target.get("from") or ""),
            to_addr=str(target.get("to") or ""),
            time=str(target.get("internal_date") or ""),
            body=str(latest_body or ""),
        ),
        attachments=list(target.get("attachments") or []),
        user_action="reply",
    )


async def _generate_thread_assist_result(
    mailbox: str,
    thread_id: str,
    latest_message_id: str,
    anchor_message_id: str,
    sampling_create_message: Any,
) -> dict[str, Any]:
    from mail_agent.llm_runtime.service import call_llm_json_safe

    messages = _visible_thread_messages(_load_thread_messages(mailbox, thread_id))
    if not messages:
        raise ValueError(f"Thread {thread_id} not found")
    overview_result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=THREAD_ASSIST_SYSTEM,
        user_message=(
            f"Subject: {_thread_original_subject(messages)}\n"
            f"Latest message subject: {messages[-1].get('subject', '')}\n"
            f"Participants: {'; '.join(_thread_participants(messages))}\n"
            f"Thread messages:\n{_thread_prompt_excerpt(messages)}\n"
        ),
        # 概览必须是完整的模型总结；不可用时让后台 run 失败并由详情页显示
        # Retry，不能把主题、snippet 或正文片段伪装成 AI 概览。
        fallback={},
        temperature=0.2,
        # 模型可能先生成 reasoning；320 token 不足以稳定完成 overview + 两个动作的 JSON。
        max_tokens=768,
        timeout=45.0,
        metadata={"tool": "inbox_thread_assist", "thread_id": thread_id},
        allow_fallback=False,
        # 残缺 JSON 不能用本地修补恢复事实，直接用更高输出额度重试一次。
        max_attempts=2,
        retry_max_tokens=1024,
        response_format={"type": "json_object"},
        on_unsupported="text",
    )
    payload = overview_result.get("payload") if isinstance(overview_result.get("payload"), dict) else {}
    overview = _one_line_overview(payload.get("overview"))
    if not overview:
        raise RuntimeError("analysis_unavailable")
    quick_replies = _normalize_quick_replies(payload.get("quick_replies"))

    return {
        "format_version": THREAD_ASSIST_CACHE_VERSION,
        "thread_id": thread_id,
        "latest_message_id": latest_message_id,
        "overview": overview,
        "quick_replies": quick_replies,
        "summary": {},
        "related_context": [],
        "fallback_used": False,
    }


async def _generate_mail_prompt_result(
    *,
    mailbox: str,
    thread_id: str,
    anchor_message_id: str,
    latest_message_id: str,
    visible_prompt: str,
    expected_artifact: str,
    user_answers: dict[str, str] | None,
    sampling_create_message: Any,
) -> dict[str, Any]:
    from mail_agent.llm_runtime.service import call_llm_json_safe

    expected_artifact = expected_artifact if expected_artifact in {"draft_reply", "send_plan"} else "summary"
    messages = _visible_thread_messages(_load_thread_messages(mailbox, thread_id))
    if not messages:
        raise ValueError(f"Thread {thread_id} not found")
    latest = messages[-1]
    thread_title = str(latest.get("subject") or "").strip() or "(no subject)"
    latest_display = _display_body_payload(latest, limit=INBOX_PROMPT_BODY_LIMIT, prefer_html=False)
    contact_context_text, _ = await _load_contact_context_for_thread(
        mailbox=mailbox,
        thread_id=thread_id,
        subject=str(latest.get("subject") or ""),
        latest_from=str(latest.get("from") or ""),
        latest_body=str(latest_display.get("body_text") or ""),
        purpose="draft_generation" if expected_artifact == "draft_reply" else "thread_summary",
        sampling_create_message=sampling_create_message,
    )
    if expected_artifact == "summary":
        result = await call_llm_json_safe(
            sampling_create_message,
            system_prompt=MAIL_SUMMARY_SYSTEM,
            user_message=(
                f"Visible prompt: {visible_prompt}\n"
                f"Response language: {_sidebar_language_instruction(visible_prompt)}\n"
                f"Mailbox: {mailbox}\n"
                f"Thread ID: {thread_id}\n"
                f"Anchor message ID: {anchor_message_id}\n"
                f"Latest message ID: {latest_message_id}\n"
                f"Contact context: {contact_context_text}\n"
                f"Thread messages:\n{_thread_prompt_excerpt(messages)}\n"
            ),
            # 展开后的线程总结与顶部概览使用同一语义：模型不可用时直接失败，
            # 前端保留可重试状态，不能展示“已查看线程”的伪总结。
            fallback={},
            temperature=0.2,
            max_tokens=1600,
            timeout=90.0,
            metadata={"tool": "inbox_mail_summary", "thread_id": thread_id},
            allow_fallback=False,
            max_attempts=2,
        )
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        assistant_text = str(payload.get("markdown") or "").strip()
        if not assistant_text:
            raise RuntimeError("analysis_unavailable")
        return {
            "mailbox": mailbox,
            "thread_id": thread_id,
            "anchor_message_id": anchor_message_id,
            "latest_message_id": latest_message_id,
            "visible_prompt": visible_prompt,
            "thread_title": thread_title,
            "assistant_text": assistant_text,
            "assistant_followup_text": "",
            "artifact": None,
            "reply_gaps": {"needs_user_input": False, "summary": "", "questions": []},
            "fallback_used": False,
        }

    answers_text = "\n".join(
        f"- {key}: {value}"
        for key, value in (user_answers or {}).items()
        if str(value).strip()
    ) or "None"
    fallback_assistant = _sidebar_fallback_text(visible_prompt, "thread_draft")
    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=MAIL_PROMPT_SYSTEM,
        user_message=(
            f"Visible prompt: {visible_prompt}\n"
            f"Response language: {_sidebar_language_instruction(visible_prompt)}\n"
            f"Expected artifact: {expected_artifact or 'none'}\n"
            f"Mailbox: {mailbox}\n"
            f"Thread ID: {thread_id}\n"
            f"Anchor message ID: {anchor_message_id}\n"
            f"Latest message ID: {latest_message_id}\n"
            f"Contact context: {contact_context_text}\n"
            f"User answers:\n{answers_text}\n\n"
            f"Thread messages:\n{_thread_prompt_excerpt(messages)}\n"
        ),
        fallback={
            "assistant_text": fallback_assistant,
            "assistant_followup_text": "",
            "draft_reply": {"body": ""},
            "reply_gaps": {"needs_user_input": False, "summary": "", "questions": []},
        },
        temperature=0.3,
        max_tokens=2400,
        timeout=150.0,
        metadata={"tool": "inbox_mail_prompt", "thread_id": thread_id},
    )
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    raw_gaps = payload.get("reply_gaps") if isinstance(payload.get("reply_gaps"), dict) else {}
    needs_user_input = bool(raw_gaps.get("needs_user_input"))
    questions_raw = raw_gaps.get("questions") if isinstance(raw_gaps.get("questions"), list) else []
    questions: list[dict[str, Any]] = []
    for index, item in enumerate(questions_raw[:3], start=1):
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        questions.append({
            "id": str(item.get("id") or f"q{index}").strip() or f"q{index}",
            "question": question[:240],
            "hint": str(item.get("hint") or "").strip()[:120],
            "required": bool(item.get("required", True)),
        })

    draft_payload = payload.get("draft_reply") if isinstance(payload.get("draft_reply"), dict) else {}
    draft_body = _normalize_generated_draft_body(draft_payload.get("body"))
    artifact = None
    if expected_artifact == "draft_reply" and draft_body and not needs_user_input:
        artifact = {
            "type": "draft_reply",
            "mailbox": mailbox,
            "thread_id": thread_id,
            "body": draft_body,
            "source_prompt": visible_prompt,
        }
    elif expected_artifact == "send_plan" and draft_body and not needs_user_input:
        import re as _re
        from_value = str(latest.get("from") or "")
        match = _re.search(r"<([^>\s]+@[^>\s]+)>", from_value)
        recipient = (match.group(1) if match else from_value).strip()
        artifact = {
            "type": "send_plan",
            "mailbox": mailbox,
            "thread_id": thread_id,
            "messages": [{
                "recipients": [recipient] if "@" in recipient else [],
                "subject": str(latest.get("subject") or "").strip() if str(latest.get("subject") or "").lower().startswith("re:") else f"Re: {str(latest.get('subject') or '').strip()}",
                "body": draft_body,
            }],
            "source_prompt": visible_prompt,
        }

    return {
        "mailbox": mailbox,
        "thread_id": thread_id,
        "anchor_message_id": anchor_message_id,
        "latest_message_id": latest_message_id,
        "visible_prompt": visible_prompt,
        "thread_title": thread_title,
        "assistant_text": str(payload.get("assistant_text") or fallback_assistant).strip(),
        "assistant_followup_text": str(payload.get("assistant_followup_text") or "").strip(),
        "artifact": artifact,
        "reply_gaps": {
            "needs_user_input": needs_user_input,
            "summary": str(raw_gaps.get("summary") or "").strip(),
            "questions": questions,
        },
        "fallback_used": bool(result.get("fallback_used")),
    }


_COMPOSE_GENERATION_PROMPT_RE = re.compile(
    r"(?:\b(?:write|create|compose|redraft|rewrite|revise|polish)\b|\bdraft\s+(?:an?\s+|the\s+|this\s+)?(?:email|draft|message)?|\bturn\b.+\binto\b|重写|改写|起草|写一封|生成(?:一封)?(?:邮件|草稿)|润色)",
    re.IGNORECASE | re.DOTALL,
)
_COMPOSE_ANALYSIS_PROMPT_RE = re.compile(
    r"(?:\bsuggest\s+changes?\b|\bimprove\b|\bfeedback\b|\breview\b|\bcritique\b|\banaly[sz]e\b|\bdiagnos(?:e|is)\b|\bwhat\b.+\bchange\b|建议|改进|优化|分析|诊断)",
    re.IGNORECASE | re.DOTALL,
)


def _compose_response_mode(*, body: str, visible_prompt: str, expected_artifact: str) -> str:
    """Choose a conservative Compose response mode from the visible user instruction.

    The expected artifact describes the UI surface, not authorization to overwrite a
    non-empty draft. An explicit writing instruction is required before returning a
    replacement draft for a Compose message that already has content.
    """
    prompt = str(visible_prompt or "").strip()
    explicit_generation = bool(_COMPOSE_GENERATION_PROMPT_RE.search(prompt))
    asks_for_analysis = bool(_COMPOSE_ANALYSIS_PROMPT_RE.search(prompt))
    if explicit_generation:
        return "revision" if body else "draft"
    if body and asks_for_analysis:
        return "analysis"
    if body:
        return "analysis"
    if expected_artifact == "compose_draft":
        return "draft"
    return "analysis"


def _normalize_compose_gaps(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    questions_raw = raw.get("questions") if isinstance(raw.get("questions"), list) else []
    questions: list[dict[str, Any]] = []
    for index, item in enumerate(questions_raw[:3], start=1):
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        questions.append({
            "id": str(item.get("id") or f"q{index}").strip()[:64] or f"q{index}",
            "question": question[:240],
            "hint": str(item.get("hint") or "").strip()[:120],
            "required": bool(item.get("required", True)),
        })
    return {
        "needs_user_input": bool(raw.get("needs_user_input")),
        "summary": str(raw.get("summary") or "").strip()[:500],
        "questions": questions,
    }


async def _generate_compose_mail_prompt_result(
    *,
    mailbox: str,
    draft: dict[str, Any],
    visible_prompt: str,
    expected_artifact: str,
    sampling_create_message: Any,
) -> dict[str, Any]:
    """Run a read-only Compose-aware sidebar prompt and return an optional artifact.

    This function deliberately has no storage or Gmail calls. The artifact is only a
    reviewable proposal; applying it remains a frontend action and sending remains a
    separate explicitly-confirmed Compose tool call.
    """
    from mail_agent.llm_runtime.service import call_llm_json_safe

    raw_recipients = draft.get("recipients") if isinstance(draft.get("recipients"), list) else []
    recipients = [str(item).strip() for item in raw_recipients if str(item).strip()][:100]
    subject = str(draft.get("subject") or "").strip()[:998]
    body = str(draft.get("body") or "").strip()[:12000]
    mode = _compose_response_mode(
        body=body,
        visible_prompt=visible_prompt,
        expected_artifact=expected_artifact,
    )
    fallback_text = _sidebar_fallback_text(
        visible_prompt,
        "compose_analysis" if mode == "analysis" else "compose_draft",
    )
    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=COMPOSE_MAIL_PROMPT_SYSTEM,
        user_message=(
            f"Visible prompt: {visible_prompt}\n"
            f"Response language: {_sidebar_language_instruction(visible_prompt)}\n"
            f"Requested response mode: {mode}\n"
            f"Mailbox: {mailbox}\n"
            f"Recipients (audience context only; never reproduce or modify): {', '.join(recipients) or '(not yet specified)'}\n"
            f"Current subject: {subject or '(not yet specified)'}\n"
            f"Current body:\n{body or '(empty)'}\n"
        ),
        fallback={
            "assistant_text": fallback_text,
            "assistant_followup_text": "",
            "suggested_subject": "",
            "compose_draft": None,
            "compose_gaps": {"needs_user_input": False, "summary": "", "questions": []},
        },
        temperature=0.3,
        max_tokens=2400,
        timeout=150.0,
        metadata={"tool": "compose_mail_prompt", "mode": mode, "recipient_count": len(recipients)},
    )
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    gaps = _normalize_compose_gaps(payload.get("compose_gaps"))
    raw_compose_draft = payload.get("compose_draft") if isinstance(payload.get("compose_draft"), dict) else {}
    proposed_body = _normalize_generated_draft_body(raw_compose_draft.get("body"))
    suggested_subject = str(
        raw_compose_draft.get("subject") or payload.get("suggested_subject") or ""
    ).strip()[:998]

    # A request for feedback must never return an accidental replace/insert control,
    # even if the model ignored the requested mode. Likewise, unanswered questions
    # keep a draft proposal out of the UI until the user supplies the missing context.
    artifact = None
    if mode in {"draft", "revision"} and proposed_body and not gaps["needs_user_input"]:
        artifact = {
            "type": "compose_draft",
            "mailbox": mailbox,
            "body": proposed_body,
            "source_prompt": visible_prompt,
            "mode": "insert" if mode == "draft" else "replace",
        }
        if suggested_subject:
            artifact["subject"] = suggested_subject

    return {
        "mailbox": mailbox,
        "visible_prompt": visible_prompt,
        "compose": {"recipients": recipients, "subject": subject, "body": body},
        "response_mode": mode,
        "assistant_text": str(payload.get("assistant_text") or fallback_text).strip()[:8000],
        "assistant_followup_text": str(payload.get("assistant_followup_text") or "").strip()[:4000],
        "artifact": artifact,
        "compose_gaps": gaps,
        "fallback_used": bool(result.get("fallback_used")),
    }


async def _handle_inbox_thread_assist_background(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    from mail_agent.storage.ops import get_inbox_thread_assist, set_inbox_thread_assist

    MAIL_AGENT_RUNS[run_id]["status"] = "running"
    _save_run_checkpoint(run_id)

    mailbox = str(arguments.get("mailbox", "")).strip()
    thread_id = str(arguments.get("thread_id", "")).strip()
    latest_message_id = str(arguments.get("latest_message_id", "")).strip()
    anchor_message_id = str(arguments.get("anchor_message_id", "")).strip()
    inflight_key = f"{mailbox.casefold()}:{thread_id}:{latest_message_id}"
    try:
        cached = await get_inbox_thread_assist(mailbox, thread_id, latest_message_id)
        cached_value = cached.get("value") if isinstance(cached.get("value"), dict) else {}
        cached_quick_replies = cached_value.get("quick_replies") if isinstance(cached_value, dict) else None
        if (
            cached.get("exists")
            and cached_value
            and cached_value.get("format_version") == THREAD_ASSIST_CACHE_VERSION
            and not cached_value.get("fallback_used")
            and str(cached_value.get("overview") or "").strip()
            and isinstance(cached_quick_replies, list)
            and len(cached_quick_replies) > 0
        ):
            MAIL_AGENT_RUNS[run_id].update(
                status="done",
                result={**cached_value, "cached": True},
                updated_at=beijing_now(),
            )
            _save_run_checkpoint(run_id)
            return
        sampling = _build_sampling_for_run(arguments, invoke_id)
        result = await _generate_thread_assist_result(mailbox, thread_id, latest_message_id, anchor_message_id, sampling)
        await set_inbox_thread_assist(mailbox, thread_id, latest_message_id, result)
        MAIL_AGENT_RUNS[run_id].update(status="done", result=result, updated_at=beijing_now())
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update(status="failed", error=str(exc), updated_at=beijing_now())
    finally:
        # 仅移除当前 run 自己登记的 key，避免旧 run 覆盖新的请求。
        if INBOX_THREAD_ASSIST_INFLIGHT.get(inflight_key) == run_id:
            INBOX_THREAD_ASSIST_INFLIGHT.pop(inflight_key, None)
        _save_run_checkpoint(run_id)


async def _handle_inbox_mail_prompt_background(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    MAIL_AGENT_RUNS[run_id]["status"] = "running"
    _save_run_checkpoint(run_id)

    mailbox = str(arguments.get("mailbox", "")).strip()
    thread_id = str(arguments.get("thread_id", "")).strip()
    anchor_message_id = str(arguments.get("anchor_message_id", "")).strip()
    latest_message_id = str(arguments.get("latest_message_id", "")).strip()
    visible_prompt = str(arguments.get("visible_prompt", "")).strip()
    expected_artifact = str(arguments.get("expected_artifact", "")).strip()
    user_answers = arguments.get("user_answers") if isinstance(arguments.get("user_answers"), dict) else None
    try:
        sampling = _build_sampling_for_run(arguments, invoke_id)
        result = await _generate_mail_prompt_result(
            mailbox=mailbox,
            thread_id=thread_id,
            anchor_message_id=anchor_message_id,
            latest_message_id=latest_message_id,
            visible_prompt=visible_prompt,
            expected_artifact=expected_artifact,
            user_answers=user_answers,
            sampling_create_message=sampling,
        )
        MAIL_AGENT_RUNS[run_id].update(status="done", result=result, updated_at=beijing_now())
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update(status="failed", error=str(exc), updated_at=beijing_now())
    _save_run_checkpoint(run_id)


async def _handle_compose_mail_prompt_background(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    """Populate a pollable run for a Compose sidebar request without persisting it."""
    MAIL_AGENT_RUNS[run_id]["status"] = "running"
    _save_run_checkpoint(run_id)

    mailbox = str(arguments.get("mailbox", "")).strip()
    draft = arguments.get("draft") if isinstance(arguments.get("draft"), dict) else {}
    visible_prompt = str(arguments.get("visible_prompt", "")).strip()
    expected_artifact = str(arguments.get("expected_artifact", "")).strip()
    try:
        sampling = _build_sampling_for_run(arguments, invoke_id)
        result = await _generate_compose_mail_prompt_result(
            mailbox=mailbox,
            draft=draft,
            visible_prompt=visible_prompt,
            expected_artifact=expected_artifact,
            sampling_create_message=sampling,
        )
        MAIL_AGENT_RUNS[run_id].update(status="done", result=result, updated_at=beijing_now())
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update(status="failed", error=str(exc), updated_at=beijing_now())
    _save_run_checkpoint(run_id)


def _card_context(card: Any) -> dict[str, str]:
    """Extract display fields from a PersistentCard for history entries."""
    try:
        original = getattr(card, "original", None)
        return {
            "card_summary": (getattr(card, "summary", "") or "")[:200],
            "card_from": (getattr(original, "from_addr", "") or "")[:120] if original else "",
            "card_subject": (getattr(original, "thread", "") or "")[:200] if original else "",
            "card_body": (getattr(original, "body", "") or "")[:300] if original else "",
        }
    except Exception:
        return {}


async def _handle_v2_tool(tool: str, arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """Handle V2 interaction tools (async, runs on the event loop)."""
    from mail_agent.storage.ops import (
        get_active_cards as storage_get_cards,
        get_scan_plan,
        set_scan_plan,
        get_inbox_settings,
        set_inbox_settings,
        get_inbox_workflow_state,
        set_inbox_workflow_state,
        get_ai_ask_history,
        set_ai_ask_history,
        update_card_status,
        add_snooze_sender,
        add_snooze_thread,
        append_learning,
        get_run_history,
    )
    from mail_agent.actions.service import (
        _fetch_thread_context_sync,
        summarize_thread,
        generate_draft_reply,
        reply_now,
    )
    from mail_agent.storage.types import PersistentCard

    mailbox = str(arguments.get("mailbox", "")).strip()
    card_id = str(arguments.get("card_id", "")).strip()

    if tool == "get_scan_plan":
        if not mailbox:
            return {
                "mailbox": "",
                "first_scan_days": 7,
                "incremental_days": 7,
                "max_messages": 100,
                "scan_categories": [],
                "updated_at": "",
            }
        plan = await get_scan_plan(mailbox)
        return {
            "mailbox": plan.mailbox,
            "scan_window_days": plan.scan_window_days,
            "max_messages": plan.max_messages,
            "scan_categories": plan.scan_categories,
            "updated_at": plan.updated_at,
        }

    if tool == "set_scan_plan":
        # Get target mailboxes: empty = all registered
        if mailbox:
            targets = [mailbox]
        else:
            from mail_agent.storage.ops import get_mailbox_registry
            registry = await get_mailbox_registry()
            targets = [e.email for e in registry.mailboxes if e.email]
            if not targets:
                return {"error": "no registered mailboxes"}
        for mb in targets:
            plan = await get_scan_plan(mb)
            val = arguments.get("scan_window_days")
            if val is not None:
                plan.scan_window_days = _clamp_int(val, plan.scan_window_days, 1, 90)
            val = arguments.get("max_messages")
            if val is not None:
                plan.max_messages = _clamp_int(val, plan.max_messages, 10, 500)
            val = arguments.get("scan_categories")
            if isinstance(val, list):
                plan.scan_categories = [str(c) for c in val if str(c) in ("promotions", "social", "updates", "forums")]
            await set_scan_plan(mb, plan)
        return {"ok": True, "mailbox": mailbox or "all", "targets": targets}

    if tool == "get_inbox_settings":
        if not mailbox:
            return {"error": "mailbox is required"}
        payload = await get_inbox_settings(mailbox)
        settings = payload["settings"].__dict__.copy()
        settings["custom_categories"] = [category.__dict__ for category in payload["settings"].custom_categories]
        return {"mailbox": mailbox, "settings": settings, "etag": payload["etag"]}

    if tool == "save_inbox_settings":
        if not mailbox:
            return {"error": "mailbox is required"}
        # 与 InboxSettings 字段对齐；含 LLM 状态轮询间隔
        fields = (
            "display_range_days",
            "time_section_mode",
            "stars_enabled",
            "stars_limit",
            "todos_enabled",
            "todos_limit",
            "llm_status_poll_seconds",
            "auto_sync_seconds",
            "initial_list_size",
            "custom_categories",
        )
        payload = await set_inbox_settings(mailbox, {name: arguments[name] for name in fields if name in arguments}, if_match=str(arguments.get("if_match") or "") or None)
        settings = payload["settings"].__dict__.copy()
        settings["custom_categories"] = [category.__dict__ for category in payload["settings"].custom_categories]
        return {"ok": True, "mailbox": mailbox, "settings": settings, "etag": payload["etag"]}

    if tool == "get_inbox_workflow_state":
        if not mailbox:
            return {"error": "mailbox is required"}
        payload = await get_inbox_workflow_state(mailbox)
        return {"mailbox": mailbox, **payload}

    if tool == "save_inbox_workflow_state":
        if not mailbox:
            return {"error": "mailbox is required"}
        state = arguments.get("state") if isinstance(arguments.get("state"), dict) else {}
        payload = await set_inbox_workflow_state(
            mailbox,
            state,
            if_match=str(arguments.get("if_match") or "") or None,
        )
        return {"ok": True, "mailbox": mailbox, **payload}

    if tool == "get_ai_ask_history":
        if not mailbox:
            return {"error": "mailbox is required"}
        payload = await get_ai_ask_history(mailbox)
        return {"mailbox": mailbox, **payload}

    if tool == "save_ai_ask_history":
        if not mailbox:
            return {"error": "mailbox is required"}
        entries = arguments.get("entries") if isinstance(arguments.get("entries"), list) else []
        payload = await set_ai_ask_history(
            mailbox,
            entries,
            if_match=str(arguments.get("if_match") or "") or None,
        )
        return {"ok": True, "mailbox": mailbox, **payload}

    if tool == "get_inbox_thread_page":
        if not mailbox:
            return {"error": "mailbox is required"}
        thread_id = str(arguments.get("thread_id", "")).strip()
        if not thread_id:
            return {"error": "thread_id is required"}
        before_raw = arguments.get("before_index")
        try:
            before_index = int(before_raw) if before_raw is not None else None
        except Exception:
            before_index = None
        include_display_body = bool(arguments.get("include_display_body", True))
        limit = _clamp_int(arguments.get("limit"), INBOX_THREAD_PAGE_SIZE, 1, INBOX_THREAD_PAGE_MAX)
        return await asyncio.to_thread(
            _build_inbox_thread_page,
            mailbox,
            thread_id,
            anchor_message_id=str(arguments.get("anchor_message_id", "")).strip(),
            before_index=before_index,
            limit=limit,
            include_display_body=include_display_body,
            force_refresh=bool(arguments.get("force_refresh", False)),
        )

    if tool == "get_inbox_message_display_body":
        if not mailbox:
            return {"error": "mailbox is required"}
        message_id = str(arguments.get("message_id", "")).strip()
        if not message_id:
            return {"error": "message_id is required"}
        try:
            from mail_agent.mail_providers.gmail.adapter import fetch_and_cache_message, normalize_mailbox, read_message
            normalized_mailbox = normalize_mailbox(mailbox)
            try:
                message = read_message(normalized_mailbox, message_id)
            except Exception:
                message = fetch_and_cache_message(normalized_mailbox, message_id)
        except Exception as exc:
            return {"error": str(exc)}
        if not isinstance(message, dict):
            return {"error": f"Message {message_id} not found"}
        return _build_inbox_message_display_response(mailbox, message)

    if tool == "get_card_detail":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        include_body = bool(arguments.get("include_body"))
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            return {"error": f"Card {card_id} not found"}
        thread_ctx = await asyncio.to_thread(_fetch_thread_context_sync, mailbox, card, preview_edges=True)

        latest_body = ""
        latest_body_html = ""
        body_loaded = False
        attachments: list[dict[str, Any]] = []
        cached_msg: dict[str, Any] | None = None
        try:
            from mail_agent.mail_providers.gmail.adapter import (
                attachment_metadata_from_message,
                normalize_mailbox,
                read_message,
            )
            cached_msg = read_message(normalize_mailbox(mailbox), card.message_id)
            if isinstance(cached_msg, dict):
                attachments = attachment_metadata_from_message(cached_msg)
        except Exception:
            cached_msg = None
        if include_body:
            body_loaded = True
            try:
                from mail_agent.mail_providers.gmail.adapter import normalize_mailbox, read_message
                msg = cached_msg if isinstance(cached_msg, dict) else read_message(normalize_mailbox(mailbox), card.message_id)
                if isinstance(msg, dict):
                    display_payload = _display_body_payload(msg, limit=INBOX_FULL_BODY_LIMIT)
                    latest_body_html = str(display_payload.get("body_html") or "")
                    latest_body = str(display_payload.get("body_text") or "")
            except Exception:
                from mail_agent.actions.service import _strip_quoted_reply
                latest_body = _strip_quoted_reply(_dedup_body(str(card.original.body or "")))[:INBOX_FULL_BODY_LIMIT]

        contact_ctx = {}
        try:
            from mail_agent.contact_memory.retriever import contact_email_from_header, retrieve_contact_context
            from mail_agent.contact_memory.types import ContactMemoryQuery
            contact_email = contact_email_from_header(card.original.from_addr)
            _detail_sampling = _build_sampling_for_run(arguments, invoke_id)
            contact = await retrieve_contact_context(ContactMemoryQuery(
                mailbox=mailbox,
                contact_email=contact_email,
                current_subject=card.original.thread or card.title,
                current_body=latest_body or card.original.body,
                current_thread_id=card.thread_id,
                purpose="thread_summary",
            ), sampling_create_message=_detail_sampling)
            from dataclasses import asdict
            contact_ctx = asdict(contact)
        except Exception:
            contact_ctx = {}
        # Load full cleanup bundle if this is a cleanup card
        if card.card_type == "cleanup_bundle" and card.bundled_count > 0:
            from mail_agent.storage.ops import get_cleanup_bundle
            try:
                full_bundled = await get_cleanup_bundle(mailbox)
                if full_bundled:
                    card.bundled_messages = full_bundled
            except Exception:
                pass
        return {
            "card": _serialize_card_for_frontend(card),
            "thread_context": thread_ctx,
            "contact_context": contact_ctx,
            "latest_body": latest_body,
            "latest_body_html": latest_body_html,
            "body_loaded": body_loaded,
            "attachments": attachments,
        }

    if tool == "get_thread_context_page":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            return {"error": f"Card {card_id} not found"}
        before_raw = arguments.get("before_index")
        try:
            before_index = int(before_raw) if before_raw is not None else None
        except Exception:
            before_index = None
        try:
            page_limit = int(arguments.get("limit") or 5)
        except Exception:
            page_limit = 5
        return await asyncio.to_thread(
            _fetch_thread_context_sync,
            mailbox,
            card,
            before_index=before_index,
            page_limit=page_limit,
        )

    if tool == "prepare_attachment_download":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        attachment_id = str(arguments.get("attachment_id", "")).strip()
        if not attachment_id:
            return {"error": "attachment_id is required"}
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            return {"error": f"Card {card_id} not found"}
        try:
            from mail_agent.mail_providers.gmail.adapter import (
                fetch_and_cache_message,
                fetch_attachment_bytes,
                find_attachment_for_token,
                normalize_mailbox,
                read_message,
            )
            normalized_mailbox = normalize_mailbox(mailbox)
            # 缓存 miss 或 token 对不上时回源 full 消息，避免缓存后附件不可用。
            try:
                msg = read_message(normalized_mailbox, card.message_id)
            except Exception:
                msg = None
            if not isinstance(msg, dict):
                msg = await asyncio.to_thread(fetch_and_cache_message, normalized_mailbox, card.message_id)
            if not isinstance(msg, dict):
                return {"ok": False, "error": f"Message {card.message_id} not found"}
            try:
                attachment = find_attachment_for_token(msg, attachment_id)
            except Exception:
                refreshed = await asyncio.to_thread(fetch_and_cache_message, normalized_mailbox, card.message_id)
                if not isinstance(refreshed, dict):
                    raise
                msg = refreshed
                attachment = find_attachment_for_token(msg, attachment_id)
            content = await asyncio.to_thread(
                fetch_attachment_bytes,
                normalized_mailbox,
                str(attachment.get("message_id") or card.message_id),
                str(attachment.get("gmail_attachment_id") or ""),
            )
            download_mode = _attachment_download_mode()
            if not _should_use_aps_files():
                return _loopback_attachment_download_payload(attachment, content)
            if download_mode == "loopback" and not _is_platform():
                return _loopback_attachment_download_payload(attachment, content)
            try:
                download = await _upload_attachment_for_download(normalized_mailbox, card_id, attachment, content)
                return {
                    "ok": True,
                    "delivery": "url",
                    "filename": attachment.get("filename") or "attachment",
                    "mime_type": attachment.get("mime_type") or "application/octet-stream",
                    "size": len(content),
                        "download_url": download.get("url") or download.get("download_url") or "",
                        "storage_key": download.get("storage_key") or "",
                        "expires_at": download.get("expires_at") or "",
                }
            except Exception as upload_exc:
                raise RuntimeError(
                    "Attachment download requires host upload or Anna Files storage in this runtime "
                    f"for files larger than {INLINE_ATTACHMENT_DIRECT_MAX_BYTES // (1024 * 1024)} MB. "
                    f"Upload failed first: {upload_exc}"
                ) from upload_exc
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if tool == "prepare_inbox_attachment_access":
        if not mailbox:
            return {"error": "mailbox is required"}
        message_id = str(arguments.get("message_id", "")).strip()
        attachment_id = str(arguments.get("attachment_id", "")).strip()
        mode = str(arguments.get("mode", "download")).strip().lower() or "download"
        if not message_id or not attachment_id:
            return {"error": "message_id and attachment_id are required"}
        if mode not in {"preview", "download"}:
            return {"error": "mode must be preview or download"}
        try:
            from mail_agent.mail_providers.gmail.adapter import (
                fetch_and_cache_message,
                fetch_attachment_bytes,
                find_attachment_for_token,
                normalize_mailbox,
                read_message,
            )
            normalized_mailbox = normalize_mailbox(mailbox)
            # 列表/History 可能只有摘要缓存；访问附件时必须能回源 full 消息。
            try:
                message = read_message(normalized_mailbox, message_id)
            except Exception:
                message = None
            if not isinstance(message, dict):
                message = await asyncio.to_thread(fetch_and_cache_message, normalized_mailbox, message_id)
            if not isinstance(message, dict):
                return {"ok": False, "error": f"Message {message_id} not found"}
            try:
                attachment = find_attachment_for_token(message, attachment_id)
            except Exception:
                refreshed = await asyncio.to_thread(fetch_and_cache_message, normalized_mailbox, message_id)
                if not isinstance(refreshed, dict):
                    raise
                message = refreshed
                attachment = find_attachment_for_token(message, attachment_id)
            if mode == "preview" and _attachment_preview_kind(attachment) == "download":
                # 预览能力必须在后端协议层再次校验，防止绕过前端直接请求任意附件的预览资源。
                return {
                    "ok": False,
                    "mode": mode,
                    "message_id": message_id,
                    "attachment_id": attachment_id,
                    "filename": str(attachment.get("filename") or "attachment"),
                    "mime_type": _normalized_attachment_mime_type(attachment),
                    "error": "This attachment type does not support preview. Download the file instead.",
                }
            content = await asyncio.to_thread(
                fetch_attachment_bytes,
                normalized_mailbox,
                str(attachment.get("message_id") or message_id),
                str(attachment.get("gmail_attachment_id") or ""),
            )
            if mode == "preview":
                if _should_use_aps_files():
                    preview = await _upload_attachment_for_download(normalized_mailbox, message_id, attachment, content)
                    preview = {
                        "ok": True,
                        "delivery": "url",
                        "filename": attachment.get("filename") or "attachment",
                        "size": len(content),
                        "preview_url": preview.get("url") or preview.get("download_url") or "",
                        "download_url": preview.get("url") or preview.get("download_url") or "",
                        "storage_key": preview.get("storage_key") or "",
                        "expires_at": preview.get("expires_at") or "",
                    }
                else:
                    preview = _loopback_attachment_download_payload(attachment, content, disposition="inline")
                    preview["preview_url"] = preview.get("download_url") or ""
                return _finalize_attachment_access_payload(
                    preview,
                    mode=mode,
                    message_id=message_id,
                    attachment_id=attachment_id,
                    attachment=attachment,
                )
            download_mode = _attachment_download_mode()
            if not _should_use_aps_files() or (download_mode == "loopback" and not _is_platform()):
                download = _loopback_attachment_download_payload(attachment, content, disposition="attachment")
                return _finalize_attachment_access_payload(
                    download,
                    mode=mode,
                    message_id=message_id,
                    attachment_id=attachment_id,
                    attachment=attachment,
                )
            try:
                uploaded = await _upload_attachment_for_download(normalized_mailbox, message_id, attachment, content)
                return _finalize_attachment_access_payload(
                    {
                        "ok": True,
                        "delivery": "url",
                        "filename": attachment.get("filename") or "attachment",
                        "size": len(content),
                        "download_url": uploaded.get("url") or uploaded.get("download_url") or "",
                        "storage_key": uploaded.get("storage_key") or "",
                        "expires_at": uploaded.get("expires_at") or "",
                    },
                    mode=mode,
                    message_id=message_id,
                    attachment_id=attachment_id,
                    attachment=attachment,
                )
            except Exception as upload_exc:
                log(f"inbox attachment upload fallback: {type(upload_exc).__name__}: {upload_exc}")
                raise RuntimeError(
                    "APS Files attachment delivery failed; retry the attachment access request."
                ) from upload_exc
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # Build sampling for Anna LLM path (same logic as _build_sampling_for_run)
    _sampling = _build_sampling_for_run(arguments, invoke_id)

    # ── Background (async) tools — return run_id immediately ──
    if tool == "start_summarize_thread":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        run_id = f"bg_{uuid.uuid4().hex[:12]}"
        MAIL_AGENT_RUNS[run_id] = {"run_id": run_id, "status": "queued", "stage": "summarize_thread", "progress": {}, "warnings": [], "started_at": beijing_now(), "updated_at": beijing_now(), "result": None, "error": "", "partial": {}}
        _save_run_checkpoint(run_id)
        asyncio.ensure_future(_handle_summarize_background(run_id, arguments, invoke_id))
        return {"success": True, "run_id": run_id, "status": "queued"}

    if tool == "start_generate_draft":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        run_id = f"bg_{uuid.uuid4().hex[:12]}"
        MAIL_AGENT_RUNS[run_id] = {"run_id": run_id, "status": "queued", "stage": "generate_draft_reply", "progress": {}, "warnings": [], "started_at": beijing_now(), "updated_at": beijing_now(), "result": None, "error": "", "partial": {}}
        _save_run_checkpoint(run_id)
        asyncio.ensure_future(_handle_generate_draft_background(run_id, arguments, invoke_id))
        return {"success": True, "run_id": run_id, "status": "queued"}

    if tool == "start_inbox_thread_assist":
        thread_id = str(arguments.get("thread_id", "")).strip()
        latest_message_id = str(arguments.get("latest_message_id", "")).strip()
        if not mailbox or not thread_id or not latest_message_id:
            return {"error": "mailbox, thread_id, and latest_message_id are required"}
        try:
            from mail_agent.storage.ops import get_inbox_thread_assist

            cached = await get_inbox_thread_assist(mailbox, thread_id, latest_message_id)
            cached_value = cached.get("value") if isinstance(cached.get("value"), dict) else {}
            if (
                cached.get("exists")
                and cached_value
                and cached_value.get("format_version") == THREAD_ASSIST_CACHE_VERSION
            ):
                return {
                    "success": True,
                    "status": "done",
                    "result": {**cached_value, "cached": True},
                }
        except Exception as exc:
            log(f"inbox thread assist cache lookup skipped: {type(exc).__name__}: {exc}")
        inflight_key = f"{mailbox.casefold()}:{thread_id}:{latest_message_id}"
        existing_run_id = INBOX_THREAD_ASSIST_INFLIGHT.get(inflight_key)
        existing_run = MAIL_AGENT_RUNS.get(existing_run_id or "") if existing_run_id else None
        if existing_run and str(existing_run.get("status") or "") in {"queued", "running"}:
            return {
                "success": True,
                "run_id": existing_run_id,
                "status": existing_run.get("status", "queued"),
                "stage": existing_run.get("stage", "inbox_thread_assist"),
            }
        if existing_run_id:
            INBOX_THREAD_ASSIST_INFLIGHT.pop(inflight_key, None)
        run_id = f"bg_{uuid.uuid4().hex[:12]}"
        MAIL_AGENT_RUNS[run_id] = {"run_id": run_id, "status": "queued", "stage": "inbox_thread_assist", "progress": {}, "warnings": [], "started_at": beijing_now(), "updated_at": beijing_now(), "result": None, "error": "", "partial": {}}
        INBOX_THREAD_ASSIST_INFLIGHT[inflight_key] = run_id
        _save_run_checkpoint(run_id)
        asyncio.ensure_future(_handle_inbox_thread_assist_background(run_id, arguments, invoke_id))
        return {"success": True, "run_id": run_id, "status": "queued"}

    if tool == "start_inbox_mail_prompt":
        thread_id = str(arguments.get("thread_id", "")).strip()
        anchor_message_id = str(arguments.get("anchor_message_id", "")).strip()
        latest_message_id = str(arguments.get("latest_message_id", "")).strip()
        visible_prompt = str(arguments.get("visible_prompt", "")).strip()
        if not mailbox or not thread_id or not anchor_message_id or not latest_message_id or not visible_prompt:
            return {"error": "mailbox, thread_id, anchor_message_id, latest_message_id, and visible_prompt are required"}
        run_id = str(arguments.get("run_id") or "").strip() or f"bg_{uuid.uuid4().hex[:12]}"
        existing = MAIL_AGENT_RUNS.get(run_id)
        if existing:
            # 前端会用同一 run_id 重试瞬时网络故障；复用任务而不是启动第二次生成。
            return {
                "success": existing.get("status") == "done",
                "run_id": run_id,
                "status": existing.get("status", "queued"),
                "result": _compact_run_result(existing.get("result")),
                "error": existing.get("error", ""),
            }
        MAIL_AGENT_RUNS[run_id] = {"run_id": run_id, "status": "queued", "stage": "inbox_mail_prompt", "progress": {}, "warnings": [], "started_at": beijing_now(), "updated_at": beijing_now(), "result": None, "error": "", "partial": {}}
        _save_run_checkpoint(run_id)
        asyncio.ensure_future(_handle_inbox_mail_prompt_background(run_id, arguments, invoke_id))
        return {"success": True, "run_id": run_id, "status": "queued"}

    if tool == "start_compose_mail_prompt":
        draft = arguments.get("draft") if isinstance(arguments.get("draft"), dict) else {}
        visible_prompt = str(arguments.get("visible_prompt", "")).strip()
        if not mailbox or not visible_prompt:
            return {"error": "mailbox and visible_prompt are required"}
        if not draft:
            return {"error": "draft is required"}
        run_id = str(arguments.get("run_id") or "").strip() or f"bg_{uuid.uuid4().hex[:12]}"
        existing = MAIL_AGENT_RUNS.get(run_id)
        if existing:
            # Retry of the same client-generated ID is idempotent: never run a
            # second LLM request that could yield a competing draft proposal.
            return {
                "success": existing.get("status") == "done",
                "run_id": run_id,
                "status": existing.get("status", "queued"),
                "result": _compact_run_result(existing.get("result")),
                "error": existing.get("error", ""),
            }
        MAIL_AGENT_RUNS[run_id] = {
            "run_id": run_id,
            "status": "queued",
            "stage": "compose_mail_prompt",
            "progress": {},
            "warnings": [],
            "started_at": beijing_now(),
            "updated_at": beijing_now(),
            "result": None,
            "error": "",
            "partial": {},
        }
        _save_run_checkpoint(run_id)
        asyncio.ensure_future(_handle_compose_mail_prompt_background(run_id, arguments, invoke_id))
        return {"success": True, "run_id": run_id, "status": "queued"}

    if tool == "summarize_thread":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            return {"error": f"Card {card_id} not found"}
        result = await summarize_thread(card, mailbox, sampling_create_message=_sampling)
        summary = result.get("summary") if isinstance(result, dict) else {}
        if isinstance(summary, dict):
            import json as _json
            card.thread_summary = _json.dumps(summary, ensure_ascii=False)
            from mail_agent.storage.ops import set_active_cards
            await set_active_cards(mailbox, cards)
        return result

    if tool == "generate_draft_reply":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        reply_mode = str(arguments.get("reply_mode", "reply_to_sender")).strip() or "reply_to_sender"
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            return {"error": f"Card {card_id} not found"}
        user_answers = arguments.get("user_answers") if isinstance(arguments.get("user_answers"), dict) else None
        result = await generate_draft_reply(card, mailbox, reply_mode, sampling_create_message=_sampling, user_answers=user_answers)
        draft_body = (result.get("draft") or {}).get("body", "") if isinstance(result, dict) else ""
        if draft_body:
            card.draft_reply = draft_body
            from mail_agent.storage.ops import set_active_cards
            await set_active_cards(mailbox, cards)
        return result

    if tool == "generate_ask_draft":
        message_id = str(arguments.get("message_id", "")).strip()
        thread_id = str(arguments.get("thread_id", "")).strip()
        from_addr = str(arguments.get("from_addr", "")).strip()
        subject = str(arguments.get("subject", "")).strip()
        user_answers = arguments.get("user_answers") if isinstance(arguments.get("user_answers"), dict) else {}
        if not mailbox or not message_id:
            return {"error": "mailbox and message_id are required"}
        if not user_answers:
            return {"error": "user_answers is required"}
        from mail_agent.ask.answer import generate_ask_item_draft
        return await generate_ask_item_draft(
            message_id=message_id,
            thread_id=thread_id,
            mailbox=mailbox,
            from_addr=from_addr,
            subject=subject,
            user_answers=user_answers,
            sampling_create_message=_sampling,
        )

    if tool == "revise_draft":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        current_draft = str(arguments.get("current_draft", "")).strip()
        revision_input = str(arguments.get("revision_input", "")).strip()
        user_answers = arguments.get("user_answers") if isinstance(arguments.get("user_answers"), dict) else None
        if not current_draft and not revision_input:
            return {"error": "current_draft or revision_input is required"}
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            return {"error": f"Card {card_id} not found"}
        result = await generate_draft_reply(card, mailbox, "reply_to_sender", sampling_create_message=_sampling, current_draft=current_draft, revision_input=revision_input)
        draft_body = (result.get("draft") or {}).get("body", "") if isinstance(result, dict) else ""
        if draft_body:
            card.draft_reply = draft_body
            from mail_agent.storage.ops import set_active_cards
            await set_active_cards(mailbox, cards)
        return result

    if tool == "record_card_decision":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        decision = str(arguments.get("decision", "")).strip()
        if decision not in ("no_action_needed", "handled_manually", "dismissed"):
            return {"error": f"Invalid decision: {decision}"}
        updated = await update_card_status(mailbox, card_id, "resolved" if decision != "dismissed" else "dismissed", decision)
        # Record card title for history
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        card_title = card.title if card else card_id
        # Record weak signal: no_action_needed → LearningRecord
        if decision == "no_action_needed":
            if card:
                await append_learning(card.original.from_addr, "no_action_needed")
        if card:
            if decision == "handled_manually":
                try:
                    from mail_agent.sync.gmail_status import fetch_gmail_thread_state
                    state = fetch_gmail_thread_state(mailbox, card.thread_id) if card.thread_id else None
                    if state:
                        card.gmail_state = state.to_card_state()
                    if state and state.latest_from_owner:
                        from mail_agent.storage.types import _now
                        decision = "replied_in_gmail"
                        card.status = "resolved"
                        card.resolution = decision
                        card.resolved_at = card.resolved_at or _now()
                        card.updated_at = _now()
                    from mail_agent.storage.ops import set_active_cards
                    await set_active_cards(mailbox, cards)
                except Exception:
                    pass
            try:
                from mail_agent.contact_memory.indexer import ingest_card_event
                await ingest_card_event(
                    mailbox,
                    card,
                    event_type="user_decision",
                    source="handle_action",
                    user_action=decision,
                    sampling_create_message=_sampling,
                )
            except Exception:
                pass
        # Write history
        from mail_agent.storage.ops import append_card_action
        await append_card_action(mailbox, card_id, card_title, decision, "", **_card_context(card) if card else {})
        return {"ok": updated is not None, "card_id": card_id, "decision": decision}

    if tool == "mark_card_read":
        return await _handle_mark_card_read(arguments)

    if tool == "clear_active_cards":
        if not mailbox:
            return {"error": "mailbox is required"}
        from mail_agent.storage.ops import set_active_cards as _set_active, set_scan_state
        from mail_agent.storage.types import ActiveCards, ScanState, _now
        await _set_active(mailbox, ActiveCards(cards=[], updated_at=_now()))
        # Reset scan state so the UI shows first-run welcome
        await set_scan_state(mailbox, ScanState(
            mailbox=mailbox,
            last_scan_ts="",
            last_message_internal_date="",
            total_scans=0,
            total_processed=0,
        ))
        return {"ok": True, "cleared": True}

    if tool == "mark_cleanup_read":
        return await _handle_mark_cleanup_read(arguments)

    if tool == "record_snooze":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        snooze_option = str(arguments.get("snooze_option", "")).strip()
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        card_title = card.title if card else card_id
        if snooze_option == "dont_prioritize":
            if card:
                await add_snooze_sender(card.original.from_addr)
                await add_snooze_thread(card.original.thread)
                reasons = arguments.get("reasons") if isinstance(arguments.get("reasons"), list) else []
                if reasons:
                    from mail_agent.storage.ops import get_user_prefs, set_snooze_prefs
                    prefs = await get_user_prefs()
                    for r in reasons:
                        if str(r) not in prefs.snooze.reasons:
                            prefs.snooze.reasons.append(str(r))
                    await set_snooze_prefs(prefs.snooze)
                await update_card_status(mailbox, card_id, "resolved", "dont_prioritize")
            from mail_agent.storage.ops import append_card_action
            await append_card_action(mailbox, card_id, card_title, "snooze", "dont_prioritize", **_card_context(card) if card else {})
            return {"ok": True, "card_id": card_id, "option": snooze_option}
        else:
            from datetime import datetime as _datetime, timedelta as _timedelta
            from mail_agent.storage.types import BEIJING_TZ
            now_ts = _datetime.now(BEIJING_TZ)
            if snooze_option == "tomorrow":
                until = (now_ts + _timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            elif snooze_option == "next_week":
                days_until_monday = (7 - now_ts.weekday()) % 7 or 7
                until = (now_ts + _timedelta(days=days_until_monday)).replace(hour=9, minute=0, second=0, microsecond=0)
            else:
                return {"error": f"Unknown snooze option: {snooze_option}"}
            for c in cards.cards:
                if c.card_id == card_id:
                    c.status = "snoozed"
                    c.snooze_until = until.isoformat()
                    from mail_agent.storage.ops import set_active_cards
                    await set_active_cards(mailbox, cards)
                    break
            from mail_agent.storage.ops import append_card_action
            await append_card_action(mailbox, card_id, card_title, "snooze", snooze_option, **_card_context(card) if card else {})
            return {"ok": True, "card_id": card_id, "option": snooze_option, "snooze_until": until.isoformat()}

    if tool == "restore_card":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        await update_card_status(mailbox, card_id, "pending")
        # Remove from snooze prefs (don't-prioritize)
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if card:
            try:
                from mail_agent.storage.ops import remove_snooze_sender, remove_snooze_thread
                await remove_snooze_sender(card.original.from_addr)
                await remove_snooze_thread(card.original.thread)
            except Exception:
                pass
        # If it's a cleanup bundle card, mark messages as UNREAD in Gmail
        if card and card.card_type == "cleanup_bundle" and card.bundled_count > 0:
            try:
                from mail_agent.storage.ops import get_cleanup_bundle
                full_bundled = await get_cleanup_bundle(mailbox)
                bundled = full_bundled if full_bundled else card.bundled_messages
                import json as _json3
                import urllib.request as _ur2
                from mail_agent.mail_providers.gmail.adapter import get_access_token as _gt2
                msg_ids = [str(m.get("message_id", "")) for m in bundled if str(m.get("message_id", ""))]
                if msg_ids:
                    token = _gt2(mailbox)
                    body = _json3.dumps({"ids": msg_ids, "addLabelIds": ["UNREAD"]}).encode("utf-8")
                    req = _ur2.Request(
                        "https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify",
                        data=body,
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                        method="POST",
                    )
                    with _ur2.urlopen(req, timeout=30) as resp:
                        pass
            except Exception:
                pass
        # Write history
        card_title = card.title if card else card_id
        from mail_agent.storage.ops import append_card_action
        await append_card_action(mailbox, card_id, card_title, "restore", "", **_card_context(card) if card else {})
        return {"ok": True, "card_id": card_id}

    if tool == "delete_custom_plan":
        plan_id = str(arguments.get("plan_id", "")).strip()
        if not plan_id:
            return {"error": "plan_id is required"}
        from mail_agent.storage.ops import delete_custom_plan
        await delete_custom_plan(plan_id)
        return {"ok": True, "plan_id": plan_id}

    if tool == "clear_cards":
        category = str(arguments.get("category", "")).strip()
        if not mailbox or not category:
            return {"error": "mailbox and category are required"}
        from mail_agent.storage.ops import clear_cards_by_category
        removed = await clear_cards_by_category(mailbox, category)
        return {"ok": True, "removed": removed, "category": category, "mailbox": mailbox}

    if tool == "clear_history":
        from mail_agent.storage.ops import clear_run_history
        await clear_run_history()
        return {"ok": True}

    if tool == "reset_all_data":
        from mail_agent.storage.ops import reset_all_data
        import shutil
        await reset_all_data()
        # Local file-system cleanup: remove the entire data directory.
        # APS stores data remotely so this is a no-op there.
        root = data_root()
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        return {"ok": True}

    if tool == "reset_mailbox_scan_history":
        mbox = str(arguments.get("mailbox", "")).strip()
        if not mbox:
            return {"error": "mailbox is required"}
        from mail_agent.storage.ops import reset_mailbox_scan_history
        return await reset_mailbox_scan_history(mbox)

    if tool == "delete_mailbox_data":
        mbox = str(arguments.get("mailbox", "")).strip()
        if not mbox:
            return {"error": "mailbox is required"}
        from mail_agent.storage.ops import delete_mailbox_data
        return await delete_mailbox_data(mbox)

    if tool == "reply_now":
        log(f"[reply_now] mailbox={mailbox} card_id={card_id} dry_run={arguments.get('dry_run', True)} reply_mode={arguments.get('reply_mode', 'reply_to_sender')} draft_len={len(str(arguments.get('draft_body', '')))}")
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        draft_body = str(arguments.get("draft_body", "")).strip()
        if not draft_body:
            return {"error": "draft_body is required"}
        reply_mode = str(arguments.get("reply_mode", "reply_to_sender")).strip() or "reply_to_sender"
        dry_run = arguments.get("dry_run", True)
        if not isinstance(dry_run, bool):
            dry_run = True
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            log(f"[reply_now] card not found: {card_id} among {len(cards.cards)} cards")
            return {"error": f"Card {card_id} not found"}
        log(f"[reply_now] card found: {card.title} thread_id={card.thread_id} to={card.original.from_addr}")
        if not dry_run:
            try:
                from mail_agent.sync.gmail_status import fetch_gmail_thread_state
                pre_state = fetch_gmail_thread_state(mailbox, card.thread_id) if card.thread_id else None
                if pre_state and pre_state.latest_from_owner:
                    from mail_agent.storage.types import _now
                    card.gmail_state = pre_state.to_card_state()
                    card.status = "resolved"
                    card.resolution = "replied_in_gmail"
                    card.resolved_at = _now()
                    card.updated_at = _now()
                    from mail_agent.storage.ops import set_active_cards
                    await set_active_cards(mailbox, cards)
                    return {"ok": True, "dry_run": False, "already_replied": True, "message": "Thread already has a Gmail reply from the mailbox owner."}
            except Exception as exc:
                log(f"[reply_now] pre-send Gmail state check failed, continuing: {type(exc).__name__}: {exc}")
        result = await reply_now(card, mailbox, draft_body, reply_mode, dry_run=dry_run)
        log(f"[reply_now] result: ok={result.get('ok')} dry_run={result.get('dry_run')} error={result.get('error', '')}")
        if result.get("ok") and not dry_run:
            warnings = list(result.get("warnings") or []) if isinstance(result.get("warnings"), list) else []
            try:
                from mail_agent.sync.gmail_status import mark_card_thread_read_in_gmail, refresh_card_gmail_state
                sync_result = await mark_card_thread_read_in_gmail(mailbox, card)
                result["gmail_sync"] = sync_result
                await refresh_card_gmail_state(mailbox, card)
            except Exception as exc:
                warnings.append(f"gmail sync after reply failed: {type(exc).__name__}: {exc}")
            from mail_agent.storage.types import _now
            card.status = "resolved"
            card.resolution = "replied"
            card.resolved_at = _now()
            card.updated_at = _now()
            from mail_agent.storage.ops import set_active_cards
            await set_active_cards(mailbox, cards)
            if warnings:
                result["warnings"] = warnings
        if result.get("ok"):
            try:
                from mail_agent.contact_memory.indexer import ingest_card_event
                await ingest_card_event(
                    mailbox,
                    card,
                    event_type="user_replied",
                    source="handle_action",
                    user_action="reply",
                    draft_excerpt=draft_body,
                    sampling_create_message=_sampling,
                )
            except Exception:
                pass
        # Write history
        from mail_agent.storage.ops import append_card_action
        detail_preview = draft_body[:80]
        await append_card_action(mailbox, card_id, card.title, "reply", detail_preview, **_card_context(card))
        return result

    if tool == "reply_from_ask":
        if not mailbox:
            return {"error": "mailbox is required"}
        thread_id = str(arguments.get("thread_id", "")).strip()
        to_addr = str(arguments.get("to_addr", "")).strip()
        body = str(arguments.get("body", "")).strip()
        body_html = str(arguments.get("body_html", "")).strip()
        if not thread_id or not to_addr or not body:
            return {"error": "thread_id, to_addr, and body are required"}
        reply_mode = str(arguments.get("reply_mode", "reply_to_sender")).strip() or "reply_to_sender"
        # 前端显式传入的抄送 / 密送（字符串或列表均可）
        cc_raw = arguments.get("cc_addr") if arguments.get("cc_addr") is not None else arguments.get("cc")
        bcc_raw = arguments.get("bcc_addr") if arguments.get("bcc_addr") is not None else arguments.get("bcc")
        if isinstance(cc_raw, list):
            cc_addr = ", ".join(str(item).strip() for item in cc_raw if str(item).strip())
        else:
            cc_addr = str(cc_raw or "").strip()
        if isinstance(bcc_raw, list):
            bcc_addr = ", ".join(str(item).strip() for item in bcc_raw if str(item).strip())
        else:
            bcc_addr = str(bcc_raw or "").strip()
        dry_run = arguments.get("dry_run", True)
        if not isinstance(dry_run, bool):
            dry_run = True
        from mail_agent.mail_providers.gmail.adapter import send_reply
        from mail_agent.mail_providers.gmail.outgoing_attachments import (
            delete_staged_attachments,
        )
        import asyncio as _asyncio
        attachment_meta = arguments.get("attachments") if isinstance(arguments.get("attachments"), list) else []
        if dry_run:
            return {"ok": True, "dry_run": True, "message": "Mock: reply was NOT sent."}
        try:
            loaded_attachments = await _load_outgoing_attachments_for_send(mailbox, attachment_meta)
            result = await _asyncio.to_thread(
                send_reply,
                mailbox,
                thread_id,
                to_addr,
                body,
                body_html=body_html or None,
                reply_mode=reply_mode,
                cc_addr=cc_addr,
                bcc_addr=bcc_addr,
                attachments=loaded_attachments,
            )
            await _delete_outgoing_attachments(mailbox, attachment_meta)
            from mail_agent.storage.ops import append_card_action
            await append_card_action(mailbox, "", thread_id, "reply_from_ask", body[:80])
            return {"ok": True, "dry_run": False, "result": result}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if tool == "search_compose_contacts":
        query = str(arguments.get("query", "")).strip()
        if not mailbox or not query:
            return {"error": "mailbox and query is required"}
        from mail_agent.mail_providers.gmail.adapter import search_contacts
        import asyncio as _asyncio
        try:
            return await _asyncio.to_thread(search_contacts, mailbox, query, limit=int(arguments.get("limit") or 10))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if tool == "begin_stage_outgoing_attachment":
        # APS 模式只协商预签名地址；文件字节由浏览器直接上传到 APS。
        if not mailbox:
            return {"error": "mailbox is required"}
        filename = str(arguments.get("filename") or "attachment")
        mime_type = str(arguments.get("mime_type") or "application/octet-stream")
        try:
            size = int(arguments.get("size") or 0)
        except (TypeError, ValueError):
            return {"error": "size is required"}
        try:
            existing_total = int(arguments.get("existing_total_bytes") or 0)
        except (TypeError, ValueError):
            existing_total = 0
        draft_scope = str(arguments.get("draft_scope") or "compose").strip().lower() or "compose"
        draft_key = str(arguments.get("draft_key") or "").strip()
        try:
            from mail_agent.mail_providers.gmail.outgoing_attachments import create_stage_upload_slot, validate_outgoing_attachment_meta
            meta = validate_outgoing_attachment_meta(
                filename=filename,
                mime_type=mime_type,
                size=size,
                existing_total_bytes=existing_total,
            )
            attachment_id = uuid.uuid4().hex
            if _should_use_aps_files():
                # 平台附件始终走 APS Files 预签名 PUT；begin 只协商 URL，不传文件字节。
                storage_key = _aps_outgoing_attachment_path(mailbox, attachment_id, meta["filename"])
                begin = await _aps_files.upload_begin(
                    path=storage_key,
                    size_bytes=meta["size"],
                    content_type=meta["mime_type"],
                    metadata={"mailbox": mailbox, "attachment_id": attachment_id, "draft_scope": draft_scope, "draft_key": draft_key},
                    scope="user",
                    timeout=APS_FILES_REVERSE_RPC_TIMEOUT_SECONDS,
                )
                upload_url = str(begin.get("put_url") or begin.get("presigned_url") or "")
                if not upload_url:
                    raise RuntimeError("APS Files did not return an upload URL.")
                return {
                    "ok": True,
                    "attachment_id": attachment_id,
                    "filename": meta["filename"],
                    "mime_type": meta["mime_type"],
                    "size": meta["size"],
                    "storage_key": storage_key,
                    "upload_url": upload_url,
                    "upload_headers": begin.get("headers") or begin.get("fields") or {},
                    "expires_at": begin.get("expires_at") or "",
                }
            slot = create_stage_upload_slot(
                mailbox,
                filename=meta["filename"],
                mime_type=meta["mime_type"],
                size=meta["size"],
                existing_total_bytes=existing_total,
                draft_scope=draft_scope,
                draft_key=draft_key,
            )
            base_url = _ensure_loopback_download_server()
            return {
                "ok": True,
                "attachment_id": slot["attachment_id"],
                "filename": slot["filename"],
                "mime_type": slot["mime_type"],
                "size": slot["size"],
                "storage_key": slot["storage_key"],
                "upload_url": f"{base_url}/upload/{slot['upload_token']}",
                "expires_at": datetime.fromtimestamp(float(slot["expires_at_ts"]), tz=timezone.utc).isoformat(),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if tool == "complete_stage_outgoing_attachment":
        if not mailbox:
            return {"error": "mailbox is required"}
        storage_key = str(arguments.get("storage_key") or "").strip()
        if not storage_key:
            return {"error": "storage_key is required"}
        try:
            size = int(arguments.get("size") or 0)
        except (TypeError, ValueError):
            return {"error": "size is required"}
        if _should_use_aps_files():
            from mail_agent.mail_providers.gmail.adapter import sanitize_mailbox_id
            expected_prefix = f"anna-inbox/mailbox/{sanitize_mailbox_id(mailbox)}/outgoing/"
            if not storage_key.startswith(expected_prefix):
                return {"ok": False, "error": "Invalid APS attachment object path"}
            result = await _aps_files.upload_complete(
                path=storage_key,
                size_bytes=size,
                content_type=str(arguments.get("mime_type") or "application/octet-stream"),
                scope="user",
                timeout=APS_FILES_REVERSE_RPC_TIMEOUT_SECONDS,
            )
            return {"ok": True, "storage_key": storage_key, **result}
        return {"ok": True, "storage_key": storage_key}

    if tool == "delete_staged_outgoing_attachment":
        if not mailbox:
            return {"error": "mailbox is required"}
        storage_key = str(arguments.get("storage_key") or "").strip()
        if not storage_key:
            return {"error": "storage_key is required"}
        if _should_use_aps_files():
            from mail_agent.mail_providers.gmail.adapter import sanitize_mailbox_id
            expected_prefix = f"anna-inbox/mailbox/{sanitize_mailbox_id(mailbox)}/outgoing/"
            if not storage_key.startswith(expected_prefix):
                return {"ok": False, "error": "Invalid APS attachment object path"}
            result = await _aps_files.delete(
                path=storage_key,
                scope="user",
                timeout=APS_FILES_REVERSE_RPC_TIMEOUT_SECONDS,
            )
            return {"ok": True, **result}
        from mail_agent.mail_providers.gmail.outgoing_attachments import delete_staged_attachment
        deleted = delete_staged_attachment(mailbox, storage_key)
        return {"ok": True, "deleted": deleted}

    if tool == "prepare_staged_outgoing_attachment_access":
        # 草稿恢复后给图片预览用的短期 URL（APS）或本地 loopback。
        if not mailbox:
            return {"error": "mailbox is required"}
        storage_key = str(arguments.get("storage_key") or "").strip()
        filename = str(arguments.get("filename") or "attachment")
        mime_type = str(arguments.get("mime_type") or "application/octet-stream")
        if not storage_key:
            return {"error": "storage_key is required"}
        try:
            if _should_use_aps_files():
                from mail_agent.mail_providers.gmail.adapter import sanitize_mailbox_id
                expected_prefix = f"anna-inbox/mailbox/{sanitize_mailbox_id(mailbox)}/outgoing/"
                if not storage_key.startswith(expected_prefix):
                    return {"ok": False, "error": "Invalid APS attachment object path"}
                access = await _aps_files.download_url(
                    path=storage_key,
                    expires_in=900,
                    scope="user",
                    timeout=APS_FILES_REVERSE_RPC_TIMEOUT_SECONDS,
                )
                preview_url = str(access.get("url") or access.get("download_url") or "")
                payload = {"ok": True, "delivery": "url", "filename": filename, "mime_type": mime_type, "preview_url": preview_url, "download_url": preview_url, "expires_at": access.get("expires_at") or ""}
            else:
                from mail_agent.mail_providers.gmail.outgoing_attachments import read_staged_attachment
                content = read_staged_attachment(mailbox, storage_key)
                payload = _loopback_attachment_download_payload(
                    {"filename": filename, "mime_type": mime_type},
                    content,
                    disposition="inline",
                )
            payload["preview_url"] = payload.get("download_url") or ""
            return payload
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if tool in {"get_compose_draft", "create_or_update_compose_draft", "delete_compose_draft", "list_compose_drafts"}:
        draft_id = str(arguments.get("draft_id", "")).strip()
        from mail_agent.storage.ops import delete_compose_draft, get_compose_draft, list_compose_drafts, set_compose_draft
        if tool == "get_compose_draft":
            if not mailbox or not draft_id:
                return {"error": "mailbox and draft_id are required"}
            result = await get_compose_draft(mailbox, draft_id)
            return {"mailbox": mailbox, **result}
        if tool == "delete_compose_draft":
            if not mailbox or not draft_id:
                return {"error": "mailbox and draft_id are required"}
            # 删除草稿时同步清理已 stage 的外发附件文件。
            try:
                existing = await get_compose_draft(mailbox, draft_id)
                draft_value = existing.get("draft") if isinstance(existing.get("draft"), dict) else {}
                await _delete_outgoing_attachments(mailbox, draft_value.get("attachments") if isinstance(draft_value, dict) else [])
            except Exception:
                pass
            await delete_compose_draft(mailbox, draft_id)
            return {"ok": True, "mailbox": mailbox, "draft_id": draft_id}
        if tool == "list_compose_drafts":
            if not mailbox:
                return {"error": "mailbox is required"}
            return await list_compose_drafts(mailbox, limit=int(arguments.get("limit") or 100))
        if not mailbox:
            return {"error": "mailbox is required"}
        raw_draft = arguments.get("draft") if isinstance(arguments.get("draft"), dict) else {}
        result = await set_compose_draft(mailbox, raw_draft, if_match=str(arguments.get("if_match") or "") or None)
        return {"mailbox": mailbox, **result}

    if tool == "send_compose_emails":
        if not mailbox:
            return {"error": "mailbox is required"}
        raw_messages = arguments.get("messages") if isinstance(arguments.get("messages"), list) else []
        if not raw_messages:
            return {"error": "messages is required"}
        from mail_agent.mail_providers.gmail.adapter import send_compose_email
        from mail_agent.mail_providers.gmail.outgoing_attachments import (
            delete_staged_attachments,
        )
        import asyncio as _asyncio
        results: list[dict[str, Any]] = []
        for item in raw_messages[:100]:
            draft = item if isinstance(item, dict) else {}
            draft_id = str(draft.get("id") or "")
            try:
                attachment_meta = draft.get("attachments") if isinstance(draft.get("attachments"), list) else []
                loaded_attachments = await _load_outgoing_attachments_for_send(mailbox, attachment_meta)
                sent = await _asyncio.to_thread(
                    send_compose_email,
                    mailbox,
                    draft.get("recipients") if isinstance(draft.get("recipients"), list) else [],
                    str(draft.get("subject") or ""),
                    str(draft.get("body") or ""),
                    cc=draft.get("cc") if isinstance(draft.get("cc"), list) else draft.get("cc"),
                    bcc=draft.get("bcc") if isinstance(draft.get("bcc"), list) else draft.get("bcc"),
                    body_html=str(draft.get("body_html") or "") or None,
                    attachments=loaded_attachments,
                )
                await _delete_outgoing_attachments(mailbox, attachment_meta)
                results.append({"id": draft_id, "ok": True, "result": sent})
            except Exception as exc:
                results.append({"id": draft_id, "ok": False, "error": str(exc)})
        return {"ok": all(item.get("ok") for item in results), "results": results}

    if tool == "mark_read_from_ask":
        if not mailbox:
            return {"error": "mailbox is required"}
        raw_ids = arguments.get("message_ids") or []
        message_ids = [str(mid).strip() for mid in raw_ids if str(mid).strip()] if isinstance(raw_ids, list) else []
        if not message_ids:
            return {"error": "message_ids (non-empty array) is required"}
        from mail_agent.mail_providers.gmail.adapter import batch_mark_read
        import asyncio as _asyncio
        try:
            result = await _asyncio.to_thread(batch_mark_read, mailbox, message_ids)
            from mail_agent.storage.ops import append_card_action
            await append_card_action(mailbox, "", "", "mark_read_from_ask", f"{len(message_ids)} emails")
            return {"ok": True, "marked": len(message_ids), "result": result}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if tool == "set_message_starred":
        if not mailbox:
            return {"error": "mailbox is required"}
        message_id = str(arguments.get("message_id", "")).strip()
        starred = arguments.get("starred", True)
        if not message_id or not isinstance(starred, bool):
            return {"error": "message_id and boolean starred are required"}
        from mail_agent.mail_providers.gmail.adapter import set_message_starred
        import asyncio as _asyncio
        try:
            result = await _asyncio.to_thread(set_message_starred, mailbox, message_id, starred)
            return {"ok": True, "message_id": message_id, "starred": starred, "result": result}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if tool == "modify_message_labels":
        if not mailbox:
            return {"error": "mailbox is required"}
        raw_message_ids = arguments.get("message_ids") or []
        add_label_ids = arguments.get("add_label_ids") or []
        remove_label_ids = arguments.get("remove_label_ids") or []
        message_ids = [str(mid).strip() for mid in raw_message_ids if str(mid).strip()] if isinstance(raw_message_ids, list) else []
        add_labels = [str(label).strip().upper() for label in add_label_ids if str(label).strip()] if isinstance(add_label_ids, list) else []
        remove_labels = [str(label).strip().upper() for label in remove_label_ids if str(label).strip()] if isinstance(remove_label_ids, list) else []
        if not message_ids:
            return {"error": "message_ids (non-empty array) is required"}
        from mail_agent.mail_providers.gmail.adapter import modify_message_labels
        import asyncio as _asyncio
        try:
            result = await _asyncio.to_thread(
                modify_message_labels,
                mailbox,
                message_ids,
                add_label_ids=add_labels,
                remove_label_ids=remove_labels,
            )
            return {"ok": True, **result}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if tool == "update_inbox_thread_state":
        if not mailbox:
            return {"error": "mailbox is required"}
        thread_id = str(arguments.get("thread_id", "")).strip()
        operation = str(arguments.get("operation", "")).strip().lower()
        if not thread_id or not operation:
            return {"error": "thread_id and operation are required"}
        from mail_agent.mail_providers.gmail.adapter import update_thread_state
        import asyncio as _asyncio
        try:
            result = await _asyncio.to_thread(update_thread_state, mailbox, thread_id, operation)
            return {"ok": True, **result}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if tool == "trash_from_ask":
        if not mailbox:
            return {"error": "mailbox is required"}
        raw_ids = arguments.get("message_ids") or []
        message_ids = [str(mid).strip() for mid in raw_ids if str(mid).strip()] if isinstance(raw_ids, list) else []
        if not message_ids:
            return {"error": "message_ids (non-empty array) is required"}
        from mail_agent.mail_providers.gmail.adapter import trash_email
        import asyncio as _asyncio
        errors = []
        for mid in message_ids:
            try:
                await _asyncio.to_thread(trash_email, mailbox, mid)
            except Exception as exc:
                errors.append(f"{mid}: {exc}")
        from mail_agent.storage.ops import append_card_action
        await append_card_action(mailbox, "", "", "trash_from_ask", f"{len(message_ids)} emails")
        if errors:
            return {"ok": False, "trashed": len(message_ids) - len(errors), "errors": errors}
        return {"ok": True, "trashed": len(message_ids)}

    if tool == "get_inbox_thread_draft":
        thread_id = str(arguments.get("thread_id", "")).strip()
        if not mailbox or not thread_id:
            return {"error": "mailbox and thread_id are required"}
        from mail_agent.storage.ops import get_inbox_thread_draft
        draft = await get_inbox_thread_draft(mailbox, thread_id)
        value = draft.get("value") if isinstance(draft.get("value"), dict) else {}
        attachments = value.get("attachments") if isinstance(value.get("attachments"), list) else []
        return {
            "mailbox": mailbox,
            "thread_id": thread_id,
            "exists": bool(draft.get("exists")),
            "etag": str(draft.get("etag") or ""),
            "body": str(value.get("body") or ""),
            "body_html": str(value.get("body_html") or ""),
            "attachments": attachments,
            "updated_at": str(value.get("updated_at") or ""),
        }

    if tool == "list_inbox_thread_drafts":
        if not mailbox:
            return {"error": "mailbox is required"}
        limit = max(1, min(int(arguments.get("limit") or 100), 500))
        from mail_agent.storage.ops import list_inbox_thread_drafts, set_inbox_thread_draft
        payload = await list_inbox_thread_drafts(mailbox, limit=limit)
        messages: list[dict[str, Any]] = []
        for draft in payload.get("drafts") or []:
            if not isinstance(draft, dict):
                continue
            body = str(draft.get("body") or "")
            if not body.strip():
                continue
            meta = draft.get("message") if isinstance(draft.get("message"), dict) else {}
            thread_id = str(draft.get("thread_id") or meta.get("thread_id") or "")
            metadata_complete = bool(
                (meta.get("id") or meta.get("message_id"))
                and (meta.get("from") or meta.get("to"))
            )
            if thread_id and not metadata_complete:
                try:
                    thread_messages = _read_cached_thread_messages(mailbox, thread_id)
                    if not thread_messages:
                        thread_messages = await asyncio.to_thread(_load_thread_messages, mailbox, thread_id)
                    if thread_messages:
                        latest = thread_messages[-1]
                        latest_labels = [str(label) for label in (latest.get("label_ids") or [])]
                        meta = {
                            **meta,
                            "id": str(latest.get("id") or meta.get("id") or ""),
                            "thread_id": thread_id,
                            "internal_date": str(latest.get("internal_date") or meta.get("internal_date") or ""),
                            "date": str(latest.get("date") or meta.get("date") or ""),
                            "from": str(latest.get("from") or meta.get("from") or ""),
                            "to": str(latest.get("to") or meta.get("to") or ""),
                            "subject": str(latest.get("subject") or meta.get("subject") or ""),
                            "label_ids": latest_labels,
                            "important": "IMPORTANT" in [label.upper() for label in latest_labels],
                            "starred": "STARRED" in [label.upper() for label in latest_labels],
                            "has_attachment": bool(latest.get("attachments")),
                            "attachment_count": len(latest.get("attachments") or []),
                        }
                        try:
                            await set_inbox_thread_draft(
                                mailbox,
                                thread_id,
                                body,
                                body_html=str(draft.get("body_html") or "") or None,
                                if_match=str(draft.get("etag") or "") or None,
                                message=meta,
                                updated_at=str(draft.get("updated_at") or "") or None,
                            )
                        except Exception:
                            pass
                except Exception as exc:
                    log(f"draft metadata recovery failed for {thread_id}: {type(exc).__name__}: {exc}")
            message_id = str(meta.get("id") or meta.get("message_id") or thread_id or f"draft_{len(messages) + 1}")
            labels = [str(label)[:80] for label in (meta.get("label_ids") or []) if str(label).upper() != "DRAFT"][:31]
            labels.append("DRAFT")
            messages.append({
                "id": message_id[:128],
                "thread_id": thread_id[:128],
                "mailbox": mailbox,
                "internal_date": str(meta.get("internal_date") or "")[:32],
                "date": str(meta.get("date") or draft.get("updated_at") or "")[:128],
                "from": str(meta.get("from") or "")[:512],
                "to": str(meta.get("to") or "")[:512],
                "subject": str(meta.get("subject") or "(no subject)")[:512],
                "snippet": body[:120],
                "body_preview": body[:120],
                "draft_body": body,
                "draft_local": True,
                "label_ids": labels,
                "unread": False,
                "important": bool(meta.get("important")),
                "starred": bool(meta.get("starred")),
                "has_attachment": bool(meta.get("has_attachment")),
                "attachment_count": int(meta.get("attachment_count") or 0),
                "body_cached": False,
            })
        return {
            "mailbox": mailbox,
            "category": "drafts",
            "count": len(messages),
            "messages": messages,
            "updated_at": str(payload.get("updated_at") or ""),
        }

    if tool == "save_inbox_thread_draft":
        thread_id = str(arguments.get("thread_id", "")).strip()
        body = str(arguments.get("body", ""))
        body_html = str(arguments.get("body_html", ""))
        if_match = str(arguments.get("if_match", "")).strip() or None
        if not mailbox or not thread_id:
            return {"error": "mailbox and thread_id are required"}
        message = arguments.get("message") if isinstance(arguments.get("message"), dict) else {}
        attachments = arguments.get("attachments") if isinstance(arguments.get("attachments"), list) else None
        from mail_agent.storage.ops import set_inbox_thread_draft
        result = await set_inbox_thread_draft(
            mailbox,
            thread_id,
            body,
            body_html=body_html or None,
            if_match=if_match,
            message=message,
            attachments=attachments,
        )
        return {
            "ok": True,
            "mailbox": mailbox,
            "thread_id": thread_id,
            "etag": str(result.get("etag") or ""),
            "updated": bool(result.get("ok", True)),
        }

    if tool == "delete_inbox_thread_draft":
        thread_id = str(arguments.get("thread_id", "")).strip()
        if not mailbox or not thread_id:
            return {"error": "mailbox and thread_id are required"}
        from mail_agent.storage.ops import delete_inbox_thread_draft, get_inbox_thread_draft
        try:
            existing = await get_inbox_thread_draft(mailbox, thread_id)
            value = existing.get("value") if isinstance(existing.get("value"), dict) else {}
            await _delete_outgoing_attachments(mailbox, value.get("attachments") if isinstance(value, dict) else [])
        except Exception:
            pass
        result = await delete_inbox_thread_draft(mailbox, thread_id)
        return {"ok": True, "mailbox": mailbox, "thread_id": thread_id, "result": result}

    if tool == "record_learning":
        pattern = str(arguments.get("pattern", "")).strip()
        action = str(arguments.get("action", "")).strip()
        if not pattern or not action:
            return {"error": "pattern and action are required"}
        await append_learning(pattern, action)
        return {"ok": True, "pattern": pattern, "action": action}

    if tool == "get_run_history":
        history = await get_run_history(limit=20)
        return {"history": [serialize_value(h) for h in history]}

    return {"error": f"Unknown V2 tool: {tool}"}

__all__ = [name for name in globals() if not name.startswith("__")]
