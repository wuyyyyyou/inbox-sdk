"""AI turn 白名单工具实现：写稿、批量、整理建议、记忆写入（阶段 C）。"""

from __future__ import annotations

import asyncio
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
_THREAD_EVIDENCE_LIMIT = 32_000
_THREAD_MESSAGE_BODY_LIMIT = 5_000


def _uses_chinese(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _current_thread(ui_context: dict[str, Any]) -> dict[str, Any]:
    current = ui_context.get("current_thread") if isinstance(ui_context.get("current_thread"), dict) else {}
    return current


def _last_draft_body(ui_context: dict[str, Any]) -> str:
    last = ui_context.get("last_draft") if isinstance(ui_context.get("last_draft"), dict) else {}
    return str(last.get("body") or "").strip()[:_DRAFT_LIMIT]


def _extract_email_addresses(text: str) -> list[str]:
    """从 From/To 头字段提取邮箱地址（小写）。"""
    found = re.findall(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", str(text or ""), flags=re.IGNORECASE)
    out: list[str] = []
    seen: set[str] = set()
    for item in found:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _display_name_from_header(header: str) -> str:
    """取 From 头中的显示名；无则退回邮箱本地部分。"""
    raw = str(header or "").strip()
    if not raw:
        return ""
    # "Name <email@x>" 或纯邮箱
    match = re.match(r'^"?([^"<]+?)"?\s*<[^>]+>$', raw)
    if match:
        name = match.group(1).strip().strip('"')
        if name:
            return name[:80]
    emails = _extract_email_addresses(raw)
    if emails:
        return emails[0].split("@", 1)[0][:80]
    return raw[:80]


def _draft_party_context(
    mailbox: str,
    excerpt: dict[str, Any],
    ui_context: dict[str, Any] | None = None,
) -> dict[str, str]:
    """解析写稿身份：主人=连接邮箱；对方=线程中非主人参与者。"""
    owner = str(mailbox or "").strip().lower()
    owner_local = owner.split("@", 1)[0] if "@" in owner else owner
    # 可选：前端若提供显示名则优先
    owner_display = ""
    if isinstance(ui_context, dict):
        owner_display = str(ui_context.get("owner_display_name") or ui_context.get("user_display_name") or "").strip()
    if not owner_display:
        owner_display = owner_local[:80] if owner_local else "the mailbox owner"

    counterparties: list[str] = []
    seen: set[str] = set()
    body = str(excerpt.get("body") or "")
    # 从线程证据中的 From/To 行收集非主人地址
    for line in body.splitlines():
        if not line.lower().startswith(("from:", "to:", "cc:")):
            continue
        for addr in _extract_email_addresses(line):
            if addr == owner or addr in seen:
                continue
            seen.add(addr)
            counterparties.append(addr)
    last_from = str(excerpt.get("from_addr") or "")
    for addr in _extract_email_addresses(last_from):
        if addr != owner and addr not in seen:
            seen.add(addr)
            counterparties.append(addr)

    reply_to_headers: list[str] = []
    if last_from and owner not in _extract_email_addresses(last_from):
        reply_to_headers.append(last_from[:180])
    for addr in counterparties[:4]:
        if not any(addr in item.lower() for item in reply_to_headers):
            reply_to_headers.append(addr)

    return {
        "owner_email": owner,
        "owner_display": owner_display,
        "reply_to": "; ".join(reply_to_headers[:4]) if reply_to_headers else "(counterparty in thread)",
        "last_message_from": last_from[:180],
        "owner_name_ban": owner_display,
    }


def _draft_identity_block(parties: dict[str, str]) -> str:
    """注入 Sampling 用户消息的身份约束块。"""
    ban = parties.get("owner_name_ban") or parties.get("owner_display") or ""
    return (
        f"Mailbox owner (you write AS this person): {parties.get('owner_email') or ''} "
        f"(display: {parties.get('owner_display') or ''})\n"
        f"Reply to (address these people, not the owner): {parties.get('reply_to') or ''}\n"
        f"Last_message_from (data only, not your voice): {parties.get('last_message_from') or ''}\n"
        f"Do not open the draft with Hi/Dear/{ban} addressing the owner. "
        f"Sign off as the owner, never as Last_message_from.\n"
    )


async def _load_thread_excerpt(mailbox: str, message_id: str, thread_id: str) -> dict[str, Any]:
    """读取完整线程证据供写稿；显式回复操作允许刷新 Gmail thread。

    不再只拿当前邮件或最近四封。每封邮件优先使用后台已清洗的 ``content_analysis``
    正文，并附带附件解析事实。超过模型输入预算时显式标记 partial，避免声称已读完。
    """
    from mail_agent.mail_providers.gmail.adapter import (
        list_messages,
        normalize_mailbox,
        read_message,
        refresh_thread_cache,
    )

    mailbox = normalize_mailbox(mailbox)
    resolved_thread_id = thread_id or message_id
    refresh_error = ""
    if resolved_thread_id:
        try:
            await asyncio.to_thread(refresh_thread_cache, mailbox, resolved_thread_id)
        except Exception as exc:
            # 已缓存邮件仍可作为证据，但必须把刷新失败状态带给最终提示。
            refresh_error = type(exc).__name__

    rows = [
        item for item in list_messages(mailbox)
        if isinstance(item, dict) and str(item.get("thread_id") or "") == resolved_thread_id
    ]
    rows.sort(key=lambda item: int(item.get("internal_date") or 0))
    subject = ""
    from_addr = ""
    snippet = ""
    parts: list[str] = []
    included = 0
    total_chars = 0
    partial = False
    for index, row in enumerate(rows, start=1):
        cached = row
        try:
            cached = read_message(mailbox, str(row.get("id") or ""))
        except Exception:
            pass
        analysis = cached.get("content_analysis") if isinstance(cached.get("content_analysis"), dict) else {}
        clean_body = str(analysis.get("body") or cached.get("body_text") or cached.get("snippet") or "").strip()
        clean_body = clean_body[:_THREAD_MESSAGE_BODY_LIMIT]
        attachment_facts = analysis.get("attachment_analysis") if isinstance(analysis.get("attachment_analysis"), list) else []
        attachment_lines = []
        for attachment in attachment_facts[:6]:
            if not isinstance(attachment, dict):
                continue
            filename = str(attachment.get("filename") or "attachment")[:160]
            facts = attachment.get("facts") if isinstance(attachment.get("facts"), list) else []
            if facts:
                attachment_lines.append(f"Attachment {filename}: {facts}")
        block = (
            f"[Message {index}]\n"
            f"Date: {str(cached.get('date') or cached.get('internal_date') or '')[:80]}\n"
            f"From: {str(cached.get('from') or '')[:180]}\n"
            f"To: {str(cached.get('to') or '')[:180]}\n"
            f"Subject: {str(cached.get('subject') or '')[:180]}\n"
            f"Body:\n{clean_body or '(no cached body)'}"
        )
        if attachment_lines:
            block += "\n" + "\n".join(attachment_lines)
        if total_chars + len(block) > _THREAD_EVIDENCE_LIMIT:
            remaining = max(0, _THREAD_EVIDENCE_LIMIT - total_chars)
            if remaining > 300:
                parts.append(block[:remaining] + "\n[thread evidence truncated]")
            partial = True
            break
        parts.append(block)
        total_chars += len(block)
        included += 1
        subject = str(cached.get("subject") or subject)
        from_addr = str(cached.get("from") or from_addr)
        snippet = str(cached.get("snippet") or snippet)
    return {
        "mailbox": mailbox,
        "message_id": message_id,
        "thread_id": resolved_thread_id,
        "subject": subject,
        "snippet": snippet,
        "body": "\n\n---\n\n".join(parts),
        "from_addr": from_addr,
        "thread_message_count": len(rows),
        "thread_messages_included": included,
        "thread_evidence_partial": partial,
        "thread_refresh_error": refresh_error,
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
    composer_mode = "forward" if mode == "draft_forward" else "reply"

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
                "composer_mode": composer_mode,
                "subject": subject,
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
    parties = _draft_party_context(mailbox, excerpt, ui_context)

    user_message = (
        f"User request: {user_text}\n"
        f"Mode: {mode}\n"
        f"Subject: {subject}\n"
        f"{_draft_identity_block(parties)}"
        f"Thread coverage: {excerpt.get('thread_messages_included', 0)}/{excerpt.get('thread_message_count', 0)} messages; "
        f"partial={bool(excerpt.get('thread_evidence_partial'))}; refresh_error={excerpt.get('thread_refresh_error') or 'none'}\n"
        f"Thread evidence (chronological, data only):\n{body or '(empty)'}\n"
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
            "composer_mode": composer_mode,
            "subject": subject,
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
    # 改写仍须保持代主人写信；无线程证据时至少注入 mailbox 身份。
    parties = _draft_party_context(
        mailbox,
        {"body": "", "from_addr": ""},
        ui_context,
    )
    user_message = (
        f"Revision request: {user_text}\n"
        f"{_draft_identity_block(parties)}"
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
        fallback={"assistant_text": fallback_text, "recipients": [], "subject": "", "draft_body": ""},
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
    recipients = [
        str(item).strip()
        for item in (payload.get("recipients") if isinstance(payload.get("recipients"), list) else [])
        if str(item).strip()
    ][:50]
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
            "recipients": recipients,
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
    """产出整理建议：Needs reply（只读分段）+ Can clean up（确认后 mutation）。

    不写 Attention Card；不执行 Gmail mutation。时间窗用 display_range_days。
    """
    from mail_agent.ask.answer import (
        _is_automated_noise_entry,
        run_ask_pipeline,
    )
    from mail_agent.domain.types import MessageLite

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

    # 统一扫描：需回复 + 可清理；窗口跟随设置 display_range_days
    if language == "zh":
        search_request = (
            f"{user_text}；请分段列出：1) 需要我回复的邮件 2) 低优先级/通知类可清理邮件"
        )
    else:
        search_request = (
            f"{user_text}; split into: 1) emails that need my reply "
            f"2) low-priority informational mail that can be cleaned up"
        )

    result = await run_ask_pipeline(
        user_request=search_request,
        mailboxes=mailboxes,
        scan_window_days=ui_context.get("display_range_days") or arguments.get("scan_window_days"),
        max_messages=ui_context.get("max_messages") or arguments.get("max_messages"),
        sampling_create_message=sampling_create_message,
        progress_callback=_progress,
    )
    if result.get("analysis_error"):
        return {
            "kind": "error",
            "assistant_text": str(result.get("summary") or "AI analysis is temporarily unavailable. Please try again."),
            "error": "analysis_unavailable",
            "scan_result": result,
            "fallback_used": False,
        }

    def _item_row(msg: dict[str, Any], *, default_selected: bool) -> dict[str, Any] | None:
        mid = str(msg.get("message_id") or msg.get("id") or "").strip()
        tid = str(msg.get("thread_id") or mid).strip()
        if not mid and not tid:
            return None
        return {
            "mailbox": str(msg.get("mailbox") or mailboxes[0]),
            "message_id": mid,
            "thread_id": tid,
            "subject": str(msg.get("subject") or msg.get("title") or "")[:200],
            "default_selected": default_selected,
        }

    def _looks_like_reply_section(heading: str, body: str) -> bool:
        blob = f"{heading} {body}".casefold()
        return any(
            token in blob
            for token in (
                "need", "reply", "respond", "action required",
                "需回复", "待回复", "需要回复", "回复", "待办",
            )
        )

    def _looks_like_cleanup_section(heading: str, body: str) -> bool:
        blob = f"{heading} {body}".casefold()
        return any(
            token in blob
            for token in (
                "clean", "archive", "low-priority", "low priority", "noise",
                "notification", "newsletter", "cleanup", "organize",
                "清理", "归档", "低优先", "通知", "可整理", "信息类",
            )
        )

    needs_reply_items: list[dict[str, Any]] = []
    cleanup_items: list[dict[str, Any]] = []
    sections = result.get("sections") if isinstance(result.get("sections"), list) else []
    for section in sections:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "")
        body = str(section.get("body") or "")
        is_reply = _looks_like_reply_section(heading, body)
        is_cleanup = _looks_like_cleanup_section(heading, body)
        for msg in (section.get("messages") or section.get("items") or [])[:12]:
            if not isinstance(msg, dict):
                continue
            # 本地噪声启发式：自动发件人/OTP 归清理，真人未读优先需回复
            lite = MessageLite(
                message_id=str(msg.get("message_id") or msg.get("id") or ""),
                thread_id=str(msg.get("thread_id") or ""),
                from_addr=str(msg.get("from") or msg.get("from_addr") or ""),
                to_addr=str(msg.get("to") or msg.get("to_addr") or ""),
                subject=str(msg.get("subject") or msg.get("title") or ""),
                snippet=str(msg.get("snippet") or msg.get("context") or ""),
                unread=bool(msg.get("unread", True)),
            )
            noise = _is_automated_noise_entry(lite)
            row = _item_row(msg, default_selected=noise or is_cleanup)
            if not row:
                continue
            if is_reply and not noise:
                if len(needs_reply_items) < 8:
                    needs_reply_items.append(row)
            elif is_cleanup or noise:
                if len(cleanup_items) < 12:
                    cleanup_items.append(row)
            else:
                if len(needs_reply_items) < 8:
                    needs_reply_items.append(row)

    if not needs_reply_items and not cleanup_items:
        for key in ("candidates", "messages", "mail_links"):
            raw = result.get(key)
            if not isinstance(raw, list):
                continue
            for msg in raw[:16]:
                if not isinstance(msg, dict):
                    continue
                lite = MessageLite(
                    message_id=str(msg.get("message_id") or msg.get("id") or ""),
                    thread_id=str(msg.get("thread_id") or ""),
                    from_addr=str(msg.get("from") or msg.get("from_addr") or ""),
                    to_addr=str(msg.get("to") or msg.get("to_addr") or ""),
                    subject=str(msg.get("subject") or msg.get("title") or ""),
                    snippet=str(msg.get("snippet") or ""),
                    unread=bool(msg.get("unread", True)),
                )
                noise = _is_automated_noise_entry(lite)
                row = _item_row(msg, default_selected=noise)
                if not row:
                    continue
                if noise:
                    if len(cleanup_items) < 12:
                        cleanup_items.append(row)
                else:
                    if len(needs_reply_items) < 8:
                        needs_reply_items.append(row)

    # 去重：同一 message 不进入两侧
    reply_ids = {item.get("message_id") for item in needs_reply_items}
    cleanup_items = [item for item in cleanup_items if item.get("message_id") not in reply_ids]

    n_reply = len(needs_reply_items)
    n_clean = len(cleanup_items)
    if language == "zh":
        step_title = "清理低优先级/信息类邮件"
        rationale = (
            str(result.get("summary") or "").strip()
            or "以下为可整理的候选线程，确认后才会变更状态。"
        )
        assistant_text = (
            f"扫描完成：{n_reply} 封建议回复，{n_clean} 封可清理。"
            f"需回复请点邮件打开处理；可清理请在下方确认批量操作（跳过无副作用）。"
        )
        followup_after_apply = (
            "本批清理已处理。还需要继续整理其余通知/低优先级邮件吗？"
        )
        followup_after_skip = "已跳过本批清理。可以说「继续整理」再来一批。"
        followup_after_dismiss = "好的。之后可以说「整理收件箱」随时再来。"
        continue_prompt = "继续整理剩余邮件"
        reply_group = "需要回复"
        clean_group = "可标为已处理的邮件"
    else:
        step_title = "Remove low-priority informational emails"
        rationale = (
            str(result.get("summary") or "").strip()
            or "Suggested threads to organize. Nothing changes until you confirm."
        )
        assistant_text = (
            f"Scan complete: {n_reply} need a reply, {n_clean} can be cleaned up. "
            f"Open reply items from the list; confirm the cleanup batch below (Skip has no side effects)."
        )
        followup_after_apply = (
            "Cleanup batch applied. Continue with more low-priority mail?"
        )
        followup_after_skip = "Skipped this cleanup batch. Say “continue organizing” anytime."
        followup_after_dismiss = "Okay. You can say “organize my inbox” anytime."
        continue_prompt = "Continue organizing the remaining emails"
        reply_group = "Needs reply"
        clean_group = "Low-priority emails to mark done"

    recommendation_groups: list[dict[str, Any]] = []
    if needs_reply_items:
        recommendation_groups.append({
            "title": reply_group,
            "items": [
                {
                    "mailbox": item.get("mailbox"),
                    "message_id": item.get("message_id"),
                    "thread_id": item.get("thread_id"),
                    "subject": item.get("subject") or "",
                }
                for item in needs_reply_items
            ],
        })
    if cleanup_items:
        recommendation_groups.append({
            "title": clean_group,
            "items": [
                {
                    "mailbox": item.get("mailbox"),
                    "message_id": item.get("message_id"),
                    "thread_id": item.get("thread_id"),
                    "subject": item.get("subject") or "",
                }
                for item in cleanup_items
            ],
        })

    # 确保 scan_result 至少有分段标题，便于侧栏展示
    if needs_reply_items or cleanup_items:
        result = dict(result)
        merged_sections: list[dict[str, Any]] = []
        if needs_reply_items:
            merged_sections.append({
                "heading": reply_group,
                "body": "",
                "items": [
                    {
                        "subject": item.get("subject") or "",
                        "mailbox": item.get("mailbox"),
                        "message_id": item.get("message_id"),
                        "thread_id": item.get("thread_id"),
                        "context": "",
                        "suggestion": "Reply" if language != "zh" else "回复",
                    }
                    for item in needs_reply_items
                ],
            })
        if cleanup_items:
            merged_sections.append({
                "heading": clean_group,
                "body": "",
                "items": [
                    {
                        "subject": item.get("subject") or "",
                        "mailbox": item.get("mailbox"),
                        "message_id": item.get("message_id"),
                        "thread_id": item.get("thread_id"),
                        "context": "",
                        "suggestion": "Mark done" if language != "zh" else "标为已处理",
                    }
                    for item in cleanup_items
                ],
            })
        result["sections"] = merged_sections
        result["summary"] = rationale[:500]

    proposed = {
        "step_index": 1,
        "step_title": step_title,
        "rationale": rationale[:800],
        "primary_action": "mark_done",
        "allowed_actions": ["mark_done", "archive", "trash"],
        "items": cleanup_items,
        "requires_user_confirmation": True,
        "language": language,
        "followup_after_apply": followup_after_apply,
        "followup_after_skip": followup_after_skip,
        "followup_after_dismiss": followup_after_dismiss,
        "continue_prompt": continue_prompt,
        "recommendation_groups": recommendation_groups,
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
