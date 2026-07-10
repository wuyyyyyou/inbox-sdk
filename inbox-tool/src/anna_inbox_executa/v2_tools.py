from __future__ import annotations

import re

from anna_inbox_executa.common import *
from anna_inbox_executa.card_tools import _handle_generate_draft_background, _handle_summarize_background, _serialize_card_for_frontend
from anna_inbox_executa.gmail_tools import _dedup_body, _resolve_cid_images, _sanitize_email_html
from anna_inbox_executa.sampling_tools import *
from anna_inbox_executa.storage_tools import *

INLINE_ATTACHMENT_DIRECT_MAX_BYTES = 4 * 1024 * 1024
_DOWNLOAD_SERVER_LOCK = threading.Lock()
_DOWNLOAD_SERVER: Any | None = None
_DOWNLOAD_SERVER_THREAD: threading.Thread | None = None
_DOWNLOAD_TOKENS: dict[str, dict[str, Any]] = {}
_DOWNLOAD_TOKEN_TTL_SECONDS = 15 * 60


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
                self.parts.append(_html_escape(data, quote=False))

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
    """Upload attachment bytes and return a short-lived URL.

    Prefer host/uploadFile because it is independent of the selected KV
    backend. Fall back to APS Files only when host upload is unavailable.
    """
    from mail_agent.mail_providers.gmail.adapter import sanitize_mailbox_id

    filename = _safe_attachment_filename(str(attachment.get("filename") or "attachment"))
    mime_type = _normalized_attachment_mime_type(attachment)

    host_presign_started = False
    host_unavailable_error = ""
    try:
        negotiated = await host_upload.negotiate(
            filename=filename,
            mime_type=mime_type,
            size_bytes=len(content),
            purpose="user_artifact",
            metadata={
                "mailbox": mailbox,
                "card_id": card_id,
                "message_id": str(attachment.get("message_id") or ""),
                "artifact_kind": "email_attachment",
            },
            timeout=30.0,
        )
        host_presign_started = True
        put_url = str(negotiated.get("put_url") or "")
        r2_key = str(negotiated.get("r2_key") or "")
        if not put_url or not r2_key:
            raise RuntimeError("Host upload did not return a presigned upload target.")
        put_error: Exception | None = None
        try:
            await asyncio.to_thread(_put_presigned_url_sync, put_url, negotiated.get("headers") or {}, content, mime_type)
        except Exception as exc:
            put_error = exc
            log(f"host attachment presigned PUT returned error before confirm: {type(exc).__name__}: {exc}")
        if put_error is None:
            return await host_upload.confirm(r2_key=r2_key, timeout=30.0)
        try:
            return await host_upload.confirm(r2_key=r2_key, timeout=30.0)
        except Exception as confirm_exc:
            raise RuntimeError(f"Temporary attachment upload failed after presigned PUT error: {put_error}") from confirm_exc
    except Exception as host_exc:
        if host_presign_started:
            raise
        host_unavailable_error = f"{type(host_exc).__name__}: {host_exc}"
        log(f"host attachment upload unavailable: {type(host_exc).__name__}: {host_exc}")

    from mail_agent.storage.client import get_files

    files = get_files()
    for method_name in ("upload_begin", "upload_complete", "download_url"):
        if not hasattr(files, method_name):
            if host_unavailable_error:
                raise RuntimeError(
                    "Attachment download requires host upload or Anna Files storage in this runtime. "
                    f"Host upload failed first: {host_unavailable_error}"
                )
            raise RuntimeError("Attachment download requires host upload or Anna Files storage in this runtime.")

    path = (
        f"anna-inbox/mailbox/{sanitize_mailbox_id(mailbox)}/attachments/"
        f"{_safe_attachment_filename(card_id)}/{attachment.get('id')}/{filename}"
    )
    begin = await files.upload_begin(
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
    )
    put_url = str(begin.get("put_url") or begin.get("presigned_url") or "")
    if not put_url:
        raise RuntimeError("Anna Files did not return an upload URL.")
    upload_headers = begin.get("headers") or begin.get("fields") or {}
    put_error: Exception | None = None
    etag = ""
    try:
        etag = await asyncio.to_thread(_put_presigned_url_sync, put_url, upload_headers, content, mime_type)
    except Exception as exc:
        put_error = exc
        log(f"aps files presigned PUT returned error before complete: {type(exc).__name__}: {exc}")
    try:
        await files.upload_complete(
            path=path,
            etag=etag or None,
            size_bytes=len(content),
            content_type=mime_type,
            scope="user",
        )
    except Exception as complete_exc:
        if put_error is not None:
            raise RuntimeError(f"Attachment file upload failed after presigned PUT error: {put_error}") from complete_exc
        raise
    return await files.download_url(path=path, expires_in=900, scope="user")


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
        file_path = meta.get("path")
        if file_path:
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
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Expose-Headers", "Content-Disposition, Content-Type, Content-Length")

            def do_OPTIONS(self) -> None:
                self.send_response(204)
                self._send_cors_headers()
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

            def do_GET(self) -> None:
                _cleanup_expired_download_tokens()
                path = urllib.parse.urlsplit(self.path).path
                prefix = next(
                    (candidate for candidate in ("/download/", "/preview/") if path.startswith(candidate)),
                    "",
                )
                if not prefix:
                    self.send_error(404)
                    return
                token = path[len(prefix):].split("/", 1)[0].strip()
                meta = _DOWNLOAD_TOKENS.get(token)
                if not meta:
                    self.send_error(404)
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
    effective_mime = str(guessed or mime_type).lower()
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
INBOX_THREAD_RESPONSE_MAX_BYTES = 256 * 1024
INBOX_PAGE_BODY_LIMIT_STEPS = (200000, 120000, 80000, 48000, 24000, 12000, 6000, 3000, 1500, 750, 320)
INBOX_PROMPT_MESSAGE_LIMIT = 8
INBOX_PROMPT_BODY_LIMIT = 1200

