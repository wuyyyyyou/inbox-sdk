"""Homepage Gmail feed regression tests (no network or LLM)."""

from __future__ import annotations

import sys
import tempfile
import json
from pathlib import Path
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> None:
    from anna_inbox_executa.gmail_tools import get_cached_email, list_cached_emails, list_inbox_emails
    from mail_agent.mail_providers.gmail.adapter import clear_mailbox_cache, search_gmail, update_thread_state

    captured: dict[str, object] = {}

    def fake_search(mailbox: str, query: str, limit: int) -> list[str]:
        captured.update(mailbox=mailbox, query=query, limit=limit)
        return ["new", "old"]

    messages = [
        {
            "id": "old", "thread_id": "t-old", "internal_date": "1", "from": "Old <old@example.com>",
            "subject": "Old", "snippet": "Earlier", "label_ids": ["INBOX"], "attachments": [],
        },
        {
            "id": "new", "thread_id": "t-new", "internal_date": "2", "from": "New <new@example.com>",
            "to": "User <user@example.com>", "subject": "Important", "snippet": "Newest", "body_preview": "Preview", "label_ids": ["INBOX", "UNREAD", "IMPORTANT"],
            "attachments": [{"filename": "brief.pdf"}],
        },
    ]

    with (
        patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
        patch("mail_agent.mail_providers.gmail.adapter.gmail_request", return_value={"emailAddress": "user@example.com"}),
        patch("mail_agent.mail_providers.gmail.adapter.clear_mailbox_cache", return_value={"mailbox": "user@example.com"}) as clear_cache,
        patch("mail_agent.mail_providers.gmail.adapter.live_search_metadata_and_cache", side_effect=fake_search),
        patch("mail_agent.mail_providers.gmail.adapter.list_messages", return_value=messages),
    ):
        result = list_inbox_emails("USER@example.com", 7, 100)
        starred = list_inbox_emails("USER@example.com", 30, 50, "starred")
        all_mail = list_inbox_emails("USER@example.com", 7, 500, "all", True)

    assert captured == {"mailbox": "user@example.com", "query": "in:anywhere -in:chats newer_than:7d", "limit": 500}
    assert [item["id"] for item in result["messages"]] == ["new", "old"]
    assert result["messages"][0]["unread"] is True
    assert result["messages"][0]["important"] is True
    assert result["messages"][0]["attachment_count"] == 1
    assert result["messages"][0]["to"] == "User <user@example.com>"
    assert "body_text" not in result["messages"][0]
    assert starred["category"] == "starred"
    assert all_mail["category"] == "all"
    assert all_mail["cache_reset"] == {"mailbox": "user@example.com"}
    clear_cache.assert_called_once_with("user@example.com")

    with tempfile.TemporaryDirectory() as temp_dir:
        cache_root = Path(temp_dir)
        target = cache_root / "user_example.com"
        sibling = cache_root / "other_example.com"
        target.mkdir()
        sibling.mkdir()
        (target / "index.json").write_text("{}", encoding="utf-8")
        (sibling / "index.json").write_text("{}", encoding="utf-8")
        with (
            patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
            patch("mail_agent.mail_providers.gmail.adapter.cache_dir", return_value=cache_root),
            patch("mail_agent.mail_providers.gmail.adapter._storage_cache_enabled", return_value=False),
        ):
            cleared = clear_mailbox_cache("USER@example.com")
        assert cleared["deleted_local_cache"] is True
        assert not target.exists()
        assert sibling.exists()

    now_ms = 1_720_000_000_000
    recent_messages = [
        dict(messages[0], internal_date=str(now_ms - 20 * 24 * 60 * 60 * 1000), label_ids=["TRASH"]),
        dict(messages[1], internal_date=str(now_ms - 2 * 24 * 60 * 60 * 1000)),
        {
            "id": "star", "thread_id": "t-star", "internal_date": str(now_ms - 1 * 24 * 60 * 60 * 1000),
            "from": "Star <star@example.com>", "subject": "Flagged", "snippet": "Flagged", "label_ids": ["STARRED"], "attachments": [],
        },
    ]
    with (
        patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
        patch("mail_agent.mail_providers.gmail.adapter.read_cache", return_value={"updated_at": "now", "messages": recent_messages}),
        patch("mail_agent.mail_providers.gmail.adapter.cache_debug_info", return_value={"backend": "local"}),
        patch("anna_inbox_executa.gmail_tools.time.time", return_value=now_ms / 1000),
    ):
        cached_feed = list_cached_emails("USER@example.com", 7, 10, "all")
        starred_cached_feed = list_cached_emails("USER@example.com", 7, 1, "starred")
    cached_new = next(item for item in cached_feed["messages"] if item["id"] == "new")
    assert cached_feed["category"] == "all"
    assert starred_cached_feed["category"] == "starred"
    assert starred_cached_feed["count"] == 1
    assert starred_cached_feed["messages"][0]["id"] == "star"
    assert cached_new["important"] is True
    assert cached_new["unread"] is True
    assert cached_new["attachment_count"] == 1

    gmail_pages = [
        {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "page-2"},
        {"messages": [{"id": "m3"}]},
    ]
    with patch("mail_agent.mail_providers.gmail.adapter.gmail_request", side_effect=gmail_pages) as request:
        ids = search_gmail("user@example.com", "in:anywhere -in:chats newer_than:7d", 500)
    assert ids == ["m1", "m2", "m3"]
    assert request.call_args_list[0].args[2]["includeSpamTrash"] == "true"
    assert request.call_args_list[1].args[2]["pageToken"] == "page-2"

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    thread_payload = {"id": "thread-1", "messages": [{"id": "m1"}, {"id": "m2"}]}
    with (
        patch("mail_agent.mail_providers.gmail.adapter.get_access_token", return_value="test-token"),
        patch("mail_agent.mail_providers.gmail.adapter.urllib.request.urlopen", return_value=FakeResponse(thread_payload)) as urlopen,
        patch("mail_agent.mail_providers.gmail.adapter.patch_cached_message_labels") as patch_labels,
    ):
        updated = update_thread_state("user@example.com", "thread-1", "mark_unread")
    request_object = urlopen.call_args.args[0]
    assert request_object.full_url.endswith("/users/me/threads/thread-1/modify")
    assert json.loads(request_object.data) == {"addLabelIds": ["UNREAD"], "removeLabelIds": []}
    assert updated["message_ids"] == ["m1", "m2"]
    patch_labels.assert_called_once_with("user@example.com", ["m1", "m2"], add_label_ids=["UNREAD"], remove_label_ids=[])

    with (
        patch("mail_agent.mail_providers.gmail.adapter.get_access_token", return_value="test-token"),
        patch("mail_agent.mail_providers.gmail.adapter.urllib.request.urlopen", side_effect=[FakeResponse(thread_payload), FakeResponse(thread_payload)]) as urlopen,
        patch("mail_agent.mail_providers.gmail.adapter.patch_cached_message_labels") as patch_labels,
    ):
        update_thread_state("user@example.com", "thread-1", "untrash")
    requests = [call.args[0] for call in urlopen.call_args_list]
    assert requests[0].full_url.endswith("/users/me/threads/thread-1/untrash")
    assert requests[1].full_url.endswith("/users/me/threads/thread-1/modify")
    assert json.loads(requests[1].data) == {"addLabelIds": ["INBOX"], "removeLabelIds": []}
    patch_labels.assert_called_once_with("user@example.com", ["m1", "m2"], add_label_ids=["INBOX"], remove_label_ids=["TRASH"])

    fetched = {"id": "missing", "body_text": "Fetched body"}
    with (
        patch("mail_agent.mail_providers.gmail.adapter.normalize_mailbox", return_value="user@example.com"),
        patch("mail_agent.mail_providers.gmail.adapter.read_message", side_effect=ValueError("not cached")),
        patch("mail_agent.mail_providers.gmail.adapter.fetch_and_cache_message", return_value=fetched),
        patch("mail_agent.mail_providers.gmail.adapter.cache_debug_info", return_value={"backend": "local"}),
    ):
        detail = get_cached_email("USER@example.com", "missing")
    assert detail["message"]["body_text"] == "Fetched body"
    print("PASS inbox feed tests")


if __name__ == "__main__":
    main()
