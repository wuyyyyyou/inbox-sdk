"""Mail agent pipeline orchestrator."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from typing import Any

_BEIJING_TZ = timezone(timedelta(hours=8))
_logger = logging.getLogger(__name__)


def _fmt_ts(epoch_ms: str) -> str:
    """Convert Gmail internalDate (epoch ms string) to human-readable Beijing time."""
    if not epoch_ms:
        return ""
    try:
        dt = datetime.fromtimestamp(int(epoch_ms) / 1000.0, tz=_BEIJING_TZ)
        return dt.strftime("%b %d, %Y %H:%M")
    except (ValueError, TypeError, OSError):
        return epoch_ms

from .candidate import generate_candidates
from .context import read_candidate_context
from .phase1 import run_phase1_batch_classify
from .guards import apply_rule_guards
from ..planning.intent import parse_intent
from ..judgment_engine.service import evaluate_item, evaluate_items_batch
from .plan import create_run_id, generate_action_plan
from .scan import build_scan_plan, run_mail_scan
from ..planning.strategies import get as get_strategy
from ..domain.types import (
    ActionPlan,
    CustomScanPlan,
    MailTaskInput,
    MailTaskPlan,
    MailboxProfile,
)

ProgressCallback = Callable[[str, dict[str, Any]], None]
EVALUATE_CONCURRENCY = 2
_PERSIST_LOCKS: dict[str, asyncio.Lock] = {}

# Storage integration (lazy import to avoid circular deps at module level)
_storage_available: bool | None = None


def _apply_incremental_window(scan_plan: dict[str, Any], last_internal_date: str) -> None:
    """为增量扫描设置 stop_at_internal_date，避免重复扫描已处理的旧邮件。"""
    if not last_internal_date:
        return
    queries = scan_plan.get("queries") if isinstance(scan_plan.get("queries"), list) else []
    for query in queries:
        if isinstance(query, dict):
            query["stop_at_internal_date"] = last_internal_date


def _storage_ready() -> bool:
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


def _dedupe_by_thread(messages: list[Any]) -> list[Any]:
    """每个线程保留一条消息，优先级：最新 INBOX > 最新 DRAFT > 丢弃。

    纯 SENT 线程（用户发出但无回复）直接丢弃——用户已处理过，不需要提醒。
    DRAFT 仅在没有 INBOX 时保留——表示用户开始写但未发送。

    Gmail 一个 thread 下同时包含 INBOX（收件）和 SENT（发件）消息，
    旧的按时间戳去重会让用户自己的 SENT 回复覆盖掉对方的最新 INBOX。
    """
    from collections import defaultdict

    thread_msgs: dict[str, list[Any]] = defaultdict(list)
    for m in messages:
        tid = m.thread_id or m.message_id
        thread_msgs[tid].append(m)

    def _max_by_date(msgs: list[Any]) -> Any:
        return max(msgs, key=lambda m: int(getattr(m, 'internal_date', '0') or 0))

    result: list[Any] = []
    for msgs in thread_msgs.values():
        inbox_msgs = [m for m in msgs if 'INBOX' in (getattr(m, 'label_ids', None) or [])]
        draft_msgs = [m for m in msgs if 'DRAFT' in (getattr(m, 'label_ids', None) or [])]

        if inbox_msgs:
            result.append(_max_by_date(inbox_msgs))
        elif draft_msgs:
            result.append(_max_by_date(draft_msgs))
        # else: 纯 SENT → 丢弃

    return result


def _thread_latest_is_from_owner(mailbox: str, thread_id: str, owner_email: str) -> bool | None:
    """通过 Gmail API 检查线程最新一封是否来自用户本人（即已回复过）。

    返回 True=已回复, False=未回复, None=检查失败（超时/网络错误）。
    """
    import re
    import urllib.parse
    import urllib.request
    from ..mail_providers.gmail.adapter import get_access_token, GMAIL_API_BASE

    try:
        token = get_access_token(mailbox)
        path = f"/users/me/threads/{urllib.parse.quote(thread_id, safe='')}"
        url = GMAIL_API_BASE + path + "?fields=messages(id,internalDate,payload/headers)"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        _logger.warning("thread_latest_is_from_owner failed for thread %s: %s", thread_id, exc)
        return None

    messages = data.get("messages") or []
    if not messages:
        return False

    latest = max(messages, key=lambda m: int(m.get("internalDate", "0") or 0))
    headers = (latest.get("payload") or {}).get("headers") or []

    from_addr = ""
    for h in headers:
        if (h.get("name") or "").lower() == "from":
            from_addr = h.get("value") or ""
            break

    match = re.search(r"<([^>]+)>", from_addr)
    if match:
        from_addr = match.group(1)

    return from_addr.strip().lower() == (owner_email or "").strip().lower()


async def _get_scan_plan_config(mailbox: str) -> Any | None:
    """读取用户的扫描计划配置。无配置或存储不可用时返回 None。"""
    if not _storage_ready():
        return None
    try:
        from ..storage.ops import get_scan_plan
        return await get_scan_plan(mailbox)
    except Exception:
        return None


async def run_mail_task(
    input_: MailTaskInput,
    *,
    sampling_create_message: Any,
    primary_count: int = 20,
    progress_callback: ProgressCallback | None = None,
) -> ActionPlan:
    """Run the full mail agent pipeline."""
    run_id = create_run_id()

    _report_progress(progress_callback, "parse_intent")
    task_plan = await parse_intent(input_, sampling_create_message)

    strategy = get_strategy(task_plan.strategy_mode)
    if not strategy:
        raise ValueError(f"Unknown strategy mode: {task_plan.strategy_mode}")

    mailbox_profile = MailboxProfile(mailbox_id=input_.mailbox_id, owner=input_.mailbox_id)
    scan_plan = build_scan_plan(task_plan, strategy)

    import re as _re

    # Read scan state and user config
    last_message_internal_date = ""
    is_first_scan = True
    if _storage_ready():
        try:
            from ..storage.ops import get_scan_state as _gss
            _st = await _gss(input_.mailbox_id)
            last_message_internal_date = _st.last_message_internal_date
            is_first_scan = _st.total_scans == 0
        except Exception:
            pass

    scan_plan_config = await _get_scan_plan_config(input_.mailbox_id)

    if scan_plan_config:
        window_days = scan_plan_config.scan_window_days
        configured_max = scan_plan_config.max_messages
    else:
        window_days = 7
        configured_max = 100

    # Stop at last processed message time (incremental scan)
    _apply_incremental_window(scan_plan, last_message_internal_date)

    # Apply scan window to Gmail queries
    queries = scan_plan.get("queries") if isinstance(scan_plan.get("queries"), list) else []
    for q in queries:
        if isinstance(q, dict):
            q_str = str(q.get("query") or "")
            q_str = _re.sub(r"newer_than:\d+d", f"newer_than:{window_days}d", q_str)
            # Inject extra scan categories (promotions, social, updates, forums)
            categories = scan_plan_config.scan_categories if scan_plan_config else []
            for cat in categories:
                if cat in ("promotions", "social", "updates", "forums"):
                    q_str += f" OR category:{cat} newer_than:{window_days}d"
            q["query"] = q_str

    budget = dict(scan_plan.get("budget", {}))
    requested_max = max(1, input_.max_messages or configured_max)
    budget["max_messages"] = min(budget.get("max_messages", requested_max), requested_max, configured_max)
    scan_plan["budget"] = budget

    _report_progress(progress_callback, "scan", max_messages=budget["max_messages"], window_days=window_days)
    _logger.info("scan started: mailbox=%s max_messages=%s window_days=%s", input_.mailbox_id, budget["max_messages"], window_days)
    messages = await run_mail_scan(input_.mailbox_id, scan_plan, progress_callback=progress_callback)
    _report_progress(progress_callback, "scan_done", scanned=len(messages), max_messages=budget["max_messages"])
    _logger.info("scan done: %d messages fetched", len(messages))

    # ── Storage: filter already-processed messages ─────────────────
    all_message_ids = [m.message_id for m in messages if m.message_id]
    new_message_ids = all_message_ids
    if _storage_ready() and all_message_ids:
        from ..storage.ops import filter_unprocessed
        new_message_ids = await filter_unprocessed(input_.mailbox_id, all_message_ids)
        skipped = len(all_message_ids) - len(new_message_ids)
        _report_progress(
            progress_callback,
            "storage_filter",
            total=len(all_message_ids),
            new=len(new_message_ids),
            skipped=skipped,
            partial={
                "brief_debug": {
                    "total_scanned_ids": len(all_message_ids),
                    "skipped_processed": skipped,
                    "new_after_processed": len(new_message_ids),
                }
            },
        )
        _logger.info("storage_filter: %d total, %d new, %d skipped", len(all_message_ids), len(new_message_ids), skipped)
    new_id_set = set(new_message_ids)
    new_messages = [m for m in messages if m.message_id in new_id_set]

    new_messages = _dedupe_by_thread(new_messages)
    _report_progress(progress_callback, "thread_dedup", before=len(new_id_set), after=len(new_messages))
    _logger.info("thread_dedup: %d messages → %d after dedup", len(new_id_set), len(new_messages))

    # 过滤已回复线程：最新一封来自用户本人 → 已处理过，跳过
    already_replied_count = 0
    check_timeouts = 0
    filtered_messages: list[Any] = []
    total_after_dedup = len(new_messages)
    for i, m in enumerate(new_messages):
        tid = m.thread_id or m.message_id
        _report_progress(progress_callback, "check_replied", current=i + 1, total=total_after_dedup)
        try:
            result = _thread_latest_is_from_owner(input_.mailbox_id, tid, input_.mailbox_id)
            if result is None:
                check_timeouts += 1
            elif result:
                already_replied_count += 1
                try:
                    from ..contact_memory.indexer import ingest_thread_observation
                    await ingest_thread_observation(
                        input_.mailbox_id,
                        tid,
                        m.from_addr,
                        m.subject,
                        sampling_create_message=sampling_create_message,
                        source="gmail_scan",
                        user_action="owner_replied",
                    )
                except Exception:
                    pass
                continue
        except Exception:
            _logger.warning("check_replied error for thread %s, keeping message", tid)
            check_timeouts += 1
        filtered_messages.append(m)
    _logger.info("already_replied_filter: %d filtered (already replied), %d timeouts, %d remaining",
                 already_replied_count, check_timeouts, len(filtered_messages))
    _report_progress(
        progress_callback,
        "already_replied_filter",
        filtered=already_replied_count,
        remaining=len(filtered_messages),
        timeout=check_timeouts,
    )
    new_messages = filtered_messages

    _report_progress(progress_callback, "phase1", scanned=len(new_messages))
    _logger.info("phase1 started: %d messages to classify", len(new_messages))
    phase1_result = await run_phase1_batch_classify(new_messages, strategy, mailbox_profile, sampling_create_message)
    candidates = phase1_result["candidates"]
    low_value_items = phase1_result["low_value_items"]
    _report_progress(
        progress_callback,
        "phase1_done",
        scanned=len(messages),
        candidates=len(candidates),
        low_value=len(low_value_items),
        partial={
            "brief_debug": {
                "candidates": len(candidates),
                "low_value": len(low_value_items),
            }
        },
    )
    _logger.info("phase1 done: %d candidates, %d low_value", len(candidates), len(low_value_items))

    contexts = []
    for index, candidate in enumerate(candidates, start=1):
        _report_progress(progress_callback, "read_context", current=index, total=len(candidates))
        ctx = await read_candidate_context(input_.mailbox_id, candidate)
        try:
            from ..contact_memory.retriever import contact_email_from_header, format_contact_context_for_prompt, retrieve_contact_context
            from ..contact_memory.types import ContactMemoryQuery
            contact_context = await retrieve_contact_context(ContactMemoryQuery(
                mailbox=input_.mailbox_id,
                contact_email=contact_email_from_header(str(candidate.evidence.get("from", ""))),
                current_subject=str(candidate.evidence.get("subject", "")),
                current_body=str(candidate.evidence.get("snippet", "")),
                current_thread_id=candidate.thread_id,
                purpose="card_generation",
            ), sampling_create_message=sampling_create_message)
            rendered_contact_context = format_contact_context_for_prompt(contact_context)
            if rendered_contact_context != "No relevant contact memory.":
                ctx.contact_context = rendered_contact_context
        except Exception:
            pass
        contexts.append(ctx)
    _report_progress(progress_callback, "read_context_done", total=len(contexts))
    _logger.info("read_context done: %d candidates", len(contexts))

    # Load snooze preferences once for both evaluation paths
    snooze_prefs = None
    if _storage_ready():
        try:
            from ..storage.ops import get_user_prefs
            prefs = await get_user_prefs()
            snooze_prefs = prefs.snooze
        except Exception:
            snooze_prefs = None

    if sampling_create_message is not None:
        _report_progress(
            progress_callback,
            "evaluate",
            current=0,
            total=len(contexts),
            evaluated=0,
            mode="anna_batch",
        )
        judgments = await evaluate_items_batch(
            task_plan=task_plan,
            strategy=strategy,
            mailbox_profile=mailbox_profile,
            candidate_contexts=contexts,
            sampling_create_message=sampling_create_message,
            max_sampling_calls=max(1, math.ceil(len(contexts) / 3)),
            snooze_prefs=snooze_prefs,
            progress_callback=progress_callback,
        )
        _report_progress(progress_callback, "evaluate_done", total=len(judgments), evaluated=len(judgments), mode="anna_batch")
        _report_progress(progress_callback, "plan", judgments=len(judgments))

        # Apply rule guards — Anna batch path must match DashScope single-item path
        for i, j in enumerate(judgments):
            judgments[i] = apply_rule_guards(j, strategy)

        if _storage_ready():
            try:
                await _persist_run_results(
                    run_id=run_id,
                    mailbox=input_.mailbox_id,
                    messages=messages,
                    candidates=candidates,
                    judgments=judgments,
                    strategy_mode=task_plan.strategy_mode,
                    user_request=input_.user_request,
                    mode=input_.mode if hasattr(input_, 'mode') else "auto",
                    low_value_items=low_value_items,
                    sampling_create_message=sampling_create_message,
                )
                _report_progress(progress_callback, "storage_saved")
            except Exception as exc:
                _report_progress(progress_callback, "storage_error", reason=str(exc)[:200])

        return generate_action_plan(run_id, task_plan, judgments)

    semaphore = asyncio.Semaphore(EVALUATE_CONCURRENCY)
    progress_lock = asyncio.Lock()
    evaluated_count = 0

    async def _evaluate_one(index: int, ctx: Any) -> Any:
        nonlocal evaluated_count
        async with semaphore:
            _report_progress(
                progress_callback,
                "evaluate",
                current=index,
                total=len(contexts),
                evaluated=evaluated_count,
                concurrency=EVALUATE_CONCURRENCY,
            )
            try:
                judgment = await evaluate_item(
                    task_plan=task_plan,
                    strategy=strategy,
                    mailbox_profile=mailbox_profile,
                    candidate_context=ctx,
                    sampling_create_message=sampling_create_message,
                    snooze_prefs=snooze_prefs,
                )
                judgment = apply_rule_guards(judgment, strategy)
            except Exception as exc:
                from ..judgment_engine.service import create_fallback_judgment
                judgment = create_fallback_judgment(
                    ctx.candidate.candidate_id,
                    strategy,
                    f"evaluation failed: {type(exc).__name__}: {exc}",
                )
            async with progress_lock:
                evaluated_count += 1
                _report_progress(
                    progress_callback,
                    "evaluate",
                    current=index,
                    total=len(contexts),
                    evaluated=evaluated_count,
                    concurrency=EVALUATE_CONCURRENCY,
                )
            return judgment

    if contexts:
        judgments = await asyncio.gather(*(_evaluate_one(index, ctx) for index, ctx in enumerate(contexts, start=1)))
    else:
        judgments = []
    _report_progress(progress_callback, "evaluate_done", total=len(judgments), evaluated=len(judgments))

    _report_progress(progress_callback, "plan", judgments=len(judgments))
    action_plan = generate_action_plan(run_id, task_plan, judgments)

    # ── Storage: persist results ──────────────────────────────────
    if _storage_ready():
        try:
            await _persist_run_results(
                run_id=run_id,
                mailbox=input_.mailbox_id,
                messages=messages,
                candidates=candidates,
                judgments=judgments,
                strategy_mode=task_plan.strategy_mode,
                user_request=input_.user_request,
                mode=input_.mode if hasattr(input_, 'mode') else "auto",
                low_value_items=low_value_items,
                sampling_create_message=sampling_create_message,
            )
            _report_progress(progress_callback, "storage_saved")
        except Exception as exc:
            _report_progress(progress_callback, "storage_error", reason=str(exc)[:200])

    return action_plan


_EXECUTION_SYSTEM_PROMPT = """You are Anna, an executive email assistant. The mailbox owner is your principal — address them directly as "you" in all title, summary, context, and suggestion text. Analyze the emails below based on the task instructions. Output a single JSON object — the very first character of your response MUST be `{`. Do NOT wrap the JSON in markdown fences.

