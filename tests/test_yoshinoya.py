"""吉野家: 一覧・カテゴリ・詳細・お知らせ見出しのパーサ、発売日の抽出と照合、再登場(同slug)、
販売状況の追跡、title生成、ネタフル記事の振り分けのテスト。
フィクスチャ(tests/fixtures/yoshinoya/)は2026-10-11に取得した公式ページのスナップショット
(入口・全12カテゴリの一覧・詳細2件・2026年のお知らせ一覧)。PDFは取得していない。"""

from __future__ import annotations

import json
import re
import unittest

import helpers  # noqa: F401
from helpers import FakeGetter, TempData, fx
import crawl
import fetch_netaful_reviews as nr
import generate_site as g
import parsers as P
from fetch import BlockedError

UNIT = '<div class="menu__unit'


def run_y(getter, day, pending=None):
    pending = {} if pending is None else pending
    stats = {}
    crawl.crawl_yoshinoya(getter, pending, stats, day)
    return pending, stats["yoshinoya"]


def getter(**kw):
    return FakeGetter(fallback_detail=True, **kw)


def drop_unit(page: str, slug: str) -> str:
    """一覧ページから、リンクに /{slug}/ を含む商品欄を取り除く。"""
    head, *units = page.split(UNIT)
    keep = [u for u in units if f"/{slug}/" not in u.split("</a>")[0]]
    return head + "".join(UNIT + u for u in keep)


def drop_everywhere(slug: str) -> dict:
    return {f"/menu/{c}/": drop_unit(fx(helpers.YOSHINOYA_PAGES[f"/menu/{c}/"]), slug) for c in helpers.YOSHINOYA_CATS}


def clone_unit(page: str, from_slug: str, new_slug: str, new_name: str) -> str:
    head, *units = page.split(UNIT)
    src = next(u for u in units if f"/{from_slug}/" in u.split("</a>")[0])
    new = src.replace(from_slug, new_slug)
    new = re.sub(r'(menu__title">)[^<]*(<span class="icon__arrow)', rf"\g<1>{new_name}\g<2>", new)
    return head + "".join(UNIT + u + (UNIT + new if u is src else "") for u in units)


def news_with(title: str, date: str, category: str = "プレスリリース") -> str:
    """お知らせ一覧の先頭に1件足したページ。date は 2026.10.13 の形。"""
    page = fx("yoshinoya/news_2026.html")
    unit = ('<div class="news__unit"><p class="news__title"><a href="https://www.yoshinoya.com/wp-content/uploads/x.pdf" class="pdf">'
            f'<span class="budge__new"><span>NEW</span></span><span>{title}<span class="icon__pdf"></span></span></a></p>'
            f'<div class="news__cap"><div class="news__date">{date}</div>'
            f'<div class="news__cat"><a href="/news/2026/x"> {category}<span></span></a></div></div></div>')
    return page.replace('<div class="news__unit">', unit + '<div class="news__unit">', 1)


