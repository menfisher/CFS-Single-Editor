from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import db_storage


class DbStorageUploadTests(unittest.TestCase):
    def test_snapshot_and_verify_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "live.db"
            snapshot = root / "snapshot.db"
            conn = sqlite3.connect(source)
            try:
                conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, name TEXT)")
                conn.execute("INSERT INTO sample (name) VALUES ('alpha')")
                conn.commit()
            finally:
                conn.close()

            db_storage.snapshot_sqlite_database(source, snapshot)
            self.assertTrue(db_storage.verify_sqlite_database(snapshot))
            self.assertFalse(db_storage.verify_sqlite_database(root / "missing.db"))

            verify_conn = sqlite3.connect(snapshot)
            try:
                row = verify_conn.execute("SELECT name FROM sample").fetchone()
            finally:
                verify_conn.close()
            self.assertEqual(row[0], "alpha")

    def test_verify_rejects_corrupt_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            corrupt = Path(temp_dir) / "corrupt.db"
            corrupt.write_bytes(b"not-a-sqlite-file")
            self.assertFalse(db_storage.verify_sqlite_database(corrupt))

    def test_upload_uses_temp_blob_then_copies_to_final(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = Path(temp_dir) / "snapshot.db"
            conn = sqlite3.connect(snapshot)
            try:
                conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                conn.commit()
            finally:
                conn.close()

            temp_blob = MagicMock()
            final_blob = MagicMock()
            bucket = MagicMock()
            bucket.blob.return_value = temp_blob
            bucket.copy_blob = MagicMock(return_value=final_blob)
            client = MagicMock()
            client.bucket.return_value = bucket

            mock_storage = MagicMock()
            mock_storage.Client.return_value = client
            google_cloud = MagicMock()
            google_cloud.storage = mock_storage

            with patch.dict(
                sys.modules,
                {
                    "google.cloud": google_cloud,
                    "google.cloud.storage": mock_storage,
                },
            ):
                db_storage.upload_verified_sqlite_snapshot(
                    snapshot,
                    "gs://example-bucket/sharecontacts.db",
                )

            temp_blob.upload_from_filename.assert_called_once_with(str(snapshot))
            bucket.copy_blob.assert_called_once_with(
                temp_blob,
                bucket,
                "sharecontacts.db",
            )
            temp_blob.delete.assert_called_once_with()

    def test_upload_refuses_failed_quick_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            corrupt = Path(temp_dir) / "corrupt.db"
            corrupt.write_bytes(b"bad")
            with self.assertRaises(RuntimeError):
                db_storage.upload_verified_sqlite_snapshot(
                    corrupt,
                    "gs://example-bucket/sharecontacts.db",
                )


if __name__ == "__main__":
    unittest.main()
