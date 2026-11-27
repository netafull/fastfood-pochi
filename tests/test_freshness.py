"""フレッシュネスバーガー(www.freshnessburger.co.jp)のパーサ・巡回・サイト生成のテスト。

公式ページのコピーはリポジトリに入れていないので、実物の構造を再現した合成ページ
(tests/synthetic_freshness.py)だけで検証する。実サイトでの試行結果は作業レポートに残す。
プレスリリースPDFの取得・解析は crawl.fetch_freshness_press を差し替えて検証する
(ゼッテリアと同じ方式)。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import crawl  # noqa: E402
import generate_site as g  # noqa: E402
import fetch_netaful_reviews as nr  # noqa: E402
import notify  # noqa: E402
import parsers  # noqa: E402
import synthetic_freshness as F  # noqa: E402
from helpers import TempData  # noqa: E402

CHAIN = "freshness"
BASE = F.BASE


class FakeFreshness:
    """URLで引く偽の取得器。robots.txt は実物どおりトップページのHTMLが返る。"""

    def __init__(self, pages=None, robots=None):
        self.pages = F.freshness_pages() if pages is None else pages
        self.robots = F.ROBOTS_HTML if robots is None else robots
        self.urls: list[str] = []

    def __call__(self, chain, url):
        assert chain == CHAIN
        self.urls.append(url)
        if url == F.ROBOTS_URL:
            return self.robots
        if url not in self.pages:
            raise RuntimeError(f"unexpected url: {url}")
        return self.pages[url]


class FreshnessParserTests(unittest.TestCase):
    def test_menu_hamburger(self):
        d = parsers.parse_freshness_menu(F.hamburger_page(), "hamburger")
        items = {i["product_id"]: i for i in d["items"]}
        self.assertEqual(set(items), {"classic_burger", "wagyu_burger"})   # 店舗限定は除く
        classic = items["classic_burger"]
        self.assertEqual(classic["name"], "合成クラシックバーガー")   # <br> はつなげる
        self.assertEqual(classic["category"], "Premium Burger")   # 公式の見出しをそのまま使う
        self.assertEqual(classic["price"], 690)
        self.assertEqual(classic["prices"], [{"label": "", "price": 690}])
        self.assertEqual(classic["official_url"], BASE + "/menu/hamburger/classic_burger")

    def test_menu_sizes(self):
        d = parsers.parse_freshness_menu(F.sidemenu_page(), "sidemenu")
        items = {i["product_id"]: i for i in d["items"]}
        potato = items["fried_potato"]
        self.assertEqual([(p["label"], p["price"]) for p in potato["prices"]],
                         [("S", 300), ("R", 350), ("L", 450)])
        self.assertEqual(potato["price"], 300)   # 代表は先頭のサイズ
        self.assertEqual(potato["category"], "Fries")
        self.assertEqual(items["corn_potage"]["category"], "Others")

    def test_menu_card_without_detail_link(self):
        d = parsers.parse_freshness_menu(F.beverage_page(), "beverage")
        items = {i["product_id"]: i for i in d["items"]}
        cola = items["synthetic_cola"]   # 詳細リンクが無いカードは画像ファイル名がID
        self.assertEqual(cola["category"], "Soft Drink")
        self.assertEqual(cola["official_url"], F.BEVERAGE_URL)
        coffee = items["synthetic_coffee"]
        self.assertEqual(coffee["category"], "Coffee & Espresso / Tea & Milk Foam")   # 実体参照を戻す

    def test_menu_empty(self):
        d = parsers.parse_freshness_menu("<html><body>空</body></html>", "hamburger")
        self.assertEqual(d["items"], [])

    def test_news_list(self):
        items = parsers.parse_freshness_news_list(F.news_list_page())
        self.assertEqual([i["url"] for i in items],
                         [F.WAGYU_RELEASE_URL, F.NOTICE_URL, F.CORN_RELEASE_URL, F.OLD_RELEASE_URL])
        self.assertEqual(items[0]["date"], "2026-11-26")
        self.assertEqual(items[0]["category"], "おしらせ")
        self.assertEqual(items[2]["category"], "新メニュー")

    def test_press_parse(self):
        info = parsers.parse_freshness_press(F.wagyu_press_pages())
        self.assertEqual(info["published_date"], "2026-11-26")
        self.assertEqual(info["launch_date"], "2026-12-02")   # 【販売期間】の開始日
        self.assertEqual(info["planned_end_text"], "2027年1月12日（火）")
        self.assertEqual(info["items"], [{"name": "合成和牛バーガー", "price": 1080}])

    def test_press_match(self):
        info = parsers.parse_freshness_press(F.wagyu_press_pages())
        self.assertTrue(parsers.match_freshness_press("合成和牛バーガー", 1080, info))
        self.assertFalse(parsers.match_freshness_press("合成和牛バーガー", 999, info))   # 価格が違う
        self.assertFalse(parsers.match_freshness_press("合成クラシックバーガー", 1080, info))
        self.assertFalse(parsers.match_freshness_press("合成和牛バーガー", 1080, None))


class FreshnessCrawlTests(unittest.TestCase):
    def crawl_once(self, today="2026-11-27", pages=None, press="default", fake=None):
        if not hasattr(self, "_td"):   # 同じテストの中の複数回の巡回は同じデータ置き場で続ける
            self._td = TempData(regular=True)
            self._td.__enter__()
            self.addCleanup(self._td.__exit__, None, None, None)
            self.pending = {}
        self.fake = fake or FakeFreshness(pages=pages)
        if press == "default":
            press_map = {F.WAGYU_RELEASE_URL: F.wagyu_press_dict(),
                         F.CORN_RELEASE_URL: F.corn_press_dict()}
        else:
            press_map = press or {}
        self.press_fetched: list[str] = []

        def fake_press(getter, url):
            self.press_fetched.append(url)
            return press_map.get(url)

        with mock.patch.object(crawl.pdf_update, "have_pdfminer", lambda: True), \
                mock.patch.object(crawl, "fetch_freshness_press", fake_press):
            stats: dict = {}
            crawl.crawl_freshness(self.fake, self.pending, stats, today)
        self.stats = stats[CHAIN]
        self.prods = {}
        pdir = crawl.PRODUCTS / CHAIN
        if pdir.exists():
            for f in sorted(pdir.glob("*.json")):
                self.prods[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        self.baseline = crawl.baseline_for(CHAIN)
        self.state = crawl.load_json(crawl.DATA / "freshness_state.json", None)
        return self.stats

    def test_first_run(self):
        st = self.crawl_once()
        self.assertTrue(st["list_ok"])
        self.assertEqual(set(self.prods), set(F.ALL_PIDS))
        wagyu = self.prods["wagyu_burger"]   # 販売期間のあるプレスリリースで期間限定になる
        self.assertEqual(wagyu["limited_kind"], "期間限定")
        self.assertEqual(wagyu["badges"], ["期間限定"])
        self.assertEqual(wagyu["launch_date"], "2026-12-02")
        self.assertEqual(wagyu["launch_date_source"], "press_pdf")
        self.assertEqual(wagyu["planned_end_text"], "2027年1月12日（火）")
        self.assertEqual(wagyu["news_url"], F.WAGYU_RELEASE_URL)
        corn = self.prods["corn_potage"]   # 発売日だけのリリー��で新メニューになる
        self.assertEqual(corn["limited_kind"], "新メニュー")
        self.assertEqual(corn["launch_date"], "2026-11-25")
        self.assertEqual(corn["launch_date_source"], "press_pdf")
        classic = self.prods["classic_burger"]
        self.assertTrue(classic["is_regular"])
        self.assertIsNone(classic["launch_date"])
        cola = self.prods["synthetic_cola"]
        self.assertEqual(cola["price"], 260)
        self.assertEqual([(v["label"], v["price"]) for v in cola["price_variants"]],
                         [("S", 260), ("T", 330), ("G", 400)])
        self.assertEqual(len(self.baseline), len(F.ALL_PIDS))
        self.assertEqual(self.state["menu_count"], len(F.ALL_PIDS))
        self.assertIn(F.WAGYU_RELEASE_URL, self.state["releases"])
        self.assertIn(F.CORN_RELEASE_URL, self.state["releases"])
        # PDFでないお知らせと古いお知らせは、照合済みの印だけ付く (古いものは取得もしない)
        self.assertIn(F.NOTICE_URL, self.state["release_checked"])
        self.assertIn(F.OLD_RELEASE_URL, self.state["release_checked"])
        self.assertNotIn(F.OLD_RELEASE_URL, self.press_fetched)
        self.assertEqual(st["news_matched"], 2)

    def test_regular_promoted_when_release_arrives_later(self):
        pages = F.freshness_pages(news=F.news_list_page(without_wagyu=True))
        self.crawl_once(pages=pages)
        self.assertTrue(self.prods["wagyu_burger"]["is_regular"])
        self.crawl_once(today="2026-11-28")   # 和牛のプレスリリースが一覧に載った回
        wagyu = self.prods["wagyu_burger"]
        self.assertFalse(wagyu["is_regular"])
        self.assertEqual(wagyu["limited_kind"], "期間限定")
        self.assertEqual(wagyu["launch_date"], "2026-12-02")

    def test_ended_flow(self):
        self.crawl_once()
        menu = F.sidemenu_page()
        start = menu.find("/menu/sidemenu/corn_potage")
        chunk_start = menu.rfind("<a class='menulist_list'", 0, start)
        chunk_end = menu.find("</a>", start) + len("</a>")
        pages = F.freshness_pages()
        pages[F.SIDEMENU_URL] = menu[:chunk_start] + menu[chunk_end:]
        self.crawl_once(today="2026-11-28", pages=pages)
        self.assertEqual(self.prods["corn_potage"]["status"], "on_sale")   # 別の日に1回: 猶予
        self.crawl_once(today="2026-11-29", pages=pages)
        p = self.prods["corn_potage"]
        self.assertEqual(p["status"], "ended")
        self.assertEqual(p["ended_observed_at"], "2026-11-29")

    def test_partial_page_failure_skips_ended_judgement(self):
        self.crawl_once()
        pages = F.freshness_pages()
        del pages[F.BEVERAGE_URL]   # ドリンクのページだけ取れない回が続いても終了にしない
        self.crawl_once(today="2026-11-28", pages=pages)
        self.crawl_once(today="2026-11-29", pages=pages)
        self.assertEqual(self.prods["synthetic_cola"]["status"], "on_sale")

    def test_list_failure_keeps_everything(self):
        st = self.crawl_once(pages={})
        self.assertFalse(st["list_ok"])
        self.assertEqual(self.prods, {})

    def test_price_change_recorded(self):
        self.crawl_once()
        pages = F.freshness_pages()
        pages[F.HAMBURGER_URL] = F.hamburger_page().replace(">690<", ">700<")
        self.crawl_once(today="2026-11-28", pages=pages)
        p = self.prods["classic_burger"]
        self.assertEqual(p["price"], 700)
        self.assertEqual(len(p["price_history"]), 2)


class FreshnessSiteTests(unittest.TestCase):
    def test_chain_config(self):
        self.assertEqual(g.CHAIN_NAME[CHAIN], "フレッシュネスバーガー")
        self.assertEqual(g.CHAIN_SOURCE[CHAIN], "フレッシュネスバーガー公式サイト")
        self.assertEqual(g.CHAINS[-1]["slug"], CHAIN)
        self.assertTrue(g.CHAIN_LIST_TEXT.endswith("・大戸屋・フレッシュネスバーガー"))
        self.assertIn("--freshness:", g.CSS)
        self.assertIn('data-chain="freshness"', g.CSS)
        colors = {c["slug"]: c["color"].lower() for c in g.CHAINS}
        self.assertEqual(len(set(colors.values())), len(colors))   # 既存の色と重ならない
        self.assertEqual(notify.CHAIN_LABEL[CHAIN], "フレッシュネスバーガー")
        self.assertIn(CHAIN, crawl.CRAWLERS)
        self.assertEqual(crawl.CONFIG["max_requests_per_run"][CHAIN], 40)
        self.assertEqual(crawl.CONFIG["request_interval_seconds"][CHAIN], 3)
        self.assertNotIn(CHAIN, nr.TAGS)   # ネタフルのタグは未確認なので付けない

    def test_category_icons(self):
        def icon(cat):
            return g.category_icon({"chain": CHAIN, "category": cat, "name": "合成"})[0]
        self.assertEqual(icon("Premium Burger"), "burger")
        self.assertEqual(icon("Fries"), "sozai")
        self.assertEqual(icon("Soft Drink"), "drink")
        self.assertEqual(icon("Alcohol"), "alcohol")

    def test_rendered_item_page_notes(self):
        pending: dict = {}
        press_map = {F.WAGYU_RELEASE_URL: F.wagyu_press_dict(),
                     F.CORN_RELEASE_URL: F.corn_press_dict()}
        with TempData(regular=True), \
                mock.patch.object(crawl.pdf_update, "have_pdfminer", lambda: True), \
                mock.patch.object(crawl, "fetch_freshness_press",
                                  lambda getter, url: press_map.get(url)):
            crawl.crawl_freshness(FakeFreshness(), pending, {}, "2026-11-27")
            p = json.loads((crawl.PRODUCTS / CHAIN / "wagyu_burger.json").read_text(encoding="utf-8"))
        h = g.render_item_page(p)
        self.assertIn("期間限定として発表されたメニューです", h)
        self.assertIn("「販売期間」の記載によります", h)
        self.assertIn("フレッシュネスバーガー公式サイト", h)


if __name__ == "__main__":
    unittest.main()