class TestParsers(unittest.TestCase):
    def test_index_finds_all_categories_by_following_links(self):
        cats = P.parse_yoshinoya_index(fx("yoshinoya/menu_index.html"))
        self.assertEqual([c["slug"] for c in cats], helpers.YOSHINOYA_CATS)
        self.assertEqual(cats[0]["label"], "期間限定")

    def test_path_resolution_uses_page_host(self):
        self.assertEqual(P.yoshinoya_normalize_path("https://www.yoshinoya.com/menu/gyudon"), "/menu/gyudon/")
        self.assertEqual(P.yoshinoya_normalize_path("/menu/set/x/"), "/menu/set/x/")
        self.assertEqual(P.yoshinoya_normalize_path("../set/x", "https://www.yoshinoya.com/menu/gentei/"), "/menu/set/x/")
        self.assertIsNone(P.yoshinoya_normalize_path("https://stores.yoshinoya.com/menu/a/b/"))
        self.assertIsNone(P.yoshinoya_normalize_path("/news/2026/"))

    def test_product_id(self):
        self.assertEqual(P.yoshinoya_product_id("/menu/yoshinoyanodon/tsukimi_gyutoji_don/"),
                         "yoshinoyanodon-tsukimi_gyutoji_don")
        self.assertIsNone(P.yoshinoya_product_id("/menu/gyudon/"))

    def test_gentei_list(self):
        es = P.parse_yoshinoya_list(fx("yoshinoya/list_gentei.html"))
        self.assertEqual(len(es), 5)
        by = {e["product_id"]: e for e in es}
        a = by["gentei-gyusanma-tei"]
        self.assertEqual((a["name"], a["price"], a["price_ex_tax"]), ("牛さんま定食", 899, 818))
        self.assertTrue(a["is_new"] and a["is_limited"])
        self.assertFalse(a["takeout_icon"])
        t = by["yoshinoyanodon-tsukimi_gyutoji_don"]
        self.assertEqual((t["price"], t["price_label"]), (699, "並盛"))
        self.assertTrue(t["is_limited"] and not t["is_new"] and t["takeout_icon"])
        # 同じ商品が別カテゴリの一覧にも載るが、URL(=ID)は同じ
        s = {e["product_id"] for e in P.parse_yoshinoya_list(fx("yoshinoya/list_set.html"))}
        self.assertIn("yoshinoyanodon-tsukimi_gyutoji_don".replace("yoshinoyanodon", "set").replace("don", "tei"), s)
        self.assertIn("gentei-gyusanma-tei", s)

    def test_non_menu_entry_has_no_price(self):
        es = P.parse_yoshinoya_list(fx("yoshinoya/list_karaage.html"))
        none = [e for e in es if e["price"] is None]
        self.assertEqual([e["name"] for e in none], ["から揚げの美味しい温め直し方"])
        # 1,000円以上はカンマ付き
        by = {e["name"]: e for e in es}
        self.assertEqual(by["鬼おろしポン酢から揚げ定食"]["price"], 1006)

    def test_all_lists_parse(self):
        for c in helpers.YOSHINOYA_CATS:
            self.assertGreater(len(P.parse_yoshinoya_list(fx(f"yoshinoya/list_{c}.html"))), 0, c)

    def test_detail_with_sizes_and_takeout(self):
        d = P.parse_yoshinoya_detail(fx("yoshinoya/detail_tsukimi_gyutoji_don.html"))
        self.assertEqual(d["name"], "月見牛とじ丼")
        self.assertEqual([(r["label"], r["price"]) for r in d["eatin"]], [("並盛", 699), ("大盛", 875)])
        self.assertEqual([(r["label"], r["price"]) for r in d["takeout_prices"]], [("並盛", 686), ("大盛", 859)])
        self.assertTrue(d["takeout"])
        self.assertTrue(d["is_limited"] and not d["is_new"])
        pv = P.yoshinoya_price_variants(d)
        self.assertEqual([(v["label"], v["price"]) for v in pv],
                         [("並盛", 699), ("大盛", 875), ("並盛・テイクアウト", 686), ("大盛・テイクアウト", 859)])
        self.assertEqual(P.yoshinoya_pick_primary(d)["price"], 699)

    def test_detail_takeout_not_available(self):
        d = P.parse_yoshinoya_detail(fx("yoshinoya/detail_gyusanma_tei.html"))
        self.assertEqual(d["eatin"], [{"label": "", "price": 899, "price_ex_tax": 818}])
        self.assertEqual(d["takeout_prices"], [])
        self.assertFalse(d["takeout"])
        self.assertTrue(d["is_new"] and d["is_limited"])
        self.assertEqual(len(P.yoshinoya_price_variants(d)), 1)

    def test_detail_without_price_is_none(self):
        self.assertIsNone(P.parse_yoshinoya_detail("<html><h1>x</h1></html>"))

    def test_news_list(self):
        news = P.parse_yoshinoya_news(fx("yoshinoya/news_2026.html"))
        self.assertEqual(len(news), 91)
        self.assertEqual(news[0]["date"], "2026-10-10")  # 「2026.10.10」
        a = next(n for n in news if "牛さんま定食" in n["title"])
        self.assertEqual(a["date"], "2026-10-07")  # 「2026.10.7」はゼロ埋めされる
        self.assertEqual(a["category"], "プレスリリース")
        self.assertTrue(a["url"].endswith(".pdf"))
        self.assertNotIn("NEW", a["title"])
        self.assertNotIn("<", a["title"])


