from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Windows pipe encoding: -X utf8 covers console but not pipes;
# explicit reconfigure ensures stdin/stdout are UTF-8 regardless.
for _stream in (sys.stdin, sys.stdout):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        log(f"reconfigure {_stream} failed")

# Ensure src/ is on sys.path so mail_agent and executa_sdk are importable
# when running via `py -3 src/anna_inbox_executa/main.py`
_SRC_DIR = str(Path(__file__).resolve().parents[1])
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


def _is_platform() -> bool:
    """检测是否运行在 Anna 平台（非本地 dev）。"""
    if getattr(sys, "_MEIPASS", ""):
        return True
    if os.environ.get("GMAIL_ACCESS_TOKEN") or os.environ.get("GOOGLE_ACCESS_TOKEN"):
        return True
    return False


def data_root() -> Path:
    """返回统一的数据根目录。平台模式用 CWD/.data，本地 dev 用 src/.data。"""
    if _is_platform():
        return Path("./.data/").resolve()
    return (Path(__file__).resolve().parents[1] / ".data").resolve()

from executa_sdk import PROTOCOL_VERSION_V2, SamplingClient, SamplingError
from executa_sdk.storage import StorageClient, FilesClient, StorageError, make_response_router
from mail_agent.storage.keys import app_key

JSONRPC_VERSION = "2.0"
DEFAULT_TOOL_ID = "inbox-tool"
DEFAULT_VERSION = "0.2.0"
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
STDOUT_LOCK = threading.Lock()
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
TOKEN_URI = "https://oauth2.googleapis.com/token"
MAX_STDIO_MESSAGE_BYTES = 512 * 1024

DEFAULT_MANIFEST = {
    "name": DEFAULT_TOOL_ID,
    "display_name": "Zhaopy Mail Agent RD6B87R5",
    "version": DEFAULT_VERSION,
    "description": "Minimal Anna Executa skeleton for reading Gmail through local token files and DashScope or Anna sampling LLM.",
    "author": "Zhaopy",
    "host_capabilities": [
        "llm.sample",
        "llm.complete",
        "storage.user",
        "aps.kv",
        "aps.scope.user.read",
        "aps.scope.user.write",
    ],
    "credentials": [
        {
            "name": "GMAIL_ACCESS_TOKEN",
            "display_name": "Gmail Access Token",
            "description": "Optional Google OAuth access token. Local token files are used when platform injection is unavailable.",
            "required": True,
            "sensitive": True,
        },
        {
            "name": "GOOGLE_ACCESS_TOKEN",
            "display_name": "Google Access Token",
            "description": "Alternative Google OAuth access token name supported by Anna platform credential mapping.",
            "required": True,
            "sensitive": True,
        },
        {
            "name": "DASHSCOPE_API_KEY",
            "display_name": "DashScope API Key",
            "description": "DashScope API key for direct LLM access.",
            "required": True,
            "sensitive": True,
        },
        {
            "name": "DASHSCOPE_MODEL",
            "display_name": "DashScope Model",
            "description": "DashScope model name (e.g. qwen3-max).",
            "required": False,
            "sensitive": False,
        },
        {
            "name": "GMAIL_MULTI_TOKENS",
            "display_name": "Gmail Multi-Mailbox Tokens",
            "description": "JSON array of {email, access_token, refresh_token, client_id, client_secret, expires_at} for additional mailboxes.",
            "required": False,
            "sensitive": True,
        },
    ],
    "tools": [
        {
            "name": "check_google_oauth",
            "description": "Check whether Anna injected a Google/Gmail OAuth credential for this invocation.",
            "parameters": [],
        },
        {
            "name": "test_aps_storage",
            "description": "Smoke-test Anna Persistent Storage KV without touching Gmail or the mail-agent pipeline.",
            "parameters": [
                {
                    "name": "key_suffix",
                    "type": "string",
                    "description": "Optional suffix for the temporary debug key.",
                    "required": False,
                },
                {
                    "name": "value",
                    "type": "string",
                    "description": "Optional value to write and read back.",
                    "required": False,
                },
            ],
        },
        {
            "name": "read_primary_emails",
            "description": "Read recent Gmail Primary emails through local token files, save them as JSON, and deduplicate by message id.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email.", "required": True},
                {"name": "limit", "type": "integer", "description": "How many recent Primary messages to read.", "required": False},
            ],
        },
        {
            "name": "list_cached_emails",
            "description": "List compact local Gmail message summaries for one mailbox.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email.", "required": True},
            ],
        },
        {
            "name": "get_cached_email",
            "description": "Read one full locally cached Gmail message JSON by message id.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email.", "required": True},
                {"name": "message_id", "type": "string", "description": "Gmail message id.", "required": True},
            ],
        },
        {
            "name": "check_gmail_auth",
            "description": "Check whether Gmail authorization is available. Platform: checks for injected OAuth token. Local: checks for token file existence (does not validate expiry).",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
            ],
        },
        {
            "name": "run_mail_agent",
            "description": "Run the full Anna mail agent pipeline: scan emails, generate candidates, evaluate with LLM, and produce an action plan.",
            "parameters": [
                {"name": "user_request", "type": "string", "description": "Natural language request from user, e.g. '帮我看看有什么重要邮件'", "required": True},
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "mode", "type": "string", "description": "Strategy mode: auto, default_secretary, creator_opportunity, security_billing", "required": False},
                {"name": "max_messages", "type": "integer", "description": "Maximum messages to scan.", "required": False},
                {"name": "primary_count", "type": "integer", "description": "How many recent Primary emails to fetch first.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider: dashscope or anna-llm.", "required": False},
            ],
        },
        {
            "name": "start_mail_agent_run",
            "description": "Start the full mail-agent pipeline in the background and poll status separately.",
            "parameters": [
                {"name": "user_request", "type": "string", "description": "Natural language request from user.", "required": True},
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "mode", "type": "string", "description": "Strategy mode: auto, default_secretary, creator_opportunity, security_billing", "required": False},
                {"name": "max_messages", "type": "integer", "description": "Maximum messages to scan.", "required": False},
                {"name": "primary_count", "type": "integer", "description": "How many recent Primary emails to fetch first.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider: dashscope or anna-llm.", "required": False},
            ],
        },
        {
            "name": "get_mail_agent_run",
            "description": "Poll a background mail-agent run by run id.",
            "parameters": [
                {"name": "run_id", "type": "string", "description": "Run id returned by start_mail_agent_run.", "required": True},
            ],
        },
        {
            "name": "get_active_cards",
            "description": "Get all active attention cards for a mailbox from persistent storage.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
            ],
        },
        {
            "name": "list_mailboxes",
            "description": "Discover and list registered mailboxes, including selected and authorization state.",
            "parameters": [],
        },
        {
            "name": "set_mailbox_selected",
            "description": "Set whether a mailbox participates in Brief scans and card display.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "selected", "type": "boolean", "description": "Whether the mailbox is selected.", "required": True},
            ],
        },
        {
            "name": "remove_mailbox",
            "description": "Remove a mailbox from the registry without deleting its stored data.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
            ],
        },
        {
            "name": "get_scan_plan",
            "description": "Get the current scan plan configuration for a mailbox (schedule, time range, priorities, etc.).",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
            ],
        },
        {
            "name": "set_scan_plan",
            "description": "Save or update the scan plan for a mailbox.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "scan_window_days", "type": "integer", "description": "Days to look back for all scans.", "required": False},
                {"name": "max_messages", "type": "integer", "description": "Maximum messages per scan.", "required": False},
                {"name": "scan_categories", "type": "array", "description": "Extra Gmail categories to scan: promotions, social, updates, forums.", "required": False},
            ],
        },
        {
            "name": "get_card_detail",
            "description": "Get a single card's full detail including thread context.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID from get_active_cards.", "required": True},
            ],
        },
        {
            "name": "summarize_thread",
            "description": "Ask Anna to summarize a thread behind a card.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
            ],
        },
        {
            "name": "generate_draft_reply",
            "description": "Ask Anna to draft a reply for a card's thread.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
                {"name": "reply_mode", "type": "string", "description": "reply_to_sender or reply_all.", "required": False},
            ],
        },
        {
            "name": "revise_draft",
            "description": "Ask Anna to revise a draft reply based on user feedback.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
                {"name": "current_draft", "type": "string", "description": "The current draft text to revise.", "required": True},
                {"name": "revision_input", "type": "string", "description": "User's revision request.", "required": True},
            ],
        },
        {
            "name": "generate_ask_draft",
            "description": "Generate a draft reply for an Ask scan result item, incorporating user answers to clarifying questions.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "message_id", "type": "string", "description": "Gmail message ID of the email to reply to.", "required": True},
                {"name": "thread_id", "type": "string", "description": "Gmail thread ID.", "required": False},
                {"name": "from_addr", "type": "string", "description": "Sender email address.", "required": False},
                {"name": "subject", "type": "string", "description": "Email subject.", "required": False},
                {"name": "user_answers", "type": "object", "description": "Map of question text to user's answer.", "required": True},
            ],
        },
        {
            "name": "start_summarize_thread",
            "description": "Start a background thread summarization. Returns run_id immediately. Poll with get_mail_agent_run.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
                {"name": "ai_provider", "type": "string", "description": "LLM provider.", "required": False},
            ],
        },
        {
            "name": "start_generate_draft",
            "description": "Start background draft generation or revision. If current_draft is provided, revises it. Returns run_id immediately. Poll with get_mail_agent_run.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
                {"name": "reply_mode", "type": "string", "description": "reply_to_sender or reply_all.", "required": False},
                {"name": "current_draft", "type": "string", "description": "Existing draft to revise (optional).", "required": False},
                {"name": "revision_input", "type": "string", "description": "User instructions for generation or revision (optional).", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider.", "required": False},
            ],
        },
        {
            "name": "record_card_decision",
            "description": "Record a user decision on a card (no_action_needed, handled_manually, dismissed).",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
                {"name": "decision", "type": "string", "description": "no_action_needed | handled_manually | dismissed.", "required": True},
            ],
        },
        {
            "name": "clear_active_cards",
            "description": "Dismiss all active cards for a mailbox (resets the brief).",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
            ],
        },
        {
            "name": "mark_cleanup_read",
            "description": "Mark cleanup-bundle messages as read both in Gmail (remove UNREAD label) and locally.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Cleanup bundle card ID.", "required": True},
                {"name": "message_ids", "type": "array", "description": "Gmail message IDs to mark as read.", "required": True},
            ],
        },
        {
            "name": "record_snooze",
            "description": "Snooze a card (tomorrow, next_week) or record a permanent preference (dont_prioritize).",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
                {"name": "snooze_option", "type": "string", "description": "tomorrow | next_week | dont_prioritize.", "required": True},
            ],
        },
        {
            "name": "restore_card",
            "description": "Restore a snoozed/dismissed/resolved card back to pending.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
            ],
        },
        {
            "name": "clear_cards",
            "description": "Clear brief cards by category for a mailbox. Category: all, reply, review, or cleanup.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "category", "type": "string", "description": "all | reply | review | cleanup", "required": True},
            ],
        },
        {
            "name": "clear_history",
            "description": "Delete all run history records.",
        },
        {
            "name": "reset_all_data",
            "description": "Reset all persistent data — cards, history, cache, scan state, contacts. Returns app to first-run state.",
        },
        {
            "name": "record_learning",
            "description": "Record a learning feedback from the user about email patterns.",
            "parameters": [
                {"name": "pattern", "type": "string", "description": "The pattern the user noticed.", "required": True},
                {"name": "action", "type": "string", "description": "What action to remember.", "required": True},
            ],
        },
        {
            "name": "reply_now",
            "description": "Send the draft reply via Gmail. Default dry_run=True mocks the send without actually emailing.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
                {"name": "draft_body", "type": "string", "description": "The draft body to send.", "required": True},
                {"name": "reply_mode", "type": "string", "description": "reply_to_sender | reply_all.", "required": False},
                {"name": "dry_run", "type": "boolean", "description": "If true (default), mock the send without actually emailing.", "required": False},
            ],
        },
        {
            "name": "reply_from_ask",
            "description": "Send a reply via Gmail for Ask results (no card dependency).",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "thread_id", "type": "string", "description": "Gmail thread ID.", "required": True},
                {"name": "to_addr", "type": "string", "description": "Recipient email address.", "required": True},
                {"name": "body", "type": "string", "description": "Reply body text.", "required": True},
                {"name": "reply_mode", "type": "string", "description": "reply_to_sender | reply_all.", "required": False},
                {"name": "dry_run", "type": "boolean", "description": "Default true (mock). Set false to really send.", "required": False},
            ],
        },
        {
            "name": "mark_read_from_ask",
            "description": "Mark Gmail messages as read (remove UNREAD label) for Ask results.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "message_ids", "type": "array", "description": "Gmail message IDs to mark as read.", "required": True},
            ],
        },
        {
            "name": "trash_from_ask",
            "description": "Move Gmail messages to trash for Ask results.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "message_ids", "type": "array", "description": "Gmail message IDs to trash.", "required": True},
            ],
        },
        {
            "name": "get_run_history",
            "description": "Get the history of past mail agent runs.",
            "parameters": [],
        },
        {
            "name": "start_custom_scan",
            "description": "Start a custom (Ask) scan: generate a plan from natural language and execute it in the background.",
            "parameters": [
                {"name": "user_request", "type": "string", "description": "Natural language request from user.", "required": True},
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "max_messages", "type": "integer", "description": "Maximum messages to scan.", "required": False},
                {"name": "primary_count", "type": "integer", "description": "How many recent Primary emails to fetch first.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider: dashscope or anna-llm.", "required": False},
            ],
        },
        {
            "name": "re_run_custom_scan",
            "description": "Re-run a previously saved custom scan plan against the current inbox (skips LLM planning).",
            "parameters": [
                {"name": "plan_id", "type": "string", "description": "Plan ID from get_custom_plans.", "required": True},
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "max_messages", "type": "integer", "description": "Maximum messages to scan.", "required": False},
                {"name": "primary_count", "type": "integer", "description": "How many recent Primary emails to fetch first.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider: dashscope or anna-llm.", "required": False},
            ],
        },
        {
            "name": "get_authorized_email",
            "description": "Discover the authorized Gmail account from the platform-injected token. Returns empty string in local dev mode.",
            "parameters": [],
        },
        {
            "name": "get_custom_plans",
            "description": "List all saved custom scan plans with lightweight metadata.",
            "parameters": [],
        },
        {
            "name": "get_custom_plan_detail",
            "description": "Get a single custom plan's full details.",
            "parameters": [
                {"name": "plan_id", "type": "string", "description": "Plan ID from get_custom_plans.", "required": True},
            ],
        },
        {
            "name": "list_contact_memories",
            "description": "List contact memory summaries for one or more mailboxes.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address, or all.", "required": False},
                {"name": "mailboxes", "type": "array", "description": "Mailbox email addresses.", "required": False},
                {"name": "storage_provider", "type": "string", "description": "Storage backend: local or aps.", "required": False},
            ],
        },
        {
            "name": "get_contact_memory",
            "description": "Get one contact memory file.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "contact_email", "type": "string", "description": "Contact email address.", "required": True},
                {"name": "storage_provider", "type": "string", "description": "Storage backend: local or aps.", "required": False},
            ],
        },
        {
            "name": "delete_contact_memory",
            "description": "Delete one contact memory file.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "contact_email", "type": "string", "description": "Contact email address.", "required": True},
                {"name": "storage_provider", "type": "string", "description": "Storage backend: local or aps.", "required": False},
            ],
        },
        {
            "name": "clear_contact_memories",
            "description": "Delete all contact memories for one or more mailboxes.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address, or all.", "required": False},
                {"name": "mailboxes", "type": "array", "description": "Mailbox email addresses.", "required": False},
                {"name": "storage_provider", "type": "string", "description": "Storage backend: local or aps.", "required": False},
            ],
        },
    ],
    "runtime": {"type": "python", "min_version": "3.10"},
}


