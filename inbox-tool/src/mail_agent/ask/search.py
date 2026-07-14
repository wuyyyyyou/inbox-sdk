"""Ask pipeline search: code-based query building + adaptive search execution.

build_queries(): constructs validated Gmail queries from AskPlan structured params.
  - People: resolves name_hint → email via contact memory lookup
  - Topics: uses search_terms (NOT concept) for Gmail OR groups
  - Applies direction, timeframe, and syntax normalization

execute_search(): runs Gmail query search with adaptive broadening.
  - 0 results → progressively broaden the query
  - Caches matched messages, then reads them as MessageLite objects
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..domain.types import MessageLite
from .planner import AskPlan

_logger = logging.getLogger(__name__)

# 中文注释：Ask 搜索默认最多读取的邮件数量，避免旧 scan_plan dict 被当成 max_threads。
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


async def execute_search(
    mailbox: str,
    queries: list[dict[str, Any]],
    *,
    progress_callback: Any = None,
    max_broaden_attempts: int = 2,
    max_messages: int = _DEFAULT_MAX_MESSAGES,
) -> list[MessageLite]:
    """Execute Gmail search with adaptive broadening.

    Attempt 1: search as-is
    Attempt 2 (0 results): broaden level 1 (drop person/topic filters)
    Attempt 3 (0 results): broaden level 2 (keep only direction + timeframe)

    Uses Gmail query search, caches matched messages, and returns MessageLite.
    """
    from ..mail_providers.gmail.adapter import get_messages_lite_async, live_search_and_cache

    # 中文注释：前端 Scan Plan 的数量上限必须覆盖每个查询，防止多查询合并后超量读取。
    try:
        message_cap = max(1, min(int(max_messages), _DEFAULT_MAX_MESSAGES))
    except (TypeError, ValueError):
        message_cap = _DEFAULT_MAX_MESSAGES
    current_queries = list(queries)

    for attempt in range(max_broaden_attempts + 1):
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
            matched_ids = live_search_and_cache(mailbox, query_text, query_limit)
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
            if attempt > 0:
                _logger.info("Search broadened level %d, found %d messages", attempt, len(messages))
            return messages

        if attempt < max_broaden_attempts:
            current_queries = [_broaden_query(q, attempt + 1) for q in queries]
            _logger.info("Search attempt %d returned 0 results, broadening to level %d",
                         attempt + 1, attempt + 1)

    return []
