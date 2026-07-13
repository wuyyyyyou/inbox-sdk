"""Integration tests for multi-token flow.

Covers: credential parse → APS seed → discovery → auth check → token lookup.
Run:  PYTHONPATH=src python src/tests/test_multi_token_integration.py
"""

from __future__ import annotations

import json
import io
import os
import ssl
import sys
import threading
import time
import urllib.error
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
    adapter.set_platform_accounts([])
    adapter.configure_platform_accounts(None, None)


def clear_env():
    """Remove Gmail-related env vars."""
    for k in ("GMAIL_ACCESS_TOKEN", "GOOGLE_ACCESS_TOKEN", "DASHSCOPE_API_KEY", "DASHSCOPE_MODEL"):
        os.environ.pop(k, None)


# ── Tests ───────────────────────────────────────────────────────────

class TestMultiTokenIntegration:
    def __init__(self, _store: MockStorage):
        self._store = _store

    def run(self):
        self.test_platform_account_id_alias_is_supported()
        self.test_platform_credentials_multi_account_flow()
        self.test_platform_credentials_grant_error_is_reported()
        self.test_mailbox_list_includes_credentials_status()
        self.test_single_token_backward_compat()
        self.test_multi_token_only()
        self.test_mixed_platform_and_multi()
        self.test_full_snapshot_is_authoritative()
        self.test_dedup_platform_in_multi()
        self.test_malformed_json_graceful_degrade()
        self.test_removed_token_is_pruned()
        self.test_missing_snapshot_does_not_unbind()
        self.test_concurrent_snapshot_reads_are_atomic()
        self.test_token_refresh_updates_map()
        self.test_concurrent_refresh_runs_once()
        self.test_unbind_during_refresh_does_not_restore_account()
        self.test_check_gmail_auth_all_sources()
        self.test_discover_mailboxes_merges_all()
        self.test_get_authorized_email_tool()

    # ── 0. Anna Credentials multi-account bridge ────────────────

    def test_platform_account_id_alias_is_supported(self):
        section("0. Platform account id alias")
        clear_env()
        clear_adapter_state()

        from mail_agent.mail_providers.gmail import adapter

        adapter.set_platform_accounts([
            {"id": "account-kateq", "email": "kateq@anna.partners", "is_default": True, "status": "active"},
            {"id": "account-kateqh", "email": "kateqh@anna.partners", "is_default": False, "status": "active"},
        ])

        accounts = adapter.get_platform_accounts()
        check("id alias keeps both platform accounts", [item["email"] for item in accounts], ["kateq@anna.partners", "kateqh@anna.partners"])
        check(
            "id alias becomes token account id",
            adapter.get_platform_account("kateqh@anna.partners").get("account_id"),
            "account-kateqh",
        )
        clear_adapter_state()

    def test_platform_credentials_multi_account_flow(self):
        section("0. Platform credentials multi-account flow")
        clear_env()
        clear_adapter_state()

        from mail_agent.mail_providers.gmail import adapter
        from anna_inbox_executa.mailbox_tools import _discover_mailboxes

        adapter.set_platform_accounts([
            {"account_id": "account-work", "email": "work@example.com", "label": "Work", "is_default": False, "status": "active"},
            {"account_id": "account-personal", "email": "personal@example.com", "label": "Personal", "is_default": True, "status": "active"},
        ])
        requested_ids: list[str] = []

        def resolve_token(account_id: str) -> str:
            requested_ids.append(account_id)
            return f"short-lived-{account_id}"

        adapter.configure_platform_accounts(lambda: [], resolve_token)
        check("default account first", adapter.get_platform_accounts()[0]["email"], "personal@example.com")
        check("work uses account id", adapter.get_access_token("work@example.com"), "short-lived-account-work")
        check("token request id", requested_ids, ["account-work"])

        with patch("anna_inbox_executa.mailbox_tools.refresh_platform_google_accounts", return_value=[]), \
             patch.object(adapter, "list_available_mailboxes_from_tokens", return_value=[]):
            discovered = _discover_mailboxes()
        check("platform account count", len(discovered), 2)
        check("platform default listed first", discovered[0]["email"], "personal@example.com")
        check("platform source", discovered[1]["auth_source"], "platform_credentials")
        check("metadata only display name", discovered[0]["display_name"], "Personal")

        clear_adapter_state()

    def test_platform_credentials_grant_error_is_reported(self):
        section("0a. Platform credentials grant errors are surfaced safely")
        clear_env()
        clear_adapter_state()

        from anna_inbox_executa import common
        from executa_sdk.credentials import CredentialsError

        async def rejected_list_accounts(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise CredentialsError(-32061, "not granted", {"credentials_token": "must-not-leak"})

        with (
            patch.object(common, "_platform_credentials_ready", True),
            patch.object(common.platform_credentials, "list_accounts", side_effect=rejected_list_accounts),
        ):
            check("grant error discovers no accounts", common.refresh_platform_google_accounts(), [])
            status = common.get_platform_credentials_status()

        check("grant error code", status["code"], "not_granted")
        check("grant error action", status["action"], "enable_connected_accounts")
        check("grant error is unavailable", status["available"], False)
        status_text = json.dumps(status, ensure_ascii=False)
        check("grant error omits credential token", "credentials_token" in status_text, False)
        clear_adapter_state()

    def test_mailbox_list_includes_credentials_status(self):
        section("0b. Mailbox list includes safe credentials status")
        from anna_inbox_executa.mailbox_tools import _sync_list_mailboxes

        with patch("anna_inbox_executa.mailbox_tools._discover_mailboxes", return_value=[]), \
             patch("anna_inbox_executa.mailbox_tools.get_platform_credentials_status", return_value={
                 "available": False,
                 "code": "not_granted",
                 "message": "Enable Google Connected accounts for Anna Inbox, then retry.",
                 "action": "enable_connected_accounts",
             }):
            payload = _sync_list_mailboxes()

        status = payload["credentials_status"]
        check("mailbox list grant action", status["action"], "enable_connected_accounts")
        check("mailbox list has no credentials", "token" in json.dumps(status).lower(), False)

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
            from anna_inbox_executa.mailbox_tools import _discover_mailboxes

            discovered = _discover_mailboxes()
            check("discovered count", len(discovered), 1)
            check("email", discovered[0]["email"], "primary@gmail.com")
            check("auth_source", discovered[0]["auth_source"], "platform")

            from anna_inbox_executa.gmail_tools import _check_gmail_auth

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

    # ── 3a. Full snapshot takes precedence over legacy singleton ─

    def test_full_snapshot_is_authoritative(self):
        section("3a. Full multi-token snapshot is authoritative")
        clear_env()
        clear_adapter_state()

        os.environ["GMAIL_ACCESS_TOKEN"] = "legacy_default_token"
        from mail_agent.mail_providers.gmail import adapter
        from anna_inbox_executa.gmail_tools import _check_gmail_auth

        adapter.set_multi_tokens([
            {"email": "primary@gmail.com", "access_token": "snapshot_primary_token"},
            {"email": "extra@gmail.com", "access_token": "snapshot_extra_token"},
        ])

        with patch.object(adapter, "get_authorized_email", return_value="primary@gmail.com"):
            check("primary uses snapshot token", adapter.get_access_token("primary@gmail.com"), "snapshot_primary_token")
            check("extra uses snapshot token", adapter.get_access_token("extra@gmail.com"), "snapshot_extra_token")
            primary_auth = _check_gmail_auth("primary@gmail.com")
            extra_auth = _check_gmail_auth("extra@gmail.com")
            check("primary snapshot auth", primary_auth["authorized"], True)
            check("extra snapshot auth", extra_auth["authorized"], True)
            check("extra snapshot source", extra_auth["source"], "platform_multi")

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
        from anna_inbox_executa.common import beijing_now

        with _patch.object(adapter, "get_authorized_email", return_value="primary@gmail.com"), \
             _patch.object(adapter, "list_available_mailboxes_from_tokens", return_value=[]):
            from anna_inbox_executa.mailbox_tools import _discover_mailboxes

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
        from anna_inbox_executa.common import apply_runtime_credentials

        adapter.set_multi_tokens([{"email": "keep@gmail.com", "access_token": "keep"}])

        # Invalid input must retain the last valid binding snapshot.
        context = {"credentials": {"GMAIL_MULTI_TOKENS": "not valid json {{{"}}
        apply_runtime_credentials(context)

        check("bad json retains snapshot", adapter.get_multi_token_emails(), ["keep@gmail.com"])

        # Simulate valid but empty list
        context2 = {"credentials": {"GMAIL_MULTI_TOKENS": "[]"}}
        apply_runtime_credentials(context2)
        check("map empty after empty list", adapter.get_multi_token_emails(), [])

        clear_env()

    def test_removed_token_is_pruned(self):
        section("6. Removed credential token is pruned")
        clear_env()
        clear_adapter_state()
        self._store._store.clear()

        from anna_inbox_executa.common import apply_runtime_credentials
        from mail_agent.mail_providers.gmail import adapter
        from mail_agent.storage import client as storage_client
        storage_client._storage = self._store

        apply_runtime_credentials({"credentials": {"GMAIL_MULTI_TOKENS": json.dumps([
            {"email": "keep@gmail.com", "access_token": "keep"},
            {"email": "xin@anna.partners", "access_token": "removed"},
        ])}})
        check("both tokens initially present", adapter.get_multi_token_emails(), ["keep@gmail.com", "xin@anna.partners"])

        apply_runtime_credentials({"credentials": {"GMAIL_MULTI_TOKENS": json.dumps([
            {"email": "keep@gmail.com", "access_token": "keep"},
        ])}})
        check("removed token pruned", adapter.get_multi_token_emails(), ["keep@gmail.com"])

        apply_runtime_credentials({"credentials": {}})
        check("missing credential retains snapshot", adapter.get_multi_token_emails(), ["keep@gmail.com"])
        clear_adapter_state()

    def test_missing_snapshot_does_not_unbind(self):
        section("6a. Missing credential field does not change bindings")
        clear_env()
        clear_adapter_state()

        from anna_inbox_executa.common import apply_runtime_credentials
        from mail_agent.mail_providers.gmail import adapter

        adapter.set_multi_tokens([{"email": "bound@gmail.com", "access_token": "bound"}])
        apply_runtime_credentials({"credentials": {"DASHSCOPE_MODEL": "qwen-plus"}})
        check("unrelated credential retains binding", adapter.get_multi_token_emails(), ["bound@gmail.com"])

    def test_concurrent_snapshot_reads_are_atomic(self):
        section("6b. Concurrent binding updates expose complete snapshots")
        clear_env()
        clear_adapter_state()

        from mail_agent.mail_providers.gmail import adapter

        first = [
            {"email": "a@gmail.com", "access_token": "a"},
            {"email": "b@gmail.com", "access_token": "b"},
        ]
        second = [
            {"email": "c@gmail.com", "access_token": "c"},
            {"email": "d@gmail.com", "access_token": "d"},
        ]
        allowed = {("a@gmail.com", "b@gmail.com"), ("c@gmail.com", "d@gmail.com")}
        observed: list[tuple[str, ...]] = []

        def writer() -> None:
            for index in range(500):
                adapter.set_multi_tokens(first if index % 2 == 0 else second)

        def reader() -> None:
            for _ in range(1000):
                observed.append(tuple(adapter.get_multi_token_emails()))

        adapter.set_multi_tokens(first)
        threads = [threading.Thread(target=writer), threading.Thread(target=reader), threading.Thread(target=reader)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        check("all observed snapshots are complete", all(snapshot in allowed for snapshot in observed), True)

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
        from unittest.mock import patch

        mock_response_data = _json.dumps({"access_token": "NEW_REFRESHED_TOKEN", "expires_in": 3600}).encode()
        mock_response = io.BytesIO(mock_response_data)

        with patch("urllib.request.urlopen", return_value=mock_response):
            tok = adapter.get_access_token("refresh_test@gmail.com")
            check("refreshed token", tok, "NEW_REFRESHED_TOKEN")
            check("map updated", adapter._multi_token_map["refresh_test@gmail.com"]["access_token"], "NEW_REFRESHED_TOKEN")

    def test_concurrent_refresh_runs_once(self):
        section("6c. Concurrent reads share one token refresh")
        clear_env()
        clear_adapter_state()

        from mail_agent.mail_providers.gmail import adapter

        adapter.set_multi_tokens([{
            "email": "concurrent@gmail.com", "access_token": "old", "refresh_token": "r",
            "client_id": "c", "client_secret": "s", "expires_at": time.time() - 10,
        }])
        results: list[str] = []

        def response(*_args: Any, **_kwargs: Any) -> io.BytesIO:
            return io.BytesIO(json.dumps({"access_token": "fresh", "expires_in": 3600}).encode())

        def reader() -> None:
            results.append(adapter.get_access_token("concurrent@gmail.com"))

        with patch("urllib.request.urlopen", side_effect=response) as mocked_urlopen:
            threads = [threading.Thread(target=reader), threading.Thread(target=reader)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        check("both readers receive refreshed token", sorted(results), ["fresh", "fresh"])
        check("refresh request count", mocked_urlopen.call_count, 1)

    def test_unbind_during_refresh_does_not_restore_account(self):
        section("6d. Unbinding during refresh cannot resurrect a mailbox")
        clear_env()
        clear_adapter_state()

        from mail_agent.mail_providers.gmail import adapter

        adapter.set_multi_tokens([{
            "email": "remove@gmail.com", "access_token": "old", "refresh_token": "r",
            "client_id": "c", "client_secret": "s", "expires_at": time.time() - 10,
        }])
        refresh_started = threading.Event()
        allow_refresh_to_finish = threading.Event()
        errors: list[str] = []

        def delayed_response(*_args: Any, **_kwargs: Any) -> io.BytesIO:
            refresh_started.set()
            allow_refresh_to_finish.wait(timeout=2)
            return io.BytesIO(json.dumps({"access_token": "fresh", "expires_in": 3600}).encode())

        def reader() -> None:
            try:
                adapter.get_access_token("remove@gmail.com")
            except ValueError as exc:
                errors.append(str(exc))

        with patch("urllib.request.urlopen", side_effect=delayed_response):
            thread = threading.Thread(target=reader)
            thread.start()
            check("refresh started", refresh_started.wait(timeout=2), True)
            adapter.set_multi_tokens([])
            allow_refresh_to_finish.set()
            thread.join(timeout=2)

        check("mailbox remains unbound", adapter.get_multi_token_emails(), [])
        check("in-flight caller sees unbind", len(errors), 1)

    def test_token_refresh_retries_then_succeeds(self):
        section("6b. Token refresh retries transient network errors")
        clear_env()
        clear_adapter_state()

        from mail_agent.mail_providers.gmail import adapter

        adapter.set_multi_tokens([
            {"email": "retry_test@gmail.com", "access_token": "old_retry_token", "refresh_token": "r", "client_id": "c", "client_secret": "s", "expires_at": time.time() - 10},
        ])

        mock_response = io.BytesIO(json.dumps({"access_token": "RETRIED_TOKEN", "expires_in": 3600}).encode())
        transient_error = urllib.error.URLError(ssl.SSLError("UNEXPECTED_EOF_WHILE_READING"))

        with (
            patch("urllib.request.urlopen", side_effect=[transient_error, mock_response]) as mocked_urlopen,
            patch("time.sleep", return_value=None),
        ):
            tok = adapter.get_access_token("retry_test@gmail.com")
            check("retried token", tok, "RETRIED_TOKEN")
            check("retry count", mocked_urlopen.call_count, 2)

    def test_token_refresh_urlerror_falls_back_to_existing_token(self):
        section("6c. Token refresh URL error falls back to current token")
        clear_env()
        clear_adapter_state()

        from mail_agent.mail_providers.gmail import adapter

        adapter.set_multi_tokens([
            {"email": "fallback_test@gmail.com", "access_token": "still_usable_token", "refresh_token": "r", "client_id": "c", "client_secret": "s", "expires_at": time.time() - 10},
        ])

        transient_error = urllib.error.URLError(ssl.SSLError("UNEXPECTED_EOF_WHILE_READING"))

        with (
            patch("urllib.request.urlopen", side_effect=[transient_error, transient_error, transient_error]),
            patch("time.sleep", return_value=None),
        ):
            tok = adapter.get_access_token("fallback_test@gmail.com")
            check("fallback token", tok, "still_usable_token")

    # ── 7. _check_gmail_auth all sources ───────────────────────

    def test_check_gmail_auth_all_sources(self):
        section("7. _check_gmail_auth covers all sources")
        clear_env()
        clear_adapter_state()

        from mail_agent.mail_providers.gmail import adapter
        from anna_inbox_executa.gmail_tools import _check_gmail_auth

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
            from anna_inbox_executa.mailbox_tools import _discover_mailboxes

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
            from anna_inbox_executa.mailbox_tools import _discover_mailboxes

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
