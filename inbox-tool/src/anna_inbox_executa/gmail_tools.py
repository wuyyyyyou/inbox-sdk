from __future__ import annotations

import sys
from contextvars import copy_context

from anna_inbox_executa.common import *

def repo_root() -> Path:
    # main.py lives at inbox-tool/src/anna_inbox_executa/main.py.
    return Path(__file__).resolve().parents[3]


def tool_root() -> Path:
    return Path(__file__).resolve().parents[1]


HOME_FEED_SNIPPET_MAX_CHARS = 120
HOME_FEED_BODY_PREVIEW_MAX_CHARS = 120
CACHED_FEED_RESPONSE_MAX_BYTES = 48 * 1024
CACHED_EMAIL_BODY_MAX_CHARS = 24_000


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


def _thread_original_subjects(messages: list[dict[str, Any]]) -> dict[str, str]:
    def sort_key(item: dict[str, Any]) -> int:
        try:
            return int(item.get("internal_date") or 0)
        except (TypeError, ValueError):
            return 0

    subjects: dict[str, str] = {}
    for item in sorted(messages, key=sort_key):
        thread_id = str(item.get("thread_id") or "").strip()
        subject = str(item.get("subject") or "").strip()
        if thread_id and subject and thread_id not in subjects:
            subjects[thread_id] = subject
    return subjects


def _compact_inbox_message(item: dict[str, Any], mailbox: str, thread_subjects: dict[str, str] | None = None) -> dict[str, Any]:
    labels = [str(label)[:80] for label in (item.get("label_ids") or [])][:32]
    attachments = item.get("attachments") if isinstance(item.get("attachments"), list) else []
    try:
        stored_attachment_count = int(item.get("attachment_count") or 0)
    except (TypeError, ValueError):
        stored_attachment_count = 0
    attachment_count = max(len(attachments), stored_attachment_count)
    has_attachment = bool(attachments) or bool(item.get("has_attachment")) or attachment_count > 0
    latest_subject = str(item.get("subject") or "").strip()
    thread_id = str(item.get("thread_id") or "").strip()
    subject = str((thread_subjects or {}).get(thread_id) or item.get("original_subject") or latest_subject)
    return {
        "id": str(item.get("id") or "")[:128],
        "thread_id": thread_id[:128],
        "mailbox": mailbox,
        "internal_date": str(item.get("internal_date") or "")[:32],
        "date": str(item.get("date") or "")[:128],
        "from": str(item.get("from") or "")[:512],
        "to": str(item.get("to") or "")[:512],
        "subject": subject[:512],
        "latest_subject": latest_subject[:512],
        "snippet": str(item.get("snippet") or "")[:HOME_FEED_SNIPPET_MAX_CHARS],
        "body_preview": str(item.get("body_preview") or "")[:HOME_FEED_BODY_PREVIEW_MAX_CHARS],
        "label_ids": labels,
        "unread": "UNREAD" in labels,
        "important": "IMPORTANT" in labels,
        "starred": "STARRED" in labels,
        "has_attachment": has_attachment,
        "attachment_count": attachment_count,
        "body_cached": bool(item.get("body_cached")),
    }


def _inbox_message_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    try:
        internal_date = int(item.get("internal_date") or 0)
    except (TypeError, ValueError):
        internal_date = 0
    return (-internal_date, str(item.get("id") or ""))


def _cached_rpc_frame_size(tool: str, data: dict[str, Any]) -> int:
    frame = {
        "jsonrpc": "2.0",
        "id": "x" * 128,
        "result": {"success": True, "tool": tool, "data": data},
    }
    return len(json.dumps(frame, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))


