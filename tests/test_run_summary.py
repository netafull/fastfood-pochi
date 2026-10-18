"""crawl.py の実行要約(blocked / list_failed / errors / 連続日数)のテスト。ネットワーク不使用。"""
import json
import unittest
from unittest import mock

import helpers  # noqa: F401
from helpers import TempData
import crawl
from fetch import BlockedError


def _all_fail(chain, url):
    raise OSError("down")


def _blocked(chain, url):
    raise BlockedError("HTTP 403")


class TestRunSummaryProblems(unittest.TestCase):
    def test_fetch_failure_is_list_failed(self):
        pending, stats = {}, {}
        crawl.crawl_mcdonalds(_all_fail, pending, stats, "2026-10-08")
        self.assertEqual(crawl.collect_problems(stats), {"blocked": [], "list_failed": ["mcdonalds"], "errors": []})

    def test_empty_cards_is_list_failed(self):
        def g(chain, url):
            return "<html></html>" if "/menu/" in url else (_ for _ in ()).throw(OSError("x"))
        pending, stats = {}, {}
        crawl.crawl_mcdonalds(g, pending, stats, "2026-10-08")
        self.assertIn("mcdonalds", crawl.collect_problems(stats)["list_failed"])

    def test_blocked_is_not_list_failed(self):
        pending, stats = {}, {}
        crawl.crawl_mcdonalds(_blocked, pending, stats, "2026-10-08")
        self.assertEqual(crawl.collect_problems(stats), {"blocked": ["mcdonalds"], "list_failed": [], "errors": []})

    def test_suspected_restructure_is_not_list_failed(self):
        stats = {"matsuya": {"list_ok": False, "suspected_restructure": {"count": 40}}}
        self.assertEqual(crawl.collect_problems(stats)["list_failed"], [])

    def test_healthy_chain_has_no_problem_and_detail_failure_is_only_counted(self):
        stats = {"mos": {"list_ok": True, "failed": 3}}
        self.assertEqual(crawl.collect_problems(stats), {"blocked": [], "list_failed": [], "errors": []})
        with TempData() as base:
            crawl.save_run_summary(stats)
            data = json.loads((base / "run_summary.json").read_text())
        self.assertEqual(data["detail_failed"], {"mos": 3})
        self.assertEqual(data["list_failed"], [])

    def test_summary_file_has_new_fields(self):
        stats = {"mos": {"list_ok": False, "blocked": True, "failed": 2},
                 "marugame": {"list_ok": False, "failed": 1},
                 "sukiya": {"list_ok": False, "error": "RuntimeError: x"},
                 "mcdonalds": {"list_ok": True, "new_products": 1}}
        with TempData() as base:
            crawl.save_run_summary(stats, {"marugame": 2})
            data = json.loads((base / "run_summary.json").read_text())
        self.assertEqual(data["blocked"], ["mos"])
        self.assertEqual(data["list_failed"], ["marugame"])
        self.assertEqual(data["errors"], ["sukiya"])
        self.assertEqual(data["streaks"], {"marugame": 2})
        self.assertEqual(data["detail_failed"], {})  # 問題のある社の失敗は種別側に出るので重ねない
        self.assertEqual(data["new"]["mcdonalds"], 1)

    def test_main_records_exception_as_error_and_streak(self):
        def boom(getter, pending, stats, today=None):
            raise RuntimeError("bug")
        with TempData() as base, \
                mock.patch.dict(crawl.CRAWLERS, {"mos": boom}), \
                mock.patch.object(crawl, "Getter", mock.MagicMock()):
            self.assertEqual(crawl.main(["mos"]), 0)
            data = json.loads((base / "run_summary.json").read_text())
            pend = json.loads((base / "pending.json").read_text())
        self.assertEqual(data["errors"], ["mos"])
        self.assertEqual(data["streaks"], {"mos": 1})
        self.assertEqual(pend["_health"]["mos"]["days"], 1)


class TestHealthStreak(unittest.TestCase):
    def test_streak_counts_by_date(self):
        pending = {}
        bad = {"marugame": {"list_ok": False}}
        self.assertEqual(crawl.update_health(pending, bad, "2026-10-01"), {"marugame": 1})
        self.assertEqual(crawl.update_health(pending, bad, "2026-10-01"), {"marugame": 1})  # 同日の再実行
        self.assertEqual(crawl.update_health(pending, bad, "2026-10-02"), {"marugame": 2})
        self.assertEqual(crawl.update_health(pending, bad, "2026-10-03"), {"marugame": 3})
        self.assertEqual(crawl.update_health(pending, bad, "2026-10-06"), {"marugame": 1})  # 空いたら数え直し

    def test_recovery_clears_and_untouched_kept(self):
        pending = {}
        crawl.update_health(pending, {"mos": {"blocked": True}, "marugame": {"list_ok": False}}, "2026-10-01")
        crawl.update_health(pending, {"mos": {"list_ok": True}}, "2026-10-02")  # mos だけ巡回して復旧
        self.assertNotIn("mos", pending["_health"])
        self.assertEqual(pending["_health"]["marugame"]["days"], 1)
        crawl.update_health(pending, {"marugame": {"list_ok": True}}, "2026-10-02")
        self.assertNotIn("_health", pending)


if __name__ == "__main__":
    unittest.main()
