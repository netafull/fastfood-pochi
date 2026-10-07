import json
import unittest
from pathlib import Path

import helpers
from helpers import FakeGetter, TempData, fx
import crawl
from fetch import BlockedError


def card(code, name, price, limited=False, started=None):
    started_attr = f" data-time-limited-offer-started-at='{started}T00:00:00+09:00'" if started else ""
    hidden = "" if limited else " hidden"
    return (
        f"<div class='product-list-card grid-item-inner'>"
        f"<a data-product-code='{code}' data-mobile-redirect-href='/products/{code}/' "
        f"href='/products/{code}/'></a>"
        f"<img src='/assets/images/limit_badge.svg' data-time-limited-offer='true'{started_attr} "
        f"alt='期間限定' class='badge absolute{hidden}'></img>"
        f"<strong class='product-list-card-name font-bold'>{name}</strong>"
        f"<span class='product-list-card-price-number text-2xl'>{price}~</span></div>\n"
    )


class SynthMcd:
    """全ページが同じ商品カード一覧を返す、マクドナルドの偽サイト。"""

    def __init__(self, cards, detail="mcdonalds/p_6900.html"):
        self.cards = cards  # list[str]
        self.detail = detail
        self.calls = []
        self.fail = set()
        self.blocked = False

    def __call__(self, chain, url):
        self.calls.append(url)
        path = url.replace(crawl.MCD_BASE, "")
        if self.blocked:
            raise BlockedError("HTTP 403")
        if path in self.fail:
            raise OSError("boom")
        if path == "/robots.txt":
            return fx("mcdonalds/robots.txt")
        if path.startswith("/menu/"):
            return "<html>" + "".join(self.cards) + "</html>"
        if path.startswith("/products/"):
            return fx(self.detail)
        raise OSError(path)


def run_mcd(g, day):
    pending, stats = {}, {}
    crawl.crawl_mcdonalds(g, pending, stats, day)
    return pending, stats["mcdonalds"]


def prod(chain, pid):
    return crawl.load_product(chain, pid)


class TestRobots(unittest.TestCase):
    def test_disallowed_paths(self):
        allowed = crawl.mcd_allowed_checker(fx("mcdonalds/robots.txt"))
        self.assertTrue(allowed("/menu/"))
        self.assertTrue(allowed("/menu/burger/"))
        self.assertTrue(allowed("/products/6900/"))
        self.assertFalse(allowed("/mop/"))
        self.assertFalse(allowed("/order/"))
        self.assertFalse(allowed("/coupon-details01/"))
        self.assertFalse(allowed("/private/x"))

    def test_fallback_when_robots_missing(self):
        allowed = crawl.mcd_allowed_checker(None)
        self.assertTrue(allowed("/menu/"))
        self.assertFalse(allowed("/mop/"))
        self.assertFalse(allowed("/coupon-x/"))

    def test_user_agent_is_honest(self):
        self.assertEqual(crawl.UA, "FastfoodPochi/1.0 (+https://fastfood.netaful.jp/)")
        self.assertGreaterEqual(min(crawl.INTERVALS.values()), 3)


