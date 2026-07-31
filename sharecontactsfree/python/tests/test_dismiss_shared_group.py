"""Tests for dismissing orphan/recovered shared groups."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import db
from app.services.app_data import remove_recipient_shared_group


class DismissSharedGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db.DATABASE_PATH = Path(self._tmpdir.name) / "test.db"
        db.init_db()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_dismiss_recovered_group_by_name_and_owner(self) -> None:
        recipient = "recipient@example.com"
        owner = "al.ms.lacontacts@gmail.com"
        db.save_app_data(
            recipient,
            {
                "sharedGroups": [
                    {
                        "owner": owner,
                        "name": "AL South - Birmingham (GRAHAM)",
                        "resourceName": "recovered/abc-123",
                        "shareId": "share-uuid-1",
                        "created": "contactGroups/old-google-id",
                        "memberCount": 4,
                        "status": "Shared (recovered)",
                    }
                ]
            },
        )

        result = remove_recipient_shared_group(
            recipient,
            owner,
            "recovered/abc-123",
            share_id="share-uuid-1",
            created="contactGroups/old-google-id",
            group_name="AL South - Birmingham (GRAHAM)",
        )

        self.assertEqual(result["sharedGroups"], [])
        stored = db.get_app_data(recipient) or {}
        self.assertEqual(stored.get("sharedGroups"), [])
        dismissed = set(stored.get("dismissedSharedGroups") or [])
        self.assertTrue(
            dismissed.intersection(
                {
                    "created:contactGroups/old-google-id",
                    "share:al.ms.lacontacts@gmail.com|share-uuid-1",
                    "group:al.ms.lacontacts@gmail.com|recovered/abc-123",
                    "name:al.ms.lacontacts@gmail.com|al south - birmingham (graham)",
                }
            )
        )


    def test_dismiss_recovered_group_with_unknown_owner(self) -> None:
        recipient = "recipient@example.com"
        ui_owner = "al.ms.lacontacts@gmail.com"
        db.save_app_data(
            recipient,
            {
                "sharedGroups": [
                    {
                        "owner": "unknown",
                        "name": "AL South - Birmingham (GRAHAM)",
                        "resourceName": "recovered/abc-123",
                        "shareId": "share-uuid-1",
                        "created": "contactGroups/old-google-id",
                        "memberCount": 4,
                        "status": "Shared (recovered)",
                    }
                ],
                "invitedByOwner": {ui_owner: "2026-01-01T00:00:00Z"},
            },
        )

        result = remove_recipient_shared_group(
            recipient,
            ui_owner,
            "recovered/abc-123",
            share_id="share-uuid-1",
            group_name="AL South - Birmingham (GRAHAM)",
        )

        self.assertEqual(result["sharedGroups"], [])
        stored = db.get_app_data(recipient) or {}
        self.assertEqual(stored.get("sharedGroups"), [])
        dismissed = set(stored.get("dismissedSharedGroups") or [])
        self.assertIn("share-id:share-uuid-1", dismissed)
        self.assertIn("name:al south - birmingham (graham)", dismissed)


if __name__ == "__main__":
    unittest.main()
