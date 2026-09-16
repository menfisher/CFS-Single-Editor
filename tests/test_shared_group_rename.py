import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from app.services.shared_group_rename_service import (
    apply_shared_contacts_group_rename,
    group_names_equivalent,
    replace_membership_group_name,
    sync_share_invite_display_name,
)


class SharedGroupRenameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE contacts (
              id INTEGER PRIMARY KEY,
              group_membership TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self.conn.commit()

        def fake_fetch_all(query, params=()):
            return [dict(row) for row in self.conn.execute(query, params).fetchall()]

        @contextmanager
        def fake_get_connection():
            yield self.conn

        self.patches = [
            patch("app.services.shared_group_rename_service.fetch_all", fake_fetch_all),
            patch("app.services.shared_group_rename_service.get_connection", fake_get_connection),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in self.patches:
            item.stop()
        self.conn.close()

    def test_group_names_equivalent_ignores_shared_suffix(self) -> None:
        self.assertTrue(group_names_equivalent("TriState Separates (Shared)", "TriState Separates"))
        self.assertFalse(group_names_equivalent("TriState Separates (Shared)", "AL South - Birmingham (Shared)"))

    def test_replace_membership_keeps_field_meeting_groups(self) -> None:
        current = "AL South - Birmingham (Shared):::TriState Separates (Shared):::myContacts"
        next_value = replace_membership_group_name(
            current,
            "TriState Separates (Shared)",
            "AlMsLa (Shared)",
        )
        self.assertEqual(
            next_value,
            "AL South - Birmingham (Shared):::AlMsLa (Shared):::myContacts",
        )

    def test_apply_rename_updates_local_and_calls_share_without_contact_sync(self) -> None:
        self.conn.execute(
            "INSERT INTO contacts (id, group_membership) VALUES (1, ?)",
            ("AL South - Birmingham (Shared):::TriState Separates (Shared):::myContacts",),
        )
        self.conn.commit()
        with patch(
            "app.services.shared_group_rename_service._load_google_account",
            return_value={"account_status": "connected"},
        ), patch(
            "app.services.shared_group_rename_service._get_valid_access_token",
            return_value=("token", "owner@example.com"),
        ), patch(
            "app.services.shared_group_rename_service._contact_groups_lookup",
            return_value=(
                {"contactGroups/abc": "TriState Separates (Shared)"},
                {"TriState Separates (Shared)": "contactGroups/abc"},
            ),
        ), patch(
            "app.services.shared_group_rename_service._rename_owner_google_group"
        ) as rename_owner, patch(
            "app.services.shared_group_rename_service.rename_share_app_group",
            return_value={"ok": True, "result": {"recipients_updated": 5, "google_renamed": 5}},
        ) as rename_share, patch(
            "app.services.shared_group_rename_service._remember_rename_pending",
        ):
            result = apply_shared_contacts_group_rename(
                "TriState Separates (Shared)",
                "AlMsLa (Shared)",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["notice_key"], "shared_group_renamed")
        self.assertEqual(result["local_updated"], 1)
        self.assertEqual(result["resource_name"], "contactGroups/abc")
        rename_owner.assert_called_once_with("token", "contactGroups/abc", "AlMsLa (Shared)")
        rename_share.assert_called_once_with(
            "owner@example.com",
            "contactGroups/abc",
            "TriState Separates (Shared)",
            "AlMsLa (Shared)",
        )
        row = dict(self.conn.execute("SELECT group_membership FROM contacts WHERE id = 1").fetchone())
        self.assertEqual(
            row["group_membership"],
            "AL South - Birmingham (Shared):::AlMsLa (Shared):::myContacts",
        )

    def test_sync_invite_display_name_posts_same_old_and_new(self) -> None:
        with patch(
            "app.services.shared_group_rename_service._load_google_account",
            return_value={"account_email": "owner@example.com"},
        ), patch(
            "app.services.shared_group_rename_service.get_pending_shared_group_rename",
            return_value={"resource_name": "contactGroups/abc"},
        ), patch(
            "app.services.shared_group_rename_service.rename_share_app_group",
            return_value={"ok": True, "display_name_saved": True},
        ) as rename_share:
            result = sync_share_invite_display_name("AlMsLa (Shared)")

        self.assertEqual(result, {"ok": True, "display_name_saved": True})
        rename_share.assert_called_once_with(
            "owner@example.com",
            "contactGroups/abc",
            "AlMsLa (Shared)",
            "AlMsLa (Shared)",
        )


if __name__ == "__main__":
    unittest.main()