class TestMcdFirstRun(unittest.TestCase):
    def test_records_limited_singles_only_and_saves_baseline(self):
        with TempData() as base:
            g = FakeGetter(fallback_detail=True)
            pending, st = run_mcd(g, "2026-10-07")
            ids = {p.stem for p in (base / "products/mcdonalds").glob("*.json")}
            self.assertIn("6900", ids)
            self.assertIn("2520", ids)
            self.assertNotIn("1210", ids)  # 定番ビッグマックは詳細を取らない
            self.assertFalse(any(i.startswith("8016") or i == "801714" for i in ids))  # セット商品は対象外
            self.assertEqual(st["new_products"], len(ids))
            baseline = json.loads((base / "baseline_ids.json").read_text())
            self.assertIn("1210", baseline["mcdonalds"])
            self.assertNotIn("801714", baseline["mcdonalds"])
            # Disallowのパスには行っていない
            self.assertFalse(any("/mop/" in c or "/order/" in c for c in g.calls))
            self.assertTrue(st["list_ok"])

    def test_product_fields(self):
        with TempData():
            run_mcd(FakeGetter(fallback_detail=True), "2026-10-07")
            p = prod("mcdonalds", "6900")
            self.assertEqual(p["chain"], "mcdonalds")
            self.assertEqual(p["status"], "on_sale")
            self.assertEqual(p["price"], 460)
            self.assertEqual(p["launch_date"], "2026-09-02")  # 詳細ページの受付開始日を採用
            self.assertEqual(p["list_started_at"], "2026-08-18")
            self.assertEqual(p["limited_kind"], "期間限定")
            self.assertEqual(p["category"], "バーガー")
            self.assertEqual(p["official_url"], "https://www.mcdonalds.co.jp/products/6900/")
            self.assertEqual(p["price_history"], [{"date": "2026-10-07", "price": 460}])
            self.assertIsNone(p["image"])
            self.assertLessEqual(len(p["description"]), 100)
            self.assertEqual(p["nutrition"]["kcal"], 401.0)

    def test_second_run_does_not_refetch_details(self):
        with TempData():
            run_mcd(FakeGetter(fallback_detail=True), "2026-10-07")
            g2 = FakeGetter(fallback_detail=True)
            _, st = run_mcd(g2, "2026-10-08")
            self.assertEqual(st["detail_requests"], 0)
            self.assertEqual(st["new_products"], 0)
            self.assertFalse(any("/products/" in c for c in g2.calls))
            self.assertEqual(prod("mcdonalds", "6900")["last_seen_at"], "2026-10-08")
            # first_seen_at は実時計(now_iso)で入るので、日付の決め打ちにしない。
            # 2日目の実行で上書きされていないこと(=1回目と同じ値のまま)を確かめる
            self.assertEqual(prod("mcdonalds", "6900")["first_seen_at"][:10], crawl.today_jst())

    def test_existing_file_core_fields_not_overwritten(self):
        with TempData():
            run_mcd(FakeGetter(fallback_detail=True), "2026-10-07")
            p = prod("mcdonalds", "6900")
            p["description"] = "手で書き換えた説明"
            crawl.save_product("mcdonalds", p)
            run_mcd(FakeGetter(fallback_detail=True), "2026-10-08")
            self.assertEqual(prod("mcdonalds", "6900")["description"], "手で書き換えた説明")


