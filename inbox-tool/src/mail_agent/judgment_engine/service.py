"""Item evaluator (§13–§14) — build prompts, call LLM, parse JSON output."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from datetime import datetime, timezone, timedelta
from ..domain.types import (
    BaseJudgment,
    CandidateContext,
    FinalDecision,
    JudgmentResult,
    MailStrategy,
    MailTaskPlan,
    MailboxProfile,
    StrategyMode,
)
from ..storage.types import SnoozePrefs

_BEIJING_TZ = timezone(timedelta(hours=8))


def _fmt_ts(epoch_ms: str) -> str:
    """Convert Gmail internalDate (epoch ms string) to human-readable Beijing time."""
    if not epoch_ms:
        return ""
    try:
        dt = datetime.fromtimestamp(int(epoch_ms) / 1000.0, tz=_BEIJING_TZ)
        return dt.strftime("%b %d, %Y %H:%M")
    except (ValueError, TypeError, OSError):
        return epoch_ms


def _extract_email(from_addr: str) -> str:
    """从 'Name <email>' 格式中提取纯 email 地址。"""
    match = re.search(r"<([^>]+)>", from_addr)
    return match.group(1).strip().lower() if match else from_addr.strip().lower()


# ── Render context for prompt ─────────────────────────────────────

def _render_context_for_prompt(ctx: CandidateContext, owner_email: str = "") -> str:
    """Render candidate context as a string for the LLM prompt."""
    c = ctx.candidate
    parts: list[str] = []

    # Phase 1 classification reason — prominently displayed
    phase1_llm_reason = c.evidence.get("llm_reason", "")
    phase1_item_type = c.evidence.get("llm_item_type", "")
    if phase1_item_type or phase1_llm_reason:
        parts.append(f"Phase1 classification: {phase1_item_type} — {phase1_llm_reason}")

    parts.append(f"Candidate ID: {c.candidate_id}")
    parts.append(f"Kind (rule hint): {c.kind}")
    parts.append(f"Priority hint: {c.priority_hint}")
    parts.append(f"Confidence: {c.confidence}")
    parts.append(f"From: {c.evidence.get('from', '')}")
    parts.append(f"Subject: {c.evidence.get('subject', '')}")
    parts.append(f"Snippet: {c.evidence.get('snippet', '')}")
    parts.append(f"Date: {_fmt_ts(str(c.evidence.get('date', '')))}")
    parts.append(f"Labels: {json.dumps(c.evidence.get('labels', []), ensure_ascii=False)}")
    parts.append(f"Matched signals: {json.dumps(c.evidence.get('matched_signals', []), ensure_ascii=False)}")

    body_and_thread = _render_body_and_thread(ctx, owner_email)
    if body_and_thread:
        parts.append(body_and_thread)
    if getattr(ctx, "contact_context", ""):
        parts.append(f"\n--- Related Contact Memory ---\n{ctx.contact_context}")

    return "\n".join(parts)


def _render_body_and_thread(ctx: CandidateContext, owner_email: str = "") -> str:
    """只渲染消息体和线程上下文，不含候选元信息。

    对于 thread_context：
    - 如果用户回复过：从最后一条用户回复截断，只展示之后的连续收信
    - 如果从未回复：展示全部线程消息
    """
    parts: list[str] = []
    if ctx.type == "message_detail" and ctx.message:
        parts.append(f"\n--- Full Message Body ---")
        parts.append(f"Body text: {ctx.message.body_text[:1200]}")
    if ctx.type == "thread_context" and ctx.thread:
        msgs = list(ctx.thread.messages)
        owner_lower = (owner_email or "").strip().lower()

        # 从尾部向前找用户最后回复的位置
        cut = -1
        for i in range(len(msgs) - 1, -1, -1):
            if _extract_email(msgs[i].from_addr) == owner_lower:
                cut = i
                break

        if cut >= 0:
            visible = msgs[cut:]
            header = (f"\n--- Thread Context ({len(visible)} of {len(msgs)} messages, "
                      f"since your last reply) ---")
            footer = ("\n↑ Messages marked UNREPLIED need your attention. "
                      "Your last reply is provided for context.")
        else:
            visible = msgs
            header = (f"\n--- Thread Context ({len(visible)} messages, "
                      f"no reply from you yet) ---")
            footer = "\n↑ Focus on the most recent messages above."

        parts.append(header)
        for i, msg in enumerate(visible):
            if cut >= 0 and i == 0:
                label = " (your last reply)"
            elif cut >= 0 and i == len(visible) - 1:
                label = " ← LATEST UNREPLIED"
            elif cut >= 0:
                label = " ← UNREPLIED"
            elif i == len(visible) - 1:
                label = " ← LATEST"
            else:
                label = ""
            parts.append(f"\nMessage {i+1}{label}:")
            parts.append(f"  From: {msg.from_addr}")
            parts.append(f"  Date: {_fmt_ts(msg.internal_date)}")
            parts.append(f"  Subject: {msg.subject}")
            parts.append(f"  Body: {msg.body_text[:400]}")
        parts.append(footer)
    return "\n".join(parts)


def _render_snooze_prefs_context(prefs: SnoozePrefs | None) -> str:
    """Render snooze preferences as prompt context for deprioritization."""
    if not prefs:
        return ""
    has_senders = bool(prefs.senders)
    has_threads = bool(prefs.threads)
    if not has_senders and not has_threads:
        return ""

    parts = [
        "",
        "## User Attention Preferences",
        "The user has indicated they want to deprioritize attention from these sources.",
        "These are NOT blocks — emails from these sources CAN still surface if they genuinely require user action.",
        "However, apply a stricter standard: only surface items where the user MUST personally act",
        "(reply, approve, review a security issue). Routine updates, newsletters, automated",
        "notifications, and informational emails from these sources should be classified as low/ignore.",
    ]
    if has_senders:
        parts.append(f"Deprioritized senders: {', '.join(prefs.senders)}")
    if has_threads:
        parts.append(f"Deprioritized threads: {', '.join(prefs.threads)}")
    return "\n".join(parts)


# ── Build judgment schemas ────────────────────────────────────────

def _base_schema() -> dict[str, Any]:
    """Reduced base schema — removed item_type and should_surface (derived from final_decision)."""
    return {
        "requires_user_action": "boolean",
        "can_agent_prepare": "boolean",
        "can_agent_handle_after_approval": "boolean",
        "risk_level": "one of: none | low | medium | high | critical",
        "other_party_waiting": "boolean",
        "user_is_blocking": "boolean",
        "reason": "string (short English, explain WHY this judgment)",
    }


def _secretary_schema() -> dict[str, Any]:
    return {
        "urgency": "one of: today | this_week | later | none",
        "who_should_act": "one of: user | agent_after_approval | no_action",
    }


def _creator_schema() -> dict[str, Any]:
    return {
        "relationship_status": "one of: continue | needs_follow_up | waiting_for_them | waiting_for_us | paused | rejected | not_worth_pursuing | unknown",
        "opportunity_quality": "one of: high | medium | low | unknown",
        "current_blocker": "string (short English)",
        "suggested_next_step": "one of: send_short_update | share_build_or_demo | ask_for_requirements | send_pricing_or_terms | wait | close_or_archive | manual_review",
        "should_save_to_pipeline": "boolean",
    }


def _security_schema() -> dict[str, Any]:
    return {
        "risk_category": "one of: login_alert | verification_code | password_or_recovery | payment_failed | invoice_or_receipt | subscription_change | quota_or_storage | account_restriction | permission_or_access | normal_account_notice | unknown",
        "severity": "one of: critical | warning | info | no_issue",
        "affected_service": "string",
        "affected_account": "string",
        "amount": "string",
        "deadline": "string",
        "user_confirmation_needed": "boolean",
        "recommended_handling": "one of: confirm_login | check_payment | review_invoice | increase_quota_or_clean_storage | review_account_access | record_only | ignore",
    }


def _final_decision_schema() -> dict[str, Any]:
    return {
        "user_action": "one of: reply | review",
        "action_reason": "one of: question_asked | waiting_for_you | unsent_draft | courtesy_due | upcoming_event | deal_or_pipeline | security_or_billing | receipt_or_notice | cleanup",
        "display_bucket": "string (short English label)",
        "priority": "one of: critical | high | medium | low | ignore",
        "should_show_in_main_result": "boolean",
        "should_show_in_lower_priority": "boolean",
        "recommended_actions": "array of {action_type, risk_level, requires_approval, payload, reason}",
        "user_facing_summary": "string (short English, ≤15 words)",
        "user_facing_reason": "string (English, ≤80 chars, explain to user WHY this matters)",
        "user_facing_recommendation": "string (English, ≤80 chars, tell user WHAT to do)",
        "reply_gaps": "{needs_user_input: bool, summary: string, questions: [{id, question, hint, required}]}",
    }


# ── Few-shot rendering ────────────────────────────────────────────

def _render_few_shot_examples(examples: list[dict[str, str]]) -> str:
    """Render few-shot examples into prompt text."""
    if not examples:
        return ""
    lines = ["## Few-Shot Examples"]
    for i, ex in enumerate(examples, start=1):
        lines.append(f"\n### Example {i}: {ex.get('input_summary', '')}")
        lines.append(f"Email: {ex.get('input_detail', '')}")
        lines.append(f"Output: {ex.get('output', '')}")
    return "\n".join(lines)


# ── Build Judgment Prompt ─────────────────────────────────────────

def build_judgment_prompt(
    task_plan: MailTaskPlan,
    strategy: MailStrategy,
    mailbox_profile: MailboxProfile,
    candidate_context: CandidateContext,
    snooze_prefs: SnoozePrefs | None = None,
) -> str:
    """Build the LLM judgment prompt with few-shot examples and structured rubric."""
    mode_schema = {}
    if strategy.id == "default_secretary":
        mode_schema = _secretary_schema()
    elif strategy.id == "creator_opportunity":
        mode_schema = _creator_schema()
    elif strategy.id == "security_billing":
        mode_schema = _security_schema()

    few_shot = _render_few_shot_examples(strategy.judgment_policy.few_shot_examples)

    user_facing_fields = """
  "final_decision": {
    "user_action": "reply|review",
    "display_bucket": "short English category label",
    "priority": "critical|high|medium|low|ignore",
    "should_show_in_main_result": true/false,
    "should_show_in_lower_priority": true/false,
    "recommended_actions": [{"action_type":"...", "risk_level":"...", "requires_approval":true/false, "payload":{}, "reason":"..."}],
    "user_facing_summary": "English ≤12 words. Core object (person/project/company/risk) + what needs attention. This is card Line 1.",
    "user_facing_reason": "English ≤30 words. WHAT happened: who did what, when, and current status. Verifiable facts only. Card Line 2.",
    "user_facing_recommendation": "English ≤15 words. Specific next action. Be concrete, not generic. Card Line 3.",
    "needs": "English ≤4 words. Concise label for what the user needs to do or decide. Examples: 'Kate's reply', 'Timing decision', 'Receipt acknowledgement', 'Manual review'.",
    "latest_action": "English ≤8 words. What recently happened — the latest action by a person or service. Examples: 'sent collaboration briefs', 'followed up about partnership'.",
    "latest_actor": "English name. The person or service who performed the latest action.",
    "reply_gaps": {"needs_user_input": true, "summary": "one-sentence summary", "questions": [{"id":"q1", "question":"What do you want to reply?", "hint":"short hint", "required": true}]}
  }"""

    prompt = f"""You are Anna, an executive email assistant. Evaluate the email against the strategy below. Output ONLY JSON — no markdown, no explanation.

