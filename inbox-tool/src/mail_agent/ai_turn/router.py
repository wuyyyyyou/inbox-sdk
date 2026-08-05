"""AI turn 意图路由：Sampling 输出白名单 tool 计划（阶段 C）。"""

from __future__ import annotations

import logging
import re
from typing import Any

from mail_agent.llm_runtime.service import call_llm_json_safe
from mail_agent.ai_turn.prompts import router_system_prompt

_logger = logging.getLogger(__name__)

# 阶段 C 白名单：聊天 / 搜索 / 总结 / 写改稿 / 批量 / 整理建议 / 记忆；不含 mutation。
AI_TURN_ALLOWED_TOOLS = frozenset({
    "chat_general",
    "clarify",
    "search_mail",
    "rank_answer",
    "summarize_thread",
    "draft_reply",
    "revise_draft",
    "summarize_then_draft",
    "compose_new",
    "batch_compose",
    "batch_draft",
    "batch_outreach",
    "propose_inbox_actions",
    "remember_preference",
})

# 需要当前线程的工具
_THREAD_REQUIRED_TOOLS = frozenset({
    "summarize_thread",
    "draft_reply",
    "summarize_then_draft",
})

# 需要多选 selected_threads 的批量工具
_BATCH_REQUIRED_TOOLS = frozenset({
    "batch_draft",
    "batch_outreach",
})

# Router 仅输出一个极小 JSON 计划。固定 150 tokens 足够覆盖三步工具计划，
# 并避免路由阶段挤占后续检索、分析和回答的输出预算。
_ROUTER_MAX_OUTPUT_TOKENS = 150
# Router 的总输入必须低于 500 tokens。这里额外预留 20 tokens 给估算误差，
# 宁可裁掉旧摘要，也不让 Router 挤占后续检索和回答阶段的上下文。
_ROUTER_INPUT_TOKEN_BUDGET = 480
# call_llm_json_safe 会在 user message 末尾追加 JSON-only 约束。该固定文本不属于
# 动态上下文，但仍会进入模型输入，因此提前预留额度以保证总输入不超过目标。
# 当前 JSON-only 后缀实测约 51 tokens，预留 55 留出估算余量。
_ROUTER_JSON_PROTOCOL_TOKEN_OVERHEAD = 55
# 瞬时 Sampling 失败时最多尝试 2 次，再进入确定性 fallback。
_ROUTER_SAMPLING_MAX_ATTEMPTS = 2
# Router 提示词与其它 Sampling 提示词统一集中在 prompts.py，避免协议约束散落。
_ROUTER_SYSTEM = router_system_prompt()


