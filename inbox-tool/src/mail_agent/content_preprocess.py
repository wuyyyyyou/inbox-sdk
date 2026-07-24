"""邮件正文与附件的受限后台预处理。

原始 ``body_text`` 和 MIME payload 继续由 Gmail 缓存保存；本模块只写派生结构化
结果，供 AI 检索、线程证据和附件事实使用。解析失败必须显式标记，不能伪造结果。
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree

_TEXT_LIMIT = 24_000
_ATTACHMENT_BYTES_LIMIT = 8 * 1024 * 1024
_SIGNATURE_DELIMITERS = (
    re.compile(r"\n--\s*\n"),
    re.compile(r"\n(?:best|best regards|kind regards|regards|thanks|thank you|cheers|sincerely|祝好|此致|谢谢)[,，]?\s*\n", re.IGNORECASE),
)
_QUOTE_BOUNDARIES = (
    re.compile(r"\nOn .{0,120} wrote:\s*\n", re.IGNORECASE),
    re.compile(r"\n(?:From|发件人|寄件者):.+\n(?:Sent|发送时间|Date|日期):", re.IGNORECASE),
    re.compile(r"\n-{2,}\s*(?:Original Message|转发邮件|原始邮件).*$", re.IGNORECASE | re.DOTALL),
)
_EMAIL_RE = re.compile(r"(?<![\w.+-])([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})(?![\w.-])")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{6,}\d)(?!\w)")
_INVOICE_RE = re.compile(r"\b(?:invoice|receipt|bill)\s*(?:#|no\.?|number)?\s*([A-Z0-9-]{4,})\b", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"(?<!\w)([$€£¥]|USD|EUR|GBP|CNY|RMB)\s*([0-9][0-9,]*(?:\.\d{2})?)", re.IGNORECASE)
_PAYMENT_RE = re.compile(r"\b(paid|unpaid|overdue|due|pending|已支付|未支付|逾期|待付款)\b", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean_lines(text: str) -> str:
    """折叠空白并移除不可见控制符，保留段落以便引用与回复读取。"""
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()[:_TEXT_LIMIT]


def split_message_body(raw_text: str) -> dict[str, str]:
    """分离当前正文、历史引用与签名；规则保守，无法确认时保留正文。"""
    text = _clean_lines(raw_text)
    quoted = ""
    for boundary in _QUOTE_BOUNDARIES:
        match = boundary.search(text)
        if match:
            quoted = text[match.start():].strip()
            text = text[:match.start()].strip()
            break
    signature = ""
    for delimiter in _SIGNATURE_DELIMITERS:
        match = delimiter.search(text)
        if match and match.start() > 0:
            signature = text[match.end():].strip()
            text = text[:match.start()].strip()
            break
    return {"body": text, "quoted_text": quoted[:_TEXT_LIMIT], "signature": signature[:4000]}


def extract_signature_contacts(signature: str) -> list[dict[str, str]]:
    """从签名提取可验证的联系方式；姓名/职位/公司仅在文本行中保守猜测。"""
    text = _clean_lines(signature)
    if not text:
        return []
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    emails = list(dict.fromkeys(_EMAIL_RE.findall(text)))
    phones = list(dict.fromkeys(match.group(0).strip() for match in _PHONE_RE.finditer(text)))
    name = lines[0][:120] if lines and "@" not in lines[0] and len(lines[0]) <= 120 else ""
    title = ""
    company = ""
    for line in lines[1:5]:
        lowered = line.lower()
        if not title and any(token in lowered for token in ("ceo", "cto", "founder", "manager", "director", "engineer", "负责人", "经理", "总监")):
            title = line[:160]
        elif not company and "@" not in line and not _PHONE_RE.search(line):
            company = line[:160]
    if not (emails or phones or name):
        return []
    return [{
        "name": name,
        "title": title,
        "company": company,
        "emails": ", ".join(emails[:5]),
        "phones": ", ".join(phones[:5]),
    }]


def extract_invoice_facts(text: str, *, source: str) -> list[dict[str, str]]:
    """提取带来源的金额/发票/付款状态，正文与附件结果绝不混成同一个字段。"""
    value = _clean_lines(text)
    if not value:
        return []
    invoice_numbers = list(dict.fromkeys(_INVOICE_RE.findall(value)))[:5]
    amounts = [f"{match.group(1)} {match.group(2)}" for match in _AMOUNT_RE.finditer(value)][:8]
    statuses = list(dict.fromkeys(match.group(1) for match in _PAYMENT_RE.finditer(value)))[:3]
    if not (invoice_numbers or amounts or statuses):
        return []
    return [{
        "source": source,
        "invoice_numbers": ", ".join(invoice_numbers),
        "amounts": ", ".join(amounts),
        "payment_status": ", ".join(statuses),
    }]


def _xml_text(data: bytes, *, paths: list[str]) -> str:
    """解析 Office Open XML 中指定节点的文本，拒绝畸形 XML。"""
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return ""
    values: list[str] = []
    for path in paths:
        for node in root.findall(path):
            if node.text:
                values.append(node.text)
    return _clean_lines("\n".join(values))


def _extract_office_text(content: bytes, suffix: str) -> str:
    """使用 stdlib ZIP/XML 读取 DOCX/XLSX/PPTX 文本，避免引入重型 Office 运行时。"""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            parts: list[str] = []
            if suffix == ".docx":
                if "word/document.xml" in names:
                    parts.append(_xml_text(archive.read("word/document.xml"), paths=[".//{*}t"]))
            elif suffix == ".pptx":
                for name in names:
                    if name.startswith("ppt/slides/slide") and name.endswith(".xml"):
                        parts.append(_xml_text(archive.read(name), paths=[".//{*}t"]))
            elif suffix == ".xlsx":
                shared: list[str] = []
                if "xl/sharedStrings.xml" in names:
                    shared = _xml_text(archive.read("xl/sharedStrings.xml"), paths=[".//{*}t"]).split("\n")
                for name in names:
                    if not name.startswith("xl/worksheets/sheet") or not name.endswith(".xml"):
                        continue
                    root = ElementTree.fromstring(archive.read(name))
                    cells: list[str] = []
                    for cell in root.findall(".//{*}c"):
                        value = cell.findtext("{*}v") or ""
                        if cell.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                            value = shared[int(value)]
                        if value:
                            cells.append(value)
                    parts.append("\n".join(cells))
            return _clean_lines("\n".join(part for part in parts if part))
    except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
        return ""


def _extract_pdf_text(content: bytes) -> tuple[str, str]:
    """优先 pypdf；未安装时只记录依赖不可用，不能用正则伪造可靠 PDF 内容。"""
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except Exception:
        return "", "parser_unavailable:pypdf"
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = reader.pages[:30]
        return _clean_lines("\n".join(page.extract_text() or "" for page in pages)), "ok"
    except Exception as exc:
        return "", f"parse_error:{type(exc).__name__}"


def parse_attachment_content(content: bytes, *, filename: str, mime_type: str) -> dict[str, Any]:
    """受限附件解析：文本、Office、PDF；图片等待平台 OCR，结果不保存附件字节。"""
    if len(content) > _ATTACHMENT_BYTES_LIMIT:
        return {"status": "skipped_too_large", "text": "", "facts": [], "size": len(content)}
    lower_name = filename.lower()
    mime = mime_type.lower()
    text = ""
    status = "ok"
    if mime.startswith("text/") or lower_name.endswith((".txt", ".csv", ".md", ".json")):
        text = content.decode("utf-8", errors="replace")
        if lower_name.endswith(".csv"):
            try:
                rows = list(csv.reader(io.StringIO(text)))[:200]
                text = "\n".join(" | ".join(row) for row in rows)
            except csv.Error:
                pass
    elif lower_name.endswith((".docx", ".xlsx", ".pptx")):
        text = _extract_office_text(content, lower_name[-5:])
        status = "ok" if text else "parse_error:office"
    elif mime == "application/pdf" or lower_name.endswith(".pdf"):
        text, status = _extract_pdf_text(content)
    elif mime.startswith("image/") or lower_name.endswith((".png", ".jpg", ".jpeg", ".webp", ".tiff", ".bmp")):
        # 本地 Pillow/Tesseract 会显著增大 Executa 二进制；图片不在本地下载解析，
        # 等待 Anna 平台提供可控预算的 OCR 接口后再由专用任务调用。
        status = "ocr_pending:platform_interface"
    else:
        status = "unsupported_type"
    text = _clean_lines(text)
    return {
        "status": status,
        "text": text[:_TEXT_LIMIT],
        "facts": extract_invoice_facts(text, source="attachment"),
        "size": len(content),
    }


def preprocess_message(message: dict[str, Any], *, fetch_attachment: Any = None) -> dict[str, Any]:
    """生成并写回派生正文、签名、联系人与附件解析结果。

    ``fetch_attachment`` 由 Gmail adapter 注入，避免本模块保存 OAuth/token 逻辑。
    """
    derived = split_message_body(str(message.get("body_text") or ""))
    attachments = message.get("attachments") if isinstance(message.get("attachments"), list) else []
    analyses: list[dict[str, Any]] = []
    for item in attachments[:12]:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "attachment")[:240]
        mime_type = str(item.get("mimeType") or "application/octet-stream")[:160]
        attachment_id = str(item.get("attachmentId") or "")
        result: dict[str, Any] = {"filename": filename, "mime_type": mime_type, "status": "metadata_only", "text": "", "facts": []}
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > _ATTACHMENT_BYTES_LIMIT:
            result["status"] = "skipped_too_large"
        elif mime_type.lower().startswith("image/") or filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".tiff", ".bmp")):
            # 没有平台 OCR 前不下载图片字节，避免无效网络与内存占用。
            result["status"] = "ocr_pending:platform_interface"
        elif fetch_attachment is None or not attachment_id:
            result["status"] = "pending_download"
        else:
            try:
                result.update(parse_attachment_content(fetch_attachment(attachment_id), filename=filename, mime_type=mime_type))
            except Exception as exc:
                result["status"] = f"download_or_parse_error:{type(exc).__name__}"
        analyses.append(result)
    message["content_analysis"] = {
        "version": 1,
        "processed_at": _now(),
        "body": derived["body"],
        "quoted_text": derived["quoted_text"],
        "signature": derived["signature"],
        "contacts": extract_signature_contacts(derived["signature"]),
        "body_invoice_facts": extract_invoice_facts(derived["body"], source="body"),
        "attachment_analysis": analyses,
    }
    return message


__all__ = ["parse_attachment_content", "preprocess_message", "split_message_body"]