## Output format
{
  "title": "Brief answer title (<=12 words)",
  "summary": "One or two sentences summarizing the key finding",
  "sections": [
    {
      "heading": "Section heading",
      "body": "Narrative text (optional — for timeline, explanation, context)",
      "items": [
        {
          "subject": "Email subject or person name",
          "from": "Sender email address copied from email data",
          "context": "What this is about (verifiable facts)",
          "suggestion": "What to do (optional)",
          "draft": "Draft reply text — REQUIRED when the user asked for reply drafts. Every item with a message_id must include a draft in that case. Otherwise omit.",
          "message_id": "Gmail message ID copied from email data (e.g. 19e62c874f8b17df)",
          "thread_id": "Gmail thread ID copied from email data",
          "reply_gaps": {"needs_user_input": false, "summary": "", "questions": []}
        }
      ]
    }
  ]
}

## Field guide
- title: concise answer to the user's question
- summary: 1-2 sentences of the most important finding. If nothing found, say so honestly.
- sections: 1-4 focused groups. Each covers one topic, person, or action category.
  - heading: short label for this group
  - body: optional narrative (timeline, explanation). Omit if items communicate enough.
  - items: specific emails, people, or actions. Use context for verifiable facts, suggestion for next steps.
  - draft: ONLY include when the user explicitly asked for reply drafts.
  - reply_gaps: ONLY include for items that need a reply. When the email requires a response but you lack key information that only the user knows (e.g. their availability, opinions, preferences, specific dates, budget), ask clarifying questions instead of guessing. Set needs_user_input=true and provide 1-4 specific, answerable questions. Each question should be a single sentence. If you have enough information to draft a reply confidently, set needs_user_input=false and omit questions.
