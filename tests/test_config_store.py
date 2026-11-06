import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import config_store
from config_store import (
    ConflictError,
    FeedValidationError,
    atomic_write,
    backup_file,
    migrate_feed_rename,
    write_config_file,
    write_env_file,
    write_feeds_file,
    write_feeds_text,
)
from feed_config import Feed


class ConfigStoreTests(unittest.TestCase):
    def test_atomic_write_and_backup_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file.txt"
            atomic_write(path, "one\n")
            for index in range(7):
                backup_file(path, keep=5)
                atomic_write(path, f"value-{index}\n")
            backups = list(Path(tmp).glob("file.txt.*.bak"))
            self.assertLessEqual(len(backups), 5)
            self.assertTrue((Path(tmp) / "file.txt.bak").exists())

    def test_env_write_preserves_comments_unknown_keys_and_sets_600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# keep\nFOO=bar\nOPENAI_MODEL=old\n", encoding="utf-8")
            with self.assertRaises(ConflictError):
                write_env_file(path, {"OPENAI_MODEL": "new"}, expected_revision=None)
            write_env_file(
                path,
                {"OPENAI_MODEL": "new", "SMTP_HOST": "smtp.example.com"},
                expected_revision=config_store.file_revision(path),
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn("# keep", text)
            self.assertIn("FOO=bar", text)
            self.assertIn("OPENAI_MODEL=new", text)
            self.assertIn("SMTP_HOST=smtp.example.com", text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue((Path(tmp) / ".env.bak").exists())

    def test_conflict_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("A=1\n", encoding="utf-8")
            revision = config_store.file_revision(path)
            path.write_text("A=2\n", encoding="utf-8")
            with self.assertRaises(ConflictError):
                write_env_file(path, {"A": "3"}, expected_revision=revision)

    def test_feeds_write_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feeds.txt"
            text = "# comment\nA | https://a.example/rss\n"
            path.write_text(text, encoding="utf-8")
            write_feeds_file(
                path,
                [Feed(name="A", url="https://a.example/rss"), Feed(name="B", url="https://b.example/rss")],
                expected_revision=config_store.file_revision(path),
            )
            self.assertIn("# comment", path.read_text(encoding="utf-8"))
            self.assertIn("B | https://b.example/rss", path.read_text(encoding="utf-8"))
            with self.assertRaises(FeedValidationError):
                write_feeds_file(path, [Feed(name="C", url="ftp://bad")], expected_revision=config_store.file_revision(path))
            write_feeds_text(
                path,
                "# imported\nC | https://c.example/rss\n",
                expected_revision=config_store.file_revision(path),
            )
            self.assertIn("# imported", path.read_text(encoding="utf-8"))

    def test_config_write_preserves_unknown_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"paths": {"archive_dir": "old"}, "custom": {"x": 1}}), encoding="utf-8")
            write_config_file(
                path,
                {"paths": {"report_dir": "reports"}, "run": {"timeout": 30}},
                expected_revision=config_store.file_revision(path),
            )
            parsed = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["paths"]["archive_dir"], "old")
            self.assertEqual(parsed["paths"]["report_dir"], "reports")
            self.assertEqual(parsed["custom"], {"x": 1})

    def test_rename_rekey_moves_archive_and_rewrites_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "rss-downloads"
            db_path = root / ".rss_collector.sqlite3"
            old_file = archive / "old-feed" / "2026-01-01" / "a.md"
            old_file.parent.mkdir(parents=True)
            old_file.write_text("# A\n", encoding="utf-8")
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE entries (feed_name TEXT NOT NULL, entry_id TEXT NOT NULL, link TEXT NOT NULL, title TEXT NOT NULL, file_path TEXT NOT NULL, saved_at TEXT NOT NULL, PRIMARY KEY (feed_name, entry_id))"
            )
            connection.execute(
                "INSERT INTO entries VALUES (?,?,?,?,?,?)",
                ("old-feed", "1", "https://a.example/1", "A", str(old_file), "2026-01-01T00:00:00+00:00"),
            )
            connection.commit()
            connection.close()
            result = migrate_feed_rename("Old Feed", "New Feed", "rekey", archive, db_path)
            self.assertEqual(result["moved_files"], 1)
            new_file = archive / "new-feed" / "2026-01-01" / "a.md"
            self.assertTrue(new_file.exists())
            self.assertFalse(old_file.exists())
            connection = sqlite3.connect(db_path)
            row = connection.execute("SELECT feed_name, file_path FROM entries").fetchone()
            connection.close()
            self.assertEqual(row[0], "new-feed")
            self.assertIn("new-feed", row[1])

    def test_rename_display_only_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = migrate_feed_rename("Old Feed", "New Feed", "display", root / "rss-downloads", root / "db.sqlite3")
            self.assertEqual(result["moved_files"], 0)
            self.assertTrue(result["warnings"])


if __name__ == "__main__":
    unittest.main()