def _compact_cached_email_detail(item: dict[str, Any], mailbox: str) -> dict[str, Any]:
    body = str(item.get("body_text") or item.get("body_preview") or item.get("snippet") or "")
    body_truncated = len(body) > CACHED_EMAIL_BODY_MAX_CHARS
    return {
        "id": str(item.get("id") or "")[:128],
        "thread_id": str(item.get("thread_id") or "")[:128],
        "mailbox": mailbox,
        "internal_date": str(item.get("internal_date") or "")[:32],
        "date": str(item.get("date") or "")[:128],
        "from": str(item.get("from") or "")[:512],
        "to": str(item.get("to") or "")[:512],
        "subject": str(item.get("subject") or "")[:512],
        "label_ids": [str(label)[:80] for label in (item.get("label_ids") or [])][:32],
        "snippet": str(item.get("snippet") or "")[:HOME_FEED_SNIPPET_MAX_CHARS],
        "body_preview": str(item.get("body_preview") or "")[:HOME_FEED_BODY_PREVIEW_MAX_CHARS],
        "body_text": body[:CACHED_EMAIL_BODY_MAX_CHARS],
        "body_truncated": body_truncated,
    }


def _normalize_inbox_category(category_arg: Any) -> str:
    category = str(category_arg or "all").strip().lower()
    category_queries = {
        "inbox": "in:inbox",
        "todos": "in:inbox",
        "snoozed": "in:inbox",
        "done": "in:inbox",
        "starred": "is:starred",
        "drafts": "in:drafts",
        "sent": "in:sent",
        "trash": "in:trash",
        "spam": "in:spam",
        "all": "in:anywhere -in:chats",
    }
    if category not in category_queries:
        return "all"
    return category


def _inbox_category_base_query(category: str) -> str:
    category_queries = {
        "inbox": "in:inbox",
        "todos": "in:inbox",
        "snoozed": "in:inbox",
        "done": "in:inbox",
        "starred": "is:starred",
        "drafts": "in:drafts",
        "sent": "in:sent",
        "trash": "in:trash",
        "spam": "in:spam",
        "all": "in:anywhere -in:chats",
    }
    return category_queries[category]


def _inbox_category_query(category: str, days: int) -> str:
    if days <= 0:
        return _inbox_category_base_query(category)
    return f"{_inbox_category_base_query(category)} newer_than:{days}d"


def _gmail_fallback_query(category: str, days: int) -> str:
    base_query = _inbox_category_base_query(category)
    if category == "inbox" and days > 0:
        return f"{base_query} newer_than:{days}d"
    return base_query


def _matches_cached_category(item: dict[str, Any], category: str) -> bool:
    labels = {str(label).upper() for label in (item.get("label_ids") or [])}
    if category in {"all", "todos", "snoozed", "done"}:
        return True
    if category == "inbox":
        return "INBOX" in labels
    if category == "starred":
        return "STARRED" in labels
    if category == "drafts":
        return "DRAFT" in labels
    if category == "sent":
        return "SENT" in labels
    if category == "trash":
        return "TRASH" in labels
    if category == "spam":
        return "SPAM" in labels
    return True


