"""松のや: 松屋フーズ系パーサ・巡回の共通化(松屋とは接頭辞・おすすめカテゴリだけ違う)、
ニュース照合、サイト生成、ネタフル記事の松屋/松のや振り分けのテスト。
フィクスチャ(tests/fixtures/matsunoya/)は2026-10-11に取得した公式ページのスナップショット。
取得していないカテゴリの一覧は helpers.matsunoya_category_page がosusumeから作る。"""

from __future__ import annotations

import re
import unittest

import helpers  # noqa: F401
from helpers import FakeGetter, TempData, fx
import crawl
import fetch_netaful_reviews as nr
import generate_site as g
import parsers as P
from fetch import BlockedError

BLOCK_RE = re.compile(r'<li class="item">.*?<!-- //\.item -->', re.S)


def run_matsunoya(getter, day, pending=None):
    pending = {} if pending is None else pending
    stats = {}
    crawl.crawl_matsunoya(getter, pending, stats, day)
    return pending, stats["matsunoya"]


def getter(**kw):
    return FakeGetter(fallback_detail=True, **kw)


def drop_item(page: str, slug_part: str) -> str:
    for b in BLOCK_RE.findall(page):
        if slug_part in b:
            page = page.replace(b, "")
    return page


def clone_item(page: str, from_slug: str, new_slug: str, new_name: str | None = None) -> str:
    src = next(b for b in BLOCK_RE.findall(page) if from_slug in b)
    new = src.replace(from_slug, new_slug)
    if new_name:
        new = re.sub(r'(<span class="txt">)[^<]*(</span>)', rf"\g<1>{new_name}\g<2>", new)
    return page.replace(src, src + "\n" + new)


class TestParsers(unittest.TestCase):
    def test_list_uses_brand_root(self):
        es = P.parse_matsuya_list(fx("matsunoya/cat_osusume.html"), "matsunoya")
        self.assertEqual(len(es), 23)
        e = es[0]
        self.assertEqual(e["product_id"], "souzai-souzai_set_nebiki_rosu1_kaki2_hp_261007")
        self.assertEqual(e["price"], 490)
        self.assertEqual(e["detail_path"], "/matsunoya/menu/souzai/souzai_set_nebiki_rosu1_kaki2_hp_261007.html")
        # 松屋として読むと、パスの接頭辞が違うので商品にならない
        self.assertEqual(P.parse_matsuya_list(fx("matsunoya/cat_osusume.html")), [])

    def test_matsuya_defaults_unchanged(self):
        es = P.parse_matsuya_list(fx("matsuya/cat_limited.html"))
        self.assertEqual(es[0]["product_id"], "curry-kashmir_hp_261006")
        self.assertEqual(P.matsuya_product_id("/matsuya/menu/curry/x_hp_261006.html"), "curry-x_hp_261006")
        self.assertIsNone(P.matsuya_product_id("/matsunoya/menu/curry/x_hp_261006.html"))
        self.assertEqual(P.matsuya_product_id("/matsunoya/menu/teishoku/tei_aji_hp_260916.html", "matsunoya"),
                         "teishoku-tei_aji_hp_260916")

    def test_category_discovery(self):
        cats = crawl.matsuya_discover_categories(fx("matsunoya/menu.html"), "matsunoya")
        self.assertEqual(sorted(cats), sorted(helpers.MATSUNOYA_MENU_CATS))
        # 松屋の入口として読むと松のやのカテゴリは拾わない
        self.assertEqual(crawl.matsuya_discover_categories(fx("matsunoya/menu.html")), [])

    def test_detail_same_structure(self):
        d = P.parse_matsuya_detail(fx("matsunoya/p_teishoku-tei_kaki5_hp_260916.html"))
        self.assertEqual(d["name"], "カキフライ（5個）定食")
        self.assertEqual(d["prices"], [{"label": "", "price": 1030}])
        self.assertEqual(d["nutrition_variants"][0]["nutrition"]["kcal"], 894.0)
        self.assertEqual(d["nutrition_as_of"], "2026-10-07")
        self.assertIn("小麦", d["allergens"]["contains"])
        d2 = P.parse_matsuya_detail(fx("matsunoya/p_donburi-don_kaki_hp_260916.html"))
        self.assertEqual([p["label"] for p in d2["prices"]], ["並盛", "大盛"])
        self.assertEqual(P.matsuya_pick_primary(d2["prices"])["price"], 930)

    def test_name_variants_strip_brand_prefix(self):
        self.assertEqual(P.matsuya_name_variants("松のやの背脂生姜タレ定食", "matsunoya")[1], "背脂生姜タレ定食")
        self.assertEqual(len(P.matsuya_name_variants("松のやの背脂生姜タレ定食", "matsuya")), 1)
        self.assertEqual(P.matsuya_name_variants("松屋の牛めしランチ")[1], "牛めしランチ")


