"""AI 侧栏统一 turn 入口：可轮询 run + Sampling 绑定 invoke。"""

from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from anna_inbox_executa.sampling_tools import *
from anna_inbox_executa.brief_flow import _merge_partial


def _public_ai_turn_state(run_id: str) -> dict[str, Any]:
    state = MAIL_AGENT_RUNS.get(run_id) or {}
    return {
        "success": state.get("status") == "done",
        "run_id": run_id,
        "status": state.get("status", "queued"),
        "stage": state.get("stage", ""),
        "progress": state.get("progress", {}),
        "partial": state.get("partial", {}),
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "result": _compact_run_result(state.get("result")),
        "error": state.get("error", ""),
        "needs_continue": bool(state.get("needs_continue")),
    }


def start_ai_turn(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """启动一次 AI turn：阻塞至 wait_timeout，超时后返回可轮询状态。

    中文注释：与 start_custom_scan 相同，保持 invoke 存活以便 Sampling 反向 RPC。
    """
    run_id = str(arguments.get("run_id") or "").strip()
    if not run_id or len(run_id) < 8:
        run_id = f"at_{uuid.uuid4().hex[:12]}"
    existing = MAIL_AGENT_RUNS.get(run_id)
    if existing:
        # 中文注释：同一 run_id 重试必须复用后台任务，避免重复 LLM / Gmail 调用。
        return _public_ai_turn_state(run_id)

    MAIL_AGENT_RUNS[run_id] = {
        "run_id": run_id,
        "status": "queued",
        "stage": "routing",
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
        _start_ai_turn_async(run_id, arguments, invoke_id),
        loop,
    )
    wait_timeout = int(arguments.get("wait_timeout_seconds", 60))
    try:
        future.result(timeout=wait_timeout)
    except FutureTimeoutError:
        # 中文注释：超时不取消后台；前端用 get_mail_agent_run 继续轮询。
        state = MAIL_AGENT_RUNS[run_id]
        state["status"] = "running"
        state["needs_continue"] = True
        state["updated_at"] = beijing_now()
        _save_run_checkpoint(run_id)
    return _public_ai_turn_state(run_id)


async def _start_ai_turn_async(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    """异步执行 Router + 白名单工具。"""
    from mail_agent.ai_turn.runner import run_ai_turn

    try:
        MAIL_AGENT_RUNS[run_id]["status"] = "running"
        MAIL_AGENT_RUNS[run_id]["stage"] = "routing"
        MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
        _save_run_checkpoint(run_id)

        sampling = _build_sampling_for_run(arguments, invoke_id)
        user_text = str(arguments.get("user_text") or arguments.get("user_request") or "").strip()
        ui_context = arguments.get("ui_context") if isinstance(arguments.get("ui_context"), dict) else {}

        def _update_progress(stage: str, progress: dict[str, Any] | None = None) -> None:
            progress = dict(progress or {})
            partial_update = progress.pop("partial", None)
            if isinstance(partial_update, dict):
                _merge_partial(run_id, partial_update)
            MAIL_AGENT_RUNS[run_id]["stage"] = stage
            MAIL_AGENT_RUNS[run_id]["progress"] = progress
            MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
            _save_run_checkpoint(run_id)

        outcome = await run_ai_turn(
            user_text,
            ui_context,
            arguments,
            sampling_create_message=sampling,
            progress_callback=_update_progress,
        )

        kind = str(outcome.get("kind") or "chat")
        result_data: dict[str, Any] = {
            "success": True,
            "kind": kind,
            "assistant_text": str(outcome.get("assistant_text") or ""),
            "route": outcome.get("route") if isinstance(outcome.get("route"), dict) else {},
            "fallback_used": bool(outcome.get("fallback_used")),
        }
        if kind == "clarify":
            result_data["clarify"] = str(outcome.get("clarify") or outcome.get("assistant_text") or "")
        if kind == "mail_context" and isinstance(outcome.get("mail_context"), dict):
            result_data["mail_context"] = outcome["mail_context"]
        if kind == "scan" and isinstance(outcome.get("scan_result"), dict):
            # 中文注释：展开 Ask 结果字段，便于前端复用 buildCustomRunResult。
            scan = outcome["scan_result"]
            result_data.update({
                "plan_id": scan.get("plan_id", ""),
                "plan_title": scan.get("plan_title", ""),
                "plan_description": scan.get("plan_description", ""),
                "summary": scan.get("summary", result_data["assistant_text"]),
                "sections": scan.get("sections", []),
                "plan_topics": scan.get("plan_topics", []),
                "plan_timeframe": scan.get("plan_timeframe", ""),
                "plan_direction": scan.get("plan_direction", ""),
                "plan_goal": scan.get("plan_goal", ""),
                "plan_queries": scan.get("plan_queries", []),
                "messages_scanned": scan.get("messages_scanned", 0),
                "candidates_found": scan.get("candidates_found", 0),
                "llm_meta": scan.get("llm_meta", {}),
            })
            if not result_data["assistant_text"]:
                result_data["assistant_text"] = str(scan.get("summary") or "")

        MAIL_AGENT_RUNS[run_id].update({
            "status": "done",
            "stage": "done",
            "updated_at": beijing_now(),
            "result": result_data,
            "error": "",
        })
        _save_run_checkpoint(run_id)
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update({
            "status": "failed",
            "stage": "failed",
            "updated_at": beijing_now(),
            "error": str(exc),
        })
        _save_run_checkpoint(run_id)


__all__ = ["start_ai_turn"]
