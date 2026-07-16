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
# explicit reconfigure keeps diagnostic writes and stdin/stdout stable.
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

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
from executa_sdk.credentials import CredentialsClient, CredentialsError
from executa_sdk.storage import StorageClient, FilesClient, StorageError, make_response_router
from executa_sdk.host_upload import HostUploadClient
from mail_agent.storage.keys import app_key

JSONRPC_VERSION = "2.0"
DEFAULT_TOOL_ID = "inbox-tool"
DEFAULT_VERSION = "0.2.0"
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
STDOUT_LOCK = threading.Lock()
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
TOKEN_URI = "https://oauth2.googleapis.com/token"
MAX_STDIO_MESSAGE_BYTES = 512 * 1024
# Anna Desktop 的 tools.invoke 宿主在约 64 KiB 的单帧附近会直接结束子进程，
# 而不是返回可捕获的 JSON-RPC 错误。预留协议包装、请求 ID 和运行时余量后，
# Inbox 详情统一以 48 KiB 为上限；大正文必须走 loopback / 文件通道。
MAX_INBOX_THREAD_RESPONSE_BYTES = 48 * 1024
# 前端与 manifest 为连通性检测保留 15 秒；后端在 12 秒内结束，给 stdio 排队和响应写回预留余量。
CONNECTIVITY_CHECK_TIMEOUT_SECONDS = 12.0
CONNECTIVITY_GMAIL_ACCOUNT_TIMEOUT_SECONDS = 3.0