def _uses_chinese(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _has_thread(ui_context: dict[str, Any]) -> bool:
    current = ui_context.get("current_thread") if isinstance(ui_context.get("current_thread"), dict) else {}
    return str(current.get("kind") or "") in {"thread", "compose"} and bool(
        str(current.get("message_id") or current.get("thread_id") or "").strip()
        or (str(current.get("kind") or "") == "compose")
    )


def _has_last_draft(ui_context: dict[str, Any]) -> bool:
    last = ui_context.get("last_draft") if isinstance(ui_context.get("last_draft"), dict) else {}
    return bool(str(last.get("body") or "").strip())


def _selected_threads_count(ui_context: dict[str, Any]) -> int:
    """统计多选线程数量（供批量工具与 Router 降级使用）。"""
    selected = ui_context.get("selected_threads")
    if not isinstance(selected, list):
        return 0
    return sum(1 for item in selected if isinstance(item, dict))


def _has_inbox_search_intent(user_text: str) -> bool:
    """识别明确的全邮箱检索请求，避免 Router 将其错误降级为普通聊天。

    该判断只用于覆盖 ``chat_general``：用户明确要求查找、搜索、列举未读/紧急
    邮件或找出待自己回复的邮件时，必须进入 ``search_mail``，由 Ask 管线返回
    经过候选校验的邮件链接。
    写信、改稿等非聊天工具不会被这里改写，仍由结构化 Router 处理。
    """
    lowered = (user_text or "").casefold()
    return any(
        token in lowered
        for token in (
            "find", "search", "inbox", "email", "mail", "urgent", "unread", "invoice",
            "needs my reply", "need my reply", "awaiting my reply", "waiting for my reply",
            "等我回复", "待我回复", "需要我回复", "谁在等我",
            "找", "搜索", "收件箱", "邮件", "未读", "紧急", "发票",
        )
    )


def _explicit_current_thread_request(user_text: str) -> bool:
    """仅把明确指向当前邮件的指代词视为当前线程请求。"""
    lowered = (user_text or "").casefold()
    return bool(re.search(
        r"(?:this\s+(?:email|mail|message|thread)|current\s+(?:email|thread)|"
        r"the\s+(?:email|message|thread)\s+(?:above|here)|"
        r"这封(?:邮件|信)?|当前(?:邮件|线程)|上面的(?:邮件|信)|该(?:邮件|信)|此(?:邮件|信))",
        lowered,
    ))


def _fallback_route(user_text: str, ui_context: dict[str, Any], reason: str) -> dict[str, Any]:
    """Router 失败时的确定性降级，保证 turn 仍可执行。"""
    language = "zh" if _uses_chinese(user_text) else "en"
    has_thread = _has_thread(ui_context)
    has_draft = _has_last_draft(ui_context)
    lowered = (user_text or "").casefold()
    current_thread_request = _explicit_current_thread_request(user_text)

    deferred_draft_hint = bool(re.search(
        r"(?:先不要|暂不|暂时不要|不要先|稍后|之后|接下来).{0,24}(?:draft|草稿|起草|生成)|"
        r"(?:加入上下文|添加到上下文|记住|保存模板).{0,32}(?:不要|暂不|稍后|之后|接下来)",
        lowered,
        flags=re.IGNORECASE,
    ))

    remember_hint = bool(
        re.search(r"\bremember(?:\s+to)?\b|记住", lowered)
    )
    if remember_hint or deferred_draft_hint:
        return {
            "language": language,
            "use_current_thread": False,
            "clarify": None,
            "steps": [{"tool": "remember_preference", "params": {}}],
            "router_fallback": True,
            "router_reason": reason[:200],
        }

    organize_hint = any(
        token in lowered
        for token in (
            "organize", "archive", "clean up", "cleanup", "mark done",
            "整理", "归档", "清理", "标为已处理",
        )
    )
    if organize_hint:
        return {
            "language": language,
            "use_current_thread": False,
            "clarify": None,
            "steps": [{"tool": "propose_inbox_actions", "params": {}}],
            "router_fallback": True,
            "router_reason": reason[:200],
        }

    selected_count = _selected_threads_count(ui_context)
    batch_hint = any(
        token in lowered
        for token in (
            "batch", "each of", "for each", "all selected", "these emails", "these threads",
            "批量", "多封", "每封", "这些邮件", "勾选", "选中的",
        )
    )
    outreach_hint = any(
        token in lowered
        for token in (
            "outreach", "personalized", "personalize", "dm", "cold email",
            "个性化", "触达", "私信", "外联",
        )
    )
    draft_hint = any(
        token in lowered
        for token in (
            "draft", "reply", "respond", "write a reply", "write back",
            "起草", "回复", "写回信", "写回复",
        )
    )
    compose_hint = any(
        token in lowered
        for token in (
            "compose", "new email", "write an email", "write email", "fyi", "outline",
            "写一封", "写封邮件", "写邮件", "新邮件", "大纲",
        )
    )
    compose_recipient_count = len(set(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", user_text, re.IGNORECASE)))
    if compose_hint and compose_recipient_count >= 2:
        return {
            "language": language,
            "use_current_thread": False,
            "clarify": None,
            "steps": [{"tool": "batch_compose", "params": {}}],
            "router_fallback": True,
            "router_reason": reason[:200],
        }
    # 多选 + 批量/多封意图 → batch；outreach 优先于普通 batch_draft
    if selected_count >= 2 and (batch_hint or draft_hint or outreach_hint):
        tool = "batch_outreach" if outreach_hint else "batch_draft"
        return {
            "language": language,
            "use_current_thread": False,
            "clarify": None,
            "steps": [{"tool": tool, "params": {}}],
            "router_fallback": True,
            "router_reason": reason[:200],
        }

    revise_hint = any(
        token in lowered
        for token in (
            "shorten", "friendlier", "more professional", "revise", "rewrite", "improve draft",
            "缩短", "改写", "润色", "更专业", "更友好", "修改草稿",
        )
    )
    if revise_hint and has_draft:
        return {
            "language": language,
            "use_current_thread": has_thread,
            "clarify": None,
            "steps": [{"tool": "revise_draft", "params": {}}],
            "router_fallback": True,
            "router_reason": reason[:200],
        }

    summarize_then = any(token in lowered for token in ("summar", "总结", "概括")) and draft_hint
    if has_thread and summarize_then:
        return {
            "language": language,
            "use_current_thread": True,
            "clarify": None,
            "steps": [{"tool": "summarize_then_draft", "params": {}}],
            "router_fallback": True,
            "router_reason": reason[:200],
        }
    if has_thread and draft_hint and current_thread_request:
        return {
            "language": language,
            "use_current_thread": True,
            "clarify": None,
            "steps": [{"tool": "draft_reply", "params": {}}],
            "router_fallback": True,
            "router_reason": reason[:200],
        }

    explicit_search_action = any(
        token in lowered
        for token in ("find", "search", "inbox", "urgent", "unread", "找", "搜索", "收件箱", "紧急", "未读")
    )
    if compose_hint and not explicit_search_action:
        return {
            "language": language,
            "use_current_thread": False,
            "clarify": None,
            "steps": [{"tool": "compose_new", "params": {}}],
            "router_fallback": True,
            "router_reason": reason[:200],
        }

    if draft_hint:
        return {
            "language": language,
            "use_current_thread": False,
            "clarify": None,
            "steps": [
                {"tool": "search_mail", "params": {}},
                {"tool": "compose_new", "params": {}},
            ],
            "router_fallback": True,
            "router_reason": reason[:200],
        }

    scan_hint = _has_inbox_search_intent(user_text)
    summary_hint = any(
        token in lowered
        for token in ("summar", "总结", "概括", "这封", "this email", "this thread", "action item", "待办")
    )
    # 打开线程时的「总结这封邮件」会命中「邮件」关键词，须优先于全箱搜索。
    if has_thread and summary_hint and not compose_hint:
        return {
            "language": language,
            "use_current_thread": True,
            "clarify": None,
            "steps": [{"tool": "summarize_thread", "params": {}}],
            "router_fallback": True,
            "router_reason": reason[:200],
        }
    if scan_hint and compose_hint:
        return {
            "language": language,
            "use_current_thread": False,
            "clarify": None,
            "steps": [
                {"tool": "search_mail", "params": {}},
                {"tool": "compose_new", "params": {}},
            ],
            "router_fallback": True,
            "router_reason": reason[:200],
        }
    if compose_hint and not has_thread:
        return {
            "language": language,
            "use_current_thread": False,
            "clarify": None,
            "steps": [{"tool": "compose_new", "params": {}}],
            "router_fallback": True,
            "router_reason": reason[:200],
        }
    if scan_hint:
        return {
            "language": language,
            "use_current_thread": False,
            "clarify": None,
            "steps": [
                {"tool": "search_mail", "params": {}},
                {"tool": "rank_answer", "params": {}},
            ],
            "router_fallback": True,
            "router_reason": reason[:200],
        }
    if has_thread and re.search(r"\b(it|this|that)\b|这个|那封|它", lowered):
        clarify = "请先打开一封邮件，或说明要搜索整个收件箱。" if language == "zh" else (
            "Open an email first, or say you want to search the whole inbox."
        )
        # 有线程时指代应走总结而非 clarify；仅当误判时
        return {
            "language": language,
            "use_current_thread": True,
            "clarify": None,
            "steps": [{"tool": "summarize_thread", "params": {}}],
            "router_fallback": True,
            "router_reason": reason[:200],
        }
    return {
        "language": language,
        "use_current_thread": False,
        "clarify": None,
        "steps": [{"tool": "chat_general", "params": {}}],
        "router_fallback": True,
        "router_reason": reason[:200],
    }


def _fast_local_route(user_text: str, ui_context: dict[str, Any]) -> dict[str, Any] | None:
    """为无歧义的高频请求跳过 Router Sampling，减少一次网络往返。

    此处只接受已有确定性规则可稳定处理的意图；其余自然语言仍交给 Router，
    避免用关键词覆盖复杂写作或多步骤请求。
    """
    lowered = (user_text or "").casefold().strip()
    has_thread = _has_thread(ui_context)
    compose_hint = any(
        token in lowered
        for token in ("compose", "new email", "write an email", "write email", "写一封", "写封邮件", "写邮件", "新邮件")
    )
    # 复用同一意图判断，避免快速路径和 fallback 各自维护关键词导致待回复邮件误入聊天。
    explicit_inbox_search = _has_inbox_search_intent(user_text)
    thread_action = has_thread and (
        _explicit_current_thread_request(user_text)
        or any(token in lowered for token in ("summar", "总结", "概括", "待办"))
    )
    greeting = bool(re.fullmatch(r"(?:hi|hello|hey|你好|嗨|在吗)[!！。,.?？\s]*", lowered))
    explicit_preference = bool(re.search(r"\bremember(?:\s+to)?\b|记住", lowered))
    if (explicit_inbox_search and not compose_hint) or thread_action or greeting or explicit_preference:
        return _fallback_route(user_text, ui_context, "fast_local_route")
    return None


def _route_for_user_selected_intent(user_text: str, ui_context: dict[str, Any]) -> dict[str, Any] | None:
    """把界面中的显式用户选择转换为稳定的白名单执行计划。

    来源包括：Router 澄清弹层、侧栏 starter 快捷按钮。这里不是根据关键词猜测
    意图：用户已在界面主动选定范围或任务类型，继续让 Router 模型二次判断会
    导致同一选择反复进入澄清或误落到 chat_general。仅映射范围级计划；邮件
    内容的检索、排序和生成仍由后续白名单工具与模型完成。
    """
    intent = str(ui_context.get("routing_intent") or "").strip()
    language = "zh" if _uses_chinese(user_text) else "en"
    if intent == "inbox":
        return {
            "language": language,
            "use_current_thread": False,
            "clarify": None,
            "steps": [{"tool": "search_mail", "params": {}}, {"tool": "rank_answer", "params": {}}],
            "router_fallback": False,
            "router_user_selected": True,
            "router_reason": "user_selected_inbox",
        }
    if intent == "organize":
        # 侧栏「整理收件箱」starter：只提议动作，不直接变更 Gmail 状态。
        return {
            "language": language,
            "use_current_thread": False,
            "clarify": None,
            "steps": [{"tool": "propose_inbox_actions", "params": {}}],
            "router_fallback": False,
            "router_user_selected": True,
            "router_reason": "user_selected_organize",
        }
    if intent == "chat":
        return {
            "language": language,
            "use_current_thread": False,
            "clarify": None,
            "steps": [{"tool": "chat_general", "params": {}}],
            "router_fallback": False,
            "router_user_selected": True,
            "router_reason": "user_selected_chat",
        }
    if intent == "compose":
        return {
            "language": language,
            "use_current_thread": False,
            "clarify": None,
            "steps": [{"tool": "compose_new", "params": {}}],
            "router_fallback": False,
            "router_user_selected": True,
            "router_reason": "user_selected_compose",
        }
    if intent == "current_thread" and _has_thread(ui_context):
        return {
            "language": language,
            "use_current_thread": True,
            "clarify": None,
            "steps": [{"tool": "summarize_thread", "params": {}}],
            "router_fallback": False,
            "router_user_selected": True,
            "router_reason": "user_selected_current_thread",
        }
    return None


def _estimate_router_input_tokens(text: str) -> int:
    """保守估算 Router 输入 token 数，供本地裁剪而非计费或审计使用。"""
    tokens = 0
    for chunk in re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]", text or ""):
        if re.fullmatch(r"[\u3400-\u9fff]", chunk):
            tokens += 1
        elif chunk.isascii() and (chunk[0].isalnum() or chunk[0] == "_"):
            tokens += max(1, (len(chunk) + 3) // 4)
        else:
            tokens += 1
    return tokens


def _truncate_router_text(text: str, token_budget: int) -> str:
    """将动态上下文裁剪到近似 token 额度，保留前缀中的用户原始请求。"""
    normalized = str(text or "").strip()
    if not normalized or token_budget <= 0:
        return ""
    if _estimate_router_input_tokens(normalized) <= token_budget:
        return normalized
    low, high = 0, len(normalized)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = normalized[:middle].rstrip() + "..."
        if _estimate_router_input_tokens(candidate) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return normalized[:low].rstrip() + "..."


def _build_router_user_message(
    user_text: str,
    *,
    current_thread_kind: str,
    has_current_thread: bool,
    has_last_draft: bool,
    selected_threads_count: int,
    routing_intent: str,
    conversation_summary: str,
    memory_summary: str,
) -> str:
    """构造受输入预算约束的 Router 上下文，动态摘要只使用用户请求后的剩余额度。"""
    fixed_context = (
        f"\n\nContext: thread_kind={current_thread_kind}; has_current_thread={has_current_thread}; "
        f"has_last_draft={has_last_draft}; selected_threads_count={selected_threads_count}; "
        f"routing_intent={routing_intent or 'none'}"
    )
    available = max(
        1,
        _ROUTER_INPUT_TOKEN_BUDGET
        - _ROUTER_JSON_PROTOCOL_TOKEN_OVERHEAD
        - _estimate_router_input_tokens(_ROUTER_SYSTEM),
    )
    request_prefix = "User request:\n"
    request_budget = max(1, available - _estimate_router_input_tokens(request_prefix + fixed_context))
    request = _truncate_router_text(user_text, request_budget)
    message = f"{request_prefix}{request}{fixed_context}"
    remaining = available - _estimate_router_input_tokens(message)
    for label, summary in (("Conversation", conversation_summary), ("Memory", memory_summary)):
        if remaining <= 0 or not summary:
            break
        prefix = f"\n{label}: "
        content_budget = remaining - _estimate_router_input_tokens(prefix)
        if content_budget <= 0:
            break
        message += prefix + _truncate_router_text(summary, content_budget)
        remaining = available - _estimate_router_input_tokens(message)
    return message


def _normalize_route(payload: dict[str, Any], user_text: str, ui_context: dict[str, Any]) -> dict[str, Any]:
    """校验并裁剪 Router JSON。"""
    language = str(payload.get("language") or ("zh" if _uses_chinese(user_text) else "en")).strip().lower()
    if language not in {"zh", "en"}:
        language = "zh" if _uses_chinese(user_text) else "en"
    use_current = bool(payload.get("use_current_thread"))
    clarify_raw = payload.get("clarify")
    clarify = str(clarify_raw).strip() if clarify_raw not in (None, "") else None
    steps_in = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    steps: list[dict[str, Any]] = []
    for item in steps_in[:3]:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "").strip()
        if tool not in AI_TURN_ALLOWED_TOOLS:
            _logger.warning("ai_turn router rejected tool: name=%s", tool[:64])
            continue
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        steps.append({"tool": tool, "params": params})

    has_thread = _has_thread(ui_context)
    has_draft = _has_last_draft(ui_context)
    selected_count = _selected_threads_count(ui_context)
    current_thread_request = _explicit_current_thread_request(user_text)

    if use_current and not has_thread:
        clarify = clarify or (
            "请先打开一封邮件，这样我才知道你指的是哪一封。"
            if language == "zh"
            else "Open an email first so I know which message you mean."
        )
        steps = [s for s in steps if s["tool"] not in _THREAD_REQUIRED_TOOLS]
        use_current = False

    # 需要线程的工具在无线程时剔除
    if not has_thread:
        filtered = [s for s in steps if s["tool"] not in _THREAD_REQUIRED_TOOLS]
        if len(filtered) != len(steps) and not filtered:
            clarify = clarify or (
                "请先打开一封邮件，再试一次。"
                if language == "zh"
                else "Open an email first, then try again."
            )
        steps = filtered

    if has_thread and not current_thread_request:
        use_current = False
        if any(s["tool"] in {"draft_reply", "summarize_then_draft"} for s in steps):
            steps = [s for s in steps if s["tool"] not in _THREAD_REQUIRED_TOOLS]
            if not any(s["tool"] == "search_mail" for s in steps):
                steps.insert(0, {"tool": "search_mail", "params": {}})
            if not any(s["tool"] == "compose_new" for s in steps):
                steps.append({"tool": "compose_new", "params": {}})
        elif any(s["tool"] == "summarize_thread" for s in steps):
            steps = [s for s in steps if s["tool"] != "summarize_thread"]
            if not any(s["tool"] == "search_mail" for s in steps):
                steps.insert(0, {"tool": "search_mail", "params": {}})
            if not any(s["tool"] == "rank_answer" for s in steps):
                steps.append({"tool": "rank_answer", "params": {}})

    # 批量工具：无多选时剔除并 clarify（单封走 draft_reply）
    if selected_count < 2:
        batch_steps = [s for s in steps if s["tool"] in _BATCH_REQUIRED_TOOLS]
        if batch_steps:
            steps = [s for s in steps if s["tool"] not in _BATCH_REQUIRED_TOOLS]
            if not steps:
                if selected_count == 1 and has_thread:
                    steps = [{"tool": "draft_reply", "params": {}}]
                else:
                    clarify = clarify or (
                        "请先勾选至少 2 封邮件，再请求批量起草。"
                        if language == "zh"
                        else "Select at least 2 emails before asking for batch drafts."
                    )

    # revise 无 draft 时降级 clarify
    if any(s["tool"] == "revise_draft" for s in steps) and not has_draft:
        steps = [s for s in steps if s["tool"] != "revise_draft"]
        if not steps:
            clarify = clarify or (
                "请先提供要改写的草稿。"
                if language == "zh"
                else "Provide a draft to revise first."
            )

    if clarify and not steps:
        return {
            "language": language,
            "use_current_thread": use_current,
            "clarify": clarify,
            "steps": [],
            "router_fallback": False,
            "router_reason": "",
        }
    if not steps:
        # 模型给出空计划代表 Router 输出不可用；使用本地确定性路由继续完成请求，
        # 不能让用户为本应自动完成的总结、搜索或问候重复选择操作范围。
        return _fallback_route(user_text, ui_context, "empty_steps")
    return {
        "language": language,
        "use_current_thread": use_current,
        "clarify": None if steps else clarify,
        "steps": steps,
        "router_fallback": False,
        "router_reason": "",
    }


async def route_ai_turn(
    user_text: str,
    ui_context: dict[str, Any] | None,
    *,
    sampling_create_message: Any = None,
    memory_summary: str = "",
    conversation_summary: str = "",
) -> dict[str, Any]:
    """对用户话术做结构化选型；失败时走确定性降级。"""
    context = ui_context if isinstance(ui_context, dict) else {}
    selected_route = _route_for_user_selected_intent(user_text, context)
    if selected_route is not None:
        return selected_route
    fast_local_route = _fast_local_route(user_text, context)
    if fast_local_route is not None:
        return fast_local_route
    current = context.get("current_thread") if isinstance(context.get("current_thread"), dict) else {}
    last_draft = context.get("last_draft") if isinstance(context.get("last_draft"), dict) else {}
    routing_intent = str(context.get("routing_intent") or "").strip()
    user_message = _build_router_user_message(
        user_text,
        current_thread_kind=str(current.get("kind") or "none"),
        has_current_thread=_has_thread(context),
        has_last_draft=bool(str(last_draft.get("body") or "").strip()),
        selected_threads_count=_selected_threads_count(context),
        routing_intent=routing_intent,
        conversation_summary=conversation_summary,
        memory_summary=memory_summary,
    )
    if sampling_create_message is None:
        return _fallback_route(user_text, context, "no_sampling")
    try:
        result = await call_llm_json_safe(
            sampling_create_message,
            system_prompt=_ROUTER_SYSTEM,
            user_message=user_message,
            fallback={},
            temperature=0.0,
            max_tokens=_ROUTER_MAX_OUTPUT_TOKENS,
            timeout=30.0,
            metadata={"tool": "ai_turn_router"},
            response_format={"type": "json_object"},
            on_unsupported="text",
            allow_fallback=False,
            allow_sampling_provider_fallback=True,
            # 瞬时 SamplingError / 空响应时重试一次，再走本地 fallback。
            max_attempts=_ROUTER_SAMPLING_MAX_ATTEMPTS,
        )
    except Exception as exc:
        return _fallback_route(user_text, context, f"router_error:{type(exc).__name__}")
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    if not payload:
        return _fallback_route(user_text, context, "empty_router_payload")
    return _normalize_route(payload, user_text, context)


__all__ = ["AI_TURN_ALLOWED_TOOLS", "route_ai_turn"]
