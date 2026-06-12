from __future__ import annotations

from anna_inbox_executa.common import *

def repo_root() -> Path:
    # main.py lives at inbox-tool/src/anna_inbox_executa/main.py.
    return Path(__file__).resolve().parents[3]


def tool_root() -> Path:
    return Path(__file__).resolve().parents[1]


def sanitize_mailbox_id(mailbox: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in mailbox.strip())
    return safe.strip("._") or "default"


def normalize_mailbox(mailbox: str) -> str:
    raw = str(mailbox or "").strip().lower()
    if "@" in raw and "." in raw.split("@")[-1]:
        return raw
    raise ValueError(f"Unsupported mailbox: {mailbox}")


def token_dir() -> Path:
    override = os.environ.get("ANNA_INBOX_TOKEN_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return repo_root() / "scripts" / "google_token" / ".secrets" / "gmail_tokens"


def load_local_token_record(mailbox: str) -> dict[str, Any]:
    candidates = [
        token_dir() / f"{sanitize_mailbox_id(mailbox)}.json",
        token_dir() / "default.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            record["_token_file"] = str(path)
            return record
    raise ValueError(f"Local Gmail token file not found for {mailbox}")


def get_access_token(mailbox: str) -> str:
    record = load_local_token_record(mailbox)
    if should_refresh_token(record):
        refresh_access_token(record)
    token = record.get("access_token")
    if not token:
        raise ValueError(f"Local Gmail access token is missing for {mailbox}")
    return str(token)


def should_refresh_token(record: dict[str, Any]) -> bool:
    if not record.get("refresh_token"):
        return False
    try:
        return float(record.get("expires_at") or 0) <= time.time() + 60
    except (TypeError, ValueError):
        return False


def refresh_access_token(record: dict[str, Any]) -> None:
    client_id = record.get("client_id")
    client_secret = record.get("client_secret")
    refresh_token = record.get("refresh_token")
    if not client_id or not client_secret or not refresh_token:
        raise ValueError("Gmail refresh token is missing client metadata")
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    request = urllib.request.Request(TOKEN_URI, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    record["access_token"] = payload["access_token"]
    if "expires_in" in payload:
        record["expires_at"] = int(time.time()) + int(payload["expires_in"])
    record["updated_at"] = beijing_now()
    token_file = record.get("_token_file")
    if token_file:
        clean_record = {key: value for key, value in record.items() if not key.startswith("_")}
        Path(str(token_file)).write_text(json.dumps(clean_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def gmail_request(mailbox: str, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
    token = get_access_token(mailbox)
    url = GMAIL_API_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = read_http_error(exc)
        raise ValueError(f"Gmail API request failed: {exc.code} {details}") from exc
    return json.loads(raw) if raw else {}


def read_http_error(exc: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        raw = exc.read().decode("utf-8")
        return json.loads(raw) if raw else {"status_code": exc.code}
    except Exception:
        return {"status_code": exc.code}


def header_map(message: dict[str, Any]) -> dict[str, str]:
    headers = ((message.get("payload") or {}).get("headers") or [])
    result: dict[str, str] = {}
    for header in headers:
        if isinstance(header, dict) and header.get("name"):
            result[str(header["name"]).lower()] = str(header.get("value") or "")
    return result


def decode_gmail_body(message: dict[str, Any]) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType") or "")
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        data = body.get("data")
        if data and mime_type in {"text/plain", "text/html"}:
            try:
                decoded = base64.urlsafe_b64decode(str(data) + "=" * (-len(str(data)) % 4)).decode("utf-8", errors="replace")
                if mime_type == "text/plain":
                    plain_parts.append(decoded)
                else:
                    html_parts.append(decoded)
            except Exception:
                return
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    walk(payload)

    # Prefer text/html — modern email uses it as the canonical format.
    # Callers are responsible for stripping HTML tags as needed.
    parts = html_parts or plain_parts
    return "\n\n".join(part.strip() for part in parts if part.strip())[:30000]


def _dedup_body(text: str) -> str:
    """Remove duplicated adjacent segments in body text.

    The old decode_gmail_body joined both text/plain and text/html parts
    with ``\\n\\n``, so multipart/alternative emails contain the same
    content twice — once as plain text, once as HTML.  This compares
    segments after stripping HTML tags and collapsing whitespace.
    """
    if not text or "\n\n" not in text:
        return text
    import re
    parts = text.split("\n\n")

    def _normalize(s: str) -> str:
        stripped = re.sub(r"<[^>]+>", "", s)
        stripped = re.sub(r"&nbsp;", " ", stripped)
        stripped = re.sub(r"&amp;", "&", stripped)
        stripped = re.sub(r"&lt;", "<", stripped)
        stripped = re.sub(r"&gt;", ">", stripped)
        stripped = re.sub(r"&quot;", '"', stripped)
        return " ".join(stripped.split())

    result = [parts[0]]
    for i in range(1, len(parts)):
        if _normalize(result[-1]) != _normalize(parts[i]):
            result.append(parts[i])
    return "\n\n".join(result)


def _inject_link_attrs(tag_str: str) -> str:
    """Add target='_blank' rel='noopener noreferrer' to <a> if missing."""
    result = tag_str
    if 'target=' not in result:
        result = result[:-1] + ' target="_blank">' if result.endswith('>') else result + ' target="_blank">'
    if 'rel=' not in result:
        result = result[:-1] + ' rel="noopener noreferrer">' if result.endswith('>') else result + ' rel="noopener noreferrer">'
    return result


def _inject_img_attrs(tag_str: str) -> str:
    """Add loading='lazy' referrerpolicy='no-referrer' to <img> if missing."""
    result = tag_str
    if 'loading=' not in result:
        result = result + ' loading="lazy"'
    if 'referrerpolicy=' not in result:
        result = result + ' referrerpolicy="no-referrer"'
    return result


def _sanitize_email_html(html_text: str) -> str:
    """Strip XSS vectors from email HTML. Keeps images, links, formatting."""
    import re
    html_text = re.sub(
        r"<script[^>]*>.*?</script>",
        "", html_text, flags=re.DOTALL | re.IGNORECASE,
    )
    html_text = re.sub(
        r"<style[^>]*>.*?</style>",
        "", html_text, flags=re.DOTALL | re.IGNORECASE,
    )

    # Strip event-handler attributes (onclick, onerror, onload, etc.)
    html_text = re.sub(
        r'\s+on\w+\s*=\s*"[^"]*"',
        "", html_text, flags=re.IGNORECASE,
    )
    html_text = re.sub(
        r"\s+on\w+\s*=\s*'[^']*'",
        "", html_text, flags=re.IGNORECASE,
    )
    html_text = re.sub(
        r'\s+on\w+\s*=\s*\S+',
        "", html_text, flags=re.IGNORECASE,
    )

    # Remove javascript: / vbscript: URL schemes — delete the entire attribute
    html_text = re.sub(
        r'\s*(?:href|src|action|formaction)\s*=\s*["\'][^"\']*javascript\s*:[^"\']*["\']',
        "", html_text, flags=re.IGNORECASE,
    )
    html_text = re.sub(
        r'\s*(?:href|src|action|formaction)\s*=\s*["\'][^"\']*vbscript\s*:[^"\']*["\']',
        "", html_text, flags=re.IGNORECASE,
    )

    # Remove <iframe>, <object>, <embed> tags
    html_text = re.sub(
        r"<iframe[^>]*>.*?</iframe>",
        "", html_text, flags=re.DOTALL | re.IGNORECASE,
    )
    html_text = re.sub(
        r"<object[^>]*>.*?</object>",
        "", html_text, flags=re.DOTALL | re.IGNORECASE,
    )
    html_text = re.sub(
        r"<embed[^>]*>.*?</embed>",
        "", html_text, flags=re.DOTALL | re.IGNORECASE,
    )

    # Add target="_blank" rel="noopener noreferrer" to <a> tags that have href
    html_text = re.sub(
        r'(<a\b[^>]*href\s*=\s*["\'][^"\']+["\'][^>]*)>',
        lambda m: _inject_link_attrs(m.group(1)),
        html_text, flags=re.IGNORECASE,
    )

    # Add loading="lazy" referrerpolicy="no-referrer" to <img> tags.
    # Lazy match stops before the optional self-closing slash so we
    # don't inject attributes after it.
    html_text = re.sub(
        r'(<img\b[^>]*?)\s*/?\s*>',
        lambda m: _inject_img_attrs(m.group(1)) + '>',
        html_text, flags=re.IGNORECASE,
    )

    return html_text


def _build_cid_map(payload: dict[str, Any]) -> dict[str, str]:
    """Walk MIME parts for inline images with Content-ID, return {cid: data_uri}."""
    cid_map: dict[str, str] = {}

    def walk(part: dict[str, Any]) -> None:
        headers = part.get("headers") or []
        cid: str | None = None
        for h in headers:
            if isinstance(h, dict) and str(h.get("name") or "").lower() == "content-id":
                cid = str(h.get("value") or "").strip().strip("<>")
                break
        if cid:
            mime_type = str(part.get("mimeType") or "image/png")
            if not mime_type.startswith("image/"):
                return
            body = part.get("body") if isinstance(part.get("body"), dict) else {}
            data = body.get("data")
            if data:
                try:
                    import base64
                    raw_bytes = base64.urlsafe_b64decode(str(data) + "=" * (-len(str(data)) % 4))
                    b64 = base64.b64encode(raw_bytes).decode("ascii")
                    cid_map[cid] = f"data:{mime_type};base64,{b64}"
                except Exception:
                    pass
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(payload)
    return cid_map


def _resolve_cid_images(html_text: str, payload: dict[str, Any]) -> str:
    """Replace cid: references in <img src> with inline data URIs."""
    cid_map = _build_cid_map(payload)
    if not cid_map:
        return html_text
    import re

    def _replace(m: re.Match[str]) -> str:
        cid = m.group(1)
        # Strip optional @host suffix (cid:xxx@host)
        cid_key = cid.split("@")[0] if "@" in cid else cid
        uri = cid_map.get(cid_key) or cid_map.get(cid)
        if uri:
            return f'src="{uri}"'
        return m.group(0)

    # Match src="cid:..." or src='cid:...'
    html_text = re.sub(
        r'''src\s*=\s*["']cid:([^"'\s]+)["']''',
        _replace, html_text, flags=re.IGNORECASE,
    )
    return html_text


def _inline_remote_images(
    html_text: str,
    *,
    max_images: int = 8,
    max_image_bytes: int = 96 * 1024,
    max_total_bytes: int = 220 * 1024,
    timeout: float = 3.0,
) -> str:
    """把远程邮件图片转成 data URI，适配 Anna 宿主的图片 CSP。"""
    if not html_text:
        return html_text

    import base64
    import re
    import urllib.parse
    import urllib.request

    transparent_gif = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    inlined_count = 0
    total_bytes = 0
    cache: dict[str, str] = {}

    def _fetch_image(url: str) -> str:
        nonlocal inlined_count, total_bytes
        if url in cache:
            return cache[url]
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme.lower() not in ("http", "https"):
            cache[url] = url
            return url
        if inlined_count >= max_images or total_bytes >= max_total_bytes:
            cache[url] = transparent_gif
            return transparent_gif
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 AnnaInbox/1.0",
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                },
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > max_image_bytes:
                    cache[url] = transparent_gif
                    return transparent_gif
                data = response.read(max_image_bytes + 1)
            if len(data) > max_image_bytes or total_bytes + len(data) > max_total_bytes:
                cache[url] = transparent_gif
                return transparent_gif
            if not content_type.startswith("image/"):
                content_type = "image/png"
            total_bytes += len(data)
            inlined_count += 1
            cache[url] = f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"
            return cache[url]
        except Exception:
            cache[url] = transparent_gif
            return transparent_gif

    def _replace_src(match: re.Match[str]) -> str:
        prefix = match.group(1)
        quote = match.group(2)
        src = match.group(3).strip()
        if src.startswith(("data:", "blob:", "cid:")):
            return match.group(0)
        return f"{prefix}{quote}{_fetch_image(src)}{quote}"

    # 只处理 img 的 src，避免改动链接 href。
    return re.sub(
        r"(<img\b[^>]*?\bsrc\s*=\s*)([\"'])([^\"']+)\2",
        _replace_src,
        html_text,
        flags=re.IGNORECASE,
    )


def extract_attachments(part: dict[str, Any]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        filename = str(node.get("filename") or "")
        body = node.get("body") if isinstance(node.get("body"), dict) else {}
        if filename:
            attachments.append({
                "filename": filename,
                "mimeType": node.get("mimeType"),
                "size": body.get("size"),
                "attachmentId": body.get("attachmentId"),
            })
        for child in node.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(part)
    return attachments


def normalize_message(mailbox: str, message: dict[str, Any]) -> dict[str, Any]:
    headers = header_map(message)
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "mailbox": mailbox,
        "history_id": message.get("historyId"),
        "internal_date": message.get("internalDate"),
        "date": headers.get("date", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "bcc": headers.get("bcc", ""),
        "subject": headers.get("subject", ""),
        "message_id": headers.get("message-id", ""),
        "in_reply_to": headers.get("in-reply-to", ""),
        "references": headers.get("references", ""),
        "label_ids": message.get("labelIds") or [],
        "snippet": message.get("snippet") or "",
        "size_estimate": message.get("sizeEstimate"),
        "mime_type": payload.get("mimeType"),
        "attachments": extract_attachments(payload),
        "body_text": decode_gmail_body(message),
        "raw_headers": headers,
        "fetched_at": beijing_now(),
    }


def read_primary_emails(mailbox_arg: str, limit_arg: Any) -> dict[str, Any]:
    from mail_agent.mail_providers.gmail.adapter import cache_debug_info, live_search_and_cache, list_messages, normalize_mailbox as adapter_normalize_mailbox

    mailbox = adapter_normalize_mailbox(mailbox_arg)
    limit = max(1, min(int(limit_arg or 5), 20))
    matched_ids = live_search_and_cache(mailbox, "category:primary", limit)
    merged = sorted(list_messages(mailbox), key=lambda item: int(item.get("internal_date") or 0), reverse=True)
    fetched_summaries = [item for item in merged if str(item.get("id") or "") in set(matched_ids)]
    return {
        "mailbox": mailbox,
        "requested": limit,
        "fetched_count": len(fetched_summaries),
        "matched_ids": len(matched_ids),
        "cached_count": len(merged),
        "cache": cache_debug_info(mailbox),
        "messages": fetched_summaries,
        "updated_at": beijing_now(),
    }


def list_cached_emails(mailbox_arg: str) -> dict[str, Any]:
    from mail_agent.mail_providers.gmail.adapter import cache_debug_info, list_messages, read_cache as adapter_read_cache, normalize_mailbox as adapter_normalize_mailbox

    mailbox = adapter_normalize_mailbox(mailbox_arg)
    cached = adapter_read_cache(mailbox)
    messages = sorted(list_messages(mailbox), key=lambda item: int(item.get("internal_date") or 0), reverse=True)
    return {
        "mailbox": mailbox,
        "cache": cache_debug_info(mailbox),
        "updated_at": cached.get("updated_at"),
        "count": len(messages),
        "messages": messages,
    }


def get_cached_email(mailbox_arg: str, message_id: str) -> dict[str, Any]:
    from mail_agent.mail_providers.gmail.adapter import cache_debug_info, read_message as adapter_read_message, normalize_mailbox as adapter_normalize_mailbox

    mailbox = adapter_normalize_mailbox(mailbox_arg)
    msg_id = str(message_id or "")
    message = adapter_read_message(mailbox, msg_id)
    return {
        "mailbox": mailbox,
        "cache": cache_debug_info(mailbox),
        "message": message,
    }



def _check_gmail_auth(mailbox: str) -> dict[str, Any]:
    """Check Gmail authorization status.

    Platform: GMAIL_ACCESS_TOKEN or GOOGLE_ACCESS_TOKEN env var is set.
    Multi-token: checks _multi_token_map.
    Local dev: token file exists on disk (content/expiry not validated).
    """
    import os as _os
    from mail_agent.mail_providers.gmail.adapter import _token_dir, sanitize_mailbox_id, get_authorized_email, get_multi_token_map
    from pathlib import Path as _Path

    requested = str(mailbox or "").strip().lower()

    # 系统级判断：mailbox 为空时，只看 token 有没有，不关心具体邮箱
    if not requested:
        platform_token = _os.environ.get("GMAIL_ACCESS_TOKEN") or _os.environ.get("GOOGLE_ACCESS_TOKEN")
        if platform_token and platform_token.strip():
            try:
                authorized_email = get_authorized_email().strip().lower()
            except Exception:
                authorized_email = ""
            if authorized_email:
                return {"authorized": True, "source": "platform", "authorized_email": authorized_email, "mode": "any"}
        multi = get_multi_token_map()
        if multi:
            return {"authorized": True, "source": "platform_multi", "authorized_email": list(multi.keys())[0] if multi else "", "mode": "any"}
        token_dir = _token_dir()
        if token_dir.exists():
            for p in token_dir.glob("*.json"):
                if p.name != "default.json":
                    return {"authorized": True, "source": "local_file", "mode": "any"}
        return {"authorized": False, "source": "none", "mode": "any"}

    # Platform path — check for injected OAuth credential
    platform_token = _os.environ.get("GMAIL_ACCESS_TOKEN") or _os.environ.get("GOOGLE_ACCESS_TOKEN")
    if platform_token and platform_token.strip():
        authorized_email = get_authorized_email().strip().lower()
        authorized = bool(authorized_email and requested == authorized_email)
        return {
            "authorized": authorized,
            "source": "platform",
            "authorized_email": authorized_email,
        }

    # Multi-token path — check in-memory map
    if requested in get_multi_token_map():
        return {"authorized": True, "source": "platform_multi", "authorized_email": requested}

    # Local dev path — check for token file existence only
    token_dir = _token_dir()
    candidates = [
        token_dir / f"{sanitize_mailbox_id(mailbox)}.json",
        token_dir / "default.json",
    ]
    for path in candidates:
        if _Path(path).exists():
            return {"authorized": True, "source": "local_file", "token_file": str(path)}

    return {"authorized": False, "source": "none"}

__all__ = [name for name in globals() if not name.startswith("__")]