DEFAULT_MANIFEST = {
    "name": DEFAULT_TOOL_ID,
    "display_name": "Zhaopy Mail Agent RD6B87R5",
    "version": DEFAULT_VERSION,
    "description": "Minimal Anna Executa skeleton for reading Gmail through local token files and DashScope or Anna sampling LLM.",
    "author": "Zhaopy",
    "host_capabilities": [
        "llm.sample",
        "llm.complete",
        "host.upload",
        "upload.inline",
        "upload.negotiate",
        "upload.confirm",
        "storage.user",
        "aps.kv",
        "aps.scope.user.read",
        "aps.scope.user.write",
    ],
    "credentials": [
        {
            "name": "GOOGLE_ACCESS_TOKEN",
            "display_name": "Google Connected Accounts",
            "description": "Enables Anna's Google multi-account credentials API. Accounts and short-lived tokens are requested on demand; do not paste a token here.",
            "required": False,
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
    ],
    "tools": [
        {
            "name": "check_google_oauth",
            "description": "Check Anna Google connected-account availability through the credentials Reverse RPC without exposing tokens.",
            "parameters": [],
        },
        {
            "name": "check_sampling_status",
            "description": "Lightweight Anna LLM sampling connectivity check.",
            "parameters": [],
        },
        {
            "name": "check_gmail_api_status",
            "description": "Lightweight Gmail API connectivity and latency check via users/me/profile.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address. Empty uses any authorized mailbox.", "required": False},
            ],
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
            "name": "list_inbox_emails",
            "description": "Refresh All-mail Gmail metadata into the local cache and return a compact snapshot. Category is diagnostic only; classification is done from the All-mail cache.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email.", "required": True},
                {"name": "days", "type": "integer", "description": "Recent day window, from 1 to 30.", "required": False},
                {"name": "limit", "type": "integer", "description": "Maximum All-mail messages to fetch into cache, from 1 to 500.", "required": False},
                {"name": "category", "type": "string", "description": "Diagnostic category label only. Fetch always uses All mail.", "required": False},
                {"name": "clear_cache", "type": "boolean", "description": "Clear this mailbox's Gmail cache before rebuilding the All-mail snapshot.", "required": False},
            ],
        },
        {
            "name": "list_cached_emails",
            "description": "List compact messages from the local All-mail cache, optionally projected by category labels.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email.", "required": True},
                {"name": "days", "type": "integer", "description": "Recent day window to filter cached mail, from 1 to 30.", "required": False},
                {"name": "limit", "type": "integer", "description": "Maximum cached messages to return per page, from 1 to 100.", "required": False},
                {"name": "category", "type": "string", "description": "Project All-mail cache by labels: inbox, todos, starred, snoozed, done, drafts, sent, trash, spam, or all.", "required": False},
                {"name": "offset", "type": "integer", "description": "Zero-based offset within the filtered cached messages.", "required": False},
            ],
        },
        {
            "name": "get_cached_email",
            "description": "Read one bounded Gmail text body by id, fetching and caching it when the body is not cached yet.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email.", "required": True},
                {"name": "message_id", "type": "string", "description": "Gmail message id.", "required": True},
            ],
        },
        {
            "name": "check_gmail_auth",
            "description": "Check whether a Gmail mailbox is authorized. Platform: checks Anna connected-account metadata; local: checks token-file availability.",
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
            "description": "Discover Anna connected Google accounts and list registered mailboxes, including selected and authorization state.",
            "parameters": [],
        },
        {
            "name": "get_mailbox_registry",
            "description": "Read the persisted mailbox registry without triggering Gmail or avatar discovery.",
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
            "name": "get_inbox_settings",
            "description": "Get mailbox-scoped Inbox display settings.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
            ],
        },
        {
            "name": "save_inbox_settings",
            "description": "Save mailbox-scoped Inbox display settings using optimistic concurrency.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "if_match", "type": "string", "description": "Current settings etag.", "required": False},
                {"name": "display_range_days", "type": "integer", "description": "7, 30, or 60.", "required": False},
                {"name": "time_section_mode", "type": "string", "description": "detailed, recent_then_months, or months_only.", "required": False},
                {"name": "stars_enabled", "type": "boolean", "description": "Show Stars section.", "required": False},
                {"name": "stars_limit", "type": "integer", "description": "Starred thread limit.", "required": False},
                {"name": "todos_enabled", "type": "boolean", "description": "Show Todos section.", "required": False},
                {"name": "todos_limit", "type": "integer", "description": "Todo thread limit.", "required": False},
                {"name": "llm_status_poll_seconds", "type": "integer", "description": "LLM connectivity poll interval in seconds: 0, 30, 60, 120, or 300.", "required": False},
                {"name": "initial_list_size", "type": "integer", "description": "Initial list thread count: 100, 200, or 400.", "required": False},
                {"name": "custom_categories", "type": "array", "description": "Saved local Inbox Splits with id, name, query, hide_when_empty, and bundling_behavior.", "required": False},
            ],
        },
        {
            "name": "get_card_detail",
            "description": "Get a single card's detail including thread context. Set include_body=true only when the user explicitly asks to view the original email body.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID from get_active_cards.", "required": True},
                {"name": "include_body", "type": "boolean", "description": "Whether to include the original email body for user display.", "required": False},
            ],
        },
        {
            "name": "list_gmail_emails_page",
            "description": "Load one All-mail Gmail page, merge summaries into the local All-mail cache, and return compact messages.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email.", "required": True},
                {"name": "days", "type": "integer", "description": "Recent day window, from 1 to 30.", "required": False},
                {"name": "limit", "type": "integer", "description": "Maximum messages to return, from 1 to 100.", "required": False},
                {"name": "category", "type": "string", "description": "Diagnostic category label only. Fetch always uses All mail.", "required": False},
                {"name": "page_token", "type": "string", "description": "Opaque Gmail page token returned by the previous page.", "required": False},
                {"name": "page_offset", "type": "integer", "description": "Offset within the current Gmail page after response byte limiting.", "required": False},
                {"name": "exclude_message_ids", "type": "array", "description": "Already rendered message IDs to skip.", "required": False},
            ],
        },
        {
            "name": "get_inbox_thread_page",
            "description": "Get one page of an Inbox Gmail thread with bounded display bodies and attachment metadata.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "thread_id", "type": "string", "description": "Gmail thread ID.", "required": True},
                {"name": "anchor_message_id", "type": "string", "description": "Message initially opened from the Inbox list.", "required": False},
                {"name": "before_index", "type": "integer", "description": "Exclusive thread index boundary for older-page pagination.", "required": False},
                {"name": "limit", "type": "integer", "description": "Maximum messages to return in this page. Defaults to 5.", "required": False},
                {"name": "include_display_body", "type": "boolean", "description": "Whether to include bounded display HTML/text for each message.", "required": False},
            ],
        },
        {
            "name": "get_inbox_message_display_body",
            "description": "Get the full display body for one Inbox message when the thread page returned a truncated body.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "message_id", "type": "string", "description": "Gmail message ID.", "required": True},
            ],
        },
        {
            "name": "resolve_contact_avatars",
            "description": "Resolve Google Contact avatar URLs for email addresses, falling back to Gravatar. Returns permission_required when Contacts scope is unavailable.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email.", "required": True},
                {"name": "emails", "type": "array", "description": "Contact email addresses.", "required": True},
            ],
        },
        {
            "name": "prepare_inbox_attachment_access",
            "description": "Prepare short-lived preview or download access for an Inbox attachment.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "message_id", "type": "string", "description": "Gmail message ID owning the attachment.", "required": True},
                {"name": "attachment_id", "type": "string", "description": "Opaque attachment token from Inbox thread DTO.", "required": True},
                {"name": "mode", "type": "string", "description": "preview or download.", "required": False},
            ],
        },
        {
            "name": "prepare_attachment_download",
            "description": "Prepare a short-lived download URL for a Gmail attachment behind a card.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID from get_active_cards.", "required": True},
                {"name": "attachment_id", "type": "string", "description": "Opaque attachment ID from get_card_detail.", "required": True},
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
            "name": "start_inbox_thread_assist",
            "description": "Start AI overview generation for an Inbox thread. Returns run_id immediately on cache miss; poll with get_mail_agent_run.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "thread_id", "type": "string", "description": "Gmail thread ID.", "required": True},
                {"name": "latest_message_id", "type": "string", "description": "Latest message ID used for cache invalidation.", "required": True},
                {"name": "anchor_message_id", "type": "string", "description": "Message initially opened from the Inbox list.", "required": False},
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
            "name": "start_inbox_mail_prompt",
            "description": "Start a mail-context AI prompt from the left Anna sidebar and optionally return a draft-reply artifact.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "thread_id", "type": "string", "description": "Gmail thread ID.", "required": True},
                {"name": "anchor_message_id", "type": "string", "description": "Message initially opened from the Inbox list.", "required": True},
                {"name": "latest_message_id", "type": "string", "description": "Latest thread message ID.", "required": True},
                {"name": "visible_prompt", "type": "string", "description": "Prompt visible in the left Anna sidebar.", "required": True},
                {"name": "run_id", "type": "string", "description": "Client-generated ID used to safely retry the same AI task.", "required": False},
                {"name": "expected_artifact", "type": "string", "description": "Expected artifact type: draft_reply or summary.", "required": False},
                {"name": "user_answers", "type": "object", "description": "Optional answers to reply-gap questions.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider.", "required": False},
            ],
        },
        {
            "name": "start_compose_mail_prompt",
            "description": "Start a read-only Compose-context AI prompt from the Anna sidebar. It may return a reviewable Compose draft artifact but never saves or sends email.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "draft", "type": "object", "description": "Current Compose snapshot: recipients, subject, and body.", "required": True},
                {"name": "visible_prompt", "type": "string", "description": "Prompt visible in the Anna sidebar.", "required": True},
                {"name": "run_id", "type": "string", "description": "Client-generated ID used to safely retry the same AI task.", "required": False},
                {"name": "expected_artifact", "type": "string", "description": "Expected artifact type: compose_draft. Feedback requests for non-empty drafts remain analysis-only.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider.", "required": False},
            ],
        },
        {
            "name": "get_inbox_thread_draft",
            "description": "Read a persisted Inbox thread draft from mailbox-scoped storage.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "thread_id", "type": "string", "description": "Gmail thread ID.", "required": True},
            ],
        },
        {
            "name": "save_inbox_thread_draft",
            "description": "Persist an Inbox thread draft body to mailbox-scoped storage.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "thread_id", "type": "string", "description": "Gmail thread ID.", "required": True},
                {"name": "body", "type": "string", "description": "Draft body text.", "required": True},
                {"name": "if_match", "type": "string", "description": "Optional etag for optimistic concurrency.", "required": False},
                {"name": "message", "type": "object", "description": "Optional compact source message metadata for the local Drafts folder.", "required": False},
            ],
        },
        {
            "name": "list_inbox_thread_drafts",
            "description": "List locally persisted Inbox thread drafts for the Anna Drafts folder.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "limit", "type": "integer", "description": "Maximum drafts to return.", "required": False},
            ],
        },
        {
            "name": "delete_inbox_thread_draft",
            "description": "Delete a persisted Inbox thread draft from mailbox-scoped storage.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "thread_id", "type": "string", "description": "Gmail thread ID.", "required": True},
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
            "name": "mark_card_read",
            "description": "Mark a review card's Gmail message as read (remove UNREAD label) and resolve the card locally as read.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
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
            "name": "reset_mailbox_scan_history",
            "description": "Clear Brief scan history for one mailbox — active cards, processed index, scan state, run records, and history entries. Keeps auth, cache, contact memory, scan plan, and mailbox registration.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "The mailbox email address whose Brief scan history should be reset.", "required": True},
            ],
        },
        {
            "name": "delete_mailbox_data",
            "description": "Delete all persistent data for a single mailbox — cards, processed-message index, scan state, run records, Gmail cache, contact memories, and registry entry. Other mailboxes are not affected.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "The mailbox email address to delete all data for.", "required": True},
            ],
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
            "name": "modify_message_labels",
            "description": "Modify a bounded allowlist of Gmail system labels for Inbox detail actions.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "message_ids", "type": "array", "description": "Gmail message IDs to modify.", "required": True},
                {"name": "add_label_ids", "type": "array", "description": "System label IDs to add. Allowlist: UNREAD, IMPORTANT, STARRED, INBOX, TRASH.", "required": False},
                {"name": "remove_label_ids", "type": "array", "description": "System label IDs to remove. Allowlist: UNREAD, IMPORTANT, STARRED, INBOX, TRASH.", "required": False},
            ],
        },
        {
            "name": "set_message_starred",
            "description": "Add or remove Gmail's STARRED label for one message.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "message_id", "type": "string", "description": "Gmail message ID.", "required": True},
                {"name": "starred", "type": "boolean", "description": "Whether the message should be starred.", "required": True},
            ],
        },
        {
            "name": "update_inbox_thread_state",
            "description": "Apply a guarded Gmail read, star, importance, or trash transition to an entire thread.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "thread_id", "type": "string", "description": "Gmail thread ID.", "required": True},
                {"name": "operation", "type": "string", "description": "mark_read | mark_unread | star | unstar | mark_important | mark_not_important | trash | untrash.", "required": True},
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
                {"name": "scan_window_days", "type": "integer", "description": "Default Scan Plan day range when the request has no explicit time.", "required": False},
                {"name": "primary_count", "type": "integer", "description": "How many recent Primary emails to fetch first.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider: dashscope or anna-llm.", "required": False},
                {"name": "storage_provider", "type": "string", "description": "Storage provider: local or aps.", "required": False},
                {"name": "wait_timeout_seconds", "type": "integer", "description": "How long this invoke should wait before returning a pollable running state.", "required": False},
            ],
            "timeout": 600,
        },
        {
            "name": "start_ai_turn",
            "description": "Unified AI sidebar turn: local router selects whitelist tools (chat, search, summarize, draft, propose, memory) then returns a pollable run.",
            "parameters": [
                {"name": "user_text", "type": "string", "description": "Natural language user message.", "required": True},
                {"name": "mailbox", "type": "string", "description": "Primary mailbox email address.", "required": False},
                {"name": "ui_context", "type": "object", "description": "Read-only screen context: current thread, last_draft, selected mailboxes, display range, etc.", "required": False},
                {"name": "conversation_id", "type": "string", "description": "Ephemeral sidebar conversation id for multi-turn registry (process-local).", "required": False},
                {"name": "run_id", "type": "string", "description": "Client-generated run ID for polling.", "required": False},
                {"name": "max_messages", "type": "integer", "description": "Max messages for inbox search tools.", "required": False},
                {"name": "scan_window_days", "type": "integer", "description": "Default day range for inbox search.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider: dashscope or anna-llm.", "required": False},
                {"name": "storage_provider", "type": "string", "description": "Storage provider: local or aps.", "required": False},
                {"name": "wait_timeout_seconds", "type": "integer", "description": "How long this invoke should wait before returning a pollable running state.", "required": False},
            ],
            "timeout": 600,
        },
        {
            "name": "apply_proposed_actions",
            "description": "Apply user-confirmed inbox organize actions (mark_done/archive/trash). Never call without explicit UI confirmation.",
            "parameters": [
                {"name": "action", "type": "string", "description": "mark_done | archive | trash", "required": True},
                {"name": "items", "type": "array", "description": "Selected items: mailbox, message_id, thread_id.", "required": True},
            ],
        },
        {
            "name": "list_saved_prompts",
            "description": "List AI sidebar saved prompts.",
            "parameters": [],
        },
        {
            "name": "save_saved_prompt",
            "description": "Create or update a saved prompt.",
            "parameters": [
                {"name": "prompt_id", "type": "string", "description": "Existing id to update; omit to create.", "required": False},
                {"name": "title", "type": "string", "description": "Short title.", "required": False},
                {"name": "body", "type": "string", "description": "Prompt body text.", "required": True},
            ],
        },
        {
            "name": "delete_saved_prompt",
            "description": "Delete a saved prompt by id.",
            "parameters": [
                {"name": "prompt_id", "type": "string", "description": "Prompt id.", "required": True},
            ],
        },
        {
            "name": "list_ai_memories",
            "description": "List AI personalization memory preference lines.",
            "parameters": [],
        },
        {
            "name": "add_ai_memory",
            "description": "Add an AI memory preference (short behavior note, no email body).",
            "parameters": [
                {"name": "text", "type": "string", "description": "Preference text.", "required": True},
                {"name": "source", "type": "string", "description": "chat | settings", "required": False},
            ],
        },
        {
            "name": "delete_ai_memory",
            "description": "Delete one AI memory entry.",
            "parameters": [
                {"name": "memory_id", "type": "string", "description": "Memory id.", "required": True},
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
                {"name": "scan_window_days", "type": "integer", "description": "Default Scan Plan day range when the request has no explicit time.", "required": False},
                {"name": "primary_count", "type": "integer", "description": "How many recent Primary emails to fetch first.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider: dashscope or anna-llm.", "required": False},
                {"name": "storage_provider", "type": "string", "description": "Storage provider: local or aps.", "required": False},
                {"name": "wait_timeout_seconds", "type": "integer", "description": "How long this invoke should wait before returning a pollable running state.", "required": False},
            ],
            "timeout": 600,
        },
        {
            "name": "get_authorized_email",
            "description": "Discover the primary authorized Gmail mailbox from Anna connected accounts, with legacy local fallback.",
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
    # 只记录响应形态，不记录模型正文，便于定位 Anna sampling 空响应。
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


def _encoded_frame_size(message: dict[str, Any]) -> int:
    return len(json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))


