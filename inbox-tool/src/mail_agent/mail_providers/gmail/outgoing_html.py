"""外发富文本 HTML 的白名单净化。

邮件正文来自浏览器编辑器，不能信任调用方已经完成净化。这里不用正则处理
HTML，而是逐个解析标签、属性和样式，只重新输出本功能明确允许的最小集合。
"""

from __future__ import annotations

from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit


_ALLOWED_TAGS = {"p", "br", "strong", "em", "u", "span", "ul", "ol", "li", "a", "h1", "h2", "h3"}
_VOID_TAGS = {"br"}
_FONT_SIZE_BY_LEGACY_VALUE = {"1": 12, "2": 14, "3": 16, "4": 18, "5": 20, "6": 24, "7": 24}
_ALLOWED_FONT_SIZES = {12, 14, 16, 18, 20, 24}
_ALLOWED_COLORS = {"#1f2937", "#dc2626", "#d97706", "#16a34a", "#2563eb", "#7c3aed"}
_RGB_COLORS = {
    "rgb(31,41,55)": "#1f2937",
    "rgb(220,38,38)": "#dc2626",
    "rgb(217,119,6)": "#d97706",
    "rgb(22,163,74)": "#16a34a",
    "rgb(37,99,235)": "#2563eb",
    "rgb(124,58,237)": "#7c3aed",
}
_IGNORED_CONTENT_TAGS = {"script", "style", "iframe", "object", "embed", "svg", "math"}


def _safe_href(value: str) -> str:
    """仅保留外发邮件中允许的链接协议，拒绝相对地址和脚本协议。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return raw
    if parsed.scheme == "mailto" and parsed.path:
        return raw
    return ""


def _normalize_color(value: str) -> str:
    """浏览器可能把固定色板转成 rgb，统一回安全且稳定的十六进制值。"""
    normalized = str(value or "").strip().lower().replace(" ", "")
    normalized = _RGB_COLORS.get(normalized, normalized)
    return normalized if normalized in _ALLOWED_COLORS else ""


def _safe_style(value: str) -> str:
    """样式只允许字号和文字颜色，避免 class/style 携带布局或外部资源。"""
    color = ""
    font_size = ""
    for declaration in str(value or "").split(";"):
        if ":" not in declaration:
            continue
        property_name, raw_value = declaration.split(":", 1)
        property_name = property_name.strip().lower()
        if property_name == "color":
            color = _normalize_color(raw_value)
        elif property_name == "font-size":
            raw_size = raw_value.strip().lower().removesuffix("px").strip()
            try:
                parsed_size = int(raw_size)
            except ValueError:
                continue
            if parsed_size in _ALLOWED_FONT_SIZES:
                font_size = f"{parsed_size}px"
    styles = []
    if font_size:
        styles.append(f"font-size: {font_size}")
    if color:
        styles.append(f"color: {color}")
    return "; ".join(styles)


class _OutgoingHtmlSanitizer(HTMLParser):
    """将输入 HTML 重建为固定白名单，永不复用原始属性或原始标签。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open_tags: list[str] = []
        self.ignored_content_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in _IGNORED_CONTENT_TAGS:
            self.ignored_content_depth += 1
            return
        if self.ignored_content_depth:
            return
        # 浏览器兼容命令会产生 b/i/div/font；在净化边界统一映射到协议允许的输出。
        normalized_tag = {"b": "strong", "i": "em", "div": "p", "font": "span"}.get(normalized_tag, normalized_tag)
        if normalized_tag not in _ALLOWED_TAGS:
            return
        attributes = {str(name or "").lower(): str(value or "") for name, value in attrs}
        output_attributes: list[tuple[str, str]] = []
        if normalized_tag == "a":
            href = _safe_href(attributes.get("href", ""))
            if href:
                output_attributes.append(("href", href))
        if normalized_tag == "span":
            style = _safe_style(attributes.get("style", ""))
            if tag.lower() == "font":
                legacy_size = _FONT_SIZE_BY_LEGACY_VALUE.get(attributes.get("size", ""))
                legacy_color = _normalize_color(attributes.get("color", ""))
                legacy_style = "; ".join(
                    item for item in (
                        f"font-size: {legacy_size}px" if legacy_size else "",
                        f"color: {legacy_color}" if legacy_color else "",
                    ) if item
                )
                style = _safe_style(legacy_style) or style
            if style:
                output_attributes.append(("style", style))
        rendered_attributes = "".join(f' {name}="{escape(value, quote=True)}"' for name, value in output_attributes)
        self.parts.append(f"<{normalized_tag}{rendered_attributes}>")
        if normalized_tag not in _VOID_TAGS:
            self.open_tags.append(normalized_tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in _IGNORED_CONTENT_TAGS:
            self.ignored_content_depth = max(0, self.ignored_content_depth - 1)
            return
        if self.ignored_content_depth:
            return
        normalized_tag = {"b": "strong", "i": "em", "div": "p", "font": "span"}.get(normalized_tag, normalized_tag)
        if normalized_tag not in _ALLOWED_TAGS or normalized_tag in _VOID_TAGS:
            return
        # 只关闭实际写入过的最近同名标签，异常嵌套不会生成破损的结束标签。
        if normalized_tag not in self.open_tags:
            return
        while self.open_tags:
            opened = self.open_tags.pop()
            self.parts.append(f"</{opened}>")
            if opened == normalized_tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.ignored_content_depth:
            self.parts.append(escape(data, quote=False))

    def close(self) -> None:
        super().close()
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")


def sanitize_outgoing_html(value: str | None) -> str:
    """返回可存储、可发送的安全 HTML；空值保持为空以兼容纯文本路径。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parser = _OutgoingHtmlSanitizer()
    parser.feed(raw)
    parser.close()
    return "".join(parser.parts).strip()
