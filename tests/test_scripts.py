import json
import tempfile
import unittest
from pathlib import Path

import build_site
import daily_report
import settings as settings_module


class ScriptTests(unittest.TestCase):
    def test_load_articles_skips_disabled_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp) / "rss-downloads"
            for feed in ("enabled", "disabled"):
                directory = input_dir / feed / "2026-01-02"
                directory.mkdir(parents=True)
                (directory / "a.md").write_text(
                    "---\n"
                    f'title: "Title {feed}"\n'
                    f"feed: {feed}\n"
                    'link: "https://example.com/a"\n'
                    'published: "2026-01-02T00:00:00+00:00"\n'
                    "---\n\n# Title\n\nBody\n",
                    encoding="utf-8",
                )
            articles = daily_report.load_articles(input_dir, "2026-01-02", disabled_feeds={"disabled"})
            self.assertEqual([article.feed for article in articles], ["enabled"])

    def test_article_prompt_truncation_and_batching(self):
        article = daily_report.Article(
            title="T",
            feed="f",
            link="https://example.com",
            published="2026-01-01",
            body="x" * 200,
            path=Path("a.md"),
        )
        prompt = daily_report.article_to_prompt(article, max_chars=20)
        self.assertIn("[内容已截断]", prompt)
        batches = daily_report.build_batches([article, article], max_chars_per_article=50, max_chars_per_batch=100)
        self.assertEqual(len(batches), 2)

    def test_daily_report_parser_defaults_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(json.dumps({"run": {"date_offset": 0, "timeout": 9}}), encoding="utf-8")
            loaded = settings_module.load_settings(root, environ={})
            parser = daily_report.build_parser(loaded)
            args = parser.parse_args([])
            self.assertEqual(args.timeout, 9)
            self.assertEqual(args.date, loaded.default_report_date())
            self.assertEqual(args.input, root.resolve() / "rss-downloads")

    def test_build_site_custom_dirs_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "reports"
            reports.mkdir()
            (reports / "2026-01-02.md").write_text("# 2026-01-02 RSS 日报\n\n## 今日概览\n\n内容\n", encoding="utf-8")
            summary = root / "summary.json"
            exit_code = build_site.main(["--reports", str(reports), "--site", str(root / "site"), "--summary-json", str(summary)])
            self.assertEqual(exit_code, 0)
            self.assertTrue((root / "site" / "index.html").exists())
            self.assertTrue((root / "site" / "reports" / "2026-01-02.html").exists())
            parsed = json.loads(summary.read_text(encoding="utf-8"))
            self.assertTrue(parsed["ok"])
            self.assertEqual(parsed["reports"], 1)


if __name__ == "__main__":
    unittest.main()
