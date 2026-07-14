"""AI turn 意图路由：Sampling 输出白名单 tool 计划。"""

from __future__ import annotations

import logging
import re
from typing import Any

from mail_agent.llm_runtime.service import call_llm_json_safe

_logger = logging.getLogger(__name__)

# 中文注释：阶段 A 仅开放聊天 / 搜索 / 总结；其余意图在后续阶段扩展。
AI_TURN_ALLOWED_TOOLS = frozenset({
    "chat_general",
    "clarify",
    "search_mail",
    "rank_answer",
    "summarize_thread",
})

_ROUTER_MAX_TOKENS = 384
_ROUTER_SYSTEM = """You are Anna's inbox AI turn router. Choose tools from a fixed whitelist only.
Return one JSON object. First character must be `{`. No markdown.

Whitelist tools:
- chat_general: greetings, capability questions, non-mail conversation
- clarify: missing current email when user refers to "this email", or intent is ambiguous
- search_mail: find / list / prioritize / organize across the inbox (Gmail search)
- rank_answer: after search_mail, summarize or rank results (optional second step)
- summarize_thread: summarize or extract from the currently open email/thread

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
5. Hi/hello/what can you do → chat_general.
6. Never invent tools outside the whitelist. Never choose send/delete/archive tools.
"""


def _uses_chinese(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _fallback_route(user_text: str, ui_context: dict[str, Any], reason: str) -> dict[str, Any]:
    """Router 失败时的确定性降级，保证 turn 仍可执行。"""
    language = "zh" if _uses_chinese(user_text) else "en"
    current = ui_context.get("current_thread") if isinstance(ui_context.get("current_thread"), dict) else {}
    has_thread = str(current.get("kind") or "") in {"thread", "compose"} and bool(
        str(current.get("message_id") or current.get("thread_id") or "").strip()
    )
    lowered = (user_text or "").casefold()
    # 中文注释：降级只做粗粒度分流，正式选型仍以 Sampling Router 为准。
    scan_hint = any(
        token in lowered
        for token in (
            "find", "search", "inbox", "email", "mail", "urgent", "unread", "invoice",
            "找", "搜索", "收件箱", "邮件", "未读", "紧急", "发票", "整理",
        )
    )
    summary_hint = any(
        token in lowered
        for token in ("summar", "总结", "概括", "这封", "this email", "this thread", "action item", "待办")
    )
    # 中文注释：打开线程时的「总结这封邮件」会命中「邮件」关键词，须优先于全箱搜索。
    if has_thread and summary_hint:
        return {
            "language": language,
            "use_current_thread": True,
            "clarify": None,
            "steps": [{"tool": "summarize_thread", "params": {}}],
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
        return {
            "language": language,
            "use_current_thread": False,
            "clarify": clarify,
            "steps": [],
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

    current = ui_context.get("current_thread") if isinstance(ui_context.get("current_thread"), dict) else {}
    has_thread = str(current.get("kind") or "") in {"thread", "compose"} and bool(
        str(current.get("message_id") or current.get("thread_id") or "").strip()
    )
    if use_current and not has_thread:
        clarify = clarify or (
            "请先打开一封邮件，这样我才知道你指的是哪一封。"
            if language == "zh"
            else "Open an email first so I know which message you mean."
        )
        steps = [s for s in steps if s["tool"] not in {"summarize_thread"}]
        use_current = False

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
) -> dict[str, Any]:
    """对用户话术做结构化选型；失败时走确定性降级。"""
    context = ui_context if isinstance(ui_context, dict) else {}
    current = context.get("current_thread") if isinstance(context.get("current_thread"), dict) else {}
    screen = context.get("screen") if isinstance(context.get("screen"), dict) else {}
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
        f"display_range_days={context.get('display_range_days')}\n"
        f"max_messages={context.get('max_messages')}\n"
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
