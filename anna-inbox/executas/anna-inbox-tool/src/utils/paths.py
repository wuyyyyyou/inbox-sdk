from __future__ import annotations

import os
import re
from pathlib import Path


DEFAULT_LOCAL_TEST_EMAIL = "kate@anna.partners"


def tool_root() -> Path:
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    path = os.environ.get("ANNA_INBOX_DATA_DIR")
    if path:
        return Path(path).expanduser().resolve()
    return tool_root() / ".data"


def token_dir() -> Path:
    path = os.environ.get("ANNA_INBOX_TOKEN_DIR")
    if path:
        return Path(path).expanduser().resolve()
    return tool_root() / ".secrets" / "gmail_tokens"


def sanitize_mailbox_id(mailbox_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", mailbox_id.strip())
    return safe.strip("._") or "default"


def default_mailbox_id() -> str:
    return os.environ.get("ANNA_INBOX_DEFAULT_EMAIL") or DEFAULT_LOCAL_TEST_EMAIL
