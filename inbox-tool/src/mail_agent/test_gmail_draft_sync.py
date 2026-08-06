import unittest

from anna_inbox_executa import v2_tools


class ComposeDraftDirectoryTests(unittest.TestCase):
    def test_directory_entry_omits_large_bodies_and_stays_below_rpc_limit(self):
        """草稿目录只能带短预览；超大 HTML 不得通过 list_compose_drafts stdout 返回。"""
        entry = v2_tools._compact_compose_draft_directory_entry({
            "id": "draft-1",
            "mailbox": "owner@example.com",
            "recipients": ["receiver@example.com"],
            "subject": "Large draft",
            "body": "x" * (80 * 1024),
            "body_html": "<p>" + ("x" * (80 * 1024)) + "</p>",
        })
        payload = {
            "mailbox": "owner@example.com",
            "count": 1,
            "drafts": [entry],
            "has_more": False,
            "next_offset": 1,
        }
        self.assertEqual(entry["body"], "")
        self.assertEqual(len(entry["body_preview"]), 120)
        self.assertNotIn("body_html", entry)
        self.assertLessEqual(
            v2_tools._compose_draft_directory_frame_size(payload),
            v2_tools.COMPOSE_DRAFT_DIRECTORY_RESPONSE_MAX_BYTES,
        )