class TestMcdTracking(unittest.TestCase):
    def setUp(self):
        self.ctx = TempData()
        self.base = self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.regular = card("1000", "定番バーガー", 300)
        self.limited = card("2000", "限定バーガー", 400, limited=True, started="2026-10-01")

    def test_not_ended_after_one_miss_ended_after_two_days(self):
        g = SynthMcd([self.regular, self.limited])
        run_mcd(g, "2026-10-07")
        self.assertEqual(prod("mcdonalds", "2000")["status"], "on_sale")
        g.cards = [self.regular]  # 限定バーガーが一覧から消える
        run_mcd(g, "2026-10-08")
        p = prod("mcdonalds", "2000")
        self.assertEqual(p["status"], "on_sale")  # 1回では終了にしない
        self.assertEqual(p["missed_runs"], 1)
        _, st = run_mcd(g, "2026-10-09")
        p = prod("mcdonalds", "2000")
        self.assertEqual(p["status"], "ended")
        self.assertEqual(p["ended_observed_at"], "2026-10-09")
        self.assertEqual(st["ended"], 1)

    def test_same_day_rerun_counts_once(self):
        g = SynthMcd([self.regular, self.limited])
        run_mcd(g, "2026-10-07")
        g.cards = [self.regular]
        run_mcd(g, "2026-10-08")
        run_mcd(g, "2026-10-08")  # 同じ日の再実行
        self.assertEqual(prod("mcdonalds", "2000")["status"], "on_sale")

    def test_failed_list_run_never_ends_anything(self):
        g = SynthMcd([self.regular, self.limited])
        run_mcd(g, "2026-10-07")
        g.cards = [self.regular]
        g.fail = {"/menu/side/"}  # 一覧の1ページが取れない回
        for day in ("2026-10-08", "2026-10-09", "2026-10-10"):
            _, st = run_mcd(g, day)
            self.assertFalse(st["list_ok"])
        p = prod("mcdonalds", "2000")
        self.assertEqual(p["status"], "on_sale")
        self.assertEqual(p.get("missed_runs") or 0, 0)

    def test_empty_page_counts_as_failure(self):
        g = SynthMcd([self.regular, self.limited])
        run_mcd(g, "2026-10-07")
        g.cards = []  # 構造変更でカードが0件
        for day in ("2026-10-08", "2026-10-09"):
            _, st = run_mcd(g, day)
            self.assertFalse(st["list_ok"])
        self.assertEqual(prod("mcdonalds", "2000")["status"], "on_sale")

    def test_relaunch_after_end(self):
        g = SynthMcd([self.regular, self.limited])
        run_mcd(g, "2026-10-07")
        g.cards = [self.regular]
        run_mcd(g, "2026-10-08")
        run_mcd(g, "2026-10-09")
        self.assertEqual(prod("mcdonalds", "2000")["status"], "ended")
        g.cards = [self.regular, self.limited]
        run_mcd(g, "2026-10-20")
        p = prod("mcdonalds", "2000")
        self.assertEqual(p["status"], "on_sale")
        self.assertIsNone(p["ended_observed_at"])
        self.assertEqual(p["relaunches"], [{"ended_observed_at": "2026-10-09", "reappeared_at": "2026-10-20"}])

    def test_miss_streak_resets_when_back(self):
        g = SynthMcd([self.regular, self.limited])
        run_mcd(g, "2026-10-07")
        g.cards = [self.regular]
        run_mcd(g, "2026-10-08")
        g.cards = [self.regular, self.limited]
        run_mcd(g, "2026-10-09")
        g.cards = [self.regular]
        run_mcd(g, "2026-10-10")  # 連続ではないので終了にならない
        self.assertEqual(prod("mcdonalds", "2000")["status"], "on_sale")

    def test_price_history(self):
        g = SynthMcd([self.regular, self.limited])
        run_mcd(g, "2026-10-07")
        g.cards = [self.regular, card("2000", "限定バーガー", 450, limited=True, started="2026-10-01")]
        run_mcd(g, "2026-10-09")
        run_mcd(g, "2026-10-10")  # 同じ価格なら増えない
        p = prod("mcdonalds", "2000")
        self.assertEqual([h["price"] for h in p["price_history"]], [460, 450])  # 初回は詳細ページの価格
        self.assertEqual(p["price"], 450)
        self.assertEqual(p["price_history"][1]["date"], "2026-10-09")

    def test_new_regular_menu_after_baseline_is_recorded(self):
        g = SynthMcd([self.regular])
        run_mcd(g, "2026-10-07")
        self.assertIsNone(prod("mcdonalds", "1000"))  # 定番は記録しない
        g.cards = [self.regular, card("3000", "新しい定番バーガー", 500)]  # 限定バッジなし
        _, st = run_mcd(g, "2026-10-08")
        p = prod("mcdonalds", "3000")
        self.assertIsNotNone(p)
        self.assertEqual(p["limited_kind"], "新メニュー")
        self.assertEqual(st["new_products"], 1)
        self.assertIsNone(prod("mcdonalds", "1000"))

    def test_new_set_menu_is_not_recorded(self):
        g = SynthMcd([self.regular])
        run_mcd(g, "2026-10-07")
        g.cards = [self.regular, card("3001", "新しいバーガー セット", 800)]
        run_mcd(g, "2026-10-08")
        self.assertIsNone(prod("mcdonalds", "3001"))

    def test_detail_failure_goes_to_pending_and_retries(self):
        g = SynthMcd([self.regular, self.limited])
        g.fail = {"/products/2000/"}
        pending, st = run_mcd(g, "2026-10-07")
        self.assertEqual(st["failed"], 1)
        self.assertIn("2000", pending["mcdonalds"])
        self.assertIsNone(prod("mcdonalds", "2000"))
        g.fail = set()
        run_mcd(g, "2026-10-08")  # 次回は再試行される
        self.assertIsNotNone(prod("mcdonalds", "2000"))

    def test_blocked_stops_without_ending_anything(self):
        g = SynthMcd([self.regular, self.limited])
        run_mcd(g, "2026-10-07")
        g.blocked = True
        for day in ("2026-10-08", "2026-10-09", "2026-10-10"):
            _, st = run_mcd(g, day)
            self.assertTrue(st["blocked"])
        self.assertEqual(prod("mcdonalds", "2000")["status"], "on_sale")


