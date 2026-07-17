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
from typing import Any

from .sampling_budget import ASK_PLANNER_MAX_TOKENS

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

_PLANNER_SYSTEM_PROMPT = """You are Anna's Ask pipeline planner. Given a natural-language email assistant request, extract structured search parameters. You do NOT write Gmail search syntax — code will build queries from your parameters.

Your most important job is SEMANTIC EXPANSION: users speak in abstract concepts ("job candidates", "cooperation opportunities", "security issues"), but emails contain concrete words ("resume", "partnership", "password reset"). Translate concepts into searchable terms.

Output a single valid JSON object. The very first character you write MUST be `{`. Do NOT write any text before or after the JSON — no markdown fences, no explanation, no commentary.

## Output format
{
  "title": "Short user-facing task title (<=12 words, in the same language as the user's request)",
  "description": "One-sentence user-facing summary in the same language as the user's request",
  "people": [
    {"name_hint": "The name exactly as the user mentioned it", "role": "sender|recipient|either"}
  ],
  "topics": [
    {
      "concept": "What the user means (e.g. 'job candidates')",
      "search_terms": ["concrete", "searchable", "terms", "that", "appear", "in", "emails"],
      "relevance_hint": "1-2 sentences describing how to judge if an email matches this concept — sender type, email purpose, content patterns"
    }
  ],
  "timeframe": "1d|3d|7d|14d|30d|90d|180d|365d",
  "direction": "inbox|sent|all",
  "goal": "count_items|summarize_threads|find_emails|check_reply_status|draft_replies|general_qa",
  "task_prompt": "Analysis instructions for the Answer LLM. What to look for, how to group findings, what to surface. Do NOT include JSON output format instructions.",
  "gmail_flags": ["is:unread", "has:attachment"],
  "confidence": 0.85
}

## topics — the core semantic expansion job

## User-facing language

- title and description are shown to the user. Write both in the same language as the user's request. In particular, use Simplified Chinese when the request contains Chinese.
- Keep concept, relevance_hint, and task_prompt in English because they are internal planning fields.

Users say "job candidates" but emails say "resume", "CV", "interview". Users say "cooperation" but emails say "partnership", "proposal", "demo". Your job: translate the user's abstract concept into concrete searchable terms AND a relevance hint for filtering.

- concept: what the user means, in English (readable label for debugging)
- search_terms: 5-15 concrete words/phrases that actually appear in matching emails. Cover both Chinese and English. Expand abbreviations. Cover synonym variants. Do NOT put the concept word itself unless it literally appears in emails (e.g. "invoice" is fine, "job candidates" is not — use "resume","CV","interview","求职","简历"). You may use Gmail operators inside search_terms for precision: subject:"Application for" targets the subject line; subject:invoice matches subjects containing "invoice".
- relevance_hint: 1-2 English sentences describing HOW to recognize a match. Don't repeat search_terms — describe sender characteristics, email purpose, content patterns. This will guide a downstream relevance filter LLM.

Examples of good semantic expansion:

User: "找找候选人的未回复邮件"
→ concept: "job candidates awaiting reply"
→ search_terms: ["resume","CV","application","interview","cover letter","position","hiring","求职","简历","面试","应聘","position"]
→ relevance_hint: "unknown senders (not colleagues) discussing job applications, interviews, or position inquiries — especially where the sender appears to be waiting for a response"
→ direction: "all"
→ timeframe: "7d" (need both inbox and sent to determine who replied last)
→ goal: "draft_replies"

User: "有没有合作相关的邮件"
→ concept: "partnership opportunities"
→ search_terms: ["partnership","collaboration","sponsor","proposal","demo","cooperation","合作","提案","partner"]
→ relevance_hint: "external senders proposing collaboration, partnership, sponsorship, or product demos — not internal discussion"
→ direction: "inbox"
→ timeframe: "7d"

User: "帮我看看最近有什么需要处理的" / "找找最近需要浏览的邮件"
→ concept: "emails needing user attention"
→ search_terms: []  ← abstract “to browse / to handle” cannot be Gmail keywords; leave empty for broad sweep
→ relevance_hint: "emails that need the user to open, reply, decide, or act — prefer unread/human requests; deprioritize newsletters and automated notifications"
→ direction: "inbox"
→ timeframe: "30d"  ← bare “最近/recent” is NOT an explicit window; code uses the user’s current Scan Plan
→ goal: "find_emails"

User: "找找 Sarah 最近发的邮件"
→ concept: "emails from Sarah"
→ search_terms: []  ← person-based search; the system resolves "Sarah" to email via contact memory
→ relevance_hint: "emails sent by Sarah to the user"
→ direction: "inbox"
→ timeframe: "7d"
→ people: [{"name_hint": "Sarah", "role": "sender"}]

User: "等我回复的邮件"
→ concept: "emails awaiting my reply"
→ search_terms: []  ← This concept cannot be expressed in Gmail keywords! Leave empty, code will do a broad sweep.
→ relevance_hint: "sender explicitly asked a question, sent a proposal, or followed up — and the latest message in the thread is from them, not me"
→ direction: "all" (need both sides to determine who sent last)
→ timeframe: "7d"
→ goal: "draft_replies"

User: "帮我看看未读邮件"
→ concept: "unread inbox"
→ search_terms: []
→ relevance_hint: "all unread emails in the inbox"
→ direction: "inbox"
→ timeframe: "7d"
→ gmail_flags: ["is:unread"]

User: "我发了邮件但谁还没回复我"
→ concept: "sent mail awaiting reply"
→ search_terms: []
→ relevance_hint: "threads where the user sent a message and the other person hasn't replied — latest message is from the user"
→ direction: "all" (need sent mail to find threads the user started, plus inbox to check if there's a reply)
→ timeframe: "14d"
→ goal: "draft_replies"

User: "总结一下我和Alice最近的沟通"
→ concept: "conversation summary with Alice"
→ search_terms: []
→ relevance_hint: "all emails exchanged between the user and Alice — group by thread to understand the discussion"
→ direction: "all" (complete back-and-forth, no direction filter)
→ people: [{"name_hint": "Alice", "role": "either"}]
→ timeframe: "30d"
→ goal: "summarize_threads"

User: "本月有多少发票"
→ concept: "invoice count this month"
→ search_terms: ["invoice","receipt","payment","billing","charged","发票","账单","付款"]
→ relevance_hint: "billing-related emails: invoices, receipts, payment confirmations, subscription charges"
→ timeframe: "30d"
→ goal: "count_items"

## people

Extract person names exactly as the user mentions them. Use name_hint (NOT email — the system resolves names to email addresses via contact memory). DO NOT guess email domains.

If the user mentions multiple people, list each as a separate entry.
If no specific person is mentioned, return empty array [].

## timeframe

Choose the most appropriate window only when the user gives an **explicit** time unit or number.
Bare “recent / 最近 / 这几天” is NOT explicit — keep timeframe "30d" as a placeholder; runtime will replace it with the user’s current Scan Plan (display_range_days).
"today" -> 1d
"yesterday" / "last 2 days" -> 2d
"this week" / "last week" / "past week" / "本周" / "上周" -> 7d
"last N days" / "最近 N 天" -> Nd (use the number the user wrote)
"two weeks" / "past fortnight" -> 14d
"this month" / "本月" -> 30d
"last three months" / "past quarter" -> 90d
"past 6 months" / "last half year" -> 180d
"this year" / "past year" -> 365d
"all time" / "everything" -> 365d
Apply the same logic for non-English requests when they name a concrete unit or number.
Apply the same logic for non-English requests — map common time words in the user's language to the appropriate duration.

## direction

Choose based on what the user needs to SEE:

inbox — only what others sent to the user.
  Use for: "check my inbox", "what needs attention", "find emails from X", "what arrived recently"

sent — only what the user sent themselves.
  Use for: "what did I send", "my proposals", "my outreach", "review my sent mail"

all — NO direction filter. Gmail searches BOTH inbox and sent.
  REQUIRED for: reply status checks, "did I reply?", "needs reply?", conversation summaries,
  catch-up, "waiting for reply", "who hasn't replied to me".
  WHY: the execution LLM needs BOTH sides to determine who sent the latest message.
  If you exclude sent mail, the LLM can't tell whether the user already replied.

In detail:

- User asks about reply status / "did I reply" / "needs reply" / "have I responded" → **NO direction filter** (all).
  The execution LLM needs BOTH sides to know who sent the latest message in each thread.

- User asks "who hasn't replied to me" / "what am I waiting for" → **include sent mail** (all or in:sent).
  The execution LLM needs to see threads where the user was the last sender.

- User asks to summarize a conversation / catch up on a discussion → **NO direction filter** (all).
  The execution LLM needs the complete back-and-forth.

- Default (unclear intent): use **inbox** — show what others sent to the user.

## goal

count_items — user asks "how many", "count"
summarize_threads — summarizing conversations, catching up on discussions
find_emails — looking for specific emails
check_reply_status — asking about reply/waiting status WITHOUT needing drafts (e.g. "did I reply to X?", "has anyone replied to my proposal?")
draft_replies — asking for reply drafts, OR looking for emails that need a reply ("find emails I haven't replied to", "what needs my response", "谁还没回复"). When the user wants to FIND emails needing replies, the natural next step is drafting one — use draft_replies so the answer includes ready-to-send drafts.
general_qa — everything else

## task_prompt

Write as if instructing a smart assistant. Tell it:
1. What to look for in the emails
2. How to group and organize findings into sections
3. What kind of items to surface (people, threads, action items, dates)
4. For thread-aware tasks: check who sent the LATEST message — if from mailbox owner → already handled; if from someone else → needs attention
5. When goal is draft_replies: instruct it to draft a reply for EVERY item that needs one. If key information is missing, use reply_gaps to ask the user instead of guessing.

Do NOT include JSON output format in task_prompt.

## Notes
- topics and people may both be empty if the request is a broad check (e.g. "what's new in my inbox")
- For requests that don't need topic search (general inbox check), set topics to empty []
- If the user's request is broad/ambiguous, default to direction=inbox, timeframe=30d, goal=general_qa
"""

_PLANNER_USER_TEMPLATE = """Mailbox owner: {mailbox}
User request: {user_request}"""


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

    user_message = _PLANNER_USER_TEMPLATE.format(
        user_request=user_request,
        mailbox=mailbox or "unknown",
    )

    strict_anna_sampling = sampling_create_message is not None

    try:
        result = await call_llm_json_safe(
            sampling_create_message,
            system_prompt=_PLANNER_SYSTEM_PROMPT,
            user_message=user_message,
            fallback={},
            temperature=0.1,
            max_tokens=ASK_PLANNER_MAX_TOKENS,
            timeout=90.0,
            metadata={"tool": "ask_planner"},
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
        confidence=float(payload.get("confidence") or 0.8),
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