class TestNewsHeadings(unittest.TestCase):
    def setUp(self):
        self.news = P.parse_yoshinoya_news(fx("yoshinoya/news_2026.html"))

    def test_start_date_extraction(self):
        f = P.yoshinoya_start_date
        self.assertEqual(f("「牛さんま定食」10月8日より期間限定販売", "2026-10-07"), "2026-10-08")
        self.assertEqual(f("『牛たん・牛皿御膳』が6月18日（木）11時より販売開始", "2026-06-17"), "2026-06-18")
        self.assertEqual(f("『月見牛とじ御膳』『月見牛とじ丼』本日より販売開始", "2026-08-27"), "2026-08-27")
        self.assertEqual(f("『魯肉牛丼』を4月9日より販売開始", "2026-04-08"), "2026-04-09")
        # 年は書かれていないのでお知らせの日付から補う(12月のお知らせの1月5日は翌年)
        self.assertEqual(f("「牛すき鍋膳」1月5日より販売開始", "2026-12-28"), "2027-01-05")
        # 販売・発売の語が無い(放映開始・開催)ものは発売日にしない
        self.assertIsNone(f("新TVCM9月3日（木）より放映開始！", "2026-09-02"))
        self.assertIsNone(f("「牛さんま定食」の販売状況について", "2026-10-07"))

    def test_quoted_name_must_match_exactly(self):
        title = "秋の味覚「北海道産さんま」を牛小鉢と一緒に 「牛さんま定食」10月8日より期間限定販売"
        self.assertTrue(P.yoshinoya_title_matches("牛さんま定食", title))
        # 「さんま定食」は「牛さんま定食」に部分一致するが、別の商品なので一致させない
        self.assertFalse(P.yoshinoya_title_matches("さんま定食", title))
        self.assertFalse(P.yoshinoya_title_matches("さんま", title))
        self.assertTrue(P.yoshinoya_title_matches("牛丼・油そばセット", "『牛丼・油そばセット』を3月12日から販売開始"))
        # 括弧の無い見出しだけ部分一致(4文字以上)
        self.assertTrue(P.yoshinoya_title_matches("ドラクエウォークセット", "ドラクエウォークセット第1弾 早期販売終了のお知らせ"))
        self.assertFalse(P.yoshinoya_title_matches("牛丼", "牛丼弁当2丁800円キャンペーン"))

    def test_launch_match_against_real_headings(self):
        m = P.match_yoshinoya_launch("牛さんま定食", self.news, "2026-10-11")
        self.assertEqual(m["launch_date"], "2026-10-08")
        self.assertTrue(m["news"]["url"].endswith("news_2026100701.pdf"))
        # 同じ見出しに「本日より」とある2商品はお知らせの日付
        for name in ("月見牛とじ御膳", "月見牛とじ丼"):
            self.assertEqual(P.match_yoshinoya_launch(name, self.news, "2026-10-11")["launch_date"], "2026-08-27")
        # 見出しに載っていない商品は照合できない(誤爆させない)
        for name in ("さんま定食", "さんま（単品）", "黒カレー", "厚切り豚角煮定食"):
            self.assertIsNone(P.match_yoshinoya_launch(name, self.news, "2026-10-11"), name)

    def test_launch_match_skips_ecommerce_and_tvcm(self):
        # 公式通販の「黒カレー」を本日より販売開始 / TVCMの「牛丼・油そばセット」にも驚愕…放映開始 は店舗の発売日ではない
        self.assertIsNone(P.match_yoshinoya_launch("黒カレー", self.news, "2026-08-10"))
        m = P.match_yoshinoya_launch("牛丼・油そばセット", self.news, "2026-09-10")
        self.assertEqual(m["launch_date"], "2026-03-12") if m else None  # 窓の外(120日超)
        m2 = P.match_yoshinoya_launch("牛丼・油そばセット", self.news, "2026-03-20")
        self.assertEqual(m2["launch_date"], "2026-03-12")

    def test_window_blocks_old_headings(self):
        # 月見牛とじ丼の昨季の見出しが今季の照合に混ざらない: 参照日から120日より前は対象外
        self.assertIsNone(P.match_yoshinoya_launch("月見牛とじ丼", self.news, "2027-08-20"))
        self.assertIsNone(P.match_yoshinoya_launch("月見牛とじ丼", self.news, "2026-01-01"))

    def test_end_notice(self):
        e = P.match_yoshinoya_end_notice("ドラクエウォークセット", self.news, "2026-06-01", "2026-10-11")
        self.assertEqual(e["date"], "2026-07-08")
        self.assertIsNone(P.match_yoshinoya_end_notice("ドラクエウォークセット", self.news, "2026-07-09", "2026-10-11"))
        # 販売状況のお知らせ(終了の語なし)や開店のお知らせは終了告知にしない
        self.assertIsNone(P.match_yoshinoya_end_notice("厚切り豚角煮定食", self.news, "2026-01-01", "2026-10-11"))


