"""Ask pipeline answer stage: filter → context → answer LLM → guard.

run_ask_pipeline() is the orchestrator: plan → search → filter → context → answer → guard.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from html import escape
from typing import Any

from ..domain.types import MessageLite
from .planner import AskPlan, is_actionable_browse_request, is_needs_reply_request
from .sampling_budget import ask_answer_output_token_cap, ask_sampling_tokens

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
# 用户未指定数量时默认返回 3 条；显式数量上限 8，避免 token 与延迟爆炸。
_DEFAULT_ANSWER_ITEMS = 3
_MAX_ANSWER_ITEMS = 8
# 进入 Answer 的候选数与返回条数对齐（默认 3，上限 8），降低输入体积与截断风险。
_DEFAULT_CONTEXT_CANDIDATES = 3
_MAX_CONTEXT_CANDIDATES = 8
# 紧凑渲染：正文/线程上限（needs-reply 才带 1 条短线程）；进一步压 input tokens。
_ANSWER_BODY_LIMIT = 220
_ANSWER_THREAD_BODY_LIMIT = 80
_ANSWER_MAX_THREAD_MESSAGES = 1
_ANSWER_SNIPPET_LIMIT = 120
_ANSWER_SUBJECT_LIMIT = 120
_ANSWER_TASK_PROMPT_LIMIT = 150
_ANSWER_REQUEST_LIMIT = 360
# 正文/线程 Gmail 读限并发，避免过多候选放大上下文与模型首 token 延迟。
_CONTEXT_READ_CONCURRENCY = 4
# 从用户话术解析「要几封」：中文「5封/找5封邮件」、英文「top 5 / 5 emails」。
_ANSWER_ITEM_COUNT_PATTERNS = (
    re.compile(
        r"(?:top|first|last|find|show|list|get|return|need|want|"
        r"挑|找|找出|列出|给我|返回|显示|需要|优先处理|优先)?"
        r"\s*(\d{1,2})\s*(?:封(?:邮件|信)?|emails?|mails?|messages?|threads?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(\d{1,2})\s*(?:封(?:邮件|信)?|emails?|mails?|messages?|threads?)",
        re.IGNORECASE,
    ),
)
# 即使有必要阅读片段，回答也必须综合事实，避免退化为邮件正文复制。
_ASK_SYNTHESIS_INSTRUCTION = (
    "Summarize and synthesize the evidence in your own words. "
    "Do not copy email body verbatim, except for a short necessary quote or an exact subject."
)
# {item_limit} 由 resolve_answer_item_limit 注入；默认 3，用户指定时最高 8。
def parse_answer_item_limit(user_request: str) -> tuple[int, bool]:
    """解析期望条数。

    返回 ``(limit, explicit)``：
    - explicit=True：用户话术里写了数字（如「5 封」「top 5」）
    - explicit=False：走默认 3
    """
    text = str(user_request or "").strip()
    if not text:
        return _DEFAULT_ANSWER_ITEMS, False
    for pattern in _ANSWER_ITEM_COUNT_PATTERNS:
        matched = pattern.search(text)
        if not matched:
            continue
        try:
            count = int(matched.group(1))
        except (TypeError, ValueError):
            continue
        return max(1, min(count, _MAX_ANSWER_ITEMS)), True
    return _DEFAULT_ANSWER_ITEMS, False


def resolve_answer_item_limit(user_request: str) -> int:
    """从用户请求解析期望返回的邮件条数。

    未指定时默认 3；显式数字 clamp 到 [1, 8]。
    例如「找 5 封邮件」「top 5 emails」「需要优先处理的 8 封」。
    """
    limit, _explicit = parse_answer_item_limit(user_request)
    return limit


def resolve_context_candidate_limit(user_request: str) -> int:
    """进入 Answer 的候选上限。

    - 默认：与默认 item 上限一致（3）
    - 用户显式要 N 封：与 N 对齐（硬顶 8），不再 +2，避免 input 膨胀
    """
    item_limit, _explicit = parse_answer_item_limit(user_request)
    return min(_MAX_CONTEXT_CANDIDATES, max(1, item_limit))


def _build_answer_system_prompt(item_limit: int) -> str:
    """按本次请求的条数上限生成 Answer system prompt（prompts.py 任务隔离）。"""
    from mail_agent.ai_turn.prompts import ask_answer_system_prompt

    limit = max(1, min(int(item_limit or _DEFAULT_ANSWER_ITEMS), _MAX_ANSWER_ITEMS))
    return ask_answer_system_prompt(limit)


# 兼容旧测试与外部引用：默认「最多 3 条」的 system prompt。
_ASK_ANSWER_SYSTEM_PROMPT = _build_answer_system_prompt(_DEFAULT_ANSWER_ITEMS)


def _answer_language_instruction(user_request: str) -> str:
    """极短语言约束（省 input tokens）。"""
    if re.search(r"[\u3400-\u9fff]", user_request):
        return "lang=zh; keep subjects/names original"
    return "lang=en; keep subjects/names original"


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


def _answer_fallback(
    plan: AskPlan,
    detail: str = "",
) -> dict[str, Any]:
    """Answer LLM 失败时返回统一、可重试的用户错误。

    已完成的检索证据不能替代模型分析。若模型未能生成有效结果，必须让
    上层将本次运行标记为失败，不能把本地排序的候选邮件伪装成分析结论。
    """
    if detail:
        _logger.warning("ask answer fallback: detail=%s", str(detail)[:300])
    is_chinese = _uses_chinese(plan.user_request)
    summary = (
        "AI 分析暂时不可用，请稍后重试。"
        if is_chinese
        else "AI analysis is temporarily unavailable. Please try again."
    )
    return {
        "title": "",
        "summary": summary,
        "sections": [],
        "analysis_error": True,
        "fallback_used": False,
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
    # 候选上限随用户「要几封」动态调整（与 item_limit 对齐，默认 3，最高 8）。
    context_limit = resolve_context_candidate_limit(plan.user_request)
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
    selected = [message for _index, message in ranked[:context_limit]]
    if needs_reply:
        human = [message for message in selected if not _is_automated_noise_entry(message)]
        # 至少给模型 1–2 封真人邮件；若全噪声则仍传原排序前几封以免空上下文。
        if human:
            return human[:context_limit]
    return selected


def _header_candidates(
    candidates: list[MessageLite],
    mailbox: str,
    mailbox_by_message_id: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """将候选投影为摘要级证据，供首轮优先级排序使用。"""
    return [
        {
            "mailbox": (mailbox_by_message_id or {}).get(message.message_id or "", mailbox),
            "message_id": message.message_id or "",
            "thread_id": message.thread_id or "",
            "from": message.from_addr or "",
            "to": message.to_addr or "",
            "subject": message.subject or "",
            "date": _fmt_ts(message.internal_date or ""),
            "snippet": message.snippet or "",
            "unread": getattr(message, "unread", False),
            "label_ids": getattr(message, "label_ids", None) or [],
            "body": "",
            "thread": [],
            "contact_context": "",
        }
        for message in candidates
    ]


def _requires_detail_context(user_request: str) -> bool:
    """只有用户明确要全文、线程细节或深入分析时才读取 Gmail 正文。"""
    text = str(user_request or "").casefold()
    return any(token in text for token in (
        "详细分析", "全文", "邮件正文", "完整内容", "线程详情", "深入分析",
        "full body", "full thread", "thread detail", "detailed analysis", "read the email",
    ))


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
    from ..mail_providers.gmail.adapter import (
        get_message_detail,
        get_thread_context,
        normalize_mailbox,
        refresh_thread_cache,
    )

    normalized = normalize_mailbox(mailbox)
    # 即使未来有其他调用方绕过编排层，也不能读取无限量邮件正文（硬顶 8）。
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
                # 详细分析优先刷新实时线程；401/超时等异常后继续使用已有缓存。
                await _asyncio.to_thread(refresh_thread_cache, source_mailbox, msg.thread_id or msg.message_id)
            except Exception:
                pass
            try:
                detail = await _asyncio.to_thread(get_message_detail, source_mailbox, msg.message_id)
                if detail:
                    # 与 Answer 渲染 body 上限对齐，避免多读无用正文。
                    entry["body"] = (getattr(detail, "body_text", "") or "")[:_ANSWER_BODY_LIMIT]
            except Exception:
                pass
            try:
                thread_ctx = await _asyncio.to_thread(
                    get_thread_context, source_mailbox, msg.thread_id or msg.message_id
                )
                if thread_ctx and thread_ctx.messages:
                    entry["thread"] = []
                    for tm in thread_ctx.messages[:_ANSWER_MAX_THREAD_MESSAGES]:
                        entry["thread"].append({
                            "from": getattr(tm, "from_addr", "") or "",
                            "to": getattr(tm, "to_addr", "") or "",
                            "subject": getattr(tm, "subject", "") or "",
                            "date": _fmt_ts(getattr(tm, "internal_date", "") or ""),
                            "body": (getattr(tm, "body_text", "") or "")[:_ANSWER_THREAD_BODY_LIMIT],
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


def _clip_field(value: Any, limit: int) -> str:
    """截断单字段，去掉换行以压证据体积。"""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _render_candidates_for_llm(
    enriched: list[dict[str, Any]],
    *,
    body_limit: int = 4000,
    thread_body_limit: int = 2000,
    max_thread_messages: int = 20,
) -> str:
    """将候选渲染为单行紧凑证据（显著降低 input tokens）。"""
    # 同一 mailbox 多数情况下重复；首行声明一次即可。
    mailboxes = sorted({
        str(e.get("mailbox") or "").strip()
        for e in enriched
        if str(e.get("mailbox") or "").strip()
    })
    lines: list[str] = []
    if mailboxes:
        lines.append("mb=" + ",".join(mailboxes))
    for i, e in enumerate(enriched, 1):
        unread = "1" if e.get("unread") else "0"
        row = (
            f"#{i}"
            f" mid={_clip_field(e.get('message_id'), 64)}"
            f" tid={_clip_field(e.get('thread_id'), 64)}"
            f" u={unread}"
            f" from={_clip_field(e.get('from'), 80)}"
            f" subj={_clip_field(e.get('subject'), _ANSWER_SUBJECT_LIMIT)}"
            f" date={_clip_field(e.get('date'), 24)}"
            f" snip={_clip_field(e.get('snippet'), _ANSWER_SNIPPET_LIMIT)}"
        )
        if e.get("body") and body_limit > 0:
            row += f" body={_clip_field(e.get('body'), body_limit)}"
        if e.get("thread") and thread_body_limit > 0 and max_thread_messages > 0:
            tm = (e.get("thread") or [])[:max_thread_messages]
            if tm:
                t0 = tm[0] if isinstance(tm[0], dict) else {}
                row += (
                    f" thr={_clip_field(t0.get('from'), 40)}|"
                    f"{_clip_field(t0.get('body'), thread_body_limit)}"
                )
        lines.append(row)
    return "\n".join(lines)


def _include_thread_for_answer(plan: AskPlan) -> bool:
    """仅 needs-reply / 草稿类目标附带短线程，其它请求只靠 headers/snippet。"""
    return (
        is_needs_reply_request(plan.user_request)
        or plan.goal in ("draft_replies", "check_reply_status", "summarize_threads")
    )


def _materialize_flat_answer_payload(payload: dict[str, Any], item_limit: int) -> dict[str, Any]:
    """将模型扁平 items 转为下游 sections，并去掉模型侧 mail_links（由 guard 重建）。"""
    limit = max(1, min(int(item_limit or _DEFAULT_ANSWER_ITEMS), _MAX_ANSWER_ITEMS))
    raw_items: list[Any] = []
    top_items = payload.get("items")
    if isinstance(top_items, list) and top_items:
        raw_items = top_items
    else:
        sections = payload.get("sections")
        if isinstance(sections, list):
            for section in sections:
                if not isinstance(section, dict):
                    continue
                section_items = section.get("items")
                if isinstance(section_items, list):
                    raw_items.extend(section_items)

    clean_items: list[dict[str, Any]] = []
    for entry in raw_items[:limit]:
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        item.pop("mail_links", None)
        clean_items.append(item)

    title = str(payload.get("title") or "").strip()
    summary = str(payload.get("summary") or "").strip()
    return {
        "title": title,
        "summary": summary,
        "sections": (
            [{"heading": title[:80] if title else "", "body": "", "items": clean_items}]
            if clean_items
            else []
        ),
    }


def _local_answer_from_evidence(
    plan: AskPlan,
    enriched: list[dict[str, Any]],
    *,
    item_limit: int,
) -> dict[str, Any]:
    """Sampling 截断/失败且已有候选时：用证据构造可展示结果，避免整次 Ask 报错。

    不编造邮件事实，仅使用候选 headers/snippet；标记 local_fallback 供诊断。
    """
    limit = max(1, min(int(item_limit or _DEFAULT_ANSWER_ITEMS), _MAX_ANSWER_ITEMS))
    zh = _uses_chinese(plan.user_request)
    rows: list[dict[str, Any]] = []
    for entry in enriched[:limit]:
        if not isinstance(entry, dict):
            continue
        rows.append({
            "subject": str(entry.get("subject") or ""),
            "from": str(entry.get("from") or ""),
            "context": _clip_field(entry.get("snippet") or entry.get("context") or "", 80)
            or ("候选邮件" if zh else "Priority candidate"),
            "suggestion": "请打开查看并决定是否处理。" if zh else "Open and decide next action.",
            "mailbox": str(entry.get("mailbox") or ""),
            "message_id": str(entry.get("message_id") or ""),
            "thread_id": str(entry.get("thread_id") or ""),
            "_local_fallback": True,
        })
    title = str(plan.title or "").strip() or ("优先邮件" if zh else "Priority emails")
    if rows:
        summary = (
            f"模型输出不完整，已按检索候选列出 {len(rows)} 封供你处理。"
            if zh
            else f"Model output was incomplete; listed {len(rows)} candidates from search."
        )
    else:
        summary = (
            "未找到可展示的候选邮件。"
            if zh
            else "No candidates available to display."
        )
    return {
        "title": title,
        "summary": summary,
        "sections": ([{"heading": title[:80], "body": "", "items": rows}] if rows else []),
        "local_fallback": True,
        "analysis_error": False,
        "fallback_used": False,
    }


def _backfill_items_to_target(
    payload: dict[str, Any],
    enriched: list[dict[str, Any]],
    *,
    target: int,
    user_request: str,
) -> dict[str, Any]:
    """用户显式要 N 条而模型少返回时，用证据列表补齐到 min(N, 证据数)。

    只补已有候选上的 subject/from/id；context/suggestion 用简短占位，不编造邮件事实。
    """
    limit = max(1, min(int(target or _DEFAULT_ANSWER_ITEMS), _MAX_ANSWER_ITEMS))
    sections = payload.get("sections") if isinstance(payload.get("sections"), list) else []
    items: list[dict[str, Any]] = []
    if sections and isinstance(sections[0], dict):
        raw = sections[0].get("items")
        if isinstance(raw, list):
            items = [dict(x) for x in raw if isinstance(x, dict)]

    used_ids = {
        str(item.get("message_id") or "").strip()
        for item in items
        if str(item.get("message_id") or "").strip()
    }
    used_threads = {
        (
            str(item.get("mailbox") or "").strip().lower(),
            str(item.get("thread_id") or "").strip(),
        )
        for item in items
        if str(item.get("thread_id") or "").strip()
    }

    zh = _uses_chinese(user_request)
    default_context = "进入优先列表的候选邮件。" if zh else "Included as a priority candidate."
    default_suggestion = "请打开查看并决定是否处理。" if zh else "Open and decide next action."

    for entry in enriched:
        if len(items) >= limit:
            break
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("message_id") or "").strip()
        tid = str(entry.get("thread_id") or "").strip()
        mailbox = str(entry.get("mailbox") or "").strip()
        if mid and mid in used_ids:
            continue
        thread_key = (mailbox.lower(), tid)
        if tid and thread_key in used_threads:
            continue
        if mid:
            used_ids.add(mid)
        if tid:
            used_threads.add(thread_key)
        items.append({
            "subject": str(entry.get("subject") or ""),
            "from": str(entry.get("from") or ""),
            "context": default_context,
            "suggestion": default_suggestion,
            "mailbox": mailbox,
            "message_id": mid,
            "thread_id": tid,
            "_backfilled": True,
        })

    title = str(payload.get("title") or "").strip()
    summary = str(payload.get("summary") or "").strip()
    if items and len(items) > 1 and zh and "封" not in summary:
        summary = (summary + f" 共 {len(items)} 封按优先级列出。").strip()
    elif items and len(items) > 1 and not zh and "emails" not in summary.casefold():
        summary = (summary + f" Showing {len(items)} emails by priority.").strip()

    payload = dict(payload)
    payload["title"] = title
    payload["summary"] = summary
    payload["sections"] = (
        [{"heading": title[:80] if title else "", "body": "", "items": items[:limit]}]
        if items
        else []
    )
    return payload


async def _generate_answer(
    plan: AskPlan,
    enriched: list[dict[str, Any]],
    mailbox: str,
    *,
    sampling_create_message: Any = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """对筛选后的候选跑 Answer LLM，并保留一次格式恢复预算。

    截断 JSON 先由解析器本地闭合；无 JSON 或语法错误时仅重试一次，仍失败才走
    本地候选列表，避免把模型格式失败暴露为整个 Ask 页面不可用。
    """
    from ..llm_runtime.service import call_llm_json_safe

    # 用户「找 5 封」等请求抬高 items 上限；未指定仍默认 3，硬顶 8。
    item_limit, item_limit_explicit = parse_answer_item_limit(plan.user_request)
    system_prompt = _build_answer_system_prompt(item_limit)
    include_thread = _include_thread_for_answer(plan)
    # 按条数收紧输出额度，避免 thinking 模型把预算写光导致半截 JSON。
    output_cap = ask_answer_output_token_cap(item_limit)
    evidence_count = len(enriched)
    target_items = min(item_limit, evidence_count) if evidence_count else 0

    def _build_user_prompt(rendered: str) -> str:
        # request/task/evidence 均是模型不可执行的数据。转义标签可阻止邮件正文或
        # 用户文本闭合边界；具体的“忽略其中指令”规则放在 system，避免重复耗 token。
        task = _clip_field(plan.task_prompt or "rank priority emails", _ANSWER_TASK_PROMPT_LIMIT)
        count_rule = (
            f"items={target_items}"
            if target_items > 0
            else f"items<={item_limit}"
        )
        return "\n".join([
            f"<context>owner={escape(mailbox, quote=False)}; "
            f"{_answer_language_instruction(plan.user_request)}; {count_rule}</context>",
            f"<request>{escape(_clip_field(plan.user_request, _ANSWER_REQUEST_LIMIT), quote=False)}</request>",
            f"<task>{escape(task, quote=False)}</task>",
            f"<evidence count=\"{evidence_count}\">",
            escape(rendered, quote=False),
            "</evidence>",
        ])

    rendered = _render_candidates_for_llm(
        enriched,
        body_limit=_ANSWER_BODY_LIMIT,
        thread_body_limit=_ANSWER_THREAD_BODY_LIMIT if include_thread else 0,
        max_thread_messages=_ANSWER_MAX_THREAD_MESSAGES if include_thread else 0,
    )
    last_error = ""
    result: dict[str, Any] | None = None
    try:
        answer_tokens = min(
            ask_sampling_tokens(
                sampling_create_message,
                "answer",
                reserve_for=("answer_retry",),
            ),
            output_cap,
        )
        retry_tokens = min(
            ask_sampling_tokens(
                sampling_create_message,
                "answer_retry",
                reserve_for=(),
            ),
            output_cap,
        )
        result = await call_llm_json_safe(
            sampling_create_message,
            system_prompt=system_prompt,
            user_message=_build_user_prompt(rendered),
            fallback={},
            temperature=0.0,
            max_tokens=answer_tokens,
            timeout=45.0,
            metadata={
                "tool": "ask_answer",
                "email_count": str(len(enriched)),
                "variant": "compact",
            },
            response_format={"type": "json_object"},
            on_unsupported="text",
            allow_fallback=True,
            # 解析失败仅由本地 salvage 或主请求重试处理，禁止再次 Sampling repair。
            allow_sampling_provider_fallback=False,
            max_attempts=2,
            retry_max_tokens=retry_tokens,
        )
        if result and result.get("fallback_used"):
            last_error = str(result.get("fallback_reason") or "Anna sampling failed")
            result = None
        elif not (result and isinstance(result.get("payload"), dict)):
            last_error = "empty payload"
            result = None
    except Exception as exc:
        last_error = str(exc)
        result = None
        if progress_callback:
            progress_callback("evaluate", {"variant": "compact", "reason": last_error[:200]})

    if result is None:
        # 有检索证据时优先本地列表，而不是整页报错。
        if enriched:
            _logger.warning("ask answer local evidence fallback: detail=%s", last_error[:300])
            local = _local_answer_from_evidence(plan, enriched, item_limit=item_limit)
            local["llm_meta"] = {"fallback_reason": last_error[:300], "local_fallback": True}
            return local
        return _answer_fallback(plan, last_error or "Anna sampling failed")

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    if not payload:
        if enriched:
            return _local_answer_from_evidence(plan, enriched, item_limit=item_limit)
        return _answer_fallback(plan, "Anna returned empty analysis")
    # 扁平 items → sections，供 guard / 前端消费。
    payload = _materialize_flat_answer_payload(payload, item_limit)
    # 用户明确要 N 条而模型少吐时，用证据补齐（不编造正文事实）。
    if item_limit_explicit and target_items > 1:
        payload = _backfill_items_to_target(
            payload,
            enriched,
            target=target_items,
            user_request=plan.user_request,
        )
    # 无 items 但有证据：本地补全，避免空结果页。
    sections = payload.get("sections") if isinstance(payload.get("sections"), list) else []
    has_items = any(
        isinstance(sec, dict) and isinstance(sec.get("items"), list) and sec.get("items")
        for sec in sections
    )
    if not has_items and enriched:
        payload = _local_answer_from_evidence(plan, enriched, item_limit=item_limit)
    # 英文请求若模型生成文案混入中文：优先 scrub 保留结构，避免整份答案被丢弃。
    if not _uses_chinese(plan.user_request) and _generated_copy_contains_chinese(payload):
        _logger.warning("ask answer scrubbed Chinese generated copy for English request")
        payload = _scrub_chinese_generated_copy(payload, plan)
        if _generated_copy_contains_chinese(payload) and enriched:
            payload = _local_answer_from_evidence(plan, enriched, item_limit=item_limit)
        elif _generated_copy_contains_chinese(payload):
            return _answer_fallback(
                plan,
                "Anna returned Chinese generated copy for an English request",
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
                # 单条 item 的 mail_links 与 Answer 条数硬顶一致（最高 8）。
                if len(safe_links) >= _MAX_ANSWER_ITEMS:
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
            len(narrative_items) >= _MAX_ANSWER_ITEMS
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
    todo_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Full Ask pipeline: plan → cache-only search → filter → context → answer → guard.

    Supports multiple mailboxes: plan once, search+filter concurrently per mailbox,
    merge candidates, then single answer pass.

    If `plan` is provided, skips the Planner LLM and uses the given plan directly
    (for re-running saved plans without re-planning).
    ``todo_ids`` 供本地 is:todo 与前端 Todos 标记对齐。
    """
    from mail_agent.local_query import build_local_query_from_plan
    from .planner import (
        normalize_actionable_browse_plan,
        normalize_user_facing_plan_copy,
        plan_ask_request,
        resolve_effective_timeframe,
    )
    from .search import execute_search

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
    cache_empty_mailboxes: list[str] = []
    # 指定联系人属于精确检索意图。若移除 from:/to: 后继续扫描，返回的只是
    # 同时间范围内的无关邮件，可能造成“找到结果”与“未找到该联系人”同时出现。
    has_person_constraint = any(
        str(person.get("name_hint") or "").strip()
        for person in plan.people
        if isinstance(person, dict)
    )
    _ = has_person_constraint  # cache-only 不再 broaden 打 Gmail；保留变量供后续策略使用
    # 本地 scan_query：Thinking 后小字与一键搜索共用
    local_scan_query = build_local_query_from_plan(plan)
    primary_queries = [{"query": local_scan_query, "purpose": "local_cache", "max_results": 100, "priority": "high"}]

    async def _search_one(mbox: str) -> tuple[str, list[MessageLite], list[MessageLite]]:
        """只扫本地缓存并过滤，返回 (mailbox, all_messages, candidates)。"""
        if progress_callback:
            progress_callback("search", {
                "mailbox": mbox,
                "query_total": 1,
                "scan_window_days": timeframe_days,
                "scan_query": local_scan_query,
            })

        search_meta: dict[str, str] = {}
        messages = await execute_search(
            mbox,
            primary_queries,
            progress_callback=progress_callback,
            max_messages=max_messages if max_messages is not None else 200,
            search_meta=search_meta,
            todo_ids=list(todo_ids or []),
            local_query=local_scan_query,
        )
        if search_meta.get("cache_empty") == "1":
            cache_empty_mailboxes.append(mbox)
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
            "plan_gmail_queries": primary_queries,
            "scan_query": local_scan_query,
            "scan_source": "cache",
            "planner_llm": plan.llm_meta,
            "messages_scanned": total_scanned,
            "candidates_found": 0,
            "sections": [],
        }
        if _uses_chinese(plan.user_request):
            return {
                **empty_base,
                "title": "",
                "summary": (
                    "本地收件箱缓存为空。请先在收件箱刷新同步，再重试。"
                    if cache_empty_mailboxes
                    else f"已在本地缓存中扫描 {len(mailboxes)} 个邮箱、{total_scanned} 封邮件，但没有找到符合你要求的内容。"
                ),
            }
        return {
            **empty_base,
            "title": "",
            "summary": (
                "Local inbox cache is empty. Refresh the inbox first, then try again."
                if cache_empty_mailboxes
                else f"Scanned {total_scanned} cached emails across {len(mailboxes)} mailbox(es) but none matched your request."
            ),
        }

    if progress_callback:
        progress_callback("filter_done", {"candidates": len(all_candidates), "scanned": total_scanned})

    # ── 4. Context ──────────────────────────────────────────────────
    context_candidates = _select_candidates_for_context(all_candidates, plan)
    if _requires_detail_context(plan.user_request):
        if progress_callback:
            progress_callback("read_context", {"current": 0, "total": len(context_candidates)})
        enriched = await _read_candidate_context(
            context_candidates, primary_mailbox,
            mailbox_by_message_id=candidate_mailboxes,
            sampling_create_message=None,
            progress_callback=progress_callback,
        )
        if progress_callback:
            progress_callback("read_context_done", {"total": len(enriched)})
    else:
        # 首轮排序只用 headers/snippet，减少 Gmail 往返和提示词尺寸。
        enriched = _header_candidates(context_candidates, primary_mailbox, candidate_mailboxes)

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
    result.setdefault("plan_gmail_queries", primary_queries)
    result.setdefault("scan_query", local_scan_query)
    result.setdefault("scan_source", "cache")
    result.setdefault("planner_llm", plan.llm_meta)
    result.setdefault("messages_scanned", total_scanned)
    result.setdefault("candidates_found", len(all_candidates))
    result["data_source"] = "local_cache"

    return result


# ── Ask item draft generation ─────────────────────────────────────────

def _ask_draft_system() -> str:
    """Ask 条目草稿 system（短、任务隔离）。"""
    from mail_agent.ai_turn.prompts import ask_item_draft_system_prompt
    return ask_item_draft_system_prompt()


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

    from mail_agent.ai_turn.prompts import ask_item_draft_system_prompt

    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=ask_item_draft_system_prompt(),
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
        timeout=60.0,
        metadata={"tool": "ask_draft", "message_id": message_id},
        response_format={"type": "json_object"},
        on_unsupported="text",
        allow_fallback=True,
        allow_sampling_provider_fallback=True,
    )

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    return {
        "subject": str(payload.get("subject") or ""),
        "body": str(payload.get("body") or ""),
        "fallback_used": result.get("fallback_used", False),
    }
