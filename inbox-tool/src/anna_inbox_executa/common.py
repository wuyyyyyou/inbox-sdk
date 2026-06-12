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
            "name": "check_sampling_status",
            "description": "Lightweight Anna LLM sampling connectivity check.",
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

def serialize_value(obj: Any) -> Any:
    from dataclasses import asdict

    if hasattr(obj, "__dataclass_fields__"):
        return {key: serialize_value(value) for key, value in asdict(obj).items()}
    if isinstance(obj, list):
        return [serialize_value(item) for item in obj]
    if isinstance(obj, dict):
        return {key: serialize_value(value) for key, value in obj.items()}
    return obj

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


def dispatch_storage_response(message: dict[str, Any]) -> bool:
    return _route_storage_response(message)

__all__ = [name for name in globals() if not name.startswith("__")]
