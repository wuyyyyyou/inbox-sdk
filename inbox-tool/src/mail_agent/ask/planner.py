"""Ask pipeline planner: intent parsing + semantic expansion.

Replaces planning/custom.py's generate_custom_plan().
Key difference: LLM outputs structured search parameters + semantic expansion,
NOT raw Gmail query syntax. Code builds queries from these params.
"""

from __future__ import annotations

import uuid
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from .sampling_budget import ask_sampling_tokens

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


# ── Types ────────────────────────────────────────────────────────────

@dataclass
class AskPlan:
    """Structured plan from Planner LLM. Zero Gmail syntax — code builds queries."""
    plan_id: str = ""
    user_request: str = ""
    title: str = ""
    description: str = ""

    people: list[dict[str, str]] = field(default_factory=list)
    # [{name_hint, role: sender|recipient|either}]

    topics: list[dict[str, Any]] = field(default_factory=list)
    # [{concept, search_terms: [str], relevance_hint: str}]

    timeframe: str = "30d"
    direction: str = "inbox"  # inbox | sent | all
    goal: str = "general_qa"
    task_prompt: str = ""
    gmail_flags: list[str] = field(default_factory=list)
    # e.g. ["is:unread", "has:attachment", "is:starred"]
    created_at: str = ""
    confidence: float = 0.8
    llm_meta: dict[str, Any] = field(default_factory=dict)


# ── Planner prompts ───────────────────────────────────────────────────

# Planner 的总输入目标低于 500 tokens。系统提示、运行时 JSON 后缀和结构标签
# 会占固定额度，因此只把剩余部分分给用户原始请求，避免长请求挤占整个 Sampling。
_PLANNER_INPUT_TOKEN_BUDGET = 480
_PLANNER_JSON_PROTOCOL_TOKEN_OVERHEAD = 55
# XML 转义会把少量符号扩展为实体；该预留覆盖最坏的短请求，保证转义后仍不超预算。
_PLANNER_XML_ESCAPE_TOKEN_MARGIN = 40


