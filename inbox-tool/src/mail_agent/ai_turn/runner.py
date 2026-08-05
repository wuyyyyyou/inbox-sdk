"""AI turn 白名单工具执行与结果组装（阶段 C）。"""

from __future__ import annotations

import logging
import re
from typing import Any

from mail_agent.ai_turn.prompts import chat_general_system_prompt, thread_answer_system_prompt
from mail_agent.ai_turn.registry import format_summary_for_router, record_turn_summary
from mail_agent.ai_turn.router import route_ai_turn
from mail_agent.llm_runtime.service import (
    _ascii_escape_for_host_transport,
    call_llm_json_safe,
    extract_sampling_text,
)

_logger = logging.getLogger(__name__)

# 批量草稿的二次确认状态仅保存在进程内，且只保存线程指纹与结构化分类，
# 不保存邮件正文、查询内容或凭证。conversation_id 是确认轮的必要边界。
_BATCH_DRAFT_PREVIEWS: dict[str, dict[str, Any]] = {}
_THREAD_DRAFT_PREVIEWS: dict[str, dict[str, str]] = {}
_DRAFT_CONFIRM_RE = re.compile(
    r"^(?:yes|y|sure|ok|okay|continue|confirm|go ahead|好的?|可以|开始吧|开始生成|确认|同意|继续|就这样|用这个|生成卡片|生成草稿)[.!。！？\s]*$",
    re.IGNORECASE,
)

# 线程引用只供系统内部和前端详情跳转使用，绝不能成为用户可见的回答内容。
# 这里严格限制 token 的前缀、方括号和 ID 字符，避免误删普通 Markdown 方括号文本。
_THREAD_REF_TOKEN_RE = re.compile(r"\[THREAD_?REF_[A-Za-z0-9_-]+\]")


def _sanitize_thread_answer_markdown(markdown: str) -> str:
    """移除线程内部引用标记，并清理移除后产生的冗余空白。

    模型可能返回 ``[THREAD_REF_x]`` 或 ``[THREADREF_x]``。这些标记是内部
    跳转协议，不属于邮件摘要，因此在写入 assistant_text 前统一删除。清理
    空白时只处理 token 删除造成的连续横向空格、行尾空格和多余空行，不做
    全文折叠，避免破坏 Markdown 列表缩进、代码块和表格布局。
    """
    cleaned = _THREAD_REF_TOKEN_RE.sub("", str(markdown or ""))
    cleaned = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", cleaned)
    return cleaned.strip()


def _batch_thread_keys(ui_context: dict[str, Any]) -> tuple[str, ...]:
    """提取当前选中线程的稳定指纹，用于防止确认误作用于另一批邮件。"""
    selected = ui_context.get("selected_threads")
    if not isinstance(selected, list):
        return ()
    keys: set[str] = set()
    for item in selected:
        if not isinstance(item, dict):
            continue
        mailbox = str(item.get("mailbox") or ui_context.get("mailbox") or "").strip()
        thread_id = str(item.get("thread_id") or item.get("message_id") or "").strip()
        if mailbox and thread_id:
            keys.add(f"{mailbox}|{thread_id}")
    return tuple(sorted(keys))


def _explicit_batch_confirmation(user_text: str) -> bool:
    """只接受明确的确认短语，避免把原始起草请求当成确认。"""
    text = str(user_text or "").strip().casefold()
    if not text:
        return False
    return bool(re.fullmatch(
        r"(?:yes|ok|okay|confirm|confirmed|approve|approved|go ahead|do it|生成草稿|确认生成草稿|确认|同意|可以|好的)",
        text,
    ))


def _batch_confirmation_state(
    user_text: str,
    ui_context: dict[str, Any],
    conversation_id: str,
) -> dict[str, Any] | None:
    """返回有效确认状态；必须同时满足会话、明确确认词和选中集合未变化。"""
    if not conversation_id or not _explicit_batch_confirmation(user_text):
        return None
    state = _BATCH_DRAFT_PREVIEWS.get(conversation_id)
    if not isinstance(state, dict) or tuple(state.get("thread_keys") or ()) != _batch_thread_keys(ui_context):
        return None
    return state