def load_manifest() -> dict[str, Any]:
    pyinstaller_root = getattr(sys, "_MEIPASS", "")
    runtime_root = Path(pyinstaller_root) if pyinstaller_root else Path(__file__).resolve().parents[2]
    manifest_path = runtime_root / "manifest.json"
    try:
        with open(manifest_path, "r", encoding="utf-8-sig") as manifest_file:
            manifest = json.load(manifest_file)
        if isinstance(manifest, dict):
            return manifest
    except Exception:
        pass
    return DEFAULT_MANIFEST


MANIFEST = load_manifest()
TOOL_ID = str(MANIFEST.get("tool_id") or MANIFEST.get("name") or DEFAULT_TOOL_ID)
VERSION = str(MANIFEST.get("version") or DEFAULT_VERSION)
MANIFEST["name"] = TOOL_ID
MANIFEST["version"] = VERSION


def log(message: str) -> None:
    sys.stderr.write(f"[{TOOL_ID}] {message}\n")
    sys.stderr.flush()


def beijing_now() -> str:
    return datetime.now(BEIJING_TZ).isoformat()


def make_response(request_id: Any, result: Any | None = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": request_id}
    if error is None:
        response["result"] = result
    else:
        response["error"] = error
    return response


def make_error(code: int, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return error


def _sampling_result_shape(result: Any) -> str:
    # 中文注释：只记录响应形态，不记录模型正文，便于定位 Anna sampling 空响应。
    if not isinstance(result, dict):
        return type(result).__name__
    content = result.get("content")
    if isinstance(content, dict):
        content_shape = f"dict:{','.join(sorted(str(k) for k in content.keys()))}"
        text = content.get("text")
        if isinstance(text, str):
            content_shape = f"{content_shape}; text_len={len(text)}"
    elif isinstance(content, list):
        content_shape = f"list:{len(content)}"
        text_len = 0
        for item in content:
            if isinstance(item, str):
                text_len += len(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                text_len += len(item["text"])
        content_shape = f"{content_shape}; text_len={text_len}"
    else:
        content_shape = type(content).__name__
        if isinstance(content, str):
            content_shape = f"{content_shape}; text_len={len(content)}"
    return f"keys={','.join(sorted(str(k) for k in result.keys()))}; content={content_shape}"


def write_frame(message: dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))

    # Guard against lone surrogates (U+D800–U+DFFF) from LLM output or
    # cached email data that would break UTF-8 encoding on stdout.
    payload_bytes: bytes
    try:
        payload_bytes = payload.encode("utf-8")
    except UnicodeEncodeError:
        # Slow path: replace surrogates character by character
        payload = "".join(c if ord(c) < 0xD800 or ord(c) >= 0xE000 else "?" for c in payload)
        payload_bytes = payload.encode("utf-8")

    with STDOUT_LOCK:
        if len(payload_bytes) > MAX_STDIO_MESSAGE_BYTES:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", prefix="executa-resp-", delete=False, encoding="utf-8") as handle:
                handle.write(payload)
                path = handle.name
            sys.stdout.write(json.dumps({"jsonrpc": JSONRPC_VERSION, "id": message.get("id"), "__file_transport": path}, ensure_ascii=False) + "\n")
        else:
            sys.stdout.write(payload + "\n")
        sys.stdout.flush()


sampling = SamplingClient(write_frame=write_frame)


def _normalize_storage_provider(value: Any = "") -> str:
    raw = str(value or "").strip().lower()
    if raw in {"aps", "anna", "anna-aps"}:
        return "aps"
    if raw in {"local", "local-json", "json", "file"}:
        return "local"
    return "local"


_aps_storage = StorageClient(write_frame=write_frame)
_aps_files = FilesClient(write_frame=write_frame)
_aps_route_storage_response = make_response_router(_aps_storage, _aps_files)

from mail_agent.storage.local import make_local_clients
_local_data_dir = Path(os.environ.get("ZHAOPY_MAIL_AGENT_STORAGE_DIR") or data_root()).expanduser().resolve()
_local_storage, _local_files = make_local_clients(_local_data_dir)

from mail_agent.storage.client import init as init_storage_singleton
_active_storage_provider = ""
_route_storage_response = lambda msg: False


def _set_storage_backend(provider: Any = "") -> str:
    global _active_storage_provider, _route_storage_response
    selected = _normalize_storage_provider(provider)
    if selected == "aps":
        init_storage_singleton(_aps_storage, _aps_files, scope="user", backend="aps")
        _route_storage_response = _aps_route_storage_response
    else:
        init_storage_singleton(_local_storage, _local_files, scope="user", backend="local")
        _route_storage_response = lambda msg: False
    if selected != _active_storage_provider:
        # 中文注释：调试开关允许前端按工具调用切换 APS 或本地 JSON 存储。
        log("storage backend: aps" if selected == "aps" else f"storage backend: local-json dir={_local_data_dir}")
    _active_storage_provider = selected
    return selected


def _apply_storage_provider(arguments: dict[str, Any]) -> str:
    if "storage_provider" in arguments:
        return _set_storage_backend(arguments.get("storage_provider"))
    return _active_storage_provider or _set_storage_backend(os.environ.get("ANNA_STORAGE_BACKEND", "local"))


def _should_use_aps_storage() -> bool:
    return _active_storage_provider == "aps"


_set_storage_backend(os.environ.get("ANNA_STORAGE_BACKEND", "local"))

loop = asyncio.new_event_loop()
loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
loop_thread.start()
from mail_agent.storage.sync_bridge import bind as bind_storage_sync_bridge
bind_storage_sync_bridge(loop, loop_thread)
MAIL_AGENT_RUNS: dict[str, dict[str, Any]] = {}
RUN_STATE_LOCK = threading.RLock()
RUN_CHECKPOINT_DIR = data_root() / "anna-inbox" / "runs" / "background"


def _run_checkpoint_path(run_id: str) -> Path:
    safe_id = "".join(ch for ch in str(run_id or "") if ch.isalnum() or ch in {"_", "-"})
    return RUN_CHECKPOINT_DIR / f"{safe_id}.json"


def _is_warning_stage(stage: str) -> bool:
    """判断 stage 是否为需要持久化的警告/错误阶段。"""
    return bool(stage) and (
        stage.endswith("_empty")
        or "fallback" in stage
        or "error" in stage
        or "failed" in stage
    )


def _save_run_checkpoint(run_id: str) -> None:
    # 中文注释：后台任务是进程内存态；落盘用于本地 runtime 重启后的轮询诊断。
    with RUN_STATE_LOCK:
        state = MAIL_AGENT_RUNS.get(run_id)
        if not state:
            return
        RUN_CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        path = _run_checkpoint_path(run_id)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, default=str), encoding="utf-8")
        tmp_path.replace(path)