def list_inbox_emails(
    mailbox_arg: str,
    days_arg: Any = 30,
    limit_arg: Any = 100,
    category_arg: Any = "inbox",
    clear_cache_arg: Any = False,
) -> dict[str, Any]:
    """刷新/拉取首页邮件：始终按 All mail 写入统一本地缓存，再返回首屏快照。

    策略（与前端分类投影对齐）：
    1. clear_cache=True 时先清空该邮箱 Gmail 缓存
    2. 固定用 All mail query（含 trash/spam、排除 chats）从 Gmail 拉 metadata 并写入缓存
    3. 缓存写入可用较高 limit；**RPC 响应**按 CACHED_FEED_RESPONSE_MAX_BYTES 截断，
       避免单帧过大导致 host 杀进程（executa process exited）
    4. 响应带 has_more / next_offset，前端用 list_cached_emails 继续读本地缓存
    5. category 参数仅作诊断字段保留，不再驱动 Gmail query
    """
    from mail_agent.mail_providers.gmail.adapter import (
        clear_mailbox_cache,
        live_search_metadata_and_cache,
        list_messages,
        gmail_request,
        normalize_mailbox as adapter_normalize_mailbox,
        set_cached_mailbox_history_cursor,
    )

    mailbox = adapter_normalize_mailbox(mailbox_arg)
    # 身份校验沿用原有 Gmail 请求入口；后续摘要批次会在 adapter 内只取一次
    # 短期 token。这样首页刷新最多两次凭据反向 RPC，而非每封摘要各取一次。
    profile = gmail_request(mailbox, "/users/me/profile", {"fields": "emailAddress,historyId"})
    authorized_email = str(profile.get("emailAddress") or "").strip().lower()
    if authorized_email and authorized_email != mailbox:
        raise ValueError(f"Gmail credential mismatch: selected {mailbox}, authorized {authorized_email}")
    # 用户强制刷新时清空缓存，再重建 All mail 快照
    cache_reset = clear_mailbox_cache(mailbox) if clear_cache_arg is True else None
    days_input = 30 if days_arg in (None, "") else days_arg
    days = max(0, min(int(days_input), 3650))
    # 缓存抓取上限；响应条数另受 48KiB 帧预算约束
    limit = max(1, min(int(limit_arg or 100), 500))
    # 请求侧 category 仅记录意图；实际抓取固定为 all
    category = _normalize_inbox_category(category_arg)
    query = _inbox_category_query("all", days)
    matched_ids = live_search_metadata_and_cache(mailbox, query, limit)
    # All-mail 快照已写入缓存后才记录 cursor；快照写入失败不能把未建立的基线
    # 伪装为可增量同步状态。
    set_cached_mailbox_history_cursor(mailbox, str(profile.get("historyId") or ""), scope_days=days)
    by_id = {
        str(item.get("id") or ""): item
        for item in list_messages(mailbox)
        if isinstance(item, dict) and item.get("id")
    }
    thread_subjects = _thread_original_subjects(list(by_id.values()))

    all_messages: list[dict[str, Any]] = []
    for message_id in matched_ids:
        item = by_id.get(str(message_id))
        if not item:
            continue
        all_messages.append(_compact_inbox_message(item, mailbox, thread_subjects))
    all_messages.sort(key=_inbox_message_sort_key)

    # 按 JSON-RPC 帧预算截断返回条数（与 list_cached_emails 一致），缓存仍保留全量
    messages: list[dict[str, Any]] = []
    response_limit = min(limit, 100)
    for item in all_messages:
        if len(messages) >= response_limit:
            break
        candidate_messages = [*messages, item]
        candidate_payload = {
            "mailbox": mailbox,
            "days": days,
            "category": category,
            "query": query,
            "count": len(candidate_messages),
            "offset": 0,
            "next_offset": len(candidate_messages),
            "has_more": len(all_messages) > len(candidate_messages),
            "messages": candidate_messages,
            "updated_at": beijing_now(),
            "cache_reset": cache_reset,
        }
        if messages and _cached_rpc_frame_size("list_inbox_emails", candidate_payload) > CACHED_FEED_RESPONSE_MAX_BYTES:
            break
        messages = candidate_messages

    next_offset = len(messages)
    has_more = len(all_messages) > next_offset
    return {
        "mailbox": mailbox,
        "days": days,
        "category": category,
        "query": query,
        "count": len(messages),
        "offset": 0,
        "next_offset": next_offset,
        "has_more": has_more,
        "cached_total": len(all_messages),
        "messages": messages,
        "updated_at": beijing_now(),
        "cache_reset": cache_reset,
    }


def resolve_contact_avatars(mailbox_arg: str, emails_arg: Any) -> dict[str, Any]:
    from mail_agent.mail_providers.gmail.adapter import normalize_mailbox as adapter_normalize_mailbox, resolve_contact_avatar_urls

    mailbox = adapter_normalize_mailbox(mailbox_arg)
    emails = [str(item) for item in emails_arg] if isinstance(emails_arg, list) else []
    return {"mailbox": mailbox, **resolve_contact_avatar_urls(mailbox, emails)}


