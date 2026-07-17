"""Ask pipeline answer stage: filter → context → answer LLM → guard.

run_ask_pipeline() is the orchestrator: plan → search → filter → context → answer → guard.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any

from ..domain.types import MessageLite
from .planner import AskPlan, is_actionable_browse_request, is_needs_reply_request
from .sampling_budget import ASK_ANSWER_MAX_TOKENS

# 自动通知 / 验证码噪声：needs-reply 与 browse 排序时降权或剔除。
_NOISE_SENDER_MARKERS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "notification@", "notifications@", "mailer-daemon", "bounce@",
    "newsletter", "marketing@", "em@", "em1.", "priority.instagram",
    "discoursemail.com", "accounts.google.com", "noreply-accounts",
)
_NOISE_SUBJECT_MARKERS = (
    "验证码", "verification code", "otp", "one time code", "one-time code",
    "is your code", "your code is", "unsubscribe", "newsletter",
    "错过的精彩", "confirm your new account", "account no longer on hold",
    "shared some google", "共享了一些", "邮箱验证码",
)

_logger = logging.getLogger(__name__)
_BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# 反向 JSON-RPC 不能使用文件传输；筛选请求需远低于 512 KiB 协议帧上限。
# 全部命中用于扫描统计，只有排序靠前的有限候选才能读取正文和线程。
_MAX_CONTEXT_CANDIDATES = 8
# 正文/线程 Gmail 读限并发，避免 8 封串行放大墙钟时间。
_CONTEXT_READ_CONCURRENCY = 4
# 即使有必要阅读片段，回答也必须综合事实，避免退化为邮件正文复制。
_ASK_SYNTHESIS_INSTRUCTION = (
    "Summarize and synthesize the evidence in your own words. "
    "Do not copy email body verbatim, except for a short necessary quote or an exact subject."
)
_ASK_ANSWER_SYSTEM_PROMPT = """You are Anna, an executive email assistant. Return ONE valid JSON object only. No markdown fences, no commentary, no TypeScript/schema type names.

Example shape (replace every value with real content from the emails; never copy the words string/array/object or trailing ?):
{
  "title": "Emails awaiting your reply",
  "summary": "Two threads look like they need a response.",
  "sections": [
    {
      "heading": "Needs reply",
      "body": "Human senders asked a question or requested action.",
      "items": [
        {
          "subject": "exact subject from evidence",
          "from": "exact from from evidence",
          "context": "why this needs attention",
          "suggestion": "what you could do next",
          "mailbox": "provided mailbox",
          "message_id": "provided message id",
          "thread_id": "provided thread id",
          "mail_links": [
            {
              "label": "exact subject",
              "mailbox": "provided mailbox",
              "thread_id": "provided thread id",
              "message_id": "provided message id"
            }
          ]
        }
      ]
    }
  ]
}

Rules:
- Use only the supplied email evidence. Prefer real subjects/IDs from the evidence list.
- Keep generated copy concise and in the user's language.
- Never put both a non-empty draft and reply_gaps.needs_user_input=true on the same item.
- If nothing matches the request, still return valid JSON with an honest summary and empty sections/items."""


def _answer_language_instruction(user_request: str) -> str:
    """Keep generated answer copy aligned with the user's language."""
    if re.search(r"[\u3400-\u9fff]", user_request):
        return (
            "Write all generated natural-language fields in Simplified Chinese. "
            "Keep email subjects, names, addresses, and quoted source text in their original language."
        )
    return (
        "Write all generated natural-language fields in English. Do not output Chinese or another language "
        "for generated copy. "
        "Keep email subjects, names, addresses, and quoted source text in their original language."
    )


