"""外发附件：本地 stage、校验与草稿引用。

大文件字节不得进入 JSON-RPC / APS KV。流程：
1. begin_stage → 返回 loopback PUT URL + attachment_id
2. 前端 PUT 文件字节到 loopback
3. 草稿只持久化元数据（id/filename/mime/size/storage_key）
4. 发送时按 storage_key 读字节，组装 MIME 后调用 Gmail
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# 个人 Gmail：全部附件合计 ≤ 25MB（官方帮助文档，非单文件上限）
OUTGOING_ATTACHMENT_TOTAL_MAX_BYTES = 25 * 1024 * 1024
# 单文件同样不得超过合计上限
OUTGOING_ATTACHMENT_SINGLE_MAX_BYTES = OUTGOING_ATTACHMENT_TOTAL_MAX_BYTES
_STAGE_TOKEN_TTL_SECONDS = 2 * 60 * 60

# 对齐 Gmail 安全拦截常见可执行/脚本类型（见 support.google.com/mail/answer/6590）
_BLOCKED_EXTENSIONS = frozenset({
    "ade", "adp", "apk", "appx", "appxbundle", "bat", "cab", "chm", "cmd", "com",
    "cpl", "diagcab", "diagcfg", "diagpack", "dll", "dmg", "ex", "ex_", "exe",
    "hta", "img", "ins", "iso", "isp", "jar", "jnlp", "js", "jse", "lib", "lnk",
    "mde", "mjs", "msc", "msi", "msix", "msixbundle", "msp", "mst", "nsh", "pif",
    "ps1", "scr", "sct", "shb", "sys", "vb", "vbe", "vbs", "vhd", "vxd", "wsc",
    "wsf", "wsh", "xll",
})

_STAGE_UPLOAD_TOKENS: dict[str, dict[str, Any]] = {}
_STAGE_LOCK = threading.Lock()


def _data_root() -> Path:
    from .adapter import _data_root as adapter_data_root
    return adapter_data_root()


def sanitize_mailbox_id(mailbox: str) -> str:
    from .adapter import sanitize_mailbox_id as _sanitize
    return _sanitize(mailbox)


def outgoing_stage_dir(mailbox: str) -> Path:
    """外发附件 stage 根目录（按邮箱隔离）。"""
    path = _data_root() / "anna-inbox" / "outgoing_attachments" / sanitize_mailbox_id(mailbox)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_filename(filename: str) -> str:
    name = str(filename or "attachment").strip().replace("\\", "_").replace("/", "_")
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return name[:160] or "attachment"


def attachment_extension(filename: str) -> str:
    name = str(filename or "").strip().lower()
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1]


def is_blocked_outgoing_filename(filename: str) -> bool:
    """是否命中 Gmail 常见可执行/脚本扩展名黑名单。"""
    ext = attachment_extension(filename)
    if not ext:
        return False
    return ext in _BLOCKED_EXTENSIONS


def validate_outgoing_attachment_meta(
    *,
    filename: str,
    mime_type: str,
    size: int,
    existing_total_bytes: int = 0,
) -> dict[str, Any]:
    """校验外发附件元数据；通过时返回规范化字段。"""
    safe_name = _safe_filename(filename)
    if is_blocked_outgoing_filename(safe_name):
        raise ValueError(f"File type is blocked for security reasons: {safe_name}")
    try:
        size_int = int(size)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid attachment size") from exc
    if size_int < 0:
        raise ValueError("Invalid attachment size")
    if size_int > OUTGOING_ATTACHMENT_SINGLE_MAX_BYTES:
        raise ValueError(
            f"Attachment exceeds the {OUTGOING_ATTACHMENT_SINGLE_MAX_BYTES // (1024 * 1024)} MB limit"
        )
    total = int(existing_total_bytes or 0) + size_int
    if total > OUTGOING_ATTACHMENT_TOTAL_MAX_BYTES:
        raise ValueError(
            f"Total attachments exceed the {OUTGOING_ATTACHMENT_TOTAL_MAX_BYTES // (1024 * 1024)} MB limit"
        )
    mime = str(mime_type or "application/octet-stream").strip().lower() or "application/octet-stream"
    return {
        "filename": safe_name,
        "mime_type": mime[:120],
        "size": size_int,
    }


def _cleanup_expired_stage_tokens() -> None:
    now = time.time()
    expired = [
        token for token, meta in _STAGE_UPLOAD_TOKENS.items()
        if float(meta.get("expires_at_ts") or 0) <= now
    ]
    for token in expired:
        meta = _STAGE_UPLOAD_TOKENS.pop(token, {})
        path = meta.get("path")
        if path and not meta.get("committed"):
            try:
                Path(str(path)).unlink(missing_ok=True)
            except OSError:
                pass


def stage_file_path(mailbox: str, attachment_id: str, filename: str) -> Path:
    return outgoing_stage_dir(mailbox) / f"{attachment_id}-{_safe_filename(filename)}"


def create_stage_upload_slot(
    mailbox: str,
    *,
    filename: str,
    mime_type: str,
    size: int,
    existing_total_bytes: int = 0,
    draft_scope: str = "compose",
    draft_key: str = "",
) -> dict[str, Any]:
    """创建待 PUT 的 stage 槽位，返回 attachment_id 与本地路径元数据。"""
    meta = validate_outgoing_attachment_meta(
        filename=filename,
        mime_type=mime_type,
        size=size,
        existing_total_bytes=existing_total_bytes,
    )
    attachment_id = uuid.uuid4().hex
    path = stage_file_path(mailbox, attachment_id, meta["filename"])
    token = uuid.uuid4().hex
    expires_at_ts = time.time() + _STAGE_TOKEN_TTL_SECONDS
    with _STAGE_LOCK:
        _cleanup_expired_stage_tokens()
        _STAGE_UPLOAD_TOKENS[token] = {
            "mailbox": str(mailbox or "").strip().lower(),
            "attachment_id": attachment_id,
            "filename": meta["filename"],
            "mime_type": meta["mime_type"],
            "expected_size": meta["size"],
            "path": str(path),
            "draft_scope": str(draft_scope or "compose"),
            "draft_key": str(draft_key or ""),
            "expires_at_ts": expires_at_ts,
            "committed": False,
        }
    return {
        "attachment_id": attachment_id,
        "filename": meta["filename"],
        "mime_type": meta["mime_type"],
        "size": meta["size"],
        "upload_token": token,
        "storage_key": str(path),
        "expires_at_ts": expires_at_ts,
    }


def get_stage_upload_slot(token: str) -> dict[str, Any] | None:
    with _STAGE_LOCK:
        _cleanup_expired_stage_tokens()
        meta = _STAGE_UPLOAD_TOKENS.get(str(token or "").strip())
        return dict(meta) if isinstance(meta, dict) else None


def commit_stage_upload(token: str, content: bytes) -> dict[str, Any]:
    """写入 PUT 字节并标记已提交。"""
    with _STAGE_LOCK:
        _cleanup_expired_stage_tokens()
        meta = _STAGE_UPLOAD_TOKENS.get(str(token or "").strip())
        if not isinstance(meta, dict):
            raise ValueError("Upload slot not found or expired")
        expected = int(meta.get("expected_size") or 0)
        if expected and len(content) != expected:
            # 允许浏览器与声明 size 略有偏差时仍以实际字节为准，但不能超总限
            if len(content) > OUTGOING_ATTACHMENT_SINGLE_MAX_BYTES:
                raise ValueError("Attachment exceeds size limit")
        path = Path(str(meta.get("path") or ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        meta["committed"] = True
        meta["size"] = len(content)
        _STAGE_UPLOAD_TOKENS[str(token)] = meta
        return {
            "ok": True,
            "attachment_id": str(meta.get("attachment_id") or ""),
            "filename": str(meta.get("filename") or "attachment"),
            "mime_type": str(meta.get("mime_type") or "application/octet-stream"),
            "size": len(content),
            "storage_key": str(path),
            "mailbox": str(meta.get("mailbox") or ""),
        }


def read_staged_attachment(mailbox: str, storage_key: str) -> bytes:
    """读取已 stage 的附件字节；校验路径必须落在该邮箱 stage 目录内。"""
    root = outgoing_stage_dir(mailbox).resolve()
    path = Path(str(storage_key or "")).expanduser().resolve()
    if root not in path.parents and path != root:
        raise ValueError("Invalid attachment storage key")
    if not path.is_file():
        raise ValueError("Staged attachment not found")
    data = path.read_bytes()
    if len(data) > OUTGOING_ATTACHMENT_SINGLE_MAX_BYTES:
        raise ValueError("Staged attachment exceeds size limit")
    return data


def delete_staged_attachment(mailbox: str, storage_key: str) -> bool:
    """删除单个 stage 文件。"""
    try:
        root = outgoing_stage_dir(mailbox).resolve()
        path = Path(str(storage_key or "")).expanduser().resolve()
        if root not in path.parents and path != root:
            return False
        if path.is_file():
            path.unlink()
            return True
    except OSError:
        return False
    return False


def delete_staged_attachments(mailbox: str, items: list[dict[str, Any]] | None) -> int:
    deleted = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("storage_key") or "")
        if key and delete_staged_attachment(mailbox, key):
            deleted += 1
    return deleted


def load_outgoing_attachments_for_send(
    mailbox: str,
    items: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """从草稿元数据加载发送用附件（含 content 字节）。"""
    loaded: list[dict[str, Any]] = []
    total = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "attachment")
        mime_type = str(item.get("mime_type") or "application/octet-stream")
        storage_key = str(item.get("storage_key") or "")
        if not storage_key:
            continue
        if is_blocked_outgoing_filename(filename):
            raise ValueError(f"File type is blocked for security reasons: {filename}")
        content = read_staged_attachment(mailbox, storage_key)
        total += len(content)
        if total > OUTGOING_ATTACHMENT_TOTAL_MAX_BYTES:
            raise ValueError(
                f"Total attachments exceed the {OUTGOING_ATTACHMENT_TOTAL_MAX_BYTES // (1024 * 1024)} MB limit"
            )
        loaded.append({
            "id": str(item.get("id") or ""),
            "filename": _safe_filename(filename),
            "mime_type": mime_type,
            "size": len(content),
            "content": content,
        })
    return loaded


def normalize_draft_attachment_meta(items: Any) -> list[dict[str, Any]]:
    """清洗草稿中的附件元数据列表（不含 content）。"""
    result: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return result
    for item in items[:40]:
        if not isinstance(item, dict):
            continue
        attachment_id = str(item.get("id") or "").strip()
        filename = _safe_filename(str(item.get("filename") or "attachment"))
        storage_key = str(item.get("storage_key") or "").strip()
        if not attachment_id or not storage_key:
            continue
        if is_blocked_outgoing_filename(filename):
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        result.append({
            "id": attachment_id,
            "filename": filename,
            "mime_type": str(item.get("mime_type") or "application/octet-stream")[:120],
            "size": max(0, size),
            "storage_key": storage_key,
        })
    return result


def write_stage_meta_sidecar(path: Path, meta: dict[str, Any]) -> None:
    """可选：写入与 stage 文件并列的 JSON 元数据，便于调试。"""
    try:
        sidecar = path.with_suffix(path.suffix + ".meta.json")
        sidecar.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
