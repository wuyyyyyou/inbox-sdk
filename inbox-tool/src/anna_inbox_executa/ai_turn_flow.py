"""AI 侧栏统一 turn 入口：可轮询 run + Sampling 绑定 invoke。"""

from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from anna_inbox_executa.common import get_ai_sidebar_mode, log
from anna_inbox_executa.sampling_tools import *
from anna_inbox_executa.diagnostics import activate_trace, current_trace, deactivate_trace, snapshot


def _gmail_access_failed_without_results(trace: dict[str, Any] | None, candidates_found: int) -> bool:
    """判断 AI 检索是否因 Gmail 授权失败而没有得到任何可用邮件。"""
    if candidates_found > 0:
        return False
    diagnostic = snapshot(trace)
    spans = diagnostic.get("spans") if isinstance(diagnostic, dict) else []
    return any(
        isinstance(span, dict)
        and span.get("stage") == "gmail.http"
        and str(span.get("http_status") or "") in {"401", "403"}
        for span in spans
    )


def _gmail_access_unavailable_message(user_text: str) -> str:
    """生成授权失效时的可执行用户提示，不把认证错误伪装成零搜索结果。"""
    if any("\u3400" <= char <= "\u9fff" for char in str(user_text or "")):
        return "AI 暂时无法读取邮箱。Google 账号授权已失效或权限不足，请在设置中重新连接 Google 账号，刷新收件箱后再重试。"
    return "AI is temporarily unavailable because Google account access has expired or lacks permission. Reconnect your Google account in Settings, refresh the inbox, then try again."


def _confirmed_evidence_thread_ids(outcome: dict[str, Any]) -> list[str]:
    """提取已确认 Evidence 的线程标识，供前端将 THREAD_REF 限定为可打开详情。

    本地 Agent 的完整 Evidence 只在后端工具环内流转，轮询结果不能携带邮件主题、
    发件人或正文。这里仅在严格命中时公开去重后的线程 ID；无命中和相近结果均返回
    空列表，避免前端把非确认邮件渲染为可点击入口。
    """
    if str(outcome.get("match_status") or "") != "confirmed":
        return []
    rows = outcome.get("results") if isinstance(outcome.get("results"), list) else []
    thread_ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        thread_id = str(row.get("thread_id") or row.get("thread_ref") or "").strip()
        thread_id = thread_id.removeprefix("THREAD_REF_").strip()
        if thread_id and thread_id not in seen:
            seen.add(thread_id)
            thread_ids.append(thread_id)
    return thread_ids


def _confirmed_evidence_thread_labels(outcome: dict[str, Any]) -> dict[str, str]:
    """提取已确认线程的主题，供 Local 侧栏为详情入口显示邮件标题。

    标题与线程 ID 均来自同一轮严格命中的 Evidence；相近结果、模型自行生成的
    标题或空主题不会进入轮询结果，避免详情按钮错误地指向未确认邮件。
    """
    if str(outcome.get("match_status") or "") != "confirmed":
        return {}
    rows = outcome.get("results") if isinstance(outcome.get("results"), list) else []
    labels: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        thread_id = str(row.get("thread_id") or row.get("thread_ref") or "").strip()
        thread_id = thread_id.removeprefix("THREAD_REF_").strip()
        subject = " ".join(str(row.get("subject") or "").split())[:160]
        if thread_id and subject and thread_id not in labels:
            labels[thread_id] = subject
    return labels


def _log_ai_turn_failure(run_id: str, error: object) -> None:
    """把前端可见失败码写入 stderr，同时避免日志记录模型正文或凭据。"""
    detail = " ".join(str(error or "unknown error").split())[:300]
    # LLM JSON 解析错误有时附带模型原文 preview/excerpt，不能写入后端日志。
    if any(marker in detail.casefold() for marker in ("preview=", "excerpt=", "authorization:")):
        detail = type(error).__name__
    log(f"ai_turn failed: run_id={run_id[:80]} error={detail}")


