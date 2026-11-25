"""ゼッテリア(13チェーン目)のテスト: パーサ(一覧・詳細・お知らせ)、プレスリリースPDFからの発売日・終了予定の抽出と照合、
フェア掲載を根拠にした期間限定の判定、セット商品も記録すること、発売日の出どころ(launch_date_source)の区別、
baseline・定番・販売状況の追跡、robots.txt(404=制限なし)、サイトの表示。
フィクスチャは tests/synthetic_zetteria.py の合成データ(公式ページ・PDFのコピーはリポジトリに入れない)。"""

from __future__ import annotations

import json
import unittest
from datetime import date
from unittest import mock

import helpers  # noqa: F401
from helpers import TempData
import crawl
import fetch_netaful_reviews as nr
import generate_site as g
import notify
import parsers as P
import synthetic_zetteria as Z

CHAIN = "zetteria"
BASE = "https://www.zetteria.jp"
D0 = "2026-11-25"
FAIR = BASE + "/menu/fair.html"
SIDE = BASE + "/menu/side.html"
NEWS = BASE + "/news/"
PRESS = BASE + Z.PRESS_URL


class FakeZetteria:
    """URLからページを返す偽の取得器。詳細URLは汎用の詳細ページを返す。robots は実物どおり404にできる。"""

    def __init__(self, pages=None, details=None, robots="404"):
        self.pages = dict(pages) if pages is not None else Z.zet_pages()
        self.pages.setdefault(NEWS, Z.zet_news())
        self.details = dict(details or {})
        self.robots = robots
        self.urls = []

    def __call__(self, chain, url):
        self.urls.append(url)
        if url.endswith("/robots.txt"):
            if self.robots == "404":
                raise crawl.NotFoundError("HTTP 404")
            return self.robots
        if url in self.details:
            return self.details[url]
        if url in self.pages:
            return self.pages[url]
        if url.startswith(BASE + "/menu/") and url.endswith(".html"):
            return Z.zet_detail()
        raise OSError(f"unexpected url {url}")


