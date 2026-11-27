"""CoCo壱番屋(www.ichibanya.co.jp)のパーサ・巡回・サイト生成のテスト。

公式ページのコピーはリポジトリに入れていないので、実物の構造を再現した合成ページ
(tests/synthetic_cocoichi.py)だけで検証する。実サイトでの試行結果は作業レポートに残す。
"""

from __future__ import annotations

import datetime
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import crawl  # noqa: E402
import generate_site as g  # noqa: E402
import fetch_netaful_reviews as nr  # noqa: E402
import notify  # noqa: E402
import parsers  # noqa: E402
import synthetic_cocoichi as C  # noqa: E402
from helpers import TempData  # noqa: E402

CHAIN = "cocoichi"
BASE = C.BASE


class FakeCocoichi:
    """URLで引く偽の取得器。robots.txt は実物どおり User-agent: * のみ。"""

    def __init__(self, pages=None, robots="User-agent: *\n"):
        self.pages = C.cocoichi_pages() if pages is None else pages
        self.robots = robots
        self.urls: list[str] = []

    def __call__(self, chain, url):
        assert chain == CHAIN
        self.urls.append(url)
        if url == C.ROBOTS_URL:
            return self.robots
        if url not in self.pages:
            raise RuntimeError(f"unexpected url: {url}")
        return self.pages[url]


class CocoichiParserTests(unittest.TestCase):
    def test_list_items(self):
        pages = C.cocoichi_pages()
        d = parsers.parse_cocoichi_list(pages[C.list_url(1)])
        self.assertEqual(d["category"], "肉類のカレー")
        self.assertEqual([i["product_id"] for i in d["items"]], ["101", "102"])
        self.assertEqual(d["items"][0]["name"], "合成ポークカレー")
        self.assertEqual(d["items"][0]["price"], 710)   # 税込を取る
        self.assertFalse(d["items"][0]["is_teaser"])
        self.assertEqual(d["items"][0]["official_url"], C.detail_url("101"))

    def test_list_teaser_name_stripped(self):
        pages = C.cocoichi_pages()
        d = parsers.parse_cocoichi_list(pages[C.list_url(2)])
        self.assertEqual(d["category"], "期間/数量限定メニュー")
        teaser = {i["product_id"]: i for i in d["items"]}["202"]
        self.assertEqual(teaser["name"], "合成冬野菜カレー")   # 【予告】は外す
        self.assertTrue(teaser["is_teaser"])

    def test_list_empty(self):
        d = parsers.parse_cocoichi_list("<html><body>空</body></html>")
        self.assertEqual(d["items"], [])
        self.assertIsNone(d["category"])

    def test_detail_launch(self):
        today = datetime.date(2026, 11, 27)
        page = C.detail_page("12月1日より販売開始！")
        self.assertEqual(parsers.parse_cocoichi_detail_launch(page, today), "2026-12-01")
        self.assertIsNone(parsers.parse_cocoichi_detail_launch(C.detail_page("説明だけ"), today))

    def test_detail_launch_year_rollover(self):
        # 年末に翌年1月の予告 → 来年。予告は未来の発売日なので、1月に12月の日付が
        # 出たら今年の12月とみなす。3日以内の過去は巡回のずれとして今年扱い
        self.assertEqual(parsers.parse_cocoichi_detail_launch(
            C.detail_page("1月15日より販売開始"), datetime.date(2026, 12, 20)), "2027-01-15")
        self.assertEqual(parsers.parse_cocoichi_detail_launch(
            C.detail_page("12月20日より販売開始"), datetime.date(2027, 1, 3)), "2027-12-20")
        self.assertEqual(parsers.parse_cocoichi_detail_launch(
            C.detail_page("12月1日より販売開始"), datetime.date(2027, 1, 10)), "2027-12-01")
        self.assertEqual(parsers.parse_cocoichi_detail_launch(
            C.detail_page("11月26日より販売開始"), datetime.date(2026, 11, 27)), "2026-11-26")

    def test_excluded(self):
        self.assertEqual(parsers.cocoichi_excluded({"name": "", "price": 100}), "no_name")
        self.assertEqual(parsers.cocoichi_excluded({"name": "x", "price": None}), "no_price")
        self.assertIsNone(parsers.cocoichi_excluded({"name": "x", "price": 100}))