def _load_run_checkpoint(run_id: str) -> dict[str, Any] | None:
    path = _run_checkpoint_path(run_id)
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return loaded if isinstance(loaded, dict) else None


def _get_run_state(run_id: str) -> dict[str, Any] | None:
    with RUN_STATE_LOCK:
        state = MAIL_AGENT_RUNS.get(run_id)
        if state:
            return state
        checkpoint = _load_run_checkpoint(run_id)
        if not checkpoint:
            return None
        if checkpoint.get("status") in {"queued", "running"}:
            checkpoint = dict(checkpoint)
            last_stage = checkpoint.get("stage") or "unknown"
            checkpoint["status"] = "failed"
            checkpoint["stage"] = "failed"
            checkpoint["error"] = (
                "run interrupted because the Executa process restarted; "
                f"last stage was {last_stage}"
            )
            checkpoint["updated_at"] = beijing_now()
            MAIL_AGENT_RUNS[run_id] = checkpoint
            _save_run_checkpoint(run_id)
        return checkpoint


def _compact_run_payload(value: Any, *, text_limit: int = 1200) -> Any:
    # 中文注释：轮询状态只需要展示摘要，完整邮件正文保留在持久化卡片和详情接口里。
    if isinstance(value, list):
        return [_compact_run_payload(item, text_limit=text_limit) for item in value]
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"body", "body_text", "body_html", "raw", "raw_message"} and isinstance(item, str):
                compact[key] = item[:text_limit]
                if len(item) > text_limit:
                    compact[f"{key}_truncated"] = True
                continue
            compact[key] = _compact_run_payload(item, text_limit=text_limit)
        return compact
    return value


def _compact_run_result(result: Any) -> Any:
    if result is None:
        return None
    if isinstance(result, dict) and result.get("tool") == "run_mail_agent":
        action_plan = result.get("action_plan") if isinstance(result.get("action_plan"), dict) else {}
        # 中文注释：Brief 轮询完成后前端会再读持久化卡片，这里避免重复返回完整 cards/proposed_actions。
        return {
            "success": result.get("success", True),
            "tool": result.get("tool"),
            "action_plan": {
                "run_id": action_plan.get("run_id", ""),
                "strategy_mode": action_plan.get("strategy_mode", ""),
                "title": action_plan.get("title", ""),
                "summary": action_plan.get("summary", ""),
                "main_items": _compact_run_payload(action_plan.get("main_items", []), text_limit=800),
                "lower_priority_items": _compact_run_payload(action_plan.get("lower_priority_items", []), text_limit=800),
            },
            "cards_count": len(result.get("cards") or []),
            "meta": _compact_run_payload(result.get("meta") or {}, text_limit=800),
        }
    return _compact_run_payload(result, text_limit=1200)


def handle_initialize(params: dict[str, Any]) -> dict[str, Any]:
    protocol_version = str((params or {}).get("protocolVersion") or "1.1")
    v2 = protocol_version == PROTOCOL_VERSION_V2
    if not v2:
        manifest_has_llm = "llm.sample" in MANIFEST.get("host_capabilities", [])
        lines = [
            "Sampling unavailable — pre-condition check:",
            f"  [{'OK' if manifest_has_llm else 'MISSING'}  ] Executa manifest host_capabilities includes 'llm.sample'",
            f"  [{'OK' if v2 else 'FAILED'}] Host protocol is 2.0 (got {protocol_version!r})",
            f"  [UNKNOWN] App manifest includes 'llm.sample' in host_capabilities — check Anna platform",
            f"  [UNKNOWN] User granted sampling permission — check Anna platform Settings → App Permissions",
        ]
        sampling.disable("\n".join(lines))
    return {
        "protocolVersion": PROTOCOL_VERSION_V2 if v2 else "1.1",
        "serverInfo": {"name": TOOL_ID, "version": VERSION},
        "client_capabilities": {"sampling": {}, "storage": {}} if v2 else {},
        "capabilities": {"sampling": {}, "storage": {}} if v2 else {},
    }


def read_credentials(context: dict[str, Any]) -> dict[str, str]:
    raw_credentials = context.get("credentials") if isinstance(context, dict) else {}
    credentials = raw_credentials if isinstance(raw_credentials, dict) else {}
    gmail_token = credentials.get("GMAIL_ACCESS_TOKEN") or os.environ.get("GMAIL_ACCESS_TOKEN")
    google_token = credentials.get("GOOGLE_ACCESS_TOKEN") or os.environ.get("GOOGLE_ACCESS_TOKEN")
    return {
        "GMAIL_ACCESS_TOKEN": gmail_token or "",
        "GOOGLE_ACCESS_TOKEN": google_token or "",
    }


def apply_runtime_credentials(context: dict[str, Any]) -> None:
    raw_credentials = context.get("credentials") if isinstance(context, dict) else {}
    credentials = raw_credentials if isinstance(raw_credentials, dict) else {}
    for name in ("DASHSCOPE_API_KEY", "DASHSCOPE_MODEL", "GMAIL_ACCESS_TOKEN", "GOOGLE_ACCESS_TOKEN"):
        value = credentials.get(name) or os.environ.get(name)
        if value:
            os.environ[name] = str(value)

    # 多 token 凭证：解析 JSON → 合并进 APS 工作副本 → 加载到内存
    multi_raw = credentials.get("GMAIL_MULTI_TOKENS", "")
    if multi_raw and multi_raw.strip():
        try:
            tokens = json.loads(multi_raw)
            if isinstance(tokens, list) and len(tokens) > 0:
                _run_storage_query(_merge_multi_tokens_seed(tokens), timeout=10.0)
                all_tokens = _run_storage_query(_get_all_multi_tokens(), timeout=10.0)
                from mail_agent.mail_providers.gmail.adapter import set_multi_tokens
                set_multi_tokens(all_tokens)
        except (json.JSONDecodeError, Exception):
            pass


def check_google_oauth(context: dict[str, Any]) -> dict[str, Any]:
    credentials = read_credentials(context)
    has_gmail = bool(credentials["GMAIL_ACCESS_TOKEN"])
    has_google = bool(credentials["GOOGLE_ACCESS_TOKEN"])
    from mail_agent.mail_providers.gmail.adapter import get_multi_token_emails
    multi_emails = get_multi_token_emails()
    return {
        "authorized": has_gmail or has_google or len(multi_emails) > 0,
        "credential_names": {
            "gmail": "present" if has_gmail else "missing",
            "google": "present" if has_google else "missing",
        },
        "multi_token_count": len(multi_emails),
        "multi_token_emails": multi_emails,
        "next_step": "Google OAuth credential is available." if has_gmail or has_google or multi_emails else "Authorize Google/Gmail in Anna platform authorizations, then retry.",
        "checked_at": beijing_now(),
    }


def repo_root() -> Path:
    # main.py lives at inbox-tool/src/anna_inbox_executa/main.py.
    return Path(__file__).resolve().parents[3]


def tool_root() -> Path:
    return Path(__file__).resolve().parents[1]


def sanitize_mailbox_id(mailbox: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in mailbox.strip())
    return safe.strip("._") or "default"


def normalize_mailbox(mailbox: str) -> str:
    raw = str(mailbox or "").strip().lower()
    if "@" in raw and "." in raw.split("@")[-1]:
        return raw
    raise ValueError(f"Unsupported mailbox: {mailbox}")


