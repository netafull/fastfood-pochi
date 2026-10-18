import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401
import notify


def _summary(**kw):
    base = {"new": {}, "ended": {}, "suspected": {}, "blocked": [], "list_failed": [], "errors": [],
            "streaks": {}, "detail_failed": {}}
    base.update(kw)
    return base


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
            missing = notify.read_run_summary(Path(t) / "missing.json")
            self.assertEqual((missing["new"], missing["ended"], missing["suspected"]), ({}, {}, {}))
            self.assertEqual((missing["blocked"], missing["list_failed"], missing["errors"]), ([], [], []))

    def test_old_format_run_summary_still_readable(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "run_summary.json"
            p.write_text(json.dumps({"new": {"mos": 1}, "ended": {"mos": 0}, "suspected": {}}), encoding="utf-8")
            s = notify.read_run_summary(p)
            self.assertEqual(s["new"], {"mos": 1})
            self.assertEqual(s["list_failed"], [])
            self.assertIn("新メニュー 1件", notify.build_message(s)[0])

    def test_problem_only_message(self):
        s = _summary(list_failed=["marugame"], streaks={"marugame": 3})
        title, body = notify.build_message(s)
        self.assertEqual(title, "ファストフードポチ: 巡回に問題(丸亀製麺)")
        self.assertIn("丸亀製麺: 一覧の取得に失敗(3日連続)", body)

    def test_blocked_and_error_kinds(self):
        s = _summary(blocked=["mos"], errors=["mos", "matsuya"], streaks={"mos": 1})
        title, body = notify.build_message(s)
        self.assertIn("モスバーガー: ブロックされた・例外で停止", body)
        self.assertNotIn("1日連続", body)
        self.assertIn("松屋: 例外で停止", body)

    def test_new_plus_problem_in_one_message(self):
        s = _summary(new={"mcdonalds": 2}, blocked=["mos"], detail_failed={"mcdonalds": 1})
        title, body = notify.build_message(s)
        self.assertEqual(title, "ファストフードポチ: 新メニュー 2件・問題あり: モスバーガー")
        self.assertIn("マクドナルド 2件", body)
        self.assertIn("モスバーガー: ブロックされた", body)
        self.assertIn("詳細ページの取得失敗 1件", body)

    def test_detail_failures_alone_do_not_notify(self):
        self.assertIsNone(notify.build_message(_summary(detail_failed={"mos": 5})))

    def test_suspected_is_not_duplicated_as_problem(self):
        s = _summary(suspected={"matsuya": {"count": 40, "threshold": 10, "consecutive_days": 1}})
        title, body = notify.build_message(s)
        self.assertNotIn("巡回の問題", body)
        self.assertEqual(len(notify.build_warnings(s)), 1)

    def test_main_sends_with_high_priority_for_problem_only(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "run_summary.json"
            p.write_text(json.dumps({"new": {"mos": 0}, "ended": {"mos": 0}, "suspected": {},
                                     "blocked": ["mos"], "list_failed": [], "errors": [],
                                     "streaks": {}, "detail_failed": {}}), encoding="utf-8")
            saved = notify.RUN_SUMMARY
            notify.RUN_SUMMARY = p
            try:
                with mock.patch.object(notify, "send") as send, \
                        mock.patch.dict("os.environ", {"NTFY_TOPIC": "t"}), \
                        mock.patch.object(sys, "argv", ["notify.py", "--summary"]):
                    self.assertEqual(notify.main(), 0)
                send.assert_called_once()
                self.assertEqual(send.call_args.args[4], 4)
                p.write_text(json.dumps({"new": {"mos": 0}, "ended": {"mos": 0}}), encoding="utf-8")
                with mock.patch.object(notify, "send") as send, \
                        mock.patch.dict("os.environ", {"NTFY_TOPIC": "t"}), \
                        mock.patch.object(sys, "argv", ["notify.py", "--summary"]):
                    self.assertEqual(notify.main(), 0)
                send.assert_not_called()
            finally:
                notify.RUN_SUMMARY = saved

    def test_topic_not_in_code(self):
        src = (Path(notify.__file__)).read_text(encoding="utf-8")
        self.assertIn('os.environ.get("NTFY_TOPIC"', src)


if __name__ == "__main__":
    unittest.main()