def _limit_inbox_thread_response_frame(message: dict[str, Any]) -> dict[str, Any]:
    """最终防线：绝不输出超过 48 KiB 的线程页 JSON-RPC 帧。"""
    result = message.get("result") if isinstance(message.get("result"), dict) else None
    if not result or result.get("tool") != "get_inbox_thread_page":
        return message
    data = result.get("data") if isinstance(result.get("data"), dict) else None
    messages = data.get("messages") if isinstance(data, dict) and isinstance(data.get("messages"), list) else None
    if messages is None or _encoded_frame_size(message) <= MAX_INBOX_THREAD_RESPONSE_BYTES:
        return message

    # The page builder already applies progressive budgets. This is an exact
    # frame-level safety net for unusually long request IDs or metadata.
    while _encoded_frame_size(message) > MAX_INBOX_THREAD_RESPONSE_BYTES:
        changed = False
        for item in messages:
            if not isinstance(item, dict):
                continue
            body_text = item.get("body_text")
            if isinstance(body_text, str) and body_text:
                if len(body_text) > 320:
                    item["body_text"] = body_text[: max(320, len(body_text) // 2)].rstrip()
                else:
                    item.pop("body_text", None)
                item["body_truncated"] = True
                changed = True
            elif item.get("body_html"):
                # Never return partial HTML; remove it and let the client use
                # the on-demand display-body tool.
                item.pop("body_html", None)
                item["body_truncated"] = True
                changed = True
        if changed:
            continue
        if len(messages) > 1:
            messages.pop(0)
            previous_cursor = int(data.get("next_before_index") or 0)
            data["next_before_index"] = previous_cursor + 1
            data["has_earlier"] = True
            data["returned_count"] = len(messages)
            continue

        data.update({
            "messages": [],
            "returned_count": 0,
            "has_earlier": True,
            "error": "Thread metadata exceeds the 48 KiB response limit.",
        })
        messages = data["messages"]
        if _encoded_frame_size(message) > MAX_INBOX_THREAD_RESPONSE_BYTES:
            # Keep only the fields required for a bounded, actionable error.
            result["data"] = {
                "mailbox": str(data.get("mailbox") or ""),
                "thread_id": str(data.get("thread_id") or ""),
                "messages": [],
                "returned_count": 0,
                "has_earlier": True,
                "next_before_index": data.get("next_before_index"),
                "latest_message_id": str(data.get("latest_message_id") or ""),
                "error": "Thread response exceeds the 48 KiB limit.",
            }
        break
    return message


def write_frame(message: dict[str, Any]) -> None:
    message = _limit_inbox_thread_response_frame(message)
    # Keep stdio frames ASCII-only. JSON escapes preserve the original Unicode
    # after parsing, and avoid GBK pipe/logging crashes in Windows hosts.
    payload = json.dumps(message, ensure_ascii=True, separators=(",", ":"))

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
            sys.stdout.write(json.dumps({"jsonrpc": JSONRPC_VERSION, "id": message.get("id"), "__file_transport": path}, ensure_ascii=True) + "\n")
        else:
            sys.stdout.write(payload + "\n")
        sys.stdout.flush()


sampling = SamplingClient(write_frame=write_frame)
host_upload = HostUploadClient(write_frame=write_frame)
platform_credentials = CredentialsClient(write_frame=write_frame)
_platform_credentials_ready = False
_platform_credentials_status_lock = threading.RLock()
_platform_credentials_status: dict[str, Any] = {
    "available": False,
    "code": "not_checked",
    "message": "Google Connected accounts have not been checked.",
    "action": "retry",
}


def _set_platform_credentials_status(*, available: bool, code: str, message: str, action: str) -> None:
    """更新当前进程最近一次 Google 多账号查询的安全状态。

    该状态会随 ``list_mailboxes`` 返回前端，因此只允许保存固定
    错误分类、通用提示和下一步动作。Reverse RPC 的 error data 可能含有平台
    内部凭据上下文，绝不能在这里保留、写日志或透传。
    """
    with _platform_credentials_status_lock:
        _platform_credentials_status.update({
            "available": available,
            "code": code,
            "message": message,
            "action": action,
        })


def get_platform_credentials_status() -> dict[str, Any]:
    """返回可安全展示的 Google Connected accounts 查询状态副本。"""
    with _platform_credentials_status_lock:
        return dict(_platform_credentials_status)


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
        # 调试开关允许前端按工具调用切换 APS 或本地 JSON 存储。
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

# LLM / Gmail 连通性探测专用线程池：与 Brief/Ask 等长任务隔离，避免占满 RPC worker
CONNECTIVITY_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="connectivity-check")


def refresh_platform_google_accounts(timeout_seconds: float = 12.0) -> list[dict[str, Any]]:
    """Refresh in-memory Google account metadata through Anna Credentials.

    Account metadata is safe to retain for the active process; access tokens
    are intentionally neither stored nor returned from this function.
    """
    if not _platform_credentials_ready:
        # 本地旧 runtime 没有 Reverse RPC；仍允许其使用既有单 token
        # 兼容路径，但前端可以根据该状态提示升级 runtime 才能发现多个账户。
        _set_platform_credentials_status(
            available=False,
            code="protocol_unsupported",
            message="This Anna runtime does not support Google multi-account discovery.",
            action="upgrade_runtime",
        )
        return []
    try:
        timeout = max(0.1, float(timeout_seconds))
        future = asyncio.run_coroutine_threadsafe(
            platform_credentials.list_accounts(provider="google", timeout=timeout), loop,
        )
        payload = future.result(timeout=timeout)
    except CredentialsError as exc:
        from mail_agent.mail_providers.gmail.adapter import set_platform_accounts
        # 用户未向本 App 授予 Connected accounts 时，不能继续使用
        # 默认注入 token 冒充完整账户列表；清空旧 metadata 防止断开授权后残留。
        set_platform_accounts([])
        if exc.code == -32061:
            _set_platform_credentials_status(
                available=False,
                code="not_granted",
                message="Enable Google Connected accounts for Anna Inbox, then retry.",
                action="enable_connected_accounts",
            )
        else:
            _set_platform_credentials_status(
                available=False,
                code="unavailable",
                message="Google connected-account discovery is temporarily unavailable.",
                action="retry",
            )
        log(f"platform Google account listing unavailable: {exc.code}")
        return []
    except Exception as exc:
        from mail_agent.mail_providers.gmail.adapter import set_platform_accounts
        set_platform_accounts([])
        _set_platform_credentials_status(
            available=False,
            code="unavailable",
            message="Google connected-account discovery is temporarily unavailable.",
            action="retry",
        )
        log(f"platform Google account listing unavailable: {type(exc).__name__}")
        return []

    accounts = payload.get("accounts") if isinstance(payload, dict) else []
    normalized = [item for item in accounts if isinstance(item, dict)] if isinstance(accounts, list) else []
    from mail_agent.mail_providers.gmail.adapter import set_platform_accounts
    set_platform_accounts(normalized)
    _set_platform_credentials_status(
        available=True,
        code="ok",
        message="Google Connected accounts are available.",
        action="none",
    )
    return normalized


def resolve_platform_google_token(account_id: str, timeout_seconds: float = 35.0) -> str:
    """Get one short-lived token without logging, returning, or persisting it."""
    try:
        timeout = max(0.1, float(timeout_seconds))
        future = asyncio.run_coroutine_threadsafe(
            platform_credentials.get_token(provider="google", account_id=account_id, timeout=timeout), loop,
        )
        payload = future.result(timeout=timeout)
    except CredentialsError as exc:
        raise ValueError(f"Google authorization is unavailable for this mailbox ({exc.code})") from exc
    except Exception as exc:
        raise ValueError("Google authorization token request failed") from exc
    token = str(payload.get("access_token") or "") if isinstance(payload, dict) else ""
    if not token:
        raise ValueError("Google authorization returned no access token for this mailbox")
    return token


from mail_agent.mail_providers.gmail.adapter import configure_platform_accounts
configure_platform_accounts(refresh_platform_google_accounts, resolve_platform_google_token)
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
    # 后台任务是进程内存态；落盘用于本地 runtime 重启后的轮询诊断。
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
    # 轮询状态只需要展示摘要，完整邮件正文保留在持久化卡片和详情接口里。
    if isinstance(value, list):
        return [_compact_run_payload(item, text_limit=text_limit) for item in value]
    if isinstance(value, dict):
        is_draft_artifact = str(value.get("type") or "") in {"draft_reply", "compose_draft"}
        compact: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"body", "body_text", "body_html", "raw", "raw_message"} and isinstance(item, str):
                field_limit = 12000 if is_draft_artifact and key == "body" else text_limit
                compact[key] = item[:field_limit]
                if len(item) > field_limit:
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
        # Brief 轮询完成后前端会再读持久化卡片，这里避免重复返回完整 cards/proposed_actions。
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