class TestNews(unittest.TestCase):
    def setUp(self):
        self.news = P.parse_matsuya_news(fx("matsunoya/whatsnew_full.json"))

    def test_menu_only(self):
        self.assertTrue(0 < len(self.news) < 926)
        self.assertGreaterEqual(self.news[0]["date"], self.news[-1]["date"])

    def test_title_match(self):
        a = P.match_matsuya_news_title("ジンギスカン定食", self.news, "2026-10-09", "matsunoya")
        self.assertEqual(a["date"], "2026-10-09")
        self.assertTrue(a["url"].endswith("/185627.html"))

    def test_article(self):
        a = P.parse_matsuya_article(fx("matsunoya/n_175938.html"))
        self.assertEqual(a["launch_date"], "2026-09-16")
        short, _n = P.shorten_description(a["description_src"])
        self.assertTrue(short.endswith("。"))
        self.assertLessEqual(len(short), 100)


@helpers.needs_fixtures
class TestCrawl(unittest.TestCase):
    def test_first_run_records_recommended_and_makes_baseline(self):
        with TempData():
            g1 = getter()
            _, st = run_matsunoya(g1, "2026-10-11")
            self.assertTrue(st["list_ok"])
            self.assertFalse(st["blocked"])
            self.assertEqual(st["list_requests"], 10)  # robots + 入口 + 8カテゴリ
            self.assertEqual(st["limited_seen"], 23)
            self.assertEqual(st["new_products"], 23)
            self.assertEqual(len(crawl.load_chain_products("matsunoya")), 23)
            self.assertEqual(crawl.load_chain_products("matsuya"), [])
            self.assertGreater(len(crawl.baseline_for("matsunoya")), 30)
            self.assertIsNone(crawl.baseline_for("matsuya"))
            self.assertEqual(sum(1 for u in g1.calls if u.endswith("whatsnew.json")), 1)
            self.assertTrue(all(u.startswith(crawl.MATSUYA_BASE) for u in g1.calls))
            self.assertFalse(any("/matsuya/" in u for u in g1.calls))
            arts = [u for u in g1.calls if "/whatsnew/menu/" in u]
            self.assertEqual(len(arts), len(set(arts)))
            self.assertLessEqual(len(g1.calls), crawl.MAX_REQUESTS["matsunoya"])

    def test_product_fields(self):
        with TempData():
            run_matsunoya(getter(), "2026-10-11")
            p = crawl.load_product("matsunoya", "teishoku-tei_kaki5_hp_260916")
            self.assertEqual(p["chain"], "matsunoya")
            self.assertEqual(p["status"], "on_sale")
            self.assertTrue(p["in_recommended"])
            self.assertEqual(p["category"], "定食")
            self.assertEqual(p["price"], 1030)
            self.assertEqual(p["nutrition"]["kcal"], 894.0)
            self.assertIsNone(p["image"])
            self.assertTrue(p["official_url"].startswith("https://www.matsuyafoods.co.jp/matsunoya/menu/teishoku/"))
            # 「おすすめ」の列はカテゴリ名にしない
            c = crawl.load_product("matsunoya", "souzai-souzai_set_nebiki_rosu1_kaki2_hp_261007")
            self.assertEqual(c["category"], "惣菜")
            self.assertIn("osusume", c["source_list"])

    def test_news_matching_by_title_and_unmatched(self):
        with TempData():
            ov = {"/matsunoya/menu/teishoku/index.html": clone_item(
                fx("matsunoya/cat_teishoku.html"), "tei_aji_hp_260916", "tei_jingisukan_hp_261009", "ジンギスカン定食")}
            run_matsunoya(getter(overrides=ov), "2026-10-11")
            p = crawl.load_product("matsunoya", "teishoku-tei_jingisukan_hp_261009")
            self.assertIsNone(p)  # おすすめにも載らず、baseline作成回なので記録しない
            run_matsunoya(getter(overrides=ov), "2026-10-12")
        with TempData():
            run_matsunoya(getter(), "2026-10-11")
            ov = {"/matsunoya/menu/teishoku/index.html": clone_item(
                fx("matsunoya/cat_teishoku.html"), "tei_aji_hp_260916", "tei_jingisukan_hp_261009", "ジンギスカン定食")}
            _, st = run_matsunoya(getter(overrides=ov), "2026-10-12")
            self.assertEqual(st["new_products"], 1)
            p = crawl.load_product("matsunoya", "teishoku-tei_jingisukan_hp_261009")
            self.assertEqual(p["limited_kind"], "新メニュー")
            self.assertEqual(p["news_date"], "2026-10-09")
            self.assertTrue(p["news_url"].endswith("/185627.html"))
            self.assertFalse(p["in_recommended"])
            self.assertEqual(st["news_matched"], 1)

    def test_news_fetch_failure_does_not_stop_recording(self):
        with TempData():
            _, st = run_matsunoya(getter(fail={"/matsunoya/whatsnew/data/whatsnew.json"}), "2026-10-11")
            self.assertEqual(st["new_products"], 23)
            p = crawl.load_product("matsunoya", "teishoku-tei_kaki5_hp_260916")
            self.assertEqual(p["limited_kind"], "おすすめ掲載")

    def test_second_run_does_not_refetch_details(self):
        with TempData():
            run_matsunoya(getter(), "2026-10-11")
            g2 = getter()
            _, st = run_matsunoya(g2, "2026-10-12")
            self.assertEqual(st["new_products"], 0)
            self.assertEqual(st["detail_requests"], 0)
            self.assertEqual(len(g2.calls), 10)

    def _without_aji(self):
        return {f"/matsunoya/menu/{c}/index.html": drop_item(helpers.matsunoya_category_page(c), "tei_aji_hp_260916")
                for c in ("osusume", "teishoku")}

    def test_end_needs_two_misses_on_different_days(self):
        pid = "teishoku-tei_aji_hp_260916"
        with TempData():
            run_matsunoya(getter(), "2026-10-11")
            ov = self._without_aji()
            run_matsunoya(getter(overrides=ov), "2026-10-12")
            self.assertEqual(crawl.load_product("matsunoya", pid)["status"], "on_sale")
            _, st = run_matsunoya(getter(overrides=ov), "2026-10-13")
            p = crawl.load_product("matsunoya", pid)
            self.assertEqual(p["status"], "ended")
            self.assertEqual(p["ended_observed_at"], "2026-10-13")
            self.assertEqual(st["ended"], 1)
            run_matsunoya(getter(), "2026-10-20")
            self.assertEqual(crawl.load_product("matsunoya", pid)["status"], "on_sale")

    def test_gone_from_osusume_but_still_on_menu_is_not_ended(self):
        with TempData():
            run_matsunoya(getter(), "2026-10-11")
            ov = {"/matsunoya/menu/osusume/index.html": drop_item(fx("matsunoya/cat_osusume.html"), "tei_aji_hp_260916")}
            for d in ("2026-10-12", "2026-10-13", "2026-10-14"):
                run_matsunoya(getter(overrides=ov), d)
            self.assertEqual(crawl.load_product("matsunoya", "teishoku-tei_aji_hp_260916")["status"], "on_sale")

    def test_failed_category_blocks_end_judgement(self):
        with TempData():
            run_matsunoya(getter(), "2026-10-11")
            ov = self._without_aji()
            for d in ("2026-10-12", "2026-10-13", "2026-10-14"):
                _, st = run_matsunoya(getter(overrides=ov, fail={"/matsunoya/menu/morning/index.html"}), d)
                self.assertFalse(st["list_ok"])
            self.assertEqual(crawl.load_product("matsunoya", "teishoku-tei_aji_hp_260916")["status"], "on_sale")

    def test_new_item_after_baseline_is_recorded(self):
        with TempData():
            run_matsunoya(getter(), "2026-10-11")
            ov = {"/matsunoya/menu/teishoku/index.html": clone_item(
                fx("matsunoya/cat_teishoku.html"), "tei_rosu_hp_260506", "tei_new_hp_261020", "新ロースかつ定食")}
            _, st = run_matsunoya(getter(overrides=ov), "2026-10-21")
            self.assertEqual(st["new_products"], 1)
            p = crawl.load_product("matsunoya", "teishoku-tei_new_hp_261020")
            self.assertEqual(p["limited_kind"], "新規掲載")
            self.assertFalse(p["in_recommended"])

    def test_matsuya_and_matsunoya_do_not_mix(self):
        """同じ実行で両方巡回しても、記録・baselineはチェーンごとに別。"""
        with TempData():
            run_matsunoya(getter(), "2026-10-11")
            stats = {}
            crawl.crawl_matsuya(FakeGetter(fallback_detail=True), {}, stats, "2026-10-11")
            self.assertEqual(stats["matsuya"]["new_products"], 8)
            self.assertEqual(len(crawl.load_chain_products("matsunoya")), 23)
            self.assertEqual(len(crawl.load_chain_products("matsuya")), 8)
            self.assertNotEqual(crawl.baseline_for("matsunoya"), crawl.baseline_for("matsuya"))

    def test_blocked_stops_matsunoya_only(self):
        def blocked(chain, url):
            raise BlockedError("HTTP 403")
        with TempData():
            pending, stats = {}, {}
            crawl.crawl_matsunoya(blocked, pending, stats, "2026-10-11")
            self.assertTrue(stats["matsunoya"]["blocked"])
            self.assertEqual(crawl.load_chain_products("matsunoya"), [])
            self.assertIsNone(crawl.baseline_for("matsunoya"))

    def test_robots_disallow_is_respected(self):
        with TempData():
            ov = {"/robots.txt": "User-agent: *\nDisallow: /matsunoya/menu/osusume/\n"}
            g1 = getter(overrides=ov)
            _, st = run_matsunoya(g1, "2026-10-11")
            self.assertFalse(st["list_ok"])
            self.assertFalse(any("/osusume/" in u for u in g1.calls))
            self.assertIsNone(crawl.baseline_for("matsunoya"))

    def test_chain_selectable_from_cli_and_configured(self):
        self.assertIn("matsunoya", crawl.CRAWLERS)
        self.assertEqual(crawl.main(["nosuchchain"]), 2)
        self.assertGreaterEqual(crawl.INTERVALS["matsunoya"], 3)
        self.assertLessEqual(crawl.MAX_REQUESTS["matsunoya"], 60)
        # 全チェーン合計の最悪値が workflow の timeout(60分)に収まる: 上限リクエスト数×間隔
        worst = sum(crawl.MAX_REQUESTS[c] * max(crawl.INTERVALS[c], 3) for c in crawl.CRAWLERS)
        self.assertLess(worst, 60 * 60)


