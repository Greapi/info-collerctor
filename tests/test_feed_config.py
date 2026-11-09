import tempfile
import unittest
from pathlib import Path

from feed_config import (
    Feed,
    FeedsDocument,
    disabled_archive_names,
    normalize_url,
    parse_feed_line,
    read_feeds,
    safe_name,
    source_type,
    validate_feeds,
    validate_url,
)


class FeedConfigTests(unittest.TestCase):
    def test_parse_legacy_and_extended_lines(self):
        feed = parse_feed_line("Name | https://example.com/rss", 1)
        self.assertEqual((feed.name, feed.url, feed.enabled, feed.note), ("Name", "https://example.com/rss", True, ""))
        feed = parse_feed_line("Name | https://example.com/rss | enabled=false | note=暂停 | custom=1", 2)
        self.assertFalse(feed.enabled)
        self.assertEqual(feed.note, "暂停")
        self.assertEqual(feed.extra, ("custom=1",))
        self.assertEqual(feed.archive_name, "name")

    def test_render_preserves_comments_and_extra_columns(self):
        text = "# comment\nName | https://example.com/rss | note=hi | custom=1\n\n"
        document = FeedsDocument.parse(text)
        rendered = document.render()
        self.assertIn("# comment", rendered)
        self.assertIn("Name | https://example.com/rss | note=hi | custom=1", rendered)
        self.assertTrue(rendered.endswith("\n"))

    def test_validate_duplicate_ignores_host_case_and_trailing_slash(self):
        feeds = [
            Feed(name="A", url="https://Example.com/rss/"),
            Feed(name="B", url="https://example.com/rss"),
        ]
        errors = validate_feeds(feeds)
        self.assertTrue(any(item["field"] == "url" for item in errors))
        self.assertEqual(normalize_url("https://Example.com/rss/"), "https://example.com/rss")

    def test_validate_rejects_non_http_and_missing_host(self):
        self.assertTrue(validate_url("ftp://example.com/rss"))
        self.assertTrue(validate_url("https:///rss"))
        self.assertTrue(validate_url("not a url"))
        self.assertEqual(validate_url("http://127.0.0.1:1200/rss"), "")

    def test_duplicate_archive_name_is_rejected(self):
        errors = validate_feeds([Feed(name="中文 名称", url="https://a.example/rss"), Feed(name="中文-名称", url="https://b.example/rss")])
        self.assertTrue(any(item["field"] == "name" for item in errors))

    def test_read_feeds_and_disabled_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feeds.txt"
            path.write_text("A | https://a.example/rss\nB | https://b.example/rss | enabled=false\n", encoding="utf-8")
            self.assertEqual(len(read_feeds(path)), 2)
            self.assertEqual(disabled_archive_names(path), {"b"})

    def test_source_type(self):
        self.assertEqual(source_type("http://127.0.0.1:1200/github/activity/x"), "RSSHub")
        self.assertEqual(source_type("https://example.com/rss"), "RSS/Atom")

    def test_safe_name_keeps_chinese_characters(self):
        self.assertEqual(safe_name("中文 名称"), "中文-名称")


if __name__ == "__main__":
    unittest.main()