def _protocol_at_least(protocol_version: str, major: int, minor: int) -> bool:
    """比较 Host 协议版本，兼容未来 2.x 版本而不把 2.1 当作旧协议。"""
    try:
        parts = protocol_version.split(".", 2)
        received = (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except (TypeError, ValueError):
        return False
    return received >= (major, minor)


def handle_initialize(params: dict[str, Any]) -> dict[str, Any]:
    global _platform_credentials_ready
    protocol_version = str((params or {}).get("protocolVersion") or "1.1")
    # 接受 2.0 及以后次版本（如 2.1），避免把更高 2.x 误判为未协商 v2。
    v2 = _protocol_at_least(protocol_version, 2, 0)
    _platform_credentials_ready = v2
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
        host_upload.disable("Host upload unavailable because protocol v2 was not negotiated.")
        platform_credentials.disable("Platform credentials require Executa protocol 2.0.")
    return {
        "protocolVersion": protocol_version if v2 else "1.1",
        "serverInfo": {"name": TOOL_ID, "version": VERSION},
        "client_capabilities": {"sampling": {}, "storage": {}, "upload": {}} if v2 else {},
        "capabilities": {"sampling": {}, "storage": {}, "upload": {}} if v2 else {},
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


_BASE_ENV_CREDENTIALS = {
    name: os.environ.get(name)
    for name in ("DASHSCOPE_API_KEY", "DASHSCOPE_MODEL", "GMAIL_ACCESS_TOKEN", "GOOGLE_ACCESS_TOKEN")
}
_RUNTIME_INJECTED_CREDENTIALS: set[str] = set()
_RUNTIME_CREDENTIAL_LOCK = threading.RLock()


def apply_runtime_credentials(context: dict[str, Any]) -> None:
    raw_credentials = context.get("credentials") if isinstance(context, dict) else {}
    credentials = raw_credentials if isinstance(raw_credentials, dict) else {}
    with _RUNTIME_CREDENTIAL_LOCK:
        # 缺失字段表示本次 invoke 不更新该凭据；显式空值才恢复启动环境。
        for name in _BASE_ENV_CREDENTIALS:
            if name not in credentials:
                continue
            value = credentials.get(name)
            if value:
                os.environ[name] = str(value)
                _RUNTIME_INJECTED_CREDENTIALS.add(name)
            elif name in _RUNTIME_INJECTED_CREDENTIALS:
                original = _BASE_ENV_CREDENTIALS.get(name)
                if original is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = original
                _RUNTIME_INJECTED_CREDENTIALS.discard(name)

        # 多账号绑定是进程内共享快照：缺失字段不更新，显式 [] 才解绑全部。
        if "GMAIL_MULTI_TOKENS" not in credentials:
            return
        multi_raw = credentials.get("GMAIL_MULTI_TOKENS")
        try:
            tokens = multi_raw if isinstance(multi_raw, list) else json.loads(str(multi_raw))
            if not isinstance(tokens, list):
                log("ignored invalid GMAIL_MULTI_TOKENS snapshot: expected a JSON array")
                return
        except (json.JSONDecodeError, TypeError):
            log("ignored invalid GMAIL_MULTI_TOKENS snapshot: malformed JSON")
            return
        try:
            from anna_inbox_executa.mailbox_tools import _get_all_multi_tokens, _merge_multi_tokens_seed
            _run_storage_query(_merge_multi_tokens_seed(tokens), timeout=10.0)
            all_tokens = _run_storage_query(_get_all_multi_tokens(), timeout=10.0)
            from mail_agent.mail_providers.gmail.adapter import set_multi_tokens
            set_multi_tokens(all_tokens)
        except Exception as exc:
            # 更新失败时保留上一份已生效快照，避免半更新状态。
            log(f"ignored GMAIL_MULTI_TOKENS snapshot update: {type(exc).__name__}")


def check_google_oauth(context: dict[str, Any]) -> dict[str, Any]:
    credentials = read_credentials(context)
    has_gmail = bool(credentials["GMAIL_ACCESS_TOKEN"])
    has_google = bool(credentials["GOOGLE_ACCESS_TOKEN"])
    platform_accounts = refresh_platform_google_accounts()
    from mail_agent.mail_providers.gmail.adapter import get_multi_token_emails
    multi_emails = get_multi_token_emails()
    return {
        "authorized": has_gmail or has_google or len(platform_accounts) > 0 or len(multi_emails) > 0,
        "credential_names": {
            "gmail": "present" if has_gmail else "missing",
            "google": "present" if has_google else "missing",
        },
        "platform_account_count": len(platform_accounts),
        "platform_accounts": [
            {
                "account_id": str(item.get("account_id") or ""),
                "email": str(item.get("email") or ""),
                "is_default": bool(item.get("is_default")),
                "status": str(item.get("status") or ""),
            }
            for item in platform_accounts
        ],
        "legacy_multi_token_count": len(multi_emails),
        "next_step": "Google OAuth credential is available." if has_gmail or has_google or platform_accounts or multi_emails else "Authorize Google/Gmail in Anna platform authorizations, then retry.",
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


def dispatch_host_upload_response(message: dict[str, Any]) -> bool:
    return host_upload.dispatch_response(message)


def dispatch_platform_credentials_response(message: dict[str, Any]) -> bool:
    return platform_credentials.dispatch_response(message)

__all__ = [name for name in globals() if not name.startswith("__")]