def _public_ai_turn_state(run_id: str) -> dict[str, Any]:
    state = MAIL_AGENT_RUNS.get(run_id) or {}
    status = str(state.get("status") or "queued")
    progress = state.get("progress") if isinstance(state.get("progress"), dict) else {}
    result = _compact_run_result(state.get("result"))
    # 尽早把 scan_query 顶到公开状态，便于 Host/前端在 Thinking 后展示可点 chip
    scan_query = ""
    if isinstance(progress, dict):
        scan_query = str(progress.get("scan_query") or "").strip()
    if not scan_query and isinstance(result, dict):
        scan_query = str(result.get("scan_query") or "").strip()
    return {
        # JSON-RPC facade 将 success:false 解释为本次工具调用失败并直接抛错。
        # running/queued 是已成功建立、等待前端轮询的异步状态，不能误报失败。
        "success": status != "failed",
        "run_id": run_id,
        "status": status,
        "stage": state.get("stage", ""),
        "progress": progress,
        "partial": state.get("partial", {}),
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "result": result,
        "error": state.get("error", ""),
        "needs_continue": bool(state.get("needs_continue")),
        "scan_query": scan_query,
        "scan_source": "cache" if scan_query else "",
        "diagnostics": snapshot(state.get("diagnostics")),
    }


def start_ai_turn(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """启动一次 AI turn：阻塞至 wait_timeout，超时后返回可轮询状态。

    与 start_custom_scan 相同，保持 invoke 存活以便 Sampling 反向 RPC。
    本地侧栏开关开启时，前端会改走本入口而非 Host Agent Session。
    """
    run_id = str(arguments.get("run_id") or "").strip()
    if not run_id or len(run_id) < 8:
        run_id = f"at_{uuid.uuid4().hex[:12]}"
    existing = MAIL_AGENT_RUNS.get(run_id)
    if existing:
        # 同一 run_id 重试必须复用后台任务，避免重复 LLM / Gmail 调用。
        return _public_ai_turn_state(run_id)

    # 仅 stderr 日志：标明入口；sidebar_local 走与 Host 同工具的本地 Agent 环。
    sidebar_mode = get_ai_sidebar_mode()
    source = str(arguments.get("source") or arguments.get("entry") or "start_ai_turn").strip()[:64]
    path = "local_agent_session" if source == "sidebar_local" else "local_router"
    log(
        f"ai_sidebar path={path} mode={sidebar_mode} "
        f"source={source or 'start_ai_turn'} run_id={run_id[:80]}"
    )

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
        "sampling": {},
        "diagnostics": current_trace(),
    }
    _save_run_checkpoint(run_id)
    # 后台任务在 loop 线程重绑 invoke_id，供 sampling reverse RPC 注入。
    from executa_sdk.context import run_with_invoke_id
    future = asyncio.run_coroutine_threadsafe(
        run_with_invoke_id(invoke_id, _start_ai_turn_async(run_id, arguments, invoke_id)),
        loop,
    )
    # 首个 invoke 只负责快速建立后台 run；完整邮件分析由前端轮询 run_id。
    # 等待过久不会加快最终回答，反而会占用平台 60 秒工具窗口。
    try:
        requested_wait_timeout = int(arguments.get("wait_timeout_seconds", 5))
    except (TypeError, ValueError):
        requested_wait_timeout = 5
    wait_timeout = max(1, min(requested_wait_timeout, 5))
    try:
        future.result(timeout=wait_timeout)
    except FutureTimeoutError:
        # 超时不取消后台；前端用 get_mail_agent_run 继续轮询。
        state = MAIL_AGENT_RUNS[run_id]
        state["status"] = "running"
        state["needs_continue"] = True
        state["updated_at"] = beijing_now()
        _save_run_checkpoint(run_id)
    return _public_ai_turn_state(run_id)


