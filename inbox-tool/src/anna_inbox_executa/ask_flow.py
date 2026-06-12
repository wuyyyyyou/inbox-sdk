from __future__ import annotations

from anna_inbox_executa.common import *
from anna_inbox_executa.sampling_tools import *
from anna_inbox_executa.contact_memory_flow import _memory_mailboxes

async def run_custom_scan_background(run_id: str, plan: Any, arguments: dict[str, Any], invoke_id: str) -> None:
    """Background execution of a custom scan (plan already generated or loaded)."""
    MAIL_AGENT_RUNS[run_id]["status"] = "running"
    MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
    _save_run_checkpoint(run_id)

    def _update_progress(stage: str, progress: dict[str, Any]) -> None:
        partial_update = progress.pop("partial", None)
        if isinstance(partial_update, dict):
            _merge_partial(run_id, partial_update)
        MAIL_AGENT_RUNS[run_id]["stage"] = stage
        MAIL_AGENT_RUNS[run_id]["progress"] = progress
        MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
        # 持久化警告/错误阶段，不被后续正常阶段覆盖
        if _is_warning_stage(stage):
            warnings = MAIL_AGENT_RUNS[run_id].setdefault("warnings", [])
            entry = {"stage": stage, "at": beijing_now(), "detail": progress}
            # 去重：同一个 stage 只保留最新一次
            existing = [w for w in warnings if w.get("stage") != stage]
            existing.append(entry)
            MAIL_AGENT_RUNS[run_id]["warnings"] = existing[-10:]  # 最多保留 10 条
        _save_run_checkpoint(run_id)

    try:
        from mail_agent.core.pipeline import run_custom_scan
        result = await run_custom_scan(
            plan=plan,
            mailbox=arguments.get("mailbox", ""),
            sampling_create_message=_build_sampling_for_run(arguments, invoke_id),
            progress_callback=_update_progress,
        )
        # Update plan result metadata
        from mail_agent.storage.ops import update_plan_result
        section_count = len(result.get("sections", []))
        item_count = sum(len(s.get("items", [])) for s in result.get("sections", []))
        await update_plan_result(plan.plan_id, f"{section_count} sections, {item_count} items")

        # Store result for frontend
        planner_llm = getattr(plan, "llm_meta", {})
        executor_llm = result.get("llm_meta", {}) if isinstance(result, dict) else {}
        result_data: dict[str, Any] = {
            "success": True,
            "run_id": run_id,
            "plan_id": plan.plan_id,
            "plan_title": plan.title,
            "plan_description": plan.description,
            "plan_gmail_queries": plan.gmail_queries,
            "plan_read_depth": plan.read_depth or "message_detail",
            "title": result.get("title", ""),
            "summary": result.get("summary", ""),
            "sections": result.get("sections", []),
            "ai_provider": str(arguments.get("ai_provider", "anna-llm") or "anna-llm"),
            "planner_llm": planner_llm,
            "executor_llm": executor_llm,
            "trace": {
                "plan": {
                    "plan_id": plan.plan_id,
                    "title": plan.title,
                    "description": plan.description,
                    "gmail_queries": plan.gmail_queries,
                    "read_depth": plan.read_depth or "message_detail",
                },
                "sources": MAIL_AGENT_RUNS[run_id].get("partial", {}).get("sources", []),
                "progress": MAIL_AGENT_RUNS[run_id].get("progress", {}),
                "started_at": MAIL_AGENT_RUNS[run_id].get("started_at"),
                "updated_at": beijing_now(),
            },
        }
        MAIL_AGENT_RUNS[run_id].update({
            "status": "done",
            "stage": "done",
            "updated_at": beijing_now(),
            "result": result_data,
        })
        _save_run_checkpoint(run_id)
        # Write run history
        from mail_agent.storage.ops import append_run_history
        from mail_agent.storage.types import RunHistoryEntry, _now
        history_entry = RunHistoryEntry(
            run_id=run_id,
            mailbox=arguments.get("mailbox", ""),
            ts=_now(),
            entry_type="scan",
            request=str(arguments.get("user_request", ""))[:100],
            result=f"{section_count} sections, {item_count} items",
            summary=result.get("summary", "")[:200],
        )
        await append_run_history(history_entry)
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update({
            "status": "failed",
            "stage": "failed",
            "updated_at": beijing_now(),
            "error": str(exc),
        })
        _save_run_checkpoint(run_id)

