"""Contact avatar resolution regression tests (no network)."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> None:
    from mail_agent.mail_providers.gmail import adapter

    def reset_avatar_state() -> None:
        adapter._contact_avatar_cache.clear()
        adapter._contact_avatar_loaded.clear()

    def fake_people_get(_token: str, path: str, _params: dict[str, object]) -> dict[str, object]:
        if path == "people/me/connections":
            return {
                "connections": [
                    {
                        "emailAddresses": [{"value": "contact@example.com"}],
                        "photos": [{"url": "https://people.example/avatar.jpg"}],
                    }
                ]
            }
        if path == "otherContacts":
            return {"otherContacts": []}
        raise AssertionError(f"unexpected People API path: {path}")

    reset_avatar_state()
    with (
        patch.object(adapter, "get_access_token", return_value="token"),
        patch.object(adapter, "_people_api_get", side_effect=fake_people_get),
        patch.object(adapter, "get_account_avatar_url", return_value=""),
    ):
        result = adapter.resolve_contact_avatar_urls(
            "owner@example.com",
            ["Contact@Example.com", "grav@example.com", "missing@example.com"],
        )

    assert result["permission_required"] is False
    assert result["avatars"]["contact@example.com"] == "https://people.example/avatar.jpg"
    assert "missing@example.com" not in result["avatars"]

    reset_avatar_state()
    http_403 = HTTPError("", 403, "Forbidden", {}, io.BytesIO(b'{"error":{"message":"denied"}}'))
    with (
        patch.object(adapter, "get_access_token", return_value="token"),
        patch.object(adapter, "_people_api_get", side_effect=http_403),
    ):
        denied = adapter.resolve_contact_avatar_urls("owner@example.com", ["grav@example.com"])

    assert denied["permission_required"] is True
    assert "grav@example.com" not in denied["avatars"]
    print("PASS contact avatar tests")


if __name__ == "__main__":
    main()