class CocoichiCrawlTests(unittest.TestCase):
    def crawl_once(self, today="2026-11-27", pages=None, fake=None, regular=True):
        if not hasattr(self, "_td"):   # 同じテストの中の複数回の巡回は同じデータ置き場で続ける
            self._td = TempData(regular=regular)
            self._td.__enter__()
            self.addCleanup(self._td.__exit__, None, None, None)
            self.pending = {}
        self.fake = fake or FakeCocoichi(pages=pages)
        stats: dict = {}
        crawl.crawl_cocoichi(self.fake, self.pending, stats, today)
        self.stats = stats[CHAIN]
        self.prods = {}
        pdir = crawl.PRODUCTS / CHAIN
        if pdir.exists():
            for f in sorted(pdir.glob("*.json")):
                self.prods[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        self.baseline = crawl.baseline_for(CHAIN)
        self.state = crawl.load_json(crawl.DATA / "cocoichi_state.json", None)
        return self.stats

    def test_first_run(self):
        st = self.crawl_once()
        self.assertTrue(st["list_ok"])
        self.assertEqual(set(self.prods), set(C.ALL_PIDS))
        p = self.prods["202"]   # 期間限定の【予告】。詳細ページから発売日が付く
        self.assertEqual(p["limited_kind"], "期間限定")
        self.assertEqual(p["badges"], ["期間限定"])
        self.assertEqual(p["name"], "合成冬野菜カレー")
        self.assertEqual(p["launch_date"], "2026-12-05")
        self.assertEqual(p["launch_date_source"], "menu_detail")
        self.assertEqual(p["category"], "期間/数量限定メニュー")
        t = self.prods["301"]   # 通常カテゴリーの【予告】は新メニュー
        self.assertEqual(t["limited_kind"], "新メニュー")
        self.assertEqual(t["launch_date"], "2026-12-01")
        self.assertEqual(t["launch_date_source"], "menu_detail")
        lim = self.prods["201"]   # 販売中の期間限定。発売日は付かない
        self.assertEqual(lim["limited_kind"], "期間限定")
        self.assertIsNone(lim["launch_date"])
        self.assertEqual(lim["launch_date_source"], "not_found")
        for pid in ("101", "102"):
            self.assertTrue(self.prods[pid]["is_regular"], pid)
        self.assertEqual(self.prods["101"]["price"], 710)
        self.assertEqual(len(self.baseline), len(C.ALL_PIDS))
        self.assertEqual(self.state["teaser_launch"], {"202": "2026-12-05", "301": "2026-12-01"})
        self.assertEqual(st["limited_seen"], 3)   # 期間限定一覧2件 + 予告1件(301)

    def test_detail_fetched_once(self):
        self.crawl_once()
        fake2 = FakeCocoichi()
        self.crawl_once(today="2026-11-28", fake=fake2)
        details = [u for u in fake2.urls if "detail.html" in u]
        self.assertEqual(details, [])   # 発売日が取れた予告の詳細は取り直さない

    def test_teaser_becomes_on_sale_keeping_launch_date(self):
        self.crawl_once()
        lists = dict(C.DEFAULT_LISTS)
        lists[12] = ("その他", [("301", "合成チーズカレーパン", 291, 320)])   # 【予告】が外れた
        pages = C.cocoichi_pages(lists=lists)
        self.crawl_once(today="2026-12-01", pages=pages)
        p = self.prods["301"]
        self.assertEqual(p["name"], "合成チーズカレーパン")
        self.assertEqual(p["launch_date"], "2026-12-01")   # state の発売日が残る
        self.assertEqual(p["launch_date_source"], "menu_detail")

    def test_ended_flow(self):
        self.crawl_once()
        lists = dict(C.DEFAULT_LISTS)
        lists[1] = ("肉類のカレー", [("101", "合成ポークカレー", 646, 710)])
        pages = C.cocoichi_pages(lists=lists)
        self.crawl_once(today="2026-11-28", pages=pages)
        self.assertEqual(self.prods["102"]["status"], "on_sale")   # 別の日に1回: 猶予
        self.crawl_once(today="2026-11-29", pages=pages)
        p = self.prods["102"]
        self.assertEqual(p["status"], "ended")
        self.assertEqual(p["ended_observed_at"], "2026-11-29")

    def test_list_failure_keeps_everything(self):
        st = self.crawl_once(pages={})
        self.assertFalse(st["list_ok"])
        self.assertEqual(self.prods, {})

    def test_partial_failure_no_ended_judgement(self):
        self.crawl_once()
        pages = C.cocoichi_pages()
        del pages[C.list_url(1)]   # 肉類の一覧だけ失敗させる
        self.crawl_once(today="2026-11-28", pages=pages)
        self.crawl_once(today="2026-11-29", pages=pages)
        self.assertEqual(self.prods["101"]["status"], "on_sale")   # 終了判定しない

    def test_price_change_recorded(self):
        self.crawl_once()
        lists = dict(C.DEFAULT_LISTS)
        lists[1] = ("肉類のカレー", [("101", "合成ポークカレー", 646, 710),
                                      ("102", "合成ビーフカレー", 800, 880)])
        pages = C.cocoichi_pages(lists=lists)
        self.crawl_once(today="2026-11-28", pages=pages)
        p = self.prods["102"]
        self.assertEqual(p["price"], 880)
        self.assertEqual(len(p["price_history"]), 2)


class CocoichiSiteTests(unittest.TestCase):
    def test_chain_config(self):
        self.assertEqual(g.CHAIN_NAME[CHAIN], "CoCo壱番屋")
        self.assertEqual(g.CHAIN_SOURCE[CHAIN], "CoCo壱番屋公式サイト")
        self.assertEqual(g.CHAINS[-4]["slug"], CHAIN)
        self.assertTrue(g.CHAIN_LIST_TEXT.endswith("・やよい軒・CoCo壱番屋・はなまるうどん・大戸屋・フレッシュネスバーガー"))
        self.assertIn("--cocoichi:", g.CSS)
        self.assertIn('data-chain="cocoichi"', g.CSS)
        colors = {c["slug"]: c["color"].lower() for c in g.CHAINS}
        self.assertEqual(len(set(colors.values())), len(colors))   # 既存の色と重ならない
        self.assertEqual(notify.CHAIN_LABEL[CHAIN], "CoCo壱番屋")
        self.assertIn(CHAIN, crawl.CRAWLERS)
        self.assertEqual(crawl.CONFIG["max_requests_per_run"][CHAIN], 30)
        self.assertEqual(crawl.CONFIG["request_interval_seconds"][CHAIN], 3)
        self.assertNotIn(CHAIN, nr.TAGS)   # ネタフルのタグは未確認なので付けない

    def test_category_icons(self):
        def icon(cat):
            return g.category_icon({"chain": CHAIN, "category": cat, "name": "合成"})[0]
        self.assertEqual(icon("肉類のカレー"), "curry")
        self.assertEqual(icon("期間/数量限定メニュー"), "curry")
        self.assertEqual(icon("麺類"), "noodles")
        self.assertEqual(icon("サラダ"), "salad")
        self.assertEqual(icon("ドリンク"), "drink")
        self.assertEqual(icon("デザート"), "sweets")

    def test_rendered_item_page_notes(self):
        pending: dict = {}
        with TempData(regular=True):
            crawl.crawl_cocoichi(FakeCocoichi(), pending, {}, "2026-11-27")
            p = json.loads((crawl.PRODUCTS / CHAIN / "202.json").read_text(encoding="utf-8"))
            lim = json.loads((crawl.PRODUCTS / CHAIN / "201.json").read_text(encoding="utf-8"))
        h = g.render_item_page(p)
        self.assertIn("公式サイトの商品ページに書かれていた", h)
        self.assertIn("CoCo壱番屋公式サイト", h)
        hl = g.render_item_page(lim)
        self.assertIn("「期間/数量限定メニュー」に掲載されたメニューです", hl)


if __name__ == "__main__":
    unittest.main()