def _draft_confirmation_state(user_text: str, conversation_id: str) -> dict[str, str] | None:
    if not conversation_id or not _DRAFT_CONFIRM_RE.fullmatch(str(user_text or "").strip()):
        return None
    state = _THREAD_DRAFT_PREVIEWS.get(conversation_id)
    return dict(state) if isinstance(state, dict) else None


def _draft_target_from_evidence(evidence: dict[str, Any], default_mailbox: str) -> dict[str, str] | None:
    results = evidence.get("results") if isinstance(evidence.get("results"), list) else []
    for item in results:
        if not isinstance(item, dict):
            continue
        mailbox = str(item.get("mailbox") or default_mailbox).strip()
        message_id = str(item.get("message_id") or "").strip()
        thread_id = str(item.get("thread_id") or "").strip()
        if mailbox and (message_id or thread_id):
            return {
                "mailbox": mailbox,
                "message_id": message_id,
                "thread_id": thread_id or message_id,
                "subject": str(item.get("subject") or "").strip()[:200],
            }
    return None

def _uses_chinese(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _mailboxes_from_context(ui_context: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    """Ask 仅检索当前活动邮箱，避免多账号选择状态扩大一次问答的读取范围。

    ``selected_mailboxes`` 仍会作为只读界面上下文传入 Router，但不属于 Ask 的
    检索授权范围。当前邮箱缺失时才回退到工具 arguments，保证旧调用仍有明确目标。
    """
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
    system = chat_general_system_prompt(language, memory_summary)
    if sampling_create_message is None:
        text = (
            "你好，我是 AI 助理。你可以让我搜索、总结、起草回复或建议整理收件箱（整理须你确认）。"
            if language == "zh"
            else "Hi, I'm your AI assistant. I can search, summarize, draft replies, or suggest inbox organization (you confirm actions)."
        )
        return {"kind": "chat", "assistant_text": text, "fallback_used": True}
    try:
        # 纯聊天只进行一次最终回答生成。非 ASCII 文本仍需在直连 Sampling 前转义，
        # 防止 Windows Anna bridge 通过 GBK stdout 写反向 RPC 时发生编码崩溃。
        result = await sampling_create_message(
            messages=[{
                "role": "user",
                "content": {
                    "type": "text",
                    "text": _ascii_escape_for_host_transport(user_text),
                },
            }],
            # 闲聊输出不宜过紧：500 易在句中被 Host stopReason=length 截断。
            max_tokens=2048,
            system_prompt=_ascii_escape_for_host_transport(system),
            temperature=0.4,
            include_context="none",
            metadata={"tool": "ai_turn_chat"},
            timeout=60.0,
        )
        text = extract_sampling_text(result).strip()
    except Exception as exc:
        _logger.warning("ai_turn chat_general failed: error_type=%s", type(exc).__name__)
        text = ""

    if text:
        return {"kind": "chat", "assistant_text": text, "fallback_used": False}

    unavailable = "AI 聊天暂时不可用，请稍后重试。" if language == "zh" else "AI chat is temporarily unavailable. Please try again."
    return {"kind": "error", "assistant_text": unavailable, "error": "chat_unavailable", "fallback_used": False}


async def tool_summarize_thread(
    user_text: str,
    ui_context: dict[str, Any],
    *,
    language: str,
    sampling_create_message: Any,
    memory_summary: str = "",
) -> dict[str, Any]:
    """基于当前线程回答邮件问题，并把自然语言主体限制为 Markdown。

    线程问题不再先经过关键词 Router。当前线程本身就是稳定的执行边界；模型
    只负责根据证据回答问题，状态变更和发送仍由独立的确认工具处理。
    """
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
        # 仅保留最近 4 封、每封 600 字符，避免长引用链挤占模型输出空间。
        from mail_agent.mail_providers.gmail.adapter import get_thread_context, refresh_thread_cache

        # 当前线程优先取 Gmail 最新内容；失败时仍可由下方缓存读取继续回答。
        try:
            refresh_thread_cache(mailbox, thread_id)
        except Exception:
            pass
        thread = get_thread_context(mailbox, thread_id, max_messages=4)
        excerpts: list[str] = []
        for index, item in enumerate(getattr(thread, "messages", [])[-4:], start=1):
            item_body = str(getattr(item, "body_text", "") or "").strip()[:600]
            item_subject = str(getattr(item, "subject", "") or "").strip()
            item_from = str(getattr(item, "from_addr", "") or "").strip()
            if item_body or item_subject:
                excerpts.append(
                    f"Message {index}\nFrom: {item_from}\nSubject: {item_subject}\nBody: {item_body}"
                )
        body = "\n\n---\n\n".join(excerpts)
        if not body:
            detail = get_message_detail(mailbox, message_id)
            if detail:
                body = (getattr(detail, "body_text", "") or "")[:1200]
                subject = subject or (getattr(detail, "subject", "") or "")
                snippet = snippet or (getattr(detail, "snippet", "") or "")
    except Exception:
        pass

    if sampling_create_message is None:
        return {
            "kind": "error",
            "assistant_text": "AI 总结暂时不可用，请稍后重试。" if language == "zh" else "AI summary is temporarily unavailable. Please try again.",
            "error": "analysis_unavailable",
            "fallback_used": False,
        }

    system_prompt = thread_answer_system_prompt(language, memory_summary)

    try:
        result = await call_llm_json_safe(
            sampling_create_message,
            system_prompt=system_prompt,
            user_message=(
                f"Question: {user_text}\n"
                f"Subject: {subject}\n"
                f"Thread evidence:\n{body or snippet or '(empty)'}\n"
            ),
            fallback={},
            temperature=0.2,
            max_tokens=1600,
            timeout=45.0,
            metadata={"tool": "ai_turn_thread_answer"},
            response_format={"type": "json_object"},
            on_unsupported="text",
            allow_fallback=False,
            allow_sampling_provider_fallback=True,
            max_attempts=2,
        )
    except Exception as exc:
        _logger.warning("ai_turn thread summary failed: error_type=%s", type(exc).__name__)
        return {
            "kind": "error",
            "assistant_text": "AI 总结暂时不可用，请稍后重试。" if language == "zh" else "AI summary is temporarily unavailable. Please try again.",
            "error": "analysis_unavailable",
            "fallback_used": False,
        }
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    # 模型输出可能夹带内部 THREAD_REF，必须先清洗再暴露给前端。
    text = _sanitize_thread_answer_markdown(str(payload.get("markdown") or ""))
    if not text:
        return {
            "kind": "error",
            "assistant_text": "AI 总结暂时不可用，请稍后重试。" if language == "zh" else "AI summary is temporarily unavailable. Please try again.",
            "error": "analysis_unavailable",
            "fallback_used": False,
        }
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


async def tool_search_and_answer(
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

    todo_raw = ui_context.get("todo_message_ids") if isinstance(ui_context.get("todo_message_ids"), list) else []
    todo_ids = [str(item) for item in todo_raw if str(item).strip()]
    result = await run_ask_pipeline(
        user_request=user_text,
        mailboxes=mailboxes,
        scan_window_days=ui_context.get("display_range_days") or arguments.get("scan_window_days"),
        max_messages=ui_context.get("max_messages") or arguments.get("max_messages"),
        sampling_create_message=sampling_create_message,
        progress_callback=_progress,
        todo_ids=todo_ids,
    )
    summary = str(result.get("summary") or result.get("plan_title") or "").strip()
    if result.get("analysis_error"):
        # Ask 已确认检索完成但分析模型未返回有效 JSON。该情况必须进入失败态，
        # 让侧栏显示重试入口，不能将候选邮件或计划信息当作最终分析输出。
        return {
            "kind": "error",
            "assistant_text": summary,
            "error": "analysis_unavailable",
            "fallback_used": False,
        }
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
    scan_query = str(result.get("scan_query") or "").strip()
    return {
        "kind": "scan",
        "assistant_text": summary,
        "scan_result": result,
        "scan_query": scan_query,
        "scan_source": "cache",
        "fallback_used": bool(
            result.get("fallback_used") or (result.get("llm_meta") or {}).get("fallback_used")
        ),
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

    # 邮箱上下文只是页面状态而非读取授权。除显式 UI artifact 外，本轮由
    # Router 模型判断聊天、搜索或线程操作，不能在后端用关键词硬编码分流。
    language = "zh" if _uses_chinese(text) else "en"
    current = context.get("current_thread") if isinstance(context.get("current_thread"), dict) else {}
    requested_artifact = str(
        context.get("requested_artifact") or arguments.get("requested_artifact") or ""
    ).strip()

    recent = context.get("recent_conversation") if isinstance(context.get("recent_conversation"), list) else []
    if _explicit_batch_confirmation(text):
        for item in reversed(recent):
            if not isinstance(item, dict) or str(item.get("role") or "") != "user":
                continue
            source = str(item.get("content") or "")
            if len(set(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", source, re.IGNORECASE))) < 2:
                continue
            from mail_agent.ai_turn.tools import tool_batch_compose_new
            return await tool_batch_compose_new(
                source, context, arguments, language=language,
                sampling_create_message=sampling_create_message, memory_summary=memory_summary,
                confirmed=True, progress_callback=progress_callback,
            )

    # 批量确认必须在 Router 前拦截：确认词本身没有“批量起草”关键词，
    # 交给 Router 可能会误路由为闲聊或新的批量预览。
    batch_state = _batch_confirmation_state(text, context, conversation_id)
    if batch_state is not None:
        from mail_agent.ai_turn.tools import tool_batch_draft, tool_batch_outreach

        confirmed_tool = str(batch_state.get("tool") or "batch_draft")
        if progress_callback:
            progress_callback("draft", {"stage": confirmed_tool, "confirmed": True})
        common = {
            "language": str(batch_state.get("language") or ("zh" if _uses_chinese(text) else "en")),
            "sampling_create_message": sampling_create_message,
            "memory_summary": "",
            "confirmed": True,
            "skip_thread_keys": set(str(item) for item in (batch_state.get("skipped_thread_keys") or [])),
            "skipped_subjects": [str(item)[:200] for item in (batch_state.get("skipped_subjects") or [])],
            "progress_callback": progress_callback,
        }
        if confirmed_tool == "batch_outreach":
            outcome = await tool_batch_outreach(text, context, **common)
        else:
            outcome = await tool_batch_draft(text, context, **common)
        outcome["route"] = {"execution": "batch_draft_confirmed", "tool": confirmed_tool}
        _BATCH_DRAFT_PREVIEWS.pop(conversation_id, None)
        return outcome

    from mail_agent.ai_turn.router import _explicit_current_thread_request

    draft_confirmation = _draft_confirmation_state(text, conversation_id)
    if draft_confirmation is not None:
        draft_context = dict(context)
        draft_context["current_thread"] = {
            "kind": "thread",
            **draft_confirmation,
        }
        outcome = await tool_draft_reply(
            text,
            draft_context,
            language=language,
            sampling_create_message=sampling_create_message,
            memory_summary=memory_summary,
            mode=("draft_forward" if str(context.get("draft_composer_mode") or "") == "forward" else "draft_reply"),
        )
        _THREAD_DRAFT_PREVIEWS.pop(conversation_id, None)
        outcome["route"] = {"execution": "thread_draft_confirmed"}
        return outcome

    if requested_artifact in {"draft_reply", "send_plan"}:
        from mail_agent.evidence_flow import query_mail_evidence

        evidence_context = dict(context)
        evidence_context["current_thread"] = dict(current)
        explicit_current = _explicit_current_thread_request(text)
        evidence = await query_mail_evidence(
            text,
            evidence_context,
            sampling_create_message=sampling_create_message,
            conversation_id=conversation_id,
            scope_kind="current_thread" if explicit_current else "all_indexed",
        )
        if str(evidence.get("match_status") or "") not in {"confirmed", "candidate_match"}:
            return {
                "kind": "clarify",
                "assistant_text": str(evidence.get("assistant_text") or "未找到当前邮件的可用内容，暂时无法起草回复。"),
                "evidence": evidence,
                "route": {"execution": "thread_evidence"},
            }
        target = _draft_target_from_evidence(evidence, str(context.get("mailbox") or "").strip())
        if target is None:
            return {
                "kind": "clarify",
                "assistant_text": "已找到邮件，但无法确认可回复的线程。请提供更完整的主题或联系人。",
                "route": {"execution": "thread_evidence"},
            }
        if conversation_id:
            _THREAD_DRAFT_PREVIEWS[conversation_id] = target
        summary = str(evidence.get("assistant_text") or "已确认目标邮件。").strip()
        confirmation = "我已确认目标邮件。请确认是否生成回复草稿。"
        return {
            "kind": "clarify",
            "assistant_text": f"{summary}\n\n{confirmation}",
            "clarify": confirmation,
            "match_status": evidence.get("match_status"),
            "results": evidence.get("results") or [],
            "route": {"execution": "thread_draft_preview"},
        }

    if requested_artifact == "revise_draft":
        if progress_callback:
            progress_callback("draft", {"stage": "revise_draft"})
        outcome = await tool_revise_draft(
            text,
            context,
            language=language,
            sampling_create_message=sampling_create_message,
            memory_summary=memory_summary,
        )
        outcome["route"] = {"execution": "revise_draft"}
        return outcome

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
        if isinstance(route.get("clarification"), dict):
            outcome["clarification"] = route["clarification"]
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
        if isinstance(route.get("clarification"), dict):
            outcome["clarification"] = route["clarification"]
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
                progress_callback=progress_callback,
            )
        else:
            outcome = await tool_batch_draft(
                text,
                context,
                language=language,
                sampling_create_message=sampling_create_message,
                memory_summary=memory_summary,
                progress_callback=progress_callback,
            )
        if outcome.get("kind") == "batch_draft_preview" and conversation_id:
            # 状态只保留结构化的线程集合和跳过集合，绝不持有正文。
            preview_items = outcome.get("batch_preview") if isinstance(outcome.get("batch_preview"), list) else []
            skipped_items = outcome.get("skipped_no_action") if isinstance(outcome.get("skipped_no_action"), list) else []
            _BATCH_DRAFT_PREVIEWS[conversation_id] = {
                "tool": primary,
                "language": language,
                "thread_keys": _batch_thread_keys(context),
                "skipped_thread_keys": [
                    f"{item.get('mailbox')}|{item.get('thread_id')}"
                    for item in skipped_items
                    if isinstance(item, dict)
                ],
                "skipped_subjects": [
                    str(item.get("subject") or "")[:200]
                    for item in skipped_items
                    if isinstance(item, dict)
                ],
                "preview_count": len(preview_items),
            }
        outcome["route"] = route
        return _record(primary, outcome)

    if primary == "batch_compose":
        from mail_agent.ai_turn.tools import tool_batch_compose_new
        outcome = await tool_batch_compose_new(
            text, context, arguments, language=language,
            sampling_create_message=sampling_create_message, memory_summary=memory_summary,
            progress_callback=progress_callback,
        )
        outcome["route"] = route
        return _record("batch_compose", outcome)

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
        search_out = await tool_search_and_answer(
            text,
            context,
            arguments,
            sampling_create_message=sampling_create_message,
            progress_callback=progress_callback,
            with_rank="rank_answer" in tools,
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
        outcome = await tool_summarize_thread(
            text,
            context,
            language=language,
            sampling_create_message=sampling_create_message,
            memory_summary=memory_summary,
        )
        outcome["route"] = route
        return _record("summarize_thread", outcome)

    if primary == "search_mail":
        outcome = await tool_search_and_answer(
            text,
            context,
            arguments,
            sampling_create_message=sampling_create_message,
            progress_callback=progress_callback,
            with_rank=True,
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


__all__ = [
    "_sanitize_thread_answer_markdown",
    "run_ai_turn",
    "tool_search_and_answer",
    "tool_summarize_thread",
]