class ZetteriaParseTests(unittest.TestCase):
    def test_menu_sections_categories_and_br(self):
        entries = P.parse_zetteria_menu(Z.zet_pages()[FAIR], "フェア")
        by_id = {e["product_id"]: e for e in entries}
        self.assertEqual(set(by_id), {"burger020901", "burger020902", "sweets050901", "side24"})
        e = by_id["burger020901"]
        self.assertEqual(e["name"], "合成柚子竜田バーガー")   # <br> は除去される
        self.assertEqual(e["category"], "フェア バーガー")
        self.assertEqual(e["price"], 620)
        self.assertEqual(e["price_variants"], None)           # 単品だけなら variants は持たない
        self.assertEqual(by_id["sweets050901"]["category"], "フェア スイーツ・ドリンク")
        self.assertEqual(by_id["side24"]["category"], "フェア サイド")
        self.assertEqual(e["official_url"], BASE + "/menu/burger020901.html")

    def test_menu_size_variants_and_tilde(self):
        by_id = {e["product_id"]: e for e in P.parse_zetteria_menu(Z.zet_pages()[SIDE], "サイド")}
        pot = by_id["side010901"]
        self.assertEqual(pot["price"], 250)                   # サイズ別は先頭(=最小)
        self.assertEqual(pot["price_variants"], [{"label": "S:", "price": 250}, {"label": "M:", "price": 340}])
        self.assertFalse(pot["price_tilde"])
        self.assertEqual(by_id["side010902"]["price"], 300)
        self.assertTrue(by_id["side010902"]["price_tilde"])   # ¥300~ の ~
        zep = P.parse_zetteria_menu(Z.zet_pages()[BASE + "/menu/zeppin.html"], "絶品バーガー")[0]
        self.assertEqual((zep["price"], zep["price_tilde"]), (590, True))

    def test_menu_morning_category_cleanup(self):
        by_id = {e["product_id"]: e for e in P.parse_zetteria_menu(Z.zet_pages()[BASE + "/menu/morning.html"], "モーニング")}
        self.assertEqual(by_id["morning010901"]["category"], "モーニング")   # 「〜10:30AM」の注記は落とす
        self.assertEqual(by_id["burger020905"]["category"], "バーガー")

    def test_detail_scoped_to_main_block(self):
        d = P.parse_zetteria_detail(Z.zet_detail())
        self.assertEqual(d["name"], "合成柚子竜田バーガー")   # セットメニューの名前は読まない
        self.assertEqual(d["price"], 620)
        self.assertIn("合成の説明文です", d["description"])
        self.assertIsNone(P.parse_zetteria_detail("<html><body>なにもない</body></html>"))

    def test_detail_price_size_wrapper(self):
        html = Z.zet_detail().replace(
            '<div class="box">\n<div class="txt">\n<p class="item">単品</p>\n<p class="price"><span class="yen">¥</span>620</p>\n</div>\n</div>',
            '<div class="box">\n<div class="txt">\n<p class="item">単品</p>\n<div class="price-size">\n<p class="price"><span class="yen">¥</span>390</p>\n</div>\n</div>\n</div>')
        d = P.parse_zetteria_detail(html)
        self.assertEqual(d["price"], 390)

    def test_news_dedup_and_shape(self):
        news = P.parse_zetteria_news(Z.zet_news())
        self.assertEqual(len(news), 3)                        # タブの重複は除かれる
        self.assertEqual(news[0]["date"], "2026-11-11")
        self.assertEqual(news[0]["category"], "プレスリリース")
        self.assertEqual(news[0]["url"], PRESS)
        self.assertEqual(set(news[0]), {"date", "title", "url", "category"})

    def test_excluded(self):
        self.assertIsNone(P.zetteria_excluded({"name": "x", "price": 100}))
        self.assertEqual(P.zetteria_excluded({"name": "x", "price": None}), "no_price")
        self.assertEqual(P.zetteria_excluded({"name": "", "price": 100}), "no_name")
        # セット商品は除外しない(オーナー決定)
        self.assertIsNone(P.zetteria_excluded({"name": "合成ランチセット", "price": 700}))

    def test_press_text_drops_ruby_and_joins(self):
        text = P.zetteria_press_text(Z.zet_press_pages())
        self.assertNotIn("ゆず", text)                        # ルビ(行高5.5)は落ちる
        self.assertIn("合成柚子竜田バーガー", text)           # 空白を除いてつながる
        self.assertIn("620円", text)

    def test_press_parse(self):
        p = P.parse_zetteria_press(Z.zet_press_pages())
        self.assertEqual(p["published_date"], "2026-11-11")
        self.assertEqual(p["launch_date"], "2026-11-18")
        self.assertEqual(p["planned_end_text"], "12月上旬まで販売予定")
        self.assertEqual(p["items"], [{"name": "合成柚子竜田バーガー", "price": 620}])

    def test_press_parse_year_rollover(self):
        p = P.parse_zetteria_press(Z.zet_press_pages(launch="1月6日（火）から期間限定で", published="2026年12月20日"))
        self.assertEqual(p["launch_date"], "2027-01-06")

    def test_press_match(self):
        press = Z.zet_press_dict()
        self.assertTrue(P.match_zetteria_press("合成柚子竜田バーガー", 620, press))
        self.assertTrue(P.match_zetteria_press("合成柚子竜田バーガー", 999, press))   # 商品概要の名前で一致すれば価格は見ない
        self.assertFalse(P.match_zetteria_press("無関係バーガー", 620, press))
        composite = {"items": [{"name": "別の商品", "price": 620}], "text": "本文に合成柚子竜田バーガーが載る"}
        self.assertTrue(P.match_zetteria_press("合成柚子竜田バーガー", 620, composite))
        self.assertFalse(P.match_zetteria_press("合成柚子竜田バーガー", 999, composite))   # 複合照合は価格も一致が条件
        self.assertFalse(P.match_zetteria_press("合成柚子竜田バーガー", 620, None))


