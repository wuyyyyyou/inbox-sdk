from __future__ import annotations

from anna_inbox_executa.common import *


def _clamp_int(value: Any, fallback: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return min(max_value, max(min_value, parsed))
from anna_inbox_executa.gmail_tools import *
from anna_inbox_executa.sampling_tools import *

async def run_mail_agent_pipeline(
    *,
    user_request: str,
    mailbox: str,
    mode: str,
    max_messages: int,
    primary_count: int = 20,
    ai_provider: str = "anna-llm",
    invoke_id: str,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """Run the full mail agent pipeline."""
    from dataclasses import asdict
    from mail_agent.llm_runtime.service import dashscope_available
    from mail_agent.core.pipeline import run_mail_task
    from mail_agent.domain.types import MailTaskInput

    provider = str(ai_provider or "anna-llm").strip()
    invoke_id = invoke_id or f"local_{uuid.uuid4().hex}"
    log(f"mail-agent pipeline start: mailbox={mailbox} mode={mode} provider={provider} request={user_request[:80]} primary_count={primary_count}")
    if provider == "dashscope" and not dashscope_available():
        raise RuntimeError("DASHSCOPE_API_KEY is not set; DashScope provider cannot run.")
    if progress_callback is None:
        progress_callback = lambda stage, progress: log(f"mail-agent progress: {stage} {json.dumps(progress, ensure_ascii=False)}")

    input_ = MailTaskInput(
        user_request=str(user_request or "").strip(),
        mailbox_id=str(mailbox or "").strip(),
        user_email=str(mailbox or "").strip(),
        mode=mode if mode else "auto",
        max_messages=max_messages,
        dry_run=True,
    )

    if not input_.user_request or not input_.mailbox_id:
        return {"success": False, "error": "user_request and mailbox are required"}

    sampling_create_message = _build_sampling_for_run(
        {"ai_provider": provider}, invoke_id
    )

    action_plan = await run_mail_task(
        input_,
        sampling_create_message=sampling_create_message,
        primary_count=primary_count,
        progress_callback=progress_callback,
    )

    # Convert to serializable dict
    def _serialize(obj: Any) -> Any:
        if hasattr(obj, "__dataclass_fields__"):
            return {k: _serialize(v) for k, v in asdict(obj).items()}
        if isinstance(obj, list):
            return [_serialize(item) for item in obj]
        if isinstance(obj, dict):
            return {k: _serialize(v) for k, v in obj.items()}
        return obj

    log(f"mail-agent pipeline done: strategy={action_plan.strategy_mode} main={len(action_plan.main_items)} lower={len(action_plan.lower_priority_items)}")

    # Read active cards from storage for V2 frontend
    active_cards: list[dict[str, Any]] = []
    if progress_callback:
        progress_callback("reading_cards", {"mailbox": input_.mailbox_id, "main_items": len(action_plan.main_items)})
    try:
        from mail_agent.storage.client import is_ready
        if is_ready():
            from mail_agent.storage.ops import get_active_cards
            from mail_agent.cards.service import cards_to_frontend
            stored = await get_active_cards(input_.mailbox_id)
            active_cards = cards_to_frontend(stored)
    except Exception as exc:
        log(f"pipeline get_active_cards failed: {type(exc).__name__}: {exc}")
        if progress_callback:
            progress_callback("read_cards_error", {"reason": f"{type(exc).__name__}: {exc}"[:200]})

    return {
        "success": True,
        "tool": "run_mail_agent",
        "action_plan": _serialize(action_plan),
        "cards": active_cards,
        "meta": {
            "run_id": action_plan.run_id,
            "strategy_mode": action_plan.strategy_mode,
            "main_items": len(action_plan.main_items),
            "lower_priority_items": len(action_plan.lower_priority_items),
            "proposed_actions": len(action_plan.proposed_actions),
            "approval_memo": action_plan.approval_memo,
            "llm_provider": provider,
        },
    }


def _merge_partial(run_id: str, partial_update: dict[str, Any]) -> None:
    target = MAIL_AGENT_RUNS[run_id].setdefault("partial", {})
    for key, value in partial_update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            target[key].update(value)
        else:
            target[key] = value


async def run_mail_agent_background(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    """Warm Gmail cache only — do NOT run the full pipeline.

    The real pipeline runs in continue_mail_agent_run (blocking invoke with
    live sampling token). This background task just fetches messages into the
    local cache so the blocking invoke's scan phase is fast.
    """
    MAIL_AGENT_RUNS[run_id]["status"] = "running"
    MAIL_AGENT_RUNS[run_id]["stage"] = "scan"
    MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
    _save_run_checkpoint(run_id)

    try:
        from mail_agent.core.scan import build_scan_plan, run_mail_scan
        from mail_agent.core.pipeline import _get_scan_plan_config
        from mail_agent.planning.strategies import get as get_strategy
        from mail_agent.planning.intent import parse_intent
        from mail_agent.domain.types import MailTaskInput, MailTaskPlan

        mode = arguments.get("mode", "auto")
        mailbox = arguments.get("mailbox", "")
        max_messages = arguments.get("max_messages", 50)
        primary_count = arguments.get("primary_count", 30)

        if mode and mode != "auto":
            strategy = get_strategy(mode)
        else:
            strategy = get_strategy("default_secretary")
        if strategy:
            input_ = MailTaskInput(
                user_request=arguments.get("user_request", ""),
                mailbox_id=mailbox,
                user_email=mailbox,
                mode=mode,
                max_messages=max_messages,
                dry_run=True,
            )
            task_plan = MailTaskPlan(
                strategy_mode=strategy.id,
                user_request=input_.user_request,
                scope={},
            )
            scan_plan = build_scan_plan(task_plan, strategy)
            budget = scan_plan.get("budget", {})
            budget["max_messages"] = min(budget.get("max_messages", 100), max_messages)
            scan_plan["budget"] = budget
            scan_plan_config = await _get_scan_plan_config(mailbox)
            scan_max_messages = _clamp_int(scan_plan_config.max_messages if scan_plan_config else arguments.get("max_messages", budget.get("max_messages") or max_messages), 100, 10, 500)
            scan_window_days = _clamp_int(scan_plan_config.scan_window_days if scan_plan_config else arguments.get("scan_window_days", 7), 7, 1, 90)
            scanned = await run_mail_scan(mailbox, scan_max_messages, newer_than_days=scan_window_days)
            MAIL_AGENT_RUNS[run_id]["progress"] = {"scanned": len(scanned), "scan_window_days": scan_window_days}
        MAIL_AGENT_RUNS[run_id].update({
            "status": "done",
            "stage": "done",
            "updated_at": beijing_now(),
        })
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update({
            "status": "failed",
            "stage": "failed",
            "updated_at": beijing_now(),
            "error": str(exc),
        })
    _save_run_checkpoint(run_id)

def start_mail_agent_run(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    # 接收前端预生成的 run_id，便于后续 continue 调用期间轮询同一个运行状态。
    run_id = str(arguments.get("run_id") or "").strip()
    if not run_id or len(run_id) < 8:
        run_id = f"bg_{uuid.uuid4().hex[:12]}"
    MAIL_AGENT_RUNS[run_id] = {
        "run_id": run_id,
        "status": "queued",
        "stage": "queued",
        "progress": {},
        "warnings": [],
        "started_at": beijing_now(),
        "updated_at": beijing_now(),
        "result": None,
        "error": "",
        "partial": {},
        "needs_continue": True,
        "cards_added": 0,
        "brief": {
            "stage": "queued",
            "cards_version": 0,
            "messages": [],
            "phase1_cursor": 0,
            "phase1_batch_size": 20,
            "candidates": [],
            "low_value_items": [],
            "phase2_cursor": 0,
            "judgments": [],
        },
    }
    _save_run_checkpoint(run_id)
    # 保存参数给 continue_mail_agent_run 复用；不在这个 invoke 里启动 LLM。
    MAIL_AGENT_RUNS[run_id]["partial"]["_args"] = dict(arguments)
    return {
        "success": True,
        "run_id": run_id,
        "status": "queued",
        "stage": MAIL_AGENT_RUNS[run_id]["stage"],
        "progress": MAIL_AGENT_RUNS[run_id]["progress"],
        "started_at": MAIL_AGENT_RUNS[run_id]["started_at"],
    }


def _brief_messages_from_dict(items: list[dict[str, Any]]) -> list[Any]:
    from mail_agent.domain.types import MessageLite

    return [
        MessageLite(
            message_id=str(item.get("message_id") or ""),
            thread_id=str(item.get("thread_id") or ""),
            from_addr=str(item.get("from_addr") or ""),
            to_addr=str(item.get("to_addr") or ""),
            cc=str(item.get("cc") or ""),
            subject=str(item.get("subject") or ""),
            snippet=str(item.get("snippet") or ""),
            internal_date=str(item.get("internal_date") or ""),
            label_ids=list(item.get("label_ids") or []),
            unread=bool(item.get("unread")),
            starred=bool(item.get("starred")),
            important=bool(item.get("important")),
            has_attachment=bool(item.get("has_attachment")),
            attachments=list(item.get("attachments") or []),
            headers=dict(item.get("headers") or {}),
        )
        for item in items
        if isinstance(item, dict)
    ]


def _brief_candidates_from_dict(items: list[dict[str, Any]]) -> list[Any]:
    from mail_agent.domain.types import CandidateItem

    return [
        CandidateItem(
            candidate_id=str(item.get("candidate_id") or ""),
            kind=item.get("kind") or "unsure",
            message_ids=list(item.get("message_ids") or []),
            thread_id=str(item.get("thread_id") or ""),
            evidence=dict(item.get("evidence") or {}),
            priority_hint=item.get("priority_hint") or "unknown",
            read_depth_required=item.get("read_depth_required") or "header_only",
            source=item.get("source") or "rule",
            confidence=float(item.get("confidence") or 0.5),
        )
        for item in items
        if isinstance(item, dict)
    ]


def _brief_judgments_from_dict(items: list[dict[str, Any]]) -> list[Any]:
    from mail_agent.domain.types import BaseJudgment, FinalDecision, JudgmentResult

    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        result.append(JudgmentResult(
            candidate_id=str(item.get("candidate_id") or ""),
            strategy_mode=item.get("strategy_mode") or "default_secretary",
            base_judgment=BaseJudgment(**dict(item.get("base_judgment") or {})),
            mode_judgment=dict(item.get("mode_judgment") or {}),
            final_decision=FinalDecision(**dict(item.get("final_decision") or {})),
            confidence=float(item.get("confidence") or 0.5),
        ))
    return result


def _brief_to_dict_list(items: list[Any]) -> list[dict[str, Any]]:
    from dataclasses import asdict, is_dataclass

    result: list[dict[str, Any]] = []
    for item in items:
        if is_dataclass(item):
            result.append(asdict(item))
        elif isinstance(item, dict):
            result.append(item)
    return result


def _brief_count_values(items: list[Any], attr: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = ""
        if isinstance(item, dict):
            value = str(item.get(attr) or "")
        else:
            value = str(getattr(item, attr, "") or "")
        if not value:
            value = "unknown"
        counts[value] = counts.get(value, 0) + 1
    return counts


def _brief_count_judgment_priorities(items: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        priority = ""
        if isinstance(item, dict):
            fd = item.get("final_decision") if isinstance(item.get("final_decision"), dict) else {}
            priority = str(fd.get("priority") or "")
        else:
            priority = str(getattr(getattr(item, "final_decision", None), "priority", "") or "")
        if not priority:
            priority = "unknown"
        counts[priority] = counts.get(priority, 0) + 1
    return counts


def _brief_count_judgment_fallbacks(items: list[Any]) -> int:
    count = 0
    for item in items:
        mode = {}
        if isinstance(item, dict):
            mode = item.get("mode_judgment") if isinstance(item.get("mode_judgment"), dict) else {}
        else:
            mode = getattr(item, "mode_judgment", None) if isinstance(getattr(item, "mode_judgment", None), dict) else {}
        if mode.get("fallback_reason"):
            count += 1
    return count


def _brief_completed_candidate_ids(items: list[Any]) -> set[str]:
    completed: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            candidate_id = str(item.get("candidate_id") or "")
        else:
            candidate_id = str(getattr(item, "candidate_id", "") or "")
        if candidate_id:
            completed.add(candidate_id)
    return completed


def _brief_phase2_contiguous_cursor(candidates: list[Any], completed_ids: set[str]) -> int:
    cursor = 0
    for candidate in candidates:
        candidate_id = str(getattr(candidate, "candidate_id", "") or "")
        if candidate_id and candidate_id in completed_ids:
            cursor += 1
            continue
        break
    return cursor


def _brief_phase2_prompt_profile(candidate: Any, context_type: str) -> str:
    phase1_action = str((getattr(candidate, "evidence", {}) or {}).get("user_action") or "").strip().lower()
    candidate_kind = str(getattr(candidate, "kind", "") or "")
    priority_hint = str(getattr(candidate, "priority_hint", "") or "")
    if phase1_action == "review" and candidate_kind in {"safe_account_record", "safe_cleanup_bundle"}:
        return "compact_review"
    if phase1_action == "review" and candidate_kind == "account_notice_possible" and priority_hint in {"low", "unknown"}:
        return "compact_review"
    if phase1_action == "reply":
        return f"reply_{context_type or 'unknown'}"
    return f"full_{context_type or 'unknown'}"


def _brief_update_state(run_id: str, *, status: str = "running", stage: str, progress: dict[str, Any], cards_added: int = 0, needs_continue: bool = True) -> None:
    state = MAIL_AGENT_RUNS[run_id]
    state["status"] = status
    state["stage"] = stage
    state["progress"] = progress
    state["cards_added"] = cards_added
    state["needs_continue"] = needs_continue
    state["updated_at"] = beijing_now()
    _save_run_checkpoint(run_id)


async def _brief_prepare_scan(run_id: str, arguments: dict[str, Any]) -> None:
    from mail_agent.core.pipeline import _dedupe_by_thread, _get_scan_plan_config, _storage_ready
    from mail_agent.core.scan import run_mail_scan
    from mail_agent.domain.types import MailTaskInput
    from mail_agent.planning.intent import parse_intent
    from mail_agent.planning.strategies import get as get_strategy

    state = MAIL_AGENT_RUNS[run_id]
    brief = state.setdefault("brief", {})
    user_request = str(arguments.get("user_request") or "")
    mailbox = str(arguments.get("mailbox") or "")
    mode = str(arguments.get("mode") or "auto")
    max_messages = _clamp_int(arguments.get("max_messages"), 50, 10, 500)

    _brief_update_state(run_id, stage="scan", progress={"current": 0, "total": max_messages, "mailbox": mailbox})
    input_ = MailTaskInput(user_request=user_request, mailbox_id=mailbox, user_email=mailbox, mode=mode, max_messages=max_messages, dry_run=True)
    task_plan = await parse_intent(input_, None)
    strategy = get_strategy(task_plan.strategy_mode)
    if not strategy:
        raise ValueError(f"Unknown strategy mode: {task_plan.strategy_mode}")

    scan_plan_config = await _get_scan_plan_config(mailbox)
    configured_max = _clamp_int(scan_plan_config.max_messages if scan_plan_config else arguments.get("max_messages"), max_messages, 10, 500)
    scan_window_days = _clamp_int(scan_plan_config.scan_window_days if scan_plan_config else arguments.get("scan_window_days"), 7, 1, 90)

    sync_summary: dict[str, Any] = {}
    if _storage_ready():
        _brief_update_state(run_id, stage="sync_gmail_state", progress={"mailbox": mailbox, "current": 0, "total": 1})
        try:
            from mail_agent.sync.gmail_status import reconcile_active_cards_with_gmail
            sync_summary = await reconcile_active_cards_with_gmail(mailbox, reason="scan_start")
            brief["gmail_sync"] = sync_summary
            _brief_update_state(run_id, stage="sync_gmail_state", progress={
                "mailbox": mailbox,
                "checked_threads": sync_summary.get("checked_threads", 0),
                "resolved_replied": sync_summary.get("resolved_replied", 0),
                "removed_missing": sync_summary.get("removed_missing", 0),
            })
        except Exception as exc:
            sync_summary = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            brief["gmail_sync"] = sync_summary
            log(f"brief gmail sync failed: {type(exc).__name__}: {exc}")

    last_message_internal_date = ""
    is_first_scan = True
    if _storage_ready():
        try:
            from mail_agent.storage.ops import get_scan_state
            scan_state = await get_scan_state(mailbox)
            last_message_internal_date = str(scan_state.last_message_internal_date or "")
            is_first_scan = int(scan_state.total_scans or 0) == 0
        except Exception as exc:
            log(f"brief scan_state read failed: {type(exc).__name__}: {exc}")

    scan_started = time.time()
    after_timestamp = "" if is_first_scan else last_message_internal_date
    scan_base_progress = {
        "current": 0,
        "total": configured_max,
        "mailbox": mailbox,
        "scan_window_days": scan_window_days,
        "incremental": bool(after_timestamp),
        "after_timestamp": after_timestamp[:20],
    }
    _brief_update_state(run_id, stage="scan", progress=scan_base_progress)

    def _scan_progress(stage: str, progress: dict[str, Any]) -> None:
        merged = dict(scan_base_progress)
        merged.update(progress or {})
        _brief_update_state(run_id, stage=stage, progress=merged)

    messages = await run_mail_scan(
        mailbox,
        configured_max,
        newer_than_days=scan_window_days,
        after_timestamp=after_timestamp,
        progress_callback=_scan_progress,
    )
    scan_elapsed_ms = int((time.time() - scan_started) * 1000)
    all_message_ids = [m.message_id for m in messages if m.message_id]
    new_message_ids = all_message_ids
    if _storage_ready() and all_message_ids:
        from mail_agent.storage.ops import filter_unprocessed
        new_message_ids = await filter_unprocessed(mailbox, all_message_ids)
    new_id_set = set(new_message_ids)
    new_messages = [m for m in messages if m.message_id in new_id_set]

    brief.update({
        "stage": "phase1",
        "strategy_mode": task_plan.strategy_mode,
        "task_plan": _brief_to_dict_list([task_plan])[0],
        "messages": _brief_to_dict_list(new_messages),
        "phase1_cursor": 0,
        "phase1_batch_size": 20,
        "candidates": [],
        "low_value_items": [],
        "phase2_cursor": 0,
        "judgments": [],
    })
    _brief_update_state(
        run_id,
        stage="phase1",
        progress={
            "current": 0,
            "total": len(new_messages),
            "scanned": len(messages),
            "new": len(new_message_ids),
            "skipped": max(0, len(all_message_ids) - len(new_message_ids)),
            "new_threads": len(_dedupe_by_thread(new_messages)),
            "scan_elapsed_ms": scan_elapsed_ms,
            "scan_window_days": scan_window_days,
            "incremental": bool(after_timestamp),
            "after_timestamp": after_timestamp[:20],
            "gmail_sync": sync_summary,
        },
    )


async def _brief_run_phase1_slice(run_id: str, sampling_create_message: Any) -> None:
    from mail_agent.core.phase1 import _run_phase1_single_batch
    from mail_agent.domain.types import MailboxProfile
    from mail_agent.planning.strategies import get as get_strategy

    state = MAIL_AGENT_RUNS[run_id]
    brief = state.setdefault("brief", {})
    messages = _brief_messages_from_dict(list(brief.get("messages") or []))
    cursor = int(brief.get("phase1_cursor") or 0)
    batch_size = int(brief.get("phase1_batch_size") or 20)
    total = len(messages)
    strategy = get_strategy(str(brief.get("strategy_mode") or "default_secretary"))
    if not strategy:
        raise ValueError(f"Unknown strategy mode: {brief.get('strategy_mode')}")
    profile = MailboxProfile(mailbox_id=str((state.get("partial") or {}).get("_args", {}).get("mailbox") or ""), owner=str((state.get("partial") or {}).get("_args", {}).get("mailbox") or ""))

    if cursor >= total:
        brief["stage"] = "check_replied"
        _brief_update_state(run_id, stage="check_replied", progress={"current": 0, "total": len(brief.get("candidates") or [])})
        return

    batches = []
    batch_total = max(1, (total + batch_size - 1) // batch_size)
    for start in range(cursor, min(total, cursor + batch_size * 4), batch_size):
        batches.append((start, messages[start:start + batch_size]))
    _brief_update_state(run_id, stage="phase1", progress={"current": cursor, "total": total, "batch_count": len(batches), "batch_size": batch_size})
    async def _run_one_phase1(start: int, batch: list[Any]) -> dict[str, Any]:
        try:
            return await _run_phase1_single_batch(batch, strategy, profile, sampling_create_message, batch_index=(start // batch_size) + 1, batch_total=batch_total)
        except Exception:
            from mail_agent.core.candidate import generate_candidates
            return {"candidates": generate_candidates(batch, strategy, profile), "low_value_items": []}

    results = await asyncio.gather(*[_run_one_phase1(start, batch) for start, batch in batches])

    candidates = _brief_candidates_from_dict(list(brief.get("candidates") or []))
    low_value_items = list(brief.get("low_value_items") or [])
    for result in results:
        candidates.extend(result.get("candidates") or [])
        low_value_items.extend(result.get("low_value_items") or [])
    phase1_metrics: dict[str, int] = {
        "phase1_input": 0,
        "phase1_prefiltered": 0,
        "phase1_rule_candidates": 0,
        "phase1_llm_messages": 0,
    }
    for result in results:
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        for key in phase1_metrics:
            phase1_metrics[key] += int(metrics.get(key) or 0)

    deduped: dict[str, Any] = {}
    for candidate in candidates:
        key = candidate.thread_id or (candidate.message_ids[0] if candidate.message_ids else candidate.candidate_id)
        deduped[key] = candidate
    cursor = min(total, cursor + batch_size * len(batches))
    brief["phase1_cursor"] = cursor
    brief["candidates"] = _brief_to_dict_list(list(deduped.values()))
    brief["low_value_items"] = low_value_items
    if cursor >= total:
        brief["stage"] = "check_replied"
        stage = "phase1_done"
    else:
        stage = "phase1"
    _brief_update_state(
        run_id,
        stage=stage,
        progress={
            "current": cursor,
            "total": total,
            "candidates": len(brief["candidates"]),
            "low_value": len(low_value_items),
            "sampling_calls_used": sum(1 for result in results if int(((result.get("metrics") if isinstance(result.get("metrics"), dict) else {}) or {}).get("phase1_llm_messages") or 0) > 0),
            "phase1_priority_hint_distribution": _brief_count_values(brief["candidates"], "priority_hint"),
            **phase1_metrics,
        },
    )


async def _brief_check_replied_after_phase1(run_id: str) -> None:
    from mail_agent.core.pipeline import _thread_latest_is_from_owner

    state = MAIL_AGENT_RUNS[run_id]
    brief = state.setdefault("brief", {})
    args = (state.get("partial") or {}).get("_args", {})
    mailbox = str(args.get("mailbox") or "")
    candidates = _brief_candidates_from_dict(list(brief.get("candidates") or []))
    if not candidates:
        brief["stage"] = "phase2"
        brief["phase2_cursor"] = 0
        _brief_update_state(run_id, stage="phase2", progress={"evaluated": 0, "total": 0, "already_replied": 0})
        return

    kept_candidates = []
    latest_from_owner_by_thread: dict[str, bool] = {}
    already_replied_count = 0
    total = len(candidates)
    for index, candidate in enumerate(candidates, 1):
        _brief_update_state(run_id, stage="check_replied", progress={"current": index, "total": total})
        thread_key = candidate.thread_id or (candidate.message_ids[0] if candidate.message_ids else candidate.candidate_id)
        if not thread_key:
            kept_candidates.append(candidate)
            continue
        if thread_key not in latest_from_owner_by_thread:
            try:
                latest_from_owner_by_thread[thread_key] = bool(_thread_latest_is_from_owner(mailbox, thread_key, mailbox))
            except Exception:
                latest_from_owner_by_thread[thread_key] = False
        if latest_from_owner_by_thread[thread_key]:
            already_replied_count += 1
            continue
        kept_candidates.append(candidate)

    brief["candidates"] = _brief_to_dict_list(kept_candidates)
    brief["already_replied_count"] = already_replied_count
    brief["phase2_cursor"] = 0
    brief["stage"] = "phase2"
    _brief_update_state(
        run_id,
        stage="phase2",
        progress={"evaluated": 0, "total": len(kept_candidates), "checked": total, "already_replied": already_replied_count},
    )


async def _brief_persist_cards(run_id: str, new_judgments: list[Any]) -> int:
    from mail_agent.cards.service import build_card, cards_to_frontend, is_card_actionable, merge_cards
    from mail_agent.storage.ops import get_active_cards, set_active_cards
    from mail_agent.storage.types import ActiveCards
    from mail_agent.sync.gmail_status import fetch_gmail_thread_state

    state = MAIL_AGENT_RUNS[run_id]
    brief = state.setdefault("brief", {})
    mailbox = str((state.get("partial") or {}).get("_args", {}).get("mailbox") or "")
    messages = _brief_messages_from_dict(list(brief.get("messages") or []))
    candidates = _brief_candidates_from_dict(list(brief.get("candidates") or []))
    msg_map = {m.message_id: m for m in messages if m.message_id}
    cand_map = {c.candidate_id: c for c in candidates}
    new_cards = []
    for judgment in new_judgments:
        candidate = cand_map.get(judgment.candidate_id)
        if not candidate or not candidate.message_ids:
            continue
        card = build_card(candidate, judgment, msg_map.get(candidate.message_ids[0]), mailbox)
        if card.thread_id and card.user_action in ("reply", "review"):
            try:
                thread_state = await asyncio.to_thread(fetch_gmail_thread_state, mailbox, card.thread_id)
                card.gmail_state = thread_state.to_card_state()
            except Exception as exc:
                from mail_agent.storage.types import _now
                card.gmail_state = {
                    **(card.gmail_state or {}),
                    "last_synced_at": _now(),
                    "sync_failed": True,
                    "sync_error": f"{type(exc).__name__}: {str(exc)[:160]}",
                }
                log(f"brief new card gmail state failed: mailbox={mailbox} thread={card.thread_id} error={type(exc).__name__}: {exc}")
        if not is_card_actionable(card):
            continue
        new_cards.append(card)
    if not new_cards:
        return 0
    active = await get_active_cards(mailbox)
    merged = merge_cards(active, new_cards)
    await set_active_cards(mailbox, merged)
    brief["cards_version"] = int(brief.get("cards_version") or 0) + 1
    brief["last_cards"] = [{"id": c.get("id"), "title": c.get("title")} for c in cards_to_frontend(ActiveCards(cards=new_cards))]
    return len(new_cards)


async def _brief_run_phase2_slice(run_id: str, sampling_create_message: Any) -> None:
    from mail_agent.core.context import read_candidate_context
    from mail_agent.core.guards import apply_rule_guards
    from mail_agent.domain.types import MailTaskPlan, MailboxProfile
    from mail_agent.judgment_engine.service import build_anna_single_judgment_prompt, create_fallback_judgment, _parse_compact_batch_item
    from mail_agent.llm_runtime.service import call_llm_json_safe
    from mail_agent.planning.strategies import get as get_strategy

    phase2_llm_concurrency = 4
    phase2_slice_budget_s = 45.0
    phase2_sampling_timeout_s = 60.0
    phase2_persist_reserve_s = 6.0

    state = MAIL_AGENT_RUNS[run_id]
    brief = state.setdefault("brief", {})
    args = (state.get("partial") or {}).get("_args", {})
    mailbox = str(args.get("mailbox") or "")
    task_plan_raw = dict(brief.get("task_plan") or {})
    task_plan = MailTaskPlan(
        raw_user_request=str(task_plan_raw.get("raw_user_request") or args.get("user_request") or ""),
        mailbox_id=mailbox,
        user_email=mailbox,
        strategy_mode=task_plan_raw.get("strategy_mode") or brief.get("strategy_mode") or "default_secretary",
        goals=list(task_plan_raw.get("goals") or []),
        constraints=list(task_plan_raw.get("constraints") or []),
        scope=dict(task_plan_raw.get("scope") or {}),
    )
    strategy = get_strategy(task_plan.strategy_mode)
    if not strategy:
        raise ValueError(f"Unknown strategy mode: {task_plan.strategy_mode}")
    profile = MailboxProfile(mailbox_id=mailbox, owner=mailbox)
    candidates = _brief_candidates_from_dict(list(brief.get("candidates") or []))
    existing_judgments = _brief_judgments_from_dict(list(brief.get("judgments") or []))
    judgments_by_id = {judgment.candidate_id: judgment for judgment in existing_judgments if judgment.candidate_id}
    completed_ids = _brief_completed_candidate_ids(existing_judgments)
    cursor = _brief_phase2_contiguous_cursor(candidates, completed_ids)
    brief["phase2_cursor"] = cursor
    total = len(candidates)
    evaluated_total = len(completed_ids)
    if evaluated_total >= total:
        brief["stage"] = "finalizing"
        _brief_update_state(run_id, stage="finalizing", progress={"evaluated": evaluated_total, "total": total})
        return

    slice_started = time.monotonic()
    pending_candidates = [candidate for candidate in candidates if candidate.candidate_id not in completed_ids]
    batch = pending_candidates[:phase2_llm_concurrency]
    _brief_update_state(
        run_id,
        stage="read_context",
        progress={
            "current": evaluated_total,
            "total": total,
            "batch": len(batch),
            "batch_size": len(batch),
            "phase2_llm_concurrency": phase2_llm_concurrency,
            "phase2_sampling_timeout_s": phase2_sampling_timeout_s,
            "timeout_s": phase2_sampling_timeout_s,
            "max_tokens": 8000,
            "phase2_pending_count": len(pending_candidates),
        },
    )

    async def _read_one_context(candidate: Any) -> tuple[Any, int]:
        started = time.monotonic()
        context = await read_candidate_context(mailbox, candidate)
        return context, int((time.monotonic() - started) * 1000)

    context_started = time.monotonic()
    context_results = await asyncio.gather(*[_read_one_context(candidate) for candidate in batch])
    phase2_context_read_ms = int((time.monotonic() - context_started) * 1000)
    candidate_metrics = dict(brief.get("phase2_candidate_metrics") or {})
    contexts: list[Any] = []
    for context, context_read_ms in context_results:
        contexts.append(context)
        candidate_id = str(getattr(getattr(context, "candidate", None), "candidate_id", "") or "")
        if candidate_id:
            metric = dict(candidate_metrics.get(candidate_id) or {})
            metric["context_type"] = str(getattr(context, "type", "") or "")
            metric["context_read_ms"] = context_read_ms
            candidate_metrics[candidate_id] = metric
    brief["phase2_candidate_metrics"] = candidate_metrics

    sampling_timeout_s = phase2_sampling_timeout_s

    async def _evaluate_one(ctx: Any) -> tuple[Any, dict[str, Any]]:
        candidate_id = str(ctx.candidate.candidate_id or "")
        prompt = build_anna_single_judgment_prompt(task_plan, strategy, profile, ctx, None)
        prompt_chars = len(prompt)
        sampling_started = time.monotonic()
        metric = dict(candidate_metrics.get(candidate_id) or {})
        context_type = str(getattr(ctx, "type", "") or "")
        prompt_profile = _brief_phase2_prompt_profile(ctx.candidate, context_type)
        metric["context_type"] = context_type
        metric["prompt_chars"] = prompt_chars
        metric["prompt_profile"] = prompt_profile
        log(
            "phase2 prompt: "
            + json.dumps(
                {
                    "run_id": run_id,
                    "mailbox": mailbox,
                    "candidate_id": candidate_id,
                    "kind": str(getattr(ctx.candidate, "kind", "") or ""),
                    "priority_hint": str(getattr(ctx.candidate, "priority_hint", "") or ""),
                    "context_type": context_type,
                    "prompt_profile": prompt_profile,
                    "prompt_chars": prompt_chars,
                    "slice_budget_s": round(phase2_slice_budget_s, 1),
                    "persist_reserve_s": round(phase2_persist_reserve_s, 1),
                    "timeout_s": round(sampling_timeout_s, 1),
                },
                ensure_ascii=False,
            )
        )
        try:
            result = await call_llm_json_safe(
                sampling_create_message,
                system_prompt="You are a strict JSON generator. Output ONLY valid JSON — no explanation, no markdown, no code fences.",
                user_message=prompt,
                fallback={},
                temperature=0.1,
                max_tokens=8000,
                timeout=sampling_timeout_s,
                metadata={"tool": "evaluate_item_single", "strategy_mode": strategy.id, "candidate_count": "1"},
                allow_fallback=True,
                allow_sampling_provider_fallback=False,
                max_attempts=1,
            )
            metric["sampling_elapsed_ms"] = int((time.monotonic() - sampling_started) * 1000)
            payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
            if not payload or result.get("fallback_used"):
                raise ValueError(str(result.get("fallback_reason") or "empty Anna sampling response"))
            payload.setdefault("candidate_id", ctx.candidate.candidate_id)
            metric["fallback_used"] = False
            log(
                "phase2 result: "
                + json.dumps(
                    {
                        "run_id": run_id,
                        "candidate_id": candidate_id,
                        "context_type": context_type,
                        "prompt_profile": prompt_profile,
                        "prompt_chars": prompt_chars,
                        "sampling_elapsed_ms": metric["sampling_elapsed_ms"],
                        "fallback_used": False,
                    },
                    ensure_ascii=False,
                )
            )
            return apply_rule_guards(_parse_compact_batch_item(payload, strategy), strategy), metric
        except Exception as exc:
            metric["sampling_elapsed_ms"] = int((time.monotonic() - sampling_started) * 1000)
            metric["fallback_used"] = True
            metric["fallback_reason"] = f"{type(exc).__name__}: {exc}"[:200]
            log(
                "phase2 result: "
                + json.dumps(
                    {
                        "run_id": run_id,
                        "candidate_id": candidate_id,
                        "context_type": context_type,
                        "prompt_profile": prompt_profile,
                        "prompt_chars": prompt_chars,
                        "sampling_elapsed_ms": metric["sampling_elapsed_ms"],
                        "fallback_used": True,
                        "fallback_reason": metric["fallback_reason"],
                    },
                    ensure_ascii=False,
                )
            )
            return create_fallback_judgment(ctx.candidate.candidate_id, strategy, f"evaluation failed: {type(exc).__name__}: {exc}"), metric

    _brief_update_state(
        run_id,
        stage="evaluate",
        progress={
            "evaluated": evaluated_total,
            "total": total,
            "batch": len(batch),
            "batch_size": len(batch),
            "sampling_calls_used": len(batch),
            "phase2_llm_concurrency": phase2_llm_concurrency,
            "phase2_context_read_ms": phase2_context_read_ms,
            "phase2_pending_count": len(pending_candidates),
            "phase2_sampling_timeout_s": sampling_timeout_s,
            "timeout_s": sampling_timeout_s,
            "max_tokens": 8000,
        },
    )
    batch_judgments: list[Any] = []
    sampling_batch_started = time.monotonic()
    sampling_first_done_ms = 0
    persist_total_ms = 0
    pending = [asyncio.create_task(_evaluate_one(ctx)) for ctx in contexts]
    for task in asyncio.as_completed(pending):
        judgment, metric = await task
        batch_judgments.append(judgment)
        metric_candidate_id = str(judgment.candidate_id or "")
        if metric_candidate_id:
            candidate_metrics[metric_candidate_id] = metric
            brief["phase2_candidate_metrics"] = candidate_metrics
        judgments_by_id[judgment.candidate_id] = judgment
        completed_ids.add(judgment.candidate_id)
        if sampling_first_done_ms <= 0:
            sampling_first_done_ms = int((time.monotonic() - sampling_batch_started) * 1000)
        brief["judgments"] = _brief_to_dict_list([judgments_by_id[candidate.candidate_id] for candidate in candidates if candidate.candidate_id in judgments_by_id])
        persist_started = time.monotonic()
        cards_added_partial = await _brief_persist_cards(run_id, [judgment])
        persist_elapsed_ms = int((time.monotonic() - persist_started) * 1000)
        persist_total_ms += persist_elapsed_ms
        current_evaluated = len(completed_ids)
        brief["phase2_cursor"] = _brief_phase2_contiguous_cursor(candidates, completed_ids)
        _brief_update_state(
            run_id,
            stage="evaluate",
            progress={
                "evaluated": current_evaluated,
                "total": total,
                "batch": len(batch),
                "batch_size": len(batch),
                "sampling_calls_used": len(batch),
                "phase2_llm_concurrency": phase2_llm_concurrency,
                "cards_added": cards_added_partial,
                "cards_version": int(brief.get("cards_version") or 0),
                "phase2_priority_distribution": _brief_count_judgment_priorities(brief.get("judgments") or []),
                "phase2_context_read_ms": phase2_context_read_ms,
                "phase2_prompt_chars": sum(int((candidate_metrics.get(candidate.candidate_id) or {}).get("prompt_chars") or 0) for candidate in batch),
                "phase2_sampling_first_done_ms": sampling_first_done_ms,
                "phase2_sampling_batch_ms": int((time.monotonic() - sampling_batch_started) * 1000),
                "phase2_persist_ms": persist_total_ms,
                "phase2_cards_added": cards_added_partial,
                "phase2_pending_count": max(0, total - current_evaluated),
                "fallback": _brief_count_judgment_fallbacks(brief.get("judgments") or []),
                "phase2_sampling_timeout_s": sampling_timeout_s,
                "timeout_s": sampling_timeout_s,
                "max_tokens": 8000,
            },
            cards_added=cards_added_partial,
        )
    brief["phase2_cursor"] = _brief_phase2_contiguous_cursor(candidates, completed_ids)
    if len(completed_ids) >= total:
        brief["stage"] = "finalizing"
    _brief_update_state(
        run_id,
        stage="evaluate_done" if brief["stage"] != "finalizing" else "finalizing",
        progress={
            "evaluated": len(completed_ids),
            "total": total,
            "batch_size": len(batch),
            "phase2_llm_concurrency": phase2_llm_concurrency,
            "cards_version": int(brief.get("cards_version") or 0),
            "phase2_priority_distribution": _brief_count_judgment_priorities(brief.get("judgments") or []),
            "phase2_context_read_ms": phase2_context_read_ms,
            "phase2_prompt_chars": sum(int((candidate_metrics.get(candidate.candidate_id) or {}).get("prompt_chars") or 0) for candidate in batch),
            "phase2_sampling_first_done_ms": sampling_first_done_ms,
            "phase2_sampling_batch_ms": int((time.monotonic() - sampling_batch_started) * 1000),
            "phase2_persist_ms": persist_total_ms,
            "phase2_cards_added": sum(1 for judgment in batch_judgments if judgment.final_decision.should_show_in_main_result or judgment.final_decision.should_show_in_lower_priority),
            "phase2_pending_count": max(0, total - len(completed_ids)),
            "fallback": _brief_count_judgment_fallbacks(brief.get("judgments") or []),
            "phase2_sampling_timeout_s": sampling_timeout_s,
            "timeout_s": sampling_timeout_s,
            "max_tokens": 8000,
        },
        cards_added=0,
    )


async def _brief_finalize_run(run_id: str) -> None:
    from mail_agent.cards.service import build_cleanup_bundle, cards_to_frontend, merge_cards
    from mail_agent.storage.ops import append_run_history, get_active_cards, get_scan_state, mark_messages_processed_batch, save_run_record, set_active_cards, set_cleanup_bundle, set_scan_state
    from mail_agent.storage.types import ProcessedMessage, RunHistoryEntry, RunRecord, ScanState, _now

    state = MAIL_AGENT_RUNS[run_id]
    brief = state.setdefault("brief", {})
    args = (state.get("partial") or {}).get("_args", {})
    mailbox = str(args.get("mailbox") or "")
    messages = _brief_messages_from_dict(list(brief.get("messages") or []))
    candidates = _brief_candidates_from_dict(list(brief.get("candidates") or []))
    judgments = _brief_judgments_from_dict(list(brief.get("judgments") or []))
    candidate_msg_ids = {c.message_ids[0] for c in candidates if c.message_ids}
    j_by_cand = {j.candidate_id: j for j in judgments}
    c_by_msg = {c.message_ids[0]: c for c in candidates if c.message_ids}
    processed_msgs = []
    for msg in messages:
        if not msg.message_id:
            continue
        candidate = c_by_msg.get(msg.message_id)
        judgment = j_by_cand.get(candidate.candidate_id) if candidate else None
        processed = ProcessedMessage(
            message_id=msg.message_id,
            thread_id=msg.thread_id or "",
            from_addr=msg.from_addr or "",
            subject=msg.subject or "",
            snippet=msg.snippet or "",
            internal_date=msg.internal_date or "",
            processed_at=_now(),
            run_id=run_id,
            is_candidate=msg.message_id in candidate_msg_ids,
            candidate_kind=(candidate.kind if candidate else ""),
            priority=(judgment.final_decision.priority if judgment else (candidate.priority_hint if candidate else "low")),
            read_depth=(candidate.read_depth_required if candidate else "header_only"),
            confidence=(judgment.confidence if judgment else 0.0),
        )
        processed_msgs.append(processed)
    if processed_msgs:
        await mark_messages_processed_batch(mailbox, processed_msgs)

    cleanup_full = []
    cleanup_card = None
    if brief.get("low_value_items"):
        cleanup_card, cleanup_full = build_cleanup_bundle(run_id, mailbox, list(brief.get("low_value_items") or []), messages)
    if cleanup_full:
        await set_cleanup_bundle(mailbox, cleanup_full, preserve_existing=False)
        if cleanup_card is not None:
            cleanup_card.bundled_count = len(cleanup_full)
            active = await get_active_cards(mailbox)
            await set_active_cards(mailbox, merge_cards(active, [cleanup_card]))
            brief["cards_version"] = int(brief.get("cards_version") or 0) + 1

    previous_state = await get_scan_state(mailbox)
    latest_internal_date = max((str(m.internal_date) for m in messages if m.internal_date), default=previous_state.last_message_internal_date, key=lambda value: int(value or 0))
    sync_history_id = str((brief.get("gmail_sync") or {}).get("last_history_id") or "")
    await set_scan_state(mailbox, ScanState(
        mailbox=mailbox,
        last_scan_ts=_now(),
        last_message_internal_date=latest_internal_date,
        last_history_id=sync_history_id or previous_state.last_history_id,
        total_scans=previous_state.total_scans + 1,
        total_processed=previous_state.total_processed + len(processed_msgs),
    ))

    cards_summary = cards_to_frontend(await get_active_cards(mailbox))
    run_record = RunRecord(
        run_id=run_id,
        mailbox=mailbox,
        strategy_mode=str(brief.get("strategy_mode") or ""),
        user_request=str(args.get("user_request") or ""),
        mode=str(args.get("mode") or "auto"),
        scanned_count=len(messages),
        candidate_count=len(candidates),
        main_count=sum(1 for j in judgments if j.final_decision.should_show_in_main_result),
        lower_count=sum(1 for j in judgments if j.final_decision.should_show_in_lower_priority),
        summary=[f"Scanned {len(messages)} messages, found {len(candidates)} candidates."],
        strategy=[f"Strategy: {brief.get('strategy_mode') or ''}"],
        cards=[{"id": card.get("id"), "title": card.get("title")} for card in cards_summary],
    )
    await save_run_record(mailbox, run_record)
    reply_count = sum(1 for j in judgments if j.final_decision.user_action == "reply")
    review_count = sum(1 for j in judgments if j.final_decision.user_action == "review")
    cleanup_count = sum(1 for j in judgments if j.final_decision.user_action == "cleanup")
    if cleanup_card is not None:
        cleanup_count += 1
    important_count = sum(1 for j in judgments if j.final_decision.priority in ("critical", "high", "medium"))
    history_parts = [f"Scanned {len(messages)} emails"]
    if reply_count:
        history_parts.append(f"{reply_count} needs reply")
    if review_count:
        history_parts.append(f"{review_count} needs review")
    if cleanup_count:
        history_parts.append(f"{cleanup_count} cleanup")
    if important_count:
        history_parts.append(f"{important_count} important")
    await append_run_history(RunHistoryEntry(
        run_id=run_id,
        mailbox=mailbox,
        ts=_now(),
        entry_type="scan",
        request=str(args.get("user_request") or "")[:100],
        mode=str(args.get("mode") or "auto"),
        strategy=str(brief.get("strategy_mode") or ""),
        result=", ".join(history_parts),
        summary=(
            f"Scanned {len(messages)} messages, found {len(candidates)} candidates.\n"
            f"Needs reply: {reply_count} · Needs review: {review_count} · Cleanup: {cleanup_count}"
        ),
    ))
    state.update({
        "status": "done",
        "stage": "done",
        "needs_continue": False,
        "cards_added": 0,
        "progress": {"evaluated": len(judgments), "total": len(candidates), "cards_version": int(brief.get("cards_version") or 0)},
        "updated_at": beijing_now(),
        "result": {"success": True, "run_id": run_id, "cards_version": int(brief.get("cards_version") or 0)},
    })
    _save_run_checkpoint(run_id)


async def _continue_mail_agent_run_async(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """推进 Brief 短 invoke 状态机，每次只处理一个有限阶段。"""
    run_id = str(arguments.get("run_id") or "")
    state = _get_run_state(run_id)
    if not state:
        return {"success": False, "run_id": run_id, "error": "run not found"}
    saved_args = (state.get("partial") or {}).get("_args") or {}
    saved_args.update({key: value for key, value in arguments.items() if key != "run_id" and value not in (None, "")})
    state.setdefault("partial", {})["_args"] = saved_args
    ai_provider = str(arguments.get("ai_provider") or saved_args.get("ai_provider", "anna-llm"))

    if not str(saved_args.get("mailbox") or ""):
        return {"success": False, "run_id": run_id, "error": "mailbox is required"}

    sampling_fn = _build_sampling_for_run({"ai_provider": ai_provider}, invoke_id)
    try:
        state["status"] = "running"
        state["needs_continue"] = True
        state["cards_added"] = 0
        brief = state.setdefault("brief", {})
        stage = str(brief.get("stage") or state.get("stage") or "queued")
        if stage in ("queued", "scan", "scanning", "filtering"):
            await _brief_prepare_scan(run_id, saved_args)
        elif stage == "phase1":
            await _brief_run_phase1_slice(run_id, sampling_fn)
        elif stage == "check_replied":
            await _brief_check_replied_after_phase1(run_id)
        elif stage == "phase2":
            await _brief_run_phase2_slice(run_id, sampling_fn)
        elif stage == "finalizing":
            await _brief_finalize_run(run_id)
        elif stage == "done":
            state["status"] = "done"
            state["needs_continue"] = False
        else:
            brief["stage"] = "phase1"
        return _public_run_view(state)
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update({
            "status": "failed",
            "stage": "failed",
            "updated_at": beijing_now(),
            "error": str(exc),
            "needs_continue": False,
        })
        _save_run_checkpoint(run_id)
        return _public_run_view(MAIL_AGENT_RUNS[run_id])

__all__ = [name for name in globals() if not name.startswith("__")]
