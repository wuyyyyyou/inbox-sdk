"""Integration tests for multi-token flow.

Covers: credential parse → APS seed → discovery → auth check → token lookup.
Run:  PYTHONPATH=src python src/tests/test_multi_token_integration.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

_SRC = str(Path(__file__).resolve().parents[1])
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# ── Helpers ──────────────────────────────────────────────────────────

_printed = 0
_failed = 0


def check(name: str, actual: Any, expected: Any) -> None:
    global _printed, _failed
    ok = actual == expected
    if not ok:
        _failed += 1
        print(f"  FAIL [{name}]: expected {expected!r}, got {actual!r}")
    else:
        _printed += 1


def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def summary() -> None:
    global _printed, _failed
    total = _printed + _failed
    print(f"\n  Passed: {_printed}/{total}" + (f"  FAILED: {_failed}" if _failed else "  ALL OK"))


# ── Mock storage backend ────────────────────────────────────────────

class MockStorage:
    """In-memory storage that mimics APS KV get/set."""

    def __init__(self):
        self._store: dict[str, Any] = {}

    async def get(self, key: str, scope: str = "user") -> dict:
        if key in self._store:
            return {"exists": True, "value": self._store[key]}
        return {"exists": False, "value": None}

    async def set(self, key: str, value: Any, scope: str = "user") -> dict:
        self._store[key] = value
        return {"ok": True}

    async def list(self, prefix: str, cursor: Any = None, limit: int = 200, scope: str = "user") -> dict:
        items = [{"key": k} for k in self._store if k.startswith(prefix)]
        return {"items": items, "next_cursor": None}


# ── Setup ───────────────────────────────────────────────────────────

def setup_mock_storage():
    """Patch storage.client globals so all storage ops use MockStorage."""
    from mail_agent.storage import client as storage_client

    storage = MockStorage()
    files = MagicMock()
    storage_client._storage = storage
    storage_client._files = files
    storage_client._scope = "user"
    return storage


def setup_event_loop():
    """Ensure an asyncio event loop is running in a background thread."""
    import asyncio
    import threading

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return loop
    except RuntimeError:
        pass

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    asyncio.set_event_loop(loop)
    return loop


def clear_adapter_state():
    """Reset global state in adapter.py between tests."""
    from mail_agent.mail_providers.gmail import adapter

    adapter._multi_token_map.clear()
    adapter._discovered_email = ""


def clear_env():
    """Remove Gmail-related env vars."""
    for k in ("GMAIL_ACCESS_TOKEN", "GOOGLE_ACCESS_TOKEN", "DASHSCOPE_API_KEY", "DASHSCOPE_MODEL"):
        os.environ.pop(k, None)


# ── Tests ───────────────────────────────────────────────────────────

class TestMultiTokenIntegration:
    def __init__(self, _store: MockStorage):
        self._store = _store

    def run(self):
        self.test_single_token_backward_compat()
        self.test_multi_token_only()
        self.test_mixed_platform_and_multi()
        self.test_dedup_platform_in_multi()
        self.test_malformed_json_graceful_degrade()
        self.test_token_refresh_updates_map()
        self.test_check_gmail_auth_all_sources()
        self.test_discover_mailboxes_merges_all()
        self.test_get_authorized_email_tool()

    # ── 1. Single token backward compat ────────────────────────

    def test_single_token_backward_compat(self):
        section("1. Single token backward compatibility")
        clear_env()
        clear_adapter_state()

        os.environ["GMAIL_ACCESS_TOKEN"] = "platform_token_abc"

        self._store._store.clear()
        from mail_agent.storage.ops import MAILBOX_REGISTRY_KEY
        self._store._store[MAILBOX_REGISTRY_KEY] = {"mailboxes": [], "updated_at": ""}

        from unittest.mock import patch as _patch

        with _patch("mail_agent.mail_providers.gmail.adapter.get_authorized_email", return_value="primary@gmail.com"), \
             _patch("mail_agent.mail_providers.gmail.adapter.list_available_mailboxes_from_tokens", return_value=[]):
            from anna_inbox_executa.main import _discover_mailboxes

            discovered = _discover_mailboxes()
            check("discovered count", len(discovered), 1)
            check("email", discovered[0]["email"], "primary@gmail.com")
            check("auth_source", discovered[0]["auth_source"], "platform")

            from anna_inbox_executa.main import _check_gmail_auth

            auth = _check_gmail_auth("primary@gmail.com")
            check("auth authorized", auth["authorized"], True)
            check("auth source", auth["source"], "platform")

            auth2 = _check_gmail_auth("other@gmail.com")
            check("auth other", auth2["authorized"], False)

        clear_env()
        clear_adapter_state()

    # ── 2. Multi-token only (no platform token) ─────────────────

    def test_multi_token_only(self):
        section("2. Multi-token only (no platform token)")
        clear_env()
        clear_adapter_state()
        self._store._store.clear()

        from mail_agent.mail_providers.gmail import adapter

        tokens = [
            {"email": "second@gmail.com", "access_token": "tok_2nd", "refresh_token": "r2", "client_id": "c", "client_secret": "s", "expires_at": time.time() + 3600},
            {"email": "third@gmail.com", "access_token": "tok_3rd"},
        ]
        adapter.set_multi_tokens(tokens)

        check("multi_token_emails", adapter.get_multi_token_emails(), ["second@gmail.com", "third@gmail.com"])
        check("multi_token_map size", len(adapter.get_multi_token_map()), 2)

        # get_access_token
        tok = adapter.get_access_token("second@gmail.com")
        check("token second", tok, "tok_2nd")
        tok = adapter.get_access_token("third@gmail.com")
        check("token third", tok, "tok_3rd")

        # normalize_mailbox
        check("normalize second", adapter.normalize_mailbox("second@gmail.com"), "second@gmail.com")
        check("normalize third", adapter.normalize_mailbox("third@gmail.com"), "third@gmail.com")

        # unknown
        try:
            adapter.get_access_token("unknown@gmail.com")
            check("unknown should raise", False, True)
        except ValueError:
            pass

    # ── 3. Mixed platform + multi-token ────────────────────────

    def test_mixed_platform_and_multi(self):
        section("3. Mixed: platform token + multi-tokens")
        clear_env()
        clear_adapter_state()

        os.environ["GMAIL_ACCESS_TOKEN"] = "platform_tok"

        from mail_agent.mail_providers.gmail import adapter

        adapter.set_multi_tokens([
            {"email": "extra@gmail.com", "access_token": "tok_extra", "refresh_token": "r", "client_id": "c", "client_secret": "s", "expires_at": time.time() + 3600},
        ])

        from unittest.mock import patch

        with patch.object(adapter, "get_authorized_email", return_value="primary@gmail.com"):
            # Platform token should be used for primary@gmail.com
            tok = adapter.get_access_token("primary@gmail.com")
            check("platform email uses platform token", tok, "platform_tok")

            # Multi-token should be used for extra@gmail.com
            tok2 = adapter.get_access_token("extra@gmail.com")
            check("extra email uses multi token", tok2, "tok_extra")

        clear_env()
        clear_adapter_state()

    # ── 4. Dedup: platform email in multi-token ────────────────

    def test_dedup_platform_in_multi(self):
        section("4. Dedup: platform email also in multi-token")
        clear_env()
        clear_adapter_state()
        self._store._store.clear()

        os.environ["GMAIL_ACCESS_TOKEN"] = "platform_tok_xyz"

        from mail_agent.mail_providers.gmail import adapter

        # Register multi-tokens including one that matches platform email
        adapter.set_multi_tokens([
            {"email": "primary@gmail.com", "access_token": "tok_old", "refresh_token": "r", "client_id": "c", "client_secret": "s", "expires_at": time.time() + 3600},
            {"email": "extra@gmail.com", "access_token": "tok_extra"},
        ])

        from unittest.mock import patch as _patch
        from anna_inbox_executa.main import beijing_now

        with _patch.object(adapter, "get_authorized_email", return_value="primary@gmail.com"), \
             _patch.object(adapter, "list_available_mailboxes_from_tokens", return_value=[]):
            from anna_inbox_executa.main import _discover_mailboxes

            discovered = _discover_mailboxes()
            emails = [d["email"] for d in discovered]
            check("discovered count", len(discovered), 2)
            check("primary first", discovered[0]["email"], "primary@gmail.com")
            check("primary auth_source", discovered[0]["auth_source"], "platform")
            check("extra second", discovered[1]["email"], "extra@gmail.com")
            check("extra auth_source", discovered[1]["auth_source"], "platform_multi")
            check("no duplicate", emails.count("primary@gmail.com"), 1)

        clear_env()
        clear_adapter_state()

    # ── 5. Malformed JSON graceful degrade ─────────────────────

    def test_malformed_json_graceful_degrade(self):
        section("5. Malformed JSON → graceful degrade")
        clear_env()
        clear_adapter_state()

        from mail_agent.mail_providers.gmail import adapter
        from anna_inbox_executa.main import apply_runtime_credentials

        # Simulate bad JSON in credential
        context = {"credentials": {"GMAIL_MULTI_TOKENS": "not valid json {{{"}}
        apply_runtime_credentials(context)

        # Should NOT crash; multi_token_map should remain empty
        check("map empty after bad json", adapter.get_multi_token_emails(), [])

        # Simulate valid but empty list
        context2 = {"credentials": {"GMAIL_MULTI_TOKENS": "[]"}}
        apply_runtime_credentials(context2)
        check("map empty after empty list", adapter.get_multi_token_emails(), [])

        clear_env()

    # ── 6. Token refresh updates map ───────────────────────────

    def test_token_refresh_updates_map(self):
        section("6. Token refresh updates in-memory map")
        clear_env()
        clear_adapter_state()

        from mail_agent.mail_providers.gmail import adapter

        old_token = "old_access_token"
        adapter.set_multi_tokens([
            {"email": "refresh_test@gmail.com", "access_token": old_token, "refresh_token": "r", "client_id": "c", "client_secret": "s", "expires_at": time.time() - 10},
        ])

        # Mock the HTTP refresh call
        import json as _json
        import io
        from unittest.mock import patch

        mock_response_data = _json.dumps({"access_token": "NEW_REFRESHED_TOKEN", "expires_in": 3600}).encode()
        mock_response = io.BytesIO(mock_response_data)

        with patch("urllib.request.urlopen", return_value=mock_response):
            tok = adapter.get_access_token("refresh_test@gmail.com")
            check("refreshed token", tok, "NEW_REFRESHED_TOKEN")
            check("map updated", adapter._multi_token_map["refresh_test@gmail.com"]["access_token"], "NEW_REFRESHED_TOKEN")

    # ── 7. _check_gmail_auth all sources ───────────────────────

    def test_check_gmail_auth_all_sources(self):
        section("7. _check_gmail_auth covers all sources")
        clear_env()
        clear_adapter_state()

        from mail_agent.mail_providers.gmail import adapter
        from anna_inbox_executa.main import _check_gmail_auth

        # Case A: platform
        os.environ["GMAIL_ACCESS_TOKEN"] = "plat"
        adapter.set_multi_tokens([
            {"email": "multi@gmail.com", "access_token": "m"},
        ])
        from unittest.mock import patch

        with patch.object(adapter, "get_authorized_email", return_value="primary@gmail.com"):
            r = _check_gmail_auth("primary@gmail.com")
            check("platform auth", r["authorized"], True)
            check("platform source", r["source"], "platform")

        # Case B: multi-token
        clear_env()
        r = _check_gmail_auth("multi@gmail.com")
        check("multi auth", r["authorized"], True)
        check("multi source", r["source"], "platform_multi")

        # Case C: none
        r = _check_gmail_auth("nobody@gmail.com")
        check("none auth", r["authorized"], False)
        check("none source", r["source"], "none")

        clear_env()
        clear_adapter_state()

    # ── 8. _discover_mailboxes merges all sources ───────────────

    def test_discover_mailboxes_merges_all(self):
        section("8. _discover_mailboxes merges all 3 sources")
        clear_env()
        clear_adapter_state()

        os.environ["GMAIL_ACCESS_TOKEN"] = "plat"
        from mail_agent.mail_providers.gmail import adapter

        adapter.set_multi_tokens([
            {"email": "extra@gmail.com", "access_token": "e"},
        ])

        from unittest.mock import patch

        with patch.object(adapter, "get_authorized_email", return_value="primary@gmail.com"), patch.object(adapter, "list_available_mailboxes_from_tokens", return_value=[{"email": "local@gmail.com", "provider": "gmail", "auth_source": "local_file", "authorized": True}]):
            from anna_inbox_executa.main import _discover_mailboxes

            discovered = _discover_mailboxes()
            emails = [d["email"] for d in discovered]
            sources = [d["auth_source"] for d in discovered]

            check("count", len(discovered), 3)
            check("emails", emails, ["primary@gmail.com", "extra@gmail.com", "local@gmail.com"])
            check("sources", sources, ["platform", "platform_multi", "local_file"])

        clear_env()
        clear_adapter_state()

    # ── 9. get_authorized_email tool handler ────────────────────

    def test_get_authorized_email_tool(self):
        section("9. get_authorized_email tool returns all mailboxes")
        clear_env()
        clear_adapter_state()

        os.environ["GMAIL_ACCESS_TOKEN"] = "plat"
        from mail_agent.mail_providers.gmail import adapter

        adapter.set_multi_tokens([
            {"email": "extra@gmail.com", "access_token": "e"},
        ])

        from unittest.mock import patch

        with patch.object(adapter, "get_authorized_email", return_value="primary@gmail.com"), \
             patch.object(adapter, "list_available_mailboxes_from_tokens", return_value=[]):
            from anna_inbox_executa.main import _discover_mailboxes

            discovered = _discover_mailboxes()
            authorized = [d["email"] for d in discovered if d.get("authorized")]
            primary = authorized[0] if authorized else ""
            source = discovered[0]["auth_source"] if discovered else "none"

            check("authorized emails", authorized, ["primary@gmail.com", "extra@gmail.com"])
            check("primary", primary, "primary@gmail.com")
            check("source", source, "platform")

        clear_env()
        clear_adapter_state()


# ── Main ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Multi-Token Integration Tests")
    print(f"Working dir: {Path.cwd()}")
    print(f"Src path: {_SRC}")

    loop = setup_event_loop()
    storage = setup_mock_storage()

    tester = TestMultiTokenIntegration(storage)
    tester.run()

    summary()