def token_dir() -> Path:
    override = os.environ.get("ANNA_INBOX_TOKEN_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return repo_root() / "scripts" / "google_token" / ".secrets" / "gmail_tokens"


def load_local_token_record(mailbox: str) -> dict[str, Any]:
    candidates = [
        token_dir() / f"{sanitize_mailbox_id(mailbox)}.json",
        token_dir() / "default.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            record["_token_file"] = str(path)
            return record
    raise ValueError(f"Local Gmail token file not found for {mailbox}")


def get_access_token(mailbox: str) -> str:
    record = load_local_token_record(mailbox)
    if should_refresh_token(record):
        refresh_access_token(record)
    token = record.get("access_token")
    if not token:
        raise ValueError(f"Local Gmail access token is missing for {mailbox}")
    return str(token)


def should_refresh_token(record: dict[str, Any]) -> bool:
    if not record.get("refresh_token"):
        return False
    try:
        return float(record.get("expires_at") or 0) <= time.time() + 60
    except (TypeError, ValueError):
        return False


def refresh_access_token(record: dict[str, Any]) -> None:
    client_id = record.get("client_id")
    client_secret = record.get("client_secret")
    refresh_token = record.get("refresh_token")
    if not client_id or not client_secret or not refresh_token:
        raise ValueError("Gmail refresh token is missing client metadata")
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    request = urllib.request.Request(TOKEN_URI, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    record["access_token"] = payload["access_token"]
    if "expires_in" in payload:
        record["expires_at"] = int(time.time()) + int(payload["expires_in"])
    record["updated_at"] = beijing_now()
    token_file = record.get("_token_file")
    if token_file:
        clean_record = {key: value for key, value in record.items() if not key.startswith("_")}
        Path(str(token_file)).write_text(json.dumps(clean_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def gmail_request(mailbox: str, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
    token = get_access_token(mailbox)
    url = GMAIL_API_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = read_http_error(exc)
        raise ValueError(f"Gmail API request failed: {exc.code} {details}") from exc
    return json.loads(raw) if raw else {}


def read_http_error(exc: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        raw = exc.read().decode("utf-8")
        return json.loads(raw) if raw else {"status_code": exc.code}
    except Exception:
        return {"status_code": exc.code}


def header_map(message: dict[str, Any]) -> dict[str, str]:
    headers = ((message.get("payload") or {}).get("headers") or [])
    result: dict[str, str] = {}
    for header in headers:
        if isinstance(header, dict) and header.get("name"):
            result[str(header["name"]).lower()] = str(header.get("value") or "")
    return result


def decode_gmail_body(message: dict[str, Any]) -> str:
    parts: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType") or "")
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        data = body.get("data")
        if data and mime_type in {"text/plain", "text/html"}:
            try:
                decoded = base64.urlsafe_b64decode(str(data) + "=" * (-len(str(data)) % 4)).decode("utf-8", errors="replace")
                parts.append(decoded)
            except Exception:
                return
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    walk(payload)
    text = "\n\n".join(part.strip() for part in parts if part.strip())
    return text[:30000]


def extract_attachments(part: dict[str, Any]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        filename = str(node.get("filename") or "")
        body = node.get("body") if isinstance(node.get("body"), dict) else {}
        if filename:
            attachments.append({
                "filename": filename,
                "mimeType": node.get("mimeType"),
                "size": body.get("size"),
                "attachmentId": body.get("attachmentId"),
            })
        for child in node.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(part)
    return attachments


def normalize_message(mailbox: str, message: dict[str, Any]) -> dict[str, Any]:
    headers = header_map(message)
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "mailbox": mailbox,
        "history_id": message.get("historyId"),
        "internal_date": message.get("internalDate"),
        "date": headers.get("date", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "bcc": headers.get("bcc", ""),
        "subject": headers.get("subject", ""),
        "message_id": headers.get("message-id", ""),
        "in_reply_to": headers.get("in-reply-to", ""),
        "references": headers.get("references", ""),
        "label_ids": message.get("labelIds") or [],
        "snippet": message.get("snippet") or "",
        "size_estimate": message.get("sizeEstimate"),
        "mime_type": payload.get("mimeType"),
        "attachments": extract_attachments(payload),
        "body_text": decode_gmail_body(message),
        "raw_headers": headers,
        "fetched_at": beijing_now(),
    }


def read_primary_emails(mailbox_arg: str, limit_arg: Any) -> dict[str, Any]:
    from mail_agent.mail_providers.gmail.adapter import cache_debug_info, live_search_and_cache, list_messages, normalize_mailbox as adapter_normalize_mailbox

    mailbox = adapter_normalize_mailbox(mailbox_arg)
    limit = max(1, min(int(limit_arg or 5), 20))
    matched_ids = live_search_and_cache(mailbox, "category:primary", limit)
    merged = sorted(list_messages(mailbox), key=lambda item: int(item.get("internal_date") or 0), reverse=True)
    fetched_summaries = [item for item in merged if str(item.get("id") or "") in set(matched_ids)]
    return {
        "mailbox": mailbox,
        "requested": limit,
        "fetched_count": len(fetched_summaries),
        "matched_ids": len(matched_ids),
        "cached_count": len(merged),
        "cache": cache_debug_info(mailbox),
        "messages": fetched_summaries,
        "updated_at": beijing_now(),
    }


def list_cached_emails(mailbox_arg: str) -> dict[str, Any]:
    from mail_agent.mail_providers.gmail.adapter import cache_debug_info, list_messages, read_cache as adapter_read_cache, normalize_mailbox as adapter_normalize_mailbox

    mailbox = adapter_normalize_mailbox(mailbox_arg)
    cached = adapter_read_cache(mailbox)
    messages = sorted(list_messages(mailbox), key=lambda item: int(item.get("internal_date") or 0), reverse=True)
    return {
        "mailbox": mailbox,
        "cache": cache_debug_info(mailbox),
        "updated_at": cached.get("updated_at"),
        "count": len(messages),
        "messages": messages,
    }


def get_cached_email(mailbox_arg: str, message_id: str) -> dict[str, Any]:
    from mail_agent.mail_providers.gmail.adapter import cache_debug_info, read_message as adapter_read_message, normalize_mailbox as adapter_normalize_mailbox

    mailbox = adapter_normalize_mailbox(mailbox_arg)
    msg_id = str(message_id or "")
    message = adapter_read_message(mailbox, msg_id)
    return {
        "mailbox": mailbox,
        "cache": cache_debug_info(mailbox),
        "message": message,
    }



def _check_gmail_auth(mailbox: str) -> dict[str, Any]:
    """Check Gmail authorization status.

    Platform: GMAIL_ACCESS_TOKEN or GOOGLE_ACCESS_TOKEN env var is set.
    Multi-token: checks _multi_token_map.
    Local dev: token file exists on disk (content/expiry not validated).
    """
    import os as _os
    from mail_agent.mail_providers.gmail.adapter import _token_dir, sanitize_mailbox_id, get_authorized_email, get_multi_token_map
    from pathlib import Path as _Path

    requested = str(mailbox or "").strip().lower()

    # 系统级判断：mailbox 为空时，只看 token 有没有，不关心具体邮箱
    if not requested:
        platform_token = _os.environ.get("GMAIL_ACCESS_TOKEN") or _os.environ.get("GOOGLE_ACCESS_TOKEN")
        if platform_token and platform_token.strip():
            try:
                authorized_email = get_authorized_email().strip().lower()
            except Exception:
                # Gmail API unreachable — token exists, assume authorized
                return {"authorized": True, "source": "platform", "warning": "Gmail API unreachable; token assumed valid", "mode": "any"}
            if not authorized_email:
                return {"authorized": True, "source": "platform", "warning": "Token present but email lookup failed", "mode": "any"}
            return {"authorized": True, "source": "platform", "authorized_email": authorized_email, "mode": "any"}
        multi = get_multi_token_map()
        if multi:
            return {"authorized": True, "source": "platform_multi", "authorized_email": list(multi.keys())[0] if multi else "", "mode": "any"}
        token_dir = _token_dir()
        if token_dir.exists():
            for p in token_dir.glob("*.json"):
                if p.name != "default.json":
                    return {"authorized": True, "source": "local_file", "mode": "any"}
        return {"authorized": False, "source": "none", "mode": "any"}

    # Platform path — check for injected OAuth credential
    platform_token = _os.environ.get("GMAIL_ACCESS_TOKEN") or _os.environ.get("GOOGLE_ACCESS_TOKEN")
    if platform_token and platform_token.strip():
        authorized_email = get_authorized_email().strip().lower()
        authorized = bool(authorized_email and requested == authorized_email)
        return {
            "authorized": authorized,
            "source": "platform",
            "authorized_email": authorized_email,
        }

    # Multi-token path — check in-memory map
    if requested in get_multi_token_map():
        return {"authorized": True, "source": "platform_multi", "authorized_email": requested}

    # Local dev path — check for token file existence only
    token_dir = _token_dir()
    candidates = [
        token_dir / f"{sanitize_mailbox_id(mailbox)}.json",
        token_dir / "default.json",
    ]
    for path in candidates:
        if _Path(path).exists():
            return {"authorized": True, "source": "local_file", "token_file": str(path)}

    return {"authorized": False, "source": "none"}


async def _handle_mark_cleanup_read(arguments: dict[str, Any]) -> dict[str, Any]:
    """Mark cleanup-bundle messages as read in Gmail and update the card in storage."""
    mailbox = str(arguments.get("mailbox", "")).strip()
    card_id = str(arguments.get("card_id", "")).strip()
    raw_ids = arguments.get("message_ids") or []
    message_ids = [str(mid).strip() for mid in raw_ids if str(mid).strip()] if isinstance(raw_ids, list) else []
    if not mailbox or not card_id or not message_ids:
        return {"error": "mailbox, card_id, and message_ids (non-empty array) are required"}

    # 1. Gmail batchModify: remove UNREAD label
    gmail_result = None
    gmail_error = ""
    try:
        import json as _json2
        import urllib.request as _ur
        from mail_agent.mail_providers.gmail.adapter import get_access_token
        token = get_access_token(mailbox)
        body = _json2.dumps({
            "ids": message_ids,
            "removeLabelIds": ["UNREAD"],
        }).encode("utf-8")
        req = _ur.Request(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with _ur.urlopen(req, timeout=30) as resp:
            raw_body = resp.read().decode("utf-8")
            gmail_result = _json2.loads(raw_body) if raw_body.strip() else {"ok": True}
    except Exception as exc:
        gmail_error = str(exc)

    # 2. Keep card visible (pending) — read state is tracked frontend-side
    # Write history
    card_title = card_id
    try:
        from mail_agent.storage.ops import get_active_cards as _cards_for_title
        cards_obj = await _cards_for_title(mailbox)
        card_obj = next((c for c in cards_obj.cards if c.card_id == card_id), None)
        card_title = card_obj.title if card_obj else card_id
    except Exception:
        pass
    from mail_agent.storage.ops import append_card_action
    await append_card_action(mailbox, card_id, card_title, "cleanup_read", f"{len(message_ids)} emails")

    return {
        "ok": gmail_error == "",
        "marked_count": len(message_ids),
        "gmail_result": gmail_result,
        "gmail_error": gmail_error,
    }



async def run_aps_storage_smoke(arguments: dict[str, Any]) -> dict[str, Any]:
    """只验证 APS KV 的最小读写链路，不触碰业务邮箱数据。"""
    from mail_agent.storage.client import get_storage, scope as default_scope

    storage = get_storage()
    suffix = str(arguments.get("key_suffix") or uuid.uuid4().hex[:8]).strip()
    key = app_key(f"debug/aps_smoke/{suffix}")
    value = {
        "value": str(arguments.get("value") or "hello aps"),
        "ts": beijing_now(),
    }
    storage_scope = default_scope()

    set_result = await storage.set(key, value, scope=storage_scope)
    get_result = await storage.get(key, scope=storage_scope)
    list_result = await storage.list(prefix=app_key("debug/aps_smoke/"), limit=20, scope=storage_scope)
    list_all_result = await storage.list(limit=20, scope=storage_scope)
    delete_result = await storage.delete(key, scope=storage_scope)
    after_delete = await storage.get(key, scope=storage_scope)

    return {
        "success": bool(get_result.get("exists")) and get_result.get("value") == value,
        "backend": "aps" if _should_use_aps_storage() else "local-json",
        "scope": storage_scope,
        "key": key,
        "set": set_result,
        "get": get_result,
        "list": list_result,
        "list_all": list_all_result,
        "list_count": len(list_result.get("items") or []),
        "delete": delete_result,
        "exists_after_delete": bool(after_delete.get("exists")),
    }


def serialize_value(obj: Any) -> Any:
    from dataclasses import asdict

    if hasattr(obj, "__dataclass_fields__"):
        return {key: serialize_value(value) for key, value in asdict(obj).items()}
    if isinstance(obj, list):
        return [serialize_value(item) for item in obj]
    if isinstance(obj, dict):
        return {key: serialize_value(value) for key, value in obj.items()}
    return obj


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

    sampling_create_message = None
    if provider == "anna-llm":
        async def sampling_create_message_with_invoke(**kwargs: Any) -> dict[str, Any]:
            metadata = {str(key): str(value) for key, value in (kwargs.get("metadata") or {}).items()}
            metadata["executa_invoke_id"] = invoke_id
            kwargs["metadata"] = metadata
            log(f"anna sampling start: max_tokens={kwargs.get('max_tokens')} metadata={metadata}")
            started = time.time()
            result = await sampling.create_message(**kwargs)
            log(f"anna sampling done: elapsed_ms={int((time.time() - started) * 1000)} model={result.get('model')} shape={_sampling_result_shape(result)}")
            return result

        sampling_create_message = sampling_create_message_with_invoke

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
    try:
        from mail_agent.storage.client import is_ready
        if is_ready():
            from mail_agent.storage.ops import get_active_cards
            from mail_agent.cards.service import cards_to_frontend
            stored = await get_active_cards(input_.mailbox_id)
            active_cards = cards_to_frontend(stored)
    except Exception:
        pass

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
        primary_count = arguments.get("primary_count", 20)
        max_messages = arguments.get("max_messages", primary_count)
        result = await run_mail_agent_pipeline(
            user_request=arguments.get("user_request", ""),
            mailbox=arguments.get("mailbox", ""),
            mode=arguments.get("mode", "auto"),
            max_messages=max_messages,
            primary_count=primary_count,
            ai_provider=arguments.get("ai_provider", "anna-llm"),
            invoke_id=invoke_id,
            progress_callback=_update_progress,
        )
        MAIL_AGENT_RUNS[run_id].update({
            "status": "done",
            "stage": "done",
            "updated_at": beijing_now(),
            "result": result,
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


def _build_sampling_for_run(arguments: dict[str, Any], invoke_id: str) -> Any:
    """Build sampling_create_message for a run based on ai_provider arg."""
    provider = str(arguments.get("ai_provider", "anna-llm")).strip()
    if provider == "anna-llm":
        async def _sampling(**kwargs: Any) -> dict[str, Any]:
            metadata = {str(key): str(value) for key, value in (kwargs.get("metadata") or {}).items()}
            metadata["executa_invoke_id"] = invoke_id
            kwargs["metadata"] = metadata
            tool_name = metadata.get("tool", "unknown")
            started = time.time()
            log(f"anna ask sampling start: tool={tool_name} max_tokens={kwargs.get('max_tokens')} metadata={metadata}")
            result = await sampling.create_message(**kwargs)
            log(f"anna ask sampling done: tool={tool_name} elapsed_ms={int((time.time() - started) * 1000)} model={result.get('model')} shape={_sampling_result_shape(result)}")
            return result
        return _sampling
    return None


def start_mail_agent_run(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
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
    }
    _save_run_checkpoint(run_id)
    future = asyncio.run_coroutine_threadsafe(run_mail_agent_background(run_id, arguments, invoke_id), loop)
    if arguments.get("wait") is True:
        wait_timeout = int(arguments.get("wait_timeout_seconds", 300))
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
    return {
        "success": True,
        "run_id": run_id,
        "status": "queued",
        "stage": MAIL_AGENT_RUNS[run_id]["stage"],
        "progress": MAIL_AGENT_RUNS[run_id]["progress"],
        "started_at": MAIL_AGENT_RUNS[run_id]["started_at"],
    }


def start_custom_scan(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """Start a custom scan: generate plan (LLM) then execute in background."""
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
    if arguments.get("wait") is True:
        wait_timeout = int(arguments.get("wait_timeout_seconds", 180))
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
    return {
        "success": True,
        "run_id": run_id,
        "status": "queued",
        "stage": "planning",
        "progress": {},
        "started_at": MAIL_AGENT_RUNS[run_id]["started_at"],
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
    """Re-run a previously saved custom scan plan (skip LLM planning)."""
    plan_id = str(arguments.get("plan_id", "")).strip()
    if not plan_id:
        return {"success": False, "error": "plan_id is required"}

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
    asyncio.run_coroutine_threadsafe(
        _re_run_custom_scan_async(run_id, plan_id, arguments, invoke_id),
        loop,
    )
    return {
        "success": True,
        "run_id": run_id,
        "status": "queued",
        "stage": "planning_done",
        "progress": {},
        "started_at": MAIL_AGENT_RUNS[run_id]["started_at"],
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


def _run_storage_query(coro: Any, timeout: float = 60.0) -> Any:
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


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


def _memory_mailboxes(arguments: dict[str, Any]) -> list[str]:
    raw_mailboxes = arguments.get("mailboxes")
    if isinstance(raw_mailboxes, list):
        mailboxes = [str(item).strip().lower() for item in raw_mailboxes if str(item).strip()]
        if mailboxes:
            return sorted(dict.fromkeys(mailboxes))
    mailbox = str(arguments.get("mailbox", "")).strip().lower()
    if mailbox and mailbox != "all":
        return [mailbox]
    from mail_agent.storage.ops import get_mailbox_registry

    registry = _run_storage_query(get_mailbox_registry())
    selected = [entry.email for entry in getattr(registry, "mailboxes", []) if getattr(entry, "selected", False)]
    if selected:
        return sorted(dict.fromkeys(str(item).strip().lower() for item in selected if str(item).strip()))
    discovered = _discover_mailboxes()
    return sorted(dict.fromkeys(str(item.get("email", "")).strip().lower() for item in discovered if str(item.get("email", "")).strip()))


def _sync_list_contact_memories(arguments: dict[str, Any]) -> dict[str, Any]:
    from mail_agent.contact_memory.manager import list_memory_summaries

    mailboxes = _memory_mailboxes(arguments)
    return _run_storage_query(list_memory_summaries(mailboxes))


def _sync_get_contact_memory(arguments: dict[str, Any]) -> dict[str, Any]:
    from mail_agent.contact_memory.manager import get_memory_detail

    return _run_storage_query(get_memory_detail(
        str(arguments.get("mailbox", "")).strip().lower(),
        str(arguments.get("contact_email", "")).strip().lower(),
    ))


def _sync_delete_contact_memory(arguments: dict[str, Any]) -> dict[str, Any]:
    from mail_agent.contact_memory.manager import delete_memory

    return _run_storage_query(delete_memory(
        str(arguments.get("mailbox", "")).strip().lower(),
        str(arguments.get("contact_email", "")).strip().lower(),
    ))


def _sync_clear_contact_memories(arguments: dict[str, Any]) -> dict[str, Any]:
    from mail_agent.contact_memory.manager import clear_memory

    return _run_storage_query(clear_memory(_memory_mailboxes(arguments)))


def _registry_to_frontend(registry: Any) -> list[dict[str, Any]]:
    return [
        {
            "email": entry.email,
            "provider": entry.provider,
            "auth_source": entry.auth_source,
            "authorized": entry.authorized,
            "selected": entry.selected,
            "last_auth_checked_at": entry.last_auth_checked_at,
            "last_scan_at": entry.last_scan_at,
            "last_scan_status": entry.last_scan_status,
            "last_error": entry.last_error,
            "card_count": entry.card_count,
        }
        for entry in getattr(registry, "mailboxes", [])
    ]


async def _merge_multi_tokens_seed(seed_tokens: list[dict[str, Any]]) -> None:
    """将 credential seed 合并到 APS 工作副本，避免用旧 seed 覆盖已刷新的 token。"""
    from mail_agent.storage.ops import get_multi_tokens, set_multi_tokens

    existing = await get_multi_tokens()
    by_email: dict[str, dict[str, Any]] = {
        str(e.get("email", "")).strip().lower(): e for e in existing if e.get("email")
    }

    for seed in seed_tokens:
        email = str(seed.get("email") or "").strip().lower()
        if not email or "@" not in email:
            continue
        if email in by_email:
            existing_token = by_email[email].get("access_token", "")
            new_token = seed.get("access_token", "")
            if new_token and new_token != existing_token:
                by_email[email] = dict(seed)
        else:
            by_email[email] = dict(seed)

    await set_multi_tokens(list(by_email.values()))


async def _get_all_multi_tokens() -> list[dict[str, Any]]:
    from mail_agent.storage.ops import get_multi_tokens
    return await get_multi_tokens()


def _discover_mailboxes() -> list[dict[str, Any]]:
    from mail_agent.mail_providers.gmail.adapter import get_authorized_email, list_available_mailboxes_from_tokens, get_multi_token_emails

    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1. 平台单 token — 最高优先级，auth_source="platform"
    email = get_authorized_email().strip().lower()
    if email and email not in seen:
        seen.add(email)
        results.append({
            "email": email, "provider": "gmail",
            "auth_source": "platform", "authorized": True,
            "last_auth_checked_at": beijing_now(),
        })

    # 2. 多 token 邮箱 — auth_source="platform_multi"，去重跳过 platform 已覆盖的
    for multi_email in get_multi_token_emails():
        if multi_email not in seen:
            seen.add(multi_email)
            results.append({
                "email": multi_email, "provider": "gmail",
                "auth_source": "platform_multi", "authorized": True,
                "last_auth_checked_at": beijing_now(),
            })

    # 3. 本地 dev token 文件兜底
    for local in list_available_mailboxes_from_tokens():
        local_email = str(local.get("email", "")).strip().lower()
        if local_email and local_email not in seen:
            seen.add(local_email)
            results.append(local)

    return results


def _sync_list_mailboxes() -> dict[str, Any]:
    from mail_agent.storage.ops import merge_discovered_mailboxes

    discovered = _discover_mailboxes()
    registry = _run_storage_query(merge_discovered_mailboxes(discovered))
    mailboxes = _registry_to_frontend(registry)
    return {
        "mailboxes": mailboxes,
        "selected": [item["email"] for item in mailboxes if item.get("selected")],
        "discovered": discovered,
    }


def _sync_set_mailbox_selected(arguments: dict[str, Any]) -> dict[str, Any]:
    mailbox = str(arguments.get("mailbox", "")).strip().lower()
    if not mailbox:
        return {"error": "mailbox is required"}
    selected = arguments.get("selected", True)
    if not isinstance(selected, bool):
        selected = str(selected).lower() in ("1", "true", "yes", "on")
    from mail_agent.storage.ops import set_mailbox_selected

    registry = _run_storage_query(set_mailbox_selected(mailbox, selected))
    mailboxes = _registry_to_frontend(registry)
    return {"ok": True, "mailboxes": mailboxes, "selected": [item["email"] for item in mailboxes if item.get("selected")]}


def _sync_remove_mailbox(arguments: dict[str, Any]) -> dict[str, Any]:
    mailbox = str(arguments.get("mailbox", "")).strip().lower()
    if not mailbox:
        return {"error": "mailbox is required"}
    from mail_agent.storage.ops import remove_mailbox_from_registry

    registry = _run_storage_query(remove_mailbox_from_registry(mailbox))
    mailboxes = _registry_to_frontend(registry)
    return {"ok": True, "mailboxes": mailboxes, "selected": [item["email"] for item in mailboxes if item.get("selected")]}


def _sync_get_active_cards(arguments: dict[str, Any]) -> dict[str, Any]:
    """同步入口通过统一 storage_ops 读取 active cards 和 scan state。"""
    mailbox = str(arguments.get("mailbox", "")).strip()
    if not mailbox:
        return {"error": "mailbox is required"}

    from mail_agent.cards.service import cards_to_frontend
    from mail_agent.storage.ops import aggregate_active_cards, get_active_cards, get_scan_state

    if mailbox.lower() == "all":
        active = _run_storage_query(aggregate_active_cards())
        # Aggregate scan state across all registered mailboxes
        try:
            from mail_agent.storage.ops import get_mailbox_registry
            reg = _run_storage_query(get_mailbox_registry())
            all_mboxes = [e.email for e in (reg.mailboxes if reg else [])]
            all_states = [_run_storage_query(get_scan_state(m)) for m in all_mboxes]
            total_scans = max((getattr(s, 'total_scans', 0) for s in all_states), default=0)
            total_processed = max((getattr(s, 'total_processed', 0) for s in all_states), default=0)
            last_scan_ts = max((getattr(s, 'last_scan_ts', '') for s in all_states), default='')
            scan_state = {
                "last_scan_ts": last_scan_ts,
                "last_message_internal_date": "",
                "total_scans": total_scans,
                "total_processed": total_processed,
            }
        except Exception:
            scan_state = {"total_scans": 0, "total_processed": 0, "last_scan_ts": "", "last_message_internal_date": ""}
    else:
        active = _run_storage_query(get_active_cards(mailbox))
        state = _run_storage_query(get_scan_state(mailbox))
        scan_state = {
            "last_scan_ts": getattr(state, "last_scan_ts", ""),
            "last_message_internal_date": getattr(state, "last_message_internal_date", ""),
            "total_scans": getattr(state, "total_scans", 0),
            "total_processed": getattr(state, "total_processed", 0),
        }

    action_count = sum(1 for c in active.cards if c.status not in ("resolved", "dismissed") and c.user_action in ("reply", "review"))

    return {
        "cards": cards_to_frontend(active),
        "count": len(active.cards),
        "action_count": action_count,
        "scan_state": scan_state,
    }

def _sync_get_run_history() -> dict[str, Any]:
    from mail_agent.storage.ops import get_run_history

    history = _run_storage_query(get_run_history(limit=20))
    return {"history": [serialize_value(entry) for entry in history]}


def get_mail_agent_run(run_id_arg: str) -> dict[str, Any]:
    run_id = str(run_id_arg or "")
    state = _get_run_state(run_id)
    if not state:
        return {"success": False, "error": "run not found", "run_id": run_id}
    return {
        "success": True,
        "run_id": run_id,
        "status": state.get("status"),
        "stage": state.get("stage") or "",
        "progress": state.get("progress") or {},
        "partial": state.get("partial") or {},
        "warnings": state.get("warnings") or [],
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "error": state.get("error") or "",
        "result": _compact_run_result(state.get("result")),
    }


async def _handle_summarize_background(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    MAIL_AGENT_RUNS[run_id]["status"] = "running"
    _save_run_checkpoint(run_id)

    mailbox = str(arguments.get("mailbox", "")).strip()
    card_id = str(arguments.get("card_id", "")).strip()
    try:
        from mail_agent.storage.ops import get_active_cards as storage_get_cards, set_active_cards
        from mail_agent.actions.service import summarize_thread
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            raise ValueError(f"Card {card_id} not found")
        _sampling = _build_sampling_for_run(arguments, invoke_id)
        result = await summarize_thread(card, mailbox, sampling_create_message=_sampling)
        summary = result.get("summary") if isinstance(result, dict) else {}
        if isinstance(summary, dict):
            card.thread_summary = json.dumps(summary, ensure_ascii=False)
            await set_active_cards(mailbox, cards)
        MAIL_AGENT_RUNS[run_id].update(status="done", result=result, updated_at=beijing_now())
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update(status="failed", error=str(exc), updated_at=beijing_now())
    _save_run_checkpoint(run_id)


async def _handle_generate_draft_background(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    MAIL_AGENT_RUNS[run_id]["status"] = "running"
    _save_run_checkpoint(run_id)

    mailbox = str(arguments.get("mailbox", "")).strip()
    card_id = str(arguments.get("card_id", "")).strip()
    reply_mode = str(arguments.get("reply_mode", "reply_to_sender")).strip() or "reply_to_sender"
    current_draft = str(arguments.get("current_draft", "")).strip()
    revision_input = str(arguments.get("revision_input", "")).strip()
    user_answers = arguments.get("user_answers") if isinstance(arguments.get("user_answers"), dict) else None
    try:
        from mail_agent.storage.ops import get_active_cards as storage_get_cards, set_active_cards
        from mail_agent.actions.service import generate_draft_reply
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            raise ValueError(f"Card {card_id} not found")
        _sampling = _build_sampling_for_run(arguments, invoke_id)
        result = await generate_draft_reply(
            card, mailbox, reply_mode, sampling_create_message=_sampling,
            current_draft=current_draft, revision_input=revision_input,
            user_answers=user_answers,
        )
        draft_body = (result.get("draft") or {}).get("body", "") if isinstance(result, dict) else ""
        if draft_body:
            card.draft_reply = draft_body
            await set_active_cards(mailbox, cards)
        MAIL_AGENT_RUNS[run_id].update(status="done", result=result, updated_at=beijing_now())
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update(status="failed", error=str(exc), updated_at=beijing_now())
    _save_run_checkpoint(run_id)


def _card_context(card: Any) -> dict[str, str]:
    """Extract display fields from a PersistentCard for history entries."""
    try:
        original = getattr(card, "original", None)
        return {
            "card_summary": (getattr(card, "summary", "") or "")[:200],
            "card_from": (getattr(original, "from_addr", "") or "")[:120] if original else "",
            "card_subject": (getattr(original, "thread", "") or "")[:200] if original else "",
            "card_body": (getattr(original, "body", "") or "")[:300] if original else "",
        }
    except Exception:
        return {}


async def _handle_v2_tool(tool: str, arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """Handle V2 interaction tools (async, runs on the event loop)."""
    from mail_agent.storage.ops import (
        get_active_cards as storage_get_cards,
        get_scan_plan,
        get_scan_state,
        set_scan_plan,
        update_card_status,
        add_snooze_sender,
        add_snooze_thread,
        append_learning,
        get_run_history,
    )
    from mail_agent.cards.service import cards_to_frontend
    from mail_agent.actions.service import (
        _fetch_thread_context_sync,
        summarize_thread,
        generate_draft_reply,
        reply_now,
    )
    from mail_agent.storage.types import PersistentCard

    mailbox = str(arguments.get("mailbox", "")).strip()
    card_id = str(arguments.get("card_id", "")).strip()

    if tool == "get_active_cards":
        if not mailbox:
            return {"error": "mailbox is required"}
        cards = await storage_get_cards(mailbox)
        scan_state = await get_scan_state(mailbox)
        return {
            "cards": cards_to_frontend(cards),
            "count": len(cards.cards),
            "scan_state": {
                "last_scan_ts": scan_state.last_scan_ts,
                "last_message_internal_date": scan_state.last_message_internal_date,
                "total_scans": scan_state.total_scans,
                "total_processed": scan_state.total_processed,
            },
        }

    if tool == "get_scan_plan":
        if not mailbox:
            return {
                "mailbox": "",
                "first_scan_days": 7,
                "incremental_days": 7,
                "max_messages": 100,
                "scan_categories": [],
                "updated_at": "",
            }
        plan = await get_scan_plan(mailbox)
        return {
            "mailbox": plan.mailbox,
            "scan_window_days": plan.scan_window_days,
            "max_messages": plan.max_messages,
            "scan_categories": plan.scan_categories,
            "updated_at": plan.updated_at,
        }

    if tool == "set_scan_plan":
        # Get target mailboxes: empty = all registered
        if mailbox:
            targets = [mailbox]
        else:
            from mail_agent.storage.ops import get_mailbox_registry
            registry = await get_mailbox_registry()
            targets = [e.email for e in registry.mailboxes if e.email]
            if not targets:
                return {"error": "no registered mailboxes"}
        for mb in targets:
            plan = await get_scan_plan(mb)
            for field in ("scan_window_days", "max_messages"):
                val = arguments.get(field)
                if val is not None:
                    setattr(plan, field, int(val))
            val = arguments.get("scan_categories")
            if isinstance(val, list):
                plan.scan_categories = [str(c) for c in val if str(c) in ("promotions", "social", "updates", "forums")]
            await set_scan_plan(mb, plan)
        return {"ok": True, "mailbox": mailbox or "all", "targets": targets}

    if tool == "get_card_detail":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            return {"error": f"Card {card_id} not found"}
        thread_ctx = await asyncio.to_thread(_fetch_thread_context_sync, mailbox, card)

        # Read full body from cache, re-decode to pick up _decode_body fix.
        # New cache entries include raw payload for re-decoding; old ones
        # fall back to cached body_text with light dedup.
        latest_body = card.original.body or ""
        try:
            from mail_agent.mail_providers.gmail.adapter import normalize_mailbox, _decode_body, read_message
            msg = read_message(normalize_mailbox(mailbox), card.message_id)
            if isinstance(msg, dict):
                raw = _decode_body(msg)
                if not raw.strip():
                    raw = str(msg.get("body_text") or "")
                raw = raw[:8000]
                import re
                raw = re.sub(r"<style[^>]*>.*?</style>", "", raw, flags=re.DOTALL | re.IGNORECASE)
                raw = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL | re.IGNORECASE)
                raw = re.sub(r"<[^>]+>", "", raw)
                raw = re.sub(r"&nbsp;", " ", raw)
                raw = re.sub(r"&amp;", "&", raw)
                raw = re.sub(r"&lt;", "<", raw)
                raw = re.sub(r"&gt;", ">", raw)
                raw = re.sub(r"&quot;", '"', raw)
                raw = re.sub(r"&#\d+;", "", raw)
                raw = re.sub(r"\n{3,}", "\n\n", raw)
                raw = raw.strip()
                latest_body = raw
        except Exception:
            pass

        contact_ctx = {}
        try:
            from mail_agent.contact_memory.retriever import contact_email_from_header, retrieve_contact_context
            from mail_agent.contact_memory.types import ContactMemoryQuery
            contact_email = contact_email_from_header(card.original.from_addr)
            _detail_sampling = _build_sampling_for_run(arguments, invoke_id)
            contact = await retrieve_contact_context(ContactMemoryQuery(
                mailbox=mailbox,
                contact_email=contact_email,
                current_subject=card.original.thread or card.title,
                current_body=latest_body,
                current_thread_id=card.thread_id,
                purpose="thread_summary",
            ), sampling_create_message=_detail_sampling)
            from dataclasses import asdict
            contact_ctx = asdict(contact)
        except Exception:
            contact_ctx = {}
        return {
            "card": _serialize_card_for_frontend(card),
            "thread_context": thread_ctx,
            "contact_context": contact_ctx,
            "latest_body": latest_body,
        }

    # Build sampling for Anna LLM path (same logic as _build_sampling_for_run)
    _sampling = _build_sampling_for_run(arguments, invoke_id)

    # ── Background (async) tools — return run_id immediately ──
    if tool == "start_summarize_thread":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        run_id = f"bg_{uuid.uuid4().hex[:12]}"
        MAIL_AGENT_RUNS[run_id] = {"run_id": run_id, "status": "queued", "stage": "summarize_thread", "progress": {}, "warnings": [], "started_at": beijing_now(), "updated_at": beijing_now(), "result": None, "error": "", "partial": {}}
        _save_run_checkpoint(run_id)
        asyncio.ensure_future(_handle_summarize_background(run_id, arguments, invoke_id))
        return {"success": True, "run_id": run_id, "status": "queued"}

    if tool == "start_generate_draft":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        run_id = f"bg_{uuid.uuid4().hex[:12]}"
        MAIL_AGENT_RUNS[run_id] = {"run_id": run_id, "status": "queued", "stage": "generate_draft_reply", "progress": {}, "warnings": [], "started_at": beijing_now(), "updated_at": beijing_now(), "result": None, "error": "", "partial": {}}
        _save_run_checkpoint(run_id)
        asyncio.ensure_future(_handle_generate_draft_background(run_id, arguments, invoke_id))
        return {"success": True, "run_id": run_id, "status": "queued"}

    if tool == "summarize_thread":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            return {"error": f"Card {card_id} not found"}
        result = await summarize_thread(card, mailbox, sampling_create_message=_sampling)
        summary = result.get("summary") if isinstance(result, dict) else {}
        if isinstance(summary, dict):
            import json as _json
            card.thread_summary = _json.dumps(summary, ensure_ascii=False)
            from mail_agent.storage.ops import set_active_cards
            await set_active_cards(mailbox, cards)
        return result

    if tool == "generate_draft_reply":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        reply_mode = str(arguments.get("reply_mode", "reply_to_sender")).strip() or "reply_to_sender"
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            return {"error": f"Card {card_id} not found"}
        user_answers = arguments.get("user_answers") if isinstance(arguments.get("user_answers"), dict) else None
        result = await generate_draft_reply(card, mailbox, reply_mode, sampling_create_message=_sampling, user_answers=user_answers)
        draft_body = (result.get("draft") or {}).get("body", "") if isinstance(result, dict) else ""
        if draft_body:
            card.draft_reply = draft_body
            from mail_agent.storage.ops import set_active_cards
            await set_active_cards(mailbox, cards)
        return result

    if tool == "generate_ask_draft":
        message_id = str(arguments.get("message_id", "")).strip()
        thread_id = str(arguments.get("thread_id", "")).strip()
        from_addr = str(arguments.get("from_addr", "")).strip()
        subject = str(arguments.get("subject", "")).strip()
        user_answers = arguments.get("user_answers") if isinstance(arguments.get("user_answers"), dict) else {}
        if not mailbox or not message_id:
            return {"error": "mailbox and message_id are required"}
        if not user_answers:
            return {"error": "user_answers is required"}
        from mail_agent.ask.answer import generate_ask_item_draft
        return await generate_ask_item_draft(
            message_id=message_id,
            thread_id=thread_id,
            mailbox=mailbox,
            from_addr=from_addr,
            subject=subject,
            user_answers=user_answers,
            sampling_create_message=_sampling,
        )

    if tool == "revise_draft":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        current_draft = str(arguments.get("current_draft", "")).strip()
        revision_input = str(arguments.get("revision_input", "")).strip()
        user_answers = arguments.get("user_answers") if isinstance(arguments.get("user_answers"), dict) else None
        if not current_draft and not revision_input:
            return {"error": "current_draft or revision_input is required"}
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            return {"error": f"Card {card_id} not found"}
        result = await generate_draft_reply(card, mailbox, "reply_to_sender", sampling_create_message=_sampling, current_draft=current_draft, revision_input=revision_input)
        draft_body = (result.get("draft") or {}).get("body", "") if isinstance(result, dict) else ""
        if draft_body:
            card.draft_reply = draft_body
            from mail_agent.storage.ops import set_active_cards
            await set_active_cards(mailbox, cards)
        return result

    if tool == "record_card_decision":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        decision = str(arguments.get("decision", "")).strip()
        if decision not in ("no_action_needed", "handled_manually", "dismissed"):
            return {"error": f"Invalid decision: {decision}"}
        updated = await update_card_status(mailbox, card_id, "resolved" if decision != "dismissed" else "dismissed", decision)
        # Record card title for history
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        card_title = card.title if card else card_id
        # Record weak signal: no_action_needed → LearningRecord
        if decision == "no_action_needed":
            if card:
                await append_learning(card.original.from_addr, "no_action_needed")
        if card:
            try:
                from mail_agent.contact_memory.indexer import ingest_card_event
                await ingest_card_event(
                    mailbox,
                    card,
                    event_type="user_decision",
                    source="handle_action",
                    user_action=decision,
                    sampling_create_message=_sampling,
                )
            except Exception:
                pass
        # Write history
        from mail_agent.storage.ops import append_card_action
        await append_card_action(mailbox, card_id, card_title, decision, "", **_card_context(card) if card else {})
        return {"ok": updated is not None, "card_id": card_id, "decision": decision}

    if tool == "clear_active_cards":
        if not mailbox:
            return {"error": "mailbox is required"}
        from mail_agent.storage.ops import set_active_cards as _set_active, set_scan_state
        from mail_agent.storage.types import ActiveCards, ScanState, _now
        await _set_active(mailbox, ActiveCards(cards=[], updated_at=_now()))
        # Reset scan state so the UI shows first-run welcome
        await set_scan_state(mailbox, ScanState(
            mailbox=mailbox,
            last_scan_ts="",
            last_message_internal_date="",
            total_scans=0,
            total_processed=0,
        ))
        return {"ok": True, "cleared": True}

    if tool == "mark_cleanup_read":
        return await _handle_mark_cleanup_read(arguments)

    if tool == "record_snooze":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        snooze_option = str(arguments.get("snooze_option", "")).strip()
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        card_title = card.title if card else card_id
        if snooze_option == "dont_prioritize":
            if card:
                await add_snooze_sender(card.original.from_addr)
                await add_snooze_thread(card.original.thread)
                reasons = arguments.get("reasons") if isinstance(arguments.get("reasons"), list) else []
                if reasons:
                    from mail_agent.storage.ops import get_user_prefs, set_snooze_prefs
                    prefs = await get_user_prefs()
                    for r in reasons:
                        if str(r) not in prefs.snooze.reasons:
                            prefs.snooze.reasons.append(str(r))
                    await set_snooze_prefs(prefs.snooze)
                await update_card_status(mailbox, card_id, "resolved", "dont_prioritize")
            from mail_agent.storage.ops import append_card_action
            await append_card_action(mailbox, card_id, card_title, "snooze", "dont_prioritize", **_card_context(card) if card else {})
            return {"ok": True, "card_id": card_id, "option": snooze_option}
        else:
            from datetime import datetime as _datetime, timedelta as _timedelta
            from mail_agent.storage.types import BEIJING_TZ
            now_ts = _datetime.now(BEIJING_TZ)
            if snooze_option == "tomorrow":
                until = (now_ts + _timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            elif snooze_option == "next_week":
                days_until_monday = (7 - now_ts.weekday()) % 7 or 7
                until = (now_ts + _timedelta(days=days_until_monday)).replace(hour=9, minute=0, second=0, microsecond=0)
            else:
                return {"error": f"Unknown snooze option: {snooze_option}"}
            for c in cards.cards:
                if c.card_id == card_id:
                    c.status = "snoozed"
                    c.snooze_until = until.isoformat()
                    from mail_agent.storage.ops import set_active_cards
                    await set_active_cards(mailbox, cards)
                    break
            from mail_agent.storage.ops import append_card_action
            await append_card_action(mailbox, card_id, card_title, "snooze", snooze_option, **_card_context(card) if card else {})
            return {"ok": True, "card_id": card_id, "option": snooze_option, "snooze_until": until.isoformat()}

    if tool == "restore_card":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        await update_card_status(mailbox, card_id, "pending")
        # Remove from snooze prefs (don't-prioritize)
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if card:
            try:
                from mail_agent.storage.ops import remove_snooze_sender, remove_snooze_thread
                await remove_snooze_sender(card.original.from_addr)
                await remove_snooze_thread(card.original.thread)
            except Exception:
                pass
        # If it's a cleanup bundle card, mark messages as UNREAD in Gmail
        if card and card.card_type == "cleanup_bundle" and card.bundled_messages:
            try:
                import json as _json3
                import urllib.request as _ur2
                from mail_agent.mail_providers.gmail.adapter import get_access_token as _gt2
                msg_ids = [str(m.get("message_id", "")) for m in card.bundled_messages if str(m.get("message_id", ""))]
                if msg_ids:
                    token = _gt2(mailbox)
                    body = _json3.dumps({"ids": msg_ids, "addLabelIds": ["UNREAD"]}).encode("utf-8")
                    req = _ur2.Request(
                        "https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify",
                        data=body,
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                        method="POST",
                    )
                    with _ur2.urlopen(req, timeout=30) as resp:
                        pass
            except Exception:
                pass
        # Write history
        card_title = card.title if card else card_id
        from mail_agent.storage.ops import append_card_action
        await append_card_action(mailbox, card_id, card_title, "restore", "", **_card_context(card) if card else {})
        return {"ok": True, "card_id": card_id}

    if tool == "delete_custom_plan":
        plan_id = str(arguments.get("plan_id", "")).strip()
        if not plan_id:
            return {"error": "plan_id is required"}
        from mail_agent.storage.ops import delete_custom_plan
        await delete_custom_plan(plan_id)
        return {"ok": True, "plan_id": plan_id}

    if tool == "clear_cards":
        category = str(arguments.get("category", "")).strip()
        if not mailbox or not category:
            return {"error": "mailbox and category are required"}
        from mail_agent.storage.ops import clear_cards_by_category
        removed = await clear_cards_by_category(mailbox, category)
        return {"ok": True, "removed": removed, "category": category, "mailbox": mailbox}

    if tool == "clear_history":
        from mail_agent.storage.ops import clear_run_history
        await clear_run_history()
        return {"ok": True}

    if tool == "reset_all_data":
        from mail_agent.storage.ops import reset_all_data
        import shutil
        await reset_all_data()
        # Local file-system cleanup: remove the entire data directory.
        # APS stores data remotely so this is a no-op there.
        root = data_root()
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        return {"ok": True}

    if tool == "reply_now":
        log(f"[reply_now] mailbox={mailbox} card_id={card_id} dry_run={arguments.get('dry_run', True)} reply_mode={arguments.get('reply_mode', 'reply_to_sender')} draft_len={len(str(arguments.get('draft_body', '')))}")
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        draft_body = str(arguments.get("draft_body", "")).strip()
        if not draft_body:
            return {"error": "draft_body is required"}
        reply_mode = str(arguments.get("reply_mode", "reply_to_sender")).strip() or "reply_to_sender"
        dry_run = arguments.get("dry_run", True)
        if not isinstance(dry_run, bool):
            dry_run = True
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            log(f"[reply_now] card not found: {card_id} among {len(cards.cards)} cards")
            return {"error": f"Card {card_id} not found"}
        log(f"[reply_now] card found: {card.title} thread_id={card.thread_id} to={card.original.from_addr}")
        result = await reply_now(card, mailbox, draft_body, reply_mode, dry_run=dry_run)
        log(f"[reply_now] result: ok={result.get('ok')} dry_run={result.get('dry_run')} error={result.get('error', '')}")
        if result.get("ok") and not dry_run:
            await update_card_status(mailbox, card_id, "resolved", "replied")
        if result.get("ok"):
            try:
                from mail_agent.contact_memory.indexer import ingest_card_event
                await ingest_card_event(
                    mailbox,
                    card,
                    event_type="user_replied",
                    source="handle_action",
                    user_action="reply",
                    draft_excerpt=draft_body,
                    sampling_create_message=_sampling,
                )
            except Exception:
                pass
        # Write history
        from mail_agent.storage.ops import append_card_action
        detail_preview = draft_body[:80]
        await append_card_action(mailbox, card_id, card.title, "reply", detail_preview, **_card_context(card))
        return result

    if tool == "reply_from_ask":
        if not mailbox:
            return {"error": "mailbox is required"}
        thread_id = str(arguments.get("thread_id", "")).strip()
        to_addr = str(arguments.get("to_addr", "")).strip()
        body = str(arguments.get("body", "")).strip()
        if not thread_id or not to_addr or not body:
            return {"error": "thread_id, to_addr, and body are required"}
        reply_mode = str(arguments.get("reply_mode", "reply_to_sender")).strip() or "reply_to_sender"
        dry_run = arguments.get("dry_run", True)
        if not isinstance(dry_run, bool):
            dry_run = True
        from mail_agent.mail_providers.gmail.adapter import send_reply
        import asyncio as _asyncio
        if dry_run:
            return {"ok": True, "dry_run": True, "message": "Mock: reply was NOT sent."}
        try:
            result = await _asyncio.to_thread(send_reply, mailbox, thread_id, to_addr, body, reply_mode=reply_mode)
            from mail_agent.storage.ops import append_card_action
            await append_card_action(mailbox, "", thread_id, "reply_from_ask", body[:80])
            return {"ok": True, "dry_run": False, "result": result}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if tool == "mark_read_from_ask":
        if not mailbox:
            return {"error": "mailbox is required"}
        raw_ids = arguments.get("message_ids") or []
        message_ids = [str(mid).strip() for mid in raw_ids if str(mid).strip()] if isinstance(raw_ids, list) else []
        if not message_ids:
            return {"error": "message_ids (non-empty array) is required"}
        from mail_agent.mail_providers.gmail.adapter import batch_mark_read
        import asyncio as _asyncio
        try:
            result = await _asyncio.to_thread(batch_mark_read, mailbox, message_ids)
            from mail_agent.storage.ops import append_card_action
            await append_card_action(mailbox, "", "", "mark_read_from_ask", f"{len(message_ids)} emails")
            return {"ok": True, "marked": len(message_ids), "result": result}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if tool == "trash_from_ask":
        if not mailbox:
            return {"error": "mailbox is required"}
        raw_ids = arguments.get("message_ids") or []
        message_ids = [str(mid).strip() for mid in raw_ids if str(mid).strip()] if isinstance(raw_ids, list) else []
        if not message_ids:
            return {"error": "message_ids (non-empty array) is required"}
        from mail_agent.mail_providers.gmail.adapter import trash_email
        import asyncio as _asyncio
        errors = []
        for mid in message_ids:
            try:
                await _asyncio.to_thread(trash_email, mailbox, mid)
            except Exception as exc:
                errors.append(f"{mid}: {exc}")
        from mail_agent.storage.ops import append_card_action
        await append_card_action(mailbox, "", "", "trash_from_ask", f"{len(message_ids)} emails")
        if errors:
            return {"ok": False, "trashed": len(message_ids) - len(errors), "errors": errors}
        return {"ok": True, "trashed": len(message_ids)}

    if tool == "record_learning":
        pattern = str(arguments.get("pattern", "")).strip()
        action = str(arguments.get("action", "")).strip()
        if not pattern or not action:
            return {"error": "pattern and action are required"}
        await append_learning(pattern, action)
        return {"ok": True, "pattern": pattern, "action": action}

    if tool == "get_run_history":
        history = await get_run_history(limit=20)
        return {"history": [serialize_value(h) for h in history]}

    return {"error": f"Unknown V2 tool: {tool}"}


def _serialize_card_for_frontend(card: Any) -> dict[str, Any]:
    import re as _re

    def _dc(text: str) -> str:
        if not text:
            return ""
        text = _re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))) if int(m.group(1)) < 0x110000 else "?", text)
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"').replace("&#39;", "'")
        return text

    return {
        "id": card.card_id,
        "title": card.title,
        "summary": card.summary,
        "recommendation": card.recommendation,
        "label": card.label,
        "details": {
            "needs": card.details.needs,
            "latestActivity": card.details.latest_activity,
            "reviewed": card.details.reviewed,
            "mailbox": card.details.mailbox,
        },
        "original": {
            "source": card.original.source,
            "thread": card.original.thread,
            "from": card.original.from_addr,
            "to": card.original.to_addr,
            "time": card.original.time,
            "status": card.original.status,
            "body": _dc(card.original.body),
        },
        "actions": [
            {"id": a.id, "label": a.label, "primary": a.primary, "statusTitle": a.status_title, "status": a.status}
            for a in (card.actions or [])
        ],
        "status": card.status,
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
    if tool == "list_cached_emails":
        return {"success": True, "tool": tool, "data": list_cached_emails(arguments.get("mailbox", ""))}
    if tool == "get_cached_email":
        return {"success": True, "tool": tool, "data": get_cached_email(arguments.get("mailbox", ""), arguments.get("message_id", ""))}
    if tool == "check_gmail_auth":
        return {"success": True, "tool": tool, "data": _check_gmail_auth(arguments.get("mailbox", ""))}
    if tool == "start_mail_agent_run":
        return {"success": True, "tool": tool, "data": start_mail_agent_run(arguments, invoke_id)}
    if tool == "get_mail_agent_run":
        return {"success": True, "tool": tool, "data": get_mail_agent_run(arguments.get("run_id", ""))}
    if tool == "start_custom_scan":
        return {"success": True, "tool": tool, "data": start_custom_scan(arguments, invoke_id)}
    if tool == "re_run_custom_scan":
        return {"success": True, "tool": tool, "data": re_run_custom_scan(arguments, invoke_id)}
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

    if tool == "get_active_cards":
        return {"success": True, "tool": tool, "data": _sync_get_active_cards(arguments)}
    if tool == "list_mailboxes":
        return {"success": True, "tool": tool, "data": _sync_list_mailboxes()}
    if tool == "set_mailbox_selected":
        return {"success": True, "tool": tool, "data": _sync_set_mailbox_selected(arguments)}
    if tool == "remove_mailbox":
        return {"success": True, "tool": tool, "data": _sync_remove_mailbox(arguments)}
    if tool == "get_run_history":
        return {"success": True, "tool": tool, "data": _sync_get_run_history()}

    # ── V2 interaction tools (async → dispatch to event loop) ──
    if tool in (
        "get_card_detail", "summarize_thread",
        "generate_draft_reply", "generate_ask_draft", "revise_draft", "record_card_decision",
        "clear_active_cards", "mark_cleanup_read", "record_snooze", "restore_card", "record_learning",
        "start_summarize_thread", "start_generate_draft",
        "delete_custom_plan",
        "clear_cards", "clear_history", "reset_all_data",
        "get_scan_plan", "set_scan_plan",
        "reply_now", "reply_from_ask", "mark_read_from_ask", "trash_from_ask",
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


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    try:
        if method == "initialize":
            return make_response(request_id, result=handle_initialize(params))
        if method == "describe":
            return make_response(request_id, result=MANIFEST)
        if method == "health":
            return make_response(
                request_id,
                result={
                    "status": "healthy",
                    "timestamp": beijing_now(),
                    "version": VERSION,
                    "tools_count": len(MANIFEST["tools"]),
                },
            )
        if method == "invoke":
            return make_response(request_id, result=handle_invoke(params))
        if method == "shutdown":
            return make_response(request_id, result={"ok": True})
        return make_response(request_id, error=make_error(-32601, f"Method not found: {method}"))
    except ValueError as exc:
        return make_response(request_id, error=make_error(-32601, str(exc)))
    except RuntimeError as exc:
        try:
            error_data = json.loads(str(exc))
        except json.JSONDecodeError:
            error_data = {"code": -32603, "message": str(exc)}
        return make_response(request_id, error=make_error(int(error_data.get("code", -32603)), str(error_data.get("message", exc)), error_data.get("data")))
    except StorageError as exc:
        log(f"storage error: {exc}")
        return make_response(request_id, error=make_error(exc.code, exc.message, exc.data))
    except Exception as exc:
        trace = traceback.format_exc()
        log(f"internal error: {type(exc).__name__}: {exc}\n{trace}")
        return make_response(
            request_id,
            error=make_error(
                -32603,
                f"{type(exc).__name__}: {exc}",
                {"traceback": trace},
            ),
        )


def handle_line(line: str) -> None:
    # Windows 管道偶尔会在首行带 BOM，这里只清理协议行开头的 BOM。
    line = line.lstrip("\ufeff")
    # Diagnostic: check if stdin encoding is working for CJK text
    try:
        _diag_bytes = line.encode("utf-8")
    except Exception:
        _diag_bytes = b"<encode failed>"
    non_ascii = any(b > 127 for b in _diag_bytes)
    if non_ascii and len(line) > 40:
        log(f"stdin-diag first 80 chars: {repr(line[:80])}")

    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        write_frame(make_response(None, error=make_error(-32700, "Parse error")))
        return

    if not isinstance(message, dict):
        write_frame(make_response(None, error=make_error(-32600, "Invalid request")))
        return

    if "method" not in message:
        if not sampling.dispatch_response(message) and not _route_storage_response(message):
            log(f"unmatched response id={message.get('id')!r}")
        return

    response = handle_request(message)
    if response is not None and message.get("id") is not None:
        write_frame(response)


def main() -> None:
    # Write startup log so we can diagnose harness crashes even without stderr.
    _diag_dir = data_root() / "anna-inbox" / "diagnostics"
    _diag_dir.mkdir(parents=True, exist_ok=True)
    _diag_path = _diag_dir / "agent_startup.log"
    with open(_diag_path, "a", encoding="utf-8") as _df:
        _df.write(f"{beijing_now()} startup stdin={sys.stdin.encoding} stdout={sys.stdout.encoding}\n")

    log("ready")
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="mail-agent-rpc") as pool:
        try:
            for raw_line in sys.stdin:
                line = raw_line.strip()
                if line:
                    pool.submit(handle_line, line)
        except Exception as exc:
            with open(_diag_path, "a", encoding="utf-8") as _df:
                import traceback
                _df.write(f"{beijing_now()} CRASH {type(exc).__name__}: {exc}\n{traceback.format_exc()}\n")
            log(f"stdin loop crashed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
