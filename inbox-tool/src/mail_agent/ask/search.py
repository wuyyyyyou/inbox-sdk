"""Ask pipeline search: code-based query building + adaptive search execution.

build_queries(): constructs validated Gmail queries from AskPlan structured params.
  - People: resolves name_hint → email via contact memory lookup
  - Topics: uses search_terms (NOT concept) for Gmail OR groups
  - Applies direction, timeframe, and syntax normalization

execute_search(): runs Gmail query search with optional adaptive broadening.
  - 0 results → progressively broaden the query when the caller permits it
  - Caches matched messages, then reads them as MessageLite objects
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..domain.types import MessageLite
from .planner import AskPlan

_logger = logging.getLogger(__name__)

# Ask 搜索默认最多读取的邮件数量，避免旧 scan_plan dict 被当成 max_threads。
_DEFAULT_MAX_MESSAGES = 200
_DEFAULT_MAX_PER_QUERY = 100


# ── Contact memory lookup ──────────────────────────────────────────────

async def _resolve_people(
    name_hints: list[dict[str, str]],
    mailbox: str,
) -> dict[str, list[str]]:
    """Resolve person name_hints to email addresses via contact memory.

    Returns {name_hint: [email1, email2, ...]}.
    Empty list means no match found — caller should use name_hint directly
    in Gmail partial-name search.
    """
    if not name_hints:
        return {}

    try:
        from ..contact_memory.store import list_contact_memories
    except ImportError:
        return {p["name_hint"]: [] for p in name_hints}

    result: dict[str, list[str]] = {}

    # Load all contact memories once
    try:
        all_contacts = await list_contact_memories(mailbox)
    except Exception:
        all_contacts = []

    for person in name_hints:
        hint = str(person.get("name_hint") or "").strip().lower()
        if not hint:
            result[hint] = []
            continue

        matches: list[str] = []
        for cm in all_contacts:
            if not cm or not cm.contact_email:
                continue
            display = (cm.display_name or "").lower()
            email_local = cm.contact_email.lower().split("@")[0]

            # Three-tier match: exact display name > substring > email local part
            if hint == display:
                matches.append(cm.contact_email)
            elif hint in display:
                matches.append(cm.contact_email)
            elif hint in email_local:
                matches.append(cm.contact_email)

        # Dedup while preserving order
        seen: set[str] = set()
        result[hint] = []
        for e in matches:
            if e not in seen:
                seen.add(e)
                result[hint].append(e)

    return result


# ── Query building ─────────────────────────────────────────────────────

def _quote_gmail_term(term: str) -> str:
    """Quote a term for Gmail search if it contains spaces."""
    term = term.strip()
    if not term:
        return ""
    if " " in term and not (term.startswith('"') and term.endswith('"')):
        return '"' + term.replace('"', "") + '"'
    return term


async def build_queries(plan: AskPlan, mailbox: str) -> list[dict[str, Any]]:
    """Build validated Gmail search queries from an AskPlan.

    Returns a list of query dicts compatible with execute_search():
      [{"query": "...", "purpose": "...", "max_results": N, "priority": "high"}]

    Does NOT write Gmail syntax from scratch — constructs it deterministically
    from structured parameters.
    """
    from ..core.scan import _normalize_gmail_query

    # Resolve people
    resolved = await _resolve_people(plan.people, mailbox)

    queries: list[dict[str, Any]] = []

    # Build person-based query parts
    # Respect role: sender→from: recipient→to: either→both from: and to:
    person_from: list[str] = []
    person_to: list[str] = []
    for person in plan.people:
        hint = str(person.get("name_hint") or "").strip()
        if not hint:
            continue
        role = str(person.get("role") or "either")
        emails = resolved.get(hint.lower(), [])
        if emails:
            if role in ("sender", "either"):
                person_from.extend(f"from:{e}" for e in emails)
            if role in ("recipient", "either"):
                person_to.extend(f"to:{e}" for e in emails)
        else:
            # Partial name match fallback
            quoted = _quote_gmail_term(hint)
            if role in ("sender", "either"):
                person_from.append(f"from:{quoted}")
            if role in ("recipient", "either"):
                person_to.append(f"to:{quoted}")

    # Merge from and to into unified person OR group(s)
    person_parts: list[str] = person_from + person_to

    # Build topic-based search terms
    topic_terms: list[str] = []
    for topic in plan.topics:
        terms = topic.get("search_terms", [])
        if isinstance(terms, list):
            for t in terms:
                t_str = str(t).strip()
                if t_str:
                    topic_terms.append(_quote_gmail_term(t_str))

    # Build base filters
    filters: list[str] = []

    # Direction
    direction = plan.direction or "inbox"
    if direction == "inbox":
        filters.append("in:inbox")
    elif direction == "sent":
        filters.append("in:sent")
    # "all" → no direction filter

    # Timeframe
    timeframe = plan.timeframe or "30d"
    filters.append(f"newer_than:{timeframe}")

    # Gmail flags from planner (e.g. is:unread, has:attachment)
    for flag in (plan.gmail_flags or []):
        flag_str = str(flag).strip()
        if flag_str and flag_str not in " ".join(filters):
            filters.append(flag_str)

    base_filter = " ".join(filters)

    # Combine into query(ies)
    if person_parts and topic_terms:
        # Both people and topics: create a combined query
        person_or = "{" + " ".join(person_parts) + "}" if len(person_parts) > 1 else person_parts[0]
        topic_or = "{" + " ".join(topic_terms) + "}" if len(topic_terms) > 1 else topic_terms[0]
        query_str = f"{person_or} {topic_or} {base_filter}"
        queries.append({
            "query": _normalize_gmail_query(query_str),
            "purpose": "people_and_topics",
            "max_results": _DEFAULT_MAX_PER_QUERY,
            "priority": "high",
        })
    elif person_parts:
        # Only people
        person_or = "{" + " ".join(person_parts) + "}" if len(person_parts) > 1 else person_parts[0]
        query_str = f"{person_or} {base_filter}"
        queries.append({
            "query": _normalize_gmail_query(query_str),
            "purpose": "people_search",
            "max_results": _DEFAULT_MAX_PER_QUERY,
            "priority": "high",
        })
    elif topic_terms:
        # Only topics
        topic_or = "{" + " ".join(topic_terms) + "}" if len(topic_terms) > 1 else topic_terms[0]
        query_str = f"{topic_or} {base_filter}"
        queries.append({
            "query": _normalize_gmail_query(query_str),
            "purpose": "topic_search",
            "max_results": _DEFAULT_MAX_PER_QUERY,
            "priority": "high",
        })
    else:
        # No people, no topics — broad sweep
        query_str = base_filter
        queries.append({
            "query": _normalize_gmail_query(query_str),
            "purpose": "broad_sweep",
            "max_results": _DEFAULT_MAX_PER_QUERY,
            "priority": "high",
        })

    # Fallback: if somehow no queries were built, use a safe default
    if not queries:
        queries.append({
            "query": "newer_than:30d -in:sent -in:draft",
            "purpose": "fallback_broad",
            "max_results": _DEFAULT_MAX_PER_QUERY,
            "priority": "high",
        })

    return queries


# ── Adaptive search execution ──────────────────────────────────────────

def _broaden_query(query: dict[str, Any], level: int) -> dict[str, Any]:
    """Remove restrictive filters to broaden the search scope.

    Level 1: drop person-specific filters (from:, to:) and OR groups
    Level 2: keep only direction + timeframe, preserve leading - for exclusion
    """
    q = str(query.get("query", ""))
    max_results = int(query.get("max_results", _DEFAULT_MAX_PER_QUERY))

    if level == 1:
        # Remove from:/to: patterns — handle both plain and quoted values
        # from:alice  OR  from:"Alice Smith"
        q = re.sub(r"\bfrom:(?:\"[^\"]+\"|\S+)", "", q)
        q = re.sub(r"\bto:(?:\"[^\"]+\"|\S+)", "", q)
        # Remove OR groups {term1 term2 ...}
        q = re.sub(r"\{[^}]+\}", "", q)
        q = re.sub(r"\s+", " ", q).strip()
    elif level >= 2:
        # Keep only direction + timeframe
        parts: list[str] = []
        newer_match = re.search(r"newer_than:\S+", q)
        if newer_match:
            parts.append(newer_match.group(0))
        # Preserve leading - for exclusion filters (-in:sent, -in:draft)
        in_matches = re.findall(r"-?in:\S+", q)
        parts.extend(in_matches)
        cat_matches = re.findall(r"-?category:\S+", q)
        parts.extend(cat_matches)
        q = " ".join(parts) if parts else "newer_than:30d -in:sent -in:draft"

    return {
        "query": q,
        "purpose": f"broadened_level_{level}",
        "max_results": max_results,
        "priority": "medium",
    }


def _is_broad_query(query_text: str) -> bool:
    """宽查询：仅方向/时间/标签类约束，无 from/to/主题词 OR 组。"""
    q = (query_text or "").strip().casefold()
    if not q:
        return True
    # 强约束：人物/话题/精确短语 → 必须走 Gmail live
    if re.search(r"\bfrom:|\bto:|\bcc:|\bbcc:|\bsubject:", q):
        return False
    if "{" in q or '"' in q:
        return False
    return True


def _queries_are_broad(queries: list[dict[str, Any]]) -> bool:
    texts = [str(q.get("query") or "").strip() for q in queries if str(q.get("query") or "").strip()]
    return bool(texts) and all(_is_broad_query(t) for t in texts)


def _filter_cached_by_window(messages: list[MessageLite], queries: list[dict[str, Any]]) -> list[MessageLite]:
    """按查询中的 newer_than 粗滤本地缓存（仅 epoch 时间，无 Gmail 语法完整复刻）。"""
    days = 0
    for query in queries:
        text = str(query.get("query") or "")
        match = re.search(r"newer_than:(\d+)d", text, re.IGNORECASE)
        if match:
            days = max(days, int(match.group(1)))
    if days <= 0:
        return messages
    import time
    cutoff_ms = int((time.time() - days * 86400) * 1000)
    filtered: list[MessageLite] = []
    for msg in messages:
        try:
            ts = int(msg.internal_date or 0)
        except (TypeError, ValueError):
            ts = 0
        if ts >= cutoff_ms:
            filtered.append(msg)
    return filtered


async def execute_search(
    mailbox: str,
    queries: list[dict[str, Any]],
    *,
    progress_callback: Any = None,
    max_broaden_attempts: int = 2,
    max_messages: int = _DEFAULT_MAX_MESSAGES,
    allow_broadening: bool = True,
    search_meta: dict[str, str] | None = None,
    todo_ids: list[str] | None = None,
    local_query: str = "",
) -> list[MessageLite]:
    """只扫本地缓存（cache-only），不再调用 Gmail API。

    ``queries`` 仍可携带旧 Gmail 串，会映射为本地语法再过滤。
    缓存为空返回 []，由上层提示用户刷新收件箱。
    """
    from mail_agent.local_query import filter_cached_messages, normalize_to_local_query
    from ..mail_providers.gmail.adapter import list_cached_messages_lite

    _ = max_broaden_attempts, allow_broadening  # 保留签名兼容；cache-only 不再放宽打 Gmail
    try:
        message_cap = max(1, min(int(max_messages), _DEFAULT_MAX_MESSAGES))
    except (TypeError, ValueError):
        message_cap = _DEFAULT_MAX_MESSAGES

    # 优先使用显式 local_query；否则合并 queries 并映射本地语法
    raw_parts = [str(local_query or "").strip()]
    for item in queries or []:
        text = str((item or {}).get("query") or "").strip()
        if text:
            raw_parts.append(text)
    combined = " ".join(part for part in raw_parts if part).strip()
    scan_query = normalize_to_local_query(combined or "is:inbox")
    if search_meta is not None:
        search_meta["source"] = "local_cache"
        search_meta["scan_query"] = scan_query

    try:
        cached = list_cached_messages_lite(mailbox, message_cap)
    except Exception as exc:
        _logger.warning("list_cached_messages_lite failed: %s", type(exc).__name__)
        if search_meta is not None:
            search_meta["source"] = "local_cache"
            search_meta["cache_empty"] = "1"
            search_meta["error_type"] = type(exc).__name__
        if progress_callback:
            progress_callback("search_cache_empty", {"cached": 0})
        return []

    if not cached:
        if search_meta is not None:
            search_meta["cache_empty"] = "1"
        if progress_callback:
            progress_callback("search_cache_empty", {"cached": 0})
        return []

    hits, parsed = filter_cached_messages(
        cached,
        scan_query,
        todo_ids=todo_ids or [],
        limit=message_cap,
    )
    if search_meta is not None:
        search_meta["scan_query"] = parsed.display or scan_query
    if progress_callback:
        progress_callback(
            "search_cache_hit",
            {"cached": len(cached), "matched": len(hits), "scan_query": parsed.display or scan_query},
        )
    return hits
