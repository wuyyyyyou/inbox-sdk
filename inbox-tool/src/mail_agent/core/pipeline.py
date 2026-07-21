"""Ask / custom-scan 编排（Brief 管线已下线）。

仅保留 `run_custom_scan`：搜邮件 → 按需读 → 一次 LLM → 结构化答案。
不走 phase1 / judgment / Attention Cards。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone, timedelta
from typing import Any

from .plan import create_run_id
from .scan import run_mail_scan
from ..domain.types import CustomScanPlan

_BEIJING_TZ = timezone(timedelta(hours=8))
_logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, dict[str, Any]], None]
_storage_available: bool | None = None


def _fmt_ts(epoch_ms: str) -> str:
    """将 Gmail internalDate（epoch 毫秒字符串）转为北京时间可读文本。"""
    if not epoch_ms:
        return ""
    try:
        dt = datetime.fromtimestamp(int(epoch_ms) / 1000.0, tz=_BEIJING_TZ)
        return dt.strftime("%b %d, %Y %H:%M")
    except (ValueError, TypeError, OSError):
        return epoch_ms


def _storage_ready() -> bool:
    """惰性检测存储层是否可用。"""
    global _storage_available
    if _storage_available is None:
        try:
            from ..storage.client import is_ready
            _storage_available = is_ready()
        except Exception:
            _storage_available = False
    return _storage_available


def _report_progress(
    progress_callback: ProgressCallback | None,
    stage: str,
    **progress: Any,
) -> None:
    if progress_callback:
        progress_callback(stage, progress)


def _execution_system_prompt() -> str:
    """Custom scan 执行 system（按任务拆分、集中在 prompts.py）。"""
    from mail_agent.ai_turn.prompts import custom_scan_system_prompt
    return custom_scan_system_prompt()


async def run_custom_scan(
    plan: CustomScanPlan,
    mailbox: str,
    *,
    sampling_create_message: Any,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Ask Agent：搜邮件 → 按需读 → 一次 LLM → 答案。

    与已下线的 Brief 管线完全解耦。不走 phase1/judgment/guards/cards。
    """
    from mail_agent.mail_providers.gmail.adapter import normalize_mailbox, get_message_detail_async
    from mail_agent.llm_runtime.service import call_llm_json_safe

    query_count = len(plan.gmail_queries or [])
    _report_progress(
        progress_callback,
        "scan",
        query_total=query_count,
        read_depth=plan.read_depth or "message_detail",
        partial={
            "plan": {
                "plan_id": plan.plan_id,
                "title": plan.title,
                "description": plan.description,
                "gmail_queries": plan.gmail_queries,
                "read_depth": plan.read_depth or "message_detail",
            }
        },
    )
    normalized_mailbox = normalize_mailbox(mailbox)

    max_threads_ask = (plan.scan_budget or {}).get("max_messages", 200) if isinstance(plan.scan_budget, dict) else 200
    messages = await run_mail_scan(mailbox, max_threads_ask, progress_callback=progress_callback)
    sources = [
        {
            "subject": getattr(msg, "subject", "") or "",
            "from": getattr(msg, "from_addr", "") or "",
            "date": _fmt_ts(getattr(msg, "internal_date", "") or ""),
            "thread_id": getattr(msg, "thread_id", "") or "",
        }
        for msg in messages[:8]
    ]
    _report_progress(
        progress_callback,
        "scan_done",
        scanned=len(messages),
        query_total=query_count,
        partial={"sources": sources},
    )

    if not messages:
        return {
            "title": plan.title or "Scan result",
            "summary": "No matching emails found.",
            "sections": [],
        }

    read_depth = plan.read_depth or "message_detail"
    _report_progress(progress_callback, "read_context", current=0, total=len(messages), read_depth=read_depth)

    email_data: list[dict[str, Any]] = []
    for index, msg in enumerate(messages, 1):
        _report_progress(progress_callback, "read_context", current=index, total=len(messages), read_depth=read_depth)
        entry: dict[str, Any] = {
            "message_id": msg.message_id or "",
            "thread_id": msg.thread_id or "",
            "from": msg.from_addr or "",
            "to": msg.to_addr or "",
            "subject": msg.subject or "",
            "date": _fmt_ts(msg.internal_date or ""),
            "snippet": msg.snippet or "",
            "unread": getattr(msg, "unread", False),
            "label_ids": getattr(msg, "label_ids", None) or [],
        }

        if read_depth == "message_detail" or read_depth == "thread_context":
            try:
                detail = await get_message_detail_async(normalized_mailbox, msg.message_id)
                if detail:
                    entry["body"] = (getattr(detail, "body_text", "") or "")[:4000]
            except Exception:
                entry["body"] = ""

        if read_depth == "thread_context":
            try:
                from mail_agent.mail_providers.gmail.adapter import get_thread_context_async
                thread_ctx = await get_thread_context_async(normalized_mailbox, msg.thread_id or msg.message_id)
                if thread_ctx and thread_ctx.messages:
                    entry["thread"] = []
                    for tm in thread_ctx.messages:
                        entry["thread"].append({
                            "from": getattr(tm, "from_addr", "") or "",
                            "to": getattr(tm, "to_addr", "") or "",
                            "subject": getattr(tm, "subject", "") or "",
                            "date": _fmt_ts(getattr(tm, "internal_date", "") or ""),
                            "body": (getattr(tm, "body_text", "") or "")[:3000],
                        })
            except Exception:
                entry["thread"] = []

        email_data.append(entry)

    _report_progress(progress_callback, "read_context_done", current=len(email_data), total=len(email_data), read_depth=read_depth)

    _report_progress(
        progress_callback,
        "evaluate",
        emails=len(email_data),
        current=len(email_data),
        total=len(email_data),
        read_depth=read_depth,
        partial={"sources": sources},
    )

    def _build_user_prompt(rendered_emails: str) -> str:
        return f"""## Your Identity
You are Anna, executive assistant to {mailbox}.
In all output text, address your principal directly as "you" / "your".
Say "You received an email from Sarah" — NOT "the user received" or "Kate received".
Match by EMAIL ADDRESS (between < >), not by display name.
- If the sender's email IS your principal → this is OUTGOING mail (sent or draft).
  SENT: your principal already sent it. Include or exclude based on the user's request.
  DRAFT: your principal started writing but didn't send yet.

## User request
{plan.user_request}

## Task
{plan.task_prompt}

## Search plan used
{json.dumps(plan.gmail_queries, ensure_ascii=False)}

## Important
- Base your answer ONLY on the emails provided below. Do not invent or assume information not present.
- If the emails below do not contain what the user is looking for, say so honestly in your summary.
- If a Gmail query contains filters such as is:unread, treat the returned emails as already filtered by that condition.
- The user's request may mention people, topics, or dates — only use what you actually find in the emails.

## Emails ({len(email_data)} total)
{rendered_emails}"""

    strict_anna_sampling = sampling_create_message is not None
    variants = (
        [
            {"name": "compact", "body_limit": 1600, "thread_body_limit": 900, "max_thread_messages": 8},
            {"name": "short", "body_limit": 800, "thread_body_limit": 500, "max_thread_messages": 5},
            {"name": "headers", "body_limit": 0, "thread_body_limit": 0, "max_thread_messages": 0},
        ]
        if strict_anna_sampling
        else [{"name": "full", "body_limit": 4000, "thread_body_limit": 2000, "max_thread_messages": 20}]
    )
    result: dict[str, Any] | None = None
    last_error = ""
    for variant in variants:
        rendered_emails = _render_emails_for_llm(
            email_data,
            read_depth,
            body_limit=int(variant["body_limit"]),
            thread_body_limit=int(variant["thread_body_limit"]),
            max_thread_messages=int(variant["max_thread_messages"]),
        )
        try:
            result = await call_llm_json_safe(
                sampling_create_message,
                system_prompt=_execution_system_prompt(),
                user_message=_build_user_prompt(rendered_emails),
                fallback={"title": "Scan failed", "summary": "Unable to analyze emails.", "sections": []},
                temperature=0.2,
                max_tokens=8000,
                timeout=180.0,
                metadata={
                    "tool": "run_custom_scan_agent",
                    "email_count": str(len(email_data)),
                    "read_depth": read_depth,
                    "prompt_variant": str(variant["name"]),
                },
                allow_fallback=False if strict_anna_sampling else True,
                allow_sampling_provider_fallback=not strict_anna_sampling,
                max_attempts=1 if strict_anna_sampling else None,
            )
            break
        except Exception as exc:
            last_error = str(exc)
            _report_progress(
                progress_callback,
                "evaluate",
                emails=len(email_data),
                current=len(email_data),
                total=len(email_data),
                read_depth=read_depth,
                prompt_variant=str(variant["name"]),
                reason=last_error[:200],
            )
    if result is None:
        result = {
            "payload": {
                "title": plan.title or "Scan incomplete",
                "summary": f"Anna could not produce a usable answer. Last error: {last_error[:240]}",
                "sections": [],
            },
            "provider": "anna-sampling",
            "model": None,
            "usage": None,
            "fallback_used": True,
            "fallback_reason": last_error,
        }

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    if not payload:
        payload = {"title": plan.title or "Scan complete", "summary": "No analysis produced.", "sections": []}
    payload["llm_meta"] = {
        "provider": result.get("provider"),
        "model": result.get("model"),
        "usage": result.get("usage"),
        "fallback_used": result.get("fallback_used", False),
        "fallback_reason": result.get("fallback_reason", ""),
    }

    # 仅写跨会话 run history 索引，不再生成 Attention Cards / processed 标记。
    if _storage_ready():
        try:
            from ..storage.ops import append_run_history
            from ..storage.types import RunHistoryEntry, _now
            section_count = len(payload.get("sections") or []) if isinstance(payload, dict) else 0
            await append_run_history(RunHistoryEntry(
                run_id=create_run_id(),
                mailbox=mailbox,
                ts=_now(),
                request=(plan.user_request or "")[:100],
                mode="custom",
                strategy="custom",
                plan_id=plan.plan_id or "",
                result=f"custom scan: {section_count} sections",
                summary=str(payload.get("summary") or "")[:300],
            ))
        except Exception as exc:
            _logger.debug("custom scan history write skipped: %s", type(exc).__name__)

    return payload


