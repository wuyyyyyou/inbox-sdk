"""AI turn 白名单工具执行与结果组装（阶段 A）。"""

from __future__ import annotations

import logging
import re
from typing import Any

from mail_agent.ai_turn.router import route_ai_turn
from mail_agent.llm_runtime.service import extract_sampling_text

_logger = logging.getLogger(__name__)


def _uses_chinese(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _mailboxes_from_context(ui_context: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    selected = ui_context.get("selected_mailboxes")
    if isinstance(selected, list):
        mailboxes = [str(item).strip() for item in selected if str(item).strip()]
        if mailboxes:
            return mailboxes
    primary = str(ui_context.get("mailbox") or arguments.get("mailbox") or "").strip()
    return [primary] if primary else []


async def _tool_chat_general(
    user_text: str,
    *,
    language: str,
    sampling_create_message: Any,
) -> dict[str, Any]:
    """普通闲聊：走 budgeted Sampling，不访问邮箱。"""
    system = (
        "You are Anna, a concise inbox assistant. "
        "Answer naturally. Do not claim you scanned email unless tools did. "
        f"Respond in {'Chinese' if language == 'zh' else 'English'}."
    )
    if sampling_create_message is None:
        text = (
            "你好，我在。你可以和我聊天，也可以让我帮你查找或总结邮件。"
            if language == "zh"
            else "Hi, I'm here. You can chat, or ask me to find or summarize email."
        )
        return {"kind": "chat", "assistant_text": text, "fallback_used": True}
    try:
        result = await sampling_create_message(
            messages=[{"role": "user", "content": {"type": "text", "text": user_text}}],
            max_tokens=500,
            system_prompt=system,
            temperature=0.4,
            include_context="none",
            metadata={"tool": "ai_turn_chat"},
            timeout=60.0,
        )
        text = extract_sampling_text(result).strip()
    except Exception as exc:
        _logger.warning("ai_turn chat_general failed: error_type=%s", type(exc).__name__)
        text = ""
    if not text:
        text = (
            "你好，我在。聊天模型暂时不可用，你仍可让我搜索或总结邮件。"
            if language == "zh"
            else "Hi, I'm here. Chat is temporarily limited; you can still ask me to search or summarize email."
        )
        return {"kind": "chat", "assistant_text": text, "fallback_used": True}
    return {"kind": "chat", "assistant_text": text, "fallback_used": False}


async def _tool_summarize_thread(
    user_text: str,
    ui_context: dict[str, Any],
    *,
    language: str,
    sampling_create_message: Any,
) -> dict[str, Any]:
    """总结当前打开的邮件线程。"""
    from mail_agent.llm_runtime.service import call_llm_json_safe
    from mail_agent.mail_providers.gmail.adapter import get_message_detail, normalize_mailbox

    current = ui_context.get("current_thread") if isinstance(ui_context.get("current_thread"), dict) else {}
    mailbox = normalize_mailbox(str(current.get("mailbox") or ui_context.get("mailbox") or ""))
    message_id = str(current.get("message_id") or "").strip()
    thread_id = str(current.get("thread_id") or message_id).strip()
    if not mailbox or not message_id:
        clarify = (
            "请先打开一封邮件，再让我总结。"
            if language == "zh"
            else "Open an email first, then ask me to summarize it."
        )
        return {"kind": "clarify", "assistant_text": clarify, "clarify": clarify}

    body = ""
    subject = str(current.get("subject") or "")
    snippet = str(current.get("snippet") or "")
    try:
        detail = get_message_detail(mailbox, message_id)
        if detail:
            body = (getattr(detail, "body_text", "") or "")[:2000]
            subject = subject or (getattr(detail, "subject", "") or "")
            snippet = snippet or (getattr(detail, "snippet", "") or "")
    except Exception:
        pass

    fallback = (
        f"这是关于「{subject or '当前邮件'}」的摘要。正文未能完整读取时，请打开邮件查看详情。"
        if language == "zh"
        else f"Summary for “{subject or 'this email'}”. Open the message if the body could not be fully read."
    )
    if sampling_create_message is None:
        text = f"{fallback}\n\n{snippet}".strip()
        return {
            "kind": "mail_context",
            "assistant_text": text,
            "mail_context": {
                "kind": "thread",
                "mailbox": mailbox,
                "message_id": message_id,
                "thread_id": thread_id,
                "subject": subject,
            },
            "fallback_used": True,
        }

    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=(
            "You are Anna. Summarize the email for the user. Return JSON only: "
            '{"assistant_text": string}. Use only the provided evidence. '
            "Structure as purpose / key facts / user actions when helpful. "
            f"Language: {'Chinese' if language == 'zh' else 'English'}."
        ),
        user_message=(
            f"User request: {user_text}\n"
            f"Subject: {subject}\nSnippet: {snippet}\nBody excerpt:\n{body or '(empty)'}\n"
        ),
        fallback={"assistant_text": fallback},
        temperature=0.2,
        max_tokens=900,
        timeout=60.0,
        metadata={"tool": "ai_turn_summarize_thread"},
        allow_fallback=True,
        allow_sampling_provider_fallback=True,
        max_attempts=1,
    )
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    text = str(payload.get("assistant_text") or fallback).strip()
    return {
        "kind": "mail_context",
        "assistant_text": text,
        "mail_context": {
            "kind": "thread",
            "mailbox": mailbox,
            "message_id": message_id,
            "thread_id": thread_id,
            "subject": subject,
        },
        "fallback_used": bool(result.get("fallback_used")),
    }


async def _tool_search_and_answer(
    user_text: str,
    ui_context: dict[str, Any],
    arguments: dict[str, Any],
    *,
    sampling_create_message: Any,
    progress_callback: Any,
    with_rank: bool,
) -> dict[str, Any]:
    """复用 Ask 管线完成收件箱检索与回答。"""
    from mail_agent.ask.answer import run_ask_pipeline

    mailboxes = _mailboxes_from_context(ui_context, arguments)
    if not mailboxes:
        language = "zh" if _uses_chinese(user_text) else "en"
        text = "请先选择邮箱账号。" if language == "zh" else "Select a mailbox first."
        return {"kind": "error", "assistant_text": text, "error": "no_mailbox"}

    def _progress(stage: str, progress: dict[str, Any] | None = None) -> None:
        if progress_callback:
            progress_callback(stage, progress or {})

    result = await run_ask_pipeline(
        user_request=user_text,
        mailboxes=mailboxes,
        scan_window_days=ui_context.get("display_range_days") or arguments.get("scan_window_days"),
        max_messages=ui_context.get("max_messages") or arguments.get("max_messages"),
        sampling_create_message=sampling_create_message,
        progress_callback=_progress,
    )
    summary = str(result.get("summary") or result.get("plan_title") or "").strip()
    if not summary:
        summary = "Scan complete." if not _uses_chinese(user_text) else "扫描完成。"
    # with_rank 在阶段 A 与 search 合并为同一 Ask 管线，避免重复 LLM。
    _ = with_rank
    return {
        "kind": "scan",
        "assistant_text": summary,
        "scan_result": result,
        "fallback_used": bool((result.get("llm_meta") or {}).get("fallback_used")),
    }


async def run_ai_turn(
    user_text: str,
    ui_context: dict[str, Any] | None,
    arguments: dict[str, Any],
    *,
    sampling_create_message: Any = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """执行完整 AI turn：路由 → 白名单工具 → 统一结果。"""
    context = ui_context if isinstance(ui_context, dict) else {}
    text = str(user_text or "").strip()
    if not text:
        return {
            "kind": "error",
            "assistant_text": "Empty request.",
            "error": "empty_request",
            "route": {},
        }

    if progress_callback:
        progress_callback("routing", {"stage": "routing"})
    route = await route_ai_turn(text, context, sampling_create_message=sampling_create_message)
    language = str(route.get("language") or ("zh" if _uses_chinese(text) else "en"))

    if route.get("clarify") and not route.get("steps"):
        if progress_callback:
            progress_callback("done", {"stage": "clarify"})
        return {
            "kind": "clarify",
            "assistant_text": str(route.get("clarify")),
            "clarify": str(route.get("clarify")),
            "route": route,
        }

    steps = route.get("steps") if isinstance(route.get("steps"), list) else []
    tools = [str(step.get("tool") or "") for step in steps if isinstance(step, dict)]
    if progress_callback:
        progress_callback("routing_done", {"tools": tools, "router_fallback": bool(route.get("router_fallback"))})

    # 阶段 A 将 search + rank 合并为一次 Ask；多步时按首个主 tool 执行。
    primary = tools[0] if tools else "chat_general"
    if "search_mail" in tools or "rank_answer" in tools:
        primary = "search_mail"
    elif "summarize_thread" in tools:
        primary = "summarize_thread"
    elif "chat_general" in tools:
        primary = "chat_general"
    elif "clarify" in tools:
        primary = "clarify"

    if primary == "clarify":
        clarify = str(route.get("clarify") or (
            "能再具体一点吗？" if language == "zh" else "Could you be more specific?"
        ))
        return {"kind": "clarify", "assistant_text": clarify, "clarify": clarify, "route": route}

    if primary == "summarize_thread":
        if progress_callback:
            progress_callback("read", {"stage": "summarize_thread"})
        outcome = await _tool_summarize_thread(
            text, context, language=language, sampling_create_message=sampling_create_message,
        )
        outcome["route"] = route
        return outcome

    if primary == "search_mail":
        outcome = await _tool_search_and_answer(
            text,
            context,
            arguments,
            sampling_create_message=sampling_create_message,
            progress_callback=progress_callback,
            with_rank="rank_answer" in tools,
        )
        outcome["route"] = route
        return outcome

    if progress_callback:
        progress_callback("answer", {"stage": "chat_general"})
    outcome = await _tool_chat_general(
        text, language=language, sampling_create_message=sampling_create_message,
    )
    outcome["route"] = route
    return outcome


__all__ = ["run_ai_turn"]
