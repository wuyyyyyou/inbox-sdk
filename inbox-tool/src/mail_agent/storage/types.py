"""Persistent data types for the mail agent's local JSON store.

Key naming convention (user scope):
    anna-inbox/mailbox/{sanitized_email}/scan_state
    anna-inbox/mailbox/{sanitized_email}/processed/{gmail_message_id}
    anna-inbox/mailbox/{sanitized_email}/run/{run_id}
    anna-inbox/mailbox/{sanitized_email}/cards/active
    anna-inbox/prefs/snooze
    anna-inbox/prefs/learning
    anna-inbox/runs/history
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

BEIJING_TZ = timezone(__import__("datetime").timedelta(hours=8), name="Asia/Shanghai")


def _now() -> str:
    return datetime.now(BEIJING_TZ).isoformat()


# ── Scan state ──────────────────────────────────────────────────────


@dataclass
class MailboxRegistryEntry:
    email: str
    display_name: str = ""
    avatar_url: str = ""
    provider: str = "gmail"
    auth_source: str = ""
    authorized: bool = True
    selected: bool = True
    last_auth_checked_at: str = ""
    last_scan_at: str = ""
    last_scan_status: str = ""
    last_error: str = ""
    card_count: int = 0
    added_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass
class MailboxRegistry:
    mailboxes: list[MailboxRegistryEntry] = field(default_factory=list)
    updated_at: str = field(default_factory=_now)


@dataclass
class ScanState:
    mailbox: str
    last_scan_ts: str = ""             # ISO timestamp of last scan
    last_message_internal_date: str = ""  # 最近一次扫描到的最新 Gmail internalDate
    last_history_id: str = ""           # Gmail historyId at last scan
    total_scans: int = 0
    total_processed: int = 0

    @classmethod
    def empty(cls, mailbox: str) -> ScanState:
        return cls(mailbox=mailbox)


# ── Scan plan (user-configurable scan preferences) ──────────────────


@dataclass
class ScanPlan:
    mailbox: str
    scan_window_days: int = 7       # 扫描往回看几天（首次和增量统一）
    max_messages: int = 100         # 每轮最多扫几封
    scan_categories: list[str] = field(default_factory=list)  # 额外扫描分类：promotions, social, updates, forums
    updated_at: str = field(default_factory=_now)

    @classmethod
    def empty(cls, mailbox: str) -> ScanPlan:
        return cls(mailbox=mailbox)


# ── Inbox display preferences (mailbox-scoped) ─────────────────────


@dataclass
class InboxCustomCategory:
    """用户保存的本地 Inbox Split；查询语法由前端统一校验。"""

    id: str
    name: str
    query: str
    hide_when_empty: bool = False
    bundling_behavior: Literal["default", "by_sender", "none"] = "default"


@dataclass
class InboxSettings:
    """主页 Inbox 的显示设置，必须随 mailbox 隔离保存。"""

    mailbox: str
    display_range_days: int = 30
    time_section_mode: str = "detailed"
    stars_enabled: bool = True
    stars_limit: int = 10
    todos_enabled: bool = True
    todos_limit: int = 10
    # LLM 连通性探测轮询间隔（秒）；0 表示关闭自动轮询，默认 60
    llm_status_poll_seconds: int = 60
    # Gmail History 自动刷新间隔（秒）；0 表示只在用户手动刷新时同步，默认 5
    auto_sync_seconds: int = 5
    custom_categories: list[InboxCustomCategory] = field(default_factory=list)
    updated_at: str = field(default_factory=_now)

    @classmethod
    def empty(cls, mailbox: str) -> "InboxSettings":
        return cls(mailbox=mailbox)


# ── Processed message index ─────────────────────────────────────────


@dataclass
class ProcessedMessage:
    message_id: str
    thread_id: str = ""
    from_addr: str = ""
    subject: str = ""
    snippet: str = ""
    internal_date: str = ""
    processed_at: str = field(default_factory=_now)
    run_id: str = ""
    is_candidate: bool = False
    candidate_kind: str = ""
    priority: str = "low"
    read_depth: str = "header_only"
    confidence: float = 0.0


# ── V2 Attention Card (persisted) ───────────────────────────────────


@dataclass
class CardDetails:
    needs: str = ""              # what user action is needed
    latest_activity: str = ""    # who did what + when
    reviewed: str = ""           # Anna read depth description
    mailbox: str = ""


@dataclass
class OriginalEmail:
    source: str = "Gmail"
    thread: str = ""
    from_addr: str = ""
    to_addr: str = ""
    time: str = ""
    status: str = "Connected Gmail source"
    body: str = ""


@dataclass
class CardAction:
    id: str = ""
    label: str = ""           # 内部/抽屉详情中使用
    button_label: str = ""    # 卡片主按钮文案（更短、动作感更强）
    primary: bool = False
    status_title: str = ""
    status: str = ""


CardStatus = Literal["pending", "snoozed", "resolved", "dismissed"]


@dataclass
class PersistentCard:
    card_id: str
    message_id: str = ""
    thread_id: str = ""
    # 3-line display
    title: str = ""              # Line 1: Attention Title (≤1 visual line)
    summary: str = ""            # Line 2: Compressed Context (1–2 factual bullets)
    recommendation: str = ""     # Line 3: Suggested Next Step
    label: str = ""              # category tag
    priority: str = "medium"
    item_type: str = ""          # from judgment: reply_required, security_risk, low_value_cleanup, etc.
    draft_reply: str = ""        # persisted LLM-generated draft reply body
    thread_summary: str = ""     # persisted LLM thread summary (JSON string)
    display_section: str = "main"
    # expanded details
    details: CardDetails = field(default_factory=CardDetails)
    original: OriginalEmail = field(default_factory=OriginalEmail)
    actions: list[CardAction] = field(default_factory=list)
    # lifecycle
    status: CardStatus = "pending"
    snooze_until: str = ""       # ISO timestamp, set when snoozed
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    resolved_at: str = ""
    resolution: str = ""         # "read" | "no_action_needed" | "handled_manually" | "dismissed"
    # cleanup bundle
    card_type: str = ""          # "cleanup_bundle" for folded low-priority cards
    bundled_messages: list = field(default_factory=list)  # preview: first 3 items
    bundled_count: int = 0       # total count of cleanup messages
    user_action: str = ""        # "reply" | "review" — drives frontend category tabs
    reply_gaps: dict = field(default_factory=dict)  # {needs_user_input, summary, questions: [{id, question, hint, required}]}
    gmail_state: dict = field(default_factory=dict)  # lightweight external Gmail sync state
    attachments: list = field(default_factory=list)  # lightweight Gmail attachment metadata for UI/download


@dataclass
class ActiveCards:
    """Wraps the active cards list stored under anna-inbox/mailbox/{id}/cards/active."""
    cards: list[PersistentCard] = field(default_factory=list)
    updated_at: str = field(default_factory=_now)


# ── Run record ──────────────────────────────────────────────────────


@dataclass
class RunRecord:
    run_id: str
    mailbox: str = ""
    ts: str = field(default_factory=_now)
    strategy_mode: str = ""
    user_request: str = ""
    mode: str = "auto"
    plan_id: str = ""
    scanned_count: int = 0
    candidate_count: int = 0
    main_count: int = 0
    lower_count: int = 0
    summary: list[str] = field(default_factory=list)
    strategy: list[str] = field(default_factory=list)
    cards: list[dict[str, Any]] = field(default_factory=list)


# ── User preferences ────────────────────────────────────────────────


@dataclass
class SnoozePrefs:
    threads: list[str] = field(default_factory=list)      # thread subjects/IDs to deprioritize
    senders: list[str] = field(default_factory=list)      # sender addresses to deprioritize
    categories: list[str] = field(default_factory=list)   # categories to deprioritize
    reasons: list[str] = field(default_factory=list)      # "automated","promotional","newsletter","calendar","not_my_area","cc_only"
    updated_at: str = field(default_factory=_now)


@dataclass
class LearningRecord:
    pattern: str = ""
    action: str = ""
    learnt_at: str = field(default_factory=_now)


@dataclass
class UserPreferences:
    snooze: SnoozePrefs = field(default_factory=SnoozePrefs)
    learning: list[LearningRecord] = field(default_factory=list)


# ── Run history entry (lightweight, stored in anna-inbox/runs/history list) ────


@dataclass
class RunHistoryEntry:
    run_id: str
    mailbox: str = ""
    ts: str = ""
    request: str = ""
    mode: str = ""
    strategy: str = ""
    plan_id: str = ""
    result: str = ""      # 1-line summary
    summary: str = ""     # multi-line detail
    # card-action fields (entry_type="card_action")
    entry_type: str = "scan"         # "scan" | "card_action"
    card_id: str = ""
    card_title: str = ""
    action: str = ""                 # "read" | "snooze" | "reply" | "handled_manually" | "no_action_needed" | "cleanup_read" | "restore"
    detail: str = ""
    # card context for rendering history entries without an extra API call
    card_summary: str = ""
    card_from: str = ""
    card_subject: str = ""
    card_body: str = ""
