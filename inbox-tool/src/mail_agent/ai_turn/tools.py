"""AI turn 白名单工具实现：写稿、批量、整理建议、记忆写入（阶段 C）。"""

from __future__ import annotations

import logging
import re
from typing import Any

from mail_agent.ai_turn.prompts import (
    batch_draft_system_prompt,
    compose_new_system_prompt,
    draft_reply_system_prompt,
    revise_draft_system_prompt,
)
from mail_agent.llm_runtime.service import call_llm_json_safe, extract_sampling_text

_logger = logging.getLogger(__name__)

_BODY_LIMIT = 2000
_DRAFT_LIMIT = 8000
# 批量写稿上限：控制 Sampling 次数与前端展示体积
_BATCH_MAX_THREADS = 5


def _uses_chinese(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _current_thread(ui_context: dict[str, Any]) -> dict[str, Any]:
    current = ui_context.get("current_thread") if isinstance(ui_context.get("current_thread"), dict) else {}
    return current


def _last_draft_body(ui_context: dict[str, Any]) -> str:
    last = ui_context.get("last_draft") if isinstance(ui_context.get("last_draft"), dict) else {}
    return str(last.get("body") or "").strip()[:_DRAFT_LIMIT]


async def _load_thread_excerpt(mailbox: str, message_id: str, thread_id: str) -> dict[str, Any]:
    """读取线程摘录供写稿使用。"""
    from mail_agent.mail_providers.gmail.adapter import get_message_detail, normalize_mailbox

    mailbox = normalize_mailbox(mailbox)
    subject = ""
    snippet = ""
    body = ""
    from_addr = ""
    if message_id:
        try:
            detail = get_message_detail(mailbox, message_id)
            if detail:
                subject = str(getattr(detail, "subject", "") or "")
                snippet = str(getattr(detail, "snippet", "") or "")
                body = (getattr(detail, "body_text", "") or "")[:_BODY_LIMIT]
                from_addr = str(getattr(detail, "from_addr", "") or getattr(detail, "from", "") or "")
        except Exception:
            pass
    return {
        "mailbox": mailbox,
        "message_id": message_id,
        "thread_id": thread_id or message_id,
        "subject": subject,
        "snippet": snippet,
        "body": body,
        "from_addr": from_addr,
    }


async def tool_draft_reply(
    user_text: str,
    ui_context: dict[str, Any],
    *,
    language: str,
    sampling_create_message: Any,
    memory_summary: str = "",
    mode: str = "draft_reply",
) -> dict[str, Any]:
    """基于当前线程起草回复 / 总结后回复。"""
    current = _current_thread(ui_context)
    mailbox = str(current.get("mailbox") or ui_context.get("mailbox") or "").strip()
    message_id = str(current.get("message_id") or "").strip()
    thread_id = str(current.get("thread_id") or message_id).strip()
    if not mailbox or not (message_id or thread_id):
        clarify = (
            "请先打开一封邮件，再让我起草回复。"
            if language == "zh"
            else "Open an email first, then ask me to draft a reply."
        )
        return {"kind": "clarify", "assistant_text": clarify, "clarify": clarify}

    excerpt = await _load_thread_excerpt(mailbox, message_id, thread_id)
    subject = excerpt["subject"] or str(current.get("subject") or "")
    body = excerpt["body"] or str(current.get("snippet") or "")
    summarize_first = mode == "summarize_then_draft"

    fallback_text = (
        f"已根据「{subject or '当前邮件'}」准备草稿，请核对后发送。"
        if language == "zh"
        else f"Draft ready for “{subject or 'this email'}”. Review before sending."
    )
    if sampling_create_message is None:
        draft_body = (
            f"Hi,\n\nThanks for your email about {subject or 'this'}.\n\nBest regards"
            if language != "zh"
            else f"您好，\n\n关于「{subject or '该邮件'}」，我已收到。\n\n此致"
        )
        return {
            "kind": "draft",
            "assistant_text": fallback_text,
            "artifact": {
                "type": "draft_reply",
                "mailbox": mailbox,
                "thread_id": thread_id,
                "body": draft_body,
                "source_prompt": user_text,
            },
            "mail_context": {
                "kind": "thread",
                "mailbox": mailbox,
                "message_id": message_id,
                "thread_id": thread_id,
                "subject": subject,
            },
            "fallback_used": True,
        }

    system = draft_reply_system_prompt(language, summarize_first=summarize_first)

    user_message = (
        f"User request: {user_text}\n"
        f"Mode: {mode}\n"
        f"Subject: {subject}\n"
        f"From: {excerpt.get('from_addr') or ''}\n"
        f"Body excerpt:\n{body or '(empty)'}\n"
    )
    if memory_summary:
        user_message += f"\n{memory_summary}\n"

    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=system,
        user_message=user_message,
        fallback={"assistant_text": fallback_text, "draft_body": ""},
        temperature=0.3,
        max_tokens=2400,
        timeout=90.0,
        metadata={"tool": f"ai_turn_{mode}"},
        allow_fallback=True,
        allow_sampling_provider_fallback=True,
        max_attempts=1,
    )
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    draft_body = str(payload.get("draft_body") or "").strip()[:_DRAFT_LIMIT]
    assistant_text = str(payload.get("assistant_text") or fallback_text).strip()
    if not draft_body:
        return {
            "kind": "error",
            "assistant_text": (
                "草稿生成失败，请稍后重试。"
                if language == "zh"
                else "Could not generate a draft. Please try again."
            ),
            "error": "draft_empty",
            "fallback_used": True,
        }
    return {
        "kind": "draft",
        "assistant_text": assistant_text,
        "artifact": {
            "type": "draft_reply",
            "mailbox": mailbox,
            "thread_id": thread_id,
            "body": draft_body,
            "source_prompt": user_text,
        },
        "mail_context": {
            "kind": "thread",
            "mailbox": mailbox,
            "message_id": message_id,
            "thread_id": thread_id,
            "subject": subject,
        },
        "fallback_used": bool(result.get("fallback_used")),
    }