def noya_item(**kw):
    nut = {"kcal": 894.0, "protein_g": 16.9, "fat_g": 39.6, "carbs_g": 121.5, "sugar_g": None,
           "fiber_g": None, "salt_g": 3.6, "other": {}}
    base = {
        "chain": "matsunoya", "product_id": "teishoku-tei_kaki5_hp_260916", "name": "カキフライ（5個）定食",
        "category": "定食", "price": 1030, "price_note": "税込。", "price_variants": None,
        "launch_date": "2026-09-16", "launch_date_source": "news_article", "planned_end_text": None,
        "news_url": "https://www.matsuyafoods.co.jp/matsunoya/whatsnew/menu/175938.html",
        "description": "肉厚な真あじはふわっと、カキはぷりっとジューシーに。",
        "nutrition": nut, "nutrition_variants": None, "nutrition_as_of": "2026-10-07",
        "nutrition_note": "特定原材料・栄養成分の数値には、みそ汁も含まれております。",
        "allergens": {"contains": ["小麦"], "partial": []}, "origin": None, "limited_kind": "新メニュー",
        "official_url": "https://www.matsuyafoods.co.jp/matsunoya/menu/teishoku/tei_kaki5_hp_260916.html",
        "first_seen_at": "2026-10-11T07:00:00+09:00", "last_seen_at": "2026-10-11",
        "status": "on_sale", "ended_observed_at": None,
        "price_history": [{"date": "2026-10-11", "price": 1030}], "image": None,
    }
    base.update(kw)
    return base


