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
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
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
            "description": "Prepare a pollable brief run. Follow with continue_mail_agent_run to execute scan and LLM phases.",
            "parameters": [
                {"name": "user_request", "type": "string", "description": "Natural language request from user.", "required": True},
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "mode", "type": "string", "description": "Strategy mode: auto, default_secretary, creator_opportunity, security_billing", "required": False},
                {"name": "max_messages", "type": "integer", "description": "Maximum messages to scan.", "required": False},
                {"name": "primary_count", "type": "integer", "description": "How many recent Primary emails to fetch first.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider: dashscope or anna-llm.", "required": False},
                {"name": "run_id", "type": "string", "description": "Client-generated run ID for polling progress.", "required": False},
                {"name": "storage_provider", "type": "string", "description": "Storage provider: local or aps.", "required": False},
            ],
        },
        {
            "name": "continue_mail_agent_run",
            "description": "Advance one short Brief state-machine slice with sampling bound to this invoke.",
            "parameters": [
                {"name": "run_id", "type": "string", "description": "Run ID from start_mail_agent_run.", "required": True},
                {"name": "user_request", "type": "string", "description": "Natural language request from user.", "required": False},
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": False},
                {"name": "mode", "type": "string", "description": "Strategy mode.", "required": False},
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
            "name": "get_cleanup_bundle_page",
            "description": "Get one page of cleanup bundle messages without refreshing active cards.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address or all.", "required": True},
                {"name": "offset", "type": "integer", "description": "Cleanup item offset.", "required": False},
                {"name": "limit", "type": "integer", "description": "Maximum cleanup items to return.", "required": False},
                {"name": "storage_provider", "type": "string", "description": "Storage provider: local or aps.", "required": False},
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
                {"name": "run_id", "type": "string", "description": "Client-generated run ID for polling progress before execution completes.", "required": False},
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
                {"name": "run_id", "type": "string", "description": "Client-generated run ID for polling progress before execution completes.", "required": False},
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
        {
            "name": "generate_contact_memories",
            "description": "Start a contact-memory backfill run for active brief cards. Prefer start_contact_memory_run + continue_contact_memory_run.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address, or all.", "required": False},
                {"name": "mailboxes", "type": "array", "description": "Mailbox email addresses.", "required": False},
                {"name": "since", "type": "string", "description": "Only process cards created at or after this ISO timestamp.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider: dashscope or anna-llm.", "required": False},
                {"name": "storage_provider", "type": "string", "description": "Storage backend: local or aps.", "required": False},
            ],
        },
        {
            "name": "start_contact_memory_run",
            "description": "Create a pollable contact-memory backfill run. Follow with continue_contact_memory_run.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address, or all.", "required": False},
                {"name": "mailboxes", "type": "array", "description": "Mailbox email addresses.", "required": False},
                {"name": "since", "type": "string", "description": "Only process cards created at or after this ISO timestamp.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider: dashscope or anna-llm.", "required": False},
                {"name": "storage_provider", "type": "string", "description": "Storage backend: local or aps.", "required": False},
                {"name": "run_id", "type": "string", "description": "Optional caller-supplied run id.", "required": False},
            ],
        },
        {
            "name": "continue_contact_memory_run",
            "description": "Advance one short contact-memory backfill slice with sampling bound to this invoke.",
            "parameters": [
                {"name": "run_id", "type": "string", "description": "Run ID from start_contact_memory_run.", "required": True},
                {"name": "batch_limit", "type": "number", "description": "Maximum cards to process in this invoke.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider: dashscope or anna-llm.", "required": False},
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


def _public_run_view(state: dict[str, Any]) -> dict[str, Any]:
    brief = state.get("brief") if isinstance(state.get("brief"), dict) else {}
    return {
        "success": state.get("status") != "failed",
        "run_id": state.get("run_id", ""),
        "status": state.get("status", ""),
        "stage": state.get("stage", ""),
        "progress": state.get("progress") or {},
        "warnings": state.get("warnings") or [],
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "error": state.get("error", ""),
        "needs_continue": bool(state.get("needs_continue")),
        "cards_added": int(state.get("cards_added") or 0),
        "cards_version": int(brief.get("cards_version") or state.get("cards_version") or 0),
        "result": _compact_run_result(state.get("result")),
    }


def handle_initialize(params: dict[str, Any]) -> dict[str, Any]:
    protocol_version = str((params or {}).get("protocolVersion") or "1.1")
    v2 = protocol_version == PROTOCOL_VERSION_V2
    host_caps = (params or {}).get("capabilities") or (params or {}).get("client_capabilities") or {}
    host_cap_list = sorted(host_caps.keys()) if isinstance(host_caps, dict) else []
    sampling.record_init(protocol_version, host_cap_list, params or {})
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
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType") or "")
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        data = body.get("data")
        if data and mime_type in {"text/plain", "text/html"}:
            try:
                decoded = base64.urlsafe_b64decode(str(data) + "=" * (-len(str(data)) % 4)).decode("utf-8", errors="replace")
                if mime_type == "text/plain":
                    plain_parts.append(decoded)
                else:
                    html_parts.append(decoded)
            except Exception:
                return
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    walk(payload)

    # Prefer text/html — modern email uses it as the canonical format.
    # Callers are responsible for stripping HTML tags as needed.
    parts = html_parts or plain_parts
    return "\n\n".join(part.strip() for part in parts if part.strip())[:30000]


def _dedup_body(text: str) -> str:
    """Remove duplicated adjacent segments in body text.

    The old decode_gmail_body joined both text/plain and text/html parts
    with ``\\n\\n``, so multipart/alternative emails contain the same
    content twice — once as plain text, once as HTML.  This compares
    segments after stripping HTML tags and collapsing whitespace.
    """
    if not text or "\n\n" not in text:
        return text
    import re
    parts = text.split("\n\n")

    def _normalize(s: str) -> str:
        stripped = re.sub(r"<[^>]+>", "", s)
        stripped = re.sub(r"&nbsp;", " ", stripped)
        stripped = re.sub(r"&amp;", "&", stripped)
        stripped = re.sub(r"&lt;", "<", stripped)
        stripped = re.sub(r"&gt;", ">", stripped)
        stripped = re.sub(r"&quot;", '"', stripped)
        return " ".join(stripped.split())

    result = [parts[0]]
    for i in range(1, len(parts)):
        if _normalize(result[-1]) != _normalize(parts[i]):
            result.append(parts[i])
    return "\n\n".join(result)


def _inject_link_attrs(tag_str: str) -> str:
    """Add target='_blank' rel='noopener noreferrer' to <a> if missing."""
    result = tag_str
    if 'target=' not in result:
        result = result[:-1] + ' target="_blank">' if result.endswith('>') else result + ' target="_blank">'
    if 'rel=' not in result:
        result = result[:-1] + ' rel="noopener noreferrer">' if result.endswith('>') else result + ' rel="noopener noreferrer">'
    return result


def _inject_img_attrs(tag_str: str) -> str:
    """Add loading='lazy' referrerpolicy='no-referrer' to <img> if missing."""
    result = tag_str
    if 'loading=' not in result:
        result = result + ' loading="lazy"'
    if 'referrerpolicy=' not in result:
        result = result + ' referrerpolicy="no-referrer"'
    return result


def _sanitize_email_html(html_text: str) -> str:
    """Strip XSS vectors from email HTML. Keeps images, links, formatting."""
    import re
    html_text = re.sub(
        r"<script[^>]*>.*?</script>",
        "", html_text, flags=re.DOTALL | re.IGNORECASE,
    )
    html_text = re.sub(
        r"<style[^>]*>.*?</style>",
        "", html_text, flags=re.DOTALL | re.IGNORECASE,
    )

    # Strip event-handler attributes (onclick, onerror, onload, etc.)
    html_text = re.sub(
        r'\s+on\w+\s*=\s*"[^"]*"',
        "", html_text, flags=re.IGNORECASE,
    )
    html_text = re.sub(
        r"\s+on\w+\s*=\s*'[^']*'",
        "", html_text, flags=re.IGNORECASE,
    )
    html_text = re.sub(
        r'\s+on\w+\s*=\s*\S+',
        "", html_text, flags=re.IGNORECASE,
    )

    # Remove javascript: / vbscript: URL schemes — delete the entire attribute
    html_text = re.sub(
        r'\s*(?:href|src|action|formaction)\s*=\s*["\'][^"\']*javascript\s*:[^"\']*["\']',
        "", html_text, flags=re.IGNORECASE,
    )
    html_text = re.sub(
        r'\s*(?:href|src|action|formaction)\s*=\s*["\'][^"\']*vbscript\s*:[^"\']*["\']',
        "", html_text, flags=re.IGNORECASE,
    )

    # Remove <iframe>, <object>, <embed> tags
    html_text = re.sub(
        r"<iframe[^>]*>.*?</iframe>",
        "", html_text, flags=re.DOTALL | re.IGNORECASE,
    )
    html_text = re.sub(
        r"<object[^>]*>.*?</object>",
        "", html_text, flags=re.DOTALL | re.IGNORECASE,
    )
    html_text = re.sub(
        r"<embed[^>]*>.*?</embed>",
        "", html_text, flags=re.DOTALL | re.IGNORECASE,
    )

    # Add target="_blank" rel="noopener noreferrer" to <a> tags that have href
    html_text = re.sub(
        r'(<a\b[^>]*href\s*=\s*["\'][^"\']+["\'][^>]*)>',
        lambda m: _inject_link_attrs(m.group(1)),
        html_text, flags=re.IGNORECASE,
    )

    # Add loading="lazy" referrerpolicy="no-referrer" to <img> tags.
    # Lazy match stops before the optional self-closing slash so we
    # don't inject attributes after it.
    html_text = re.sub(
        r'(<img\b[^>]*?)\s*/?\s*>',
        lambda m: _inject_img_attrs(m.group(1)) + '>',
        html_text, flags=re.IGNORECASE,
    )

    return html_text


def _build_cid_map(payload: dict[str, Any]) -> dict[str, str]:
    """Walk MIME parts for inline images with Content-ID, return {cid: data_uri}."""
    cid_map: dict[str, str] = {}

    def walk(part: dict[str, Any]) -> None:
        headers = part.get("headers") or []
        cid: str | None = None
        for h in headers:
            if isinstance(h, dict) and str(h.get("name") or "").lower() == "content-id":
                cid = str(h.get("value") or "").strip().strip("<>")
                break
        if cid:
            mime_type = str(part.get("mimeType") or "image/png")
            if not mime_type.startswith("image/"):
                return
            body = part.get("body") if isinstance(part.get("body"), dict) else {}
            data = body.get("data")
            if data:
                try:
                    import base64
                    raw_bytes = base64.urlsafe_b64decode(str(data) + "=" * (-len(str(data)) % 4))
                    b64 = base64.b64encode(raw_bytes).decode("ascii")
                    cid_map[cid] = f"data:{mime_type};base64,{b64}"
                except Exception:
                    pass
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(payload)
    return cid_map


def _resolve_cid_images(html_text: str, payload: dict[str, Any]) -> str:
    """Replace cid: references in <img src> with inline data URIs."""
    cid_map = _build_cid_map(payload)
    if not cid_map:
        return html_text
    import re

    def _replace(m: re.Match[str]) -> str:
        cid = m.group(1)
        # Strip optional @host suffix (cid:xxx@host)
        cid_key = cid.split("@")[0] if "@" in cid else cid
        uri = cid_map.get(cid_key) or cid_map.get(cid)
        if uri:
            return f'src="{uri}"'
        return m.group(0)

    # Match src="cid:..." or src='cid:...'
    html_text = re.sub(
        r'''src\s*=\s*["']cid:([^"'\s]+)["']''',
        _replace, html_text, flags=re.IGNORECASE,
    )
    return html_text


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
                authorized_email = ""
            if authorized_email:
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
            scanned = await run_mail_scan(mailbox, scan_plan)
            MAIL_AGENT_RUNS[run_id]["progress"] = {"scanned": len(scanned)}
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


async def _test_sampling(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """Test Anna sampling with a minimal one-shot call. Returns detailed diagnostics."""
    started = time.time()
    req_id = ""
    try:
        sampling_fn = _build_sampling_for_run({"ai_provider": "anna-llm"}, invoke_id)
        if sampling_fn is None:
            return {"ok": False, "error": "sampling_fn is None — ai_provider must be 'anna-llm'", "invoke_id": invoke_id}

        req_id = uuid.uuid4().hex[:12]
        result = await sampling_fn(
            messages=[{"role": "user", "content": {"type": "text", "text": "Say 'hello' in exactly one word. Reply with only that word."}}],
            max_tokens=16,
            system_prompt="You are a test probe. Reply concisely.",
            temperature=0.0,
            include_context="none",
            metadata={"tool": "test_sampling", "executa_invoke_id": invoke_id, "test_req_id": req_id},
            timeout=30.0,
        )
        elapsed_ms = int((time.time() - started) * 1000)
        return {
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "test_req_id": req_id,
            "invoke_id": invoke_id,
            "model": result.get("model"),
            "stop_reason": result.get("stopReason"),
            "content_type": result.get("content", {}).get("type") if isinstance(result.get("content"), dict) else "unknown",
            "text": str(result.get("content", {}).get("text", ""))[:200] if isinstance(result.get("content"), dict) else "",
            "usage": result.get("usage"),
        }
    except SamplingError as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        return {
            "ok": False,
            "elapsed_ms": elapsed_ms,
            "test_req_id": req_id,
            "invoke_id": invoke_id,
            "error_code": exc.code,
            "error_message": exc.message,
            "error_data": exc.data,
        }
    except Exception as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        return {
            "ok": False,
            "elapsed_ms": elapsed_ms,
            "test_req_id": req_id,
            "invoke_id": invoke_id,
            "error_code": "client_exception",
            "error_message": str(exc),
        }


# Mirrors the real pipeline's wire format:
#   system_prompt  = short JSON-only instruction
#   user_message   = rubric + email + output format + JSON instruction
# See call_llm_json() in llm_runtime/service.py:494 and
# build_anna_single_judgment_prompt() in judgment_engine/service.py:495.

_BRIEF_TEST_SYSTEM_PROMPT = "You are a strict JSON generator. Output ONLY valid JSON — no explanation, no markdown, no code fences."

_BRIEF_TEST_USER_MESSAGE = """\
You are Anna, an executive email assistant. Evaluate exactly ONE email candidate against the strategy below.
Output ONLY a single JSON object. Do NOT wrap in markdown. Do NOT explain. The very first character you write MUST be `{`.

## Strategy
Default 秘书模式: 找出真正需要用户注意的邮件事项。

## Judgment Rubric — Binary Decision

First, answer THIS question about the email:

  **"Does this email require the user to send a reply message?"**

  If YES → user_action = "reply"
    Then pick the BEST action_reason:
    - question_asked: the sender explicitly asked a question or made a request that needs an answer. EXCLUDE automated notifications from noreply/notification addresses and social media alerts.
    - waiting_for_you: the sender is clearly waiting for the user's input, approval, or decision. EXCLUDE automated reminders and system-generated messages.
    - unsent_draft: this is a draft the user wrote but never sent
    - courtesy_due: sender invested real effort (wrote 3+ substantive sentences, shared a document, or explicitly asked for the user's thoughts). Do NOT flag automated notifications, newsletters, receipts, or one-line status updates.

  If NO → user_action = "review"
    Then pick the BEST action_reason:
    - upcoming_event: interview/meeting/deadline reminder — worth noting the time
    - deal_or_pipeline: project/partnership/deal status update worth tracking
    - security_or_billing: security alert, billing issue, subscription — needs checking
    - receipt_or_notice: receipt, subscription confirmation, normal account notice — record only
    - cleanup: newsletter, promotion, automated digest — safe to archive

  CRITICAL: If the sender address looks automated (contains "noreply", "no-reply", "notification", "@linkedin.com" alerts, social media notification bots), set user_action="review" regardless of the email body content.

  Priority & action_reason mapping (you MUST follow):
    question_asked → priority=high when explicit deadline, money/pricing/contract, or key contact. priority=medium otherwise. surface=true.
    waiting_for_you → priority=high when time-sensitive AND sender says "let me know"/"please confirm". priority=medium otherwise. surface=true.
    unsent_draft → priority=medium, surface=true
    courtesy_due → priority=medium, surface=true
    security_or_billing → priority=critical when unauthorized/payment failed/imminent interruption. priority=high otherwise. surface=true.
    upcoming_event → priority=high when within 48h. priority=medium otherwise.
    deal_or_pipeline → priority=medium
    receipt_or_notice → priority=low
    cleanup → priority=low

## Mailbox Owner
You are evaluating mail for: test@example.com
Match by EMAIL ADDRESS (between < >), not by display name.
- If the sender's email IS the mailbox owner → OUTGOING mail.
  SENT: user already sent it → surface=false, priority=low, user_action=review, action_reason=cleanup.
  DRAFT: user hasn't sent it yet → user_action=reply, action_reason=unsent_draft, priority=medium.

## Email
candidate_id: test_cand_001
kind: reply_required_possible
priority_hint: high
from: "Alice Zhang" <alice@acmecorp.com>
subject: Q3 proposal review — need your sign-off by Friday
snippet: Hi, I've attached the updated Q3 proposal incorporating feedback from the leadership review. Could you take a look and provide sign-off by Friday EOD? The procurement team is waiting on this to finalize the vendor contract. Let me know if you need any clarifications.
date: 2026-06-09
context_type: message_detail
body_text: > Hi,\n> I've attached the updated Q3 proposal v3 incorporating the feedback from the leadership review last Thursday.\n> \n> Key changes:\n> - Budget adjusted from $180k to $210k to cover the expanded scope\n> - Timeline shifted: kickoff moved to July 15\n> - Vendor selection narrowed to two finalists (AcmeTech and BuildRight)\n> \n> Could you review and provide sign-off by Friday EOD? The procurement team is blocked on the vendor contract until they have your approval.\n> \n> Let me know if you need a call to walk through the changes.\n> \n> Thanks,\n> Alice

## User request
Find emails that need my attention.

## Output format
Return exactly this JSON shape:

{
  "candidate_id": "test_cand_001",
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
  "reply_gaps": {"needs_user_input":true, "summary":"one-sentence summary", "questions":[{"id":"q1", "question":"What do you want to reply?", "hint":"short hint", "required":true}]},
  "confidence": 0.85
}

Allowed user_action: reply, review.
Allowed action_reason: question_asked, waiting_for_you, unsent_draft, courtesy_due, upcoming_event, deal_or_pipeline, security_or_billing, receipt_or_notice, cleanup.
Allowed priority: critical, high, medium, low, ignore.
Allowed action: create_draft, create_reminder, save_note, do_nothing.

## Rules
- user_action + action_reason MUST be consistent: question_asked/waiting_for_you/unsent_draft/courtesy_due → reply. upcoming_event/deal_or_pipeline/security_or_billing/receipt_or_notice/cleanup → review.
- priority: reply reasons → medium or high. security_or_billing → critical or high. schedule/logistics → medium. receipt/cleanup → low.

Return ONLY one valid JSON object. Do not include markdown fences, prose, analysis, or code comments.
"""


async def _test_sampling_brief(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """Test Anna sampling with a realistic brief-style judgment call.

    Uses the same message shape, system prompt complexity, and parameters
    (max_tokens=8000, temperature=0.1, timeout=120s) as the real brief pipeline.
    """
    import json as _json
    started = time.time()
    req_id = ""
    try:
        sampling_fn = _build_sampling_for_run({"ai_provider": "anna-llm"}, invoke_id)
        if sampling_fn is None:
            return {"ok": False, "error": "sampling_fn is None", "invoke_id": invoke_id}

        req_id = uuid.uuid4().hex[:12]
        result = await sampling_fn(
            messages=[{"role": "user", "content": {"type": "text", "text": _BRIEF_TEST_USER_MESSAGE}}],
            max_tokens=8000,
            system_prompt=_BRIEF_TEST_SYSTEM_PROMPT,
            temperature=0.1,
            include_context="none",
            metadata={"tool": "test_sampling_brief", "executa_invoke_id": invoke_id, "test_req_id": req_id},
            timeout=120.0,
        )
        elapsed_ms = int((time.time() - started) * 1000)
        text = str(result.get("content", {}).get("text", "")) if isinstance(result.get("content"), dict) else ""
        usage = result.get("usage", {})
        output_tokens = usage.get("outputTokens") if isinstance(usage, dict) else "unknown"

        json_ok = False
        json_error = ""
        parse_preview = ""
        if text:
            try:
                parsed = _json.loads(text.strip())
                json_ok = isinstance(parsed, dict) and "priority" in parsed
                parse_preview = _json.dumps({k: v for k, v in parsed.items() if k in ("priority", "user_action", "action_reason", "title")})[:300]
            except Exception as exc:
                json_error = str(exc)[:200]
                parse_preview = text.strip()[:300]

        return {
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "test_req_id": req_id,
            "invoke_id": invoke_id,
            "model": result.get("model"),
            "stop_reason": result.get("stopReason"),
            "output_tokens": output_tokens,
            "json_ok": json_ok,
            "json_error": json_error,
            "parse_preview": parse_preview,
            "usage": usage,
        }
    except SamplingError as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        return {
            "ok": False,
            "elapsed_ms": elapsed_ms,
            "test_req_id": req_id,
            "invoke_id": invoke_id,
            "error_code": exc.code,
            "error_message": exc.message,
            "error_data": exc.data,
        }
    except Exception as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        return {
            "ok": False,
            "elapsed_ms": elapsed_ms,
            "test_req_id": req_id,
            "invoke_id": invoke_id,
            "error_code": "client_exception",
            "error_message": str(exc),
        }


def _start_test_sampling_async(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """Like test_sampling_brief, but returns immediately and runs sampling in background.

    This is the decisive experiment: if the background call fails with -32001
    while the synchronous test_sampling_brief succeeds, the Anna platform binds
    sampling authorization to the *active* invoke lifecycle.
    """
    run_id = f"tsa_{uuid.uuid4().hex[:12]}"
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
        "partial": {"invoke_id": invoke_id},
    }
    asyncio.run_coroutine_threadsafe(_run_test_sampling_async(run_id, arguments, invoke_id), loop)
    return {
        "run_id": run_id,
        "status": "queued",
        "started_at": beijing_now(),
        "note": "Sampling scheduled in background — poll with get_mail_agent_run",
    }


async def _run_test_sampling_async(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    """Background sampling test — waits 2s then calls the brief-style sampling."""
    try:
        await asyncio.sleep(2.0)  # simulate real scan delay
        MAIL_AGENT_RUNS[run_id]["status"] = "running"
        MAIL_AGENT_RUNS[run_id]["stage"] = "sampling"
        MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
        result = await _test_sampling_brief(arguments, invoke_id)
        MAIL_AGENT_RUNS[run_id].update({
            "status": "done",
            "stage": "done",
            "updated_at": beijing_now(),
            "result": result,
        })
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update({
            "status": "failed",
            "stage": "failed",
            "updated_at": beijing_now(),
            "error": str(exc),
        })


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
    max_messages = int(arguments.get("max_messages") or 50)

    _brief_update_state(run_id, stage="scan", progress={"current": 0, "total": max_messages, "mailbox": mailbox})
    input_ = MailTaskInput(user_request=user_request, mailbox_id=mailbox, user_email=mailbox, mode=mode, max_messages=max_messages, dry_run=True)
    task_plan = await parse_intent(input_, None)
    strategy = get_strategy(task_plan.strategy_mode)
    if not strategy:
        raise ValueError(f"Unknown strategy mode: {task_plan.strategy_mode}")

    scan_plan_config = await _get_scan_plan_config(mailbox)
    configured_max = scan_plan_config.max_messages if scan_plan_config else 100

    messages = await run_mail_scan(mailbox, configured_max, progress_callback=lambda stage, progress: _brief_update_state(run_id, stage=stage, progress=progress))
    all_message_ids = [m.message_id for m in messages if m.message_id]
    new_message_ids = all_message_ids
    if _storage_ready() and all_message_ids:
        from mail_agent.storage.ops import filter_unprocessed
        new_message_ids = await filter_unprocessed(mailbox, all_message_ids)
    new_id_set = set(new_message_ids)
    new_messages = _dedupe_by_thread([m for m in messages if m.message_id in new_id_set])

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
            "deduped": len(new_messages),
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
        progress={"current": cursor, "total": total, "candidates": len(brief["candidates"]), "low_value": len(low_value_items), "sampling_calls_used": len(batches)},
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
    from mail_agent.cards.service import build_card, cards_to_frontend, merge_cards
    from mail_agent.storage.ops import get_active_cards, set_active_cards
    from mail_agent.storage.types import ActiveCards

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
    cursor = int(brief.get("phase2_cursor") or 0)
    total = len(candidates)
    if cursor >= total:
        brief["stage"] = "finalizing"
        _brief_update_state(run_id, stage="finalizing", progress={"evaluated": cursor, "total": total})
        return
    batch = candidates[cursor:cursor + 4]
    _brief_update_state(run_id, stage="read_context", progress={"current": cursor, "total": total, "batch": len(batch)})
    contexts = [await read_candidate_context(mailbox, candidate) for candidate in batch]

    async def _evaluate_one(index: int, ctx: Any) -> Any:
        try:
            prompt = build_anna_single_judgment_prompt(task_plan, strategy, profile, ctx, None)
            result = await call_llm_json_safe(
                sampling_create_message,
                system_prompt="You are a strict JSON generator. Output ONLY valid JSON — no explanation, no markdown, no code fences.",
                user_message=prompt,
                fallback={},
                temperature=0.1,
                max_tokens=8000,
                timeout=55.0,
                metadata={"tool": "evaluate_item_single", "strategy_mode": strategy.id, "candidate_count": "1"},
                allow_fallback=True,
                allow_sampling_provider_fallback=False,
                max_attempts=1,
            )
            payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
            if not payload or result.get("fallback_used"):
                raise ValueError(str(result.get("fallback_reason") or "empty Anna sampling response"))
            payload.setdefault("candidate_id", ctx.candidate.candidate_id)
            return apply_rule_guards(_parse_compact_batch_item(payload, strategy), strategy)
        except Exception as exc:
            return create_fallback_judgment(ctx.candidate.candidate_id, strategy, f"evaluation failed: {type(exc).__name__}: {exc}")

    _brief_update_state(run_id, stage="evaluate", progress={"evaluated": cursor, "total": total, "batch": len(batch), "sampling_calls_used": len(batch)})
    judgments = await asyncio.gather(*[_evaluate_one(cursor + offset + 1, ctx) for offset, ctx in enumerate(contexts)])
    all_judgments = _brief_judgments_from_dict(list(brief.get("judgments") or []))
    all_judgments.extend(judgments)
    brief["judgments"] = _brief_to_dict_list(all_judgments)
    brief["phase2_cursor"] = min(total, cursor + len(judgments))
    cards_added = await _brief_persist_cards(run_id, judgments)
    if brief["phase2_cursor"] >= total:
        brief["stage"] = "finalizing"
    _brief_update_state(
        run_id,
        stage="evaluate_done" if brief["stage"] != "finalizing" else "finalizing",
        progress={"evaluated": brief["phase2_cursor"], "total": total, "cards_added": cards_added, "cards_version": int(brief.get("cards_version") or 0)},
        cards_added=cards_added,
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
        await set_cleanup_bundle(mailbox, cleanup_full)
        if cleanup_card is not None:
            cleanup_card.bundled_count = len(cleanup_full)
            active = await get_active_cards(mailbox)
            await set_active_cards(mailbox, merge_cards(active, [cleanup_card]))
            brief["cards_version"] = int(brief.get("cards_version") or 0) + 1

    previous_state = await get_scan_state(mailbox)
    latest_internal_date = max((str(m.internal_date) for m in messages if m.internal_date), default=previous_state.last_message_internal_date, key=lambda value: int(value or 0))
    await set_scan_state(mailbox, ScanState(
        mailbox=mailbox,
        last_scan_ts=_now(),
        last_message_internal_date=latest_internal_date,
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


def _run_storage_query(coro: Any, timeout: float = 60.0) -> Any:
    if _active_storage_provider != "aps":
        # Local storage: run directly on the calling thread to avoid
        # depending on the main event loop (which may be dead on some
        # Anna harness versions after a background coroutine completes).
        new_loop = asyncio.new_event_loop()
        try:
            return new_loop.run_until_complete(coro)
        finally:
            new_loop.close()
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


def _start_contact_memory_run(arguments: dict[str, Any]) -> dict[str, Any]:
    run_id = str(arguments.get("run_id") or "").strip()
    if not run_id or len(run_id) < 8:
        run_id = f"cm_{uuid.uuid4().hex[:12]}"
    MAIL_AGENT_RUNS[run_id] = {
        "run_id": run_id,
        "status": "queued",
        "stage": "contact_memory_prepare",
        "progress": {},
        "warnings": [],
        "started_at": beijing_now(),
        "updated_at": beijing_now(),
        "result": None,
        "error": "",
        "partial": {"_args": dict(arguments), "contact_memory": {"prepared": False, "items": [], "cursor": 0, "backfilled": 0, "skipped_old": 0, "failed": 0}},
        "needs_continue": True,
    }
    _save_run_checkpoint(run_id)
    return _public_run_view(MAIL_AGENT_RUNS[run_id])


def _parse_contact_memory_dt(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _contact_memory_before(a: datetime, b: datetime) -> bool:
    if a.tzinfo is None and b.tzinfo is not None:
        a = a.replace(tzinfo=b.tzinfo)
    elif a.tzinfo is not None and b.tzinfo is None:
        b = b.replace(tzinfo=a.tzinfo)
    return a < b


async def _prepare_contact_memory_run(run_id: str, arguments: dict[str, Any]) -> None:
    from mail_agent.contact_memory.indexer import parse_contact
    from mail_agent.storage.ops import get_active_cards

    state = MAIL_AGENT_RUNS[run_id]
    contact_state = state.setdefault("partial", {}).setdefault("contact_memory", {})
    since_dt = _parse_contact_memory_dt(str(arguments.get("since") or ""))
    items: list[dict[str, Any]] = []
    skipped_old = 0
    for mailbox in _memory_mailboxes(arguments):
        active = await get_active_cards(mailbox)
        for card in active.cards:
            if getattr(card, "card_type", "") == "cleanup_bundle":
                continue
            card_dt = _parse_contact_memory_dt(getattr(card, "created_at", "") or getattr(card, "updated_at", ""))
            if since_dt and card_dt and _contact_memory_before(card_dt, since_dt):
                skipped_old += 1
                continue
            contact_email, _ = parse_contact(getattr(card.original, "from_addr", ""))
            thread_id = getattr(card, "thread_id", "") or getattr(card, "message_id", "")
            card_id = getattr(card, "card_id", "")
            if not contact_email or not thread_id or not card_id:
                continue
            items.append({"mailbox": mailbox, "card_id": card_id, "thread_id": thread_id, "contact_email": contact_email})
    contact_state.update({
        "prepared": True,
        "items": items,
        "cursor": 0,
        "backfilled": 0,
        "skipped_old": skipped_old,
        "failed": 0,
    })
    if not items:
        state.update({
            "status": "done",
            "stage": "done",
            "needs_continue": False,
            "progress": {"current": 0, "total": 0, "skipped_old": skipped_old},
            "result": {"ok": True, "backfilled": 0, "skipped_old": skipped_old, "failed": 0},
            "updated_at": beijing_now(),
        })
    else:
        state.update({
            "status": "running",
            "stage": "contact_memory",
            "needs_continue": True,
            "progress": {"current": 0, "total": len(items), "skipped_old": skipped_old},
            "updated_at": beijing_now(),
        })
    _save_run_checkpoint(run_id)


async def _backfill_contact_memory_target(item: dict[str, Any], sampling_create_message: Any) -> None:
    from mail_agent.contact_memory.indexer import ingest_card_event
    from mail_agent.storage.ops import get_active_cards

    mailbox = str(item.get("mailbox") or "")
    card_id = str(item.get("card_id") or "")
    active = await get_active_cards(mailbox)
    card = next((card for card in active.cards if getattr(card, "card_id", "") == card_id), None)
    if card is None:
        raise ValueError(f"Card not found for contact memory backfill: {card_id}")
    await ingest_card_event(
        mailbox,
        card,
        event_type="card_created",
        source="brief_card_background",
        user_action="backfill",
        sampling_create_message=sampling_create_message,
    )


async def _continue_contact_memory_run_async(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    run_id = str(arguments.get("run_id") or "")
    state = _get_run_state(run_id)
    if not state:
        return {"success": False, "run_id": run_id, "error": "run not found"}
    saved_args = (state.get("partial") or {}).get("_args") or {}
    saved_args.update({key: value for key, value in arguments.items() if key != "run_id" and value not in (None, "")})
    state.setdefault("partial", {})["_args"] = saved_args
    _apply_storage_provider(saved_args)
    if state.get("status") == "done":
        state["needs_continue"] = False
        return _public_run_view(state)
    try:
        contact_state = state.setdefault("partial", {}).setdefault("contact_memory", {})
        if not contact_state.get("prepared"):
            await _prepare_contact_memory_run(run_id, saved_args)
            state = MAIL_AGENT_RUNS[run_id]
            contact_state = state.setdefault("partial", {}).setdefault("contact_memory", {})
            if state.get("status") == "done":
                return _public_run_view(state)

        ai_provider = str(saved_args.get("ai_provider", "anna-llm") or "anna-llm")
        sampling_fn = _build_sampling_for_run({"ai_provider": ai_provider}, invoke_id)
        items = list(contact_state.get("items") or [])
        cursor = int(contact_state.get("cursor") or 0)
        batch_limit = max(1, min(int(saved_args.get("batch_limit") or 1), 2))
        processed = 0
        while cursor < len(items) and processed < batch_limit:
            state.update({
                "status": "running",
                "stage": "contact_memory",
                "needs_continue": True,
                "progress": {
                    "current": cursor,
                    "total": len(items),
                    "backfilled": int(contact_state.get("backfilled") or 0),
                    "failed": int(contact_state.get("failed") or 0),
                    "skipped_old": int(contact_state.get("skipped_old") or 0),
                },
                "updated_at": beijing_now(),
            })
            _save_run_checkpoint(run_id)
            item = items[cursor]
            try:
                await _backfill_contact_memory_target(item, sampling_fn)
                contact_state["backfilled"] = int(contact_state.get("backfilled") or 0) + 1
            except Exception as exc:
                contact_state["failed"] = int(contact_state.get("failed") or 0) + 1
                warnings = state.setdefault("warnings", [])
                warnings.append({"stage": "contact_memory_error", "at": beijing_now(), "detail": {"card_id": item.get("card_id", ""), "error": str(exc)[:240]}})
                state["warnings"] = warnings[-10:]
            cursor += 1
            processed += 1
            contact_state["cursor"] = cursor

        if cursor >= len(items):
            result = {
                "ok": True,
                "backfilled": int(contact_state.get("backfilled") or 0),
                "skipped_old": int(contact_state.get("skipped_old") or 0),
                "failed": int(contact_state.get("failed") or 0),
                "total": len(items),
            }
            state.update({
                "status": "done",
                "stage": "done",
                "needs_continue": False,
                "progress": {"current": len(items), "total": len(items), **result},
                "result": result,
                "updated_at": beijing_now(),
            })
        else:
            state.update({
                "status": "running",
                "stage": "contact_memory",
                "needs_continue": True,
                "progress": {
                    "current": cursor,
                    "total": len(items),
                    "backfilled": int(contact_state.get("backfilled") or 0),
                    "failed": int(contact_state.get("failed") or 0),
                    "skipped_old": int(contact_state.get("skipped_old") or 0),
                },
                "updated_at": beijing_now(),
            })
        _save_run_checkpoint(run_id)
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
    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", 50))

    from mail_agent.cards.service import cards_to_frontend
    from mail_agent.storage.ops import get_active_cards_page, get_scan_state
    from mail_agent.storage.types import ActiveCards as ActiveCardsType

    try:
        if mailbox.lower() == "all":
            from mail_agent.storage.ops import get_mailbox_registry

            async def _load_all_page() -> dict[str, Any]:
                registry = await get_mailbox_registry()
                entries = list(getattr(registry, "mailboxes", []) or [])
                selected_entries = [entry for entry in entries if getattr(entry, "selected", False)]
                scoped_entries = selected_entries or entries
                mailboxes = [
                    str(getattr(entry, "email", "") or "").strip().lower()
                    for entry in scoped_entries
                ]
                mailboxes = sorted(dict.fromkeys(item for item in mailboxes if item))
                page_limit = max(offset + limit, limit, 50) if limit > 0 else 50
                page_results = await asyncio.gather(
                    *(get_active_cards_page(item, 0, page_limit) for item in mailboxes),
                    return_exceptions=True,
                )
                all_cards: list[Any] = []
                total_cards = 0
                latest_updated = ""
                for result in page_results:
                    if isinstance(result, Exception) or not isinstance(result, dict):
                        continue
                    total_cards += int(result.get("total") or 0)
                    active_page = result.get("active")
                    latest_updated = max(latest_updated, getattr(active_page, "updated_at", "") or "")
                    for card in getattr(active_page, "cards", []) or []:
                        all_cards.append(card)
                priority_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
                all_cards.sort(key=lambda c: (priority_rank.get(str(getattr(c, "priority", "") or "").lower(), 0), getattr(c, "created_at", "") or ""), reverse=True)
                page_cards = all_cards[offset:offset + limit] if limit > 0 else all_cards
                state_results = await asyncio.gather(
                    *(get_scan_state(item) for item in mailboxes),
                    return_exceptions=True,
                )
                valid_states = [item for item in state_results if not isinstance(item, Exception)]
                return {
                    "active": ActiveCardsType(cards=list(page_cards), updated_at=latest_updated),
                    "total": total_cards,
                    "scan_state": {
                        "last_scan_ts": max((getattr(s, "last_scan_ts", "") for s in valid_states), default=""),
                        "last_message_internal_date": "",
                        "total_scans": max((getattr(s, "total_scans", 0) for s in valid_states), default=0),
                        "total_processed": max((getattr(s, "total_processed", 0) for s in valid_states), default=0),
                    },
                }

            loaded = _run_storage_query(_load_all_page(), timeout=45.0)
            active = loaded["active"]
            total = int(loaded["total"])
            scan_state = loaded["scan_state"]
            action_count = sum(1 for c in active.cards if c.status not in ("resolved", "dismissed") and c.user_action in ("reply", "review"))
        else:
            page_result = _run_storage_query(get_active_cards_page(mailbox, offset, limit), timeout=45.0)
            active = page_result["active"]
            total = page_result["total"]
            state = _run_storage_query(get_scan_state(mailbox), timeout=45.0)
            scan_state = {
                "last_scan_ts": getattr(state, "last_scan_ts", ""),
                "last_message_internal_date": getattr(state, "last_message_internal_date", ""),
                "total_scans": getattr(state, "total_scans", 0),
                "total_processed": getattr(state, "total_processed", 0),
            }
            action_count = 0  # computed below from active page for consistency; full count would need all cards
            action_count = sum(1 for c in active.cards if c.status not in ("resolved", "dismissed") and c.user_action in ("reply", "review"))

        cleanup_bundle = None
        cleanup_total = 0
        cleanup_has_more = False

        return {
            "cards": cards_to_frontend(active),
            "total": total,
            "count": len(active.cards),
            "has_more": (offset + limit) < total if limit > 0 else False,
            "offset": offset,
            "limit": limit,
            "action_count": action_count,
            "scan_state": scan_state,
            "cleanup_bundle": cleanup_bundle,
            "cleanup_total": cleanup_total,
            "cleanup_has_more": cleanup_has_more,
        }
    except Exception as exc:
        log(f"get_active_cards sync entry failed: {type(exc).__name__}: {exc}")
        return {
            "cards": [],
            "total": 0,
            "count": 0,
            "has_more": False,
            "cleanup_bundle": None,
            "cleanup_total": 0,
            "cleanup_has_more": False,
            "action_count": 0,
            "scan_state": {"total_scans": 0, "total_processed": 0, "last_scan_ts": "", "last_message_internal_date": ""},
            "error": f"{type(exc).__name__}: {exc}"[:200],
        }


def _sync_get_cleanup_bundle_page(arguments: dict[str, Any]) -> dict[str, Any]:
    mailbox = str(arguments.get("mailbox", "")).strip()
    if not mailbox:
        return {"error": "mailbox is required"}
    offset = max(0, int(arguments.get("offset", 0)))
    limit = max(1, min(int(arguments.get("limit", 100)), 100))
    from mail_agent.storage.ops import get_cleanup_bundle_page, get_mailbox_registry

    try:
        if mailbox.lower() == "all":
            async def _load_cleanup_all() -> dict[str, Any]:
                registry = await get_mailbox_registry()
                entries = list(getattr(registry, "mailboxes", []) or [])
                selected_entries = [entry for entry in entries if getattr(entry, "selected", False)]
                scoped_entries = selected_entries or entries
                mailboxes = [
                    str(getattr(entry, "email", "") or "").strip().lower()
                    for entry in scoped_entries
                ]
                mailboxes = sorted(dict.fromkeys(item for item in mailboxes if item))
                results = await asyncio.gather(
                    *(get_cleanup_bundle_page(item, offset, limit) for item in mailboxes),
                    return_exceptions=True,
                )
                items: list[dict[str, Any]] = []
                total_items = 0
                for result in results:
                    if isinstance(result, Exception) or not isinstance(result, dict):
                        continue
                    items.extend(result.get("items") or [])
                    total_items += int(result.get("total") or 0)
                return {"items": items[:limit], "total": total_items}

            page_result = _run_storage_query(_load_cleanup_all(), timeout=45.0)
        else:
            page_result = _run_storage_query(get_cleanup_bundle_page(mailbox, offset, limit), timeout=45.0)

        total = int(page_result.get("total") or 0)
        items = list(page_result.get("items") or [])
        return {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < total,
        }
    except Exception as exc:
        log(f"get_cleanup_bundle_page failed: {type(exc).__name__}: {exc}")
        return {"items": [], "total": 0, "offset": offset, "limit": limit, "has_more": False, "error": str(exc)}


def _sync_get_run_history() -> dict[str, Any]:
    from mail_agent.storage.ops import get_run_history

    history = _run_storage_query(get_run_history(limit=20))
    return {"history": [serialize_value(entry) for entry in history]}


def get_mail_agent_run(run_id_arg: str) -> dict[str, Any]:
    run_id = str(run_id_arg or "")
    state = _get_run_state(run_id)
    if not state:
        return {"success": False, "error": "run not found", "run_id": run_id}
    status = state.get("status")
    result = _compact_run_result(state.get("result"))
    cards = None
    scan_state = None
    if status == "done":
        full_result = state.get("result")
        if isinstance(full_result, dict):
            cards = full_result.get("cards")
            # Provide scan_state from the pipeline result so the frontend
            # shows category tabs immediately without needing loadActiveCards.
            scan_state = full_result.get("scan_state")
            if not isinstance(scan_state, dict):
                scan_state = {"total_scans": 1, "total_processed": 0}
    return {
        "success": True,
        "run_id": run_id,
        "status": status,
        "stage": state.get("stage") or "",
        "progress": state.get("progress") or {},
        "partial": state.get("partial") or {},
        "warnings": state.get("warnings") or [],
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "error": state.get("error") or "",
        "needs_continue": bool(state.get("needs_continue")),
        "cards_added": int(state.get("cards_added") or 0),
        "cards_version": int((state.get("brief") or {}).get("cards_version") or state.get("cards_version") or 0),
        "result": result,
        "cards": cards,
        "scan_state": scan_state,
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
        set_scan_plan,
        update_card_status,
        add_snooze_sender,
        add_snooze_thread,
        append_learning,
        get_run_history,
    )
    from mail_agent.actions.service import (
        _fetch_thread_context_sync,
        summarize_thread,
        generate_draft_reply,
        reply_now,
    )
    from mail_agent.storage.types import PersistentCard

    mailbox = str(arguments.get("mailbox", "")).strip()
    card_id = str(arguments.get("card_id", "")).strip()

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
        latest_body_html = ""
        try:
            from mail_agent.mail_providers.gmail.adapter import normalize_mailbox, _decode_body, read_message
            msg = read_message(normalize_mailbox(mailbox), card.message_id)
            if isinstance(msg, dict):
                raw = _decode_body(msg)
                if not raw.strip():
                    raw = str(msg.get("body_text") or "")
                    raw = _dedup_body(raw)  # old body_text may have duplicated parts
                raw = raw[:8000]

                # Build sanitized HTML for frontend display (before tag stripping)
                if raw.strip():
                    latest_body_html = _sanitize_email_html(raw)
                    # Resolve cid: inline images to data URIs
                    payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
                    latest_body_html = _resolve_cid_images(latest_body_html, payload)

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
                if raw:
                    latest_body = raw
        except Exception:
            pass

        # When re-decode couldn't produce a body (failed, or the message
        # has no text parts), card.original.body still holds the old
        # value set by decode_gmail_body which joined text/plain and
        # text/html parts — dedup adjacent identical segments.
        if not latest_body_html and latest_body:
            latest_body = _dedup_body(latest_body)

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
        # Load full cleanup bundle if this is a cleanup card
        if card.card_type == "cleanup_bundle" and card.bundled_count > 0:
            from mail_agent.storage.ops import get_cleanup_bundle
            try:
                full_bundled = await get_cleanup_bundle(mailbox)
                if full_bundled:
                    card.bundled_messages = full_bundled
            except Exception:
                pass
        return {
            "card": _serialize_card_for_frontend(card),
            "thread_context": thread_ctx,
            "contact_context": contact_ctx,
            "latest_body": latest_body,
            "latest_body_html": latest_body_html,
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
        if card and card.card_type == "cleanup_bundle" and card.bundled_count > 0:
            try:
                from mail_agent.storage.ops import get_cleanup_bundle
                full_bundled = await get_cleanup_bundle(mailbox)
                bundled = full_bundled if full_bundled else card.bundled_messages
                import json as _json3
                import urllib.request as _ur2
                from mail_agent.mail_providers.gmail.adapter import get_access_token as _gt2
                msg_ids = [str(m.get("message_id", "")) for m in bundled if str(m.get("message_id", ""))]
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
    if tool == "list_cached_emails":
        return {"success": True, "tool": tool, "data": list_cached_emails(arguments.get("mailbox", ""))}
    if tool == "get_cached_email":
        return {"success": True, "tool": tool, "data": get_cached_email(arguments.get("mailbox", ""), arguments.get("message_id", ""))}
    if tool == "check_gmail_auth":
        return {"success": True, "tool": tool, "data": _check_gmail_auth(arguments.get("mailbox", ""))}
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
