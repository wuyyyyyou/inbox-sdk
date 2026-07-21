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
) -> list[MessageLite]:
    """执行 Gmail 搜索（元数据优先）并在 0 结果时受控放宽。

    P0：未缓存命中走 metadata 拉取（header/snippet），不默认 full body。
    P1：宽查询优先本地缓存；不足再补 Gmail metadata search。
    """
    from ..mail_providers.gmail.adapter import (
        get_messages_lite_async,
        list_cached_messages_lite,
        live_search_metadata_and_cache,
    )

    try:
        message_cap = max(1, min(int(max_messages), _DEFAULT_MAX_MESSAGES))
    except (TypeError, ValueError):
        message_cap = _DEFAULT_MAX_MESSAGES
    current_queries = list(queries)
    broaden_attempts = max_broaden_attempts if allow_broadening else 0

    # P1：宽查询先吃本地索引（含 Inbox/All-mail 已同步缓存）
    if _queries_are_broad(current_queries):
        try:
            cached = list_cached_messages_lite(mailbox, message_cap)
            cached = _filter_cached_by_window(cached, current_queries)
            # 本地有足够命中则直接返回，避免冷路径再打 Gmail full 列表
            if len(cached) >= min(20, message_cap):
                if search_meta is not None:
                    search_meta["source"] = "local_cache"
                if progress_callback:
                    progress_callback("search_cache_hit", {"cached": len(cached)})
                return cached[:message_cap]
        except Exception as exc:
            _logger.debug("local cache preflight skipped: %s", type(exc).__name__)

    try:
        for attempt in range(broaden_attempts + 1):
            message_ids: list[str] = []
            seen_ids: set[str] = set()
            for query in current_queries:
                query_text = str(query.get("query") or "").strip()
                if not query_text:
                    continue
                try:
                    query_limit = int(query.get("max_results", _DEFAULT_MAX_PER_QUERY))
                except (TypeError, ValueError):
                    query_limit = _DEFAULT_MAX_PER_QUERY
                query_limit = max(1, min(query_limit, message_cap - len(message_ids)))
                # P0：metadata 路径，不默认 fetch format=full
                matched_ids = live_search_metadata_and_cache(mailbox, query_text, query_limit)
                for msg_id in matched_ids:
                    if msg_id not in seen_ids:
                        seen_ids.add(msg_id)
                        message_ids.append(msg_id)
                    if len(message_ids) >= message_cap:
                        break
                if len(message_ids) >= message_cap:
                    break

            messages = await get_messages_lite_async(mailbox, message_ids)
            if messages:
                if search_meta is not None:
                    search_meta["source"] = "gmail_metadata"
                if attempt > 0:
                    _logger.info("Search broadened level %d, found %d messages", attempt, len(messages))
                return messages
            if attempt < broaden_attempts:
                current_queries = [_broaden_query(q, attempt + 1) for q in queries]
                _logger.info(
                    "Search attempt %d returned 0 results, broadening to level %d",
                    attempt + 1,
                    attempt + 1,
                )
    except Exception as exc:
        # 仅实时 Gmail 调用失败时退回缓存；正常 0 结果不混入过期邮件。
        _logger.warning("Gmail search failed; using cache: %s", type(exc).__name__)
        if search_meta is not None:
            search_meta["source"] = "cache_fallback"
            search_meta["error_type"] = type(exc).__name__
        cached = list_cached_messages_lite(mailbox, message_cap)
        if progress_callback:
            progress_callback("search_fallback", {"source": "cache", "cached": len(cached)})
        return cached

    return []
