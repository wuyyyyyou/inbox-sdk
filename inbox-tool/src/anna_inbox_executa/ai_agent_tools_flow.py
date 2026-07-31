"""本地 Agent 会话的细粒度工具：选型和执行均在 Executa 内完成。

侧栏 App 通过本地 Sampling 会话完成选型；本模块只实现受限工具，
不含 apply/send 等 mutation。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from anna_inbox_executa.sampling_tools import _build_sampling_for_run

# 本地会话可调用工具白名单。
AI_AGENT_TOOL_NAMES = frozenset({
    "query_mail_evidence",
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

# 侧栏主路径只暴露一个复合只读工具；写作与确认卡保留专用工具。
AI_AGENT_SESSION_TOOL_NAMES = frozenset({
    "query_mail_evidence",
    "ai_draft_reply",
    "ai_revise_draft",
    "ai_compose_new",
    "ai_batch_draft",
    "propose_inbox_actions",
    "ai_remember_preference",
})

# 普通 Evidence 仍默认 limit=20；计数/时间窗列举可显式提高到 200，避免 3/7/30 天都截断成相同数量。
_SEARCH_CALL_MAX_RESULTS = 200
_THREAD_REF_TTL_SECONDS = 30 * 60
_THREAD_REF_LOCK = threading.Lock()
_THREAD_REF_MESSAGES: dict[str, tuple[str, float]] = {}


def _uses_chinese(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _attachment_filenames(message: Any, *, limit: int = 6) -> list[str]:
    """从 MessageLite / dict 提取附件文件名，供 Agent 回答发票等问题。"""
    names: list[str] = []
    raw_list: list[Any] = []
    if hasattr(message, "attachments"):
        raw_list = list(getattr(message, "attachments") or [])
    elif isinstance(message, dict):
        raw = message.get("attachments")
        if isinstance(raw, list):
            raw_list = raw
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        name = str(item.get("filename") or item.get("name") or "").strip()
        if not name or name in names:
            continue
        names.append(name[:160])
        if len(names) >= limit:
            break
    return names


def _merge_ui_context(arguments: dict[str, Any]) -> dict[str, Any]:
    """合并 Host 传入的 ui_context 与扁平字段，便于模型少填嵌套对象。"""
    raw = arguments.get("ui_context")
    context: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    mailbox = str(arguments.get("mailbox") or context.get("mailbox") or "").strip()
    if mailbox:
        context["mailbox"] = mailbox
    message_id = str(arguments.get("message_id") or "").strip()
    thread_id = str(arguments.get("thread_id") or "").strip()
    # 允许模型用 search 命中的 THREAD_REF 直接写回复，不要求详情抽屉已打开
    thread_ref = str(arguments.get("thread_ref") or "").strip()
    if thread_ref:
        ref_id = thread_ref.removeprefix("THREAD_REF_").strip()
        if ref_id and not thread_id:
            thread_id = ref_id
        if ref_id and not message_id and mailbox:
            mapped = _message_id_for_thread_ref(mailbox, ref_id)
            if mapped:
                message_id = mapped
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
_SEARCH_GMAIL_WORKFLOW_CANDIDATE_CAP = 60
_SEARCH_GMAIL_REQUEST_TIMEOUT_SECONDS = 12.0
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


def _apply_display_range_default(gmail_query: str, ui_context: dict[str, Any]) -> str:
    """未显式给出时间条件时，继承用户当前 7/30/60 天列表范围。"""
    query = str(gmail_query or "").strip()
    if re.search(r"\b(?:after|before|newer_than|older_than):", query, re.IGNORECASE):
        return query
    try:
        days = int(ui_context.get("display_range_days") or 0)
    except (TypeError, ValueError):
        days = 0
    if days not in {7, 30, 60}:
        return query
    return f"{query} newer_than:{days}d".strip()


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
    """仅检索本地邮件缓存并返回轻量摘要，不在 AI 问答路径实时访问 Gmail。

    display_range_days 只约束 Inbox 列表展示，不隐式缩小 AI 的缓存检索范围；
    用户给出的明确 before/after 条件仍由本地查询执行。缓存边界随结果返回，
    让模型在范围外问题上说明「无法判断」而不是误答「没有」。
    """
    from mail_agent.local_query import filter_cached_messages, normalize_to_local_query
    from mail_agent.mail_providers.gmail.adapter import (
        _to_message_lite,
        list_messages,
        normalize_mailbox,
        read_message,
    )
    from mail_agent.mail_providers.gmail.mailbox_sync import (
        boundary_honesty_note,
        get_mailbox_sync_boundary,
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
    raw_order = str(arguments.get("order") or "newest").strip().lower()
    order = "oldest" if raw_order in {"oldest", "asc", "ascending"} else "newest"
    # 全量缓存扫描：不受 display_range 或 500 条首页缓存 helper 的限制。
    cached_rows = [item for item in list_messages(mailbox) if isinstance(item, dict)]
    messages = [_to_message_lite(item) for item in cached_rows]
    # 「最早/最晚」必须在完整已索引集上确定排序，再截取 evidence 上限；不能从
    # 默认 newest 的前 20 条中猜最早日期。
    def _message_date_key(message: Any) -> int:
        try:
            return int(message.internal_date or 0)
        except (TypeError, ValueError):
            return 0

    messages.sort(key=_message_date_key, reverse=order == "newest")
    todo_ids = _workflow_message_ids(arguments, ui_context, "todo_message_ids")
    done_ids = _workflow_message_ids(arguments, ui_context, "done_message_ids")
    snoozed_ids = _workflow_message_ids(arguments, ui_context, "snoozed_message_ids")
    # Gmail 风格查询转为本地语法；工作流条件已经从 Gmail 查询中拆出，再与本地
    # 条件组合，保证 Todo/Done/Snoozed 始终使用前端权威 id 快照。
    query_parts = [normalize_to_local_query(gmail_query)]
    if local_workflow_query:
        query_parts.append(local_workflow_query)
    local_query = " AND ".join(part for part in query_parts if part).strip() or "is:all"
    # C 路径传入的候选只来自 SQLite FTS5。这里仍执行结构化时间/工作流过滤，
    # 再按 FTS 返回顺序组装轻量 evidence，不能改用普通缓存命中替代索引结果。
    raw_candidate_ids = arguments.get("candidate_message_ids")
    candidate_ids = [str(item) for item in raw_candidate_ids if str(item)] if isinstance(raw_candidate_ids, list) else []
    candidate_ids = candidate_ids[:100]
    filter_limit = 200 if candidate_ids else limit
    hits, parsed_query = filter_cached_messages(
        messages,
        local_query,
        todo_ids=todo_ids,
        done_ids=done_ids,
        snoozed_ids=snoozed_ids,
        limit=filter_limit,
    )
    if not hits and re.search(r"(?:^|\s)-?body:", local_query, re.IGNORECASE):
        # MessageLite 只带摘要，不能据此否定正文中的精确短语。仅在显式 body: 条件
        # 且摘要初筛无结果时，逐封读取已缓存正文并复用同一过滤器；绝不回源 Gmail，
        # 且正文只在进程内用于匹配，不会进入工具结果或模型上下文。
        body_search_messages = []
        for item in cached_rows:
            message = _to_message_lite(item)
            try:
                cached = read_message(mailbox, message.message_id)
            except Exception:
                cached = None
            if isinstance(cached, dict):
                analysis = cached.get("content_analysis") if isinstance(cached.get("content_analysis"), dict) else {}
                body = str(analysis.get("body") or cached.get("body_text") or message.snippet or "")
                message.snippet = body
            body_search_messages.append(message)
        hits, parsed_query = filter_cached_messages(
            body_search_messages,
            local_query,
            todo_ids=todo_ids,
            done_ids=done_ids,
            snoozed_ids=snoozed_ids,
            limit=filter_limit,
        )
    if candidate_ids:
        allowed_by_id = {str(message.message_id or ""): message for message in hits}
        hits = [allowed_by_id[message_id] for message_id in candidate_ids if message_id in allowed_by_id][:limit]
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
        # 附件文件名来自实时 metadata 缓存，便于回答发票/附件类问题
        filenames = _attachment_filenames(message)
        if filenames or bool(getattr(message, "has_attachment", False)):
            row["hasAttachment"] = bool(filenames) or bool(getattr(message, "has_attachment", False))
        if filenames:
            row["attachmentFilenames"] = filenames
        results.append(row)
    boundary = get_mailbox_sync_boundary(mailbox)
    language = _language(arguments, _user_text(arguments))
    return {
        "kind": "search",
        "mailbox": mailbox,
        "query": raw_query,
        "scan_query": local_query,
        "gmail_query": gmail_query,
        "local_query": parsed_query.display or local_query,
        "local_workflow_query": local_workflow_query,
        "scan_source": "cache",
        "cache_empty": not cached_rows,
        "readMask": mask,
        "results": results,
        "count": len(results),
        "cache_candidates_scanned": len(messages),
        "order": order,
        "result_limit": limit,
        "truncated": len(results) >= limit,
        "query_parse_error": parsed_query.error,
        "sync_boundary": boundary,
        "coverage_note": boundary_honesty_note(boundary, language),
    }


def _read_email(arguments: dict[str, Any], ui_context: dict[str, Any]) -> dict[str, Any]:
    """按 readMask 返回字段；只有 bodyFull 触发 Gmail 正文读取。"""
    from mail_agent.mail_providers.gmail.adapter import normalize_mailbox, read_message
    from mail_agent.mail_providers.gmail.mailbox_sync import get_mailbox_sync_boundary

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
    # 附件清单：优先缓存 metadata；读全文时再合并详情侧附件
    filenames = _attachment_filenames(message)
    if "bodyFull" in mask:
        # AI 路径只读缓存：正文未同步时明确返回 pending，不能隐式请求 Gmail。
        body_text = str(message.get("body_text") or "")
        if body_text:
            result["bodyFull"] = body_text[:3500]
        else:
            result["body_pending"] = True
            result["sync_boundary"] = get_mailbox_sync_boundary(mailbox)
    if filenames:
        result["attachmentFilenames"] = filenames[:8]
        result["hasAttachment"] = True
    elif bool(message.get("has_attachment") or message.get("attachments")):
        result["hasAttachment"] = True
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
    "nearby_results",
    "nearby_query",
    "match_status",
    "count",
    "cache_empty",
    "query_plan",
    "active_scope",
    "scope_reset",
    "sync_boundary",
    "coverage_note",
    "exact_count",
    "search_scope",
    "cache_total",
    "allow_full_email_text",
    "evaluation_path",
    "evaluation_metrics",
    # B12/C02：域名矛盾备注须透传给 local final，否则只能依赖模型自行察觉。
    "domain_warning_note",
    "time_span_note",
    "domain_warning_threads",
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
    if str(public.get("kind") or "").startswith("evidence"):
        # P3 Evidence 只输出结构化 sync_boundary；旧范围文案不能成为模型最终回答。
        public.pop("coverage_note", None)
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

    if tool == "query_mail_evidence":
        from mail_agent.evidence_flow import query_mail_evidence

        sampling = _build_sampling_for_run(arguments, invoke_id)
        conversation_id = str(arguments.get("conversation_id") or ui_context.get("conversation_id") or "").strip()
        query_plan = arguments.get("query_plan") if isinstance(arguments.get("query_plan"), dict) else None
        try:
            outcome = await query_mail_evidence(
                user_text,
                ui_context,
                sampling_create_message=sampling,
                conversation_id=conversation_id,
                query_plan=query_plan,
                scope_kind=str(arguments.get("scope_kind") or ""),
            )
            return {"success": True, "tool": tool, "data": _public_outcome(outcome)}
        except Exception as exc:
            return {"success": False, "tool": tool, "error": type(exc).__name__}

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
    "AI_AGENT_SESSION_TOOL_NAMES",
    "handle_ai_agent_tool",
]
