"""联系人记忆的结构化类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..storage.types import _now

ContactEventType = Literal["message_seen", "card_created", "user_replied", "user_decision", "owner_replied"]
ContactEventSource = Literal["gmail_scan", "handle_action", "gmail_backfill"]
MemoryPurpose = Literal["card_generation", "draft_generation", "thread_summary"]
ThreadStatus = Literal["open", "waiting_for_them", "closed", "unknown"]
MemoryImportance = Literal["high", "medium", "low"]
MessageDirection = Literal["inbound", "outbound"]


@dataclass
class ContactMemoryEvidence:
    mailbox: str = ""
    thread_id: str = ""
    message_id: str = ""
    date: str = ""
    source: str = ""


@dataclass
class ContactThreadSummary:
    summary: str = ""
    current_state: str = ""
    open_loop: str = ""
    status: ThreadStatus = "unknown"
    importance: MemoryImportance = "medium"


@dataclass
class ContactMessageSummary:
    message_id: str = ""
    from_addr: str = ""
    direction: MessageDirection = "inbound"
    date: str = ""
    summary: str = ""
    action_signal: str = ""


@dataclass
class ContactThreadMemory:
    thread_id: str
    subject: str = ""
    thread_summary: ContactThreadSummary = field(default_factory=ContactThreadSummary)
    message_summaries: list[ContactMessageSummary] = field(default_factory=list)
    source_refs: list[ContactMemoryEvidence] = field(default_factory=list)
    updated_at: str = field(default_factory=_now)


@dataclass
class ContactMemoryStats:
    messages_seen: int = 0
    threads_seen: int = 0
    user_replies: int = 0
    dismissed_count: int = 0
    handled_manually_count: int = 0
    no_action_count: int = 0


@dataclass
class ContactMemoryFile:
    mailbox: str
    contact_email: str
    display_name: str = ""
    threads: list[ContactThreadMemory] = field(default_factory=list)
    stats: ContactMemoryStats = field(default_factory=ContactMemoryStats)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass
class ContactMemoryQuery:
    mailbox: str
    contact_email: str
    current_subject: str = ""
    current_body: str = ""
    current_thread_id: str = ""
    purpose: MemoryPurpose = "thread_summary"


@dataclass
class ContactContextTopic:
    thread_id: str = ""
    title: str = ""
    summary: str = ""
    open_loop: str = ""
    status: ThreadStatus = "unknown"
    confidence: float = 0.0
    reason: str = ""


@dataclass
class ContactContext:
    contact_email: str = ""
    relationship_hint: str = ""
    relevant_topics: list[ContactContextTopic] = field(default_factory=list)
    reply_guidance: str = ""
    source_refs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SelectedThreadMemory:
    thread_id: str = ""
    relevance: float = 0.0
    reason: str = ""
    include_message_ids: list[str] = field(default_factory=list)


@dataclass
class ContactMemorySelection:
    selected: list[SelectedThreadMemory] = field(default_factory=list)
    none_reason: str = ""