@helpers.needs_fixtures
class TestCrawl(unittest.TestCase):
    def test_first_run_records_limited_list_only_and_makes_baseline(self):
        with TempData():
            g1 = getter()
            _, st = run_y(g1, "2026-10-11")
            self.assertTrue(st["list_ok"])
            self.assertFalse(st["blocked"])
            self.assertEqual(st["list_requests"], 14)  # robots + 入口 + 12カテゴリ
            self.assertEqual(st["limited_seen"], 5)
            self.assertEqual(st["new_products"], 5)
            self.assertEqual(len(crawl.load_chain_products("yoshinoya")), 5)
            self.assertEqual(crawl.load_chain_products("matsuya"), [])
            base = crawl.baseline_for("yoshinoya")
            self.assertGreater(len(base), 80)
            self.assertIn("gyudon-gyu-don", base)
            self.assertNotIn("karaage-karaage_reheat", base)  # 料理ではない案内
            self.assertEqual(sum(1 for u in g1.calls if u.endswith("/news/2026/")), 1)
            self.assertTrue(all(u.startswith(crawl.YOSHINOYA_BASE) for u in g1.calls))
            self.assertFalse(any(u.endswith(".pdf") or "/pdf/" in u for u in g1.calls))  # PDFは取得しない
            self.assertLessEqual(len(g1.calls), crawl.MAX_REQUESTS["yoshinoya"])

    def test_product_fields(self):
        with TempData():
            run_y(getter(), "2026-10-11")
            p = crawl.load_product("yoshinoya", "gentei-gyusanma-tei")
            self.assertEqual(p["chain"], "yoshinoya")
            self.assertEqual(p["status"], "on_sale")
            self.assertEqual(p["limited_kind"], "期間限定")
            self.assertEqual(p["badges"], ["NEW", "期間限定"])
            self.assertEqual(p["price"], 899)
            self.assertEqual(p["category"], "定食")  # 「期間限定」の列はカテゴリ名にしない
            self.assertEqual(p["launch_date"], "2026-10-08")
            self.assertEqual(p["launch_date_source"], "news_headline")
            self.assertEqual(p["news_date"], "2026-10-07")
            self.assertFalse(p["takeout"])
            # 事実情報だけ: 説明文・画像・栄養は持たない。栄養はPDFへのリンク
            self.assertIsNone(p["description"])
            self.assertIsNone(p["image"])
            self.assertIsNone(p["nutrition"])
            self.assertEqual(p["nutrition_pdf_url"], "https://www.yoshinoya.com/pdf/allergy/")
            self.assertTrue(p["official_url"].startswith("https://www.yoshinoya.com/menu/gentei/"))
            self.assertEqual(p["price_history"], [{"date": "2026-10-11", "price": 899}])
            t = crawl.load_product("yoshinoya", "yoshinoyanodon-tsukimi_gyutoji_don")
            self.assertEqual(t["limited_kind"], "期間限定")
            self.assertEqual(t["launch_date"], "2026-08-27")
            self.assertEqual(t["price"], 699)
            self.assertEqual(len(t["price_variants"]), 4)
            self.assertTrue(t["takeout"])
            # 見出しに載らない商品は発売日 null(由来も残す)
            s = crawl.load_product("yoshinoya", "gentei-sanma-tei")
            self.assertIsNone(s["launch_date"])
            self.assertEqual(s["launch_date_source"], "not_found")

    def test_counts_for_report(self):
        with TempData():
            _, st = run_y(getter(), "2026-10-11")
            self.assertEqual(st["news_matched"], 3)  # 牛さんま定食・月見牛とじ御膳・月見牛とじ丼
            self.assertEqual(st["news_unmatched"], 2)

    def test_robots_404_means_allow_all(self):
        with TempData():
            g1 = getter()
            _, st = run_y(g1, "2026-10-11")
            self.assertTrue(st["list_ok"])
            self.assertTrue(g1.calls[0].endswith("/robots.txt"))

    def test_robots_disallow_is_respected(self):
        with TempData():
            ov = {"/robots.txt": "User-agent: *\nDisallow: /menu/set/\n"}
            g1 = getter(overrides=ov)
            _, st = run_y(g1, "2026-10-11")
            self.assertFalse(st["list_ok"])
            self.assertFalse(any(u.endswith("/menu/set/") for u in g1.calls))
            self.assertIsNone(crawl.baseline_for("yoshinoya"))

    def test_second_run_only_refetches_news(self):
        with TempData():
            run_y(getter(), "2026-10-11")
            g2 = getter()
            _, st = run_y(g2, "2026-10-12")
            self.assertEqual(st["new_products"], 0)
            self.assertEqual(st["detail_requests"], 1)  # 発売日が未確定の商品のために、お知らせ一覧を1回
            self.assertEqual(len(g2.calls), 15)
            self.assertFalse(any("/menu/gentei/" in u and not u.endswith("/menu/gentei/") for u in g2.calls))

    def test_new_item_after_baseline_is_recorded_without_badge(self):
        with TempData():
            run_y(getter(), "2026-10-11")
            plain = re.sub(r'<span class="budge(?:__new)?"><span>[^<]*</span></span>', "", fx("yoshinoya/detail_gyusanma_tei.html"))
            ov = {"/menu/sidemenu/": clone_unit(fx("yoshinoya/list_sidemenu.html"), "salad", "newsalad", "新サラダ"),
                  "/menu/sidemenu/newsalad/": plain}
            _, st = run_y(getter(overrides=ov), "2026-10-12")
            self.assertEqual(st["new_products"], 1)
            p = crawl.load_product("yoshinoya", "sidemenu-newsalad")
            self.assertEqual(p["limited_kind"], "新規掲載")  # 公式の表示が無ければ「期間限定」と書かない
            self.assertEqual(p["badges"], [])
            self.assertFalse(p["in_limited_list"])

    def test_existing_regular_menu_not_recorded(self):
        with TempData():
            run_y(getter(), "2026-10-11")
            self.assertIsNone(crawl.load_product("yoshinoya", "gyudon-gyu-don"))

    def test_end_needs_two_misses_on_different_days(self):
        pid = "gentei-sanma"
        ov = drop_everywhere("sanma")
        with TempData():
            run_y(getter(), "2026-10-11")
            run_y(getter(overrides=ov), "2026-10-12")
            p = crawl.load_product("yoshinoya", pid)
            self.assertEqual(p["status"], "on_sale")
            run_y(getter(overrides=ov), "2026-10-12")  # 同じ日の再実行は1回と数える
            self.assertEqual(crawl.load_product("yoshinoya", pid)["status"], "on_sale")
            _, st = run_y(getter(overrides=ov), "2026-10-13")
            p = crawl.load_product("yoshinoya", pid)
            self.assertEqual(p["status"], "ended")
            self.assertEqual(p["ended_observed_at"], "2026-10-13")
            self.assertEqual(st["ended"], 1)

    def test_no_end_when_a_category_failed(self):
        ov = drop_everywhere("sanma")
        with TempData():
            run_y(getter(), "2026-10-11")
            run_y(getter(overrides=ov, fail={"/menu/kids/"}), "2026-10-12")
            _, st = run_y(getter(overrides=ov, fail={"/menu/kids/"}), "2026-10-13")
            self.assertFalse(st["list_ok"])
            self.assertEqual(crawl.load_product("yoshinoya", "gentei-sanma")["status"], "on_sale")

    def test_no_end_when_a_category_is_empty(self):
        ov = drop_everywhere("sanma")
        ov["/menu/kids/"] = "<html></html>"  # 構造変更の疑い(0件)
        with TempData():
            run_y(getter(), "2026-10-11")
            run_y(getter(overrides=ov), "2026-10-12")
            _, st = run_y(getter(overrides=ov), "2026-10-13")
            self.assertFalse(st["list_ok"])
            self.assertEqual(crawl.load_product("yoshinoya", "gentei-sanma")["status"], "on_sale")

    def test_block_stops_chain_and_no_ending(self):
        class Blocking(FakeGetter):
            def __call__(self, chain, url):
                if url.endswith("/menu/set/"):
                    raise BlockedError("HTTP 403")
                return super().__call__(chain, url)
        ov = drop_everywhere("sanma")
        with TempData():
            run_y(getter(), "2026-10-11")
            for d in ("2026-10-12", "2026-10-13"):
                _, st = run_y(Blocking(fallback_detail=True, overrides=ov), d)
                self.assertTrue(st["blocked"])
            self.assertEqual(crawl.load_product("yoshinoya", "gentei-sanma")["status"], "on_sale")

    def test_relaunch_same_slug_is_recorded_as_new_season(self):
        pid = "yoshinoyanodon-tsukimi_gyutoji_don"
        gone = drop_everywhere("tsukimi_gyutoji_don")
        with TempData():
            run_y(getter(), "2026-10-11")
            run_y(getter(overrides=gone), "2026-10-12")
            run_y(getter(overrides=gone), "2026-10-13")
            p = crawl.load_product("yoshinoya", pid)
            self.assertEqual((p["status"], p["ended_observed_at"]), ("ended", "2026-10-13"))
            self.assertEqual(p["launch_date"], "2026-08-27")
            # 翌年の月見の季節に、同じslugで戻ってくる。今季の発売開始を告げる見出しが出る
            ov = {"/news/2027/": news_with("『月見牛とじ丼』本日より販売開始", "2027.8.26").replace("/news/2026/x", "/news/2027/x")}
            _, st = run_y(getter(overrides=ov), "2027-08-26")
            p = crawl.load_product("yoshinoya", pid)
            self.assertEqual(p["status"], "on_sale")
            self.assertEqual(st["new_products"], 0)  # 新規ではなく再登場
            self.assertEqual(p["relaunches"], [{"ended_observed_at": "2026-10-13", "reappeared_at": "2027-08-26"}])
            self.assertEqual(p["seasons"][0]["launch_date"], "2026-08-27")
            self.assertEqual(p["seasons"][0]["ended_observed_at"], "2026-10-13")
            self.assertEqual(p["launch_date"], "2027-08-26")  # 今季の日付に更新
            self.assertEqual(p["price_history"][-1]["date"], "2027-08-26")  # 今季の価格を履歴に足す
            self.assertNotIn(pid, [x for x in crawl.baseline_for("yoshinoya") if False])

    def test_relaunch_without_headline_resets_launch_date(self):
        pid = "yoshinoyanodon-tsukimi_gyutoji_don"
        gone = drop_everywhere("tsukimi_gyutoji_don")
        with TempData():
            run_y(getter(), "2026-10-11")
            run_y(getter(overrides=gone), "2026-10-12")
            run_y(getter(overrides=gone), "2026-10-13")
            ov = {"/news/2027/": "<html></html>"}
            run_y(getter(overrides=ov), "2027-08-26")
            p = crawl.load_product("yoshinoya", pid)
            self.assertEqual(p["status"], "on_sale")
            self.assertIsNone(p["launch_date"])  # 昨季の日付を今季として出さない
            self.assertEqual(p["seasons"][0]["launch_date"], "2026-08-27")
            self.assertEqual(g.season_observed_start(p), "2027-08-26")
            self.assertIn("2027-08-26（記録開始日）", g.period_text(p))

    def test_early_end_notice_is_attached_without_changing_status(self):
        with TempData():
            run_y(getter(), "2026-10-11")
            ov = {"/news/2026/": news_with("「牛さんま定食」早期販売終了のお知らせ", "2026.10.20")}
            run_y(getter(overrides=ov), "2026-10-20")
            p = crawl.load_product("yoshinoya", "gentei-gyusanma-tei")
            self.assertEqual(p["status"], "on_sale")  # 状態は一覧からの消失で決める
            self.assertEqual(p["early_end_notice"]["date"], "2026-10-20")
            self.assertIn("早期販売終了", p["early_end_notice"]["title"])
            # 別の商品(さんま定食)には付かない
            self.assertIsNone(crawl.load_product("yoshinoya", "gentei-sanma-tei").get("early_end_notice"))

    def test_headline_found_later_fills_null_launch_date(self):
        with TempData():
            run_y(getter(), "2026-10-11")
            self.assertIsNone(crawl.load_product("yoshinoya", "gentei-sanma-tei")["launch_date"])
            ov = {"/news/2026/": news_with("『さんま定食』10月8日より期間限定販売", "2026.10.12")}
            run_y(getter(overrides=ov), "2026-10-12")
            p = crawl.load_product("yoshinoya", "gentei-sanma-tei")
            self.assertEqual(p["launch_date"], "2026-10-08")

    def test_news_year_fetch_failure_does_not_stop_recording(self):
        with TempData():
            _, st = run_y(getter(fail={"/news/2026/"}), "2026-10-11")
            self.assertEqual(st["new_products"], 5)
            self.assertIsNone(crawl.load_product("yoshinoya", "gentei-gyusanma-tei")["launch_date"])

    def test_early_year_also_reads_previous_year_news(self):
        with TempData():
            g1 = getter(overrides={"/news/2025/": "<html></html>"})
            run_y(g1, "2026-02-01")
            self.assertTrue(any(u.endswith("/news/2025/") for u in g1.calls))
        with TempData():
            g2 = getter()
            run_y(g2, "2026-10-11")
            self.assertFalse(any(u.endswith("/news/2025/") for u in g2.calls))

    def test_detail_failure_goes_to_pending(self):
        with TempData():
            pend, st = run_y(getter(fail={"/menu/gentei/sanma/"}), "2026-10-11")
            self.assertEqual(st["failed"], 1)
            self.assertIn("gentei-sanma", pend["yoshinoya"])
            self.assertIsNone(crawl.load_product("yoshinoya", "gentei-sanma"))

    def test_chain_selectable_from_cli_and_configured(self):
        self.assertIn("yoshinoya", crawl.CRAWLERS)
        self.assertGreaterEqual(crawl.INTERVALS["yoshinoya"], 3)
        self.assertLessEqual(crawl.MAX_REQUESTS["yoshinoya"], 60)
        worst = sum(crawl.MAX_REQUESTS[c] * max(crawl.INTERVALS[c], 3) for c in crawl.CRAWLERS)
        self.assertLess(worst, 60 * 60)


