import datetime as dt
import unittest
from types import SimpleNamespace

import config_ui


class ActivityTests(unittest.TestCase):
    def test_classification_uses_newest_dated_entry(self):
        today = dt.date(2026, 1, 20)
        entries = [
            SimpleNamespace(title="older", published="2026-01-01T10:00:00+00:00"),
            SimpleNamespace(title="newest", published="2026-01-18T10:00:00+00:00"),
        ]
        result = config_ui.classify_activity(entries, today=today)
        self.assertEqual(result["activity"], "active")
        self.assertEqual(result["activity_label"], "活跃")
        self.assertEqual(result["latest_published"], "2026-01-18")
        self.assertEqual(result["latest_title"], "newest")
        self.assertEqual(result["age_days"], 2)

    def test_stale_dormant_unknown_and_future(self):
        today = dt.date(2026, 1, 20)
        stale = config_ui.classify_activity(
            [SimpleNamespace(title="s", published="2025-12-20T00:00:00+00:00")], today=today
        )
        self.assertEqual(stale["activity"], "stale")
        dormant = config_ui.classify_activity(
            [SimpleNamespace(title="d", published="2025-11-01T00:00:00+00:00")], today=today
        )
        self.assertEqual(dormant["activity"], "dormant")
        unknown = config_ui.classify_activity(
            [SimpleNamespace(title="u", published="")], today=today
        )
        self.assertEqual(unknown["activity"], "unknown")
        self.assertIsNone(unknown["age_days"])
        future = config_ui.classify_activity(
            [SimpleNamespace(title="f", published="2026-02-01T00:00:00+00:00")], today=today
        )
        self.assertEqual(future["activity"], "active")
        self.assertEqual(future["age_days"], 0)

    def test_activity_date_handles_rss_dates(self):
        self.assertEqual(config_ui.activity_date("Fri, 06 Nov 2026 10:00:00 +0800"), dt.date(2026, 11, 6))
        self.assertEqual(config_ui.activity_date("2026-11-06T10:00:00Z"), dt.date(2026, 11, 6))
        self.assertIsNone(config_ui.activity_date("not a date"))


if __name__ == "__main__":
    unittest.main()
