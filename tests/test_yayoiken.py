"""やよい軒(www.yayoiken.com)のパーサ・巡回・サイト生成のテスト。

公式ページのコピーはリポジトリに入れていないので、実物の構造を再現した合成ページ
(tests/synthetic_yayoiken.py)だけで検証する。実サイトでの試行結果は作業レポートに残す。
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
import synthetic_yayoiken as Y  # noqa: E402
from helpers import TempData  # noqa: E402

CHAIN = "yayoiken"
BASE = Y.BASE


class FakeYayoiken:
    """URLで引く偽の取得器。robots.txt は実物どおり404(差し替え可能)。"""

    def __init__(self, pages=None, robots="404"):
        self.pages = Y.yayoiken_pages() if pages is None else pages
        self.robots = robots
        self.urls: list[str] = []

    def __call__(self, chain, url):
        assert chain == CHAIN
        self.urls.append(url)
        if url == Y.ROBOTS_URL:
            if self.robots == "404":
                raise crawl.NotFoundError("HTTP 404")
            return self.robots
        if url not in self.pages:
            raise RuntimeError(f"unexpected url: {url}")
        return self.pages[url]


class YayoikenParserTests(unittest.TestCase):
    def test_menu_tabs_and_counts(self):
        es = parsers.parse_yayoiken_menu(Y.yayoiken_menu_page())
        self.assertEqual(len(es), len(Y.ALL_PIDS))
        by_pid = {e["product_id"]: e for e in es}
        self.assertEqual(by_pid["101"]["category"], "定食")
        self.assertEqual(by_pid["201"]["category"], "丼・麺・お子様")
        self.assertEqual(by_pid["301"]["category"], "朝食")
        self.assertEqual(by_pid["403"]["category"], "サイド")
        self.assertEqual(by_pid["501"]["category"], "おうち定食")   # テイクアウトも記録する
        self.assertEqual(by_pid["404"]["name"], "生ビール")          # アルコールも記録する
        self.assertEqual(by_pid["101"]["official_url"], BASE + "/menu_list/view/13/101")
        self.assertIsInstance(by_pid["101"]["price"], int)

    def test_menu_new_and_limited_section_flags(self):
        by_pid = {e["product_id"]: e for e in parsers.parse_yayoiken_menu(Y.yayoiken_menu_page())}
        self.assertTrue(by_pid["101"]["is_new"])
        self.assertFalse(by_pid["101"]["in_limited_section"])   # おすすめメニューは期間限定セクションではない
        self.assertTrue(by_pid["401"]["in_limited_section"])
        self.assertFalse(by_pid["401"]["is_new"])
        self.assertTrue(by_pid["402"]["in_limited_section"])
        self.assertTrue(by_pid["402"]["is_new"])

    def test_menu_missing_box_is_empty(self):
        self.assertEqual(parsers.parse_yayoiken_menu("<html><body>空</body></html>"), [])

    def test_news_list(self):
        items = parsers.parse_yayoiken_news_list(Y.yayoiken_news_page1())
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["url"], Y.ART_FUTURE)    # 新しい順
        self.assertEqual(items[1], {"date": "2026-11-11", "title": "「やよい軒」合成チゲ定食発売のお知らせ",
                                    "url": Y.ART_CHIGE, "category": "商品情報"})
        items2 = parsers.parse_yayoiken_news_list(Y.yayoiken_news_page2())
        self.assertEqual([i["url"] for i in items2], [Y.ART_KAKI])

    def test_article_with_launch_date(self):
        d = parsers.parse_yayoiken_news_article(Y.yayoiken_article_chige())
        self.assertIsNotNone(d)
        self.assertEqual(d["launch_date"], "2026-11-17")
        # 肩書き「～…～」は落とし、[テイクアウト] の接頭辞は付ける。後半の再掲は読まない
        self.assertEqual(d["items"], [{"name": "合成チゲ定食", "price": 990},
                                      {"name": "[テイクアウト]合成チゲ定食", "price": 990}])

    def test_article_without_launch_date(self):
        d = parsers.parse_yayoiken_news_article(Y.yayoiken_article_kaki())
        self.assertIsNotNone(d)
        self.assertIsNone(d["launch_date"])
        self.assertEqual(d["items"], [{"name": "合成かきフライ（単品）", "price": 450}])

    def test_article_non_product_is_none(self):
        self.assertIsNone(parsers.parse_yayoiken_news_article(Y.yayoiken_article_shop()))

    def test_excluded(self):
        self.assertEqual(parsers.yayoiken_excluded({"name": "", "price": 100}), "no_name")
        self.assertEqual(parsers.yayoiken_excluded({"name": "x", "price": None}), "no_price")
        self.assertIsNone(parsers.yayoiken_excluded({"name": "x", "price": 100}))

    def test_match_article_needs_name_and_price(self):
        art = parsers.parse_yayoiken_news_article(Y.yayoiken_article_chige())
        self.assertTrue(parsers.match_yayoiken_article("合成チゲ定食", 990, art))
        self.assertTrue(parsers.match_yayoiken_article("[テイクアウト]合成チゲ定食", 990, art))
        self.assertFalse(parsers.match_yayoiken_article("合成チゲ定食", 991, art))   # 価格が違えば照合しない
        self.assertFalse(parsers.match_yayoiken_article("合成定食", 990, art))
        self.assertFalse(parsers.match_yayoiken_article("合成チゲ定食", None, art))
        self.assertFalse(parsers.match_yayoiken_article("合成チゲ定食", 990, None))


class YayoikenCrawlTests(unittest.TestCase):
    def crawl_once(self, today="2026-11-25", pages=None, fake=None, regular=True):
        if not hasattr(self, "_td"):   # 同じテストの中の複数回の巡回は同じデータ置き場で続ける
            self._td = TempData(regular=regular)
            self._td.__enter__()
            self.addCleanup(self._td.__exit__, None, None, None)
            self.pending = {}
        self.fake = fake or FakeYayoiken(pages=pages)
        stats: dict = {}
        crawl.crawl_yayoiken(self.fake, self.pending, stats, today)
        self.stats = stats[CHAIN]
        self.prods = {}
        pdir = crawl.PRODUCTS / CHAIN
        if pdir.exists():
            for f in sorted(pdir.glob("*.json")):
                self.prods[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        self.baseline = crawl.baseline_for(CHAIN)
        self.state = crawl.load_json(crawl.DATA / "yayoiken_state.json", None)
        self.baseline_file_exists = (crawl.DATA / "baseline_ids.json").exists()
        self.state_file_exists = (crawl.DATA / "yayoiken_state.json").exists()
        self.ramen_state_exists = (crawl.DATA / "ramen_state.json").exists()
        return self.stats

    def test_first_run(self):
        st = self.crawl_once()
        self.assertTrue(st["list_ok"])
        prods = self.prods
        self.assertEqual(set(prods), set(Y.ALL_PIDS))
        p = prods["101"]                                           # NEWバッジ+ニュース記事で発売日が付く
        self.assertEqual(p["limited_kind"], "新メニュー")
        self.assertEqual(p["launch_date"], "2026-11-17")           # 記事の・発売日(発表日11/11ではない)
        self.assertEqual(p["launch_date_source"], "news_article")
        self.assertEqual(p["news_url"], Y.ART_CHIGE)
        self.assertEqual(p["news_date"], "2026-11-11")
        self.assertEqual(p["announced_at"], "2026-11-11")
        self.assertEqual(p["category"], "定食")
        self.assertEqual(p["chain"], CHAIN)
        self.assertIsNone(p["description"])                        # 第1段階では対象外
        t = prods["501"]                                           # テイクアウトも照合される
        self.assertEqual(t["limited_kind"], "新メニュー")
        self.assertEqual(t["launch_date"], "2026-11-17")
        k = prods["401"]                                           # 期間限定セクション。記事は発売日が読めない
        self.assertEqual(k["limited_kind"], "期間限定")
        self.assertEqual(k["badges"], ["期間限定"])
        self.assertIsNone(k["launch_date"])
        self.assertEqual(k["news_url"], Y.ART_KAKI)                # news だけ付く
        self.assertEqual(prods["402"]["limited_kind"], "期間限定")   # セクション優先(NEWでもある)
        for pid in ("102", "103", "201", "301", "403", "404", "502"):
            self.assertTrue(prods[pid]["is_regular"], pid)         # 定番(アルコール・テイクアウト含む)
        self.assertIsNone(prods["103"]["news_url"])                # 記事後半の再掲とは照合しない
        self.assertEqual(len(self.baseline), len(Y.ALL_PIDS))
        self.assertEqual(st["limited_seen"], 4)                    # 期間限定セクション2件+NEW 3件の和集合
        self.assertEqual(st["news_matched"], 3)
        self.assertEqual(st["news_unmatched"], 1)                  # 402は記事が無い

    def test_article_details_fetched_once(self):
        self.crawl_once()
        fake2 = FakeYayoiken()
        self.crawl_once(fake=fake2)
        details = [u for u in fake2.urls if "/contents/news/" in u]
        self.assertEqual(details, [])                              # 照合済みの詳細は取り直さない
        self.assertIn(Y.NEWS_URL, fake2.urls)                      # 一覧は毎回取る
        self.assertIn(Y.NEWS2_URL, fake2.urls)

    def test_future_product_matched_from_stored_article(self):
        self.crawl_once()                                          # 発売前の記事だけ state に残る
        self.assertNotIn("601", self.prods)
        items = list(Y.ITEMS) + [Y.FUTURE_ITEM]
        pages = Y.yayoiken_pages(menu=Y.yayoiken_menu_page(items=items))
        fake2 = FakeYayoiken(pages=pages)
        self.crawl_once(today="2026-12-02", fake=fake2)
        p = self.prods["601"]                                      # 一覧に現れたら state の記事で照合する
        self.assertEqual(p["limited_kind"], "新メニュー")
        self.assertEqual(p["launch_date"], "2026-12-02")
        self.assertEqual(p["launch_date_source"], "news_article")
        self.assertEqual([u for u in fake2.urls if "/contents/news/" in u], [])   # 記事は取り直さない

    def test_regular_upgraded_when_moved_to_limited_section(self):
        self.crawl_once()
        self.assertTrue(self.prods["403"]["is_regular"])
        items = [(t, Y.LIMITED_SECTION if pid == "403" else s, pid, n, pr, nw)
                 for (t, s, pid, n, pr, nw) in Y.ITEMS]
        pages = Y.yayoiken_pages(menu=Y.yayoiken_menu_page(items=items))
        self.crawl_once(today="2026-11-26", pages=pages)
        p = self.prods["403"]
        self.assertEqual(p["limited_kind"], "期間限定")
        self.assertFalse(p["is_regular"])
        self.assertIn("期間限定", p["badges"])

    def test_ended_flow(self):
        self.crawl_once()
        pages = Y.yayoiken_pages(menu=Y.yayoiken_menu_page(drop=("102",)))
        self.crawl_once(today="2026-11-26", pages=pages)
        self.assertEqual(self.prods["102"]["status"], "on_sale")   # 別の日に1回: 猶予
        self.crawl_once(today="2026-11-27", pages=pages)
        p = self.prods["102"]
        self.assertEqual(p["status"], "ended")
        self.assertEqual(p["ended_observed_at"], "2026-11-27")

    def test_list_failure_keeps_everything(self):
        pages = Y.yayoiken_pages()
        del pages[Y.MENU_URL]
        st = self.crawl_once(pages=pages)
        self.assertFalse(st["list_ok"])
        self.assertEqual(self.prods, {})
        self.assertFalse(self.baseline_file_exists)
        self.assertFalse(self.state_file_exists)

    def test_robots_disallow_menu(self):
        fake = FakeYayoiken(robots="User-agent: *\nDisallow: /menu_list\n")
        st = self.crawl_once(fake=fake)
        self.assertFalse(st["list_ok"])
        self.assertNotIn(Y.MENU_URL, fake.urls)
        self.assertEqual(self.prods, {})

    def test_price_change_recorded(self):
        self.crawl_once()
        pages = Y.yayoiken_pages(menu=Y.yayoiken_menu_page(prices={"102": 850}))
        self.crawl_once(today="2026-11-26", pages=pages)
        p = self.prods["102"]
        self.assertEqual(p["price"], 850)
        self.assertEqual(len(p["price_history"]), 2)

    def test_state_is_dedicated_file(self):
        self.crawl_once()
        self.assertEqual(self.state["menu_count"], len(Y.ALL_PIDS))
        self.assertEqual(set(self.state["articles"]), {Y.ART_CHIGE, Y.ART_KAKI, Y.ART_FUTURE})
        self.assertEqual(self.state["article_checked"], [Y.ART_SHOP])
        self.assertFalse(self.ramen_state_exists)                  # 共有ファイルには相乗りしない


class YayoikenSiteTests(unittest.TestCase):
    def test_chain_config(self):
        self.assertEqual(g.CHAIN_NAME[CHAIN], "やよい軒")
        self.assertEqual(g.CHAIN_SOURCE[CHAIN], "やよい軒公式サイト")
        self.assertEqual(g.CHAINS[-2]["slug"], CHAIN)
        self.assertTrue(g.CHAIN_LIST_TEXT.endswith("・ケンタッキー・やよい軒・CoCo壱番屋"))
        self.assertIn("--yayoiken:", g.CSS)
        self.assertIn('data-chain="yayoiken"', g.CSS)
        colors = {c["slug"]: c["color"].lower() for c in g.CHAINS}
        self.assertEqual(len(set(colors.values())), len(colors))   # 既存の色と重ならない
        self.assertEqual(notify.CHAIN_LABEL[CHAIN], "やよい軒")
        self.assertIn(CHAIN, crawl.CRAWLERS)
        self.assertEqual(crawl.CONFIG["max_requests_per_run"][CHAIN], 20)
        self.assertEqual(crawl.CONFIG["request_interval_seconds"][CHAIN], 3)
        self.assertNotIn(CHAIN, nr.TAGS)                           # ネタフルのタグは未確認なので付けない

    def test_category_icons(self):
        def icon(cat):
            return g.category_icon({"chain": CHAIN, "category": cat, "name": "合成"})[0]
        self.assertEqual(icon("定食"), "donburi")
        self.assertEqual(icon("丼・麺・お子様"), "donburi")
        self.assertEqual(icon("朝食"), "bento")
        self.assertEqual(icon("おうち定食"), "bento")
        self.assertEqual(icon("サイド"), "sozai")

    def test_rendered_item_page_notes(self):
        pending: dict = {}
        with TempData(regular=True):
            crawl.crawl_yayoiken(FakeYayoiken(), pending, {}, "2026-11-25")
            p = json.loads((crawl.PRODUCTS / CHAIN / "101.json").read_text(encoding="utf-8"))
            k = json.loads((crawl.PRODUCTS / CHAIN / "401.json").read_text(encoding="utf-8"))
        h = g.render_item_page(p)
        self.assertIn("発売日は公式サイトのニュース", h)
        self.assertIn("公式サイトのニュース", h)
        self.assertIn("やよい軒公式サイト", h)
        hk = g.render_item_page(k)
        self.assertIn("「おすすめメニュー（期間限定）」に掲載されたメニューです", hk)
        self.assertIn("お知らせ日：2026-11-04（発売日の記載が読み取れませんでした）", hk)


if __name__ == "__main__":
    unittest.main()