def yoshi_item(**kw):
    base = {
        "chain": "yoshinoya", "product_id": "gentei-gyusanma-tei", "name": "牛さんま定食", "category": "定食",
        "price": 899, "price_note": "税込・店内の価格。", "price_variants": None, "takeout": False,
        "launch_date": "2026-10-08", "launch_date_source": "news_headline", "planned_end_text": None,
        "news_url": "https://www.yoshinoya.com/wp-content/uploads/2026/10/06124828/news_2026100701.pdf",
        "news_title": "「牛さんま定食」10月8日より期間限定販売", "news_date": "2026-10-07",
        "description": None, "nutrition": None, "nutrition_variants": None,
        "nutrition_pdf_url": "https://www.yoshinoya.com/pdf/allergy/", "nutrition_pdf_label": "吉野家メニュー情報",
        "origin_pdf_url": "https://www.yoshinoya.com/pdf/producing_area/",
        "allergens": None, "origin": None, "limited_kind": "期間限定", "badges": ["NEW", "期間限定"],
        "official_url": "https://www.yoshinoya.com/menu/gentei/gyusanma-tei/",
        "first_seen_at": "2026-10-11T07:00:00+09:00", "last_seen_at": "2026-10-11",
        "status": "on_sale", "ended_observed_at": None,
        "price_history": [{"date": "2026-10-11", "price": 899}], "image": None,
    }
    base.update(kw)
    return base