async def tool_revise_draft(
    user_text: str,
    ui_context: dict[str, Any],
    *,
    language: str,
    sampling_create_message: Any,
    memory_summary: str = "",
) -> dict[str, Any]:
    """改写 last_draft 或 Compose 正文。"""
    draft = _last_draft_body(ui_context)
    current = _current_thread(ui_context)
    mailbox = str(current.get("mailbox") or ui_context.get("mailbox") or "").strip()
    thread_id = str(current.get("thread_id") or current.get("message_id") or "").strip()
    is_compose = str(current.get("kind") or "") == "compose" or (
        isinstance(ui_context.get("last_draft"), dict)
        and str(ui_context["last_draft"].get("source") or "") == "compose_box"
    )
    if not draft:
        clarify = (
            "请先提供要改写的草稿，或在 Compose 中写入正文后再试。"
            if language == "zh"
            else "Provide a draft to revise, or write body text in Compose first."
        )
        return {"kind": "clarify", "assistant_text": clarify, "clarify": clarify}

    fallback_text = "已按你的要求改写草稿。" if language == "zh" else "Draft revised as requested."
    if sampling_create_message is None:
        artifact_type = "compose_draft" if is_compose else "draft_reply"
        artifact: dict[str, Any] = {
            "type": artifact_type,
            "mailbox": mailbox,
            "body": draft,
            "source_prompt": user_text,
        }
        if artifact_type == "draft_reply":
            artifact["thread_id"] = thread_id
        else:
            artifact["mode"] = "replace"
        return {
            "kind": "draft",
            "assistant_text": fallback_text,
            "artifact": artifact,
            "fallback_used": True,
        }

    system = revise_draft_system_prompt(language)
    user_message = (
        f"Revision request: {user_text}\n"
        f"Current draft (reference only):\n<<<DRAFT>>>\n{draft}\n<<<END>>>\n"
    )
    if memory_summary:
        user_message += f"\n{memory_summary}\n"

    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=system,
        user_message=user_message,
        fallback={"assistant_text": fallback_text, "draft_body": draft},
        temperature=0.3,
        max_tokens=2400,
        timeout=90.0,
        metadata={"tool": "ai_turn_revise_draft"},
        allow_fallback=True,
        allow_sampling_provider_fallback=True,
        max_attempts=1,
    )
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    draft_body = str(payload.get("draft_body") or draft).strip()[:_DRAFT_LIMIT]
    assistant_text = str(payload.get("assistant_text") or fallback_text).strip()
    if is_compose:
        artifact = {
            "type": "compose_draft",
            "mailbox": mailbox,
            "body": draft_body,
            "source_prompt": user_text,
            "mode": "replace",
        }
    else:
        artifact = {
            "type": "draft_reply",
            "mailbox": mailbox,
            "thread_id": thread_id,
            "body": draft_body,
            "source_prompt": user_text,
        }
    return {
        "kind": "draft",
        "assistant_text": assistant_text,
        "artifact": artifact,
        "mail_context": {
            "kind": "thread" if thread_id else "compose",
            "mailbox": mailbox,
            "message_id": str(current.get("message_id") or ""),
            "thread_id": thread_id,
            "subject": str(current.get("subject") or ""),
        } if mailbox else None,
        "fallback_used": bool(result.get("fallback_used")),
    }


