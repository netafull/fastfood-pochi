"""大戸屋(www.ootoya.com)のパーサ・巡回・サイト生成のテスト。

公式ページのコピーはリポジトリに入れていないので、実物の構造を再現した合成ページ
(tests/synthetic_ootoya.py)だけで検証する。実サイトでの試行結果は作業レポートに残す。
"""

from __future__ import annotations

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
import synthetic_ootoya as O  # noqa: E402
from helpers import TempData  # noqa: E402

CHAIN = "ootoya"
BASE = O.BASE


class FakeOotoya:
    """URLで引く偽の取得器。robots.txt は実物どおり空。"""

    def __init__(self, pages=None, robots=""):
        self.pages = O.ootoya_pages() if pages is None else pages
        self.robots = robots
        self.urls: list[str] = []

    def __call__(self, chain, url):
        assert chain == CHAIN
        self.urls.append(url)
        if url == O.ROBOTS_URL:
            return self.robots
        if url not in self.pages:
            raise RuntimeError(f"unexpected url: {url}")
        return self.pages[url]


class OotoyaParserTests(unittest.TestCase):
    def test_menu_items(self):
        d = parsers.parse_ootoya_menu(O.menu_page())
        items = {i["product_id"]: i for i in d["items"]}
        self.assertEqual(set(items), set(O.ALL_PIDS))   # 別タブの重複は ID で除かれる
        kaki = items["1001"]
        self.assertEqual(kaki["name"], "合成かきフライ4個")
        self.assertEqual(kaki["category"], "限定メニュー")
        self.assertTrue(kaki["is_limited"])
        self.assertEqual(kaki["price"], 1420)   # 代表は定食価格
        self.assertEqual([(p["label"], p["price"]) for p in kaki["prices"]],
                         [("定食", 1420), ("単品", 1330)])
        self.assertEqual(kaki["official_url"], O.detail_url("1001"))
        teishoku = items["2001"]
        self.assertEqual(teishoku["category"], "定食")
        self.assertFalse(teishoku["is_limited"])
        saba = items["2002"]   # 単品価格が無い商品はバリエーションなし
        self.assertEqual(saba["prices"], [{"label": "", "price": 1050, "default": True}])
        kobachi = items["3001"]
        self.assertEqual(kobachi["category"], "小鉢")
        self.assertEqual(kobachi["price"], 150)

    def test_menu_empty(self):
        d = parsers.parse_ootoya_menu("<html><body>空</body></html>")
        self.assertEqual(d["items"], [])

    def test_news_list(self):
        items = parsers.parse_ootoya_news_list(O.news_list_page())
        self.assertEqual([i["url"] for i in items],
                         [O.ARTICLE_URL, O.CAMPAIGN_ARTICLE_URL, O.OLD_ARTICLE_URL])
        self.assertEqual(items[0]["date"], "2026-10-02")
        self.assertEqual(items[0]["category"], "商品情報")
        self.assertEqual(items[1]["category"], "お知らせ")

    def test_news_article(self):
        info = parsers.parse_ootoya_news_article(O.article_page())
        self.assertIsNotNone(info)
        self.assertEqual(info["launch_date"], "2026-10-02")   # 販売期間の開始日 (康熙部首の日付)
        self.assertEqual(info["planned_end_text"], "2027年2月28日（日）")
        by_price = {i["price"]: i for i in info["items"]}
        self.assertEqual(set(by_price), {1420, 1720})
        self.assertEqual(by_price[1420]["name"], "合成かきフライ")   # 見出しの「」内から取る

    def test_news_article_campaign_is_not_product(self):
        self.assertIsNone(parsers.parse_ootoya_news_article(O.campaign_article_page()))

    def test_match_article(self):
        info = parsers.parse_ootoya_news_article(O.article_page())
        # 記事「合成かきフライ」とメニュー「合成かきフライ4個」 (表記ゆれ + 価格一致)
        self.assertTrue(parsers.match_ootoya_article("合成かきフライ4個", 1420, info))
        # 記事「＜2個増量＞合成かきフライ (6個)」とメニュー「合成かきフライ6個」
        self.assertTrue(parsers.match_ootoya_article("合成かきフライ6個", 1720, info))
        self.assertFalse(parsers.match_ootoya_article("合成かきフライ4個", 1720, info))   # 価格が違う
        self.assertFalse(parsers.match_ootoya_article("合成しょうが焼き定食", 980, info))
        self.assertFalse(parsers.match_ootoya_article("合成かきフライ4個", None, info))

    def test_excluded(self):
        self.assertEqual(parsers.ootoya_excluded({"name": "", "price": 100}), "no_name")
        self.assertEqual(parsers.ootoya_excluded({"name": "x", "price": None}), "no_price")
        self.assertIsNone(parsers.ootoya_excluded({"name": "x", "price": 100}))


