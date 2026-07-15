from __future__ import annotations

from anna_inbox_executa.common import *
from anna_inbox_executa.gmail_tools import *
from anna_inbox_executa.storage_tools import *
from anna_inbox_executa.sampling_tools import *
from anna_inbox_executa.brief_flow import *
from anna_inbox_executa.ask_flow import *
from anna_inbox_executa.ai_turn_flow import *
from anna_inbox_executa.contact_memory_flow import *
from anna_inbox_executa.mailbox_tools import *
from anna_inbox_executa.card_tools import *
from anna_inbox_executa.v2_tools import *

async def _handle_ai_personalization_tool(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """阶段 B 个性化与确认执行工具分发。"""
    if tool == "apply_proposed_actions":
        return await apply_proposed_actions_tool(arguments)
    if tool == "list_saved_prompts":
        return await list_saved_prompts_tool(arguments)
    if tool == "save_saved_prompt":
        return await save_saved_prompt_tool(arguments)
    if tool == "delete_saved_prompt":
        return await delete_saved_prompt_tool(arguments)
    if tool == "list_ai_memories":
        return await list_ai_memories_tool(arguments)
    if tool == "add_ai_memory":
        return await add_ai_memory_tool(arguments)
    if tool == "delete_ai_memory":
        return await delete_ai_memory_tool(arguments)
    return {"success": False, "error": f"unknown_tool:{tool}"}


def _resolve_continue_future(future: Any, *, run_id: str, timeout: float, label: str) -> dict[str, Any]:
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        future.cancel()
        state = _get_run_state(run_id)
        if state:
            warnings = state.setdefault("warnings", [])
            warnings.append({
                "stage": "invoke_timeout",
                "at": beijing_now(),
                "detail": {"label": label, "timeout_seconds": timeout},
            })
            state["warnings"] = warnings[-10:]
            state["status"] = "running"
            state["needs_continue"] = True
            state["updated_at"] = beijing_now()
            _save_run_checkpoint(run_id)
            return _public_run_view(state)
        return {
            "success": False,
            "run_id": run_id,
            "error": f"{label} exceeded {timeout:.0f}s invoke budget",
            "needs_continue": True,
        }


def handle_invoke(params: dict[str, Any]) -> dict[str, Any]:
    tool = params.get("tool")
    arguments = params.get("arguments") or {}
    context = params.get("context") or {}
    invoke_id = str(params.get("invoke_id") or "")
    _apply_storage_provider(arguments)
    apply_runtime_credentials(context)

    if tool == "check_google_oauth":
        return {"success": True, "tool": tool, "data": check_google_oauth(context)}
    if tool == "test_aps_storage":
        future = asyncio.run_coroutine_threadsafe(run_aps_storage_smoke(arguments), loop)
        return {"success": True, "tool": tool, "data": future.result(timeout=60.0)}
    if tool == "read_primary_emails":
        return {"success": True, "tool": tool, "data": read_primary_emails(arguments.get("mailbox", ""), arguments.get("limit", 5))}
    if tool == "list_inbox_emails":
        return {"success": True, "tool": tool, "data": list_inbox_emails(
            arguments.get("mailbox", ""),
            arguments.get("days", 30),
            arguments.get("limit", 100),
            arguments.get("category", "inbox"),
            arguments.get("clear_cache", False),
        )}
    if tool == "list_cached_emails":
        return {
            "success": True,
            "tool": tool,
            "data": list_cached_emails(
                arguments.get("mailbox", ""),
                arguments.get("days", 30),
                arguments.get("limit", 100),
                arguments.get("category", "all"),
                arguments.get("offset", 0),
            ),
        }
    if tool == "list_gmail_emails_page":
        return {
            "success": True,
            "tool": tool,
            "data": list_gmail_emails_page(
                arguments.get("mailbox", ""),
                arguments.get("days", 30),
                arguments.get("limit", 100),
                arguments.get("category", "all"),
                arguments.get("page_token", ""),
                arguments.get("page_offset", 0),
                arguments.get("exclude_message_ids", []),
            ),
        }
    if tool == "get_cached_email":
        return {"success": True, "tool": tool, "data": get_cached_email(arguments.get("mailbox", ""), arguments.get("message_id", ""))}
    if tool == "resolve_contact_avatars":
        return {"success": True, "tool": tool, "data": resolve_contact_avatars(arguments.get("mailbox", ""), arguments.get("emails", []))}
    if tool == "check_gmail_auth":
        return {"success": True, "tool": tool, "data": _check_gmail_auth(arguments.get("mailbox", ""))}
    if tool == "check_sampling_status":
        future = asyncio.run_coroutine_threadsafe(_check_sampling_status(arguments, invoke_id), loop)
        return {"success": True, "tool": tool, "data": future.result(timeout=12.0)}
    if tool == "get_sampling_debug":
        info = sampling.get_debug_info()
        info["executa_manifest_host_capabilities"] = MANIFEST.get("host_capabilities", [])
        info["executa_tool_id"] = TOOL_ID
        info["executa_version"] = VERSION
        return {"success": True, "tool": tool, "data": info}
    if tool == "test_sampling":
        future = asyncio.run_coroutine_threadsafe(_test_sampling(arguments, invoke_id), loop)
        return {"success": True, "tool": tool, "data": future.result(timeout=120.0)}
    if tool == "test_sampling_brief":
        future = asyncio.run_coroutine_threadsafe(_test_sampling_brief(arguments, invoke_id), loop)
        return {"success": True, "tool": tool, "data": future.result(timeout=180.0)}
    if tool == "test_sampling_async":
        return {"success": True, "tool": tool, "data": _start_test_sampling_async(arguments, invoke_id)}
    if tool == "start_mail_agent_run":
        return {"success": True, "tool": tool, "data": start_mail_agent_run(arguments, invoke_id)}
    if tool == "continue_mail_agent_run":
        future = asyncio.run_coroutine_threadsafe(_continue_mail_agent_run_async(arguments, invoke_id), loop)
        return {"success": True, "tool": tool, "data": _resolve_continue_future(
            future,
            run_id=str(arguments.get("run_id") or ""),
            timeout=50.0,
            label="continue_mail_agent_run",
        )}
    if tool == "get_mail_agent_run":
        return {"success": True, "tool": tool, "data": get_mail_agent_run(arguments.get("run_id", ""))}
    if tool == "start_custom_scan":
        return {"success": True, "tool": tool, "data": start_custom_scan(arguments, invoke_id)}
    if tool == "re_run_custom_scan":
        return {"success": True, "tool": tool, "data": re_run_custom_scan(arguments, invoke_id)}
    if tool == "start_ai_turn":
        return {"success": True, "tool": tool, "data": start_ai_turn(arguments, invoke_id)}
    # 阶段 B：整理确认 / Saved prompts / AI Memory（均非 Router 静默 mutation）
    if tool in (
        "apply_proposed_actions",
        "list_saved_prompts",
        "save_saved_prompt",
        "delete_saved_prompt",
        "list_ai_memories",
        "add_ai_memory",
        "delete_ai_memory",
    ):
        future = asyncio.run_coroutine_threadsafe(
            _handle_ai_personalization_tool(tool, arguments),
            loop,
        )
        try:
            return {"success": True, "tool": tool, "data": future.result(timeout=120.0)}
        except Exception as exc:
            return {"success": False, "tool": tool, "error": str(exc)}
    if tool == "get_authorized_email":
        discovered = _discover_mailboxes()
        authorized = [d["email"] for d in discovered if d.get("authorized")]
        return {
            "success": True, "tool": tool,
            "data": {
                "mailboxes": authorized,
                "primary": authorized[0] if authorized else "",
                "source": discovered[0]["auth_source"] if discovered else "none",
                "discovered": discovered,
            }
        }
    if tool == "get_custom_plans":
        return {"success": True, "tool": tool, "data": _sync_get_custom_plans()}
    if tool == "get_custom_plan_detail":
        return {"success": True, "tool": tool, "data": _sync_get_custom_plan_detail(arguments)}
    if tool == "list_contact_memories":
        return {"success": True, "tool": tool, "data": _sync_list_contact_memories(arguments)}
    if tool == "get_contact_memory":
        return {"success": True, "tool": tool, "data": _sync_get_contact_memory(arguments)}
    if tool == "delete_contact_memory":
        return {"success": True, "tool": tool, "data": _sync_delete_contact_memory(arguments)}
    if tool == "clear_contact_memories":
        return {"success": True, "tool": tool, "data": _sync_clear_contact_memories(arguments)}
    if tool == "generate_contact_memories":
        return {"success": True, "tool": tool, "data": _start_contact_memory_run(arguments)}
    if tool == "start_contact_memory_run":
        return {"success": True, "tool": tool, "data": _start_contact_memory_run(arguments)}
    if tool == "continue_contact_memory_run":
        future = asyncio.run_coroutine_threadsafe(_continue_contact_memory_run_async(arguments, invoke_id), loop)
        return {"success": True, "tool": tool, "data": _resolve_continue_future(
            future,
            run_id=str(arguments.get("run_id") or ""),
            timeout=50.0,
            label="continue_contact_memory_run",
        )}

    if tool == "get_active_cards":
        return {"success": True, "tool": tool, "data": _sync_get_active_cards(arguments)}
    if tool == "get_cleanup_bundle_page":
        return {"success": True, "tool": tool, "data": _sync_get_cleanup_bundle_page(arguments)}
    if tool == "list_mailboxes":
        return {"success": True, "tool": tool, "data": _sync_list_mailboxes()}
    if tool == "get_mailbox_registry":
        return {"success": True, "tool": tool, "data": _sync_get_mailbox_registry()}
    if tool == "set_mailbox_selected":
        return {"success": True, "tool": tool, "data": _sync_set_mailbox_selected(arguments)}
    if tool == "remove_mailbox":
        return {"success": True, "tool": tool, "data": _sync_remove_mailbox(arguments)}
    if tool == "get_run_history":
        return {"success": True, "tool": tool, "data": _sync_get_run_history()}

    # ── V2 interaction tools (async → dispatch to event loop) ──
    if tool in (
        "get_card_detail", "get_inbox_thread_page", "get_inbox_message_display_body", "get_thread_context_page",
        "prepare_inbox_attachment_access", "prepare_attachment_download", "summarize_thread",
        "generate_draft_reply", "generate_ask_draft", "revise_draft", "record_card_decision",
        "clear_active_cards", "mark_card_read", "mark_cleanup_read", "record_snooze", "restore_card", "record_learning",
        "start_summarize_thread", "start_inbox_thread_assist", "start_generate_draft", "start_inbox_mail_prompt", "start_compose_mail_prompt",
        "delete_custom_plan",
        "clear_cards", "clear_history", "reset_all_data", "reset_mailbox_scan_history", "delete_mailbox_data",
        "get_scan_plan", "set_scan_plan", "get_inbox_settings", "save_inbox_settings",
        "reply_now", "reply_from_ask", "mark_read_from_ask", "trash_from_ask", "get_inbox_thread_draft",
        "list_inbox_thread_drafts",
        "save_inbox_thread_draft", "delete_inbox_thread_draft", "modify_message_labels", "set_message_starred",
        "update_inbox_thread_state", "search_compose_contacts", "get_compose_draft", "create_or_update_compose_draft",
        "delete_compose_draft", "list_compose_drafts", "send_compose_emails",
    ):
        future = asyncio.run_coroutine_threadsafe(
            _handle_v2_tool(tool, arguments, invoke_id),
            loop,
        )
        try:
            return {"success": True, "tool": tool, "data": future.result(timeout=180.0)}
        except SamplingError as exc:
            raise RuntimeError(json.dumps({"code": exc.code, "message": exc.message, "data": exc.data}, ensure_ascii=False)) from exc

    if tool == "run_mail_agent":
        future = asyncio.run_coroutine_threadsafe(
            run_mail_agent_pipeline(
                user_request=arguments.get("user_request", ""),
                mailbox=arguments.get("mailbox", ""),
                mode=arguments.get("mode", "auto"),
                max_messages=arguments.get("max_messages", arguments.get("primary_count", 20)),
                primary_count=arguments.get("primary_count", 20),
                ai_provider=arguments.get("ai_provider", "anna-llm"),
                invoke_id=invoke_id,
            ),
            loop,
        )
        try:
            return {"success": True, "tool": tool, "data": future.result(timeout=600.0)}
        except SamplingError as exc:
            raise RuntimeError(json.dumps({"code": exc.code, "message": exc.message, "data": exc.data}, ensure_ascii=False)) from exc

    raise ValueError(f"Unknown tool: {tool}")

__all__ = [name for name in globals() if not name.startswith("__")]