async def tool_compose_new(
    user_text: str,
    ui_context: dict[str, Any],
    arguments: dict[str, Any],
    *,
    language: str,
    sampling_create_message: Any,
    memory_summary: str = "",
    prior_evidence: str = "",
) -> dict[str, Any]:
    """基于用户意图 / 检索证据写新邮件大纲或正文。"""
    mailbox = str(ui_context.get("mailbox") or arguments.get("mailbox") or "").strip()
    fallback_text = (
        "已生成新邮件草稿大纲，可插入 Compose。"
        if language == "zh"
        else "New compose draft ready to insert."
    )
    if sampling_create_message is None:
        body = user_text[:800]
        return {
            "kind": "draft",
            "assistant_text": fallback_text,
            "artifact": {
                "type": "compose_draft",
                "mailbox": mailbox,
                "body": body,
                "source_prompt": user_text,
                "mode": "insert",
            },
            "fallback_used": True,
        }

    system = compose_new_system_prompt(language)
    user_message = f"User request: {user_text}\n"
    if prior_evidence:
        user_message += f"Search evidence (truncated):\n{prior_evidence[:1500]}\n"
    if memory_summary:
        user_message += f"\n{memory_summary}\n"

    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=system,
        user_message=user_message,
        fallback={"assistant_text": fallback_text, "subject": "", "draft_body": ""},
        temperature=0.3,
        max_tokens=2000,
        timeout=90.0,
        metadata={"tool": "ai_turn_compose_new"},
        allow_fallback=True,
        allow_sampling_provider_fallback=True,
        max_attempts=1,
    )
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    draft_body = str(payload.get("draft_body") or "").strip()[:_DRAFT_LIMIT]
    subject = str(payload.get("subject") or "").strip()[:200]
    assistant_text = str(payload.get("assistant_text") or fallback_text).strip()
    if not draft_body:
        return {
            "kind": "error",
            "assistant_text": (
                "未能生成新邮件草稿。" if language == "zh" else "Could not create a compose draft."
            ),
            "error": "compose_empty",
            "fallback_used": True,
        }
    return {
        "kind": "draft",
        "assistant_text": assistant_text,
        "artifact": {
            "type": "compose_draft",
            "mailbox": mailbox,
            "body": draft_body,
            "source_prompt": user_text,
            "mode": "insert",
            "subject": subject,
        },
        "fallback_used": bool(result.get("fallback_used")),
    }