def start_custom_scan(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """Start a custom scan: generate plan (LLM) then execute.

    Blocks until the pipeline completes (keeps invoke alive for sampling token).
    The frontend polls get_mail_agent_run for progress via a separate invoke.
    """
    run_id = str(arguments.get("run_id") or "").strip()
    if not run_id or len(run_id) < 8:
        run_id = f"bg_{uuid.uuid4().hex[:12]}"
    MAIL_AGENT_RUNS[run_id] = {
        "run_id": run_id,
        "status": "queued",
        "stage": "planning",
        "progress": {},
        "warnings": [],
        "started_at": beijing_now(),
        "updated_at": beijing_now(),
        "result": None,
        "error": "",
        "partial": {},
    }
    _save_run_checkpoint(run_id)
    future = asyncio.run_coroutine_threadsafe(
        _start_custom_scan_async(run_id, arguments, invoke_id),
        loop,
    )
    wait_timeout = int(arguments.get("wait_timeout_seconds", 600))
    future.result(timeout=wait_timeout)
    return {
        "success": MAIL_AGENT_RUNS[run_id].get("status") == "done",
        "run_id": run_id,
        "status": MAIL_AGENT_RUNS[run_id]["status"],
        "stage": MAIL_AGENT_RUNS[run_id]["stage"],
        "progress": MAIL_AGENT_RUNS[run_id]["progress"],
        "partial": MAIL_AGENT_RUNS[run_id].get("partial", {}),
        "started_at": MAIL_AGENT_RUNS[run_id]["started_at"],
        "updated_at": MAIL_AGENT_RUNS[run_id]["updated_at"],
        "result": _compact_run_result(MAIL_AGENT_RUNS[run_id].get("result")),
        "error": MAIL_AGENT_RUNS[run_id].get("error", ""),
    }


async def _start_custom_scan_async(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    """Async portion: run the new Ask pipeline (plan → search → filter → answer → guard)."""
    from mail_agent.ask.answer import run_ask_pipeline
    from mail_agent.storage.ops import append_run_history, save_custom_plan, update_plan_result
    from mail_agent.storage.types import RunHistoryEntry, _now

    try:
        MAIL_AGENT_RUNS[run_id]["status"] = "running"
        MAIL_AGENT_RUNS[run_id]["stage"] = "planning"
        MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
        _save_run_checkpoint(run_id)

        sampling = _build_sampling_for_run(arguments, invoke_id)
        user_request = str(arguments.get("user_request", "")).strip()
        mailboxes = _memory_mailboxes(arguments)

        def _update_progress(stage: str, progress: dict[str, Any]) -> None:
            partial_update = progress.pop("partial", None)
            if isinstance(partial_update, dict):
                _merge_partial(run_id, partial_update)
            MAIL_AGENT_RUNS[run_id]["stage"] = stage
            MAIL_AGENT_RUNS[run_id]["progress"] = progress
            MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
            if _is_warning_stage(stage):
                warnings = MAIL_AGENT_RUNS[run_id].setdefault("warnings", [])
                entry = {"stage": stage, "at": beijing_now(), "detail": progress}
                existing = [w for w in warnings if w.get("stage") != stage]
                existing.append(entry)
                MAIL_AGENT_RUNS[run_id]["warnings"] = existing[-10:]
            _save_run_checkpoint(run_id)

        result = await run_ask_pipeline(
            user_request=user_request,
            mailboxes=mailboxes,
            sampling_create_message=sampling,
            progress_callback=_update_progress,
        )

        plan_id = result.get("plan_id", "")
        plan_title = result.get("plan_title", "")
        plan_queries = result.get("plan_queries", [])
        plan_topics = result.get("plan_topics", [])

        result_data: dict[str, Any] = {
            "success": True,
            "run_id": run_id,
            "plan_id": plan_id,
            "plan_title": plan_title,
            "plan_description": result.get("plan_description", ""),
            "plan_gmail_queries": plan_queries,
            "plan_read_depth": "per_candidate",
            "title": result.get("title", ""),
            "summary": result.get("summary", ""),
            "sections": result.get("sections", []),
            "ai_provider": str(arguments.get("ai_provider", "anna-llm") or "anna-llm"),
            "planner_llm": result.get("planner_llm", {}),
            "executor_llm": result.get("llm_meta", {}),
            "trace": {
                "plan": {
                    "plan_id": plan_id,
                    "title": plan_title,
                    "topics": plan_topics,
                    "timeframe": result.get("plan_timeframe", ""),
                    "direction": result.get("plan_direction", ""),
                    "goal": result.get("plan_goal", ""),
                    "queries": plan_queries,
                },
                "mailboxes": mailboxes,
                "messages_scanned": result.get("messages_scanned", 0),
                "candidates_found": result.get("candidates_found", 0),
                "sources": MAIL_AGENT_RUNS[run_id].get("partial", {}).get("sources", []),
                "progress": MAIL_AGENT_RUNS[run_id].get("progress", {}),
                "started_at": MAIL_AGENT_RUNS[run_id].get("started_at"),
                "updated_at": beijing_now(),
            },
        }
        MAIL_AGENT_RUNS[run_id].update({
            "status": "done",
            "stage": "done",
            "updated_at": beijing_now(),
            "result": result_data,
        })
        _save_run_checkpoint(run_id)

        # Persist plan and update result
        from types import SimpleNamespace
        plan_obj = SimpleNamespace(
            plan_id=plan_id,
            user_request=user_request,
            title=plan_title,
            description=result.get("plan_description", ""),
            task_prompt="",
            created_at=beijing_now(),
            last_used_at=beijing_now(),
            use_count=1,
            last_result_summary="",
            people=result.get("plan_topics", []),  # topics stored as people-ish for compat
            topics=result.get("plan_topics", []),
            timeframe=result.get("plan_timeframe", "30d"),
            direction=result.get("plan_direction", "inbox"),
            goal=result.get("plan_goal", "general_qa"),
            gmail_flags=result.get("plan_gmail_flags", []),
            confidence=0.8,
        )
        await save_custom_plan(plan_obj)
        section_count = len(result.get("sections", []))
        item_count = sum(len(s.get("items", [])) for s in result.get("sections", []))
        await update_plan_result(plan_id, f"{section_count} sections, {item_count} items")

        # Write run history
        history_entry = RunHistoryEntry(
            run_id=run_id,
            mailbox=mailboxes[0] if mailboxes else "",
            ts=_now(),
            entry_type="scan",
            request=user_request[:100],
            plan_id=plan_id,
            result=f"{section_count} sections, {item_count} items",
            summary=result.get("summary", "")[:200],
        )
        await append_run_history(history_entry)

    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update({
            "status": "failed",
            "stage": "failed",
            "updated_at": beijing_now(),
            "error": str(exc),
        })
        _save_run_checkpoint(run_id)


def re_run_custom_scan(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """Re-run a previously saved custom scan plan (skip LLM planning).

    Blocks until execution completes (keeps invoke alive for sampling token).
    The frontend polls get_mail_agent_run for progress via a separate invoke.
    """
    plan_id = str(arguments.get("plan_id", "")).strip()
    if not plan_id:
        return {"success": False, "error": "plan_id is required"}

    run_id = str(arguments.get("run_id") or "").strip()
    if not run_id or len(run_id) < 8:
        run_id = f"bg_{uuid.uuid4().hex[:12]}"
    MAIL_AGENT_RUNS[run_id] = {
        "run_id": run_id,
        "status": "queued",
        "stage": "planning_done",
        "progress": {},
        "warnings": [],
        "started_at": beijing_now(),
        "updated_at": beijing_now(),
        "result": None,
        "error": "",
        "partial": {},
    }
    _save_run_checkpoint(run_id)
    future = asyncio.run_coroutine_threadsafe(
        _re_run_custom_scan_async(run_id, plan_id, arguments, invoke_id),
        loop,
    )
    wait_timeout = int(arguments.get("wait_timeout_seconds", 600))
    future.result(timeout=wait_timeout)
    return {
        "success": MAIL_AGENT_RUNS[run_id].get("status") == "done",
        "run_id": run_id,
        "status": MAIL_AGENT_RUNS[run_id]["status"],
        "stage": MAIL_AGENT_RUNS[run_id]["stage"],
        "progress": MAIL_AGENT_RUNS[run_id]["progress"],
        "partial": MAIL_AGENT_RUNS[run_id].get("partial", {}),
        "started_at": MAIL_AGENT_RUNS[run_id]["started_at"],
        "updated_at": MAIL_AGENT_RUNS[run_id]["updated_at"],
        "result": _compact_run_result(MAIL_AGENT_RUNS[run_id].get("result")),
        "error": MAIL_AGENT_RUNS[run_id].get("error", ""),
    }


async def _re_run_custom_scan_async(run_id: str, plan_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    """Async portion: load plan and execute.

    Routes to the new Ask pipeline for plans saved by the new planner,
    or the old run_custom_scan path for legacy CustomScanPlan plans.
    """
    from mail_agent.storage.ops import get_custom_plan

    try:
        MAIL_AGENT_RUNS[run_id]["status"] = "running"
        MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
        _save_run_checkpoint(run_id)

        plan_dict = await get_custom_plan(plan_id)
        if not plan_dict:
            raise ValueError(f"Custom plan not found: {plan_id}")

        user_request = str(plan_dict.get("user_request", "")).strip()

        # New Ask plans have _plan_type == "ask" — reconstruct AskPlan, skip Planner
        if plan_dict.get("_plan_type") == "ask":
            from mail_agent.ask.answer import run_ask_pipeline
            from mail_agent.ask.planner import AskPlan
            from mail_agent.storage.ops import append_run_history, update_plan_result
            from mail_agent.storage.types import RunHistoryEntry, _now

            sampling = _build_sampling_for_run(arguments, invoke_id)
            mailboxes = _memory_mailboxes(arguments)

            ask_plan = AskPlan(
                plan_id=plan_id,
                user_request=user_request,
                title=str(plan_dict.get("title", "")),
                description=str(plan_dict.get("description", "")),
                people=list(plan_dict.get("people", [])),
                topics=list(plan_dict.get("topics", [])),
                timeframe=str(plan_dict.get("timeframe", "30d")),
                direction=str(plan_dict.get("direction", "inbox")),
                goal=str(plan_dict.get("goal", "general_qa")),
                task_prompt=str(plan_dict.get("task_prompt", "")),
                gmail_flags=list(plan_dict.get("gmail_flags", [])),
            )

            def _update_progress(stage: str, progress: dict[str, Any]) -> None:
                partial_update = progress.pop("partial", None)
                if isinstance(partial_update, dict):
                    _merge_partial(run_id, partial_update)
                MAIL_AGENT_RUNS[run_id]["stage"] = stage
                MAIL_AGENT_RUNS[run_id]["progress"] = progress
                MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
                if _is_warning_stage(stage):
                    warnings = MAIL_AGENT_RUNS[run_id].setdefault("warnings", [])
                    entry = {"stage": stage, "at": beijing_now(), "detail": progress}
                    existing = [w for w in warnings if w.get("stage") != stage]
                    existing.append(entry)
                    MAIL_AGENT_RUNS[run_id]["warnings"] = existing[-10:]
                _save_run_checkpoint(run_id)

            result = await run_ask_pipeline(
                mailboxes=mailboxes,
                plan=ask_plan,
                sampling_create_message=sampling,
                progress_callback=_update_progress,
            )

            section_count = len(result.get("sections", []))
            item_count = sum(len(s.get("items", [])) for s in result.get("sections", []))
            await update_plan_result(plan_id, f"{section_count} sections, {item_count} items")

            result_data: dict[str, Any] = {
                "success": True,
                "run_id": run_id,
                "plan_id": plan_id,
                "plan_title": result.get("plan_title", ""),
                "plan_description": result.get("plan_description", ""),
                "plan_gmail_queries": result.get("plan_queries", []),
                "plan_read_depth": "per_candidate",
                "title": result.get("title", ""),
                "summary": result.get("summary", ""),
                "sections": result.get("sections", []),
                "ai_provider": str(arguments.get("ai_provider", "anna-llm") or "anna-llm"),
                "planner_llm": result.get("planner_llm", {}),
                "executor_llm": result.get("llm_meta", {}),
                "trace": {
                    "plan": {"plan_id": plan_id, "title": result.get("plan_title", ""),
                             "topics": result.get("plan_topics", []),
                             "queries": result.get("plan_queries", []),
                    },
                    "mailboxes": mailboxes,
                    "messages_scanned": result.get("messages_scanned", 0),
                    "candidates_found": result.get("candidates_found", 0),
                },
            }
            MAIL_AGENT_RUNS[run_id].update({
                "status": "done",
                "stage": "done",
                "updated_at": beijing_now(),
                "result": result_data,
            })
            _save_run_checkpoint(run_id)

            history_entry = RunHistoryEntry(
                run_id=run_id,
                mailbox=mailboxes[0] if mailboxes else "",
                ts=_now(),
                entry_type="scan",
                request=user_request[:100],
                plan_id=plan_id,
                result=f"{section_count} sections, {item_count} items",
                summary=result.get("summary", "")[:200],
            )
            await append_run_history(history_entry)
            return

        # Old CustomScanPlan path
        from mail_agent.domain.types import CustomScanPlan
        plan = CustomScanPlan(
            plan_id=plan_dict.get("plan_id", plan_id),
            user_request=user_request,
            title=plan_dict.get("title", ""),
            description=plan_dict.get("description", ""),
            gmail_queries=plan_dict.get("gmail_queries", []),
            scan_budget=plan_dict.get("scan_budget", {}),
            read_depth=plan_dict.get("read_depth", "message_detail"),
            task_prompt=plan_dict.get("task_prompt", ""),
            created_at=plan_dict.get("created_at", ""),
            last_used_at=plan_dict.get("last_used_at", ""),
            use_count=plan_dict.get("use_count", 0),
            last_result_summary=plan_dict.get("last_result_summary", ""),
        )
        MAIL_AGENT_RUNS[run_id]["stage"] = "planning_done"
        MAIL_AGENT_RUNS[run_id].setdefault("partial", {})["plan"] = {
            "plan_id": plan.plan_id,
            "title": plan.title,
            "description": plan.description,
            "gmail_queries": plan.gmail_queries,
            "read_depth": plan.read_depth or "message_detail",
        }
        MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
        _save_run_checkpoint(run_id)

        await run_custom_scan_background(run_id, plan, arguments, invoke_id)
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update({
            "status": "failed",
            "stage": "failed",
            "updated_at": beijing_now(),
            "error": str(exc),
        })
        _save_run_checkpoint(run_id)

def _sync_get_custom_plans() -> dict[str, Any]:
    """同步入口通过统一 storage_ops 读取，兼容本地 JSON 和 APS。"""
    from mail_agent.storage.ops import list_custom_plans

    return {"plans": _run_storage_query(list_custom_plans())}


def _sync_get_custom_plan_detail(arguments: dict[str, Any]) -> dict[str, Any]:
    """同步入口通过统一 storage_ops 读取单个 custom plan。"""
    plan_id = str(arguments.get("plan_id", "")).strip()
    if not plan_id:
        return {"error": "plan_id is required"}
    from mail_agent.storage.ops import get_custom_plan

    plan = _run_storage_query(get_custom_plan(plan_id))
    if plan:
        return {"plan": plan}
    return {"error": f"Custom plan not found: {plan_id}"}

__all__ = [name for name in globals() if not name.startswith("__")]
