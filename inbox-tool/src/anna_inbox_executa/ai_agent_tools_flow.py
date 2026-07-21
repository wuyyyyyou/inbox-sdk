"""Host Agent 可选细粒度工具：选型在 Host，执行仍在本 Executa。

侧栏 App 通过 anna.agent.session + systemPrompt 让 Host 选型；
本模块只实现白名单工具，不含 apply/send 等 mutation。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from anna_inbox_executa.sampling_tools import _build_sampling_for_run

# Host agent.tools 白名单（与 manifest / App 声明对齐）
AI_AGENT_TOOL_NAMES = frozenset({
    "search_email",
    "read_email",
    "ai_summarize_thread",
    "ai_draft_reply",
    "ai_revise_draft",
    "ai_compose_new",
    "ai_batch_draft",
    "propose_inbox_actions",
    "ai_remember_preference",
})

_SEARCH_MAX_CALLS = 6
_SEARCH_MAX_CANDIDATES = 45
_SEARCH_SESSION_TTL_SECONDS = 30 * 60
_SEARCH_BUDGETS: dict[str, dict[str, Any]] = {}
_SEARCH_BUDGET_LOCK = threading.Lock()
_THREAD_REF_MESSAGES: dict[str, tuple[str, float]] = {}


def _uses_chinese(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _merge_ui_context(arguments: dict[str, Any]) -> dict[str, Any]:
    """合并 Host 传入的 ui_context 与扁平字段，便于模型少填嵌套对象。"""
    raw = arguments.get("ui_context")
    context: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    mailbox = str(arguments.get("mailbox") or context.get("mailbox") or "").strip()
    if mailbox:
        context["mailbox"] = mailbox
    message_id = str(arguments.get("message_id") or "").strip()
    thread_id = str(arguments.get("thread_id") or "").strip()
    subject = str(arguments.get("subject") or "").strip()
    current = context.get("current_thread") if isinstance(context.get("current_thread"), dict) else {}
    if message_id or thread_id or subject:
        merged_current = {
            "kind": str(current.get("kind") or "thread") if (message_id or thread_id) else str(current.get("kind") or "none"),
            "mailbox": str(current.get("mailbox") or mailbox),
            "message_id": message_id or str(current.get("message_id") or ""),
            "thread_id": thread_id or str(current.get("thread_id") or message_id or ""),
            "subject": subject or str(current.get("subject") or ""),
            "snippet": str(current.get("snippet") or ""),
        }
        if merged_current["message_id"] or merged_current["thread_id"]:
            merged_current["kind"] = "thread"
        context["current_thread"] = merged_current
    # 批量：允许 selected_threads 直接传入
    selected = arguments.get("selected_threads")
    if isinstance(selected, list) and selected:
        context["selected_threads"] = selected
    draft_body = str(arguments.get("draft_body") or arguments.get("last_draft_body") or "").strip()
    if draft_body:
        context["last_draft"] = {
            "body": draft_body[:8000],
            "source": str(arguments.get("draft_source") or "assistant_artifact"),
        }
    if arguments.get("display_range_days") is not None:
        try:
            context["display_range_days"] = int(arguments.get("display_range_days"))
        except (TypeError, ValueError):
            pass
    if arguments.get("max_messages") is not None:
        try:
            context["max_messages"] = int(arguments.get("max_messages"))
        except (TypeError, ValueError):
            pass
    return context


def _user_text(arguments: dict[str, Any]) -> str:
    return str(
        arguments.get("user_text")
        or arguments.get("query")
        or arguments.get("request")
        or arguments.get("prompt")
        or ""
    ).strip()


def _search_budget_key(arguments: dict[str, Any], ui_context: dict[str, Any]) -> str:
    """以匿名 conversation_id 隔离每个侧栏会话的检索额度，不写入持久存储。"""
    conversation_id = str(arguments.get("conversation_id") or ui_context.get("conversation_id") or "").strip()
    mailbox = str(arguments.get("mailbox") or ui_context.get("mailbox") or "").strip().lower()
    if not conversation_id or not mailbox:
        return ""
    return f"{mailbox}|{conversation_id[:160]}"


def _reserve_search_budget(arguments: dict[str, Any], ui_context: dict[str, Any], requested_limit: Any) -> tuple[int, dict[str, int]]:
    """原子预留一次搜索与候选额度，防止 Host 多轮工具调用扩大扫描范围。"""
    key = _search_budget_key(arguments, ui_context)
    if not key:
        raise ValueError("search_email requires mailbox and conversation_id from ui_context")
    try:
        requested = int(requested_limit or 12)
    except (TypeError, ValueError):
        requested = 12
    now = time.monotonic()
    with _SEARCH_BUDGET_LOCK:
        expired = [item for item, value in _SEARCH_BUDGETS.items() if now - float(value.get("updated_at") or 0) > _SEARCH_SESSION_TTL_SECONDS]
        for item in expired:
            _SEARCH_BUDGETS.pop(item, None)
        budget = _SEARCH_BUDGETS.setdefault(key, {"calls": 0, "candidates": 0, "updated_at": now})
        remaining_calls = max(0, _SEARCH_MAX_CALLS - int(budget["calls"]))
        remaining_candidates = max(0, _SEARCH_MAX_CANDIDATES - int(budget["candidates"]))
        if remaining_calls <= 0 or remaining_candidates <= 0:
            raise ValueError("search_email budget exhausted for this conversation")
        limit = max(1, min(requested, remaining_candidates, _SEARCH_MAX_CANDIDATES))
        budget["calls"] += 1
        budget["candidates"] += limit
        budget["updated_at"] = now
        return limit, {
            "searches_used": int(budget["calls"]),
            "searches_remaining": max(0, _SEARCH_MAX_CALLS - int(budget["calls"])),
            "candidates_reserved": int(budget["candidates"]),
            "candidates_remaining": max(0, _SEARCH_MAX_CANDIDATES - int(budget["candidates"])),
        }


def _thread_ref(thread_id: str) -> str:
    """生成前端 Markdown renderer 可识别且不含空格的稳定线程引用。"""
    return f"THREAD_REF_{str(thread_id or '').strip()}"


def _remember_thread_ref(mailbox: str, thread_id: str, message_id: str) -> None:
    """仅在进程内保存 thread_ref 到最新 message_id 的短映射，供 read_email 按需取正文。"""
    if not mailbox or not thread_id or not message_id:
        return
    now = time.monotonic()
    with _SEARCH_BUDGET_LOCK:
        expired = [key for key, value in _THREAD_REF_MESSAGES.items() if now - value[1] > _SEARCH_SESSION_TTL_SECONDS]
        for key in expired:
            _THREAD_REF_MESSAGES.pop(key, None)
        _THREAD_REF_MESSAGES[f"{mailbox}|{thread_id}"] = (message_id, now)


def _message_id_for_thread_ref(mailbox: str, thread_id: str) -> str:
    """读取短映射；过期/未知时回退原值，兼容直接传 message_id 的调用。"""
    with _SEARCH_BUDGET_LOCK:
        mapped = _THREAD_REF_MESSAGES.get(f"{mailbox}|{thread_id}")
    return str(mapped[0]) if mapped else thread_id


def _search_query(arguments: dict[str, Any]) -> str:
    """拼接 Host 提供的 Gmail 搜索词，限制长度但不试图重写 Gmail 语法。"""
    about = " ".join(str(arguments.get("about") or "").split())[:240]
    filter_text = " ".join(str(arguments.get("filter") or "").split())[:240]
    if not about and not filter_text:
        raise ValueError("search_email requires about or filter")
    return " ".join(item for item in (about, filter_text) if item)


_SEARCH_DEFAULT_MASK = ("date", "participants", "subject", "bodySnippet")
_SEARCH_ALLOWED_MASK = frozenset(_SEARCH_DEFAULT_MASK)
# 单次最多扫这么多索引条，避免大邮箱把 RPC/CPU 拖到数十秒
_SEARCH_INDEX_SCAN_CAP = 120
_SEARCH_SNIPPET_CHARS = 140
_SEARCH_SUBJECT_CHARS = 120
_SEARCH_FROM_CHARS = 100


def _search_read_mask(arguments: dict[str, Any]) -> list[str]:
    """search_email 只允许轻量字段；bodyFull 一律拒绝（必须走 read_email）。"""
    raw = arguments.get("readMask") if isinstance(arguments.get("readMask"), list) else []
    mask = [str(item) for item in raw if str(item) in _SEARCH_ALLOWED_MASK]
    return mask or list(_SEARCH_DEFAULT_MASK)


def _search_email(arguments: dict[str, Any], ui_context: dict[str, Any]) -> dict[str, Any]:
    """只扫本地索引缓存（不调用 Gmail、不读 bodyFull），返回轻量字段 + scan_query。

    默认字段：date / participants / subject / bodySnippet。
    缓存为空时返回 0 结果并提示刷新。
    """
    from mail_agent.local_query import filter_cached_messages, normalize_to_local_query
    from mail_agent.mail_providers.gmail.adapter import (
        list_cached_messages_lite,
        normalize_mailbox,
    )

    mailbox = normalize_mailbox(str(arguments.get("mailbox") or ui_context.get("mailbox") or ""))
    # 未指定 limit 时默认 12，减少 Host 上下文体积
    requested_limit = arguments.get("limit")
    if requested_limit is None or requested_limit == "":
        requested_limit = 12
    limit, budget = _reserve_search_budget(arguments, ui_context, requested_limit)
    limit = max(1, min(limit, 20))
    raw_query = _search_query(arguments)
    scan_query = normalize_to_local_query(raw_query)
    mask = _search_read_mask(arguments)
    # 前端 workflow 标记：is:todo 依赖 todo_message_ids，不在 Gmail label 中。
    todo_raw = ui_context.get("todo_message_ids") if isinstance(ui_context, dict) else None
    if not isinstance(todo_raw, list):
        todo_raw = arguments.get("todo_message_ids") if isinstance(arguments.get("todo_message_ids"), list) else []
    todo_ids = [str(item) for item in todo_raw if str(item).strip()]

    # 只读索引摘要（MessageLite），不拉全文缓存文件
    scan_pool = min(_SEARCH_INDEX_SCAN_CAP, max(limit * 5, 40))
    cached = list_cached_messages_lite(mailbox, scan_pool)
    if not cached:
        return {
            "kind": "search",
            "mailbox": mailbox,
            "query": scan_query,
            "scan_query": scan_query,
            "scan_source": "cache",
            "cache_empty": True,
            "readMask": mask,
            "results": [],
            "count": 0,
            "budget": budget,
            "assistant_text": (
                "Local inbox cache is empty. Refresh the inbox first, then try again."
            ),
        }

    hits, parsed = filter_cached_messages(
        cached,
        scan_query,
        todo_ids=todo_ids,
        limit=limit,
    )
    results: list[dict[str, Any]] = []
    for message in hits:
        message_id = str(message.message_id or "")
        thread_id = str(message.thread_id or message_id)
        if not message_id:
            continue
        _remember_thread_ref(mailbox, thread_id, message_id)
        # 固定只返回轻量字段，绝不带 bodyFull / body_text / labels 大数组
        row: dict[str, Any] = {
            "thread_ref": _thread_ref(thread_id),
            "message_id": message_id,
            "thread_id": thread_id,
        }
        if "date" in mask:
            row["date"] = str(message.internal_date or "")[:32]
        if "participants" in mask:
            row["from"] = str(message.from_addr or "")[:_SEARCH_FROM_CHARS]
            to_value = str(message.to_addr or "")[:80]
            if to_value:
                row["to"] = to_value
        if "subject" in mask:
            row["subject"] = str(message.subject or "")[:_SEARCH_SUBJECT_CHARS]
        if "bodySnippet" in mask:
            row["bodySnippet"] = str(message.snippet or "")[:_SEARCH_SNIPPET_CHARS]
        results.append(row)
    display_query = parsed.display or scan_query
    return {
        "kind": "search",
        "mailbox": mailbox,
        "query": display_query,
        "scan_query": display_query,
        "scan_source": "cache",
        "cache_empty": False,
        "readMask": mask,
        "results": results,
        "count": len(results),
        "budget": budget,
    }


def _read_email(arguments: dict[str, Any], ui_context: dict[str, Any]) -> dict[str, Any]:
    """按 readMask 返回字段；只有 bodyFull 触发 Gmail 正文读取。"""
    from mail_agent.mail_providers.gmail.adapter import (
        get_message_detail,
        normalize_mailbox,
        read_message,
    )

    mailbox = normalize_mailbox(str(arguments.get("mailbox") or ui_context.get("mailbox") or ""))
    raw_ref = str(arguments.get("thread_ref") or arguments.get("message_id") or "").strip()
    thread_id = raw_ref.removeprefix("THREAD_REF_")
    if not thread_id:
        raise ValueError("read_email requires thread_ref or message_id")
    explicit_message_id = str(arguments.get("message_id") or "").strip()
    message_id = explicit_message_id or _message_id_for_thread_ref(mailbox, thread_id)
    raw_mask = arguments.get("readMask") if isinstance(arguments.get("readMask"), list) else []
    allowed_mask = {"date", "participants", "subject", "bodySnippet", "bodyFull"}
    mask = [str(item) for item in raw_mask if str(item) in allowed_mask] or ["date", "participants", "subject", "bodySnippet"]
    try:
        message = read_message(mailbox, message_id)
    except Exception:
        message = {}
    if not isinstance(message, dict):
        message = {}
    result: dict[str, Any] = {
        "kind": "email",
        "thread_ref": _thread_ref(str(message.get("thread_id") or thread_id)),
        "message_id": str(message.get("id") or message_id),
        "thread_id": str(message.get("thread_id") or thread_id),
    }
    if "date" in mask:
        result["date"] = str(message.get("date") or message.get("internal_date") or "")[:64]
    if "participants" in mask:
        # 收件人字段对 Host 选型帮助有限，优先 from；to 仅保留短摘要。
        result["from"] = str(message.get("from") or "")[:160]
        to_value = str(message.get("to") or "")[:120]
        if to_value:
            result["to"] = to_value
    if "subject" in mask:
        result["subject"] = str(message.get("subject") or "")[:160]
    if "bodySnippet" in mask:
        result["bodySnippet"] = str(message.get("snippet") or "")[:280]
    if "bodyFull" in mask:
        detail = get_message_detail(mailbox, message_id)
        if detail is None:
            raise ValueError("email_not_found")
        # 全文仍给 Host，但硬截断，避免单封长线程撑爆 session 上下文。
        result["bodyFull"] = str(detail.body_text or "")[:3500]
    return result


def _propose_inbox_actions(arguments: dict[str, Any], ui_context: dict[str, Any]) -> dict[str, Any]:
    """把 Host 已判断的候选封装为确认卡，绝不再次搜索或执行 Gmail mutation。"""
    mailbox = str(arguments.get("mailbox") or ui_context.get("mailbox") or "").strip()
    raw_items = arguments.get("items") if isinstance(arguments.get("items"), list) else []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_items[:20]:
        if not isinstance(raw, dict):
            continue
        message_id = str(raw.get("message_id") or raw.get("thread_ref") or "").strip().removeprefix("THREAD_REF_")
        thread_id = str(raw.get("thread_id") or message_id).strip()
        item_mailbox = str(raw.get("mailbox") or mailbox).strip()
        if not item_mailbox or not message_id:
            continue
        key = f"{item_mailbox}|{message_id}"
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "mailbox": item_mailbox,
            "message_id": message_id,
            "thread_id": thread_id,
            "subject": str(raw.get("subject") or "")[:120],
            "default_selected": raw.get("default_selected") is not False,
        })
    if not items:
        raise ValueError("propose_inbox_actions requires searched candidate items")
    language = _language(arguments, "")
    is_zh = language == "zh"
    return {
        "kind": "propose",
        "assistant_text": (
            f"已准备 {len(items)} 封低优先级邮件的整理建议，请在下方确认。"
            if is_zh else f"Prepared an organization proposal for {len(items)} low-priority emails. Confirm it below."
        ),
        "proposed_actions": {
            "step_index": 1,
            "step_title": str(arguments.get("step_title") or ("整理低优先级邮件" if is_zh else "Organize low-priority emails"))[:80],
            "rationale": str(arguments.get("rationale") or ("Selected from this search pass." if not is_zh else "来自本轮搜索结果。"))[:240],
            "primary_action": "mark_done",
            "allowed_actions": ["mark_done", "archive", "trash"],
            "items": items,
            "requires_user_confirmation": True,
            "language": language,
        },
    }


def _language(arguments: dict[str, Any], user_text: str) -> str:
    hint = str(arguments.get("language") or arguments.get("language_hint") or "").strip().lower()
    if hint.startswith("zh"):
        return "zh"
    if hint.startswith("en"):
        return "en"
    return "zh" if _uses_chinese(user_text) else "en"


# Host / 前端渲染只依赖这些键；其余内部字段不进入 session 上下文。
_PUBLIC_OUTCOME_KEYS = (
    "kind",
    "assistant_text",
    "error",
    "clarify",
    "clarification",
    "artifact",
    "artifacts",
    "batch_failures",
    "mail_context",
    "proposed_actions",
    "requires_user_confirmation",
    "memory",
    "scan_query",
    "scan_source",
    "query",
    "results",
    "count",
    "cache_empty",
)


def _public_outcome(outcome: dict[str, Any]) -> dict[str, Any]:
    """只保留 Host session / 侧栏渲染需要的字段，剔除 route、sampling 等内部细节。"""
    if not isinstance(outcome, dict):
        return {"kind": "error", "assistant_text": "invalid tool outcome", "error": "invalid_outcome"}
    public: dict[str, Any] = {}
    for key in _PUBLIC_OUTCOME_KEYS:
        if key not in outcome:
            continue
        value = outcome[key]
        if value is None:
            continue
        public[key] = value
    if "kind" not in public:
        public["kind"] = "error"
        public.setdefault("error", "invalid_outcome")
        public.setdefault("assistant_text", "invalid tool outcome")
    return public


async def handle_ai_agent_tool(tool: str, arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """执行 Host 选定的细粒度工具；内部可继续用 Sampling 生成正文。"""
    from mail_agent.ai_turn.personalization import format_memory_summary_for_prompt
    from mail_agent.ai_turn.runner import tool_summarize_thread
    from mail_agent.ai_turn.tools import (
        tool_batch_draft,
        tool_compose_new,
        tool_draft_reply,
        tool_remember_preference,
        tool_revise_draft,
    )

    if tool not in AI_AGENT_TOOL_NAMES:
        return {"success": False, "error": f"unknown_ai_agent_tool:{tool}"}

    user_text = _user_text(arguments)
    if not user_text and tool not in {"ai_remember_preference", "search_email", "read_email", "propose_inbox_actions"}:
        return {
            "success": False,
            "kind": "error",
            "error": "missing_user_text",
            "assistant_text": "Missing user_text for AI tool.",
        }

    ui_context = _merge_ui_context(arguments)
    language = _language(arguments, user_text)

    # 搜索与按需读取是纯 Gmail I/O：不创建 Sampling 预算，也不传 max_tokens。
    if tool == "search_email":
        try:
            return {"success": True, "tool": tool, "data": _search_email(arguments, ui_context)}
        except Exception as exc:
            return {"success": False, "tool": tool, "error": str(exc)[:240]}
    if tool == "read_email":
        try:
            return {"success": True, "tool": tool, "data": _read_email(arguments, ui_context)}
        except Exception as exc:
            return {"success": False, "tool": tool, "error": str(exc)[:240]}
    if tool == "propose_inbox_actions":
        try:
            return {"success": True, "tool": tool, "data": _propose_inbox_actions(arguments, ui_context)}
        except Exception as exc:
            return {"success": False, "tool": tool, "error": str(exc)[:240]}

    memory_summary = await format_memory_summary_for_prompt()
    sampling = _build_sampling_for_run(arguments, invoke_id)

    # remember 可不依赖 LLM
    if tool == "ai_remember_preference":
        preference = str(arguments.get("preference") or arguments.get("text") or user_text).strip()
        outcome = await tool_remember_preference(
            preference or user_text,
            language=language,
            params={"preference": preference or user_text},
        )
        return {"success": True, "tool": tool, "data": _public_outcome(outcome)}

    if tool == "ai_summarize_thread":
        outcome = await tool_summarize_thread(
            user_text,
            ui_context,
            language=language,
            sampling_create_message=sampling,
            memory_summary=memory_summary,
        )
        return {"success": outcome.get("kind") != "error", "tool": tool, "data": _public_outcome(outcome)}

    if tool == "ai_draft_reply":
        mode = str(arguments.get("mode") or "draft_reply").strip()
        if mode not in {"draft_reply", "summarize_then_draft"}:
            mode = "draft_reply"
        outcome = await tool_draft_reply(
            user_text,
            ui_context,
            language=language,
            sampling_create_message=sampling,
            memory_summary=memory_summary,
            mode=mode,
        )
        return {"success": outcome.get("kind") != "error", "tool": tool, "data": _public_outcome(outcome)}

    if tool == "ai_revise_draft":
        outcome = await tool_revise_draft(
            user_text,
            ui_context,
            language=language,
            sampling_create_message=sampling,
            memory_summary=memory_summary,
        )
        return {"success": outcome.get("kind") != "error", "tool": tool, "data": _public_outcome(outcome)}

    if tool == "ai_compose_new":
        outcome = await tool_compose_new(
            user_text,
            ui_context,
            arguments,
            language=language,
            sampling_create_message=sampling,
            memory_summary=memory_summary,
        )
        return {"success": outcome.get("kind") != "error", "tool": tool, "data": _public_outcome(outcome)}

    if tool == "ai_batch_draft":
        mode = str(arguments.get("mode") or "batch_draft").strip()
        if mode == "batch_outreach":
            from mail_agent.ai_turn.tools import tool_batch_outreach

            outcome = await tool_batch_outreach(
                user_text,
                ui_context,
                language=language,
                sampling_create_message=sampling,
                memory_summary=memory_summary,
            )
        else:
            outcome = await tool_batch_draft(
                user_text,
                ui_context,
                language=language,
                sampling_create_message=sampling,
                memory_summary=memory_summary,
            )
        return {"success": outcome.get("kind") != "error", "tool": tool, "data": _public_outcome(outcome)}

    return {"success": False, "error": f"unhandled_ai_agent_tool:{tool}"}


# 供文档复用的精简参数骨架（describe 以 manifest / AI_AGENT_DEFAULT_TOOLS 为准）
AI_AGENT_COMMON_PARAMS = [
    {"name": "user_text", "type": "string", "description": "User request for this tool.", "required": True},
    {"name": "mailbox", "type": "string", "description": "Active mailbox.", "required": False},
    {"name": "ui_context", "type": "object", "description": "Read-only UI facts.", "required": False},
    {"name": "message_id", "type": "string", "description": "Open message id.", "required": False},
    {"name": "thread_id", "type": "string", "description": "Open thread id.", "required": False},
]


__all__ = [
    "AI_AGENT_COMMON_PARAMS",
    "AI_AGENT_TOOL_NAMES",
    "handle_ai_agent_tool",
]
