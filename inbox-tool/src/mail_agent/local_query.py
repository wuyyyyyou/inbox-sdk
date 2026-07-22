"""本地 Inbox 查询语法（与前端 inboxQuery.ts 对齐）。

供 cache-only 扫描使用：只过滤本地缓存，不调用 Gmail API。
支持：
- 字段：subject/body/from/to/is/has/before/after 与裸词
- 逻辑：AND / OR（AND 优先）
- 排除：-term / -from:x（Shortwave 风格一元否定）
- is:todo：依赖调用方传入的 todo_message_ids（前端 workflow 标记）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .domain.types import MessageLite

_FIELDS = frozenset({"subject", "body", "from", "to", "is", "has", "before", "after"})
_STATUS_VALUES = frozenset({
    "sent", "unread", "done", "todo", "inbox", "snoozed", "starred",
    "important", "draft", "trash", "spam", "all",
})
_OPERATORS = frozenset({"AND", "OR"})


@dataclass(frozen=True)
class LocalQueryTerm:
    """单个查询条件。"""

    field: str  # 含 any
    value: str
    exclude: bool = False


@dataclass(frozen=True)
class LocalQueryNode:
    """AND/OR 树节点；terms 为空且 kind 为 term 时用 term 字段。"""

    kind: str  # term | and | or
    term: LocalQueryTerm | None = None
    children: tuple["LocalQueryNode", ...] = ()


@dataclass(frozen=True)
class ParsedLocalQuery:
    """解析结果。"""

    expression: LocalQueryNode | None
    error: str = ""
    # 规范化后的展示字符串（供 Thinking 后小字 / 一键搜索）
    display: str = ""


def _join(kind: str, nodes: list[LocalQueryNode]) -> LocalQueryNode:
    if len(nodes) == 1:
        return nodes[0]
    return LocalQueryNode(kind=kind, children=tuple(nodes))


def _parse_term_token(token: str) -> tuple[LocalQueryTerm | None, str]:
    raw = token
    exclude = False
    if raw.startswith("-") and len(raw) > 1:
        exclude = True
        raw = raw[1:]
    if not raw or raw == "-":
        return None, "The - operator needs a search term after it."
    if ":" in raw:
        field, value = raw.split(":", 1)
        field = field.lower()
        if field not in _FIELDS:
            return None, f"Unknown search field: {field or raw}."
        if not value:
            return None, f"The {field}: field needs a keyword."
        if field == "is" and value.lower() not in _STATUS_VALUES:
            return None, f"is: must use {', '.join(sorted(_STATUS_VALUES))}."
        if field == "has" and value.lower() != "attachment":
            return None, "has: currently supports attachment only."
        if field in {"before", "after"} and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return None, f"{field}: must use YYYY-MM-DD."
        return LocalQueryTerm(field=field, value=value.lower(), exclude=exclude), ""
    return LocalQueryTerm(field="any", value=raw.lower(), exclude=exclude), ""


def parse_local_query(input_text: str) -> ParsedLocalQuery:
    """解析本地查询；语法错误时 expression 为 None 并带 error。"""
    raw = str(input_text or "").strip()
    if not raw:
        return ParsedLocalQuery(expression=None, error="", display="")
    tokens = raw.split()
    groups: list[LocalQueryNode] = []
    and_terms: list[LocalQueryNode] = []
    expecting_term = True
    for token in tokens:
        upper = token.upper()
        if upper in _OPERATORS:
            if expecting_term:
                return ParsedLocalQuery(
                    expression=None,
                    error=f"The {upper} operator needs a term before and after it.",
                    display=raw,
                )
            if upper == "OR":
                groups.append(_join("and", and_terms))
                and_terms = []
            expecting_term = True
            continue
        if not expecting_term:
            return ParsedLocalQuery(
                expression=None,
                error="Add an AND or OR operator between search terms.",
                display=raw,
            )
        term, err = _parse_term_token(token)
        if term is None:
            return ParsedLocalQuery(expression=None, error=err, display=raw)
        and_terms.append(LocalQueryNode(kind="term", term=term))
        expecting_term = False
    if expecting_term:
        return ParsedLocalQuery(
            expression=None,
            error="The final operator needs a search term after it.",
            display=raw,
        )
    groups.append(_join("and", and_terms))
    return ParsedLocalQuery(expression=_join("or", groups), error="", display=raw)


def _has_match(value: str | None, term: str) -> bool:
    return term in str(value or "").lower()


def _message_timestamp_ms(message: MessageLite | dict[str, Any]) -> float | None:
    """把 internal_date（毫秒 epoch 或 RFC/ISO 字符串）统一成毫秒时间戳。"""
    if isinstance(message, MessageLite):
        raw = message.internal_date
    else:
        raw = message.get("internal_date") or message.get("date") or ""
    text = str(raw or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        try:
            return float(text)
        except ValueError:
            return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(text).timestamp() * 1000
    except Exception:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return None


def _label_set(message: MessageLite | dict[str, Any]) -> set[str]:
    if isinstance(message, MessageLite):
        labels = message.label_ids or []
    else:
        labels = message.get("label_ids") or []
    return {str(item).upper() for item in labels}


def _field(message: MessageLite | dict[str, Any], name: str) -> str:
    if isinstance(message, MessageLite):
        mapping = {
            "from": message.from_addr,
            "to": message.to_addr,
            "subject": message.subject,
            "snippet": message.snippet,
            "id": message.message_id,
            "body": message.snippet,
        }
        return str(mapping.get(name) or "")
    mapping = {
        "from": message.get("from") or "",
        "to": message.get("to") or "",
        "subject": message.get("subject") or message.get("latest_subject") or "",
        "snippet": message.get("snippet") or "",
        "id": message.get("id") or message.get("message_id") or "",
        "body": message.get("body_preview") or message.get("snippet") or "",
    }
    return str(mapping.get(name) or "")


def _match_term(
    message: MessageLite | dict[str, Any],
    term: LocalQueryTerm,
    *,
    todo_ids: set[str],
    done_ids: set[str] | None,
    snoozed_ids: set[str] | None,
) -> bool:
    hit = False
    if term.field == "subject":
        hit = _has_match(_field(message, "subject"), term.value)
    elif term.field == "body":
        hit = _has_match(_field(message, "body"), term.value)
    elif term.field == "from":
        hit = _has_match(_field(message, "from"), term.value)
    elif term.field == "to":
        hit = _has_match(_field(message, "to"), term.value)
    elif term.field == "has":
        if isinstance(message, MessageLite):
            hit = term.value == "attachment" and bool(message.has_attachment)
        else:
            hit = term.value == "attachment" and bool(
                message.get("has_attachment") or message.get("attachment_count")
            )
    elif term.field == "is":
        labels = _label_set(message)
        if term.value == "todo":
            hit = _field(message, "id") in todo_ids
        elif term.value == "done" and done_ids is not None:
            hit = _field(message, "id") in done_ids
        elif term.value == "snoozed" and snoozed_ids is not None:
            hit = _field(message, "id") in snoozed_ids
        elif term.value == "all":
            hit = True
        elif term.value == "unread":
            if isinstance(message, MessageLite):
                hit = bool(message.unread) or "UNREAD" in labels
            else:
                hit = bool(message.get("unread")) or "UNREAD" in labels
        elif term.value == "starred":
            if isinstance(message, MessageLite):
                hit = bool(message.starred) or "STARRED" in labels
            else:
                hit = bool(message.get("starred")) or "STARRED" in labels
        elif term.value == "important":
            if isinstance(message, MessageLite):
                hit = bool(message.important) or "IMPORTANT" in labels
            else:
                hit = bool(message.get("important")) or "IMPORTANT" in labels
        else:
            label_map = {
                "sent": "SENT",
                "done": "DONE",
                "inbox": "INBOX",
                "snoozed": "SNOOZED",
                "draft": "DRAFT",
                "trash": "TRASH",
                "spam": "SPAM",
            }
            hit = label_map.get(term.value, "") in labels
    elif term.field in {"before", "after"}:
        ts = _message_timestamp_ms(message)
        if ts is None:
            hit = False
        else:
            try:
                boundary = datetime.strptime(term.value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                boundary_ms = boundary.timestamp() * 1000
            except ValueError:
                hit = False
            else:
                hit = ts < boundary_ms if term.field == "before" else ts >= boundary_ms
    else:
        hit = any(
            _has_match(_field(message, key), term.value)
            for key in ("from", "to", "subject", "snippet", "body")
        )
    return (not hit) if term.exclude else hit


def _match_node(
    message: MessageLite | dict[str, Any],
    node: LocalQueryNode,
    *,
    todo_ids: set[str],
    done_ids: set[str] | None,
    snoozed_ids: set[str] | None,
) -> bool:
    if node.kind == "term" and node.term is not None:
        return _match_term(
            message,
            node.term,
            todo_ids=todo_ids,
            done_ids=done_ids,
            snoozed_ids=snoozed_ids,
        )
    if node.kind == "and":
        return all(
            _match_node(message, child, todo_ids=todo_ids, done_ids=done_ids, snoozed_ids=snoozed_ids)
            for child in node.children
        )
    if node.kind == "or":
        return any(
            _match_node(message, child, todo_ids=todo_ids, done_ids=done_ids, snoozed_ids=snoozed_ids)
            for child in node.children
        )
    return False


def match_local_query(
    message: MessageLite | dict[str, Any],
    parsed: ParsedLocalQuery,
    *,
    todo_ids: Iterable[str] | None = None,
    done_ids: Iterable[str] | None = None,
    snoozed_ids: Iterable[str] | None = None,
) -> bool:
    """语法有效且命中时返回 True。"""
    if not parsed.expression or parsed.error:
        return False
    ids = {str(item) for item in (todo_ids or []) if str(item)}
    done = None if done_ids is None else {str(item) for item in done_ids if str(item)}
    snoozed = None if snoozed_ids is None else {str(item) for item in snoozed_ids if str(item)}
    return _match_node(
        message,
        parsed.expression,
        todo_ids=ids,
        done_ids=done,
        snoozed_ids=snoozed,
    )


def normalize_to_local_query(raw: str) -> str:
    """把 Host/Gmail 风格碎片尽量映射为本地语法展示与过滤串。

    无法识别的 token 保留为裸词；空白分隔项用 AND 连接。
    """
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    # 已是合法本地语法则原样返回
    if not parse_local_query(text).error:
        return text

    pieces: list[str] = []
    for token in text.split():
        lower = token.lower()
        if lower in {"and", "or"}:
            pieces.append(token.upper())
            continue
        # Gmail → 本地
        if lower.startswith("in:inbox"):
            pieces.append("is:inbox")
        elif lower.startswith("in:sent"):
            pieces.append("is:sent")
        elif lower.startswith("in:trash"):
            pieces.append("is:trash")
        elif lower.startswith("in:spam"):
            pieces.append("is:spam")
        elif lower.startswith("in:draft"):
            pieces.append("is:draft")
        elif re.fullmatch(r"newer_than:(\d+)d", lower):
            days = int(re.fullmatch(r"newer_than:(\d+)d", lower).group(1))  # type: ignore[union-attr]
            # 用 after: 近似；无精确时区时按 UTC 日切
            from datetime import timedelta
            day = (datetime.now(timezone.utc) - timedelta(days=max(0, days))).strftime("%Y-%m-%d")
            pieces.append(f"after:{day}")
        elif lower.startswith("is:") or lower.startswith("has:") or lower.startswith("from:") \
                or lower.startswith("to:") or lower.startswith("subject:") or lower.startswith("body:") \
                or lower.startswith("before:") or lower.startswith("after:") or lower.startswith("-"):
            pieces.append(token)
        else:
            # 去掉 Gmail 大括号分组
            cleaned = token.strip("{}()")
            if cleaned:
                pieces.append(cleaned)

    # 在非操作符之间插入 AND
    joined: list[str] = []
    for piece in pieces:
        if not piece:
            continue
        if joined and joined[-1].upper() not in _OPERATORS and piece.upper() not in _OPERATORS:
            joined.append("AND")
        joined.append(piece)
    candidate = " ".join(joined).strip()
    parsed = parse_local_query(candidate)
    if not parsed.error:
        return candidate
    # 兜底：整句当裸关键词（去掉操作符）
    bare = " ".join(p for p in pieces if p.upper() not in _OPERATORS and not p.startswith("-"))
    if bare:
        # 多词用 AND
        terms = bare.split()
        fallback = " AND ".join(terms) if len(terms) > 1 else bare
        if not parse_local_query(fallback).error:
            return fallback
    return "is:inbox"


def build_local_query_from_plan(plan: Any) -> str:
    """从 AskPlan 结构化字段拼本地 query（展示 + 过滤共用）。"""
    parts: list[str] = []
    direction = str(getattr(plan, "direction", None) or "inbox").strip().lower()
    if direction == "inbox":
        parts.append("is:inbox")
    elif direction == "sent":
        parts.append("is:sent")

    for flag in getattr(plan, "gmail_flags", None) or []:
        flag_str = str(flag or "").strip()
        if not flag_str:
            continue
        mapped = normalize_to_local_query(flag_str)
        if mapped and mapped not in parts:
            parts.append(mapped)

    for person in getattr(plan, "people", None) or []:
        if not isinstance(person, dict):
            continue
        hint = str(person.get("name_hint") or "").strip()
        if not hint:
            continue
        role = str(person.get("role") or "either")
        if role == "recipient":
            parts.append(f"to:{hint}")
        else:
            parts.append(f"from:{hint}")

    for topic in getattr(plan, "topics", None) or []:
        if not isinstance(topic, dict):
            continue
        terms = topic.get("search_terms") if isinstance(topic.get("search_terms"), list) else []
        for term in terms:
            text = str(term or "").strip()
            if text:
                # 多词主题保留为单个 any term（空格会破坏解析）
                pieces = text.split()
                if len(pieces) == 1:
                    parts.append(pieces[0])
                else:
                    parts.extend(pieces)

    timeframe = str(getattr(plan, "timeframe", None) or "").strip().lower()
    matched = re.fullmatch(r"(\d{1,3})d", timeframe)
    if matched:
        from datetime import timedelta
        days = max(1, min(int(matched.group(1)), 365))
        day = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        parts.append(f"after:{day}")

    if not parts:
        parts.append("is:inbox")

    # 去重保序并用 AND 连接
    seen: set[str] = set()
    ordered: list[str] = []
    for part in parts:
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(part)
    return normalize_to_local_query(" AND ".join(ordered))


def filter_cached_messages(
    messages: list[MessageLite],
    query_text: str,
    *,
    todo_ids: Iterable[str] | None = None,
    done_ids: Iterable[str] | None = None,
    snoozed_ids: Iterable[str] | None = None,
    limit: int = 45,
) -> tuple[list[MessageLite], ParsedLocalQuery]:
    """按本地 query 过滤缓存消息；返回 (命中列表, 解析结果)。"""
    normalized = normalize_to_local_query(query_text)
    parsed = parse_local_query(normalized)
    if parsed.error or not parsed.expression:
        # 解析失败时不误伤：返回空 + 带错误的 parsed
        return [], ParsedLocalQuery(expression=None, error=parsed.error or "invalid_query", display=normalized)
    try:
        cap = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        cap = 45
    hits: list[MessageLite] = []
    for message in messages:
        if match_local_query(
            message,
            parsed,
            todo_ids=todo_ids,
            done_ids=done_ids,
            snoozed_ids=snoozed_ids,
        ):
            hits.append(message)
            if len(hits) >= cap:
                break
    return hits, ParsedLocalQuery(expression=parsed.expression, error="", display=normalized)
