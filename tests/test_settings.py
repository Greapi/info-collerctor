import json
import tempfile
import unittest
from pathlib import Path

import settings as settings_module


class SettingsTests(unittest.TestCase):
    def test_priority_environment_over_config_over_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(
                json.dumps({"paths": {"archive_dir": "from-config"}, "run": {"timeout": 30}}),
                encoding="utf-8",
            )
            loaded = settings_module.load_settings(root, environ={"INFO_COLLECTOR_TIMEOUT": "45"})
            self.assertEqual(loaded.get("paths.archive_dir"), "from-config")
            self.assertEqual(loaded.setting("paths.archive_dir").source, settings_module.SOURCE_CONFIG)
            self.assertEqual(loaded.get("run.timeout"), 45)
            self.assertEqual(loaded.setting("run.timeout").source, settings_module.SOURCE_ENVIRONMENT)
            self.assertEqual(loaded.get("run.fetch_limit"), 100)
            self.assertEqual(loaded.setting("run.fetch_limit").source, settings_module.SOURCE_DEFAULT)

    def test_invalid_config_is_reported_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text("{not json", encoding="utf-8")
            loaded = settings_module.load_settings(root, environ={})
            self.assertTrue(loaded.errors)
            self.assertEqual(loaded.get("run.timeout"), 60)

    def test_invalid_environment_value_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            loaded = settings_module.load_settings(Path(tmp), environ={"INFO_COLLECTOR_TIMEOUT": "soon"})
            self.assertTrue(any("网络超时" in error for error in loaded.errors))

    def test_path_status_for_file_and_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "db.sqlite3"
            file_path.write_text("x", encoding="utf-8")
            self.assertTrue(settings_module.path_status(file_path, is_dir=False)["writable"])
            self.assertFalse(settings_module.path_status(file_path, is_dir=True)["writable"])
            self.assertTrue(settings_module.path_status(root / "missing" / "dir", is_dir=True)["writable"])

    def test_model_config_env_over_envfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                'OPENAI_API_KEY="from-file"\nOPENAI_MODEL="file-model"\nOPENAI_BASE_URL="https://file.example/v1"\n',
                encoding="utf-8",
            )
            config = settings_module.load_model_config(
                root, environ={"OPENAI_MODEL": "from-env"}
            )
            self.assertEqual(config.api_key, "from-file")
            self.assertEqual(config.model, "from-env")
            self.assertEqual(config.sources["OPENAI_MODEL"], settings_module.SOURCE_ENVIRONMENT)
            self.assertEqual(config.sources["OPENAI_API_KEY"], settings_module.SOURCE_ENVFILE)
            self.assertTrue(config.ready)

    def test_model_missing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = settings_module.load_model_config(Path(tmp), environ={})
            self.assertEqual(config.missing, ["OPENAI_API_KEY", "OPENAI_MODEL"])
            self.assertFalse(config.ready)

    def test_mail_defaults_and_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "SMTP_HOST=smtp.example.com\nSMTP_PORT=587\nMAIL_FROM=a@example.com\nMAIL_TO=b@example.com\n",
                encoding="utf-8",
            )
            config = settings_module.load_mail_config(root, environ={})
            self.assertEqual(config.port, 587)
            self.assertTrue(config.use_starttls)
            self.assertFalse(config.use_ssl)
            self.assertTrue(config.ready)
            self.assertEqual(config.sources["SMTP_PORT"], settings_module.SOURCE_ENVFILE)

    def test_secret_masking_and_text_sanitizing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("OPENAI_API_KEY=sk-super-secret\nSMTP_PASSWORD=mail-secret\n", encoding="utf-8")
            secrets = settings_module.secret_values(root, environ={})
            self.assertIn("sk-super-secret", secrets)
            masked = settings_module.mask_text("prefix sk-super-secret suffix", secrets)
            self.assertNotIn("sk-super-secret", masked)
            self.assertIn("****", masked)

    def test_default_report_date_uses_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(json.dumps({"run": {"date_offset": 0}}), encoding="utf-8")
            loaded = settings_module.load_settings(root, environ={})
            self.assertEqual(loaded.default_report_date(), __import__("datetime").date.today().isoformat())


if __name__ == "__main__":
    unittest.main()