class TestSite(unittest.TestCase):
    def test_title_and_description(self):
        it = yoshi_item()
        self.assertEqual(g.seo_title(it), "吉野家「牛さんま定食」の価格・販売期間")
        d = g.seo_description(it)
        self.assertIn("吉野家の限定メニュー", d)
        self.assertIn("899円（税込）", d)
        self.assertNotIn("kcal", d)
        d2 = g.seo_description(yoshi_item(limited_kind="新規掲載"))
        self.assertIn("吉野家のメニュー", d2)
        self.assertNotIn("限定メニュー", d2)

    def test_item_page_links_to_pdf(self):
        h = g.render_item_page(yoshi_item())
        self.assertIn("<title>吉野家「牛さんま定食」の価格・販売期間｜ファストフードポチ</title>", h)
        self.assertIn("https://www.yoshinoya.com/pdf/allergy/", h)
        self.assertIn("吉野家メニュー情報", h)
        self.assertIn("PDFをご覧ください", h)
        self.assertNotIn("栄養成分は公式サイトに掲載がありませんでした", h)
        self.assertIn("var(--yoshinoya)", h)
        self.assertIn("公式のお知らせ（2026-10-07）の見出しによります", h)
        self.assertIn("テイクアウトはできません", h)
        self.assertNotIn("<blockquote", h)  # 説明文は載せない
        self.assertIn("2026-10-08〜 販売中", h)

    def test_item_page_early_end_and_seasons(self):
        h = g.render_item_page(yoshi_item(
            early_end_notice={"date": "2026-10-20", "title": "「牛さんま定食」早期販売終了のお知らせ", "url": "https://x/y.pdf"},
            seasons=[{"launch_date": "2025-10-01", "ended_observed_at": "2025-11-20"}],
            relaunches=[{"ended_observed_at": "2025-11-20", "reappeared_at": "2026-10-11"}]))
        self.assertIn("早期販売終了のお知らせ", h)
        self.assertIn("昨季の販売：2025-10-01〜2025-11-20", h)
        self.assertIn("再び登場しました", h)

    def test_other_chain_pages_unchanged_without_pdf(self):
        # PDFリンクを持たない商品(他チェーン)は従来どおりの文言
        h = g.render_nutrition({"chain": "mcdonalds", "nutrition": None})
        self.assertIn("栄養成分は公式サイトに掲載がありませんでした", h)

    def test_chain_in_config_and_text(self):
        self.assertEqual(g.CHAIN_NAME["yoshinoya"], "吉野家")
        self.assertIn("吉野家", g.CONFIG["site_description"])
        self.assertIn("吉野家", g.CONFIG["site_tagline"])
        self.assertTrue(any("吉野家" in p and "PDF" in p for p in g.CONFIG["about"]))
        self.assertIn("--yoshinoya:", g.CSS)
        self.assertIn('data-chain="yoshinoya"', g.CSS)
        colors = {c["slug"]: c["color"].lower() for c in g.CHAINS}
        self.assertEqual(len(set(colors.values())), len(colors))
        self.assertEqual(g.CHAIN_LIST_TEXT, "マクドナルド・モスバーガー・松屋・松のや・吉野家・すき家")

    def test_chain_list_footer_and_sections(self):
        self.assertIn("/chains/yoshinoya/", g.render_chains_top())
        self.assertIn("吉野家", g.render_chains_top())
        h = g.page_shell("x", "d", "<p>b</p>", "https://fastfood.netaful.jp/")
        self.assertIn("https://www.yoshinoya.com/", h)
        self.assertIn("吉野家、すき家各社", h)

    def test_card_and_grouping(self):
        it = yoshi_item()
        self.assertIn('data-chain="yoshinoya"', g.render_card(it))
        self.assertIn("吉野家", g.render_card(it))