class TestSite(unittest.TestCase):
    def test_title_and_description(self):
        it = noya_item()
        self.assertEqual(g.seo_title(it), "松のや「カキフライ（5個）定食」のカロリー・価格・栄養成分")
        self.assertEqual(g.seo_title(noya_item(nutrition=None)), "松のや「カキフライ（5個）定食」の価格・販売期間")
        d = g.seo_description(it)
        self.assertIn("松のやのメニュー", d)
        self.assertNotIn("限定メニュー", d)
        self.assertIn("894kcal", d)

    def test_item_page(self):
        h = g.render_item_page(noya_item())
        self.assertIn("<title>松のや「カキフライ（5個）定食」のカロリー・価格・栄養成分｜ファストフードポチ</title>", h)
        self.assertIn("1030円", h)
        self.assertIn("松のや公式サイトのニュース", h)
        self.assertIn("松のやは公式サイトに「期間限定」の表示も販売期間も載せていません", h)
        self.assertIn("var(--matsunoya)", h)

    def test_chain_in_config_and_text(self):
        self.assertIn("matsunoya", g.CHAIN_NAME)
        self.assertEqual(g.CHAIN_NAME["matsunoya"], "松のや")
        self.assertIn("松のや", g.CONFIG["site_description"])
        self.assertIn("松のや", g.CONFIG["site_tagline"])
        self.assertTrue(any("松のや" in p for p in g.CONFIG["about"]))
        self.assertIn("--matsunoya:", g.CSS)
        self.assertIn('data-chain="matsunoya"', g.CSS)
        colors = {c["slug"]: c["color"].lower() for c in g.CHAINS}
        self.assertEqual(len(set(colors.values())), len(colors))

    def test_footer_and_chain_list_mention_matsunoya(self):
        h = g.render_chains_top()
        self.assertIn("/chains/matsunoya/", h)
        self.assertIn("松のや", h)


