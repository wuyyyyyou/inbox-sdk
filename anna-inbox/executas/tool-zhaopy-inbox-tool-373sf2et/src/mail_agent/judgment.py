"""Item evaluator (§13–§14) — build prompts, call LLM, parse JSON output."""

from __future__ import annotations

import json
import math
from typing import Any

from datetime import datetime, timezone, timedelta
from .types import (
    BaseJudgment,
    CandidateContext,
    FinalDecision,
    JudgmentResult,
    MailStrategy,
    MailTaskPlan,
    MailboxProfile,
    StrategyMode,
)
from .storage_types import SnoozePrefs

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


# ── Render context for prompt ─────────────────────────────────────

def _render_context_for_prompt(ctx: CandidateContext) -> str:
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

    if ctx.type == "message_detail" and ctx.message:
        m = ctx.message
        parts.append(f"\n--- Full Message Body ---")
        parts.append(f"Body text: {m.body_text[:1200]}")

    if ctx.type == "thread_context" and ctx.thread:
        t = ctx.thread
        parts.append(f"\n--- Thread Context ({len(t.messages)} messages) ---")
        for i, msg in enumerate(t.messages):
            parts.append(f"\nMessage {i+1}:")
            parts.append(f"  From: {msg.from_addr}")
            parts.append(f"  Date: {_fmt_ts(msg.internal_date)}")
            parts.append(f"  Subject: {msg.subject}")
            parts.append(f"  Body: {msg.body_text[:400]}")

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
        "bucket": "one of: must_review | needs_reply | needs_confirmation | agent_can_prepare | safe_cleanup | lower_priority | ignore",
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
        "display_bucket": "string (short English label)",
        "priority": "one of: critical | high | medium | low | ignore",
        "should_show_in_main_result": "boolean",
        "should_show_in_lower_priority": "boolean",
        "recommended_actions": "array of {action_type, risk_level, requires_approval, payload, reason}",
        "user_facing_summary": "string (short English, ≤15 words)",
        "user_facing_reason": "string (English, ≤80 chars, explain to user WHY this matters)",
        "user_facing_recommendation": "string (English, ≤80 chars, tell user WHAT to do)",
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
    "latest_actor": "English name. The person or service who performed the latest action."
  }"""

    prompt = f"""You are Anna, an executive email assistant. Evaluate the email against the strategy below. Output ONLY JSON — no markdown, no explanation.

## Strategy
{strategy.name}: {strategy.description}

{strategy.judgment_policy.rubric}
{few_shot}

## Mailbox Owner
You are evaluating mail for: {mailbox_profile.mailbox_id}
Match by EMAIL ADDRESS (between < >), not by display name.
- If the sender's email IS the mailbox owner → OUTGOING mail.
  SENT: user already sent it → should_show_in_main_result=false, priority=low.
  DRAFT: user hasn't sent it yet → surface as unsent draft reminder.