def _uses_chinese(user_request: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", user_request))


def _entry_noise_blob(entry: dict[str, Any] | MessageLite) -> str:
    """拼接发件人/主题/摘要用于噪声检测。"""
    if isinstance(entry, dict):
        return " ".join(
            str(entry.get(key) or "")
            for key in ("from", "subject", "snippet", "context")
        ).casefold()
    return " ".join(
        str(getattr(entry, key, "") or "")
        for key in ("from_addr", "subject", "snippet")
    ).casefold()


def _is_automated_noise_entry(entry: dict[str, Any] | MessageLite) -> bool:
    """判断是否为验证码/通知类噪声，通常不需要用户回复。"""
    blob = _entry_noise_blob(entry)
    if any(marker in blob for marker in _NOISE_SENDER_MARKERS):
        return True
    subject = ""
    if isinstance(entry, dict):
        subject = str(entry.get("subject") or "").casefold()
    else:
        subject = str(getattr(entry, "subject", "") or "").casefold()
    return any(marker in subject or marker in blob for marker in _NOISE_SUBJECT_MARKERS)


def _rank_enriched_for_fallback(enriched: list[dict[str, Any]], plan: AskPlan) -> list[dict[str, Any]]:
    """Answer 失败时对已读上下文做本地排序，优先真人待回复线程。"""
    needs_reply = is_needs_reply_request(plan.user_request) or plan.goal == "draft_replies"
    actionable = is_actionable_browse_request(plan.user_request) or needs_reply

    def score(entry: dict[str, Any]) -> tuple[int, int]:
        relevance = 0
        if _is_automated_noise_entry(entry):
            relevance -= 50 if needs_reply else 20
        if entry.get("unread"):
            relevance += 8
        from_addr = str(entry.get("from") or "").casefold()
        if from_addr and not any(m in from_addr for m in _NOISE_SENDER_MARKERS):
            relevance += 12 if needs_reply else 4
        blob = f"{entry.get('subject', '')} {entry.get('snippet', '')} {entry.get('body', '')}".casefold()
        if any(token in blob for token in ("?", "？", "please", "could you", "can you", "回复", "确认", "请问")):
            relevance += 10
        try:
            # date 已是可读字符串时退化为 0，仍可按相关性排序。
            timestamp = int(entry.get("internal_date") or 0)
        except (TypeError, ValueError):
            timestamp = 0
        if not actionable:
            relevance += 0
        return (relevance, timestamp)

    ranked = sorted((e for e in enriched if isinstance(e, dict)), key=score, reverse=True)
    if needs_reply:
        human = [e for e in ranked if not _is_automated_noise_entry(e)]
        # 有真人候选时只展示真人；全是噪声则返回空，由上层给“无需回复”结论。
        return human[:8] if human else []
    return ranked[:8]


def _answer_fallback(
    plan: AskPlan,
    detail: str = "",
    *,
    enriched: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Answer LLM 失败时的安全降级。

    无候选时只返回错误摘要；已有扫描证据时列出真实邮件引用，
    不编造分析，也不把解析器 excerpt 甩到侧栏。
    """
    if detail:
        _logger.warning("ask answer fallback: detail=%s", str(detail)[:300])
    is_chinese = _uses_chinese(plan.user_request)
    needs_reply = is_needs_reply_request(plan.user_request) or plan.goal == "draft_replies"
    candidates = _rank_enriched_for_fallback(list(enriched or []), plan)
    if candidates:
        items: list[dict[str, Any]] = []
        for entry in candidates:
            subject = str(entry.get("subject") or "(no subject)")[:120]
            message_id = str(entry.get("message_id") or "")
            thread_id = str(entry.get("thread_id") or "")
            mailbox = str(entry.get("mailbox") or "")
            item: dict[str, Any] = {
                "subject": subject,
                "from": str(entry.get("from") or "")[:240],
                "context": str(entry.get("snippet") or entry.get("body") or "")[:240],
                "suggestion": (
                    "打开线程确认是否需要回复。"
                    if is_chinese
                    else "Open this thread and reply if a response is still needed."
                ),
                "mailbox": mailbox,
                "message_id": message_id,
                "thread_id": thread_id,
            }
            if message_id or thread_id:
                item["mail_links"] = [{
                    "label": subject,
                    "mailbox": mailbox,
                    "thread_id": thread_id,
                    "message_id": message_id,
                    "from": str(entry.get("from") or "")[:240],
                    "date": str(entry.get("date") or "")[:80],
                    "snippet": str(entry.get("snippet") or "")[:240],
                }]
            items.append(item)
        title = plan.title or (
            "需要你回复的邮件" if is_chinese and needs_reply
            else "可能需要关注的邮件" if is_chinese
            else "Emails that may need your reply" if needs_reply
            else "Emails to review"
        )
        summary = (
            f"完整分析暂时不可用。已根据发件人与主题筛出 {len(items)} 封更可能需要你处理的邮件。"
            if is_chinese
            else f"Full analysis is temporarily unavailable. Showing {len(items)} email(s) most likely to need your attention."
        )
        heading = (
            "更可能需要回复" if is_chinese and needs_reply
            else "候选邮件" if is_chinese
            else "Likely needs reply" if needs_reply
            else "Candidate emails"
        )
        return {
            "title": title,
            "summary": summary,
            "sections": [{"heading": heading, "items": items}],
            "fallback_used": True,
            "fallback_reason": str(detail)[:240] if detail else "answer_llm_failed",
        }

    # needs-reply 且全是噪声时，给明确“无需回复”结论，而不是空白错误。
    if needs_reply and enriched:
        title = plan.title or ("需要你回复的邮件" if is_chinese else "Emails that need your reply")
        summary = (
            "在最近扫描到的邮件里，主要是通知、验证码或自动邮件，没有明显需要你亲自回复的线程。"
            if is_chinese
            else "Among the recently scanned messages, most look like notifications, codes, or automated mail — none clearly need your personal reply."
        )
        return {
            "title": title,
            "summary": summary,
            "sections": [],
            "fallback_used": True,
            "fallback_reason": str(detail)[:240] if detail else "answer_llm_failed_noise_only",
        }

    if is_chinese:
        summary = "Anna 暂时无法生成可用的回答，请换个说法重试，或打开具体邮件后再问。"
        title = plan.title or "扫描未完成"
    else:
        summary = "Anna could not produce a usable answer. Try rephrasing, or open a specific email first."
        title = plan.title or "Scan incomplete"
    return {
        "title": title,
        "summary": summary,
        "sections": [],
        "fallback_used": True,
        "fallback_reason": str(detail)[:240] if detail else "answer_llm_failed",
    }


def _generated_copy_contains_chinese(payload: dict[str, Any]) -> bool:
    """检查 Ask Answer 的模型生成文案是否错误混入中文。

    context 常引用源邮件原文（中文邮件），不参与判断；
    只检查 title/summary/heading/body/suggestion/reply_gaps。
    """
    copy_values = [payload.get("title"), payload.get("summary")]
    sections = payload.get("sections") if isinstance(payload.get("sections"), list) else []
    for section in sections:
        if not isinstance(section, dict):
            continue
        copy_values.extend((section.get("heading"), section.get("body")))
        items = section.get("items") if isinstance(section.get("items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            # context 允许保留源语言摘录；suggestion 必须跟用户请求语言一致。
            copy_values.append(item.get("suggestion"))
            gaps = item.get("reply_gaps") if isinstance(item.get("reply_gaps"), dict) else {}
            copy_values.append(gaps.get("summary"))
            questions = gaps.get("questions") if isinstance(gaps.get("questions"), list) else []
            for question in questions:
                if isinstance(question, dict):
                    copy_values.extend((question.get("question"), question.get("hint")))
    return any(_uses_chinese(str(value or "")) for value in copy_values)


def _scrub_chinese_generated_copy(payload: dict[str, Any], plan: AskPlan) -> dict[str, Any]:
    """英文请求下，把混入中文的生成字段改成安全英文，尽量保留已解析结构。"""
    cleaned = dict(payload)
    if _uses_chinese(str(cleaned.get("title") or "")):
        cleaned["title"] = plan.title or "Emails that need your reply"
    if _uses_chinese(str(cleaned.get("summary") or "")):
        cleaned["summary"] = "Here are the emails that look most relevant to your request."
    sections = cleaned.get("sections") if isinstance(cleaned.get("sections"), list) else []
    new_sections: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        sec = dict(section)
        if _uses_chinese(str(sec.get("heading") or "")):
            sec["heading"] = "Needs attention"
        if _uses_chinese(str(sec.get("body") or "")):
            sec["body"] = ""
        items = sec.get("items") if isinstance(sec.get("items"), list) else []
        new_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            if _uses_chinese(str(row.get("suggestion") or "")):
                row["suggestion"] = "Review this thread and reply if needed."
            gaps = row.get("reply_gaps") if isinstance(row.get("reply_gaps"), dict) else None
            if gaps and (
                _uses_chinese(str(gaps.get("summary") or ""))
                or any(
                    _uses_chinese(str((q or {}).get("question") or ""))
                    or _uses_chinese(str((q or {}).get("hint") or ""))
                    for q in (gaps.get("questions") or [])
                    if isinstance(q, dict)
                )
            ):
                row.pop("reply_gaps", None)
            new_items.append(row)
        sec["items"] = new_items
        new_sections.append(sec)
    cleaned["sections"] = new_sections
    cleaned["language_scrubbed"] = True
    return cleaned


async def _filter_candidates(
    messages: list[MessageLite],
    plan: AskPlan,
    *,
    sampling_create_message: Any = None,
) -> list[MessageLite]:
    """保留全部搜索命中，相关度筛选统一由本地排序在正文读取前完成。"""
    # 不再为筛选额外调用或重试 Sampling，避免空响应消耗预算并拖长 Answer 阶段。
    _ = plan, sampling_create_message
    return list(messages)


# ── Context reader ─────────────────────────────────────────────────────

def _fmt_ts(epoch_ms: str) -> str:
    if not epoch_ms:
        return ""
    try:
        dt = datetime.fromtimestamp(int(epoch_ms) / 1000.0, tz=_BEIJING_TZ)
        return dt.strftime("%b %d, %Y, %H:%M")
    except (ValueError, TypeError, OSError):
        return str(epoch_ms)[:20]


def _select_candidates_for_context(candidates: list[MessageLite], plan: AskPlan) -> list[MessageLite]:
    """按请求相关度排序并选择有限候选，避免将所有正文交给模型。"""
    # 这里是确定性本地排序，不新增 Sampling 调用；关键词仅用于缩小正文读取集合。
    needs_reply = is_needs_reply_request(plan.user_request) or plan.goal == "draft_replies"
    actionable = is_actionable_browse_request(plan.user_request) or needs_reply
    terms = [str(term).casefold().strip() for topic in plan.topics for term in topic.get("search_terms", [])]
    if not actionable:
        # 「需要浏览/处理」类请求的用户词（浏览/处理）不应作为 Gmail 命中加权，否则噪声偏大。
        terms.extend(re.findall(r"[\w\u3400-\u9fff]{2,}", plan.user_request.casefold()))
    terms = [term for term in terms if term]

    def score(item: tuple[int, MessageLite]) -> tuple[int, int, int]:
        index, message = item
        subject = (message.subject or "").casefold()
        snippet = (message.snippet or "").casefold()
        from_addr = (getattr(message, "from_addr", None) or "").casefold()
        relevance = sum(8 for term in terms if term in subject)
        relevance += sum(3 for term in terms if term in snippet)
        if getattr(message, "unread", False):
            # actionable 路径更偏向未读/待处理，提高进入上下文的机会。
            relevance += 10 if actionable else 4
        if actionable or needs_reply:
            if _is_automated_noise_entry(message):
                relevance -= 40 if needs_reply else 12
            elif from_addr:
                relevance += 14 if needs_reply else 4
            blob = f"{subject} {snippet}"
            if any(token in blob for token in ("?", "？", "please", "could you", "can you", "回复", "确认", "请问")):
                relevance += 8
        try:
            timestamp = int(message.internal_date or 0)
        except (TypeError, ValueError):
            timestamp = 0
        return (relevance, timestamp, -index)

    ranked = sorted(enumerate(candidates), key=score, reverse=True)
    selected = [message for _index, message in ranked[:_MAX_CONTEXT_CANDIDATES]]
    if needs_reply:
        human = [message for message in selected if not _is_automated_noise_entry(message)]
        # 至少给模型 1–2 封真人邮件；若全噪声则仍传原排序前几封以免空上下文。
        if human:
            return human[:_MAX_CONTEXT_CANDIDATES]
    return selected


async def _read_candidate_context(
    candidates: list[MessageLite],
    mailbox: str,
    *,
    mailbox_by_message_id: dict[str, str] | None = None,
    sampling_create_message: Any = None,
    progress_callback: Any = None,
) -> list[dict[str, Any]]:
    """Read message bodies and contact memory for each candidate.

    Returns enriched candidate dicts ready for Answer LLM rendering.
    """
    from ..mail_providers.gmail.adapter import normalize_mailbox, get_message_detail, get_thread_context

    normalized = normalize_mailbox(mailbox)
    # 即使未来有其他调用方绕过编排层，也不能读取无限量邮件正文。
    candidates = candidates[:_MAX_CONTEXT_CANDIDATES]

    # Collect unique senders for contact memory retrieval
    sender_emails: list[str] = []
    seen_senders: set[str] = set()
    for msg in candidates:
        addr = (msg.from_addr or "").strip()
        if addr and addr not in seen_senders:
            seen_senders.add(addr)
            sender_emails.append(addr)

    # Retrieve contact memory (best-effort, concurrent)
    contact_contexts: dict[str, str] = {}
    try:
        from ..contact_memory.retriever import retrieve_contact_context, format_contact_context_for_prompt
        from ..contact_memory.types import ContactMemoryQuery

        async def _fetch_one(addr: str) -> tuple[str, str]:
            try:
                ctx = await retrieve_contact_context(ContactMemoryQuery(
                    mailbox=normalized,
                    contact_email=addr,
                    purpose="thread_summary",
                ), sampling_create_message)
                return (addr, format_contact_context_for_prompt(ctx))
            except Exception:
                return (addr, "")

        import asyncio as _asyncio
        results = await _asyncio.gather(*[_fetch_one(a) for a in sender_emails[:5]])
        for addr, ctx_text in results:
            contact_contexts[addr] = ctx_text
    except ImportError:
        pass

    import asyncio as _asyncio

    # Gmail adapter 为同步 I/O，放入线程池并行读取，用信号量限制并发。
    semaphore = _asyncio.Semaphore(_CONTEXT_READ_CONCURRENCY)
    total = len(candidates)

    async def _read_one(idx: int, msg: MessageLite) -> dict[str, Any]:
        source_mailbox = (mailbox_by_message_id or {}).get(msg.message_id or "", mailbox)
        source_mailbox = normalize_mailbox(source_mailbox)
        entry: dict[str, Any] = {
            "mailbox": source_mailbox,
            "message_id": msg.message_id or "",
            "thread_id": msg.thread_id or "",
            "from": msg.from_addr or "",
            "to": msg.to_addr or "",
            "subject": msg.subject or "",
            "date": _fmt_ts(msg.internal_date or ""),
            "snippet": msg.snippet or "",
            "unread": getattr(msg, "unread", False),
            "label_ids": getattr(msg, "label_ids", None) or [],
            "body": "",
            "thread": [],
            "contact_context": contact_contexts.get(msg.from_addr or "", ""),
        }
        async with semaphore:
            try:
                detail = await _asyncio.to_thread(get_message_detail, source_mailbox, msg.message_id)
                if detail:
                    entry["body"] = (getattr(detail, "body_text", "") or "")[:1200]
            except Exception:
                pass
            try:
                thread_ctx = await _asyncio.to_thread(
                    get_thread_context, source_mailbox, msg.thread_id or msg.message_id
                )
                if thread_ctx and thread_ctx.messages:
                    entry["thread"] = []
                    for tm in thread_ctx.messages[:3]:
                        entry["thread"].append({
                            "from": getattr(tm, "from_addr", "") or "",
                            "to": getattr(tm, "to_addr", "") or "",
                            "subject": getattr(tm, "subject", "") or "",
                            "date": _fmt_ts(getattr(tm, "internal_date", "") or ""),
                            "body": (getattr(tm, "body_text", "") or "")[:400],
                        })
            except Exception:
                entry["thread"] = []
        if progress_callback and (idx % 2 == 0 or idx == total):
            progress_callback("read_context", {"current": idx, "total": total})
        return entry

    return list(await _asyncio.gather(*[
        _read_one(idx, msg) for idx, msg in enumerate(candidates, 1)
    ]))


# ── Answer LLM ─────────────────────────────────────────────────────────
# _EXECUTION_SYSTEM_PROMPT imported from core.pipeline at module top


def _render_candidates_for_llm(
    enriched: list[dict[str, Any]],
    *,
    body_limit: int = 4000,
    thread_body_limit: int = 2000,
    max_thread_messages: int = 20,
) -> str:
    """Render enriched candidates as compact text for the Answer LLM."""
    parts: list[str] = []
    for i, e in enumerate(enriched, 1):
        unread_label = " (UNREAD)" if e.get("unread") else ""
        labels = [str(l) for l in (e.get("label_ids") or []) if str(l) not in ("UNREAD",)]
        labels_str = f"  Labels: {', '.join(labels)}" if labels else ""
        parts.append(
            f"### Email {i}\n"
            f"From: {e.get('from', '')}\n"
            f"Subject: {e.get('subject', '')}{unread_label}\n"
            f"Date: {e.get('date', '')}\n"
            f"Mailbox: {e.get('mailbox', '')}\n"
            f"Thread ID: {e.get('thread_id', '')}\n"
            f"Message ID: {e.get('message_id', '')}{labels_str}"
        )
        if e.get("body") and body_limit > 0:
            parts.append(f"Snippet: {e.get('snippet', '')}")
            parts.append(f"Body:\n{e['body'][:body_limit]}")
        else:
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
        # Contact context
        if e.get("contact_context"):
            parts.append(f"Contact context: {e['contact_context']}")
        parts.append("")
    return "\n".join(parts)


async def _generate_answer(
    plan: AskPlan,
    enriched: list[dict[str, Any]],
    mailbox: str,
    *,
    sampling_create_message: Any = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """Run the Answer LLM on filtered, context-enriched candidates.

    Progressive truncation fallback: full → compact → short → headers.
    """
    from ..llm_runtime.service import call_llm_json_safe

    # Build the stable part of the user prompt (doesn't change between variants)
    def _build_user_prompt(rendered: str) -> str:
        return (
            f"## Your Identity\n"
            f"You are Anna, executive assistant to {mailbox}.\n"
            f"In all output text, address your principal directly as 'you' / 'your'.\n"
            f"Match by EMAIL ADDRESS (between < >), not by display name.\n\n"
            f"## User request\n"
            f"{plan.user_request}\n\n"
            f"## Response language\n"
            f"{_answer_language_instruction(plan.user_request)}\n\n"
            f"## Task\n"
            f"{plan.task_prompt}\n\n"
            f"## Two-phase reply generation\n"
            f"For EVERY item that needs a reply, decide between two paths:\n"
            f"PATH A — You have enough context → write the draft in the 'draft' field.\n"
            f"PATH B — You need user clarification → OMIT 'draft', set reply_gaps.needs_user_input=true "
            f"with specific questions. The user will answer, and a draft will be generated later.\n"
            f"CRITICAL: Never include both draft AND reply_gaps.needs_user_input on the same item.\n"
            f"When in doubt, choose Path B. A bad guess is worse than asking.\n\n"
            f"## Structured mail references\n"
            f"When an item cites one or more provided emails, include mail_links (maximum 5):\n"
            f"[{{\"label\": \"exact email subject\", \"mailbox\": \"provided mailbox\", "
            f"\"thread_id\": \"provided thread id\", \"message_id\": \"provided message id\"}}]\n"
            f"Use only IDs and subjects shown below. Never emit href, URLs, or invented references.\n"
            f"For a single-email item, also include its mailbox, thread_id, and message_id fields.\n\n"
            f"## Relevant emails ({len(enriched)} total)\n"
            f"{rendered}\n\n"
            f"## Important\n"
            f"- Base your answer ONLY on the emails provided below.\n"
            f"- {_ASK_SYNTHESIS_INSTRUCTION}\n"
            f"- If the emails below do not contain what the user is looking for, say so honestly.\n"
            f"- Output real JSON values only. Never emit schema tokens like string, string?, array, object, or boolean."
        )

    # Anna invoke 只尝试一次紧凑回答；失败直接返回安全 fallback，不能再消耗多轮 token。
    variants = [{"name": "compact", "body_limit": 700, "thread_body_limit": 240, "max_thread_messages": 2}]

    result: dict[str, Any] | None = None
    last_error = ""
    for variant in variants:
        rendered = _render_candidates_for_llm(
            enriched,
            body_limit=int(variant["body_limit"]),
            thread_body_limit=int(variant["thread_body_limit"]),
            max_thread_messages=int(variant["max_thread_messages"]),
        )
        try:
            result = await call_llm_json_safe(
                sampling_create_message,
                system_prompt=_ASK_ANSWER_SYSTEM_PROMPT,
                user_message=_build_user_prompt(rendered),
                fallback={"title": "Scan failed", "summary": "Unable to analyze emails.", "sections": []},
                temperature=0.2,
                max_tokens=ASK_ANSWER_MAX_TOKENS,
                timeout=60.0,
                metadata={"tool": "ask_answer", "email_count": str(len(enriched)), "variant": variant["name"]},
                allow_fallback=sampling_create_message is None,
                allow_sampling_provider_fallback=True,
                max_attempts=1 if sampling_create_message is not None else None,
            )
            break
        except Exception as exc:
            last_error = str(exc)
            if progress_callback:
                progress_callback("evaluate", {"variant": variant["name"], "reason": last_error[:200]})

    if result is None:
        # 无有效 JSON 时：有证据则列候选邮件，无证据则返回错误摘要（不伪造分析）。
        return _answer_fallback(plan, last_error or "Anna sampling failed", enriched=enriched)

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    if not payload:
        return _answer_fallback(plan, "Anna returned empty analysis", enriched=enriched)
    # 英文请求若模型生成文案混入中文：优先 scrub 保留结构，避免整份答案被丢弃。
    if not _uses_chinese(plan.user_request) and _generated_copy_contains_chinese(payload):
        _logger.warning("ask answer scrubbed Chinese generated copy for English request")
        payload = _scrub_chinese_generated_copy(payload, plan)
        if _generated_copy_contains_chinese(payload):
            return _answer_fallback(
                plan,
                "Anna returned Chinese generated copy for an English request",
                enriched=enriched,
            )
    payload["llm_meta"] = {
        "provider": result.get("provider"),
        "model": result.get("model"),
        "usage": result.get("usage"),
    }
    return payload


# ── Guard ───────────────────────────────────────────────────────────────

_FORBIDDEN_ACTIONS: list[str] = ["send", "delete", "forward", "unsubscribe"]


def _normalize_reference_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _mail_link_from_source(message_id: str, source: dict[str, str]) -> dict[str, str]:
    return {
        "label": str(source.get("subject") or "(no subject)")[:120],
        "mailbox": str(source.get("mailbox") or ""),
        "thread_id": str(source.get("thread_id") or ""),
        "message_id": message_id,
        "from": str(source.get("from") or "")[:240],
        "date": str(source.get("date") or "")[:80],
        "snippet": str(source.get("snippet") or "")[:240],
    }


def _apply_guard(
    result: dict[str, Any],
    valid_ids: set[str],
    valid_thread_ids: set[str],
    valid_sources: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Apply safety guards to the Answer LLM output.

    1. Block forbidden action language in suggestions and drafts
    2. Strip message_id / thread_id references that don't exist in source emails
    """
    sections = result.get("sections")
    if not isinstance(sections, list):
        sections = []
        result["sections"] = sections
    sources = valid_sources or {}
    linked_threads: set[tuple[str, str]] = set()

    for section in sections:
        if not isinstance(section, dict):
            continue
        items = section.get("items")
        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue

            # Guard 1: flag (don't destroy) forbidden action language.
            # "send" can appear in legitimate advice ("you should send a reply")
            # vs. automated action ("I'll send the reply for you"). We can't
            # reliably distinguish these with regex, so we flag instead of block.
            for field in ("suggestion", "draft"):
                text = str(item.get(field, ""))
                if not text:
                    continue
                lowered = text.lower()
                for action in _FORBIDDEN_ACTIONS:
                    pattern = r"\b" + re.escape(action) + r"\b"
                    if re.search(pattern, lowered):
                        item["_guard_warning"] = f"contains '{action}' — review before acting"
                        _logger.warning("Ask guard flagged '%s' in %s", action, field)
                        break

            # Guard 2: validate message_id / thread_id references
            mid = str(item.get("message_id", ""))
            if mid and mid not in valid_ids:
                item["message_id"] = ""
                _logger.warning("Ask guard stripped invalid message_id: %s", mid[:40])

            tid = str(item.get("thread_id", ""))
            if tid and tid not in valid_thread_ids:
                item["thread_id"] = ""
                _logger.warning("Ask guard stripped invalid thread_id: %s", tid[:40])

            source = sources.get(mid)
            item_source_is_valid = False
            if source:
                expected_thread = source.get("thread_id", "")
                if tid and tid != expected_thread:
                    item["thread_id"] = ""
                else:
                    item_source_is_valid = True
                    item["thread_id"] = expected_thread
                    item["mailbox"] = source.get("mailbox", "")
                    item["subject"] = source.get("subject", item.get("subject", ""))
                    item["from"] = source.get("from", item.get("from", ""))

            raw_links = item.get("mail_links")
            safe_links: list[dict[str, str]] = []
            seen_threads: set[tuple[str, str]] = set()

            def add_source_link(link_mid: str, link_source: dict[str, str]) -> None:
                if len(safe_links) >= 5:
                    return
                link_mailbox = str(link_source.get("mailbox") or "").strip().lower()
                link_tid = str(link_source.get("thread_id") or "").strip()
                if not link_mid or not link_mailbox or not link_tid:
                    return
                thread_key = (link_mailbox, link_tid)
                if thread_key in seen_threads:
                    return
                seen_threads.add(thread_key)
                safe_links.append(_mail_link_from_source(link_mid, link_source))

            if isinstance(raw_links, list):
                for raw_link in raw_links:
                    if not isinstance(raw_link, dict):
                        continue
                    link_mid = str(raw_link.get("message_id") or "").strip()
                    link_tid = str(raw_link.get("thread_id") or "").strip()
                    link_mailbox = str(raw_link.get("mailbox") or "").strip().lower()
                    link_source = sources.get(link_mid)
                    if not link_source:
                        continue
                    if link_tid != link_source.get("thread_id") or link_mailbox != link_source.get("mailbox", "").lower():
                        continue
                    add_source_link(link_mid, link_source)

            # 模型可能漏掉 mail_links；条目自身的合法 ID 仍应确定性补成链接。
            if source and item_source_is_valid:
                add_source_link(mid, source)

            # 若模型连 ID 也漏掉，只接受结果文本中完整出现的候选邮件主题，避免模糊匹配误跳转。
            reference_text = _normalize_reference_text(" ".join(
                str(item.get(field) or "") for field in ("subject", "context", "suggestion", "draft")
            ))
            item_subject = _normalize_reference_text(item.get("subject"))
            for source_mid, candidate_source in sources.items():
                candidate_subject = _normalize_reference_text(candidate_source.get("subject"))
                if not candidate_subject:
                    continue
                exact_subject_mentioned = candidate_subject in reference_text
                if len(candidate_subject) < 6:
                    exact_subject_mentioned = item_subject == candidate_subject
                if exact_subject_mentioned:
                    add_source_link(source_mid, candidate_source)

            item["mail_links"] = safe_links
            linked_threads.update(seen_threads)

    # 模型有时只在标题或摘要里列出邮件主题，未创建 sections/items；这时前端没有
    # 可渲染的链接。只对摘要中完整出现的候选主题补一个结构化条目，目标 ID 始终
    # 来自 Gmail 候选，短主题不做包含匹配以避免把无关文本错误链接到邮件。
    narrative = _normalize_reference_text(
        f"{result.get('title') or ''} {result.get('summary') or ''}"
    )
    narrative_items: list[dict[str, Any]] = []
    for source_mid, source in sources.items():
        subject = _normalize_reference_text(source.get("subject"))
        mailbox = str(source.get("mailbox") or "").strip().lower()
        thread_id = str(source.get("thread_id") or "").strip()
        if (
            len(narrative_items) >= 5
            or len(subject) < 6
            or subject not in narrative
            or not mailbox
            or not thread_id
            or (mailbox, thread_id) in linked_threads
        ):
            continue
        linked_threads.add((mailbox, thread_id))
        narrative_items.append({
            "subject": str(source.get("subject") or ""),
            "mailbox": mailbox,
            "message_id": source_mid,
            "thread_id": thread_id,
            "from": str(source.get("from") or ""),
            "mail_links": [_mail_link_from_source(source_mid, source)],
        })
    if narrative_items:
        sections.append({"items": narrative_items})

    return result


# ── Orchestrator ────────────────────────────────────────────────────────

async def run_ask_pipeline(
    user_request: str = "",
    mailboxes: list[str] | None = None,
    *,
    plan: AskPlan | None = None,
    scan_window_days: int | None = None,
    max_messages: int | None = None,
    sampling_create_message: Any = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """Full Ask pipeline: plan → search → filter → context → answer → guard.

    Supports multiple mailboxes: plan once, search+filter concurrently per mailbox,
    merge candidates, then single answer pass.

    If `plan` is provided, skips the Planner LLM and uses the given plan directly
    (for re-running saved plans without re-planning).
    """
    from .planner import (
        normalize_actionable_browse_plan,
        normalize_user_facing_plan_copy,
        plan_ask_request,
        resolve_effective_timeframe,
    )
    from .search import build_queries, execute_search

    if not mailboxes:
        return {"title": "Error", "summary": "No mailbox selected.", "sections": []}

    primary_mailbox = mailboxes[0]

    # ── 1. Plan (once, skipped if plan provided) ───────────────────
    if plan is None:
        if not user_request:
            return {"title": "Error", "summary": "No user request provided.", "sections": []}
        if progress_callback:
            progress_callback("plan", {"stage": "plan"})
        plan = await plan_ask_request(user_request, primary_mailbox, sampling_create_message=sampling_create_message)
    # 已保存计划可绕过 Planner；仍须在执行入口执行同一语言与 actionable 边界校验。
    plan = normalize_user_facing_plan_copy(plan)
    plan = normalize_actionable_browse_plan(plan)

    # 模型计划只能决定检索意图，默认扫描时间必须服从用户当前 Scan Plan。
    # 裸「最近/recent」不覆盖 Scan Plan；仅明确数字或具体单位（本周/本月等）才覆盖。
    plan.timeframe = resolve_effective_timeframe(plan.user_request or user_request, scan_window_days, plan.timeframe)

    timeframe_match = re.fullmatch(r"(\d{1,3})d", plan.timeframe)
    timeframe_days = int(timeframe_match.group(1)) if timeframe_match else 0
    if progress_callback:
        progress_callback("plan_done", {
            "title": plan.title, "goal": plan.goal,
            "direction": plan.direction, "timeframe": plan.timeframe,
            "scan_window_days": timeframe_days,
        })

    # ── 2. Search + filter per mailbox (concurrent) ──────────────────
    import asyncio

    primary_queries: list[dict[str, Any]] = []
    all_sources: list[dict[str, str]] = []
    # 指定联系人属于精确检索意图。若移除 from:/to: 后继续扫描，返回的只是
    # 同时间范围内的无关邮件，可能造成“找到结果”与“未找到该联系人”同时出现。
    has_person_constraint = any(
        str(person.get("name_hint") or "").strip()
        for person in plan.people
        if isinstance(person, dict)
    )

    async def _search_one(mbox: str) -> tuple[str, list[MessageLite], list[MessageLite]]:
        """Search + filter for a single mailbox. Returns (mailbox, all_messages, candidates)."""
        nonlocal primary_queries
        queries = await build_queries(plan, mbox)
        if not primary_queries and mbox == primary_mailbox:
            primary_queries = queries
        if progress_callback:
            progress_callback("search", {
                "mailbox": mbox,
                "query_total": len(queries),
                "scan_window_days": timeframe_days,
            })

        # 多邮箱并发时最多 broaden 1 次，避免 0 结果时 Gmail 调用成倍放大。
        messages = await execute_search(
            mbox,
            queries,
            progress_callback=progress_callback,
            max_broaden_attempts=1 if len(mailboxes) > 1 else 2,
            max_messages=max_messages if max_messages is not None else 200,
            allow_broadening=not has_person_constraint,
        )
        if not messages:
            return (mbox, [], [])

        # Capture source previews for frontend progress display
        for msg in messages[:8]:
            all_sources.append({
                "subject": getattr(msg, "subject", "") or "",
                "from": getattr(msg, "from_addr", "") or "",
                "date": _fmt_ts(getattr(msg, "internal_date", "") or ""),
                "thread_id": getattr(msg, "thread_id", "") or "",
            })

        if progress_callback:
            thread_count = len({message.thread_id for message in messages if message.thread_id})
            progress_callback("search_done", {
                "mailbox": mbox, "scanned": len(messages), "threads": thread_count,
                "scan_window_days": timeframe_days,
                "partial": {"sources": all_sources[-8:]},
            })

        filtered = await _filter_candidates(messages, plan, sampling_create_message=sampling_create_message)
        return (mbox, messages, filtered)

    per_mailbox = await asyncio.gather(*[_search_one(m) for m in mailboxes])

    # ── 3. Merge candidates ─────────────────────────────────────────
    all_candidates: list[MessageLite] = []
    candidate_mailboxes: dict[str, str] = {}
    seen_ids: set[str] = set()
    total_scanned = 0

    for _mbox, messages, candidates in per_mailbox:
        total_scanned += len(messages)
        for c in candidates:
            cid = (c.message_id or "", c.thread_id or "")
            if cid not in seen_ids:
                seen_ids.add(cid)
                all_candidates.append(c)
                if c.message_id:
                    candidate_mailboxes[c.message_id] = _mbox

    if not all_candidates:
        # 0 命中也要带回扫描计数与计划元数据，避免前端把「扫过」误显示成 0。
        empty_base = {
            "plan_id": plan.plan_id,
            "plan_title": plan.title,
            "plan_description": plan.description,
            "plan_timeframe": plan.timeframe,
            "plan_direction": plan.direction,
            "plan_goal": plan.goal,
            "plan_gmail_flags": plan.gmail_flags,
            "plan_topics": plan.topics,
            "plan_queries": primary_queries,
            "planner_llm": plan.llm_meta,
            "messages_scanned": total_scanned,
            "candidates_found": 0,
            "sections": [],
        }
        if _uses_chinese(plan.user_request):
            return {
                **empty_base,
                "title": plan.title or "未找到结果",
                "summary": f"已扫描 {len(mailboxes)} 个邮箱中的 {total_scanned} 封邮件，但没有找到符合你要求的内容。",
            }
        return {
            **empty_base,
            "title": plan.title or "No results",
            "summary": f"Scanned {total_scanned} emails across {len(mailboxes)} mailbox(es) but none matched your request.",
        }

    if progress_callback:
        progress_callback("filter_done", {"candidates": len(all_candidates), "scanned": total_scanned})

    # ── 4. Context ──────────────────────────────────────────────────
    context_candidates = _select_candidates_for_context(all_candidates, plan)
    if progress_callback:
        # 只展示安全数量，明确告知用户模型仅打开必要的有限上下文。
        progress_callback("read_context", {"current": 0, "total": len(context_candidates)})
    enriched = await _read_candidate_context(
        context_candidates, primary_mailbox,
        mailbox_by_message_id=candidate_mailboxes,
        # 联系人记忆会对每个联系人再发起 LLM 选择；Ask Session 每轮只允许规划与回答两次调用。
        sampling_create_message=None,
        progress_callback=progress_callback,
    )
    if progress_callback:
        progress_callback("read_context_done", {"total": len(enriched)})

    # ── 5. Answer LLM ───────────────────────────────────────────────
    if progress_callback:
        progress_callback("answer", {"candidates": len(enriched)})
    result = await _generate_answer(plan, enriched, primary_mailbox,
                                     sampling_create_message=sampling_create_message,
                                     progress_callback=progress_callback)

    # ── 6. Guard ────────────────────────────────────────────────────
    valid_ids = {c.message_id or "" for c in context_candidates if c.message_id}
    valid_thread_ids = {c.thread_id or "" for c in context_candidates if c.thread_id}
    valid_sources = {
        str(entry.get("message_id") or ""): {
            "mailbox": str(entry.get("mailbox") or ""),
            "thread_id": str(entry.get("thread_id") or ""),
            "subject": str(entry.get("subject") or ""),
            "from": str(entry.get("from") or ""),
            "date": str(entry.get("date") or ""),
            "snippet": str(entry.get("snippet") or ""),
        }
        for entry in enriched
        if entry.get("message_id")
    }
    result = _apply_guard(result, valid_ids, valid_thread_ids, valid_sources)

    result.setdefault("plan_id", plan.plan_id)
    result.setdefault("plan_title", plan.title)
    result.setdefault("plan_description", plan.description)
    result.setdefault("plan_timeframe", plan.timeframe)
    result.setdefault("plan_direction", plan.direction)
    result.setdefault("plan_goal", plan.goal)
    result.setdefault("plan_gmail_flags", plan.gmail_flags)
    result.setdefault("plan_topics", plan.topics)
    result.setdefault("plan_queries", primary_queries)
    result.setdefault("planner_llm", plan.llm_meta)
    result.setdefault("messages_scanned", total_scanned)
    result.setdefault("candidates_found", len(all_candidates))

    return result


# ── Ask item draft generation ─────────────────────────────────────────

_ASK_DRAFT_SYSTEM = """You are Anna, an executive email assistant. Generate a professional, concise email reply. Use the user's answers to the clarifying questions to fill in the details they provided. Do NOT make up information beyond what the user told you."""


async def generate_ask_item_draft(
    message_id: str,
    thread_id: str,
    mailbox: str,
    from_addr: str,
    subject: str,
    user_answers: dict[str, str],
    *,
    sampling_create_message: Any = None,
) -> dict[str, Any]:
    """Generate a draft reply for an Ask result item, incorporating user answers.

    Reads the original email body from Gmail cache, then calls the LLM
    with the user's answers to produce a draft.
    """
    from ..llm_runtime.service import call_llm_json_safe
    from ..mail_providers.gmail.adapter import normalize_mailbox, get_message_detail

    normalized = normalize_mailbox(mailbox)

    # Read original email body
    body_text = ""
    try:
        detail = get_message_detail(normalized, message_id)
        if detail:
            body_text = (getattr(detail, "body_text", "") or "")[:3000]
    except Exception:
        pass

    # Format user answers
    answers_text = "\n".join(f"Q: {q}\nA: {a}" for q, a in user_answers.items() if a.strip())

    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=_ASK_DRAFT_SYSTEM,
        user_message=(
            f"## Original email\n"
            f"From: {from_addr}\n"
            f"Subject: {subject}\n\n"
            f"{body_text}\n\n"
            f"## User's answers to clarifying questions\n"
            f"{answers_text}\n\n"
            f"## Instructions\n"
            f"Write a professional reply email that incorporates the user's answers above. "
            f"Do NOT add information the user didn't provide. "
            f"Output JSON: {{\"subject\": \"Reply subject\", \"body\": \"Reply body text\"}}"
        ),
        fallback={"subject": "", "body": "", "note": "Draft generation failed"},
        temperature=0.3,
        max_tokens=4096,
        timeout=120.0,
        metadata={"tool": "ask_draft", "message_id": message_id},
        allow_fallback=True,
        allow_sampling_provider_fallback=True,
    )

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    return {
        "subject": str(payload.get("subject") or ""),
        "body": str(payload.get("body") or ""),
        "fallback_used": result.get("fallback_used", False),
    }
