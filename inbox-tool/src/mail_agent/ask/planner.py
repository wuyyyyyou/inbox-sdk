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

User: "帮我看看最近有什么需要处理的"
→ concept: "general inbox check"
→ search_terms: []
→ relevance_hint: "emails in the inbox that need user attention — not newsletters or automated notifications"
→ direction: "inbox"
→ timeframe: "7d"
→ goal: "general_qa"

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

Choose the most appropriate window. Default is "30d" unless the user specifies otherwise.
"today" -> 1d
"yesterday" / "last 2 days" -> 2d
"recent" / "this week" / "last few days" / "past week" -> 7d
"two weeks" / "past fortnight" -> 14d
"this month" -> 30d
"last three months" / "past quarter" -> 90d
"past 6 months" / "last half year" -> 180d
"this year" / "past year" -> 365d
"all time" / "everything" -> 365d
Apply the same logic for non-English requests - map common time words in the user's language to the appropriate duration. → 1d
"yesterday" / "last 2 days" → 2d
"recent" / "this week" / "last few days" → 7d
"this month" → 30d
"last three months" → 90d
"this year" → 365d
"all time" / "everything" → 365d
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
    """
    request = str(user_request or "").strip().casefold()
    explicit_days: int | None = None

    # 优先匹配带数字的天/周/月表达，避免“最近”一词抢占更精确范围。
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
    elif any(token in request for token in ("this week", "last week", "past week", "本周", "这周", "上周", "recent", "最近")):
        explicit_days = 7
    elif any(token in request for token in ("this month", "本月", "这个月")):
        explicit_days = 30
    elif any(token in request for token in ("past quarter", "last quarter", "本季度", "上季度")):
        explicit_days = 90
    elif any(token in request for token in ("past 6 months", "last half year", "半年", "六个月")):
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
    timeframe = "7d" if is_urgent else "30d"
    title = "紧急邮件" if is_chinese and is_urgent else "收件箱检查" if is_chinese else "Urgent emails" if is_urgent else "Inbox check"
    description = (
        "使用保守搜索计划检查近期收件箱。"
        if is_chinese
        else "Checking recent inbox messages with a conservative fallback plan."
    )
    task_prompt = (
        "Identify time-sensitive, actionable, or explicitly requested emails. "
        "Use only the provided emails and group findings by priority."
        if is_urgent
        else "Identify actionable emails using only the provided emails and group findings by priority."
    )
    return AskPlan(
        plan_id=f"askplan_{uuid.uuid4().hex[:12]}",
        user_request=normalized_request,
        title=title,
        description=description,
        people=[],
        topics=[],
        timeframe=timeframe,
        direction="inbox",
        goal="general_qa",
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
    return normalize_user_facing_plan_copy(plan)