class TestNetafulSplit(unittest.TestCase):
    def test_tag_and_filter(self):
        self.assertEqual(nr.TAGS["yoshinoya"], 765)
        self.assertIn("yoshinoya", nr.TITLE_ALWAYS)
        self.assertTrue(nr.title_passes_filter("yoshinoya", "【吉野家】「牛さんま定食」を食べてみた"))
        # タグに付く無関係の記事(タイトルに吉野家が無い)は採用しない
        self.assertFalse(nr.title_passes_filter("yoshinoya", "【すき家】「ローストポーク丼」を食べてみた"))

    def test_other_chains_unchanged(self):
        self.assertTrue(nr.title_passes_filter("mcdonalds", "なんでも"))
        self.assertTrue(nr.title_passes_filter("matsuya", "【松屋】「ネオカシミールカレー」"))
        self.assertTrue(nr.title_passes_filter("matsunoya", "【松のや】「油淋鶏定食」"))
        self.assertFalse(nr.title_passes_filter("matsunoya", "【吉野家】牛丼"))

    def test_related_keywords(self):
        self.assertEqual(g.match_keyword_for_name("牛さんま定食")["label"], "さんま")
        self.assertEqual(g.match_keyword_for_name("月見牛とじ丼")["label"], "牛とじ")
        self.assertEqual(g.match_keyword_for_name("月見牛とじ御膳")["label"], "牛とじ")
        self.assertEqual(g.match_keyword_for_name("牛丼・油そばセット")["label"], "油そば")
        self.assertEqual(g.match_keyword_for_name("豚丼")["label"], "豚丼")
        self.assertEqual(g.match_keyword_for_name("肉味噌ねぎ牛丼")["label"], "牛丼")
        # 既存チェーンの判定は変わらない
        self.assertEqual(g.match_keyword_for_name("ロースかつ定食")["label"], "ロースかつ")
        self.assertEqual(g.match_keyword_for_name("月見バーガー")["label"], "月見")


if __name__ == "__main__":
    unittest.main()