async def tool_propose_inbox_actions(
    user_text: str,
    ui_context: dict[str, Any],
    arguments: dict[str, Any],
    *,
    language: str,
    sampling_create_message: Any,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """仅产出整理建议包，不执行 Gmail mutation。"""
    from mail_agent.ask.answer import run_ask_pipeline

    mailboxes = []
    selected = ui_context.get("selected_mailboxes")
    if isinstance(selected, list):
        mailboxes = [str(item).strip() for item in selected if str(item).strip()]
    if not mailboxes:
        primary = str(ui_context.get("mailbox") or arguments.get("mailbox") or "").strip()
        if primary:
            mailboxes = [primary]
    if not mailboxes:
        text = "请先选择邮箱账号。" if language == "zh" else "Select a mailbox first."
        return {"kind": "error", "assistant_text": text, "error": "no_mailbox"}

    def _progress(stage: str, progress: dict[str, Any] | None = None) -> None:
        if progress_callback:
            progress_callback(stage, progress or {})

    # 复用 Ask 检索候选，再组装确认卡片
    search_request = user_text
    if language == "zh" and "整理" in user_text:
        search_request = f"{user_text}；优先信息类/低优先级/可归档邮件"
    elif "organize" in user_text.casefold():
        search_request = f"{user_text}; prefer low-priority informational mail that can be marked done"

    result = await run_ask_pipeline(
        user_request=search_request,
        mailboxes=mailboxes,
        scan_window_days=ui_context.get("display_range_days") or arguments.get("scan_window_days"),
        max_messages=ui_context.get("max_messages") or arguments.get("max_messages"),
        sampling_create_message=sampling_create_message,
        progress_callback=_progress,
    )
    if result.get("analysis_error"):
        # 整理建议必须建立在完整分析之上；模型输出被截断时不能生成空确认卡，
        # 更不能暗示用户可以对 0 封邮件执行状态变更。
        return {
            "kind": "error",
            "assistant_text": str(result.get("summary") or "AI analysis is temporarily unavailable. Please try again."),
            "error": "analysis_unavailable",
            "scan_result": result,
            "fallback_used": False,
        }

    items: list[dict[str, Any]] = []
    sections = result.get("sections") if isinstance(result.get("sections"), list) else []
    for section in sections:
        if not isinstance(section, dict):
            continue
        for msg in (section.get("messages") or section.get("items") or [])[:12]:
            if not isinstance(msg, dict):
                continue
            mid = str(msg.get("message_id") or msg.get("id") or "").strip()
            tid = str(msg.get("thread_id") or mid).strip()
            if not mid and not tid:
                continue
            items.append({
                "mailbox": str(msg.get("mailbox") or mailboxes[0]),
                "message_id": mid,
                "thread_id": tid,
                "subject": str(msg.get("subject") or msg.get("title") or "")[:200],
                "default_selected": True,
            })
            if len(items) >= 12:
                break
        if len(items) >= 12:
            break

    # 无结构化 section 时尝试 candidates / messages
    if not items:
        for key in ("candidates", "messages", "mail_links"):
            raw = result.get(key)
            if not isinstance(raw, list):
                continue
            for msg in raw[:12]:
                if not isinstance(msg, dict):
                    continue
                mid = str(msg.get("message_id") or msg.get("id") or "").strip()
                tid = str(msg.get("thread_id") or mid).strip()
                if not mid and not tid:
                    continue
                items.append({
                    "mailbox": str(msg.get("mailbox") or mailboxes[0]),
                    "message_id": mid,
                    "thread_id": tid,
                    "subject": str(msg.get("subject") or msg.get("title") or "")[:200],
                    "default_selected": True,
                })

    n = len(items)
    # 主题列表不塞进 assistant_text（避免无链接/无分行）；由前端用 items 渲染可点击列表
    if language == "zh":
        step_title = "移除低优先级/信息类邮件"
        rationale = (
            str(result.get("summary") or "").strip()
            or "以下为可整理的候选线程，确认后才会变更状态。"
        )
        assistant_text = (
            f"我找到 {n} 封可整理的邮件。请先在下方确认本批操作（跳过无副作用）。"
            f"确认后我会给出完整建议，并询问是否继续整理剩余邮件。"
        )
        followup_after_apply = (
            "本批已处理。我还建议继续整理其余类似邮件（通知 / 试用 / 低优先级）。\n\n"
            "需要我继续把剩余相关邮件标为已处理并清理未读吗？"
        )
        followup_after_skip = (
            "已跳过本批。仍建议整理下列类型的邮件。\n\n"
            "需要我继续为剩余邮件生成整理建议吗？"
        )
        followup_after_dismiss = (
            "好的。我仍建议你关注这些可整理邮件（见下方列表）。"
            "之后可以说「继续整理」随时再来。"
        )
        continue_prompt = "继续整理剩余邮件"
        group_title = "可标为已处理的邮件"
    else:
        step_title = "Remove low-priority informational emails"
        rationale = (
            str(result.get("summary") or "").strip()
            or "Suggested threads to organize. Nothing changes until you confirm."
        )
        assistant_text = (
            f"I found {n} emails to organize. Confirm the batch below first (Skip has no side effects). "
            f"After that I'll show full recommendations and ask whether to continue with the rest."
        )
        followup_after_apply = (
            "Done with this batch. I still recommend organizing similar remaining emails "
            "(notifications / trials / low-priority).\n\n"
            "Would you like me to continue and mark more of these as done to clean up unread?"
        )
        followup_after_skip = (
            "Skipped this batch. I still recommend organizing emails like the list below.\n\n"
            "Would you like me to continue with suggestions for the remaining emails?"
        )
        followup_after_dismiss = (
            "Okay. I still recommend reviewing the emails listed below. "
            "You can say “continue organizing” anytime."
        )
        continue_prompt = "Continue organizing the remaining emails"
        group_title = "Low-priority emails to mark done"

    proposed = {
        "step_index": 1,
        "step_title": step_title,
        "rationale": rationale[:800],
        "primary_action": "mark_done",
        "allowed_actions": ["mark_done", "archive", "trash"],
        "items": items,
        "requires_user_confirmation": True,
        "language": language,
        # 确认/跳过/暂不继续后的叙事（对标 example/6.png）
        "followup_after_apply": followup_after_apply,
        "followup_after_skip": followup_after_skip,
        "followup_after_dismiss": followup_after_dismiss,
        "continue_prompt": continue_prompt,
        "recommendation_groups": [
            {
                "title": group_title,
                # 带 id，前端渲染为可点击链接并分行
                "items": [
                    {
                        "mailbox": item.get("mailbox"),
                        "message_id": item.get("message_id"),
                        "thread_id": item.get("thread_id"),
                        "subject": item.get("subject") or "",
                    }
                    for item in items
                ],
            }
        ],
    }
    return {
        "kind": "propose",
        "assistant_text": assistant_text,
        "proposed_actions": proposed,
        "scan_result": result,
        "fallback_used": bool((result.get("llm_meta") or {}).get("fallback_used")),
    }


def _selected_thread_refs(ui_context: dict[str, Any]) -> list[dict[str, Any]]:
    """从 ui_context.selected_threads 提取去重后的线程引用（上限 _BATCH_MAX_THREADS）。"""
    raw = ui_context.get("selected_threads")
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        mailbox = str(item.get("mailbox") or ui_context.get("mailbox") or "").strip()
        message_id = str(item.get("message_id") or "").strip()
        thread_id = str(item.get("thread_id") or message_id).strip()
        if not mailbox or not (message_id or thread_id):
            continue
        key = f"{mailbox}|{thread_id or message_id}"
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "mailbox": mailbox,
            "message_id": message_id or thread_id,
            "thread_id": thread_id or message_id,
            "subject": str(item.get("subject") or "")[:200],
        })
        if len(out) >= _BATCH_MAX_THREADS:
            break
    return out