def _render_emails_for_llm(
    email_data: list[dict[str, Any]],
    read_depth: str,
    *,
    body_limit: int = 4000,
    thread_body_limit: int = 2000,
    max_thread_messages: int = 20,
) -> str:
    """将邮件数据渲染为紧凑文本供 LLM 使用。"""
    parts: list[str] = []
    for i, e in enumerate(email_data, 1):
        unread_label = " (UNREAD)" if e.get("unread") else ""
        labels = [str(l) for l in (e.get("label_ids") or []) if str(l) not in ("UNREAD",)]
        labels_str = f"  Labels: {', '.join(labels)}" if labels else ""
        parts.append(
            f"### Email {i}\n"
            f"From: {e.get('from', '')}\n"
            f"Subject: {e.get('subject', '')}{unread_label}\n"
            f"Date: {e.get('date', '')}\n"
            f"Thread ID: {e.get('thread_id', '')}\n"
            f"Message ID: {e.get('message_id', '')}{labels_str}"
        )
        if e.get("body") and body_limit > 0:
            parts.append(f"Snippet: {e.get('snippet', '')}")
            parts.append(f"Body:\n{e['body'][:body_limit]}")
        elif read_depth == "header_only" or body_limit <= 0:
            parts.append(f"Snippet: {e.get('snippet', '')}")
        if e.get("thread") and thread_body_limit > 0 and max_thread_messages > 0:
            thread_msgs = e["thread"][:max_thread_messages]
            parts.append(f"\nThread history ({len(thread_msgs)} messages):")
            for tm in thread_msgs:
                parts.append(
                    f"  [{tm.get('date', '')}] {tm.get('from', '')}: "
                    f"{tm.get('subject', '')}\n"
                    f"    {tm.get('body', '')[:thread_body_limit]}"
                )
        parts.append("")
    return "\n".join(parts)


__all__ = [
    "run_custom_scan",
]