class OotoyaCrawlTests(unittest.TestCase):
    def crawl_once(self, today="2026-11-27", pages=None, fake=None):
        if not hasattr(self, "_td"):   # 同じテストの中の複数回の巡回は同じデータ置き場で続ける
            self._td = TempData(regular=True)
            self._td.__enter__()
            self.addCleanup(self._td.__exit__, None, None, None)
            self.pending = {}
        self.fake = fake or FakeOotoya(pages=pages)
        stats: dict = {}
        crawl.crawl_ootoya(self.fake, self.pending, stats, today)
        self.stats = stats[CHAIN]
        self.prods = {}
        pdir = crawl.PRODUCTS / CHAIN
        if pdir.exists():
            for f in sorted(pdir.glob("*.json")):
                self.prods[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        self.baseline = crawl.baseline_for(CHAIN)
        self.state = crawl.load_json(crawl.DATA / "ootoya_state.json", None)
        return self.stats

    def test_first_run(self):
        st = self.crawl_once()
        self.assertTrue(st["list_ok"])
        self.assertEqual(set(self.prods), set(O.ALL_PIDS))
        p = self.prods["1001"]   # 期間限定。ニュース記事から発売日が付く
        self.assertEqual(p["limited_kind"], "期間限定")
        self.assertEqual(p["badges"], ["期間限定"])
        self.assertEqual(p["launch_date"], "2026-10-02")
        self.assertEqual(p["launch_date_source"], "news_article")
        self.assertEqual(p["planned_end_text"], "2027年2月28日（日）")
        self.assertEqual(p["news_url"], O.ARTICLE_URL)
        self.assertEqual([(v["label"], v["price"]) for v in p["price_variants"]],
                         [("定食", 1420), ("単品", 1330)])
        self.assertIsNone(p["nutrition"])   # 第1段階では栄養成分は取らない
        self.assertEqual(self.prods["1002"]["launch_date"], "2026-10-02")
        teishoku = self.prods["2001"]   # 定番。単品価格はバリエーションに入る
        self.assertTrue(teishoku["is_regular"])
        self.assertEqual(teishoku["price"], 980)
        self.assertEqual([(v["label"], v["price"]) for v in teishoku["price_variants"]],
                         [("定食", 980), ("単品", 890)])
        self.assertIsNone(self.prods["2002"]["price_variants"])
        self.assertEqual(len(self.baseline), len(O.ALL_PIDS))
        self.assertEqual(self.state["menu_count"], len(O.ALL_PIDS))
        self.assertIn(O.ARTICLE_URL, self.state["articles"])
        # 「商品情報」以外の記事と古い記事は、取得せずに照合済みの印だけ付く
        self.assertIn(O.CAMPAIGN_ARTICLE_URL, self.state["article_checked"])
        self.assertIn(O.OLD_ARTICLE_URL, self.state["article_checked"])
        self.assertNotIn(O.OLD_ARTICLE_URL, self.fake.urls)
        self.assertEqual(st["limited_seen"], 2)
        self.assertEqual(st["news_matched"], 2)

    def test_ended_flow(self):
        self.crawl_once()
        menu2 = O.menu_page()
        start = menu2.find("合成さば定食")
        chunk_start = menu2.rfind('<div class="c-box-card-01__item">', 0, start)
        chunk_end = menu2.find("</a></div>", start) + len("</a></div>")
        pages = O.ootoya_pages(menu=menu2[:chunk_start] + menu2[chunk_end:])
        self.crawl_once(today="2026-11-28", pages=pages)
        self.assertEqual(self.prods["2002"]["status"], "on_sale")   # 別の日に1回: 猶予
        self.crawl_once(today="2026-11-29", pages=pages)
        p = self.prods["2002"]
        self.assertEqual(p["status"], "ended")
        self.assertEqual(p["ended_observed_at"], "2026-11-29")

    def test_list_failure_keeps_everything(self):
        st = self.crawl_once(pages={})
        self.assertFalse(st["list_ok"])
        self.assertEqual(self.prods, {})

    def test_price_change_recorded(self):
        self.crawl_once()
        menu = O.menu_page().replace(">980<span", ">1000<span")
        pages = O.ootoya_pages(menu=menu)
        self.crawl_once(today="2026-11-28", pages=pages)
        p = self.prods["2001"]
        self.assertEqual(p["price"], 1000)
        self.assertEqual(len(p["price_history"]), 2)


class OotoyaSiteTests(unittest.TestCase):
    def test_chain_config(self):
        self.assertEqual(g.CHAIN_NAME[CHAIN], "大戸屋")
        self.assertEqual(g.CHAIN_SOURCE[CHAIN], "大戸屋公式サイト")
        self.assertEqual(g.CHAINS[-1]["slug"], CHAIN)
        self.assertTrue(g.CHAIN_LIST_TEXT.endswith("・はなまるうどん・大戸屋"))
        self.assertIn("--ootoya:", g.CSS)
        self.assertIn('data-chain="ootoya"', g.CSS)
        colors = {c["slug"]: c["color"].lower() for c in g.CHAINS}
        self.assertEqual(len(set(colors.values())), len(colors))   # 既存の色と重ならない
        self.assertEqual(notify.CHAIN_LABEL[CHAIN], "大戸屋")
        self.assertIn(CHAIN, crawl.CRAWLERS)
        self.assertEqual(crawl.CONFIG["max_requests_per_run"][CHAIN], 30)
        self.assertEqual(crawl.CONFIG["request_interval_seconds"][CHAIN], 3)
        self.assertNotIn(CHAIN, nr.TAGS)   # ネタフルのタグは未確認なので付けない

    def test_category_icons(self):
        def icon(cat):
            return g.category_icon({"chain": CHAIN, "category": cat, "name": "合成"})[0]
        self.assertEqual(icon("定食"), "donburi")
        self.assertEqual(icon("限定メニュー"), "donburi")
        self.assertEqual(icon("弁当"), "bento")
        self.assertEqual(icon("小鉢"), "sozai")
        self.assertEqual(icon("デザート"), "sweets")

    def test_rendered_item_page_notes(self):
        pending: dict = {}
        with TempData(regular=True):
            crawl.crawl_ootoya(FakeOotoya(), pending, {}, "2026-11-27")
            p = json.loads((crawl.PRODUCTS / CHAIN / "1001.json").read_text(encoding="utf-8"))
        h = g.render_item_page(p)
        self.assertIn("「限定メニュー」に掲載されたメニューです", h)
        self.assertIn("「販売期間」の記載によります", h)
        self.assertIn("新宿センタービル店", h)
        self.assertIn("大戸屋公式サイト", h)


if __name__ == "__main__":
    unittest.main()