async def _draft_one_thread(
    user_text: str,
    *,
    mailbox: str,
    message_id: str,
    thread_id: str,
    subject_hint: str,
    language: str,
    sampling_create_message: Any,
    memory_summary: str,
    mode: str,
) -> dict[str, Any]:
    """为单封邮件生成 draft 证据隔离（不跨线程复用正文）。"""
    excerpt = await _load_thread_excerpt(mailbox, message_id, thread_id)
    subject = excerpt["subject"] or subject_hint
    body = excerpt["body"] or excerpt.get("snippet") or ""
    fallback_text = (
        f"已为「{subject or '邮件'}」准备草稿。"
        if language == "zh"
        else f"Draft ready for “{subject or 'this email'}”."
    )
    if sampling_create_message is None:
        draft_body = (
            f"您好，\n\n关于「{subject or '该邮件'}」，我已收到。\n\n此致"
            if language == "zh"
            else f"Hi,\n\nThanks for your email about {subject or 'this'}.\n\nBest regards"
        )
        return {
            "ok": True,
            "assistant_line": fallback_text,
            "artifact": {
                "type": "draft_reply",
                "mailbox": mailbox,
                "thread_id": thread_id,
                "message_id": message_id,
                "subject": subject,
                "body": draft_body,
                "source_prompt": user_text,
            },
            "fallback_used": True,
        }

    system = batch_draft_system_prompt(language, mode=mode)
    user_message = (
        f"User request: {user_text}\n"
        f"Mode: {mode}\n"
        f"Subject: {subject}\n"
        f"From: {excerpt.get('from_addr') or ''}\n"
        f"Body excerpt:\n{body or '(empty)'}\n"
    )
    if memory_summary:
        user_message += f"\n{memory_summary}\n"

    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=system,
        user_message=user_message,
        fallback={"assistant_line": fallback_text, "draft_body": ""},
        temperature=0.3,
        max_tokens=1600,
        timeout=90.0,
        metadata={"tool": f"ai_turn_{mode}"},
        allow_fallback=True,
        allow_sampling_provider_fallback=True,
        max_attempts=1,
    )
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    draft_body = str(payload.get("draft_body") or "").strip()[:_DRAFT_LIMIT]
    assistant_line = str(payload.get("assistant_line") or fallback_text).strip()
    if not draft_body:
        return {
            "ok": False,
            "assistant_line": (
                f"未能为「{subject or message_id}」生成草稿。"
                if language == "zh"
                else f"Could not draft for “{subject or message_id}”."
            ),
            "error": "draft_empty",
            "mailbox": mailbox,
            "thread_id": thread_id,
            "message_id": message_id,
            "subject": subject,
            "fallback_used": True,
        }
    return {
        "ok": True,
        "assistant_line": assistant_line,
        "artifact": {
            "type": "draft_reply",
            "mailbox": mailbox,
            "thread_id": thread_id,
            "message_id": message_id,
            "subject": subject,
            "body": draft_body,
            "source_prompt": user_text,
        },
        "fallback_used": bool(result.get("fallback_used")),
    }