THREAD_ASSIST_SYSTEM = """You are Anna's inbox thread assistant.

Return JSON only:
{
  "overview": "one short factual sentence",
  "quick_replies": [
    {"id": "short_stable_id", "label": "button text", "intent": "instruction for the assistant"}
  ]
}

Rules:
- Use the full thread context, not just the latest snippet.
- Keep overview to one line, ideally 8-12 words.
- Generate 2-3 quick_replies tailored to this exact thread.
- quick_replies labels must be short button text, 2-5 words.
- quick_replies intents must be concrete assistant instructions grounded in the thread.
- Do not invent facts or user commitments.
- If there is little to say, summarize the sender's visible intent.
- Never include HTML.
"""

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
- draft_reply and reply_gaps.needs_user_input=true are mutually exclusive.
- If the user prompt requires information that only the user would know, do not guess.
- In that case, omit draft_reply and return 1-3 specific reply_gaps questions.
- If enough information is available, return a concise plain-text draft_reply.body.
- assistant_text should briefly explain your understanding of the thread and the user's intent.
- assistant_followup_text should briefly summarize the draft strategy and invite a useful adjustment. Omit it when no reliable summary is possible.
- Do not repeat the draft body in either assistant text field.
- Do not include email headers in the draft body.
- Keep the sign-off and sender name on consecutive lines with no blank line between them.
- Do not invent dates, commitments, prices, or factual claims.
- Never include HTML.
"""

MAIL_SUMMARY_SYSTEM = """You are Anna, an executive email assistant summarizing a Gmail thread.

Return JSON only:
{
  "assistant_text": "concise factual summary of the thread"
}

