import http.client
import json
import os
import plistlib
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import config_ui
from runlock import RunLock, lock_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_FILES = [
    "rss_collector.py",
    "daily_report.py",
    "daily_workflow.py",
    "build_site.py",
    "settings.py",
    "feed_config.py",
    "env_file.py",
    "config_store.py",
    "runlock.py",
]

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Local</title><link>http://127.0.0.1/</link>
<item><title>One</title><link>http://127.0.0.1/1</link><guid>one</guid>
<pubDate>Fri, 06 Nov 2026 10:00:00 +0800</pubDate><description><![CDATA[<p>One body.</p>]]></description></item>
<item><title>Two</title><link>http://127.0.0.1/2</link><guid>two</guid>
<pubDate>Fri, 06 Nov 2026 11:00:00 +0800</pubDate><description><![CDATA[<p>Two body.</p>]]></description></item>
</channel></rss>"""


class UpstreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/feed.xml"):
            body = RSS
        elif self.path == "/healthz":
            body = b"ok"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if self.path.endswith("/chat/completions"):
            body = json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": "pong"}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


class Client:
    def __init__(self, port, token):
        self.port = port
        self.token = token

    def call(self, method, path, body=None, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Config-Token"] = self.token
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw.decode("utf-8", errors="replace")}
        return response.status, parsed


class ConfigUiHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in SCRIPT_FILES:
            shutil.copy2(ROOT / name, self.root / name)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.upstream_port = self.upstream.server_address[1]
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        (self.root / "feeds.txt").write_text(
            f"Local | http://127.0.0.1:{self.upstream_port}/feed.xml\n"
            "Disabled | https://disabled.example/rss | enabled=false | note=keep\n",
            encoding="utf-8",
        )
        (self.root / ".env").write_text(
            "# model\n"
            'OPENAI_API_KEY="sk-test-123456"\n'
            'OPENAI_MODEL="test-model"\n'
            f'OPENAI_BASE_URL="http://127.0.0.1:{self.upstream_port}/v1"\n'
            "# mail\n"
            'SMTP_HOST="smtp.example.com"\nSMTP_PORT="465"\n'
            'SMTP_USERNAME="a@example.com"\nSMTP_PASSWORD="secret-pass"\n'
            'SMTP_USE_SSL="true"\nSMTP_USE_STARTTLS="false"\n'
            'MAIL_FROM="a@example.com"\nMAIL_TO="b@example.com"\n'
            'MAIL_SUBJECT_PREFIX="RSS 日报"\nUNKNOWN_KEY="keep me"\n',
            encoding="utf-8",
        )
        (self.root / ".env.rsshub").write_text("RSSHUB_HOST=127.0.0.1\nRSSHUB_PORT=1200\n", encoding="utf-8")
        self.server = config_ui.create_server(self.root, token="test-token", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(self.port, "test-token")

    def tearDown(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        finally:
            self.upstream.shutdown()
            self.upstream.server_close()
            self.temp.cleanup()

    def confirm(self):
        status, payload = self.client.call("POST", "/api/legacy/confirm", {})
        self.assertEqual(status, 200, payload)

    def test_theme_init_script_is_served(self):
        status, payload = self.client.call("GET", "/theme-init.js")
        self.assertEqual(status, 200)
        self.assertIn("configUiTheme", payload.get("raw", ""))

    def test_legacy_gate_token_and_initial_save(self):
        status, payload = self.client.call("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertTrue(payload["legacy"]["detected"])
        status, _ = self.client.call("PUT", "/api/settings", {"paths": {"archive_dir": "rss-downloads"}})
        self.assertEqual(status, 409)
        status, _ = self.client.call("PUT", "/api/settings", {"paths": {"archive_dir": "rss-downloads"}}, token=False)
        self.assertEqual(status, 401)
        self.confirm()
        status, payload = self.client.call(
            "PUT",
            "/api/settings",
            {"paths": {"archive_dir": "rss-downloads"}, "run": {"timeout": 15, "fetch_limit": 50, "date_offset": 0}},
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue((self.root / "config.json").exists())
        self.assertEqual((self.root / ".env").stat().st_mode & 0o777, 0o600)

    def test_feeds_validation_conflict_and_rename(self):
        self.confirm()
        status, payload = self.client.call("GET", "/api/feeds")
        self.assertEqual(status, 200)
        revision = payload["revision"]
        feeds = [
            {"name": feed["name"], "url": feed["url"], "enabled": feed["enabled"], "note": feed["note"]}
            for feed in payload["feeds"]
        ]
        status, invalid = self.client.call(
            "PUT",
            "/api/feeds",
            {"feeds": feeds + [{"name": "Bad", "url": "ftp://bad.example/rss"}], "revision": revision},
        )
        self.assertEqual(status, 400)
        self.assertTrue(invalid["errors"])
        feeds[0]["name"] = "Local Renamed"
        status, saved = self.client.call(
            "PUT",
            "/api/feeds",
            {
                "feeds": feeds,
                "revision": revision,
                "rename": {"old_name": "Local", "new_name": "Local Renamed", "mode": "display"},
            },
        )
        self.assertEqual(status, 200, saved)
        self.assertTrue(saved["rename"]["warnings"])
        self.assertIn("Local Renamed", (self.root / "feeds.txt").read_text(encoding="utf-8"))
        status, _ = self.client.call("PUT", "/api/feeds", {"feeds": feeds, "revision": revision})
        self.assertEqual(status, 409)

    def test_test_feed_run_collect_and_archive(self):
        self.confirm()
        status, result = self.client.call(
            "POST",
            "/api/feeds/test",
            {"name": "Local", "url": f"http://127.0.0.1:{self.upstream_port}/feed.xml"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 2)
        status, run = self.client.call("POST", "/api/run", {"mode": "collect", "fetch": True, "send": False})
        self.assertEqual(status, 200, run)
        run_id = run["id"]
        offset = 0
        text = ""
        for _ in range(100):
            time.sleep(0.1)
            status, chunk = self.client.call("GET", f"/api/run/{run_id}/log?offset={offset}")
            text += chunk.get("text", "")
            offset = chunk.get("offset", offset)
            if chunk.get("done"):
                break
        self.assertIn("Done. Saved 2 new item(s).", text)
        status, run_status = self.client.call("GET", f"/api/run/{run_id}")
        self.assertEqual(run_status["status"], "done", run_status)
        self.assertEqual(run_status["summary"]["collect"]["saved"], 2)
        status, archive = self.client.call("GET", "/api/archive?date=2026-11-06")
        self.assertEqual(status, 200)
        self.assertEqual(archive["total"], 2)
        status, preview = self.client.call("GET", f"/api/archive/file?path={archive['files'][0]['path']}")
        self.assertEqual(status, 200)
        self.assertIn("# One", preview["content"])
        # External lock simulates a scheduled job already running.
        lock = RunLock(lock_path(self.root), label="定时任务", metadata={"mode": "workflow"})
        lock.acquire()
        try:
            status, busy = self.client.call("POST", "/api/run", {"mode": "collect", "fetch": False, "send": False})
            self.assertEqual(status, 409)
            self.assertIn("已有任务在运行", busy["error"])
        finally:
            lock.release()

    def test_model_test_and_integration_save_preserves_unknown_env_key(self):
        self.confirm()
        status, payload = self.client.call("GET", "/api/integrations")
        self.assertEqual(status, 200)
        status, result = self.client.call("POST", "/api/model/test", {})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"], result)
        status, saved = self.client.call(
            "PUT",
            "/api/integrations",
            {
                "model": {"base_url": payload["model"]["base_url"], "model": "renamed-model", "api_key": "****3456"},
                "mail": {
                    "host": "smtp.example.com",
                    "port": 465,
                    "username": "a@example.com",
                    "password": "****-pass",
                    "sender": "a@example.com",
                    "recipients": "b@example.com",
                    "subject_prefix": "RSS 日报",
                    "use_ssl": True,
                    "use_starttls": False,
                },
                "env_revision": payload["revisions"]["env"],
            },
        )
        self.assertEqual(status, 200, saved)
        env_text = (self.root / ".env").read_text(encoding="utf-8")
        self.assertIn("UNKNOWN_KEY", env_text)
        self.assertIn("# model", env_text)
        self.assertIn("OPENAI_MODEL=renamed-model", env_text)
        self.assertIn("sk-test-123456", env_text)

    def test_mail_test_returns_stage_error(self):
        self.confirm()
        status, result = self.client.call(
            "POST",
            "/api/mail/test",
            {
                "host": "127.0.0.1",
                "port": 1,
                "username": "a@example.com",
                "password": "x",
                "sender": "a@example.com",
                "recipients": "b@example.com",
                "use_ssl": False,
                "use_starttls": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "connect")
        self.assertTrue(result["error"])

    def test_rsshub_save_preserves_unknown_keys(self):
        self.confirm()
        env_path = self.root / ".env.rsshub"
        env_path.write_text("# keep this\nCUSTOM_KEY=1\nRSSHUB_HOST=127.0.0.1\nRSSHUB_PORT=1200\n", encoding="utf-8")
        status, payload = self.client.call("GET", "/api/integrations")
        self.assertEqual(status, 200)
        status, saved = self.client.call(
            "PUT",
            "/api/rsshub",
            {
                "host": "127.0.0.1",
                "port": 1201,
                "image": "diygod/rsshub:latest",
                "browserless_image": "browserless/chrome:latest",
                "redis_image": "redis:7-alpine",
                "proxy": "http://127.0.0.1:7890",
                "revision": payload["revisions"]["rsshub_env"],
            },
        )
        self.assertEqual(status, 200, saved)
        text = env_path.read_text(encoding="utf-8")
        self.assertIn("# keep this", text)
        self.assertIn("CUSTOM_KEY=1", text)
        self.assertIn("RSSHUB_PORT=1201", text)
        self.assertIn("RSSHUB_PROXY=http://127.0.0.1:7890", text)

    def test_saving_unchanged_mail_does_not_copy_environment_secret_into_env(self):
        self.confirm()
        status, payload = self.client.call("GET", "/api/integrations")
        self.assertEqual(status, 200)
        with mock.patch.dict(os.environ, {"SMTP_PASSWORD": "env-only-secret"}):
            status, saved = self.client.call(
                "PUT",
                "/api/integrations",
                {
                    "mail": {
                        "host": "smtp.example.com",
                        "port": 465,
                        "username": "a@example.com",
                        "password": "",
                        "sender": "a@example.com",
                        "recipients": "b@example.com",
                        "subject_prefix": "RSS 日报",
                        "use_ssl": True,
                        "use_starttls": False,
                    },
                    "env_revision": payload["revisions"]["env"],
                },
            )
            self.assertEqual(status, 200, saved)
        env_text = (self.root / ".env").read_text(encoding="utf-8")
        self.assertNotIn("env-only-secret", env_text)
        self.assertIn('SMTP_PASSWORD="secret-pass"', env_text)

    def test_scheduler_plist_uses_real_project_paths(self):
        text = self.server.context.scheduler_plist(5, 30)
        data = plistlib.loads(text.encode("utf-8"))
        self.assertEqual(data["Label"], "com.info-collector.daily")
        root = Path(self.root).resolve()
        self.assertEqual(data["WorkingDirectory"], str(root))
        self.assertEqual(data["ProgramArguments"][-1], str(root / "daily_workflow.py"))
        self.assertEqual(data["StartCalendarInterval"], {"Hour": 5, "Minute": 30})
        self.assertTrue(data["StandardOutPath"].endswith("logs/launchd.out.log"))

    def test_scheduler_non_macos_only_returns_crontab(self):
        self.confirm()
        with mock.patch.object(config_ui.sys, "platform", "linux"):
            result = self.server.context.scheduler_action({"action": "install", "confirm": True, "hour": 5, "minute": 30})
        self.assertTrue(result["skipped"])
        self.assertIn("daily_workflow.py", result["crontab"])

    def test_settings_save_and_health_and_data(self):
        self.confirm()
        status, settings = self.client.call("GET", "/api/settings")
        self.assertEqual(status, 200)
        status, saved = self.client.call(
            "PUT",
            "/api/settings",
            {
                "paths": {
                    "archive_dir": "rss-downloads",
                    "report_dir": "reports",
                    "db_path": ".rss_collector.sqlite3",
                    "site_dir": "docs",
                },
                "run": {"timeout": 20, "fetch_limit": 10, "date_offset": 0},
                "advanced": {"temperature": 0.3, "max_chars_per_article": 4000, "max_chars_per_batch": 20000},
                "schedule": {"hour": 5, "minute": 30},
                "config_revision": settings["revisions"]["config"],
            },
        )
        self.assertEqual(status, 200, saved)
        config = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["run"]["timeout"], 20)
        self.assertEqual(config["schedule"]["hour"], 5)
        status, health = self.client.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertGreater(len(health["checks"]), 3)
        status, scheduler = self.client.call("GET", "/api/scheduler")
        self.assertEqual(status, 200)
        self.assertFalse(scheduler["installed"])
        status, data = self.client.call("GET", "/api/data")
        self.assertEqual(status, 200)
        self.assertIn("db", data)


if __name__ == "__main__":
    unittest.main()