async def tool_batch_draft(
    user_text: str,
    ui_context: dict[str, Any],
    *,
    language: str,
    sampling_create_message: Any,
    memory_summary: str = "",
    mode: str = "batch_draft",
) -> dict[str, Any]:
    """对多选线程逐封起草回复；每封独立 evidence，禁止串上下文。

    mode:
    - batch_draft：多封简短回复草稿（AI-015）
    - batch_outreach：个性化触达/跟进（AI-016）
    """
    refs = _selected_thread_refs(ui_context)
    if not refs:
        clarify = (
            "请先在收件箱勾选需要批量起草的邮件（最多 5 封），再试一次。"
            if language == "zh"
            else "Select up to 5 emails in the inbox first, then ask for batch drafts."
        )
        return {"kind": "clarify", "assistant_text": clarify, "clarify": clarify}

    artifacts: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    any_fallback = False
    for ref in refs:
        item = await _draft_one_thread(
            user_text,
            mailbox=ref["mailbox"],
            message_id=ref["message_id"],
            thread_id=ref["thread_id"],
            subject_hint=ref.get("subject") or "",
            language=language,
            sampling_create_message=sampling_create_message,
            memory_summary=memory_summary,
            mode=mode,
        )
        if item.get("fallback_used"):
            any_fallback = True
        if item.get("ok") and isinstance(item.get("artifact"), dict):
            artifacts.append(item["artifact"])
        else:
            failures.append({
                "mailbox": ref["mailbox"],
                "thread_id": ref["thread_id"],
                "message_id": ref["message_id"],
                "subject": ref.get("subject") or "",
                "error": str(item.get("error") or "failed"),
            })

    if not artifacts:
        return {
            "kind": "error",
            "assistant_text": (
                "批量草稿全部生成失败，请稍后重试。"
                if language == "zh"
                else "All batch drafts failed. Please try again."
            ),
            "error": "batch_draft_empty",
            "fallback_used": True,
        }

    n = len(artifacts)
    fail_n = len(failures)
    if language == "zh":
        assistant_text = f"已为 {n} 封邮件生成草稿（证据按封隔离，请逐封核对后发送）。"
        if fail_n:
            assistant_text += f" 另有 {fail_n} 封未能生成。"
        if mode == "batch_outreach":
            assistant_text = f"已为 {n} 封生成个性化跟进草稿（变量按收件人隔离）。" + (
                f" 另有 {fail_n} 封失败。" if fail_n else ""
            )
    else:
        assistant_text = (
            f"Prepared {n} draft{'s' if n != 1 else ''} "
            f"(one evidence set per message — review before sending)."
        )
        if fail_n:
            assistant_text += f" {fail_n} could not be generated."
        if mode == "batch_outreach":
            assistant_text = (
                f"Prepared {n} personalized outreach draft{'s' if n != 1 else ''} "
                f"(variables isolated per recipient)."
            )
            if fail_n:
                assistant_text += f" {fail_n} failed."

    return {
        "kind": "draft",
        "assistant_text": assistant_text,
        # 兼容单 artifact 前端：首封放 artifact，全部放 artifacts
        "artifact": artifacts[0],
        "artifacts": artifacts,
        "batch_failures": failures,
        "fallback_used": any_fallback,
    }


async def tool_batch_outreach(
    user_text: str,
    ui_context: dict[str, Any],
    *,
    language: str,
    sampling_create_message: Any,
    memory_summary: str = "",
) -> dict[str, Any]:
    """批量个性化 outreach；内部复用 batch_draft 隔离逻辑。"""
    return await tool_batch_draft(
        user_text,
        ui_context,
        language=language,
        sampling_create_message=sampling_create_message,
        memory_summary=memory_summary,
        mode="batch_outreach",
    )


