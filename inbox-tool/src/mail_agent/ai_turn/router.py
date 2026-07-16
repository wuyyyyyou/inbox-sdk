"""AI turn 意图路由：Sampling 输出白名单 tool 计划（阶段 C）。"""

from __future__ import annotations

import logging
import re
from typing import Any

from mail_agent.llm_runtime.service import call_llm_json_safe

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

_ROUTER_MAX_TOKENS = 448
_ROUTER_SYSTEM = """You are Anna's inbox AI turn router. Choose tools from a fixed whitelist only.
Return one JSON object. First character must be `{`. No markdown.

Whitelist tools:
- chat_general: greetings, capability questions, non-mail conversation
- clarify: missing current email when user refers to "this email", or intent is ambiguous
- search_mail: find / list / prioritize across the inbox (Gmail search)
- rank_answer: after search_mail, summarize or rank results (optional second step)
- summarize_thread: summarize or extract from the currently open email/thread
- draft_reply: draft a reply to the current open email/thread
- revise_draft: revise an existing draft body (last_draft or compose body provided)
- summarize_then_draft: summarize current thread then draft a reply
- compose_new: write a new outbound email outline/body (not a reply)
- batch_draft: short reply drafts for MULTIPLE selected emails (selected_threads_count >= 2)
- batch_outreach: personalized outreach/DM for MULTIPLE selected emails (variables isolated)
- propose_inbox_actions: SUGGEST organize actions only (mark done/archive/trash cards); never executes
- remember_preference: user explicitly says remember / 记住 a preference

Schema:
{
  "language": "zh" | "en",
  "use_current_thread": boolean,
  "clarify": string | null,
  "steps": [{"tool": "<whitelist>", "params": {}}]
}

Rules:
1. steps length 1-3. Prefer one step when enough.
2. If user says this/that email and no current thread is available, set clarify and empty steps.
3. Inbox-wide find/search/urgent/unread/invoice → search_mail (+ optional rank_answer).
4. Open thread + summarize/what is this about/action items → summarize_thread with use_current_thread true.
5. Open thread + draft/reply/write response → draft_reply (or summarize_then_draft if they ask summarize then reply).
6. Shorten / friendlier / more professional / revise draft + last_draft present → revise_draft.
7. Organize my inbox / archive low priority / clean up → propose_inbox_actions (optionally after search_mail).
8. Remember to… / 记住… → remember_preference.
9. Search then write FYI / outline / new email → search_mail then compose_new.
10. Multiple selected threads + draft/reply for each → batch_draft; personalized outreach/DM → batch_outreach.
11. Hi/hello/what can you do → chat_general.
12. Never invent tools outside the whitelist. Never choose send/delete/archive/trash/mark_read as tools.
"""


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

    该判断只用于覆盖 ``chat_general``：用户明确要求查找、搜索、列举未读或紧急
    邮件时，必须进入 ``search_mail``，由 Ask 管线返回经过候选校验的邮件链接。
    写信、改稿等非聊天工具不会被这里改写，仍由结构化 Router 处理。
    """
    lowered = (user_text or "").casefold()
    return any(
        token in lowered
        for token in (
            "find", "search", "inbox", "email", "mail", "urgent", "unread", "invoice",
            "找", "搜索", "收件箱", "邮件", "未读", "紧急", "发票",
        )
    )


def _fallback_route(user_text: str, ui_context: dict[str, Any], reason: str) -> dict[str, Any]:
    """Router 失败时的确定性降级，保证 turn 仍可执行。"""
    language = "zh" if _uses_chinese(user_text) else "en"
    has_thread = _has_thread(ui_context)
    has_draft = _has_last_draft(ui_context)
    lowered = (user_text or "").casefold()

    remember_hint = bool(
        re.search(r"\bremember(?:\s+to)?\b|记住", lowered)
    )
    if remember_hint:
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
    if has_thread and draft_hint:
        return {
            "language": language,
            "use_current_thread": True,
            "clarify": None,
            "steps": [{"tool": "draft_reply", "params": {}}],
            "router_fallback": True,
            "router_reason": reason[:200],
        }

    compose_hint = any(
        token in lowered
        for token in ("compose", "new email", "fyi", "outline", "写一封", "新邮件", "大纲")
    )
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

    # Router Sampling 偶尔会把「找未读邮件」这类明确检索误判为聊天。仅覆盖纯聊天
    # 路径，使后续回答必须基于 Ask 候选并返回可验证的 mail_links。
    if (
        _has_inbox_search_intent(user_text)
        and steps
        and all(step["tool"] == "chat_general" for step in steps)
    ):
        clarify = None
        steps = [
            {"tool": "search_mail", "params": {}},
            {"tool": "rank_answer", "params": {}},
        ]

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
    current = context.get("current_thread") if isinstance(context.get("current_thread"), dict) else {}
    screen = context.get("screen") if isinstance(context.get("screen"), dict) else {}
    last_draft = context.get("last_draft") if isinstance(context.get("last_draft"), dict) else {}
    thread_hint = {
        "kind": str(current.get("kind") or "none"),
        "has_message": bool(str(current.get("message_id") or "").strip()),
        "has_thread": bool(str(current.get("thread_id") or "").strip()),
        "subject": str(current.get("subject") or "")[:120],
    }
    user_message = (
        f"User request:\n{user_text}\n\n"
        f"Screen: view={screen.get('view') or 'unknown'} focus={screen.get('focus') or 'unknown'}\n"
        f"Current thread hint (no body): {thread_hint}\n"
        f"has_last_draft={bool(str(last_draft.get('body') or '').strip())} "
        f"last_draft_source={str(last_draft.get('source') or '')}\n"
        f"display_range_days={context.get('display_range_days')}\n"
        f"max_messages={context.get('max_messages')}\n"
        f"selected_threads_count={len(context.get('selected_threads') or []) if isinstance(context.get('selected_threads'), list) else 0}\n"
    )
    if conversation_summary:
        user_message += f"\n{conversation_summary}\n"
    if memory_summary:
        user_message += f"\n{memory_summary}\n"
    if sampling_create_message is None:
        return _fallback_route(user_text, context, "no_sampling")
    try:
        result = await call_llm_json_safe(
            sampling_create_message,
            system_prompt=_ROUTER_SYSTEM,
            user_message=user_message,
            fallback={},
            temperature=0.0,
            max_tokens=_ROUTER_MAX_TOKENS,
            timeout=45.0,
            metadata={"tool": "ai_turn_router"},
            allow_fallback=False,
            allow_sampling_provider_fallback=True,
            max_attempts=1,
        )
    except Exception as exc:
        return _fallback_route(user_text, context, f"router_error:{type(exc).__name__}")
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    if not payload:
        return _fallback_route(user_text, context, "empty_router_payload")
    return _normalize_route(payload, user_text, context)


__all__ = ["AI_TURN_ALLOWED_TOOLS", "route_ai_turn"]