## Email
{_render_context_for_prompt(candidate_context)}

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
- user_action: "reply" if a person is waiting for a response; "review" if no reply needed but worth awareness (schedule change, pipeline update, scorecard, internal note). There is NO "ignore" here — Phase 1 already filtered those out.
- base_judgment.risk_level: security/billing→critical/high, needs reply→medium, notifications→low/none
- final_decision.priority MUST match risk_level
- final_decision.should_show_in_main_result: critical/high/medium→true, low/none→false
- requires_user_action=true when the user needs to act
- If the email mentions a specific upcoming event (interview, meeting, deadline) within the next few days, priority must be at least medium and should_show_in_main_result must be true.
- "Review before Thursday's interview" / "before tomorrow's sync" / "submit before hiring sync" → priority=medium or higher.
- user_action=reply + priority=low is INVALID. If a reply is needed, priority>=medium and should_show_in_main_result=true.
- user_facing_summary: name the core person, project, company, or risk event. Be specific.
- user_facing_reason: state WHAT recently happened with verifiable facts (sender name, date, action). Do NOT explain why it matters.
- user_facing_recommendation: one specific, differentiated action. Not generic like "evaluate and respond."
- If the sender IS the mailbox owner: user_facing_summary MUST say "Unsent draft" not "Reply needed".
- NEVER suggest send_email / delete_email / unsubscribe
- Before output: re-read user_facing_recommendation. If it contains a time constraint ("before X", "by Friday", "today"), priority MUST NOT be low.
- CRITICAL — Time format: NEVER use relative time words ("tomorrow", "next Monday", "this Friday", "yesterday", "in 2 days", "next week"). ALWAYS use absolute calendar dates from the email Date field (e.g. "Jun 3", "May 28, 2:30 PM"). If no date is available, say "recently" rather than guessing a relative day.
- Output ONLY valid JSON"""
    return prompt


def _render_context_for_batch_prompt(ctx: CandidateContext) -> str:
    """把单个候选项压缩成批量评估提示词中的一段。"""
    c = ctx.candidate
    parts: list[str] = [
        f"Candidate ID: {c.candidate_id}",
        f"Kind: {c.kind}",
        f"Priority hint: {c.priority_hint}",
        f"From: {c.evidence.get('from', '')}",
        f"Subject: {c.evidence.get('subject', '')}",
        f"Snippet: {c.evidence.get('snippet', '')}",
        f"Date: {c.evidence.get('date', '')}",
        f"Labels: {json.dumps(c.evidence.get('labels', []), ensure_ascii=False)}",
        f"Signals: {json.dumps(c.evidence.get('matched_signals', []), ensure_ascii=False)}",
    ]
    if ctx.type == "message_detail" and ctx.message:
        parts.append(f"Body: {ctx.message.body_text[:800]}")
    if ctx.type == "thread_context" and ctx.thread:
        thread_parts: list[str] = []
        for i, msg in enumerate(ctx.thread.messages[:4], start=1):
            thread_parts.append(
                f"Message {i}: from={msg.from_addr}; date={_fmt_ts(msg.internal_date)}; "
                f"subject={msg.subject}; body={msg.body_text[:260]}"
            )
        parts.append("Thread:\n" + "\n".join(thread_parts))
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
        rendered_items.append(f"### Candidate {index}\n{_render_context_for_batch_prompt(ctx)}")

    rendered_text = chr(10).join(rendered_items)
    snooze_text = _render_snooze_prefs_context(snooze_prefs)
    prompt = (
        "You are Anna, an executive email assistant. Evaluate each candidate email below.\n"
        "Output a single JSON object. The very first character you write MUST be `{`.\n"
        "Do NOT wrap the JSON in markdown fences. Do NOT write any text before or after the JSON.\n\n"
        f"## Strategy\n{strategy.name}: {strategy.description}\n\n"
        f"## Mailbox Owner\n{mailbox_profile.mailbox_id} — match by EMAIL ADDRESS (between < >), not by display name.\n"
        "- Sender IS mailbox owner → OUTGOING. SENT: surface=false, priority=low.\n"
        "  DRAFT: surface=true, priority=medium, item_type=reply_required (unsent draft).\n\n"
        f"## User request\n{task_plan.raw_user_request}\n{snooze_text}\n\n"
        "## Candidates\n"
        + rendered_text + "\n\n"
        '## Output\nReturn EXACTLY:\n\n'
        '{"items":[\n'
        '  {"candidate_id":"<copy from input>","priority":"medium","surface":true,\n'
        '   "item_type":"reply_required","title":"Person re: subject","context":"Person sent X on date. No reply yet.",\n'
        '   "suggestion":"Reply to person about X by Friday.","action":"create_draft","needs":"Reply to person",\n'
        '   "latest_action":"sent a follow-up","latest_actor":"Sender Name","confidence":0.85}\n'
        ']}\n\n'
        'Every input Candidate MUST have one entry in items. Do NOT skip or add.\n'
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

{strategy.judgment_policy.rubric}
{few_shot}

## Mailbox Owner
You are evaluating mail for: {mailbox_profile.mailbox_id}
Match by EMAIL ADDRESS (between < >), not by display name.
- If the sender's email IS the mailbox owner → OUTGOING mail.
  SENT: user already sent it → surface=false, priority=low.
  DRAFT: user hasn't sent it yet → surface as unsent draft reminder.

## Email
candidate_id: {c.candidate_id}
kind: {c.kind}
priority_hint: {c.priority_hint}
from: {c.evidence.get('from', '')}
subject: {c.evidence.get('subject', '')}
snippet: {c.evidence.get('snippet', '')}
date: {c.evidence.get('date', '')}
context_type: {ctx.type}
{_render_context_for_batch_prompt(ctx)}

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
  "title": "Short card title (English ≤12 words)",
  "context": "WHAT happened: who did what, when, and current status. Verifiable facts only. English ≤30 words.",
  "suggestion": "Specific next action. Be concrete, not generic. English ≤15 words.",
  "action": "create_draft",
  "needs": "English ≤4 words label for what the user needs to decide or do.",
  "latest_action": "English ≤8 words. What recently happened — the latest action by a person or service.",
  "latest_actor": "English name or service. Who performed the latest_action.",
  "confidence": 0.85
}}}}

Allowed user_action: reply, review.
Allowed priority: critical, high, medium, low, ignore.
Allowed action: create_draft, create_reminder, save_note, do_nothing.

## Rules
- user_action: "reply" if a person is waiting for a response; "review" if no reply needed but worth awareness (schedule change, pipeline update, scorecard, internal note, calendar reschedule). There is NO "ignore" here — Phase 1 already filtered those out.
- priority: security/risk/payment failure → critical or high. Human waiting for reply → medium or high. Schedule/logistics awareness → medium. Routine receipt/notification → low.
- user_action=reply + priority=low is INVALID. If a reply is needed, priority>=medium and surface=true.
- If the suggestion text mentions a deadline or time constraint ("before Thursday", "by tomorrow", "today", "before hiring sync"), priority MUST be medium or higher.
- title: name the core person, project, company, or risk event. Be specific. If sender IS the mailbox owner, title MUST start with "Unsent draft".
- context: state WHAT recently happened with verifiable facts (sender name, date, action). Do NOT explain why it matters.
- suggestion: one specific, differentiated next action. Never generic like "evaluate and respond."
- NEVER suggest send_email / delete_email / unsubscribe. Use create_draft for reply suggestions, create_reminder for things to review, save_note for info worth recording, do_nothing when no action is needed.
- needs: concise category like "Reply to Kate", "Timing decision", "Receipt check", "Manual review".
- Before output: re-read your suggestion. If it contains a time constraint, priority MUST NOT be low.
- CRITICAL — Time format: NEVER use relative time words ("tomorrow", "next Monday", "this Friday", "yesterday", "in 2 days", "next week"). ALWAYS use absolute calendar dates from the email Date field (e.g. "Jun 3", "May 28, 2:30 PM"). If no date is available, say "recently" rather than guessing a relative day.
- Output ONLY valid JSON."""
    return prompt