async def tool_remember_preference(
    user_text: str,
    *,
    language: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """将用户显式「记住…」写入 AI Memory。"""
    from mail_agent.ai_turn.personalization import add_ai_memory

    preference = ""
    if isinstance(params, dict):
        preference = str(params.get("preference") or params.get("text") or "").strip()
    if not preference:
        # 从话术剥离「记住」前缀
        stripped = re.sub(
            r"^(?:please\s+)?(?:remember(?:\s+to)?|记住(?:一下)?|请记住)\s*[:：,]?\s*",
            "",
            user_text or "",
            flags=re.IGNORECASE,
        ).strip()
        preference = stripped or user_text
    preference = preference[:500]
    try:
        saved = await add_ai_memory(preference, source="chat")
    except Exception as exc:
        _logger.warning("remember_preference storage failed: error_type=%s", type(exc).__name__)
        text = "未能保存偏好。" if language == "zh" else "Could not save that preference."
        return {"kind": "error", "assistant_text": text, "error": "memory_storage_unavailable"}
    if not saved.get("success"):
        text = "未能保存偏好。" if language == "zh" else "Could not save that preference."
        return {"kind": "error", "assistant_text": text, "error": saved.get("error") or "memory_failed"}
    text = (
        f"已记住：{preference}"
        if language == "zh"
        else f"Got it — I'll remember: {preference}"
    )
    return {
        "kind": "memory",
        "assistant_text": text,
        "memory": saved.get("memory"),
        "fallback_used": False,
    }


async def apply_proposed_actions(
    *,
    action: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """用户确认后执行整理动作。

    mark_done / archive：Gmail mark_read + 返回 local_done 供前端写 Inbox Done 标记。
    trash：调用 Gmail trash。
    禁止在 Router 内静默调用本函数。
    """
    import asyncio

    from mail_agent.mail_providers.gmail.adapter import batch_mark_read, trash_email

    action_id = str(action or "").strip().lower()
    if action_id not in {"mark_done", "archive", "trash"}:
        return {"success": False, "error": "invalid_action"}
    clean_items = [item for item in items if isinstance(item, dict)][:50]
    if not clean_items:
        return {"success": False, "error": "empty_items"}

    # 按 mailbox 分组
    by_mailbox: dict[str, list[dict[str, Any]]] = {}
    for item in clean_items:
        mailbox = str(item.get("mailbox") or "").strip()
        if not mailbox:
            continue
        by_mailbox.setdefault(mailbox, []).append(item)

    results: list[dict[str, Any]] = []
    local_done: list[dict[str, Any]] = []
    errors: list[str] = []

    for mailbox, group in by_mailbox.items():
        message_ids = [
            str(item.get("message_id") or "").strip()
            for item in group
            if str(item.get("message_id") or "").strip()
        ]
        try:
            if action_id == "trash":
                for mid in message_ids:
                    await asyncio.to_thread(trash_email, mailbox, mid)
                results.append({"mailbox": mailbox, "action": "trash", "count": len(message_ids)})
            else:
                # mark_done / archive：与 Inbox Done 对齐 → mark_read + 本地 done
                if message_ids:
                    await asyncio.to_thread(batch_mark_read, mailbox, message_ids)
                results.append({"mailbox": mailbox, "action": action_id, "count": len(message_ids)})
                for item in group:
                    local_done.append({
                        "mailbox": mailbox,
                        "message_id": str(item.get("message_id") or ""),
                        "thread_id": str(item.get("thread_id") or ""),
                    })
        except Exception as exc:
            _logger.warning(
                "apply_proposed_actions failed: action=%s mailbox=%s error_type=%s",
                action_id,
                mailbox[:64],
                type(exc).__name__,
            )
            errors.append(f"{mailbox}:{type(exc).__name__}")

    return {
        "success": not errors,
        "action": action_id,
        "results": results,
        "local_done": local_done,
        "errors": errors,
        "requires_local_done": action_id in {"mark_done", "archive"},
    }


__all__ = [
    "apply_proposed_actions",
    "tool_batch_draft",
    "tool_batch_outreach",
    "tool_compose_new",
    "tool_draft_reply",
    "tool_propose_inbox_actions",
    "tool_remember_preference",
    "tool_revise_draft",
]