Rules:
- Summarize the email conversation only.
- Do not write or offer a draft reply.
- Do not mention drafting, draft buttons, or reply artifacts.
- Include concrete participants, asks, decisions, deadlines, and current status when available.
- Do not invent dates, commitments, prices, or factual claims.
- Never include HTML.
"""


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


def _display_body_payload(message: dict[str, Any], *, limit: int, prefer_html: bool = True) -> dict[str, Any]:
    from mail_agent.actions.service import _strip_quoted_reply
    from mail_agent.mail_providers.gmail.adapter import decode_body_for_display

    display = decode_body_for_display(message)
    raw_html = str(display.get("html") or "")
    raw_text = str(display.get("text") or "")
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}

    if prefer_html and raw_html.strip():
        sanitized_html = _sanitize_email_html(_strip_quoted_reply_html(raw_html))
        sanitized_html = _resolve_cid_images(sanitized_html, payload)
        if len(sanitized_html) <= limit:
            return {
                "body_html": sanitized_html,
                "body_truncated": False,
            }

    if raw_text.strip():
        compact_text, text_truncated = _compact_body_text(_strip_quoted_reply(raw_text), limit=limit)
        return {
            "body_text": compact_text,
            "body_truncated": text_truncated,
        }

    fallback_text = _strip_quoted_reply(_dedup_body(str(message.get("body_text") or "")))
    compact_text, text_truncated = _compact_body_text(fallback_text, limit=limit)
    return {
        "body_text": compact_text,
        "body_truncated": text_truncated,
    }


def _serialize_inbox_thread_message(message: dict[str, Any], *, include_display_body: bool, body_limit: int) -> dict[str, Any]:
    from mail_agent.mail_providers.gmail.adapter import attachment_metadata_from_message

    payload = {
        "id": str(message.get("id") or ""),
        "thread_id": str(message.get("thread_id") or ""),
        "internal_date": str(message.get("internal_date") or ""),
        "from": str(message.get("from") or ""),
        "to": str(message.get("to") or ""),
        "cc": str(message.get("cc") or ""),
        "bcc": str(message.get("bcc") or ""),
        "subject": str(message.get("subject") or ""),
        "label_ids": _normalize_label_ids(message.get("label_ids")),
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
    for body_limit in INBOX_PAGE_BODY_LIMIT_STEPS:
        data = {**base, **_display_body_payload(message, limit=body_limit)}
        if _inbox_message_display_rpc_frame_size(data) <= INBOX_THREAD_RESPONSE_MAX_BYTES:
            return data
    return {
        **base,
        "body_text": "",
        "body_truncated": True,
        "error": "Message body exceeds the 256 KiB response limit.",
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


def _load_thread_messages(mailbox: str, thread_id: str) -> list[dict[str, Any]]:
    from mail_agent.mail_providers.gmail.adapter import refresh_thread_cache

    try:
        messages = refresh_thread_cache(mailbox, thread_id)
        if messages:
            messages.sort(key=_inbox_sort_key)
            return messages
    except Exception as exc:
        log(f"refresh_thread_cache failed for {thread_id}: {type(exc).__name__}: {exc}")
    return _read_cached_thread_messages(mailbox, thread_id)


def _build_inbox_thread_page(
    mailbox: str,
    thread_id: str,
    *,
    anchor_message_id: str = "",
    before_index: int | None = None,
    limit: int = INBOX_THREAD_PAGE_SIZE,
    include_display_body: bool = True,
) -> dict[str, Any]:
    messages = _visible_thread_messages(_load_thread_messages(mailbox, thread_id))
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
        "error": "Thread metadata exceeds the 256 KiB response limit.",
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


def _one_line_overview(value: Any, *, max_chars: int = 72) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars].rsplit(" ", 1)[0].strip()
    return clipped or text[:max_chars].strip()


def _fallback_thread_overview(messages: list[dict[str, Any]], anchor_message_id: str = "") -> str:
    if not messages:
        return ""
    anchor_message = _find_thread_message(messages, anchor_message_id) if anchor_message_id else None
    target = anchor_message or messages[-1]
    subject = str(target.get("subject") or messages[-1].get("subject") or "").strip()
    snippet = str(target.get("snippet") or messages[-1].get("snippet") or "").strip()
    if not snippet:
        display = _display_body_payload(target, limit=260, prefer_html=False)
        snippet = str(display.get("body_text") or "").strip()
    candidate = snippet or subject
    if subject and candidate and subject.lower() not in candidate.lower():
        candidate = f"{subject}: {candidate}"
    return _one_line_overview(candidate, max_chars=72)


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
    fallback_overview = _fallback_thread_overview(messages, anchor_message_id)
    overview_result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=THREAD_ASSIST_SYSTEM,
        user_message=(
            f"Subject: {_thread_original_subject(messages)}\n"
            f"Latest message subject: {messages[-1].get('subject', '')}\n"
            f"Participants: {'; '.join(_thread_participants(messages))}\n"
            f"Thread messages:\n{_thread_prompt_excerpt(messages)}\n"
        ),
        fallback={"overview": fallback_overview, "quick_replies": []},
        temperature=0.2,
        max_tokens=320,
        timeout=45.0,
        metadata={"tool": "inbox_thread_assist", "thread_id": thread_id},
        max_attempts=1,
    )
    payload = overview_result.get("payload") if isinstance(overview_result.get("payload"), dict) else {}
    overview = _one_line_overview(payload.get("overview") or fallback_overview)
    quick_replies = [] if overview_result.get("fallback_used") else _normalize_quick_replies(payload.get("quick_replies"))

    return {
        "thread_id": thread_id,
        "latest_message_id": latest_message_id,
        "overview": overview,
        "quick_replies": quick_replies,
        "summary": {},
        "related_context": [],
        "fallback_used": bool(overview_result.get("fallback_used")),
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

    expected_artifact = "draft_reply" if expected_artifact == "draft_reply" else "summary"
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
                f"Mailbox: {mailbox}\n"
                f"Thread ID: {thread_id}\n"
                f"Anchor message ID: {anchor_message_id}\n"
                f"Latest message ID: {latest_message_id}\n"
                f"Contact context: {contact_context_text}\n"
                f"Thread messages:\n{_thread_prompt_excerpt(messages)}\n"
            ),
            fallback={"assistant_text": "I reviewed the thread, but could not generate a detailed summary."},
            temperature=0.2,
            max_tokens=1200,
            timeout=90.0,
            metadata={"tool": "inbox_mail_summary", "thread_id": thread_id},
        )
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        return {
            "mailbox": mailbox,
            "thread_id": thread_id,
            "anchor_message_id": anchor_message_id,
            "latest_message_id": latest_message_id,
            "visible_prompt": visible_prompt,
            "thread_title": thread_title,
            "assistant_text": str(payload.get("assistant_text") or "I reviewed the thread.").strip(),
            "assistant_followup_text": "",
            "artifact": None,
            "reply_gaps": {"needs_user_input": False, "summary": "", "questions": []},
            "fallback_used": bool(result.get("fallback_used")),
        }

    answers_text = "\n".join(
        f"- {key}: {value}"
        for key, value in (user_answers or {}).items()
        if str(value).strip()
    ) or "None"
    fallback_assistant = "I drafted a reply for this thread."
    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=MAIL_PROMPT_SYSTEM,
        user_message=(
            f"Visible prompt: {visible_prompt}\n"
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


async def _handle_inbox_thread_assist_background(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    from mail_agent.storage.ops import get_inbox_thread_assist, set_inbox_thread_assist

    MAIL_AGENT_RUNS[run_id]["status"] = "running"
    _save_run_checkpoint(run_id)

    mailbox = str(arguments.get("mailbox", "")).strip()
    thread_id = str(arguments.get("thread_id", "")).strip()
    latest_message_id = str(arguments.get("latest_message_id", "")).strip()
    anchor_message_id = str(arguments.get("anchor_message_id", "")).strip()
    try:
        cached = await get_inbox_thread_assist(mailbox, thread_id, latest_message_id)
        cached_value = cached.get("value") if isinstance(cached.get("value"), dict) else {}
        cached_quick_replies = cached_value.get("quick_replies") if isinstance(cached_value, dict) else None
        if (
            cached.get("exists")
            and cached_value
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
                fetch_attachment_bytes,
                find_attachment_for_token,
                normalize_mailbox,
                read_message,
            )
            normalized_mailbox = normalize_mailbox(mailbox)
            msg = read_message(normalized_mailbox, card.message_id)
            attachment = find_attachment_for_token(msg, attachment_id)
            content = await asyncio.to_thread(
                fetch_attachment_bytes,
                normalized_mailbox,
                str(attachment.get("message_id") or card.message_id),
                str(attachment.get("gmail_attachment_id") or ""),
            )
            download_mode = _attachment_download_mode()
            if not _should_use_aps_storage():
                return _loopback_attachment_download_payload(attachment, content)
            if download_mode == "loopback":
                return _loopback_attachment_download_payload(attachment, content)
            if download_mode != "host_preferred" and len(content) <= INLINE_ATTACHMENT_DIRECT_MAX_BYTES:
                return _inline_attachment_download_payload(attachment, content)
            try:
                download = await _upload_attachment_for_download(normalized_mailbox, card_id, attachment, content)
                return {
                    "ok": True,
                    "delivery": "url",
                    "filename": attachment.get("filename") or "attachment",
                    "mime_type": attachment.get("mime_type") or "application/octet-stream",
                    "size": len(content),
                    "download_url": download.get("url") or download.get("download_url") or "",
                    "expires_at": download.get("expires_at") or "",
                }
            except Exception as upload_exc:
                if len(content) <= INLINE_ATTACHMENT_DIRECT_MAX_BYTES:
                    log(
                        "attachment download falling back to inline payload "
                        f"({len(content)} bytes): {type(upload_exc).__name__}: {upload_exc}"
                    )
                    return _inline_attachment_download_payload(attachment, content)
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
                fetch_attachment_bytes,
                find_attachment_for_token,
                normalize_mailbox,
                read_message,
            )
            normalized_mailbox = normalize_mailbox(mailbox)
            message = read_message(normalized_mailbox, message_id)
            attachment = find_attachment_for_token(message, attachment_id)
            content = await asyncio.to_thread(
                fetch_attachment_bytes,
                normalized_mailbox,
                str(attachment.get("message_id") or message_id),
                str(attachment.get("gmail_attachment_id") or ""),
            )
            if mode == "preview":
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
            if not _should_use_aps_storage() or download_mode == "loopback":
                download = _loopback_attachment_download_payload(attachment, content, disposition="attachment")
                return _finalize_attachment_access_payload(
                    download,
                    mode=mode,
                    message_id=message_id,
                    attachment_id=attachment_id,
                    attachment=attachment,
                )
            if download_mode != "host_preferred" and len(content) <= INLINE_ATTACHMENT_DIRECT_MAX_BYTES:
                return _finalize_attachment_access_payload(
                    _inline_attachment_download_payload(attachment, content),
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
                        "expires_at": uploaded.get("expires_at") or "",
                    },
                    mode=mode,
                    message_id=message_id,
                    attachment_id=attachment_id,
                    attachment=attachment,
                )
            except Exception as upload_exc:
                log(f"inbox attachment upload fallback: {type(upload_exc).__name__}: {upload_exc}")
                if len(content) <= INLINE_ATTACHMENT_DIRECT_MAX_BYTES:
                    return _finalize_attachment_access_payload(
                        _inline_attachment_download_payload(attachment, content),
                        mode=mode,
                        message_id=message_id,
                        attachment_id=attachment_id,
                        attachment=attachment,
                    )
                download = _loopback_attachment_download_payload(attachment, content, disposition="attachment")
                return _finalize_attachment_access_payload(
                    download,
                    mode=mode,
                    message_id=message_id,
                    attachment_id=attachment_id,
                    attachment=attachment,
                )
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
            if cached.get("exists") and isinstance(cached.get("value"), dict) and cached.get("value"):
                return {
                    "success": True,
                    "status": "done",
                    "result": {**cached.get("value"), "cached": True},
                }
        except Exception as exc:
            log(f"inbox thread assist cache lookup skipped: {type(exc).__name__}: {exc}")
        run_id = f"bg_{uuid.uuid4().hex[:12]}"
        MAIL_AGENT_RUNS[run_id] = {"run_id": run_id, "status": "queued", "stage": "inbox_thread_assist", "progress": {}, "warnings": [], "started_at": beijing_now(), "updated_at": beijing_now(), "result": None, "error": "", "partial": {}}
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
        run_id = f"bg_{uuid.uuid4().hex[:12]}"
        MAIL_AGENT_RUNS[run_id] = {"run_id": run_id, "status": "queued", "stage": "inbox_mail_prompt", "progress": {}, "warnings": [], "started_at": beijing_now(), "updated_at": beijing_now(), "result": None, "error": "", "partial": {}}
        _save_run_checkpoint(run_id)
        asyncio.ensure_future(_handle_inbox_mail_prompt_background(run_id, arguments, invoke_id))
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
        if not thread_id or not to_addr or not body:
            return {"error": "thread_id, to_addr, and body are required"}
        reply_mode = str(arguments.get("reply_mode", "reply_to_sender")).strip() or "reply_to_sender"
        dry_run = arguments.get("dry_run", True)
        if not isinstance(dry_run, bool):
            dry_run = True
        from mail_agent.mail_providers.gmail.adapter import send_reply
        import asyncio as _asyncio
        if dry_run:
            return {"ok": True, "dry_run": True, "message": "Mock: reply was NOT sent."}
        try:
            result = await _asyncio.to_thread(send_reply, mailbox, thread_id, to_addr, body, reply_mode=reply_mode)
            from mail_agent.storage.ops import append_card_action
            await append_card_action(mailbox, "", thread_id, "reply_from_ask", body[:80])
            return {"ok": True, "dry_run": False, "result": result}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

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
        return {
            "mailbox": mailbox,
            "thread_id": thread_id,
            "exists": bool(draft.get("exists")),
            "etag": str(draft.get("etag") or ""),
            "body": str(value.get("body") or ""),
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
        if_match = str(arguments.get("if_match", "")).strip() or None
        if not mailbox or not thread_id:
            return {"error": "mailbox and thread_id are required"}
        message = arguments.get("message") if isinstance(arguments.get("message"), dict) else {}
        from mail_agent.storage.ops import set_inbox_thread_draft
        result = await set_inbox_thread_draft(
            mailbox,
            thread_id,
            body,
            if_match=if_match,
            message=message,
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
        from mail_agent.storage.ops import delete_inbox_thread_draft
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