async def _start_ai_turn_async(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    """异步执行 AI turn。

    - ``source=sidebar_local``：本地 Agent 环 + Host 同款 ``ai_*`` 工具（侧栏本地开关）。
    - 其它：详情/兼容路径，本地 Router + 阶段 C 白名单工具。
    """
    from mail_agent.ai_turn.runner import run_ai_turn

    trace_token = activate_trace((MAIL_AGENT_RUNS.get(run_id) or {}).get("diagnostics"))
    try:
        MAIL_AGENT_RUNS[run_id]["status"] = "running"
        MAIL_AGENT_RUNS[run_id]["stage"] = "routing"
        MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
        _save_run_checkpoint(run_id)

        sampling = _build_sampling_for_run(arguments, invoke_id)
        sampling_snapshot = getattr(sampling, "budget_snapshot", None) if sampling else None
        if callable(sampling_snapshot):
            MAIL_AGENT_RUNS[run_id]["sampling"] = sampling_snapshot()
        user_text = str(arguments.get("user_text") or arguments.get("user_request") or "").strip()
        ui_context = arguments.get("ui_context") if isinstance(arguments.get("ui_context"), dict) else {}
        source = str(arguments.get("source") or arguments.get("entry") or "").strip()

        def _update_progress(stage: str, progress: dict[str, Any] | None = None) -> None:
            progress = dict(progress or {})
            partial_update = progress.pop("partial", None)
            if isinstance(partial_update, dict):
                _merge_partial(run_id, partial_update)
            MAIL_AGENT_RUNS[run_id]["stage"] = stage
            MAIL_AGENT_RUNS[run_id]["progress"] = progress
            if callable(sampling_snapshot):
                MAIL_AGENT_RUNS[run_id]["sampling"] = sampling_snapshot()
            MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
            _save_run_checkpoint(run_id)

        if source == "sidebar_local":
            from anna_inbox_executa.local_agent_session import run_local_agent_session

            outcome = await run_local_agent_session(
                user_text,
                ui_context,
                arguments,
                sampling_create_message=sampling,
                progress_callback=_update_progress,
                invoke_id=invoke_id,
            )
        else:
            outcome = await run_ai_turn(
                user_text,
                ui_context,
                arguments,
                sampling_create_message=sampling,
                progress_callback=_update_progress,
            )

        kind = str(outcome.get("kind") or "chat")
        scan_result = outcome.get("scan_result") if isinstance(outcome.get("scan_result"), dict) else None
        candidates_found = int(scan_result.get("candidates_found") or 0) if scan_result else 0
        if kind == "scan" and scan_result and _gmail_access_failed_without_results(
            MAIL_AGENT_RUNS[run_id].get("diagnostics"), candidates_found,
        ):
            # 底层兼容路径可能把 401 转成空结果；最终出口必须恢复真实错误语义。
            unavailable = _gmail_access_unavailable_message(user_text)
            scan_result = {
                **scan_result,
                "title": "",
                "summary": unavailable,
                "sections": [],
                "availability_error": "gmail_authorization",
            }
            outcome = {**outcome, "scan_result": scan_result, "assistant_text": unavailable, "fallback_used": True}
        result_data: dict[str, Any] = {
            "success": kind != "error",
            "kind": kind,
            "assistant_text": str(outcome.get("assistant_text") or ""),
            "fallback_used": bool(outcome.get("fallback_used")),
        }
        evidence_thread_ids = _confirmed_evidence_thread_ids(outcome)
        if evidence_thread_ids:
            # Local Agent 以轮询结果回到前端时，保留已确认线程的最小引用边界。
            # 前端据此恢复 Open email 入口，并继续过滤模型编造或相近结果的引用。
            result_data["evidence_thread_ids"] = evidence_thread_ids
            evidence_thread_labels = _confirmed_evidence_thread_labels(outcome)
            if evidence_thread_labels:
                result_data["evidence_thread_labels"] = evidence_thread_labels
        if callable(sampling_snapshot):
            # 仅公开预算与用量聚合，不包含 prompt、邮件内容、模型输出或凭据。
            result_data["sampling"] = sampling_snapshot()
        if kind == "error":
            result_data["error"] = str(outcome.get("error") or "error")
        if kind == "clarify":
            result_data["clarify"] = str(outcome.get("clarify") or outcome.get("assistant_text") or "")
            # 澄清选项只包含路由范围和用户原始输入，不携带邮件内容或凭据。
            if isinstance(outcome.get("clarification"), dict):
                result_data["clarification"] = outcome["clarification"]
        if kind in {"mail_context", "draft"} and isinstance(outcome.get("mail_context"), dict):
            result_data["mail_context"] = outcome["mail_context"]
        if isinstance(outcome.get("artifact"), dict):
            result_data["artifact"] = outcome["artifact"]
        # 批量写稿：多 artifact 按封隔离（阶段 C）
        if isinstance(outcome.get("artifacts"), list):
            result_data["artifacts"] = [
                item for item in outcome["artifacts"] if isinstance(item, dict)
            ][:20]
            if not result_data.get("artifact") and result_data["artifacts"]:
                result_data["artifact"] = result_data["artifacts"][0]
        if isinstance(outcome.get("batch_failures"), list):
            result_data["batch_failures"] = outcome["batch_failures"][:20]
        if kind == "propose" and isinstance(outcome.get("proposed_actions"), dict):
            result_data["proposed_actions"] = outcome["proposed_actions"]
            result_data["requires_user_confirmation"] = True
        if kind == "memory" and isinstance(outcome.get("memory"), dict):
            result_data["memory"] = outcome["memory"]
        if kind == "scan" and isinstance(outcome.get("scan_result"), dict):
            # 展开 Ask 结果字段，便于前端复用 buildCustomRunResult。
            scan = outcome["scan_result"]
            candidates_found = int(scan.get("candidates_found") or 0)
            result_data.update({
                "plan_id": scan.get("plan_id", ""),
                "summary": scan.get("summary", result_data["assistant_text"]),
                "sections": scan.get("sections", []),
                "messages_scanned": scan.get("messages_scanned", 0),
                "candidates_found": candidates_found,
            })
            if candidates_found:
                result_data["plan_title"] = scan.get("plan_title", "")
                result_data["plan_description"] = scan.get("plan_description", "")
            if not result_data["assistant_text"]:
                result_data["assistant_text"] = str(scan.get("summary") or "")
            # scan 的 summary 已是侧栏唯一文本来源，避免与 assistant_text 重复传输。
            result_data.pop("assistant_text", None)
        # compose 路径也可能附带 scan_result
        if kind == "draft" and isinstance(outcome.get("scan_result"), dict) and "summary" not in result_data:
            scan = outcome["scan_result"]
            result_data["search_summary"] = str(scan.get("summary") or "")[:500]

        run_error = str(outcome.get("error") or "") if kind == "error" else ""
        if run_error:
            _log_ai_turn_failure(run_id, run_error)
        MAIL_AGENT_RUNS[run_id].update({
            "status": "done" if kind != "error" else "failed",
            "stage": "done" if kind != "error" else "failed",
            "updated_at": beijing_now(),
            "result": result_data,
            "error": run_error,
        })
        _save_run_checkpoint(run_id)
    except Exception as exc:
        _log_ai_turn_failure(run_id, exc)
        MAIL_AGENT_RUNS[run_id].update({
            "status": "failed",
            "stage": "failed",
            "updated_at": beijing_now(),
            "error": str(exc),
        })
        _save_run_checkpoint(run_id)
    finally:
        deactivate_trace(trace_token)


async def apply_proposed_actions_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    """用户确认整理建议后执行 mutation（非 Router 路径）。"""
    from mail_agent.ai_turn.tools import apply_proposed_actions

    action = str(arguments.get("action") or "").strip()
    items = arguments.get("items") if isinstance(arguments.get("items"), list) else []
    return await apply_proposed_actions(action=action, items=items)


async def list_saved_prompts_tool(_arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    from mail_agent.ai_turn.personalization import list_saved_prompts
    return await list_saved_prompts()


async def save_saved_prompt_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    from mail_agent.ai_turn.personalization import save_saved_prompt
    return await save_saved_prompt(
        prompt_id=str(arguments.get("prompt_id") or arguments.get("id") or ""),
        title=str(arguments.get("title") or ""),
        body=str(arguments.get("body") or arguments.get("text") or ""),
    )


async def delete_saved_prompt_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    from mail_agent.ai_turn.personalization import delete_saved_prompt
    return await delete_saved_prompt(str(arguments.get("prompt_id") or arguments.get("id") or ""))


async def list_ai_memories_tool(_arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    from mail_agent.ai_turn.personalization import list_ai_memories
    return await list_ai_memories()


async def add_ai_memory_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    from mail_agent.ai_turn.personalization import add_ai_memory
    return await add_ai_memory(
        str(arguments.get("text") or arguments.get("preference") or ""),
        source=str(arguments.get("source") or "settings"),
    )


async def delete_ai_memory_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    from mail_agent.ai_turn.personalization import delete_ai_memory
    return await delete_ai_memory(str(arguments.get("memory_id") or arguments.get("id") or ""))


__all__ = [
    "add_ai_memory_tool",
    "apply_proposed_actions_tool",
    "delete_ai_memory_tool",
    "delete_saved_prompt_tool",
    "list_ai_memories_tool",
    "list_saved_prompts_tool",
    "save_saved_prompt_tool",
    "start_ai_turn",
]