- message_id: copy from the "Message ID:" field in the email data. REQUIRED when draft is present — the user needs this to send or act on the email. Only omit for pure informational items without a draft.
- thread_id: copy from the "Thread:" field in the email data. REQUIRED when draft is present.
- from: copy from the "From:" field in the email data. REQUIRED when draft is present — this is the reply recipient.
- Use "body" alone for narrative answers (no items needed).
- Use "items" alone for lists (no body needed).
- Include "suggestion" when the user needs to decide or act.

## Rules
- Base your answer ONLY on the emails provided. If nothing matches, say so in summary.
- Use verifiable facts from the emails — no speculation, no AI reasoning.
- Be specific in suggestions. Avoid generic 'evaluate and respond.'
- NEVER fill in reply_gaps.questions with information you can verify from the emails — only ask about what you genuinely cannot know.
- CRITICAL — Time format: NEVER use relative time words. ALWAYS use "Mon DD, YYYY" format. Examples: "May 28, 2026", "Jan 3, 2026". If time of day matters, append it: "May 28, 2026, 2:30 PM". If no date is available, say "recently"."""


async def run_custom_scan(
    plan: CustomScanPlan,
    mailbox: str,
    *,
    sampling_create_message: Any,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Ask Agent：搜邮件 → 按需读 → 一次 LLM → 答案。

    与 Brief 管线完全解耦。不走 phase1/judgment/guards/cards。
    执行 LLM 根据 plan.task_prompt 直接完成分析和输出。
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

    # 1. Search
    scan_plan = {
        "queries": list(plan.gmail_queries),
        "budget": plan.scan_budget or {"max_messages": 200, "max_threads": 100},
    }
    messages = await run_mail_scan(mailbox, scan_plan, progress_callback=progress_callback)
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

    # 2. Read emails at the depth the planner chose
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

    # 3. One LLM call with task_prompt
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
        # 中文注释：执行阶段会多次降载重试，只替换邮件正文渲染，保持任务约束一致。
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
                system_prompt=_EXECUTION_SYSTEM_PROMPT,
                user_message=_build_user_prompt(rendered_emails),
                fallback={"title": "Scan failed", "summary": "Unable to analyze emails.", "sections": []},
                temperature=0.2,
                max_tokens=8192,
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
        # 中文注释：Anna 多轮降载仍失败时，返回可展示结果，避免 Ask 链路整体失败。
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

    # 4. Persist run record
    if _storage_ready():
        try:
            await _persist_run_results(
                run_id=create_run_id(),
                mailbox=mailbox,
                messages=messages,
                candidates=[],
                judgments=[],
                strategy_mode="custom",
                user_request=plan.user_request,
                mode="custom",
                plan_id=plan.plan_id,
                sampling_create_message=sampling_create_message,
            )
        except Exception:
            pass

    return payload


def _render_emails_for_llm(
    email_data: list[dict[str, Any]],
    read_depth: str,
    *,
    body_limit: int = 4000,
    thread_body_limit: int = 2000,
    max_thread_messages: int = 20,
) -> str:
    """Render email data as compact text for the LLM prompt."""
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


async def _persist_run_results(
    *,
    run_id: str,
    mailbox: str,
    messages: list[Any],
    candidates: list[Any],
    judgments: list[Any],
    strategy_mode: str,
    user_request: str,
    mode: str,
    plan_id: str = "",
    low_value_items: list[dict[str, Any]] | None = None,
    sampling_create_message: Any = None,
) -> None:
    lock = _PERSIST_LOCKS.setdefault(mailbox, asyncio.Lock())
    async with lock:
        return await _persist_run_results_locked(
            run_id=run_id, mailbox=mailbox, messages=messages,
            candidates=candidates, judgments=judgments,
            strategy_mode=strategy_mode, user_request=user_request, mode=mode,
            plan_id=plan_id, low_value_items=low_value_items,
            sampling_create_message=sampling_create_message,
        )


async def _persist_run_results_locked(
    *,
    run_id: str,
    mailbox: str,
    messages: list[Any],
    candidates: list[Any],
    judgments: list[Any],
    strategy_mode: str,
    user_request: str,
    mode: str,
    plan_id: str = "",
    low_value_items: list[dict[str, Any]] | None = None,
    sampling_create_message: Any = None,
) -> None:
    from ..storage.ops import (
        mark_messages_processed_batch,
        save_run_record,
        append_run_history,
        get_scan_state,
        get_active_cards,
        set_active_cards,
        set_scan_state,
    )
    from ..storage.types import (
        ProcessedMessage,
        RunRecord,
        RunHistoryEntry,
        ScanState,
        _now,
    )
    from ..cards.service import build_card, build_action_memo, build_cleanup_bundle, cards_to_frontend, merge_cards

    # 1. Mark ALL scanned messages as processed
    processed_msgs: list[ProcessedMessage] = []
    candidate_msg_ids: set[str] = {c.message_ids[0] for c in candidates if c.message_ids}
    candidate_map: dict[str, Any] = {}
    judgment_map: dict[str, Any] = {}
    for c in candidates:
        if c.message_ids:
            candidate_map[c.message_ids[0]] = c
    for j in judgments:
        candidate_map_j = {c.message_ids[0]: c for c in candidates if c.message_ids}
        judgment_map[j.candidate_id] = j
    # Build judgment lookup by candidate's message_id
    j_by_msg: dict[str, Any] = {}
    for c in candidates:
        if c.message_ids:
            j_by_msg[c.message_ids[0]] = c.candidate_id
    j_by_cand: dict[str, Any] = {j.candidate_id: j for j in judgments}

    for msg in messages:
        if not msg.message_id:
            continue
        is_candidate = msg.message_id in candidate_msg_ids
        pm = ProcessedMessage(
            message_id=msg.message_id,
            thread_id=msg.thread_id or "",
            from_addr=msg.from_addr or "",
            subject=msg.subject or "",
            snippet=msg.snippet or "",
            internal_date=msg.internal_date or "",
            processed_at=_now(),
            run_id=run_id,
            is_candidate=is_candidate,
            candidate_kind="",
            priority="low",
            read_depth="header_only",
        )
        if is_candidate:
            cand_id = j_by_msg.get(msg.message_id, "")
            c = next((cc for cc in candidates if cc.message_ids and cc.message_ids[0] == msg.message_id), None)
            j = j_by_cand.get(cand_id)
            if c:
                pm.candidate_kind = c.kind or ""
                pm.priority = c.priority_hint or "low"
                pm.read_depth = c.read_depth_required or "header_only"
            if j:
                pm.priority = j.final_decision.priority or pm.priority
                pm.confidence = j.confidence
        processed_msgs.append(pm)

    await mark_messages_processed_batch(mailbox, processed_msgs)

    # 2. Build cards from candidates + judgments
    msg_map: dict[str, Any] = {m.message_id: m for m in messages if m.message_id}
    new_cards = []
    for c in candidates:
        if not c.message_ids:
            continue
        mid = c.message_ids[0]
        msg = msg_map.get(mid)
        j = j_by_cand.get(c.candidate_id)
        if j:
            card = build_card(c, j, msg, mailbox)
            new_cards.append(card)

    # 2a. Build cleanup bundle from Phase 1 low-value items
    if low_value_items:
        cleanup_card = build_cleanup_bundle(run_id, mailbox, low_value_items, messages)
        if cleanup_card:
            new_cards.append(cleanup_card)

    # 3. Merge with existing active cards
    existing = await get_active_cards(mailbox)
    merged = merge_cards(existing, new_cards)
    await set_active_cards(mailbox, merged)
    try:
        from ..contact_memory.indexer import ingest_card_event, ingest_thread_observation
        card_thread_ids = {card.thread_id for card in new_cards if getattr(card, "thread_id", "")}
        for card in new_cards:
            if getattr(card, "card_type", "") != "cleanup_bundle":
                await ingest_card_event(
                    mailbox,
                    card,
                    event_type="card_created",
                    source="gmail_scan",
                    sampling_create_message=sampling_create_message,
                )
        for candidate in candidates:
            if candidate.thread_id and candidate.thread_id not in card_thread_ids:
                await ingest_thread_observation(
                    mailbox,
                    candidate.thread_id,
                    str(candidate.evidence.get("from", "")),
                    str(candidate.evidence.get("subject", "")),
                    sampling_create_message=sampling_create_message,
                    source="gmail_scan",
                    user_action="message_seen",
                )
    except Exception as exc:
        _logger.warning("contact_memory_ingest_failed mailbox=%s error=%s", mailbox, exc)

    # 4. Save run record
    cards_summary = cards_to_frontend(merged)
    scanned_count = len(messages)
    candidate_count = len(candidates)
    main_count = sum(1 for j in judgments if j.final_decision.should_show_in_main_result)
    lower_count = sum(1 for j in judgments if j.final_decision.should_show_in_lower_priority)

    run = RunRecord(
        run_id=run_id,
        mailbox=mailbox,
        strategy_mode=strategy_mode,
        user_request=user_request,
        mode=mode,
        plan_id=plan_id,
        scanned_count=scanned_count,
        candidate_count=candidate_count,
        main_count=main_count,
        lower_count=lower_count,
        summary=[f"Scanned {scanned_count} messages, found {candidate_count} candidates."],
        strategy=[f"Strategy: {strategy_mode}"],
        cards=[{"id": c["id"], "title": c["title"]} for c in cards_summary],
    )
    await save_run_record(mailbox, run)

    # 5. Update scan state
    previous_state = await get_scan_state(mailbox)
    def _internal_date_sort_key(value: str) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    latest_internal_date = max(
        (str(m.internal_date) for m in messages if getattr(m, "internal_date", "")),
        default=previous_state.last_message_internal_date,
        key=_internal_date_sort_key,
    )
    scan_state = ScanState(
        mailbox=mailbox,
        last_scan_ts=_now(),
        last_message_internal_date=latest_internal_date,
        total_scans=previous_state.total_scans + 1,
        total_processed=previous_state.total_processed + len(processed_msgs),
    )
    await set_scan_state(mailbox, scan_state)

    # 6. Append run history (cross-mailbox)
    history_entry = RunHistoryEntry(
        run_id=run_id,
        mailbox=mailbox,
        ts=_now(),
        request=user_request[:100],
        mode=mode,
        strategy=strategy_mode,
        plan_id=plan_id,
        result=f"{main_count} main, {lower_count} lower, {len(cards_summary)} cards",
        summary=f"Scanned {scanned_count}, candidates {candidate_count}, main {main_count}, lower {lower_count}",
    )
    await append_run_history(history_entry)


__all__ = [
    "run_mail_task",
    "MailTaskInput",
    "MailTaskPlan",
    "ActionPlan",
    "asdict",
    "generate_candidates",
]
