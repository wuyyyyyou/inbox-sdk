"""Ask pipeline planner: intent parsing + semantic expansion.

Replaces planning/custom.py's generate_custom_plan().
Key difference: LLM outputs structured search parameters + semantic expansion,
NOT raw Gmail query syntax. Code builds queries from these params.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

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
  "title": "Short task title (<=12 words, English)",
  "description": "One sentence summary",
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


# ── Main entry ────────────────────────────────────────────────────────

async def plan_ask_request(
    user_request: str,
    mailbox: str = "",
    *,
    sampling_create_message: Any = None,
) -> AskPlan:
    """Parse a natural-language email request into a structured AskPlan.

    Planner LLM outputs structured params + semantic expansion.
    Zero Gmail syntax — code builds queries from AskPlan fields.

    There is no fallback — if the LLM fails, the exception propagates.
    Without semantic expansion, the Ask pipeline cannot produce useful results.
    """
    from ..llm_runtime.service import call_llm_json_safe

    user_message = _PLANNER_USER_TEMPLATE.format(
        user_request=user_request,
        mailbox=mailbox or "unknown",
    )

    strict_anna_sampling = sampling_create_message is not None

    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=_PLANNER_SYSTEM_PROMPT,
        user_message=user_message,
        fallback={},
        temperature=0.1,
        max_tokens=8192,
        timeout=90.0,
        metadata={"tool": "ask_planner"},
        allow_fallback=False,
        allow_sampling_provider_fallback=True,
        max_attempts=3 if strict_anna_sampling else None,
    )

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
    return plan