class TestNetafulSplit(unittest.TestCase):
    def test_tags(self):
        self.assertEqual(nr.TAGS["matsunoya"], 937)
        self.assertEqual(nr.TAGS["matsuya"], 729)

    def test_matsunoya_titles_go_to_matsunoya_only(self):
        for t in ("【松のや】「油淋鶏定食」ニンニク×レモンの香味ダレが旨すぎた！",
                  "【松屋・松のや】「創業ビーフカレー」のカツカレー「創業ビーフロースかつカレー」を食べてみた",
                  "【松屋】2023年に食べた46食まとめ＆ベスト5【松のや】"):
            self.assertTrue(nr.title_passes_filter("matsunoya", t), t)
            self.assertFalse(nr.title_passes_filter("matsuya", t), t)

    def test_matsuya_titles_stay_with_matsuya(self):
        t = "【松屋】「ネオカシミールカレー」を食べた"
        self.assertTrue(nr.title_passes_filter("matsuya", t))
        self.assertFalse(nr.title_passes_filter("matsunoya", t))
        # 松のやのタグに付く無関係の記事(他チェーン)は採用しない
        self.assertFalse(nr.title_passes_filter("matsunoya", "「松乃家」カツカレーとも一味違う！だしカレーソースで食べるとんかつ"))
        self.assertIn("matsuya", nr.TITLE_ALWAYS)
        self.assertIn("matsunoya", nr.TITLE_ALWAYS)

    def test_other_chains_unchanged(self):
        self.assertTrue(nr.title_passes_filter("mcdonalds", "なんでも"))
        self.assertTrue(nr.title_passes_filter("mos", "【モスバーガー】月見フォカッチャ"))

    def test_related_keywords_for_tonkatsu(self):
        self.assertEqual(g.match_keyword_for_name("ロースかつ定食")["label"], "ロースかつ")
        self.assertEqual(g.match_keyword_for_name("ヒレかつ定食")["label"], "ヒレかつ")
        self.assertEqual(g.match_keyword_for_name("ロースと野菜の合い盛りタレかつ丼")["label"], "かつ丼")
        self.assertEqual(g.match_keyword_for_name("得朝ロースかつ定食")["label"], "ロースかつ")


if __name__ == "__main__":
    unittest.main()
