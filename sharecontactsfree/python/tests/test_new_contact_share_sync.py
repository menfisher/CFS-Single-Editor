"""Tests for creating recipient contacts when CFS uploads a brand-new shared contact."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db
from app.services import contact_sync


OWNER = "owner@example.com"
R1 = "r1@example.com"
RESOURCE = "contactGroups/abc123"
SHARE_ID = "share-abc"
PERSON = "people/c-new-contact"


class NewContactShareSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._prev_path = db._active_database_path
        db._active_database_path = Path(self._tmpdir.name) / "test.db"
        db.init_db()
        db.save_app_data(
            OWNER,
            {
                "groups": {
                    RESOURCE: {
                        "shared": [R1],
                        "shareId": SHARE_ID,
                        "name": "Group A",
                    }
                }
            },
        )
        db.save_app_data(
            R1,
            {
                "sharedGroups": [
                    {
                        "owner": OWNER,
                        "resourceName": RESOURCE,
                        "shareId": SHARE_ID,
                        "name": "Group A",
                        "googleGroupId": "contactGroups/recipient-group",
                    }
                ]
            },
        )

    def tearDown(self) -> None:
        db._active_database_path = self._prev_path
        self._tmpdir.cleanup()

    def test_discover_mappings_for_membership_match(self) -> None:
        member_data = {
            "resourceName": PERSON,
            "memberships": [
                {"contactGroupMembership": {"contactGroupResourceName": RESOURCE}}
            ],
        }
        mappings = contact_sync._discover_mappings_for_unmapped_contact(
            owner=OWNER,
            owner_person_id=PERSON,
            member_data=member_data,
            owner_creds=object(),
        )
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0]["recipient_email"], R1)
        self.assertEqual(mappings[0]["share_id"], SHARE_ID)
        self.assertEqual(mappings[0]["group_resource_name"], RESOURCE)
        self.assertEqual(mappings[0]["owner_person_id"], PERSON)
        self.assertEqual(mappings[0]["recipient_person_id"], "")

    def test_discover_mappings_skips_when_not_in_shared_group(self) -> None:
        member_data = {
            "resourceName": PERSON,
            "memberships": [
                {
                    "contactGroupMembership": {
                        "contactGroupResourceName": "contactGroups/other"
                    }
                }
            ],
        }
        mappings = contact_sync._discover_mappings_for_unmapped_contact(
            owner=OWNER,
            owner_person_id=PERSON,
            member_data=member_data,
            owner_creds=object(),
        )
        self.assertEqual(mappings, [])

    def test_sync_contact_changes_creates_for_unmapped_new_contact(self) -> None:
        member_data = {
            "resourceName": PERSON,
            "etag": "etag-1",
            "names": [{"familyName": "Test", "givenName": "Joe"}],
            "memberships": [
                {"contactGroupMembership": {"contactGroupResourceName": RESOURCE}}
            ],
        }

        with (
            patch("app.services.contact_sync.owner_credentials", return_value=object()),
            patch(
                "app.services.contact_sync.get_people_batch",
                return_value={PERSON: member_data},
            ),
            patch(
                "app.services.contact_sync.recipient_credentials",
                return_value=object(),
            ),
            patch(
                "app.services.contact_sync._ensure_recipient_group",
                return_value="contactGroups/recipient-group",
            ),
            patch(
                "app.services.contact_sync.create_contact_in_group",
                return_value="people/recipient-new",
            ) as create_mock,
            patch("app.services.contact_sync.set_contact_photo"),
            patch("app.services.contact_sync.ensure_contact_in_group"),
        ):
            summary = contact_sync.sync_contact_changes(OWNER, [PERSON])

        self.assertEqual(summary["requested"], 1)
        self.assertEqual(summary["created"], 1)
        self.assertEqual(summary["skipped"], 0)
        self.assertEqual(summary["errors"], 0)
        create_mock.assert_called_once()
        mappings = db.find_mappings_for_owner_person(OWNER, PERSON)
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0]["recipient_person_id"], "people/recipient-new")

    def test_sync_contact_changes_still_skips_when_no_shared_group_match(self) -> None:
        member_data = {
            "resourceName": PERSON,
            "etag": "etag-1",
            "names": [{"familyName": "Test", "givenName": "Joe"}],
            "memberships": [
                {
                    "contactGroupMembership": {
                        "contactGroupResourceName": "contactGroups/unshared"
                    }
                }
            ],
        }
        with (
            patch("app.services.contact_sync.owner_credentials", return_value=object()),
            patch(
                "app.services.contact_sync.get_people_batch",
                return_value={PERSON: member_data},
            ),
        ):
            summary = contact_sync.sync_contact_changes(OWNER, [PERSON])

        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["created"], 0)
        self.assertEqual(summary["results"][0]["status"], "no_mappings")


if __name__ == "__main__":
    unittest.main()
