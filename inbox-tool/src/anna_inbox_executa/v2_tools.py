from __future__ import annotations

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
    mime_type = str(attachment.get("mime_type") or "application/octet-stream")

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
    return {
        "ok": True,
        "delivery": "inline",
        "filename": attachment.get("filename") or "attachment",
        "mime_type": attachment.get("mime_type") or "application/octet-stream",
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

            def do_GET(self) -> None:
                _cleanup_expired_download_tokens()
                prefix = "/download/"
                if not self.path.startswith(prefix):
                    self.send_error(404)
                    return
                token = self.path[len(prefix):].split("/", 1)[0].strip()
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
                data = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
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


def _loopback_attachment_download_payload(attachment: dict[str, Any], content: bytes) -> dict[str, Any]:
    _cleanup_expired_download_tokens()
    filename = _safe_attachment_filename(str(attachment.get("filename") or "attachment"))
    mime_type = str(attachment.get("mime_type") or "application/octet-stream")
    token = uuid.uuid4().hex
    file_path = _attachment_download_dir() / f"{token}-{filename}"
    file_path.write_bytes(content)
    expires_at_ts = time.time() + _DOWNLOAD_TOKEN_TTL_SECONDS
    _DOWNLOAD_TOKENS[token] = {
        "path": str(file_path),
        "filename": filename,
        "mime_type": mime_type,
        "expires_at_ts": expires_at_ts,
    }
    base_url = _ensure_loopback_download_server()
    return {
        "ok": True,
        "delivery": "url",
        "filename": filename,
        "mime_type": mime_type,
        "size": len(content),
        "download_url": f"{base_url}/download/{token}/{urllib.parse.quote(filename, safe='')}",
        "expires_at": datetime.fromtimestamp(expires_at_ts, tz=timezone.utc).isoformat(),
    }


def _clamp_int(value: Any, fallback: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return min(max_value, max(min_value, parsed))


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

    if tool == "get_card_detail":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        include_body = bool(arguments.get("include_body"))
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            return {"error": f"Card {card_id} not found"}
        thread_ctx = await asyncio.to_thread(_fetch_thread_context_sync, mailbox, card)

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
                from mail_agent.mail_providers.gmail.adapter import decode_body_for_display, normalize_mailbox, read_message
                msg = cached_msg if isinstance(cached_msg, dict) else read_message(normalize_mailbox(mailbox), card.message_id)
                if isinstance(msg, dict):
                    display_body = decode_body_for_display(msg)
                    raw_html = str(display_body.get("html") or "")
                    raw_text = str(display_body.get("text") or "")
                    payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
                    if raw_html.strip():
                        latest_body_html = _sanitize_email_html(raw_html[:8000])
                        latest_body_html = _resolve_cid_images(latest_body_html, payload)
                    if raw_text.strip():
                        latest_body = raw_text[:8000]
                    elif not latest_body_html:
                        latest_body = _dedup_body(str(msg.get("body_text") or card.original.body or ""))[:8000]
            except Exception:
                latest_body = _dedup_body(str(card.original.body or ""))[:8000]

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
