"""按邮箱隔离的联系人记忆模块。"""

from .indexer import ingest_card_event
from .retriever import retrieve_contact_context, format_contact_context_for_prompt

__all__ = [
    "ingest_card_event",
    "retrieve_contact_context",
    "format_contact_context_for_prompt",
]