def list_cached_emails(
    mailbox_arg: str,
    days_arg: Any = 30,
    limit_arg: Any = 100,
    category_arg: Any = "all",
    offset_arg: Any = 0,
) -> dict[str, Any]:
    """从本地 All mail 缓存分页读取，并按 category 标签投影过滤。

    不访问 Gmail；依赖 list_inbox_emails / list_gmail_emails_page 事先写入的统一缓存。
    """
    from mail_agent.mail_providers.gmail.adapter import (
        cache_debug_info,
        ensure_cached_feed_index,
        get_cached_feed_page,
        normalize_mailbox as adapter_normalize_mailbox,
    )

    mailbox = adapter_normalize_mailbox(mailbox_arg)
    days_input = 30 if days_arg in (None, "") else days_arg
    days = max(0, min(int(days_input), 3650))
    limit = max(1, min(int(limit_arg or 100), 100))
    offset = max(0, int(offset_arg or 0))
    category = _normalize_inbox_category(category_arg)
    started_at = time.perf_counter()
    feed_meta = ensure_cached_feed_index(mailbox)
    page_descriptors = feed_meta.get("pages") if isinstance(feed_meta.get("pages"), list) else []
    total_cached_messages = int(feed_meta.get("message_count") or 0)
    cutoff = int((time.time() - days * 24 * 60 * 60) * 1000) if days > 0 else 0
    cache_info = cache_debug_info(mailbox)
    updated_at = feed_meta.get("updated_at")
    messages: list[dict[str, Any]] = []
    matched_total = 0
    skipped = 0
    stop_after_page = False
    for descriptor in page_descriptors:
        page_number = int(descriptor.get("page") or 0)
        oldest_internal_date = str(descriptor.get("oldest_internal_date") or "")
        if stop_after_page:
            break
        if oldest_internal_date:
            try:
                if int(oldest_internal_date) < cutoff:
                    stop_after_page = True
            except (TypeError, ValueError):
                stop_after_page = False
        page_payload = get_cached_feed_page(mailbox, page_number)
        page_messages = page_payload.get("messages") if isinstance(page_payload.get("messages"), list) else []
        for item in page_messages:
            try:
                internal_date = int(item.get("internal_date") or 0)
            except (TypeError, ValueError):
                internal_date = 0
            if cutoff and internal_date < cutoff:
                continue
            if not _matches_cached_category(item, category):
                continue
            matched_total += 1
            if skipped < offset:
                skipped += 1
                continue
            if len(messages) >= limit:
                continue
            candidate_messages = [*messages, item]
            candidate_next_offset = offset + len(candidate_messages)
            candidate_payload = {
                "mailbox": mailbox,
                "days": days,
                "category": category,
                "cache": cache_info,
                "updated_at": updated_at,
                "count": len(candidate_messages),
                "offset": offset,
                "next_offset": candidate_next_offset,
                "has_more": matched_total > candidate_next_offset,
                "messages": candidate_messages,
            }
            if messages and _cached_rpc_frame_size("list_cached_emails", candidate_payload) > CACHED_FEED_RESPONSE_MAX_BYTES:
                break
            messages = candidate_messages
        if len(messages) >= limit:
            continue
    next_offset = offset + len(messages)
    has_more = matched_total > next_offset
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
    print(
        json.dumps(
            {
                "scope": "gmail_feed_cache",
                "mailbox": mailbox,
                "days": days,
                "limit": limit,
                "offset": offset,
                "category": category,
                "cache_messages": total_cached_messages,
                "page_count": len(page_descriptors),
                "matched_messages": matched_total,
                "returned_messages": len(messages),
                "elapsed_ms": elapsed_ms,
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    return {
        "mailbox": mailbox,
        "days": days,
        "category": category,
        "cache": cache_info,
        "updated_at": updated_at,
        "count": len(messages),
        "offset": offset,
        "next_offset": next_offset,
        "has_more": has_more,
        "messages": messages,
    }


def sync_inbox_cache(mailbox_arg: str) -> dict[str, Any]:
    """只读同步第三方 Gmail 客户端的变更到 All-mail 缓存。"""
    from mail_agent.mail_providers.gmail.adapter import normalize_mailbox as adapter_normalize_mailbox, sync_cached_mailbox_history

    mailbox = adapter_normalize_mailbox(mailbox_arg)
    return sync_cached_mailbox_history(mailbox)


def list_gmail_emails_page(
    mailbox_arg: str,
    days_arg: Any = 30,
    limit_arg: Any = 100,
    category_arg: Any = "all",
    page_token_arg: Any = "",
    page_offset_arg: Any = 0,
    exclude_message_ids_arg: Any = None,
) -> dict[str, Any]:
    """加载更多：按 All mail 拉一页 Gmail，并合并写入统一本地缓存。

    不再按 category 打 Gmail；分类由缓存标签投影完成。
    category 参数仅作响应诊断字段保留。
    """
    from mail_agent.mail_providers.gmail.adapter import (
        GMAIL_PAGE_SUMMARY_FETCH_MAX_WORKERS,
        _gmail_request_token_scope,
        fetch_message_summary,
        gmail_request,
        message_summary,
        normalize_mailbox as adapter_normalize_mailbox,
        read_cache,
        write_index,
    )

    mailbox = adapter_normalize_mailbox(mailbox_arg)
    days_input = 30 if days_arg in (None, "") else days_arg
    days = max(0, min(int(days_input), 3650))
    limit = max(1, min(int(limit_arg or 100), 100))
    category = _normalize_inbox_category(category_arg)
    # 加载更多固定扩 All mail 缓存，避免按分类重复打 Gmail
    query = _gmail_fallback_query("all", days)
    current_token = str(page_token_arg or "")
    current_offset = max(0, int(page_offset_arg or 0))
    excluded = {
        str(message_id)
        for message_id in (exclude_message_ids_arg or [])[:2000]
        if str(message_id or "")
    } if isinstance(exclude_message_ids_arg, list) else set()
    messages: list[dict[str, Any]] = []
    # 本页原始 summary，用于合并进本地 index（含 body 以外的元数据）
    page_summaries: list[dict[str, Any]] = []
    pages_scanned = 0

    def _merge_page_into_cache() -> None:
        """将本页拉取到的 summary 合并进 All mail 本地缓存。"""
        if not page_summaries:
            return
        existing = read_cache(mailbox)
        by_id: dict[str, dict[str, Any]] = {}
        for item in existing.get("messages") or []:
            if isinstance(item, dict) and item.get("id"):
                by_id[str(item["id"])] = item
        for summary in page_summaries:
            mid = str(summary.get("id") or "")
            if not mid:
                continue
            by_id[mid] = message_summary(summary)
        merged = sorted(by_id.values(), key=lambda item: int(item.get("internal_date") or 0), reverse=True)
        write_index(mailbox, merged)

    def build_payload(token: str, offset: int, has_more: bool) -> dict[str, Any]:
        ordered_messages = sorted(messages, key=_inbox_message_sort_key)
        _merge_page_into_cache()
        return {
            "mailbox": mailbox,
            "days": days,
            "category": category,
            "query": query,
            "source": "gmail",
            "count": len(ordered_messages),
            "page_token": token,
            "page_offset": offset,
            "has_more": has_more,
            "messages": ordered_messages,
            "updated_at": beijing_now(),
        }

    while pages_scanned < 5 and len(messages) < limit:
        request_token = current_token
        params: dict[str, Any] = {
            "q": query,
            "maxResults": 100,
            "fields": "messages/id,nextPageToken",
            # All mail 需包含 trash/spam，与 search_gmail 行为一致
            "includeSpamTrash": "true",
        }
        if request_token:
            params["pageToken"] = request_token
        # 首个 Gmail 请求会在受限上下文中取一次 token；复制该上下文给摘要 worker，
        # 避免“加载更多”因 100 个摘要产生 100 次 credentials/getToken 反向 RPC。
        with _gmail_request_token_scope():
            page = gmail_request(mailbox, "/users/me/messages", params)
            worker_context = copy_context()
        refs = [
            str(ref.get("id"))
            for ref in (page.get("messages") or [])
            if isinstance(ref, dict) and ref.get("id")
        ] if isinstance(page, dict) else []
        api_next_token = str(page.get("nextPageToken") or "") if isinstance(page, dict) else ""
        index = min(current_offset, len(refs))
        pages_scanned += 1

        while index < len(refs) and len(messages) < limit:
            batch_end = len(refs)
            positions = [position for position in range(index, batch_end) if refs[position] not in excluded]
            summaries: dict[int, dict[str, Any]] = {}
            if positions:
                with ThreadPoolExecutor(max_workers=min(GMAIL_PAGE_SUMMARY_FETCH_MAX_WORKERS, len(positions))) as pool:
                    futures = {
                        pool.submit(worker_context.copy().run, fetch_message_summary, mailbox, refs[position]): position
                        for position in positions
                    }
                    for future, position in ((future, futures[future]) for future in futures):
                        try:
                            summary = future.result()
                        except Exception:
                            summary = None
                        if isinstance(summary, dict) and summary.get("id"):
                            summaries[position] = summary

            thread_subjects = _thread_original_subjects(list(summaries.values()))
            for position in range(index, batch_end):
                message_id = refs[position]
                summary = summaries.get(position)
                if message_id in excluded or not summary:
                    continue
                page_summaries.append(summary)
                compact = _compact_inbox_message(summary, mailbox, thread_subjects)
                candidate_messages = [*messages, compact]
                next_position = position + 1
                cursor_token = request_token if next_position < len(refs) else api_next_token
                cursor_offset = next_position if next_position < len(refs) else 0
                candidate_payload = {
                    "mailbox": mailbox,
                    "days": days,
                    "category": category,
                    "query": query,
                    "source": "gmail",
                    "count": len(candidate_messages),
                    "page_token": cursor_token,
                    "page_offset": cursor_offset,
                    "has_more": bool(next_position < len(refs) or api_next_token),
                    "messages": candidate_messages,
                    "updated_at": beijing_now(),
                }
                if messages and _cached_rpc_frame_size("list_gmail_emails_page", candidate_payload) > CACHED_FEED_RESPONSE_MAX_BYTES:
                    return build_payload(request_token, position, True)
                messages.append(compact)
                excluded.add(message_id)
                if len(messages) >= limit:
                    return build_payload(cursor_token, cursor_offset, bool(next_position < len(refs) or api_next_token))
            index = batch_end

        current_token = api_next_token
        current_offset = 0
        if messages:
            return build_payload(current_token, 0, bool(current_token))
        if not current_token:
            break

    return build_payload(current_token, current_offset, bool(current_token))


def get_cached_email(mailbox_arg: str, message_id: str) -> dict[str, Any]:
    from mail_agent.mail_providers.gmail.adapter import fetch_and_cache_message, read_message as adapter_read_message, normalize_mailbox as adapter_normalize_mailbox

    mailbox = adapter_normalize_mailbox(mailbox_arg)
    msg_id = str(message_id or "")
    try:
        message = adapter_read_message(mailbox, msg_id)
    except ValueError:
        message = fetch_and_cache_message(mailbox, msg_id)
        if not message:
            raise ValueError(f"Gmail message not found: {msg_id}")
    return {
        "mailbox": mailbox,
        "message": _compact_cached_email_detail(message, mailbox),
    }



def _check_gmail_api_status(
    mailbox: str = "",
    *,
    timeout_seconds: float = CONNECTIVITY_CHECK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """探测 Gmail API 连通性与 RTT：对当前邮箱发起一次 users/me/profile。

    与 check_gmail_auth（仅查 token/账号元数据）不同，本工具会真实打 Gmail HTTP，
    用于侧栏展示延迟并帮助用户区分「授权缺失」与「网络/API 超时」。

    必须走 adapter.get_access_token：平台上通过 credentials/getToken 取短时 token，
    不可用本文件仅读本地 token 文件的 get_access_token。
    """
    import time as _time
    import urllib.error as _urlerr
    import urllib.parse as _urlparse
    import urllib.request as _urlreq
    from mail_agent.mail_providers.gmail.adapter import (
        get_access_token as adapter_get_access_token,
        get_platform_account,
    )

    started = _time.monotonic()
    deadline = started + max(0.1, float(timeout_seconds))

    def remaining_timeout(limit: float) -> float:
        """返回当前阶段可用时间，确保账号、凭据和 Gmail HTTP 共用总预算。"""
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Gmail API connectivity check timed out")
        return min(limit, remaining)

    requested = str(mailbox or "").strip().lower()
    # mailbox 为空时只复用已有账号快照，不能在延迟探测中再发一轮无限制的账号发现。
    target = requested
    if not target:
        from mail_agent.mail_providers.gmail.adapter import get_platform_accounts
        accounts = get_platform_accounts()
        target = str((accounts[0] if accounts else {}).get("email") or "").strip().lower()
    if not target:
        return {
            "ok": False,
            "status": "unavailable",
            "message": "No Gmail mailbox available for API check.",
            "elapsed_ms": int((_time.monotonic() - started) * 1000),
            "mailbox": "",
        }
    try:
        # 已有账号快照可直接取 token；仅缺失时才在 3 秒预算内刷新，避免状态轮询额外占用反向 RPC。
        if not get_platform_account(target):
            refresh_platform_google_accounts(
                timeout_seconds=remaining_timeout(CONNECTIVITY_GMAIL_ACCOUNT_TIMEOUT_SECONDS),
            )
        # 侧栏探测使用短超时；token 优先平台 credentials，再 fallback 本地
        token = adapter_get_access_token(
            target,
            platform_token_timeout_seconds=remaining_timeout(float("inf")),
            token_refresh_timeout_seconds=remaining_timeout(float("inf")),
            refresh_platform_accounts=False,
        )
        url = GMAIL_API_BASE + "/users/me/profile?" + _urlparse.urlencode({"fields": "emailAddress"})
        request = _urlreq.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="GET",
        )
        with _urlreq.urlopen(request, timeout=remaining_timeout(8.0)) as response:
            raw = response.read().decode("utf-8")
        profile = json.loads(raw) if raw else {}
        email = str((profile or {}).get("emailAddress") or "").strip().lower()
        return {
            "ok": True,
            "status": "connected",
            "message": "Gmail API is connected.",
            "elapsed_ms": int((_time.monotonic() - started) * 1000),
            "mailbox": email or target,
        }
    except Exception as exc:
        message = str(exc) or "Gmail API check failed."
        if isinstance(exc, _urlerr.HTTPError):
            message = f"Gmail API request failed: {exc.code}"
        # 鉴权类失败标 unavailable，其余标 error（含超时/网络）
        lowered = message.lower()
        status = "unavailable" if any(
            token in lowered for token in ("401", "403", "unauthorized", "auth", "token", "credential", "not found")
        ) else "error"
        return {
            "ok": False,
            "status": status,
            "message": message[:240],
            "elapsed_ms": int((_time.monotonic() - started) * 1000),
            "mailbox": target,
        }


def _check_gmail_auth(mailbox: str) -> dict[str, Any]:
    """Check Gmail authorization status.

    Platform: Anna Credentials account metadata and on-demand token access.
    Legacy platform/local token paths remain available for development.
    Local dev: token file exists on disk (content/expiry not validated).
    """
    import os as _os
    from mail_agent.mail_providers.gmail.adapter import _token_dir, sanitize_mailbox_id, get_authorized_email, get_multi_token_map, get_platform_account, get_platform_accounts
    from pathlib import Path as _Path

    requested = str(mailbox or "").strip().lower()
    refresh_platform_google_accounts()
    platform_accounts = get_platform_accounts()

    # 系统级判断：mailbox 为空时，只看 token 有没有，不关心具体邮箱
    if not requested:
        active_accounts = [item for item in platform_accounts if str(item.get("status") or "active").lower() in {"", "active", "connected"}]
        if active_accounts:
            return {"authorized": True, "source": "platform_credentials", "authorized_email": str(active_accounts[0].get("email") or ""), "mode": "any"}
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

    platform_account = get_platform_account(requested)
    if platform_account:
        status = str(platform_account.get("status") or "active").lower()
        return {
            "authorized": status in {"", "active", "connected"},
            "source": "platform_credentials",
            "authorized_email": requested,
        }

    # Legacy multi-token snapshot support.
    if requested in get_multi_token_map():
        return {"authorized": True, "source": "platform_multi", "authorized_email": requested}

    # Legacy platform path — check the injected single OAuth credential.
    platform_token = _os.environ.get("GMAIL_ACCESS_TOKEN") or _os.environ.get("GOOGLE_ACCESS_TOKEN")
    if platform_token and platform_token.strip():
        authorized_email = get_authorized_email().strip().lower()
        authorized = bool(authorized_email and requested == authorized_email)
        return {
            "authorized": authorized,
            "source": "platform",
            "authorized_email": authorized_email,
        }

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