def _estimate_planner_input_tokens(text: str) -> int:
    """以 Router 同口径估算 token，仅用于本地截断而非计费。"""
    tokens = 0
    for chunk in re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]", text or ""):
        if re.fullmatch(r"[\u3400-\u9fff]", chunk):
            tokens += 1
        elif chunk.isascii() and (chunk[0].isalnum() or chunk[0] == "_"):
            tokens += max(1, (len(chunk) + 3) // 4)
        else:
            tokens += 1
    return tokens


def _truncate_planner_text(text: str, token_budget: int) -> str:
    """保留请求前缀并按估算额度截断，防止动态输入突破 Planner 预算。"""
    normalized = str(text or "").strip()
    if not normalized or token_budget <= 0:
        return ""
    if _estimate_planner_input_tokens(normalized) <= token_budget:
        return normalized
    low, high = 0, len(normalized)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = normalized[:middle].rstrip() + "..."
        if _estimate_planner_input_tokens(candidate) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return normalized[:low].rstrip() + "..."


def _build_planner_user_message(user_request: str, mailbox: str, system_prompt: str) -> str:
    """构造带数据边界且受总输入预算约束的 Planner user message。"""
    mailbox_data = escape(mailbox or "unknown", quote=False)
    prefix = f"<mailbox>{mailbox_data}</mailbox>\n<request>\n"
    suffix = "\n</request>"
    available = max(
        1,
        _PLANNER_INPUT_TOKEN_BUDGET
        - _PLANNER_JSON_PROTOCOL_TOKEN_OVERHEAD
        - _PLANNER_XML_ESCAPE_TOKEN_MARGIN
        - _estimate_planner_input_tokens(system_prompt)
        - _estimate_planner_input_tokens(prefix + suffix),
    )
    request = escape(_truncate_planner_text(user_request, available), quote=False)
    return prefix + request + suffix


def _parse_planner_confidence(value: Any) -> float:
    """兼容旧模型的 high/medium/low，并把数值置信度限制在合法范围。"""
    if isinstance(value, str):
        named = {"high": 0.9, "medium": 0.6, "low": 0.3}
        if value.strip().casefold() in named:
            return named[value.strip().casefold()]
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.8
    return min(1.0, max(0.0, confidence))


def resolve_effective_timeframe(
    user_request: str,
    scan_window_days: int | None,
    planned_timeframe: str = "30d",
) -> str:
    """根据用户明确时间或 Scan Plan 计算最终 Gmail 时间范围。

    Planner 的 timeframe 属于模型推断，不能在用户未指定时间时
    覆盖当前 Scan Plan；只有请求文本含明确相对时间时才允许覆盖默认范围。

    注意：单独的「最近 / recent / 这几天」属于模糊时间词，**不**视为明确窗口，
    必须 defer 到 Scan Plan（display_range_days / scan_window_days）。
    """
    request = str(user_request or "").strip().casefold()
    original = str(user_request or "")
    explicit_days: int | None = None

    # 优先匹配带数字的天/周/月表达；模糊「最近」不得抢占 Scan Plan。
    day_match = re.search(r"(?:last|past|recent)\s+(\d{1,3})\s+days?|最近\s*(\d{1,3})\s*天", request)
    week_match = re.search(r"(?:last|past|recent)\s+(\d{1,2})\s+weeks?|最近\s*(\d{1,2})\s*周", request)
    month_match = re.search(r"(?:last|past|recent)\s+(\d{1,2})\s+months?|最近\s*(\d{1,2})\s*个?月", request)
    if day_match:
        explicit_days = int(day_match.group(1) or day_match.group(2))
    elif week_match:
        explicit_days = int(week_match.group(1) or week_match.group(2)) * 7
    elif month_match:
        explicit_days = int(month_match.group(1) or month_match.group(2)) * 30
    elif any(token in request for token in ("today", "今天", "今日", "yesterday", "昨天")):
        explicit_days = 1
    # 「本周/上周/this week」是具体单位；不含裸「最近/recent」。
    elif any(token in request for token in ("this week", "last week", "past week", "本周", "这周", "上周")):
        explicit_days = 7
    elif any(token in request for token in ("this month", "本月", "这个月")):
        explicit_days = 30
    elif any(token in request for token in ("past quarter", "last quarter", "本季度", "上季度")):
        explicit_days = 90
    elif any(token in original for token in ("半年", "六个月")) or any(
        token in request for token in ("past 6 months", "last half year")
    ):
        explicit_days = 180
    elif any(token in request for token in ("this year", "past year", "last year", "今年", "过去一年", "去年")):
        explicit_days = 365

    if explicit_days is not None:
        return f"{max(1, min(explicit_days, 365))}d"

    try:
        configured_days = int(scan_window_days) if scan_window_days is not None else 0
    except (TypeError, ValueError):
        configured_days = 0
    if configured_days > 0:
        return f"{min(configured_days, 365)}d"

    # 缺少 Scan Plan 的旧调用保持原有计划时间，避免影响历史入口。
    matched = re.fullmatch(r"(\d{1,3})d", str(planned_timeframe or "").strip())
    return f"{min(max(int(matched.group(1)), 1), 365)}d" if matched else "30d"


# 「需要浏览/处理」类意图：Gmail 无法用关键词表达，必须 broad sweep + 回答阶段排序。
_ACTIONABLE_BROWSE_TOKENS = (
    "需要浏览",
    "需要处理",
    "需要看",
    "需要关注",
    "值得看",
    "待处理",
    "要处理",
    "有什么需要",
    "有哪些需要",
    "need to browse",
    "need to review",
    "need to handle",
    "need to check",
    "needs attention",
    "need attention",
    "to review",
    "to browse",
    "actionable",
)

# 「需要我回复」类意图：必须扫 inbox+sent（direction=all）才能判断谁最后发言。
_NEEDS_REPLY_TOKENS = (
    "needs my reply",
    "need my reply",
    "needs a reply",
    "need a reply",
    "awaiting my reply",
    "waiting for my reply",
    "waiting on my reply",
    "what needs my reply",
    "emails that need a reply",
    "mails that need a reply",
    "require my reply",
    "requires my reply",
    "等我回复",
    "待我回复",
    "需要我回复",
    "等我回",
    "谁在等我",
    "有没有等我回复",
)


def is_actionable_browse_request(user_request: str) -> bool:
    """判断是否为「找出需要浏览/处理的邮件」类抽象意图。"""
    text = str(user_request or "").strip()
    if not text:
        return False
    # 「需要我回复」走更精确的 reply 路径，不与 browse 混用。
    if is_needs_reply_request(text):
        return False
    lowered = text.casefold()
    return any(token in text or token in lowered for token in _ACTIONABLE_BROWSE_TOKENS)


def is_needs_reply_request(user_request: str) -> bool:
    """判断是否为「找出需要我回复的邮件」意图。"""
    text = str(user_request or "").strip()
    if not text:
        return False
    lowered = text.casefold()
    return any(token in text or token in lowered for token in _NEEDS_REPLY_TOKENS)


def normalize_actionable_browse_plan(plan: AskPlan) -> AskPlan:
    """将「需要浏览/处理」或「需要我回复」计划规范为 broad sweep。

    联系人约束保留；话题 search_terms 清空，由 Answer LLM 按 relevance_hint 排序。
    """
    if is_needs_reply_request(plan.user_request):
        return _normalize_needs_reply_plan(plan)
    if not is_actionable_browse_request(plan.user_request):
        return plan

    is_chinese = _uses_chinese_text(plan.user_request)
    relevance_hint = (
        "Prioritize emails that need the user to open, reply, decide, or act — "
        "especially unread messages, questions, requests, deadlines, and human senders. "
        "Deprioritize newsletters, promotions, automated notifications, and bulk marketing."
    )
    task_prompt = (
        "Identify emails the user should browse or handle next. "
        "Rank by urgency and need for attention. Group by priority. "
        "Skip newsletters and automated noise. Briefly explain why each item matters."
    )
    # 保留联系人约束；仅去掉无法用 Gmail 表达的「浏览/处理」关键词。
    plan.topics = [
        {
            "concept": "emails needing user attention",
            "search_terms": [],
            "relevance_hint": relevance_hint,
        }
    ]
    if plan.direction == "sent":
        plan.direction = "inbox"
    elif plan.direction not in ("inbox", "all"):
        plan.direction = "inbox"
    plan.goal = "find_emails"
    plan.task_prompt = task_prompt
    if is_chinese:
        if not str(plan.title or "").strip():
            plan.title = "需要浏览的邮件"
        if not str(plan.description or "").strip():
            plan.description = "扫描收件箱并标出需要你浏览或处理的邮件。"
    else:
        if not str(plan.title or "").strip():
            plan.title = "Emails to review"
        if not str(plan.description or "").strip():
            plan.description = "Scan the inbox and surface emails that need your attention."
    return plan


def _normalize_needs_reply_plan(plan: AskPlan) -> AskPlan:
    """「需要我回复」：direction=all + 空 search_terms，由回答阶段判断线程末条是否对方发出。"""
    is_chinese = _uses_chinese_text(plan.user_request)
    plan.topics = [
        {
            "concept": "emails awaiting user reply",
            "search_terms": [],
            "relevance_hint": (
                "Threads where the latest meaningful message is from the other party and they "
                "asked a question, requested action, or are waiting for the user. "
                "Skip newsletters, OTP/verification codes, and pure automated notifications."
            ),
        }
    ]
    plan.direction = "all"
    plan.goal = "draft_replies"
    plan.task_prompt = (
        "Identify threads that need the user's reply. Prefer human senders with questions or "
        "open requests. Explain briefly why each needs a reply. Offer a draft only when context "
        "is sufficient; otherwise set reply_gaps.needs_user_input."
    )
    if is_chinese:
        if not str(plan.title or "").strip():
            plan.title = "需要你回复的邮件"
        if not str(plan.description or "").strip():
            plan.description = "找出对方在等你回复的线程。"
    else:
        if not str(plan.title or "").strip():
            plan.title = "Emails that need your reply"
        if not str(plan.description or "").strip():
            plan.description = "Find threads where someone is waiting for your reply."
    return plan


def _uses_chinese_text(value: str) -> bool:
    """判断用户侧文案是否包含中文字符。"""
    return bool(re.search(r"[\u3400-\u9fff]", str(value or "")))


def _fallback_ask_plan(user_request: str, failure_reason: str) -> AskPlan:
    """在 Sampling 没有返回文本时生成可执行的保守搜索计划。"""
    normalized_request = str(user_request or "").strip()
    lowered_request = normalized_request.casefold()
    is_chinese = any("\u3400" <= char <= "\u9fff" for char in normalized_request)
    urgent_terms = ("urgent", "asap", "time-sensitive", "紧急", "尽快", "重要")
    is_urgent = any(term in lowered_request for term in urgent_terms)
    is_browse = is_actionable_browse_request(normalized_request)
    timeframe = "7d" if is_urgent else "30d"
    if is_chinese and is_urgent:
        title = "紧急邮件"
    elif is_chinese and is_browse:
        title = "需要浏览的邮件"
    elif is_chinese:
        title = "收件箱检查"
    elif is_urgent:
        title = "Urgent emails"
    elif is_browse:
        title = "Emails to review"
    else:
        title = "Inbox check"
    description = (
        "扫描收件箱并标出需要你浏览或处理的邮件。"
        if is_chinese and is_browse
        else "使用保守搜索计划检查近期收件箱。"
        if is_chinese
        else "Scan the inbox and surface emails that need your attention."
        if is_browse
        else "Checking recent inbox messages with a conservative fallback plan."
    )
    task_prompt = (
        "Identify time-sensitive, actionable, or explicitly requested emails. "
        "Use only the provided emails and group findings by priority."
        if is_urgent
        else "Identify emails the user should browse or handle next. "
        "Rank by urgency and need for attention. Group by priority. "
        "Skip newsletters and automated noise."
        if is_browse
        else "Identify actionable emails using only the provided emails and group findings by priority."
    )
    plan = AskPlan(
        plan_id=f"askplan_{uuid.uuid4().hex[:12]}",
        user_request=normalized_request,
        title=title,
        description=description,
        people=[],
        topics=[],
        timeframe=timeframe,
        direction="inbox",
        goal="find_emails" if is_browse else "general_qa",
        task_prompt=task_prompt,
        gmail_flags=[],
        created_at=datetime.now(BEIJING_TZ).isoformat(),
        confidence=0.3,
        llm_meta={
            "provider": "anna-sampling",
            "model": None,
            "usage": None,
            "fallback_used": True,
            "fallback_reason": str(failure_reason)[:240],
        },
    )
    return normalize_actionable_browse_plan(plan)


def normalize_user_facing_plan_copy(plan: AskPlan) -> AskPlan:
    """按用户请求语言校验 Planner 会展示在侧栏中的文案。"""
    # Planner 的 title/description 会直接作为搜索卡片标题和摘要展示。英文请求若被模型误答为中文，
    # 不仅标题错误，还会触发前端将整张卡片的状态文案切换成中文，因此在协议边界统一降级为确定性英文文案。
    if _uses_chinese_text(plan.user_request):
        return plan
    fallback = _fallback_ask_plan(plan.user_request, "invalid user-facing language")
    if _uses_chinese_text(plan.title):
        plan.title = fallback.title
    if _uses_chinese_text(plan.description):
        plan.description = fallback.description
    return plan


# ── Main entry ────────────────────────────────────────────────────────

async def plan_ask_request(
    user_request: str,
    mailbox: str = "",
    *,
    sampling_create_message: Any = None,
) -> AskPlan:
    """Parse a natural-language email request into a structured AskPlan.

    Planner LLM outputs structured params + semantic expansion.
    Zero Gmail syntax — code builds queries from AskPlan fields. Sampling
    返回空文本或失败时，改用保守的确定性计划继续搜索，避免整个 Ask run 失败。
    """
    from ..llm_runtime.service import call_llm_json_safe

    from mail_agent.ai_turn.prompts import ask_planner_system_prompt

    system_prompt = ask_planner_system_prompt()
    user_message = _build_planner_user_message(user_request, mailbox, system_prompt)

    strict_anna_sampling = sampling_create_message is not None

    try:
        result = await call_llm_json_safe(
            sampling_create_message,
            system_prompt=system_prompt,
            user_message=user_message,
            fallback={},
            temperature=0.1,
            # Planner 只占本次授权的一小部分，并为回答和主请求重试保留比例预算。
            max_tokens=ask_sampling_tokens(
                sampling_create_message,
                "planner",
                reserve_for=("answer", "answer_retry"),
            ),
            timeout=30.0,
            metadata={"tool": "ask_planner"},
            response_format={"type": "json_object"},
            on_unsupported="text",
            allow_fallback=False,
            allow_sampling_provider_fallback=True,
            max_attempts=1 if strict_anna_sampling else None,
        )
    except Exception as exc:
        return _fallback_ask_plan(user_request, str(exc))

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    if not payload or not isinstance(payload, dict):
        raise RuntimeError("Planner LLM returned empty payload")

    plan_id = f"askplan_{uuid.uuid4().hex[:12]}"
    now = datetime.now(BEIJING_TZ).isoformat()

    # Parse people
    people: list[dict[str, str]] = []
    raw_people = payload.get("people")
    if isinstance(raw_people, list):
        for p in raw_people:
            if isinstance(p, dict):
                people.append({
                    "name_hint": str(p.get("name_hint") or ""),
                    "role": str(p.get("role") or "either"),
                })

    # Parse topics
    topics: list[dict[str, Any]] = []
    raw_topics = payload.get("topics")
    if isinstance(raw_topics, list):
        for t in raw_topics:
            if isinstance(t, dict):
                search_terms = t.get("search_terms")
                if isinstance(search_terms, list):
                    search_terms = [str(s) for s in search_terms if s]
                else:
                    search_terms = []
                topics.append({
                    "concept": str(t.get("concept") or ""),
                    "search_terms": search_terms,
                    "relevance_hint": str(t.get("relevance_hint") or ""),
                })

    # Parse gmail_flags
    raw_flags = payload.get("gmail_flags")
    if isinstance(raw_flags, list):
        gmail_flags = [str(f) for f in raw_flags if isinstance(f, str) and f.strip()]
    else:
        gmail_flags = []

    plan = AskPlan(
        plan_id=plan_id,
        user_request=user_request,
        title=str(payload.get("title") or ""),
        description=str(payload.get("description") or ""),
        people=people,
        topics=topics,
        timeframe=str(payload.get("timeframe") or "30d"),
        direction=str(payload.get("direction") or "inbox"),
        goal=str(payload.get("goal") or "general_qa"),
        task_prompt=str(payload.get("task_prompt") or ""),
        gmail_flags=gmail_flags,
        created_at=now,
        confidence=_parse_planner_confidence(payload.get("confidence")),
        llm_meta={
            "provider": result.get("provider"),
            "model": result.get("model"),
            "usage": result.get("usage"),
            "fallback_used": result.get("fallback_used", False),
            "fallback_reason": result.get("fallback_reason", ""),
        },
    )
    # 「需要浏览/处理」在协议边界统一为 broad sweep，防止模型塞入无效关键词。
    plan = normalize_actionable_browse_plan(plan)
    return normalize_user_facing_plan_copy(plan)
