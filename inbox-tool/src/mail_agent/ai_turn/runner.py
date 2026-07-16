"""AI turn 白名单工具执行与结果组装（阶段 C）。"""

from __future__ import annotations

import logging
import re
from typing import Any

from mail_agent.ai_turn.registry import format_summary_for_router, record_turn_summary
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
    memory_summary: str = "",
) -> dict[str, Any]:
    """普通闲聊：走 budgeted Sampling，不访问邮箱。"""
    system = (
        "You are Anna, a concise inbox assistant (AI 助理). "
        "Answer naturally. Do not claim you scanned email unless tools did. "
        "You can help organize (suggest only), search, draft/revise, and analyze email. "
        "No calendar auto-scheduling, no silent Gmail mutations. "
        f"Respond in {'Chinese' if language == 'zh' else 'English'}."
    )
    if memory_summary:
        system += f"\n{memory_summary}"
    if sampling_create_message is None:
        text = (
            "你好，我是 AI 助理。你可以让我搜索、总结、起草回复或建议整理收件箱（整理须你确认）。"
            if language == "zh"
            else "Hi, I'm your AI assistant. I can search, summarize, draft replies, or suggest inbox organization (you confirm actions)."
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
    memory_summary: str = "",
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

    system_prompt = (
        "You are Anna. Summarize the email for the user. Return JSON only: "
        '{"assistant_text": string}. Use only the provided evidence. '
        "Structure as purpose / key facts / user actions when helpful. "
        f"Language: {'Chinese' if language == 'zh' else 'English'}."
    )
    if memory_summary:
        system_prompt += f"\n{memory_summary}"

    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=system_prompt,
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
    # 失败时不伪装本地邮件列表成功回答
    if result.get("error") or (result.get("success") is False):
        language = "zh" if _uses_chinese(user_text) else "en"
        err = str(result.get("error") or "search_failed")[:200]
        text = (
            f"搜索未能完成：{err}"
            if language == "zh"
            else f"Search could not complete: {err}"
        )
        return {
            "kind": "error",
            "assistant_text": text,
            "error": err,
            "fallback_used": True,
        }
    if not summary:
        summary = "Scan complete." if not _uses_chinese(user_text) else "扫描完成。"
    _ = with_rank
    return {
        "kind": "scan",
        "assistant_text": summary,
        "scan_result": result,
        "fallback_used": bool((result.get("llm_meta") or {}).get("fallback_used")),
    }


def _primary_tool(tools: list[str]) -> str:
    """从 steps 选出主执行工具（阶段 C 多步策略）。"""
    if not tools:
        return "chat_general"
    # 整理建议优先于纯搜索
    if "propose_inbox_actions" in tools:
        return "propose_inbox_actions"
    if "remember_preference" in tools:
        return "remember_preference"
    if "batch_outreach" in tools:
        return "batch_outreach"
    if "batch_draft" in tools:
        return "batch_draft"
    if "revise_draft" in tools:
        return "revise_draft"
    if "summarize_then_draft" in tools:
        return "summarize_then_draft"
    if "draft_reply" in tools:
        return "draft_reply"
    if "compose_new" in tools:
        # 可与 search 组合：先搜后写
        return "compose_new" if "search_mail" not in tools else "search_then_compose"
    if "search_mail" in tools or "rank_answer" in tools:
        return "search_mail"
    if "summarize_thread" in tools:
        return "summarize_thread"
    if "chat_general" in tools:
        return "chat_general"
    if "clarify" in tools:
        return "clarify"
    return tools[0]


async def run_ai_turn(
    user_text: str,
    ui_context: dict[str, Any] | None,
    arguments: dict[str, Any],
    *,
    sampling_create_message: Any = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """执行完整 AI turn：路由 → 白名单工具 → 统一结果。"""
    from mail_agent.ai_turn.personalization import (
        format_memory_summary_for_prompt,
        get_saved_prompt,
    )
    from mail_agent.ai_turn.tools import (
        tool_batch_draft,
        tool_batch_outreach,
        tool_compose_new,
        tool_draft_reply,
        tool_propose_inbox_actions,
        tool_remember_preference,
        tool_revise_draft,
    )

    context = ui_context if isinstance(ui_context, dict) else {}
    text = str(user_text or "").strip()
    conversation_id = str(
        arguments.get("conversation_id")
        or context.get("conversation_id")
        or ""
    ).strip()

    # Saved prompt 注入：以完整 user 文本前缀形式加入（不单独路由）
    saved_prompt_id = str(context.get("saved_prompt_id") or arguments.get("saved_prompt_id") or "").strip()
    if saved_prompt_id:
        try:
            prompt = await get_saved_prompt(saved_prompt_id)
            if prompt and str(prompt.get("body") or "").strip():
                body = str(prompt.get("body")).strip()
                if body not in text:
                    text = f"{body}\n\n{text}".strip() if text else body
        except Exception as exc:
            _logger.warning("ai_turn saved_prompt load failed: error_type=%s", type(exc).__name__)

    if not text:
        return {
            "kind": "error",
            "assistant_text": "Empty request.",
            "error": "empty_request",
            "route": {},
        }

    memory_summary = ""
    try:
        memory_summary = await format_memory_summary_for_prompt()
    except Exception as exc:
        _logger.warning("ai_turn memory summary failed: error_type=%s", type(exc).__name__)

    conversation_summary = format_summary_for_router(conversation_id)

    if progress_callback:
        progress_callback("routing", {"stage": "routing"})
    route = await route_ai_turn(
        text,
        context,
        sampling_create_message=sampling_create_message,
        memory_summary=memory_summary,
        conversation_summary=conversation_summary,
    )
    language = str(route.get("language") or ("zh" if _uses_chinese(text) else "en"))

    def _record(tool: str, outcome: dict[str, Any]) -> dict[str, Any]:
        """写入多轮摘要 registry。"""
        kind = str(outcome.get("kind") or "")
        has_draft = bool(
            (
                isinstance(outcome.get("artifact"), dict)
                and str(outcome["artifact"].get("body") or "").strip()
            )
            or (
                isinstance(outcome.get("artifacts"), list)
                and any(
                    isinstance(item, dict) and str(item.get("body") or "").strip()
                    for item in outcome["artifacts"]
                )
            )
        )
        candidate_count = 0
        if isinstance(outcome.get("proposed_actions"), dict):
            items = outcome["proposed_actions"].get("items")
            if isinstance(items, list):
                candidate_count = len(items)
        scan = outcome.get("scan_result") if isinstance(outcome.get("scan_result"), dict) else {}
        if scan:
            candidate_count = int(scan.get("candidates_found") or candidate_count or 0)
        record_turn_summary(
            conversation_id,
            tool=tool,
            success=kind not in {"error"},
            candidate_count=candidate_count,
            has_draft=has_draft,
            search_intent=text[:80] if tool in {"search_mail", "propose_inbox_actions"} else "",
            kind=kind,
        )
        return outcome

    if route.get("clarify") and not route.get("steps"):
        if progress_callback:
            progress_callback("done", {"stage": "clarify"})
        outcome = {
            "kind": "clarify",
            "assistant_text": str(route.get("clarify")),
            "clarify": str(route.get("clarify")),
            "route": route,
        }
        return _record("clarify", outcome)

    steps = route.get("steps") if isinstance(route.get("steps"), list) else []
    tools = [str(step.get("tool") or "") for step in steps if isinstance(step, dict)]
    step_params = {
        str(step.get("tool") or ""): (step.get("params") if isinstance(step.get("params"), dict) else {})
        for step in steps
        if isinstance(step, dict)
    }
    if progress_callback:
        progress_callback("routing_done", {"tools": tools, "router_fallback": bool(route.get("router_fallback"))})

    primary = _primary_tool(tools)

    if primary == "clarify":
        clarify = str(route.get("clarify") or (
            "能再具体一点吗？" if language == "zh" else "Could you be more specific?"
        ))
        outcome = {"kind": "clarify", "assistant_text": clarify, "clarify": clarify, "route": route}
        return _record("clarify", outcome)

    if primary == "remember_preference":
        if progress_callback:
            progress_callback("answer", {"stage": "remember_preference"})
        outcome = await tool_remember_preference(
            text, language=language, params=step_params.get("remember_preference"),
        )
        outcome["route"] = route
        return _record("remember_preference", outcome)

    if primary == "revise_draft":
        if progress_callback:
            progress_callback("draft", {"stage": "revise_draft"})
        outcome = await tool_revise_draft(
            text,
            context,
            language=language,
            sampling_create_message=sampling_create_message,
            memory_summary=memory_summary,
        )
        outcome["route"] = route
        return _record("revise_draft", outcome)

    if primary in {"draft_reply", "summarize_then_draft"}:
        if progress_callback:
            progress_callback("draft", {"stage": primary})
        outcome = await tool_draft_reply(
            text,
            context,
            language=language,
            sampling_create_message=sampling_create_message,
            memory_summary=memory_summary,
            mode=primary,
        )
        outcome["route"] = route
        return _record(primary, outcome)

    if primary in {"batch_draft", "batch_outreach"}:
        if progress_callback:
            progress_callback("draft", {"stage": primary})
        if primary == "batch_outreach":
            outcome = await tool_batch_outreach(
                text,
                context,
                language=language,
                sampling_create_message=sampling_create_message,
                memory_summary=memory_summary,
            )
        else:
            outcome = await tool_batch_draft(
                text,
                context,
                language=language,
                sampling_create_message=sampling_create_message,
                memory_summary=memory_summary,
            )
        outcome["route"] = route
        return _record(primary, outcome)

    if primary == "propose_inbox_actions":
        if progress_callback:
            progress_callback("propose", {"stage": "propose_inbox_actions"})
        # Memory「使用中文回复」等偏好可覆盖 Router 语言，供卡片文案使用
        propose_language = language
        mem_lower = (memory_summary or "").casefold()
        if any(token in mem_lower for token in ("使用中文", "中文回复", "reply in chinese", "use chinese", "respond in chinese")):
            propose_language = "zh"
        elif any(token in mem_lower for token in ("use english", "reply in english", "respond in english", "英文回复")):
            propose_language = "en"
        outcome = await tool_propose_inbox_actions(
            text,
            context,
            arguments,
            language=propose_language,
            sampling_create_message=sampling_create_message,
            progress_callback=progress_callback,
        )
        outcome["route"] = route
        return _record("propose_inbox_actions", outcome)

    if primary == "search_then_compose":
        # 先检索，证据截断后 compose_new
        if progress_callback:
            progress_callback("search", {"stage": "search_mail"})
        search_out = await _tool_search_and_answer(
            text,
            context,
            arguments,
            sampling_create_message=sampling_create_message,
            progress_callback=progress_callback,
            with_rank=True,
        )
        if search_out.get("kind") == "error":
            search_out["route"] = route
            return _record("search_mail", search_out)
        evidence = str(search_out.get("assistant_text") or "")[:1200]
        if progress_callback:
            progress_callback("draft", {"stage": "compose_new"})
        outcome = await tool_compose_new(
            text,
            context,
            arguments,
            language=language,
            sampling_create_message=sampling_create_message,
            memory_summary=memory_summary,
            prior_evidence=evidence,
        )
        # 附带搜索摘要便于前端展示
        if isinstance(search_out.get("scan_result"), dict):
            outcome["scan_result"] = search_out["scan_result"]
        outcome["route"] = route
        return _record("compose_new", outcome)

    if primary == "compose_new":
        if progress_callback:
            progress_callback("draft", {"stage": "compose_new"})
        outcome = await tool_compose_new(
            text,
            context,
            arguments,
            language=language,
            sampling_create_message=sampling_create_message,
            memory_summary=memory_summary,
        )
        outcome["route"] = route
        return _record("compose_new", outcome)

    if primary == "summarize_thread":
        if progress_callback:
            progress_callback("read", {"stage": "summarize_thread"})
        outcome = await _tool_summarize_thread(
            text,
            context,
            language=language,
            sampling_create_message=sampling_create_message,
            memory_summary=memory_summary,
        )
        outcome["route"] = route
        return _record("summarize_thread", outcome)

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
        return _record("search_mail", outcome)

    if progress_callback:
        progress_callback("answer", {"stage": "chat_general"})
    outcome = await _tool_chat_general(
        text,
        language=language,
        sampling_create_message=sampling_create_message,
        memory_summary=memory_summary,
    )
    outcome["route"] = route
    return _record("chat_general", outcome)


__all__ = ["run_ai_turn"]