class TestMos(unittest.TestCase):
    def test_first_run(self):
        with TempData() as base:
            g = FakeGetter()
            pending, stats = {}, {}
            crawl.crawl_mos(g, pending, stats, "2026-10-07")
            st = stats["mos"]
            self.assertEqual(st["new_products"], 13)
            self.assertTrue(st["list_ok"])
            p = prod("mos", "010488")
            self.assertEqual(p["price"], 630)
            self.assertEqual(p["launch_date"], "2026-09-09")
            self.assertEqual(p["planned_end_text"], "2026年11月上旬頃まで")
            self.assertEqual(p["limited_kind"], "期間限定")
            self.assertEqual(p["official_url"], "https://www.mos.jp/menu/detail/?menu_id=010488&c_id=26")
            self.assertEqual(p["nutrition"]["kcal"], 520.0)
            self.assertIn("卵", p["allergens"]["contains"])
            self.assertIsNone(prod("mos", "011293")["launch_date"])
            self.assertEqual(prod("mos", "011293")["limited_kind"], "限定メニュー")
            self.assertIsNone(prod("mos", "P00060")["nutrition"])
            self.assertTrue((base / "baseline_ids.json").exists())
            # 詳細ページは取得しない(JSONだけ)
            self.assertFalse(any("/menu/detail" in c for c in g.calls))

    def test_disappear_ends_after_two_days(self):
        with TempData():
            g = FakeGetter()
            crawl.crawl_mos(g, {}, {}, "2026-10-07")
            data = json.loads(fx("mos/data_menu_menu_category_26.json"))
            data = [x for x in data if x["id"] != "010488"]
            g2 = FakeGetter(overrides={"/data/menu/menu_category/26.json": json.dumps(data)})
            crawl.crawl_mos(g2, {}, {}, "2026-10-08")
            self.assertEqual(prod("mos", "010488")["status"], "on_sale")
            stats = {}
            crawl.crawl_mos(g2, {}, stats, "2026-10-09")
            self.assertEqual(prod("mos", "010488")["status"], "ended")
            self.assertEqual(stats["mos"]["ended"], 1)

    def test_category_failure_does_not_end(self):
        with TempData():
            crawl.crawl_mos(FakeGetter(), {}, {}, "2026-10-07")
            data = [x for x in json.loads(fx("mos/data_menu_menu_category_26.json")) if x["id"] != "010488"]
            g = FakeGetter(fail={"/data/menu/menu_category/7.json"},
                           overrides={"/data/menu/menu_category/26.json": json.dumps(data)})
            for day in ("2026-10-08", "2026-10-09", "2026-10-10"):
                crawl.crawl_mos(g, {}, {}, day)
            self.assertEqual(prod("mos", "010488")["status"], "on_sale")

    def test_blocked_does_not_affect_mcdonalds_run(self):
        with TempData():
            class G(FakeGetter):
                def __call__(self, chain, url):
                    if chain == "mos":
                        raise BlockedError("HTTP 403")
                    return super().__call__(chain, url)
            g = G(fallback_detail=True)
            stats, pending = {}, {}
            crawl.crawl_mos(g, pending, stats, "2026-10-07")
            crawl.crawl_mcdonalds(g, pending, stats, "2026-10-07")
            self.assertTrue(stats["mos"]["blocked"])
            self.assertFalse(stats["mcdonalds"]["blocked"])
            self.assertGreater(stats["mcdonalds"]["new_products"], 0)

    def test_run_summary(self):
        with TempData() as base:
            stats = {"mcdonalds": {"new_products": 2, "ended": 1}, "mos": {"new_products": 0, "ended": 0}}
            crawl.save_run_summary(stats)
            data = json.loads((base / "run_summary.json").read_text())
            self.assertEqual(data["new"], {"mcdonalds": 2, "mos": 0})
            self.assertEqual(data["ended"]["mcdonalds"], 1)


if __name__ == "__main__":
    unittest.main()
