"""はなまるうどん(www.hanamaruudon.com)のパーサ・巡回・サイト生成のテスト。

公式ページのコピーはリポジトリに入れていないので、実物の構造を再現した合成ページ
(tests/synthetic_hanamaru.py)だけで検証する。実サイトでの試行結果は作業レポートに残す。
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
import synthetic_hanamaru as H  # noqa: E402
from helpers import TempData  # noqa: E402

CHAIN = "hanamaru"
BASE = H.BASE


class FakeHanamaru:
    """URLで引く偽の取得器。robots.txt は実物どおり /test/ のみ Disallow。"""

    def __init__(self, pages=None, robots="User-agent: *\nDisallow: /test/\n"):
        self.pages = H.hanamaru_pages() if pages is None else pages
        self.robots = robots
        self.urls: list[str] = []

    def __call__(self, chain, url):
        assert chain == CHAIN
        self.urls.append(url)
        if url == H.ROBOTS_URL:
            return self.robots
        if url not in self.pages:
            raise RuntimeError(f"unexpected url: {url}")
        return self.pages[url]


class HanamaruParserTests(unittest.TestCase):
    def test_menu_items(self):
        d = parsers.parse_hanamaru_menu(H.menu_page())
        items = {i["product_id"]: i for i in d["items"]}
        self.assertEqual(set(items), set(H.ALL_PIDS))
        main = items[H.PID_MAIN]   # 期間限定の特集商品 (詳細ページなし)
        self.assertEqual(main["name"], "合成かもねぎうどん")
        self.assertEqual(main["category"], "期間限定メニュー")
        self.assertTrue(main["is_limited"])
        self.assertTrue(main["is_new"])
        self.assertEqual(main["price"], 890)   # 代表は「小」
        self.assertEqual([(p["label"], p["price"]) for p in main["prices"]],
                         [("小", 890), ("中", 1080), ("大", 1300)])
        self.assertEqual(main["official_url"], H.CAMPAIGN_URL)
        self.assertIsNone(main["detail_path"])
        sub = items[H.PID_SUB]
        self.assertEqual(sub["name"], "合成かもせいろ")
        self.assertEqual(sub["price"], 790)
        kitsune = items["02_2"]
        self.assertEqual(kitsune["name"], "合成きつね")
        self.assertEqual(kitsune["category"], "かけうどん系")
        self.assertFalse(kitsune["is_limited"])
        self.assertEqual(kitsune["price"], 580)
        self.assertEqual(kitsune["official_url"], H.detail_url("02_2"))
        chikuwa = items["05_5"]   # サイドは単品価格 (ラベルなし)
        self.assertEqual(chikuwa["category"], "天ぷら・おでん・その他")
        self.assertEqual(chikuwa["prices"], [{"label": "", "price": 130}])
        bento = items["06_1"]
        self.assertEqual(bento["category"], "テイクアウト専用")
        self.assertEqual(bento["price"], 500)

    def test_menu_commented_out_item_ignored(self):
        d = parsers.parse_hanamaru_menu(H.menu_page())
        names = [i["name"] for i in d["items"]]
        self.assertNotIn("合成終了うどん", names)   # HTMLコメント内の旧商品は拾わない

    def test_menu_empty(self):
        d = parsers.parse_hanamaru_menu("<html><body>空</body></html>")
        self.assertEqual(d["items"], [])

    def test_detail_sizes_and_kcal(self):
        pages = H.hanamaru_pages()
        d = parsers.parse_hanamaru_detail(pages[H.detail_url("02_2")])
        self.assertEqual(d["name"], "合成きつね")
        self.assertEqual([(s["label"], s["price"], s["kcal"]) for s in d["sizes"]],
                         [("小", 580, 404.0), ("中", 770, 686.0), ("大", 990, 968.0)])
        single = parsers.parse_hanamaru_detail(pages[H.detail_url("05_5")])
        self.assertEqual([(s["label"], s["price"], s["kcal"]) for s in single["sizes"]],
                         [("", 130, 156.0)])

    def test_news_list(self):
        items = parsers.parse_hanamaru_news_list(H.news_list_page())
        self.assertEqual([i["url"] for i in items], [H.CAMPAIGN_ARTICLE_URL, H.ARTICLE_URL])
        self.assertEqual(items[1]["date"], "2026-10-21")
        self.assertEqual(items[1]["category"], "期間限定メニュー")
        self.assertIn("「合成かもねぎうどん」", items[1]["title"])   # <br> は空白になる

    def test_news_article(self):
        info = parsers.parse_hanamaru_news_article(H.article_page())
        self.assertIsNotNone(info)
        self.assertEqual(info["launch_date"], "2026-10-22")   # 販売期間の開始日
        self.assertEqual(info["planned_end_text"], "12月初旬（予定）")
        by_name = {i["name"]: i for i in info["items"]}
        self.assertEqual(set(by_name), {"合成かもねぎうどん", "合成かもせいろ"})   # （温）などの注記は落ちる
        self.assertEqual(by_name["合成かもねぎうどん"]["price"], 890)
        self.assertEqual([(p["label"], p["price"]) for p in by_name["合成かもせいろ"]["prices"]],
                         [("小", 790), ("中", 980), ("大", 1200)])

    def test_news_article_campaign_is_not_product(self):
        self.assertIsNone(parsers.parse_hanamaru_news_article(H.campaign_article_page()))

    def test_match_article(self):
        info = parsers.parse_hanamaru_news_article(H.article_page())
        self.assertTrue(parsers.match_hanamaru_article("合成かもねぎうどん", 890, info))
        self.assertFalse(parsers.match_hanamaru_article("合成かもねぎうどん", 900, info))   # 価格が違う
        self.assertFalse(parsers.match_hanamaru_article("合成きつね", 580, info))
        self.assertFalse(parsers.match_hanamaru_article("合成かもねぎうどん", None, info))

    def test_limited_id_comes_from_name_not_image(self):
        # 画像ファイル名は枠ごとの使い回しなので、同じ画像名でも商品名が違えば別の ID になる
        d1 = parsers.parse_hanamaru_menu(H.menu_page())
        ids1 = {i["name"]: i["product_id"] for i in d1["items"]}
        renamed = H.menu_page().replace("合成かもねぎうどん", "合成あたらしいうどん")
        d2 = parsers.parse_hanamaru_menu(renamed)
        ids2 = {i["name"]: i["product_id"] for i in d2["items"]}
        self.assertNotEqual(ids2["合成あたらしいうどん"], ids1["合成かもねぎうどん"])
        self.assertEqual(ids2["合成あたらしいうどん"],
                         parsers.hanamaru_campaign_id("合成あたらしいうどん"))
        # 名前が同じなら ID は安定する (次の季節に戻ってきたら同じ記録に付く)
        self.assertEqual(ids1["合成かもせいろ"], parsers.hanamaru_campaign_id("合成かもせいろ"))
        self.assertEqual(ids1["合成かもねぎうどん"], H.PID_MAIN)

    def test_excluded(self):
        self.assertEqual(parsers.hanamaru_excluded({"name": "", "price": 100}), "no_name")
        self.assertEqual(parsers.hanamaru_excluded({"name": "x", "price": None}), "no_price")
        self.assertIsNone(parsers.hanamaru_excluded({"name": "x", "price": 100}))


class HanamaruCrawlTests(unittest.TestCase):
    def crawl_once(self, today="2026-11-27", pages=None, fake=None):
        if not hasattr(self, "_td"):   # 同じテストの中の複数回の巡回は同じデータ置き場で続ける
            self._td = TempData(regular=True)
            self._td.__enter__()
            self.addCleanup(self._td.__exit__, None, None, None)
            self.pending = {}
        self.fake = fake or FakeHanamaru(pages=pages)
        stats: dict = {}
        crawl.crawl_hanamaru(self.fake, self.pending, stats, today)
        self.stats = stats[CHAIN]
        self.prods = {}
        pdir = crawl.PRODUCTS / CHAIN
        if pdir.exists():
            for f in sorted(pdir.glob("*.json")):
                self.prods[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        self.baseline = crawl.baseline_for(CHAIN)
        self.state = crawl.load_json(crawl.DATA / "hanamaru_state.json", None)
        return self.stats

    def test_first_run(self):
        st = self.crawl_once()
        self.assertTrue(st["list_ok"])
        self.assertEqual(set(self.prods), set(H.ALL_PIDS))
        p = self.prods[H.PID_MAIN]   # 期間限定。ニュース記事から発売日が付く
        self.assertEqual(p["limited_kind"], "期間限定")
        self.assertEqual(p["badges"], ["期間限定"])
        self.assertEqual(p["launch_date"], "2026-10-22")
        self.assertEqual(p["launch_date_source"], "news_article")
        self.assertEqual(p["planned_end_text"], "12月初旬（予定）")
        self.assertEqual(p["news_url"], H.ARTICLE_URL)
        self.assertEqual([(v["label"], v["price"]) for v in p["price_variants"]],
                         [("小", 890), ("中", 1080), ("大", 1300)])
        self.assertIsNone(p["nutrition"])   # 期間限定は詳細ページが無いので kcal なし
        sub = self.prods[H.PID_SUB]
        self.assertEqual(sub["launch_date"], "2026-10-22")
        kitsune = self.prods["02_2"]   # 定番。詳細から全サイズと kcal が入る
        self.assertTrue(kitsune["is_regular"])
        self.assertEqual(kitsune["price"], 580)
        self.assertEqual([(v["label"], v["price"]) for v in kitsune["price_variants"]],
                         [("小", 580), ("中", 770), ("大", 990)])
        self.assertEqual(kitsune["nutrition"]["kcal"], 404.0)
        self.assertEqual(len(kitsune["nutrition_variants"]), 3)
        chikuwa = self.prods["05_5"]
        self.assertIsNone(chikuwa["price_variants"])   # 単品価格は variants にしない
        self.assertEqual(chikuwa["nutrition"]["kcal"], 156.0)
        self.assertEqual(len(self.baseline), len(H.ALL_PIDS))
        self.assertEqual(self.state["menu_count"], len(H.ALL_PIDS))
        self.assertIn(H.ARTICLE_URL, self.state["articles"])
        self.assertIn(H.CAMPAIGN_ARTICLE_URL, self.state["article_checked"])
        self.assertEqual(st["limited_seen"], 2)
        self.assertEqual(st["news_matched"], 2)

    def test_detail_not_refetched_for_existing(self):
        self.crawl_once()
        fake2 = FakeHanamaru()
        self.crawl_once(today="2026-11-28", fake=fake2)
        details = [u for u in fake2.urls if "/menu/0" in u]
        self.assertEqual(details, [])   # 詳細は初回記録のときだけ

    def test_ended_flow(self):
        self.crawl_once()
        # 02_5 の項目をメニューから丸ごと落として、消えたことにする
        menu2 = H.menu_page()
        start = menu2.find("合成かけ")
        chunk_start = menu2.rfind('<li class="menu-allstar__list__item">', 0, start)
        chunk_end = menu2.find("</li>", start) + len("</li>")
        pages = H.hanamaru_pages(menu=menu2[:chunk_start] + menu2[chunk_end:])
        self.crawl_once(today="2026-11-28", pages=pages)
        self.assertEqual(self.prods["02_5"]["status"], "on_sale")   # 別の日に1回: 猶予
        self.crawl_once(today="2026-11-29", pages=pages)
        p = self.prods["02_5"]
        self.assertEqual(p["status"], "ended")
        self.assertEqual(p["ended_observed_at"], "2026-11-29")

    def test_list_failure_keeps_everything(self):
        st = self.crawl_once(pages={})
        self.assertFalse(st["list_ok"])
        self.assertEqual(self.prods, {})

    def test_price_change_recorded(self):
        self.crawl_once()
        menu = H.menu_page().replace("<span>580</span>", "<span>600</span>")
        pages = H.hanamaru_pages(menu=menu)
        self.crawl_once(today="2026-11-28", pages=pages)
        p = self.prods["02_2"]
        self.assertEqual(p["price"], 600)
        self.assertEqual(len(p["price_history"]), 2)


class HanamaruSiteTests(unittest.TestCase):
    def test_chain_config(self):
        self.assertEqual(g.CHAIN_NAME[CHAIN], "はなまるうどん")
        self.assertEqual(g.CHAIN_SOURCE[CHAIN], "はなまるうどん公式サイト")
        self.assertEqual(g.CHAINS[-2]["slug"], CHAIN)
        self.assertTrue(g.CHAIN_LIST_TEXT.endswith("・CoCo壱番屋・はなまるうどん・大戸屋"))
        self.assertIn("--hanamaru:", g.CSS)
        self.assertIn('data-chain="hanamaru"', g.CSS)
        colors = {c["slug"]: c["color"].lower() for c in g.CHAINS}
        self.assertEqual(len(set(colors.values())), len(colors))   # 既存の色と重ならない
        self.assertEqual(notify.CHAIN_LABEL[CHAIN], "はなまるうどん")
        self.assertIn(CHAIN, crawl.CRAWLERS)
        self.assertEqual(crawl.CONFIG["max_requests_per_run"][CHAIN], 40)
        self.assertEqual(crawl.CONFIG["request_interval_seconds"][CHAIN], 3)
        self.assertNotIn(CHAIN, nr.TAGS)   # ネタフルのタグは未確認なので付けない

    def test_category_icons(self):
        def icon(cat):
            return g.category_icon({"chain": CHAIN, "category": cat, "name": "合成"})[0]
        self.assertEqual(icon("かけうどん系"), "noodles")
        self.assertEqual(icon("期間限定メニュー"), "noodles")
        self.assertEqual(icon("ごはんもの"), "bento")
        self.assertEqual(icon("天ぷら・おでん・その他"), "sozai")
        self.assertEqual(icon("テイクアウト専用"), "bento")

    def test_rendered_item_page_notes(self):
        pending: dict = {}
        with TempData(regular=True):
            crawl.crawl_hanamaru(FakeHanamaru(), pending, {}, "2026-11-27")
            p = json.loads((crawl.PRODUCTS / CHAIN / f"{H.PID_MAIN}.json").read_text(encoding="utf-8"))
        h = g.render_item_page(p)
        self.assertIn("「期間限定メニュー」に掲載されたメニューです", h)
        self.assertIn("「販売期間」の記載によります", h)
        self.assertIn("はなまるうどん公式サイト", h)


if __name__ == "__main__":
    unittest.main()
