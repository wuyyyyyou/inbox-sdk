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

_SEARCH_CALL_MAX_RESULTS = 20
_THREAD_REF_TTL_SECONDS = 30 * 60
_THREAD_REF_LOCK = threading.Lock()
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


def _reserve_search_limit(requested_limit: Any) -> int:
    """限制单次搜索返回量；不同搜索调用之间不共享额度。"""
    try:
        requested = int(requested_limit or 12)
    except (TypeError, ValueError):
        requested = 12
    return max(1, min(requested, _SEARCH_CALL_MAX_RESULTS))


def _thread_ref(thread_id: str) -> str:
    """生成前端 Markdown renderer 可识别且不含空格的稳定线程引用。"""
    return f"THREAD_REF_{str(thread_id or '').strip()}"


def _remember_thread_ref(mailbox: str, thread_id: str, message_id: str) -> None:
    """仅在进程内保存 thread_ref 到最新 message_id 的短映射，供 read_email 按需取正文。"""
    if not mailbox or not thread_id or not message_id:
        return
    now = time.monotonic()
    with _THREAD_REF_LOCK:
        expired = [key for key, value in _THREAD_REF_MESSAGES.items() if now - value[1] > _THREAD_REF_TTL_SECONDS]
        for key in expired:
            _THREAD_REF_MESSAGES.pop(key, None)
        _THREAD_REF_MESSAGES[f"{mailbox}|{thread_id}"] = (message_id, now)


def _message_id_for_thread_ref(mailbox: str, thread_id: str) -> str:
    """读取短映射；过期/未知时回退原值，兼容直接传 message_id 的调用。"""
    with _THREAD_REF_LOCK:
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
# 单次最多拉取这么多 Gmail 摘要，给本地工作流状态筛选保留余量。
_SEARCH_GMAIL_CANDIDATE_CAP = 120
_SEARCH_SNIPPET_CHARS = 140
_SEARCH_SUBJECT_CHARS = 120
_SEARCH_FROM_CHARS = 100
_LOCAL_WORKFLOW_TERM = re.compile(r"^-?is:(todo|done|snoozed)$", re.IGNORECASE)


def _search_read_mask(arguments: dict[str, Any]) -> list[str]:
    """search_email 只允许轻量字段；bodyFull 一律拒绝（必须走 read_email）。"""
    raw = arguments.get("readMask") if isinstance(arguments.get("readMask"), list) else []
    mask = [str(item) for item in raw if str(item) in _SEARCH_ALLOWED_MASK]
    return mask or list(_SEARCH_DEFAULT_MASK)


def _split_gmail_and_local_workflow_query(raw_query: str) -> tuple[str, str]:
    """拆出 Gmail 不认识的本地工作流条件，避免把它们直接发送给 Gmail。

    Gmail 条件与本地工作流条件可以用 AND 组合。若混入 OR，简单移除本地条件会改变
    逻辑含义，因此明确拒绝而不返回可能遗漏的结果。
    """
    tokens = str(raw_query or "").split()
    local_indexes = [index for index, token in enumerate(tokens) if _LOCAL_WORKFLOW_TERM.fullmatch(token)]
    if not local_indexes:
        return " ".join(tokens), ""
    if any(token.upper() == "OR" for token in tokens):
        raise ValueError("Local workflow conditions is:todo, is:done, and is:snoozed only support AND combinations.")
    local_terms = [tokens[index] for index in local_indexes]
    removed = set(local_indexes)
    # 同时移除紧邻的 AND，保留其他 Gmail 条件原有的空格语义。
    for index in local_indexes:
        if index > 0 and tokens[index - 1].upper() == "AND":
            removed.add(index - 1)
        elif index + 1 < len(tokens) and tokens[index + 1].upper() == "AND":
            removed.add(index + 1)
    gmail_query = " ".join(token for index, token in enumerate(tokens) if index not in removed).strip()
    return gmail_query or "in:anywhere", " AND ".join(local_terms)


def _workflow_message_ids(
    arguments: dict[str, Any],
    ui_context: dict[str, Any],
    field: str,
) -> list[str]:
    """读取前端列表快照中的工作流 ID；空列表也是有效的已知状态。"""
    raw = ui_context.get(field) if isinstance(ui_context, dict) else None
    if not isinstance(raw, list):
        raw = arguments.get(field) if isinstance(arguments.get(field), list) else []
    return [str(item) for item in raw if str(item).strip()][:500]


def _search_email(arguments: dict[str, Any], ui_context: dict[str, Any]) -> dict[str, Any]:
    """实时检索 Gmail 并返回轻量摘要；缓存只接收本次结果，不作为查询或回退来源。"""
    from mail_agent.local_query import filter_cached_messages
    from mail_agent.mail_providers.gmail.adapter import (
        get_messages_lite,
        live_search_metadata_and_cache,
        normalize_mailbox,
    )

    mailbox = normalize_mailbox(str(arguments.get("mailbox") or ui_context.get("mailbox") or ""))
    # 未指定 limit 时默认 12，减少 Host 上下文体积
    requested_limit = arguments.get("limit")
    if requested_limit is None or requested_limit == "":
        requested_limit = 12
    limit = _reserve_search_limit(requested_limit)
    raw_query = _search_query(arguments)
    gmail_query, local_workflow_query = _split_gmail_and_local_workflow_query(raw_query)
    mask = _search_read_mask(arguments)
    # 本地状态筛选可能淘汰前几条命中，实时多拉少量候选再截断返回量。
    candidate_limit = min(_SEARCH_GMAIL_CANDIDATE_CAP, max(limit * 5, 40))
    message_ids = live_search_metadata_and_cache(
        mailbox,
        gmail_query,
        candidate_limit,
        force_refresh=True,
        strict=True,
    )
    messages = get_messages_lite(mailbox, message_ids)
    todo_ids = _workflow_message_ids(arguments, ui_context, "todo_message_ids")
    done_ids = _workflow_message_ids(arguments, ui_context, "done_message_ids")
    snoozed_ids = _workflow_message_ids(arguments, ui_context, "snoozed_message_ids")
    if local_workflow_query:
        hits, _ = filter_cached_messages(
            messages,
            local_workflow_query,
            todo_ids=todo_ids,
            done_ids=done_ids,
            snoozed_ids=snoozed_ids,
            limit=limit,
        )
    else:
        hits = messages[:limit]
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
    return {
        "kind": "search",
        "mailbox": mailbox,
        "query": raw_query,
        "scan_query": raw_query,
        "gmail_query": gmail_query,
        "local_workflow_query": local_workflow_query,
        "scan_source": "gmail",
        "cache_empty": False,
        "readMask": mask,
        "results": results,
        "count": len(results),
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