## Strategy
{strategy.name}: {strategy.description}

## Judgment Rubric — Binary Decision

First, answer THIS question about the email:

  **"Does this email require the user to send a reply message?"**

  If YES → user_action = "reply"
    Then pick the BEST action_reason:
    - question_asked: the sender explicitly asked a question or made a request that needs an answer
    - waiting_for_you: the sender is clearly waiting for the user's input, approval, or decision
    - unsent_draft: this is a draft the user wrote but never sent
    - courtesy_due: sender invested real effort (wrote 3+ substantive sentences, shared a document, or explicitly asked for the user's thoughts). Do NOT flag automated notifications, newsletters, receipts, or one-line status updates.

  If NO → user_action = "review"
    Then pick the BEST action_reason:
    - upcoming_event: interview/meeting/deadline reminder — worth noting the time
    - deal_or_pipeline: project/partnership/deal status update worth tracking
    - security_or_billing: security alert, billing issue, subscription — needs checking
    - receipt_or_notice: receipt, subscription confirmation, normal account notice — record only
    - cleanup: newsletter, promotion, automated digest — safe to archive

  Priority & action_reason mapping (you MUST follow):
    question_asked → priority≥medium, should_show_in_main_result=true
    waiting_for_you → priority≥medium, should_show_in_main_result=true
    unsent_draft → priority≥medium, should_show_in_main_result=true
    courtesy_due → priority≥medium, should_show_in_main_result=true
    security_or_billing → priority≥high, should_show_in_main_result=true
    upcoming_event → priority≥medium
    deal_or_pipeline → priority≥medium
    receipt_or_notice → priority=low
    cleanup → priority=low, should_show_in_lower_priority=true

{few_shot}

## Mailbox Owner
You are evaluating mail for: {mailbox_profile.mailbox_id}
Match by EMAIL ADDRESS (between < >), not by display name.
- If the sender's email IS the mailbox owner → OUTGOING mail.
  SENT: user already sent it → user_action=review, action_reason=cleanup, priority=low.
  DRAFT: user hasn't sent it yet → user_action=reply, action_reason=unsent_draft, priority=medium.

## Email
{_render_context_for_prompt(candidate_context, mailbox_profile.mailbox_id)}

## User request
{task_plan.raw_user_request}
{_render_snooze_prefs_context(snooze_prefs)}

## Output format
Return a JSON object:

{{
  "base_judgment": {json.dumps(_base_schema(), ensure_ascii=False)},
  "mode_judgment": {json.dumps(mode_schema, ensure_ascii=False)},{user_facing_fields},
  "confidence": 0.0
}}

## Rules
- user_action + action_reason MUST be consistent per the rubric above. question_asked/waiting_for_you/unsent_draft/courtesy_due → reply. upcoming_event/deal_or_pipeline/security_or_billing/receipt_or_notice/cleanup → review.
- base_judgment.risk_level aliases priority: reply→medium, high-risk→high/critical, low-value→low/none
- user_facing_summary: name the core person, project, company, or risk event. Be specific.
- user_facing_reason: state WHAT recently happened with verifiable facts (sender name, email sent date, action). The date is the email's "Date" field from above — it is NOT a deadline or due date. Do NOT explain why it matters.
- user_facing_recommendation: one specific, differentiated action. Not generic like "evaluate and respond."
- If the sender IS the mailbox owner: user_facing_summary MUST say "Unsent draft" not "Reply needed".
- NEVER suggest send_email / delete_email / unsubscribe
- CRITICAL — Time format: NEVER use relative time words. ALWAYS use "Mon DD, YYYY" format. Examples: "May 28, 2026", "Jan 3, 2026". If time of day matters, append it: "May 28, 2026, 2:30 PM". If no date is available, say "recently".
- If the body mentions a deadline or timeframe ("before this weekend", "by Friday", "next Monday"), quote it verbatim in your output. Do NOT convert relative time to an absolute calendar date. Say "before this weekend", NOT "before Jun 8".
- reply_gaps (only when user_action="reply"): analyze what key questions the email is asking that the user needs to answer before a reply can be written. needs_user_input=true ONLY when the sender explicitly asked a question (pricing, availability, opinion, confirmation of a specific fact). needs_user_input=false for thank-you, FYI, or emails requiring no substantive response. questions: max 3, merge similar ones. Refer to the contact memory above for open loops and relationship context.
- Output ONLY valid JSON"""
    return prompt


def _render_context_for_batch_prompt(ctx: CandidateContext, owner_email: str = "") -> str:
    """把单个候选项压缩成批量评估提示词中的一段。"""
    c = ctx.candidate
    parts: list[str] = [
        f"Candidate ID: {c.candidate_id}",
        f"Kind: {c.kind}",
        f"Priority hint: {c.priority_hint}",
        f"From: {c.evidence.get('from', '')}",
        f"Subject: {c.evidence.get('subject', '')}",
        f"Snippet: {c.evidence.get('snippet', '')}",
        f"Date: {_fmt_ts(str(c.evidence.get('date', '')))}",
        f"Labels: {json.dumps(c.evidence.get('labels', []), ensure_ascii=False)}",
        f"Signals: {json.dumps(c.evidence.get('matched_signals', []), ensure_ascii=False)}",
    ]
    if ctx.type == "message_detail" and ctx.message:
        parts.append(f"Body: {ctx.message.body_text[:800]}")
    if ctx.type == "thread_context" and ctx.thread:
        thread_parts: list[str] = []
        msgs = list(ctx.thread.messages)
        owner_lower = (owner_email or "").strip().lower()

        # 从尾部向前找用户最后回复的位置
        cut = -1
        for i in range(len(msgs) - 1, -1, -1):
            if _extract_email(msgs[i].from_addr) == owner_lower:
                cut = i
                break

        if cut >= 0:
            visible = msgs[cut:]
            header = f"Thread ({len(visible)} of {len(msgs)} msgs, since your last reply):"
            footer = "↑ Messages marked UNREPLIED need your attention."
        else:
            visible = msgs[-4:]
            header = f"Thread ({len(visible)} of {len(msgs)} msgs, latest shown):"
            footer = "↑ Focus on the most recent messages."

        for i, msg in enumerate(visible):
            if cut >= 0 and i == 0:
                label = " (your last reply)"
            elif cut >= 0 and i == len(visible) - 1:
                label = " ← LATEST UNREPLIED"
            elif cut >= 0:
                label = " ← UNREPLIED"
            elif i == len(visible) - 1:
                label = " ← LATEST"
            else:
                label = ""
            thread_parts.append(
                f"Msg {i+1}{label}: from={msg.from_addr}; date={_fmt_ts(msg.internal_date)}; "
                f"subject={msg.subject}; body={msg.body_text[:260]}"
            )
        thread_parts.append(footer)
        parts.append("\n".join(thread_parts))
    if getattr(ctx, "contact_context", ""):
        parts.append(f"Contact memory: {ctx.contact_context}")
    return "\n".join(parts)


def build_batch_judgment_prompt(
    task_plan: MailTaskPlan,
    strategy: MailStrategy,
    mailbox_profile: MailboxProfile,
    candidate_contexts: list[CandidateContext],
    snooze_prefs: SnoozePrefs | None = None,
) -> str:
    """Build one compact LLM prompt for Anna sampling batch evaluation.

    Output fields align with PRD three-line card display:
      - title (Line 1): core object + what needs attention
      - context (Line 2): who did what + when + current status (verifiable facts)
      - suggestion (Line 3): specific next action
    """

    rendered_items = []
    for index, ctx in enumerate(candidate_contexts, start=1):
        rendered_items.append(f"### Candidate {index}\n{_render_context_for_batch_prompt(ctx, mailbox_profile.mailbox_id)}")

    rendered_text = chr(10).join(rendered_items)
    snooze_text = _render_snooze_prefs_context(snooze_prefs)
    prompt = (
        "You are Anna, an executive email assistant. Evaluate each candidate email below.\n"
        "Output a single JSON object. The very first character you write MUST be `{`.\n"
        "Do NOT wrap the JSON in markdown fences. Do NOT write any text before or after the JSON.\n\n"
        f"## Strategy\n{strategy.name}: {strategy.description}\n\n"
        "## Judgment Rubric — Binary Decision\n"
        "For each candidate, first decide: Does the user need to send a reply?\n"
        "  YES (user_action=\"reply\"): question_asked | waiting_for_you | unsent_draft | courtesy_due → priority≥medium, surface=true\n"
        "  NO  (user_action=\"review\"): upcoming_event | deal_or_pipeline | security_or_billing | receipt_or_notice | cleanup\n"
        "    security_or_billing → priority≥high; upcoming_event/deal_or_pipeline → priority≥medium; receipt_or_notice/cleanup → priority=low\n"
        f"## Mailbox Owner\n{mailbox_profile.mailbox_id} — match by EMAIL ADDRESS (between < >), not by display name.\n"
        "- Sender IS mailbox owner → OUTGOING. SENT: surface=false, priority=low, user_action=review, action_reason=cleanup.\n"
        "  DRAFT: surface=true, priority=medium, user_action=reply, action_reason=unsent_draft.\n\n"
        f"## User request\n{task_plan.raw_user_request}\n{snooze_text}\n\n"
        "## Candidates\n"
        + rendered_text + "\n\n"
        '## Output\nReturn EXACTLY:\n\n'
        '{"items":[\n'
        '  {"candidate_id":"<copy from input>","priority":"medium","surface":true,\n'
        '   "user_action":"reply","action_reason":"question_asked",\n'
        '   "title":"Person re: subject","context":"Person sent X on date. No reply yet.",\n'
        '   "suggestion":"Reply to person about X by Friday.","action":"create_draft","needs":"Reply to person",\n'
        '   "latest_action":"sent a follow-up","latest_actor":"Sender Name",\n'
        '   "reply_gaps":{"needs_user_input":true,"summary":"Needs pricing confirmation","questions":[{"id":"q1","question":"What price should we quote?","hint":"number or range","required":true}]}}\n'
        ']}\n\n'
        'Every input Candidate MUST have one entry in items. Do NOT skip or add.\n'
        '\n'
        '## Rules\n'
        '- user_action + action_reason must be consistent per rubric above.\n'
        '- reply_gaps (only when user_action="reply"): include needs_user_input and questions (max 3). needs_user_input=true only for explicit questions. Use contact memory for context.\n'
        '- context: state WHAT recently happened with verifiable facts (sender name, email sent date, action). The date is the email\'s Date field from the input — it is NOT a deadline or due date. Do NOT explain why it matters.\n'
        '- suggestion: one specific, differentiated next action.\n'
        '- CRITICAL — Time format: NEVER use relative time words. ALWAYS use "Mon DD, YYYY" format.\n'
        '- If the body mentions a deadline or timeframe ("before this weekend", "by Friday"), quote it verbatim. Do NOT convert to an absolute calendar date. Say "before this weekend", NOT "before Jun 8".\n'
    )
    return prompt


# ── Parse Judgment Output ─────────────────────────────────────────

def build_anna_single_judgment_prompt(
    task_plan: MailTaskPlan,
    strategy: MailStrategy,
    mailbox_profile: MailboxProfile,
    candidate_context: CandidateContext,
    snooze_prefs: SnoozePrefs | None = None,
) -> str:
    """为 Anna sampling 构造单候选评估 prompt，补齐 rubric + few-shot + 完整规则。

    输出仍为扁平 JSON（_parse_compact_batch_item 解析），但 prompt 质量对标 DashScope 路径的 build_judgment_prompt。
    """
    ctx = candidate_context
    c = ctx.candidate
    snooze_text = _render_snooze_prefs_context(snooze_prefs)
    few_shot = _render_few_shot_examples(strategy.judgment_policy.few_shot_examples)

    prompt = f"""You are Anna, an executive email assistant. Evaluate exactly ONE email candidate against the strategy below.
Output ONLY a single JSON object. Do NOT wrap in markdown. Do NOT explain. The very first character you write MUST be `{{`.

## Strategy
{strategy.name}: {strategy.description}

## Judgment Rubric — Binary Decision

First, answer THIS question about the email:

  **"Does this email require the user to send a reply message?"**

  If YES → user_action = "reply"
    Then pick the BEST action_reason:
    - question_asked: the sender explicitly asked a question or made a request that needs an answer
    - waiting_for_you: the sender is clearly waiting for the user's input, approval, or decision
    - unsent_draft: this is a draft the user wrote but never sent
    - courtesy_due: sender invested real effort (wrote 3+ substantive sentences, shared a document, or explicitly asked for the user's thoughts). Do NOT flag automated notifications, newsletters, receipts, or one-line status updates.

  If NO → user_action = "review"
    Then pick the BEST action_reason:
    - upcoming_event: interview/meeting/deadline reminder — worth noting the time
    - deal_or_pipeline: project/partnership/deal status update worth tracking
    - security_or_billing: security alert, billing issue, subscription — needs checking
    - receipt_or_notice: receipt, subscription confirmation, normal account notice — record only
    - cleanup: newsletter, promotion, automated digest — safe to archive

  Priority & action_reason mapping (you MUST follow):
    question_asked → priority≥medium, surface=true
    waiting_for_you → priority≥medium, surface=true
    unsent_draft → priority≥medium, surface=true
    courtesy_due → priority≥medium, surface=true
    security_or_billing → priority≥high, surface=true
    upcoming_event → priority≥medium
    deal_or_pipeline → priority≥medium
    receipt_or_notice → priority=low
    cleanup → priority=low

{few_shot}

## Mailbox Owner
You are evaluating mail for: {mailbox_profile.mailbox_id}
Match by EMAIL ADDRESS (between < >), not by display name.
- If the sender's email IS the mailbox owner → OUTGOING mail.
  SENT: user already sent it → surface=false, priority=low, user_action=review, action_reason=cleanup.
  DRAFT: user hasn't sent it yet → user_action=reply, action_reason=unsent_draft, priority=medium.

## Email
candidate_id: {c.candidate_id}
kind: {c.kind}
priority_hint: {c.priority_hint}
from: {c.evidence.get('from', '')}
subject: {c.evidence.get('subject', '')}
snippet: {c.evidence.get('snippet', '')}
date: {_fmt_ts(str(c.evidence.get('date', '')))}
context_type: {ctx.type}
{_render_body_and_thread(ctx, mailbox_profile.mailbox_id)}

## User request
{task_plan.raw_user_request}
{snooze_text}

## Output format
Return exactly this JSON shape:

{{{{
  "candidate_id": "{c.candidate_id}",
  "priority": "medium",
  "surface": true,
  "user_action": "reply",
  "action_reason": "question_asked",
  "title": "Short card title (English ≤12 words)",
  "context": "WHAT happened: who did what, when, and current status. Verifiable facts only. English ≤30 words.",
  "suggestion": "Specific next action. Be concrete, not generic. English ≤15 words.",
  "action": "create_draft",
  "needs": "English ≤4 words label for what the user needs to decide or do.",
  "latest_action": "English ≤8 words. What recently happened — the latest action by a person or service.",
  "latest_actor": "English name or service. Who performed the latest_action.",
  "reply_gaps": {{"needs_user_input":true, "summary":"one-sentence summary", "questions":[{{"id":"q1", "question":"What do you want to reply?", "hint":"short hint", "required":true}}]}},
  "confidence": 0.85
}}}}

Allowed user_action: reply, review.
Allowed action_reason: question_asked, waiting_for_you, unsent_draft, courtesy_due, upcoming_event, deal_or_pipeline, security_or_billing, receipt_or_notice, cleanup.
Allowed priority: critical, high, medium, low, ignore.
Allowed action: create_draft, create_reminder, save_note, do_nothing.

## Rules
- user_action + action_reason MUST be consistent: question_asked/waiting_for_you/unsent_draft/courtesy_due → reply. upcoming_event/deal_or_pipeline/security_or_billing/receipt_or_notice/cleanup → review.
- priority: reply reasons → medium or high. security_or_billing → critical or high. schedule/logistics → medium. receipt/cleanup → low.
- title: name the core person, project, company, or risk event. Be specific. If sender IS the mailbox owner, title MUST start with "Unsent draft".
- context: state WHAT recently happened with verifiable facts (sender name, email sent date, action). The date is the email's "Date" field from above — it is NOT a deadline or due date. Do NOT explain why it matters.
- suggestion: one specific, differentiated next action. Never generic like "evaluate and respond."
- NEVER suggest send_email / delete_email / unsubscribe. Use create_draft for reply suggestions, create_reminder for things to review, save_note for info worth recording, do_nothing when no action is needed.
- needs: concise category like "Reply to Kate", "Timing decision", "Receipt check", "Manual review".
- Before output: re-read your suggestion. If it contains a time constraint, priority MUST NOT be low.
- CRITICAL — Time format: NEVER use relative time words. ALWAYS use "Mon DD, YYYY" format. Examples: "May 28, 2026", "Jan 3, 2026". If time of day matters, append it: "May 28, 2026, 2:30 PM". If no date is available, say "recently".
- If the body mentions a deadline or timeframe ("before this weekend", "by Friday", "next Monday"), quote it verbatim in your output. Do NOT convert relative time to an absolute calendar date. Say "before this weekend", NOT "before Jun 8".
- reply_gaps (only when user_action="reply"): analyze what key questions the email is asking that need the user's input. needs_user_input=true ONLY for explicit questions. needs_user_input=false for thank-you/FYI. questions: max 3. Refer to contact memory for open loops.
- Output ONLY valid JSON."""
    return prompt


REPLY_REASONS = frozenset({"question_asked", "waiting_for_you", "unsent_draft", "courtesy_due"})
REVIEW_REASONS = frozenset({"upcoming_event", "deal_or_pipeline", "security_or_billing", "receipt_or_notice", "cleanup"})


def _enforce_consistency(judgment: JudgmentResult) -> JudgmentResult:
    """Post-process a judgment to catch LLM self-contradictions.

    Hard constraints based on action_reason, with code-level override.
    """
    fd = judgment.final_decision
    bj = judgment.base_judgment
    action_reason = _extract_action_reason(judgment)

    # 1. action_reason → user_action hard override (code is authoritative)
    if action_reason in REPLY_REASONS:
        fd.user_action = "reply"
    elif action_reason in REVIEW_REASONS:
        fd.user_action = "review"
    elif not fd.user_action:
        fd.user_action = "review"

    # 2. reply reasons → priority ≥ medium, must surface
    if action_reason in REPLY_REASONS:
        if fd.priority in ("low", "ignore"):
            fd.priority = "medium"
        fd.should_show_in_main_result = True
        fd.should_show_in_lower_priority = False

    # 3. security_or_billing → priority ≥ high, must surface
    if action_reason == "security_or_billing":
        if fd.priority not in ("critical", "high"):
            fd.priority = "high"
        fd.should_show_in_main_result = True
        fd.should_show_in_lower_priority = False

    # 4. cleanup → low priority
    if action_reason == "cleanup" and fd.priority not in ("low", "ignore"):
        fd.priority = "low"

    # 5. Deadline words in recommendation → at least medium
    rec = fd.user_facing_recommendation.lower()
    deadline_words = ("before", "by tomorrow", "by friday", "by thursday", "today", "asap", "tonight")
    if any(w in rec for w in deadline_words) and fd.priority == "low":
        fd.priority = "medium"
        fd.should_show_in_main_result = True
        fd.should_show_in_lower_priority = False

    # 6. Surface → can't be ignore
    if bj.should_surface and fd.priority == "ignore":
        fd.priority = "low"

    return judgment


def _extract_action_reason(judgment: JudgmentResult) -> str:
    """action_reason is stored in mode_judgment by both parse paths."""
    mode = judgment.mode_judgment if isinstance(judgment.mode_judgment, dict) else {}
    return str(mode.get("action_reason") or "")


def parse_judgment_output(raw_json: dict[str, Any], strategy: MailStrategy) -> JudgmentResult:
    """Parse LLM JSON output into a JudgmentResult.

    Auto-derives item_type and should_surface from other fields
    since the LLM prompt no longer asks for them explicitly.
    """
    base_raw = raw_json.get("base_judgment") if isinstance(raw_json.get("base_judgment"), dict) else {}
    mode_raw = raw_json.get("mode_judgment") if isinstance(raw_json.get("mode_judgment"), dict) else {}
    decision_raw = raw_json.get("final_decision") if isinstance(raw_json.get("final_decision"), dict) else {}

    user_action = str(decision_raw.get("user_action") or "")
    item_type = _user_action_to_item_type(user_action) if user_action else "unknown"

    # Derive should_surface from final_decision
    should_surface = bool(decision_raw.get("should_show_in_main_result"))
    # Risk-level items always surface
    risk = _safe_enum(base_raw.get("risk_level"), ["none", "low", "medium", "high", "critical"], "none")
    if risk in ("high", "critical"):
        should_surface = True

    base = BaseJudgment(
        item_type=item_type,
        requires_user_action=bool(base_raw.get("requires_user_action")),
        can_agent_prepare=bool(base_raw.get("can_agent_prepare")),
        can_agent_handle_after_approval=bool(base_raw.get("can_agent_handle_after_approval")),
        risk_level=risk,
        other_party_waiting=bool(base_raw.get("other_party_waiting")),
        user_is_blocking=bool(base_raw.get("user_is_blocking")),
        should_surface=should_surface,
        reason=str(base_raw.get("reason") or "")[:500],
    )

    # Parse final decision
    actions = decision_raw.get("recommended_actions") if isinstance(decision_raw.get("recommended_actions"), list) else []
    parsed_actions: list[dict[str, Any]] = []
    for action in actions:
        if isinstance(action, dict):
            parsed_actions.append({
                "action_type": str(action.get("action_type") or "do_nothing"),
                "risk_level": str(action.get("risk_level") or "low"),
                "requires_approval": bool(action.get("requires_approval")),
                "payload": action.get("payload") if isinstance(action.get("payload"), dict) else {},
                "reason": str(action.get("reason") or "")[:300],
            })

    if user_action not in ("reply", "review"):
        user_action = "review"

    action_reason = str(decision_raw.get("action_reason") or "")
    if not isinstance(mode_raw, dict):
        mode_raw = {}
    if action_reason:
        mode_raw["action_reason"] = action_reason

    # Extract reply_gaps from final_decision
    reply_gaps = decision_raw.get("reply_gaps") if isinstance(decision_raw.get("reply_gaps"), dict) else {}
    if reply_gaps:
        mode_raw["reply_gaps"] = reply_gaps

    decision = FinalDecision(
        display_bucket=str(decision_raw.get("display_bucket") or ""),
        priority=_safe_enum(decision_raw.get("priority"), ["critical", "high", "medium", "low", "ignore"], "low"),
        should_show_in_main_result=should_surface,
        should_show_in_lower_priority=bool(decision_raw.get("should_show_in_lower_priority")),
        recommended_actions=parsed_actions,
        user_facing_summary=str(decision_raw.get("user_facing_summary") or "")[:300],
        user_facing_reason=str(decision_raw.get("user_facing_reason") or "")[:500],
        user_facing_recommendation=str(decision_raw.get("user_facing_recommendation") or "")[:500],
        user_action=user_action,
    )

    try:
        confidence = float(raw_json.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5

    # Capture PRD-V2 Details fields from final_decision if LLM provided them
    if not isinstance(mode_raw, dict):
        mode_raw = {}
    for detail_key in ("needs", "latest_action", "latest_actor"):
        val = decision_raw.get(detail_key)
        if val and detail_key not in mode_raw:
            mode_raw[detail_key] = str(val)

    result = JudgmentResult(
        candidate_id=str(raw_json.get("candidate_id") or ""),
        strategy_mode=strategy.id,
        base_judgment=base,
        mode_judgment=mode_raw,
        final_decision=decision,
        confidence=confidence,
    )
    return _enforce_consistency(result)


def _user_action_to_item_type(user_action: str) -> str:
    if user_action == "reply":
        return "reply_required"
    if user_action == "review":
        return "account_notice"
    return "unknown"


def _safe_enum(value: Any, allowed: list[str], default: str) -> Any:
    s = str(value or default)
    if s in allowed:
        return s
    return default


# ── Create fallback judgment ──────────────────────────────────────

def create_fallback_judgment(candidate_id: str, strategy: MailStrategy, reason: str = "") -> JudgmentResult:
    """Create a conservative fallback judgment when LLM fails."""
    fallback_reason = reason[:500] if reason else "unknown"
    # Truncate fallback reason for display
    short_reason = fallback_reason[:200] if fallback_reason else "unknown error"
    judgment = JudgmentResult(
        candidate_id=candidate_id,
        strategy_mode=strategy.id,
        base_judgment=BaseJudgment(
            item_type="unknown",
            requires_user_action=True,
            risk_level="medium",
            should_surface=True,
            reason=f"LLM evaluation unavailable: {short_reason}",
        ),
        mode_judgment={"fallback_reason": short_reason},
        final_decision=FinalDecision(
            display_bucket="Manual review needed",
            priority="medium",
            should_show_in_main_result=True,
            user_facing_summary=f"Item needs manual review",
            user_facing_reason=f"Anna was unable to evaluate this email automatically. Reason: {short_reason}",
            user_facing_recommendation="Please review this email manually to decide if action is needed.",
            recommended_actions=[{
                "action_type": "do_nothing",
                "risk_level": "low",
                "requires_approval": False,
                "payload": {},
                "reason": f"LLM fallback: {short_reason}",
            }],
        ),
        confidence=0.1,
    )
    return judgment


_ITEM_TYPE_DISPLAY: dict[str, str] = {
    "reply_required": "Reply needed",
    "confirmation_required": "Confirmation needed",
    "security_risk": "Security alert",
    "billing_or_subscription": "Billing",
    "business_or_creator_thread": "Business thread",
    "account_notice": "Account notice",
    "low_value_cleanup": "Low priority",
    "unknown": "Review needed",
}


def _parse_compact_batch_item(raw: dict[str, Any], strategy: MailStrategy) -> JudgmentResult:
    """把 Anna 批量评估的紧凑 JSON 转换成统一 JudgmentResult。

    字段映射（新格式优先，兼容旧格式）：
      title (new) / summary (old)  → user_facing_summary  → card.title (Line 1)
      context (new) / reason (old) → user_facing_reason   → card.summary (Line 2)
      suggestion (new) / recommendation (old) → user_facing_recommendation → card.recommendation (Line 3)
    """
    priority = _safe_enum(raw.get("priority"), ["critical", "high", "medium", "low", "ignore"], "low")
    risk_level = "none" if priority == "ignore" else priority
    surface = bool(raw.get("surface")) or priority in ("critical", "high", "medium")
    user_action = str(raw.get("user_action") or "")
    if user_action not in ("reply", "review"):
        user_action = "review"
    item_type = _user_action_to_item_type(user_action)
    action_type = str(raw.get("action") or "do_nothing")
    if action_type in ("send_email", "delete_email", "unsubscribe"):
        action_type = "do_nothing"

    # 新字段 title/context/suggestion 优先，fallback 到旧字段 summary/reason/recommendation
    summary = str(raw.get("title") or raw.get("summary") or "")[:300]
    reason = str(raw.get("context") or raw.get("reason") or "")[:500]
    recommendation = str(raw.get("suggestion") or raw.get("recommendation") or "")[:500]
    action_reason = str(raw.get("action_reason") or "")
    reply_gaps = raw.get("reply_gaps") if isinstance(raw.get("reply_gaps"), dict) else {}
    display_bucket = str(raw.get("bucket") or _ITEM_TYPE_DISPLAY.get(item_type, item_type))

    result = JudgmentResult(
        candidate_id=str(raw.get("candidate_id") or ""),
        strategy_mode=strategy.id,
        base_judgment=BaseJudgment(
            item_type=item_type,
            requires_user_action=surface and priority != "ignore",
            can_agent_prepare=action_type in ("create_draft", "create_reminder", "save_note"),
            can_agent_handle_after_approval=False,
            risk_level=risk_level,  # type: ignore[arg-type]
            other_party_waiting=item_type in ("reply_required", "confirmation_required"),
            user_is_blocking=item_type in ("reply_required", "confirmation_required"),
            should_surface=surface,
            reason=reason,
        ),
        mode_judgment={
            "bucket": display_bucket,
            "compact_item_type": item_type,
            "action_reason": action_reason,
            "reply_gaps": reply_gaps,
            "needs": str(raw.get("needs") or ""),
            "latest_action": str(raw.get("latest_action") or ""),
            "latest_actor": str(raw.get("latest_actor") or ""),
        },
        final_decision=FinalDecision(
            display_bucket=display_bucket,
            priority=priority,  # type: ignore[arg-type]
            should_show_in_main_result=surface,
            should_show_in_lower_priority=(not surface and priority in ("low", "ignore")),
            user_facing_summary=summary,
            user_facing_reason=reason,
            user_facing_recommendation=recommendation,
            user_action=user_action,
            recommended_actions=[{
                "action_type": action_type,
                "risk_level": "low" if priority in ("low", "ignore") else "medium",
                "requires_approval": action_type in ("create_draft",),
                "payload": {},
                "reason": reason,
            }],
        ),
        confidence=float(raw.get("confidence") or 0.5),
    )
    return _enforce_consistency(result)


# ── Evaluate Item ─────────────────────────────────────────────────

async def evaluate_item(
    task_plan: MailTaskPlan,
    strategy: MailStrategy,
    mailbox_profile: MailboxProfile,
    candidate_context: CandidateContext,
    sampling_create_message: Any,
    snooze_prefs: SnoozePrefs | None = None,
) -> JudgmentResult:
    """Evaluate a single candidate item using LLM (DashScope path)."""
    from ..llm_runtime.service import call_llm_json_safe

    prompt = build_judgment_prompt(task_plan, strategy, mailbox_profile, candidate_context, snooze_prefs)

    strict_anna_sampling = sampling_create_message is not None
    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt="你是一个严格的 JSON 生成器，只输出有效 JSON，不输出解释或 markdown。",
        user_message=prompt,
        fallback={},
        temperature=0.1,
        max_tokens=8192,
        timeout=240.0,
        metadata={
            "tool": "evaluate_item",
            "strategy_mode": strategy.id,
            "candidate_id": candidate_context.candidate.candidate_id,
        },
        allow_fallback=not strict_anna_sampling,
        allow_sampling_provider_fallback=not strict_anna_sampling,
    )

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    if not payload or result.get("fallback_used"):
        fallback_reason = str(result.get("fallback_reason") or "empty or invalid LLM JSON response")
        return create_fallback_judgment(candidate_context.candidate.candidate_id, strategy, fallback_reason)

    judgment = parse_judgment_output(payload, strategy)
    judgment.candidate_id = candidate_context.candidate.candidate_id
    return judgment


async def evaluate_items_batch(
    task_plan: MailTaskPlan,
    strategy: MailStrategy,
    mailbox_profile: MailboxProfile,
    candidate_contexts: list[CandidateContext],
    sampling_create_message: Any,
    *,
    max_sampling_calls: int = 7,
    snooze_prefs: SnoozePrefs | None = None,
    progress_callback: Any = None,
) -> list[JudgmentResult]:
    """Use Anna sampling to evaluate all candidates in batches without dropping candidates."""
    from ..llm_runtime.service import call_llm_json_safe

    if not candidate_contexts:
        return []

    call_count = max(1, min(max_sampling_calls, len(candidate_contexts)))
    batch_size = max(1, math.ceil(len(candidate_contexts) / call_count))
    if sampling_create_message is not None:
        batch_size = 1
    judgments_by_id: dict[str, JudgmentResult] = {}
    evaluated_count = 0
    success_count = 0
    fallback_count = 0

    def _report(current: int, status: str, reason: str = "") -> None:
        if not progress_callback:
            return
        progress_callback(
            "evaluate",
            {
                "current": current,
                "total": len(candidate_contexts),
                "evaluated": evaluated_count,
                "succeeded": success_count,
                "fallback": fallback_count,
                "mode": "anna_batch",
                "status": status,
                **({"reason": reason[:200]} if reason else {}),
            },
        )

    for start in range(0, len(candidate_contexts), batch_size):
        batch = candidate_contexts[start:start + batch_size]
        current_index = min(start + len(batch), len(candidate_contexts))
        _report(start + 1, "running")
        expected_ids = [ctx.candidate.candidate_id for ctx in batch]
        if sampling_create_message is not None:
            prompt = build_anna_single_judgment_prompt(task_plan, strategy, mailbox_profile, batch[0], snooze_prefs)
        else:
            prompt = build_batch_judgment_prompt(task_plan, strategy, mailbox_profile, batch, snooze_prefs)

        result = await call_llm_json_safe(
            sampling_create_message,
            system_prompt="You are a strict JSON generator. Output ONLY valid JSON — no explanation, no markdown, no code fences.",
            user_message=prompt,
            fallback={"judgments": []},
            temperature=0.1,
            max_tokens=8192 if sampling_create_message is not None else min(4096, max(900, 450 * len(batch))),
            timeout=120.0 if sampling_create_message is not None else 240.0,
            metadata={
                "tool": "evaluate_item_single" if sampling_create_message is not None else "evaluate_items_batch",
                "strategy_mode": strategy.id,
                "candidate_count": str(len(batch)),
            },
            allow_fallback=True,
            allow_sampling_provider_fallback=sampling_create_message is None,
            max_attempts=2 if sampling_create_message is not None else None,
        )

        payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        if sampling_create_message is not None and payload and not result.get("fallback_used"):
            raw_judgments = [payload]
        else:
            raw_judgments = payload.get("items") if isinstance(payload.get("items"), list) else []
        if not raw_judgments:
            raw_judgments = payload.get("judgments") if isinstance(payload.get("judgments"), list) else []
        if result.get("fallback_used") or not raw_judgments:
            if sampling_create_message is not None:
                reason = str(result.get("fallback_reason") or "Anna batch evaluation returned no judgments")
                for ctx in batch:
                    judgments_by_id[ctx.candidate.candidate_id] = create_fallback_judgment(
                        ctx.candidate.candidate_id,
                        strategy,
                        reason,
                    )
                    fallback_count += 1
                    evaluated_count += 1
                _report(current_index, "fallback", reason)
                continue
            reason = str(result.get("fallback_reason") or "empty or invalid batch LLM JSON response")
            for ctx in batch:
                judgments_by_id[ctx.candidate.candidate_id] = create_fallback_judgment(
                    ctx.candidate.candidate_id,
                    strategy,
                    reason,
                )
            continue

        for raw in raw_judgments:
            if not isinstance(raw, dict):
                continue
            candidate_id = str(raw.get("candidate_id") or "")
            if sampling_create_message is not None and not candidate_id and len(expected_ids) == 1:
                candidate_id = expected_ids[0]
                raw["candidate_id"] = candidate_id
            if candidate_id not in expected_ids:
                continue
            if "base_judgment" in raw or "final_decision" in raw:
                judgment = parse_judgment_output(raw, strategy)
            else:
                judgment = _parse_compact_batch_item(raw, strategy)
            judgment.candidate_id = candidate_id
            judgments_by_id[candidate_id] = judgment
            success_count += 1
            evaluated_count += 1

        for ctx in batch:
            if ctx.candidate.candidate_id not in judgments_by_id:
                if sampling_create_message is not None:
                    reason = "Anna batch response missed this candidate"
                    judgments_by_id[ctx.candidate.candidate_id] = create_fallback_judgment(
                        ctx.candidate.candidate_id,
                        strategy,
                        reason,
                    )
                    fallback_count += 1
                    evaluated_count += 1
                    _report(current_index, "fallback", reason)
                    continue
                judgments_by_id[ctx.candidate.candidate_id] = create_fallback_judgment(
                    ctx.candidate.candidate_id,
                    strategy,
                    "batch LLM response missed this candidate",
                )
        _report(current_index, "done")

    return [judgments_by_id[ctx.candidate.candidate_id] for ctx in candidate_contexts]
