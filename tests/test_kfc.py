"""ケンタッキー(www.kfc.co.jp / japan.kfc.co.jp)のパーサ・巡回・サイト生成のテスト。

公式ページのコピーはリポジトリに入れていないので、実物の構造を再現した合成ページ
(tests/synthetic_kfc.py)だけで検証する。実サイトでの試行結果は作業レポートに残す。
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
import synthetic_kfc as K  # noqa: E402
from helpers import TempData  # noqa: E402

CHAIN = "kfc"
BASE = K.BASE
CORP = K.CORP


class FakeKfc:
    """URLで引く偽の取得器。robots は差し替えられる。"""

    def __init__(self, pages=None, robots=None, corp_robots=None):
        self.pages = K.kfc_pages() if pages is None else pages
        if robots is not None:
            self.pages[K.ROBOTS_URL] = robots
        if corp_robots is not None:
            self.pages[K.CORP_ROBOTS_URL] = corp_robots
        self.urls: list[str] = []

    def __call__(self, chain, url):
        assert chain == CHAIN
        self.urls.append(url)
        if url not in self.pages:
            raise RuntimeError(f"unexpected url: {url}")
        return self.pages[url]


class KfcParserTests(unittest.TestCase):
    def test_menu_categories_and_counts(self):
        page = K.kfc_menu_page()
        counts = {}
        for cat in parsers.KFC_CATEGORIES:
            es = parsers.parse_kfc_menu(page, cat)
            counts[cat] = len(es)
            for e in es:
                self.assertEqual(e["category"], cat)
                self.assertEqual(e["official_url"], K.menu_url(cat))
                self.assertIsInstance(e["price"], int)
        self.assertEqual(counts, {"チキン": 3, "セット": 2, "バーガー": 1, "サイド": 2, "ドリンク": 2, "キッズ": 1})
        pack = next(e for e in parsers.parse_kfc_menu(page, "チキン") if e["product_id"] == "900103")
        self.assertEqual(pack["price"], 1290)          # カンマ区切りの価格
        self.assertEqual(pack["name"], "合成パック")

    def test_menu_recommended_section_holds_limited_items(self):
        page = K.kfc_menu_page()
        for cat in parsers.KFC_CATEGORIES:
            pids = [e["product_id"] for e in parsers.parse_kfc_menu(page, cat)]
            for pid in ("900302", "900303", "900999"):
                self.assertNotIn(pid, pids)            # 期間限定はカテゴリのセクションには載らない
            self.assertEqual(len(pids), len(set(pids)))  # モバイル表示の複製で二重に数えない
        rec = parsers.parse_kfc_menu(page, parsers.KFC_RECOMMENDED)
        self.assertEqual({e["product_id"] for e in rec}, {"900101", "900302", "900303", "900999"})
        self.assertTrue(all(e["category"] == "おすすめ" for e in rec))

    def test_menu_missing_section_is_empty(self):
        self.assertEqual(parsers.parse_kfc_menu("<html><body>空</body></html>", "チキン"), [])

    def test_release_list(self):
        items = parsers.parse_kfc_release_list(K.kfc_release_list())
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["url"], K.REL_CM)    # 新しい順
        self.assertEqual(items[1], {"date": "2026-11-20", "title": "合成ポットパイを発売",
                                    "url": K.REL_LIMITED, "category": "ブランド情報"})

    def test_release_detail_limited(self):
        d = parsers.parse_kfc_release_detail(K.kfc_release_limited())
        self.assertIsNotNone(d)
        self.assertEqual(d["launch_date"], "2026-11-25")
        self.assertEqual(d["planned_end_text"], "※数量限定、なくなり次第販売終了")
        self.assertTrue(d["limited"])
        self.assertEqual(d["items"], [{"name": "合成ポットパイ", "price": 490},
                                      {"name": "合成ポットパイセット", "price": 950}])   # 内訳〔…〕は商品にしない

    def test_release_detail_new_menu(self):
        d = parsers.parse_kfc_release_detail(K.kfc_release_newmenu())
        self.assertEqual(d["launch_date"], "2026-11-19")
        self.assertIsNone(d["planned_end_text"])
        self.assertFalse(d["limited"])

    def test_release_detail_non_product_is_none(self):
        self.assertIsNone(parsers.parse_kfc_release_detail(K.kfc_release_cm()))

    def test_excluded(self):
        self.assertEqual(parsers.kfc_excluded({"name": "", "price": 100}), "no_name")
        self.assertEqual(parsers.kfc_excluded({"name": "x", "price": None}), "no_price")
        self.assertIsNone(parsers.kfc_excluded({"name": "x", "price": 100}))

    def test_match_release(self):
        rel = parsers.parse_kfc_release_detail(K.kfc_release_limited())
        self.assertTrue(parsers.match_kfc_release("合成ポットパイ", 490, rel))
        self.assertTrue(parsers.match_kfc_release("合成ポットパイセット", 950, rel))
        self.assertFalse(parsers.match_kfc_release("合成チキン", 300, rel))
        self.assertFalse(parsers.match_kfc_release("合成ポットパイ", 490, None))


class KfcCrawlTests(unittest.TestCase):
    def crawl_once(self, today="2026-11-25", pages=None, fake=None, regular=True):
        if not hasattr(self, "_td"):   # 同じテストの中の複数回の巡回は同じデータ置き場で続ける
            self._td = TempData(regular=regular)
            self._td.__enter__()
            self.addCleanup(self._td.__exit__, None, None, None)
            self.pending = {}
        self.fake = fake or FakeKfc(pages=pages)
        stats: dict = {}
        crawl.crawl_kfc(self.fake, self.pending, stats, today)
        self.stats = stats[CHAIN]
        self.prods = {}
        pdir = crawl.PRODUCTS / CHAIN
        if pdir.exists():
            for f in sorted(pdir.glob("*.json")):
                self.prods[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        self.baseline = crawl.baseline_for(CHAIN)
        self.state = crawl.load_json(crawl.DATA / "kfc_state.json", None)
        self.baseline_file_exists = (crawl.DATA / "baseline_ids.json").exists()
        self.state_file_exists = (crawl.DATA / "kfc_state.json").exists()
        self.ramen_state_exists = (crawl.DATA / "ramen_state.json").exists()
        return self.stats

    def test_first_run(self):
        st = self.crawl_once()
        self.assertTrue(st["list_ok"])
        prods = self.prods
        self.assertEqual(set(prods), set(K.ALL_PIDS))
        p = prods["900302"]                                        # リリースの【商品概要】に載る数量限定
        self.assertEqual(p["limited_kind"], "期間限定")
        self.assertEqual(p["badges"], ["期間限定"])
        self.assertEqual(p["launch_date"], "2026-11-25")           # 法人リリースの■発売日
        self.assertEqual(p["launch_date_source"], "press_release")
        self.assertEqual(p["planned_end_text"], "※数量限定、なくなり次第販売終了")
        self.assertEqual(p["news_url"], K.REL_LIMITED)
        self.assertEqual(p["news_date"], "2026-11-20")
        self.assertEqual(p["announced_at"], "2026-11-20")
        self.assertEqual(p["category"], "おすすめ")   # 期間限定は「おすすめ」のセクションで記録する
        self.assertEqual(p["chain"], CHAIN)
        self.assertIsNone(p["description"])                        # 第1段階では対象外
        self.assertEqual(prods["900303"]["limited_kind"], "期間限定")   # セットも別商品として照合される
        d = prods["900502"]                                        # リリースに載るが限定の記載は無い
        self.assertEqual(d["limited_kind"], "新メニュー")
        self.assertEqual(d["launch_date"], "2026-11-19")
        self.assertEqual(d["launch_date_source"], "press_release")
        for pid in ("900101", "900201", "900202", "900601"):       # セット商品も定番として記録する
            self.assertTrue(prods[pid]["is_regular"], pid)
        self.assertTrue(prods["900999"]["is_regular"])             # おすすめ限定でも、リリースに無いものは定番扱いから始める
        self.assertEqual(prods["900101"]["category"], "チキン")     # おすすめにも載る定番はカテゴリ側が正
        self.assertEqual(len(self.baseline), 14)
        self.assertEqual(st["limited_seen"], 2)
        self.assertEqual(st["news_matched"], 3)
        self.assertEqual(st["news_unmatched"], 0)

    def test_release_details_fetched_once(self):
        self.crawl_once()
        fake2 = FakeKfc()
        self.crawl_once(fake=fake2)
        details = [u for u in fake2.urls if "/news_release/" in u]
        self.assertEqual(details, [])                              # 照合済みの詳細は取り直さない
        self.assertIn(K.RELEASE_LIST, fake2.urls)                  # 一覧は毎回取る

    def test_ended_flow(self):
        self.crawl_once()
        pages = K.kfc_pages(menu=K.kfc_menu_page(drop=("900101",)))
        self.crawl_once(today="2026-11-26", pages=pages)
        self.assertEqual(self.prods["900101"]["status"], "on_sale")   # 別の日に1回: 猶予
        self.crawl_once(today="2026-11-27", pages=pages)
        p = self.prods["900101"]
        self.assertEqual(p["status"], "ended")
        self.assertEqual(p["ended_observed_at"], "2026-11-27")

    def test_list_failure_keeps_everything(self):
        pages = K.kfc_pages()
        del pages[K.menu_url("バーガー")]
        st = self.crawl_once(pages=pages)
        self.assertFalse(st["list_ok"])
        self.assertEqual(self.prods, {})
        self.assertFalse(self.baseline_file_exists)
        self.assertFalse(self.state_file_exists)

    def test_robots_disallow_menu(self):
        fake = FakeKfc(robots="User-agent: *\nDisallow: /menu\n")
        st = self.crawl_once(fake=fake)
        self.assertFalse(st["list_ok"])
        self.assertFalse(any("/menu?category=" in u for u in fake.urls))
        self.assertEqual(self.prods, {})

    def test_price_change_recorded(self):
        self.crawl_once()
        pages = K.kfc_pages(menu=K.kfc_menu_page(prices={"900101": 320}))
        self.crawl_once(today="2026-11-26", pages=pages)
        p = self.prods["900101"]
        self.assertEqual(p["price"], 320)
        self.assertEqual(len(p["price_history"]), 2)

    def test_state_is_dedicated_file(self):
        self.crawl_once()
        self.assertEqual(self.state["menu_count"], 14)
        self.assertEqual(set(self.state["releases"]), {K.REL_LIMITED, K.REL_NEWMENU})
        self.assertEqual(self.state["release_checked"], [K.REL_CM])
        self.assertFalse(self.ramen_state_exists)                  # 共有ファイルには相乗りしない


class KfcSiteTests(unittest.TestCase):
    def test_chain_config(self):
        self.assertEqual(g.CHAIN_NAME[CHAIN], "ケンタッキー")
        self.assertEqual(g.CHAIN_SOURCE[CHAIN], "ケンタッキー公式サイト")
        self.assertEqual(g.CHAINS[-5]["slug"], CHAIN)   # 後ろにやよい軒・CoCo壱番屋・はなまるうどん・大戸屋が入った
        self.assertTrue(g.CHAIN_LIST_TEXT.endswith("・ゼッテリア・ケンタッキー・やよい軒・CoCo壱番屋・はなまるうどん・大戸屋"))
        self.assertIn("--kfc:", g.CSS)
        self.assertIn('data-chain="kfc"', g.CSS)
        colors = {c["slug"]: c["color"].lower() for c in g.CHAINS}
        self.assertEqual(len(set(colors.values())), len(colors))   # 既存の色と重ならない
        self.assertEqual(notify.CHAIN_LABEL[CHAIN], "ケンタッキー")
        self.assertIn(CHAIN, crawl.CRAWLERS)
        self.assertEqual(crawl.CONFIG["max_requests_per_run"][CHAIN], 20)
        self.assertEqual(crawl.CONFIG["request_interval_seconds"][CHAIN], 3)
        self.assertNotIn(CHAIN, nr.TAGS)                           # ネタフルのタグは未確認なので付けない

    def test_category_icons(self):
        def icon(cat):
            return g.category_icon({"chain": CHAIN, "category": cat, "name": "合成"})[0]
        self.assertEqual(icon("チキン"), "sozai")
        self.assertEqual(icon("バーガー"), "burger")
        self.assertEqual(icon("セット"), "bento")
        self.assertEqual(icon("キッズ"), "bento")
        self.assertEqual(icon("ドリンク"), "drink")
        self.assertEqual(icon("サイド"), "sozai")

    def test_rendered_item_page_notes(self):
        pending: dict = {}
        with TempData(regular=True):
            crawl.crawl_kfc(FakeKfc(), pending, {}, "2026-11-25")
            p = json.loads((crawl.PRODUCTS / CHAIN / "900302.json").read_text(encoding="utf-8"))
        h = g.render_item_page(p)
        self.assertIn("公式のニュースリリースで、数量限定・期間限定として発売が告知されたメニューです", h)
        self.assertIn("発売日は公式のニュースリリース", h)
        self.assertIn("公式ニュースリリース", h)
        self.assertIn("ケンタッキー公式サイト", h)


if __name__ == "__main__":
    unittest.main()
