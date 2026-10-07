import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
import notify


class TestNotify(unittest.TestCase):
    def test_no_news_no_message(self):
        self.assertIsNone(notify.build_message({"new": {"mcdonalds": 0, "mos": 0}, "ended": {"mcdonalds": 0}}))

    def test_new_and_ended(self):
        title, body = notify.build_message({"new": {"mcdonalds": 2, "mos": 1}, "ended": {"mos": 3}})
        self.assertIn("新メニュー 3件", title)
        self.assertIn("終了 3件", title)
        self.assertIn("マクドナルド 2件", body)
        self.assertIn("モスバーガー 3件", body)

    def test_reads_run_summary_not_dates(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "run_summary.json"
            p.write_text(json.dumps({"new": {"mos": 4}, "ended": {}}), encoding="utf-8")
            self.assertEqual(notify.read_run_summary(p)["new"], {"mos": 4})
            self.assertEqual(notify.read_run_summary(Path(t) / "missing.json"), {"new": {}, "ended": {}})

    def test_topic_not_in_code(self):
        src = (Path(notify.__file__)).read_text(encoding="utf-8")
        self.assertIn('os.environ.get("NTFY_TOPIC"', src)


if __name__ == "__main__":
    unittest.main()