class ZetteriaCrawlTests(unittest.TestCase):
    def crawl_once(self, today=D0, regular=False, pages=None, press="default", fake=None):
        if not hasattr(self, "_td"):   # 同じテストの中の複数回の巡回は同じデータ置き場で続ける
            self._td = TempData(regular=regular)
            self._td.__enter__()
            self.addCleanup(self._td.__exit__, None, None, None)
            self.pending = {}
        self.fake = fake or FakeZetteria(pages=pages)
        press_map = {PRESS: Z.zet_press_dict()} if press == "default" else (press or {})
        with mock.patch.object(crawl, "fetch_zetteria_press", lambda getter, url: press_map.get(url)):
            stats = {}
            crawl.crawl_zetteria(self.fake, self.pending, stats, today)
        self.stats = stats[CHAIN]
        self.prods = {}
        pdir = crawl.PRODUCTS / CHAIN
        if pdir.exists():
            for f in sorted(pdir.glob("*.json")):
                self.prods[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        self.baseline = crawl.baseline_for(CHAIN)
        self.state = crawl.load_json(crawl.DATA / "zetteria_state.json", None)
        self.baseline_file_exists = (crawl.DATA / "baseline_ids.json").exists()
        self.state_file_exists = (crawl.DATA / "zetteria_state.json").exists()
        self.ramen_state_exists = (crawl.DATA / "ramen_state.json").exists()
        return self.stats

    def products(self):
        return self.prods

    def test_first_run_fair_press_and_categories(self):
        st = self.crawl_once()
        self.assertTrue(st["list_ok"])
        prods = self.products()
        self.assertEqual(set(prods), {"burger020901", "burger020902", "sweets050901", "side24"})
        p = prods["burger020901"]
        self.assertEqual(p["limited_kind"], "期間限定")                    # fair.html 掲載が根拠
        self.assertEqual(p["launch_date"], "2026-11-18")                   # プレスリリースPDFから
        self.assertEqual(p["launch_date_source"], "press_pdf")
        self.assertEqual(p["planned_end_text"], "12月上旬まで販売予定")
        self.assertEqual(p["news_url"], PRESS)
        self.assertEqual(p["category"], "フェア バーガー")
        self.assertEqual(p["chain"], CHAIN)
        # カテゴリの優先: フェア以外のページ(レギュラーバーガー)が勝つが、期間限定の根拠は残る
        self.assertEqual(prods["burger020902"]["category"], "レギュラーバーガー")
        self.assertEqual(prods["burger020902"]["limited_kind"], "期間限定")
        # sweets_drink の「フェア」より fair.html の具体的な見出しが勝つ
        self.assertEqual(prods["sweets050901"]["category"], "フェア スイーツ・ドリンク")
        self.assertEqual(st["limited_seen"], 4)
        self.assertEqual(len(self.baseline), 19)                           # 全ページの商品が baseline に入る

    def test_sets_are_recorded_as_regulars(self):
        self.crawl_once(regular=True)
        prods = self.products()
        for pid in ("morning010901", "lunch10", "dinner07", "wakuwaku010901"):   # セット商品も記録する
            self.assertIn(pid, prods)
            self.assertTrue(prods[pid]["is_regular"])
        self.assertEqual(len(prods), 19)                                   # 期間限定4 + 定番15(上限20以内)

    def test_launch_fallback_to_news_date(self):
        press = {PRESS: Z.zet_press_dict(launch_date=None)}                # 照合はできるが発売日が読めないPDF
        self.crawl_once(press=press)
        p = self.products()["burger020901"]
        self.assertEqual(p["launch_date"], "2026-11-11")                   # 発表日で代用
        self.assertEqual(p["launch_date_source"], "news_date_fallback")

    def test_launch_from_headline_when_no_pdf(self):
        pages = Z.zet_pages()
        pages[NEWS] = Z.zet_news(extra=[Z.zet_news_item(
            "2026-11-13", "プレスリリース", "合成タルタルバーガーを11月20日に発売", "/news/press/tarutaru.pdf")])
        self.crawl_once(pages=pages, press={})
        p = self.products()["burger020902"]
        self.assertEqual(p["launch_date"], "2026-11-20")
        self.assertEqual(p["launch_date_source"], "news_headline")
        p1 = self.products()["burger020901"]
        self.assertIsNone(p1["launch_date"])                               # どれでも取れなければ未設定
        self.assertEqual(p1["launch_date_source"], "not_found")

    def test_ended_flow_with_regular(self):
        self.crawl_once(regular=True)
        self.assertIn("burger020903", self.products())
        self.crawl_once(today="2026-11-26", regular=True, pages=Z.zet_pages(drop=("burger020903",)))
        self.assertEqual(self.products()["burger020903"]["status"], "on_sale")   # 別の日に1回: 猶予
        self.crawl_once(today="2026-11-27", regular=True, pages=Z.zet_pages(drop=("burger020903",)))
        p = self.products()["burger020903"]
        self.assertEqual(p["status"], "ended")
        self.assertEqual(p["ended_observed_at"], "2026-11-27")

    def test_list_failure_keeps_everything(self):
        pages = Z.zet_pages()
        del pages[FAIR]
        st = self.crawl_once(pages=pages)
        self.assertFalse(st["list_ok"])
        self.assertEqual(self.products(), {})
        self.assertFalse(self.baseline_file_exists)
        self.assertFalse(self.state_file_exists)

    def test_robots_disallow_skips_page_and_fails_list(self):
        fake = FakeZetteria(robots="User-agent: *\nDisallow: /menu/fair.html\n")
        st = self.crawl_once(fake=fake)
        self.assertFalse(st["list_ok"])
        self.assertNotIn(FAIR, fake.urls)

    def test_price_change_recorded(self):
        self.crawl_once(regular=True)
        pages = Z.zet_pages()
        pages[BASE + "/menu/burger.html"] = pages[BASE + "/menu/burger.html"].replace(
            '<span class="yen">¥</span>440', '<span class="yen">¥</span>450')
        self.crawl_once(regular=True, pages=pages)
        p = self.products()["burger020903"]
        self.assertEqual(p["price"], 450)
        self.assertEqual(len(p["price_history"]), 2)

    def test_detail_failure_goes_pending(self):
        fake = FakeZetteria(details={BASE + "/menu/burger020903.html": "<html><body>空</body></html>"})
        self.crawl_once(regular=True, fake=fake)
        self.assertNotIn("burger020903", self.products())
        self.assertEqual(self.pending[CHAIN]["burger020903"]["reason"], "empty_detail")

    def test_state_is_dedicated_file(self):
        self.crawl_once()
        self.assertEqual(self.state["menu_count"], 19)
        self.assertFalse(self.ramen_state_exists)                          # 共有ファイルには相乗りしない

    def test_rendered_item_page_notes(self):
        self.crawl_once()
        p = self.products()["burger020901"]
        h = g.render_item_page(p)
        self.assertIn("公式サイトの「フェア」に載っているメニューです", h)
        self.assertIn("公式のプレスリリース", h)
        self.assertIn("アレルギー情報ページ", h)
        self.assertIn("栄養成分一覧表", h)
        self.assertIn("ゼッテリア公式サイト", h)


class ZetteriaSiteTests(unittest.TestCase):
    def test_chain_config(self):
        self.assertEqual(g.CHAIN_NAME[CHAIN], "ゼッテリア")
        self.assertEqual(g.CHAIN_SOURCE[CHAIN], "ゼッテリア公式サイト")
        self.assertEqual(g.CHAINS[-1]["slug"], CHAIN)
        self.assertTrue(g.CHAIN_LIST_TEXT.endswith("・幸楽苑・日高屋・ゼッテリア"))
        self.assertIn("--zetteria:", g.CSS)
        self.assertIn('data-chain="zetteria"', g.CSS)
        colors = {c["slug"]: c["color"].lower() for c in g.CHAINS}
        self.assertEqual(len(set(colors.values())), len(colors))
        self.assertEqual(notify.CHAIN_LABEL[CHAIN], "ゼッテリア")
        self.assertIn(CHAIN, crawl.CRAWLERS)
        self.assertEqual(crawl.CONFIG["max_requests_per_run"][CHAIN], 40)
        self.assertEqual(crawl.CONFIG["request_interval_seconds"][CHAIN], 3)
        self.assertNotIn(CHAIN, nr.TAGS)                                   # ネタフルのタグは未確認なので付けない

    def test_category_icons(self):
        def icon(cat):
            return g.category_icon({"chain": CHAIN, "category": cat, "name": "合成"})[0]
        self.assertEqual(icon("フェア バーガー"), "burger")
        self.assertEqual(icon("モーニング"), "bento")
        self.assertEqual(icon("ドリンク"), "drink")
        self.assertEqual(icon("サイド"), "sozai")

    def test_seo_description_uses_generic_wording(self):
        chain = next(c for c in g.CHAINS if c["slug"] == CHAIN)
        self.assertIn("の期間限定・新登場メニュー", g.chain_seo_description(chain, [{"name": "合成", "status": "active", "product_id": "x1"}]))


if __name__ == "__main__":
    unittest.main()