def _enforce_consistency(judgment: JudgmentResult) -> JudgmentResult:
    """Post-process a judgment to catch LLM self-contradictions.

    Fixes cases where the LLM says "reply_required" but sets priority=low,
    or where the recommendation has a time constraint but priority is low.
    """
    fd = judgment.final_decision
    bj = judgment.base_judgment
    changed = False

    # Rule 0: user_action fallback
    if not fd.user_action:
        fd.user_action = "review"

    # Rule 1: reply → at least medium
    if fd.user_action == "reply":
        if fd.priority in ("low", "ignore"):
            fd.priority = "medium"
            fd.should_show_in_main_result = True
            fd.should_show_in_lower_priority = False
            changed = True

    # Rule 2: deadline words in recommendation → at least medium
    rec = fd.user_facing_recommendation.lower()
    deadline_words = ("before", "by tomorrow", "by friday", "by thursday", "today", "asap", "tonight")
    if any(w in rec for w in deadline_words) and fd.priority == "low":
        fd.priority = "medium"
        fd.should_show_in_main_result = True
        fd.should_show_in_lower_priority = False
        changed = True

    # Rule 3: surface + low priority mismatch — if surface=true, priority can't be ignore
    if bj.should_surface and fd.priority == "ignore":
        fd.priority = "low"

    return judgment


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
    from .llm import call_llm_json_safe

    prompt = build_judgment_prompt(task_plan, strategy, mailbox_profile, candidate_context, snooze_prefs)

    strict_anna_sampling = sampling_create_message is not None
    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt="你是一个严格的 JSON 生成器，只输出有效 JSON，不输出解释或 markdown。",
        user_message=prompt,
        fallback={},
        temperature=0.1,
        max_tokens=20480,
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
    from .llm import call_llm_json_safe

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
            max_tokens=20480 if sampling_create_message is not None else min(4096, max(900, 450 * len(batch))),
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
