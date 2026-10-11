"""松屋: パーサ・巡回と販売状況の追跡・価格・栄養2本立て・ニュース照合・サイト生成のテスト。
フィクスチャ(tests/fixtures/matsuya/)は2026-10-11に取得した公式ページのスナップショット。"""

from __future__ import annotations

import re
import unittest

import helpers  # noqa: F401
from helpers import FakeGetter, TempData, fx
import crawl
import generate_site as g
import parsers as P
from fetch import BlockedError


def run_matsuya(getter, day, pending=None):
    pending = {} if pending is None else pending
    stats = {}
    crawl.crawl_matsuya(getter, pending, stats, day)
    return pending, stats["matsuya"]


def drop_item(page: str, slug_part: str) -> str:
    """一覧ページから、hrefに slug_part を含む商品ブロックを消す。"""
    for b in re.findall(r'<li class="item">.*?<!-- //\.item -->', page, re.S):
        if slug_part in b:
            page = page.replace(b, "")
    return page


def clone_item(page: str, from_slug: str, new_slug: str, new_name: str | None = None) -> str:
    blocks = re.findall(r'<li class="item">.*?<!-- //\.item -->', page, re.S)
    src = next(b for b in blocks if from_slug in b)
    new = src.replace(from_slug, new_slug)
    if new_name:
        new = re.sub(r'(<span class="txt">)[^<]*(</span>)', rf"\g<1>{new_name}\g<2>", new)
    return page.replace(src, src + "\n" + new)


def getter(**kw):
    return FakeGetter(fallback_detail=True, **kw)


class TestListParser(unittest.TestCase):
    def test_limited_list(self):
        es = P.parse_matsuya_list(fx("matsuya/cat_limited.html"))
        self.assertEqual(len(es), 8)
        e = es[0]
        self.assertEqual(e["product_id"], "curry-kashmir_hp_261006")
        self.assertEqual(e["name"], "ネオカシミールカレー")
        self.assertEqual(e["price"], 680)
        self.assertEqual(e["product_no"], "27050")
        # `/menu/../` を含む実物のリンクが正規化される
        self.assertEqual(e["detail_path"], "/matsuya/menu/curry/kashmir_hp_261006.html")

    def test_options_have_no_price_and_are_flagged(self):
        es = P.parse_matsuya_list(fx("matsuya/cat_lunch.html"))
        opts = [e for e in es if e["option"]]
        self.assertEqual(len(opts), 3)
        self.assertTrue(all(e["price"] is None for e in opts))

    def test_normalize_path_and_id(self):
        p = P.matsuya_normalize_path("https://www.matsuyafoods.co.jp/menu/../matsuya/menu/hp/gyu3_hp_250422.html")
        self.assertEqual(p, "/matsuya/menu/hp/gyu3_hp_250422.html")
        self.assertEqual(P.matsuya_product_id(p), "hp-gyu3_hp_250422")
        self.assertIsNone(P.matsuya_product_id("/matsuya/menu/curry/index.html"))
        self.assertIsNone(P.matsuya_product_id("/shop/premium/"))
        self.assertEqual(P.matsuya_slug_date("curry-kashmir_hp_261006"), "2026-10-06")
        self.assertIsNone(P.matsuya_slug_date("morning-kobachi_gyuu_hp"))

    def test_category_discovery(self):
        cats = crawl.matsuya_discover_categories(fx("matsuya/menu.html"))
        self.assertEqual(len(cats), 12)
        self.assertIn("limited", cats)
        self.assertIn("topping", cats)


class TestDetailParser(unittest.TestCase):
    def test_set_and_single(self):
        d = P.parse_matsuya_detail(fx("matsuya/p_curry-kashmir_hp_261006.html"))
        self.assertEqual(d["name"], "ネオカシミールカレー")
        self.assertEqual(d["prices"], [{"label": "", "price": 680}, {"label": "単品", "price": 560}])
        self.assertEqual([v["label"] for v in d["nutrition_variants"]], ["", "単品"])
        self.assertEqual(d["nutrition_variants"][0]["nutrition"]["kcal"], 711.0)
        self.assertEqual(d["nutrition_variants"][1]["nutrition"]["kcal"], 322.0)
        self.assertEqual(d["nutrition_variants"][0]["nutrition"]["salt_g"], 6.3)
        self.assertEqual(d["nutrition_as_of"], "2026-10-06")
        self.assertIn("みそ汁・福神漬", d["nutrition_note"])
        self.assertIn("小麦", d["allergens"]["contains"])
        self.assertNotIn("そば", d["allergens"]["contains"])
        self.assertEqual(d["allergens"]["partial"], [])

    def test_labels_for_set(self):
        d = P.parse_matsuya_detail(fx("matsuya/p_curry-kashmir_hp_261006.html"))
        prices = P.matsuya_variant_labels(d["prices"], d["nutrition_note"])
        self.assertEqual(prices[0]["label"], "セット（みそ汁・福神漬を含む）")
        self.assertEqual(P.matsuya_pick_primary(prices)["price"], 680)

    def test_sizes_pick_regular(self):
        d = P.parse_matsuya_detail(fx("matsuya/p_don-don_gyuyaki_hp_260811.html"))
        self.assertEqual([p["label"] for p in d["prices"]], ["小盛", "並盛", "大盛"])
        self.assertEqual(P.matsuya_pick_primary(d["prices"])["price"], 630)
        self.assertEqual(P.matsuya_pick_primary(d["nutrition_variants"])["nutrition"]["kcal"], 757.0)
        # △は「含む」にせず、記号つきの条件つき表示として残す
        self.assertNotIn("鶏肉", d["allergens"]["contains"])
        self.assertIn({"name": "鶏肉", "mark": "△"}, d["allergens"]["partial"])

    def test_single_unlabeled_stays_unlabeled(self):
        v = P.matsuya_variant_labels([{"label": "", "price": 500}], "…みそ汁も含まれ…")
        self.assertEqual(v[0]["label"], "")

    def test_no_h1_is_none(self):
        self.assertIsNone(P.parse_matsuya_detail("<html>not found</html>"))


class TestNews(unittest.TestCase):
    def setUp(self):
        self.news = P.parse_matsuya_news(fx("matsuya/whatsnew_full.json"))

    def test_only_menu_category(self):
        self.assertTrue(0 < len(self.news) < 1636)
        self.assertGreaterEqual(self.news[0]["date"], self.news[-1]["date"])

    def test_title_match(self):
        a = P.match_matsuya_news_title("ネオカシミールカレー", self.news)
        self.assertEqual(a["date"], "2026-10-01")
        self.assertTrue(a["url"].endswith("/178231.html"))
        a2 = P.match_matsuya_news_title("黒麻婆豆腐定食", self.news)
        self.assertTrue(a2["url"].endswith("/177082.html"))

    def test_old_news_with_coincidental_name_is_not_matched(self):
        # 「新牛めし」は2022年の別商品の見出し(「"新"牛めし誕生」)に含まれる。掲載日から離れたニュースは採らない
        self.assertIsNotNone(P.match_matsuya_news_title("新牛めし", self.news))
        self.assertIsNone(P.match_matsuya_news_title("新牛めし", self.news, "2026-10-20"))
        self.assertIsNotNone(P.match_matsuya_news_title("ネオカシミールカレー", self.news, "2026-10-06"))
        # 掲載日が発売の少し前でも、見出しの日付が近ければ採る
        self.assertIsNotNone(P.match_matsuya_news_title("ネオカシミールカレー", self.news, "2026-09-25"))

    def test_series_sibling_is_not_in_title(self):
        self.assertIsNone(P.match_matsuya_news_title("ネオカシミールごろごろチキンカレー", self.news))

    def test_article_launch_date_and_description(self):
        a = P.parse_matsuya_article(fx("matsuya/n_178231.html"))
        self.assertEqual(a["launch_date"], "2026-10-06")
        short, _n = P.shorten_description(a["description_src"])
        self.assertTrue(short.endswith("。"))
        self.assertIn("カシミール", short)
        self.assertNotIn("このたび", short)
        a2 = P.parse_matsuya_article(fx("matsuya/n_177082.html"))
        self.assertEqual(a2["launch_date"], "2026-09-29")
        a3 = P.parse_matsuya_article(fx("matsuya/n_167268.html"))
        self.assertEqual(a3["launch_date"], "2026-08-11")
        self.assertIn("牛焼肉丼", a3["text"])

    def test_article_without_date(self):
        a = P.parse_matsuya_article('<article><div class="article-main"><p>お知らせ</p></div></article>')
        self.assertIsNone(a["launch_date"])


class TestCrawl(unittest.TestCase):
    def test_first_run_records_recommended_and_makes_baseline(self):
        with TempData():
            g1 = getter()
            _, st = run_matsuya(g1, "2026-10-11")
            self.assertTrue(st["list_ok"])
            self.assertFalse(st["blocked"])
            self.assertEqual(st["list_requests"], 14)  # robots + 入口 + 12カテゴリ
            self.assertEqual(st["limited_seen"], 8)
            self.assertEqual(st["new_products"], 8)
            self.assertEqual(len(crawl.load_chain_products("matsuya")), 8)
            baseline = crawl.baseline_for("matsuya")
            self.assertGreater(len(baseline), 60)
            # 選べるサイド(選択肢)はbaselineには入るが、商品としては記録しない
            self.assertIn("lunch-lunch_side_tamago_nama_hp_260407", baseline)
            self.assertIsNone(crawl.load_product("matsuya", "lunch-lunch_side_tamago_nama_hp_260407"))
            self.assertEqual(sum(1 for u in g1.calls if u.endswith("whatsnew.json")), 1)
            arts = [u for u in g1.calls if "/whatsnew/menu/" in u]
            self.assertEqual(len(arts), len(set(arts)))
            self.assertLessEqual(len(g1.calls), 60)

    def test_news_matching_and_fields(self):
        with TempData():
            run_matsuya(getter(), "2026-10-11")
            p = crawl.load_product("matsuya", "curry-kashmir_hp_261006")
            self.assertEqual(p["launch_date"], "2026-10-06")
            self.assertEqual(p["launch_date_source"], "news_article")
            self.assertEqual(p["limited_kind"], "新メニュー")
            self.assertTrue(p["news_url"].endswith("/178231.html"))
            self.assertIn("カシミール", p["description"])
            self.assertTrue(p["in_recommended"])
            self.assertEqual(p["category"], "カレー")
            self.assertEqual(p["status"], "on_sale")
            self.assertTrue(p["official_url"].startswith("https://www.matsuyafoods.co.jp/matsuya/menu/curry/"))
            self.assertIsNone(p["image"])
            self.assertEqual(p["price"], 680)
            self.assertEqual(p["price_variants"][1], {"label": "単品", "price": 560})
            self.assertEqual(p["nutrition"]["kcal"], 711.0)
            self.assertEqual([v["label"] for v in p["nutrition_variants"]],
                             ["セット（みそ汁・福神漬を含む）", "単品"])
            self.assertEqual(p["nutrition_as_of"], "2026-10-06")
            # 見出しに名前が無い兄弟商品は、同じ記事の本文から見つかる
            s = crawl.load_product("matsuya", "curry-kashmir_chikin_hp_261006")
            self.assertEqual(s["launch_date"], "2026-10-06")
            self.assertEqual(s["launch_date_source"], "news_article_body")
            m = crawl.load_product("matsuya", "teishoku-tei_kuromabo_hp_260929")
            self.assertEqual(m["launch_date"], "2026-09-29")
            y = crawl.load_product("matsuya", "don-don_gyuyaki_hp_260811")
            self.assertEqual(y["launch_date"], "2026-08-11")

    def test_unmatched_news_gives_null_launch(self):
        with TempData():
            g1 = getter(overrides={"/matsuya/whatsnew/data/whatsnew.json": '{"list_whatsnew": []}'})
            _, st = run_matsuya(g1, "2026-10-11")
            p = crawl.load_product("matsuya", "curry-kashmir_hp_261006")
            self.assertIsNone(p["launch_date"])
            self.assertEqual(p["launch_date_source"], "not_found")
            self.assertEqual(p["limited_kind"], "おすすめ掲載")
            self.assertEqual(st["news_unmatched"], 8)

    def test_news_fetch_failure_does_not_stop_recording(self):
        with TempData():
            g1 = getter(fail={"/matsuya/whatsnew/data/whatsnew.json"})
            _, st = run_matsuya(g1, "2026-10-11")
            self.assertEqual(st["new_products"], 8)

    def test_second_run_does_not_refetch_details(self):
        with TempData():
            run_matsuya(getter(), "2026-10-11")
            g2 = getter()
            _, st = run_matsuya(g2, "2026-10-12")
            self.assertEqual(st["new_products"], 0)
            self.assertEqual(st["detail_requests"], 0)
            self.assertEqual(len(g2.calls), 14)

    def _without_kuromabo(self):
        return {"/matsuya/menu/limited/index.html": drop_item(fx("matsuya/cat_limited.html"), "tei_kuromabo"),
                "/matsuya/menu/teishoku/index.html": drop_item(fx("matsuya/cat_teishoku.html"), "tei_kuromabo")}

    def test_end_needs_two_misses_on_different_days(self):
        pid = "teishoku-tei_kuromabo_hp_260929"
        with TempData():
            run_matsuya(getter(), "2026-10-11")
            ov = self._without_kuromabo()
            run_matsuya(getter(overrides=ov), "2026-10-12")
            self.assertEqual(crawl.load_product("matsuya", pid)["status"], "on_sale")
            run_matsuya(getter(overrides=ov), "2026-10-12")  # 同じ日の再実行は1回と数える
            self.assertEqual(crawl.load_product("matsuya", pid)["missed_runs"], 1)
            _, st = run_matsuya(getter(overrides=ov), "2026-10-13")
            p = crawl.load_product("matsuya", pid)
            self.assertEqual(p["status"], "ended")
            self.assertEqual(p["ended_observed_at"], "2026-10-13")
            self.assertEqual(st["ended"], 1)
            self.assertEqual(crawl.load_product("matsuya", "curry-kashmir_hp_261006")["status"], "on_sale")
            # 再登場したら販売中に戻り、relaunchesに記録される
            run_matsuya(getter(), "2026-10-20")
            p = crawl.load_product("matsuya", pid)
            self.assertEqual(p["status"], "on_sale")
            self.assertEqual(p["relaunches"][0]["reappeared_at"], "2026-10-20")

    def test_gone_from_recommended_but_still_on_menu_is_not_ended(self):
        """終了判定は「おすすめ」ではなく全カテゴリの一覧からの消失。"""
        with TempData():
            run_matsuya(getter(), "2026-10-11")
            ov = {"/matsuya/menu/limited/index.html": drop_item(fx("matsuya/cat_limited.html"), "tei_kuromabo")}
            for d in ("2026-10-12", "2026-10-13", "2026-10-14"):
                run_matsuya(getter(overrides=ov), d)
            p = crawl.load_product("matsuya", "teishoku-tei_kuromabo_hp_260929")
            self.assertEqual(p["status"], "on_sale")

    def test_failed_category_page_blocks_end_judgement(self):
        with TempData():
            run_matsuya(getter(), "2026-10-11")
            ov = self._without_kuromabo()
            for d in ("2026-10-12", "2026-10-13", "2026-10-14"):
                _, st = run_matsuya(getter(overrides=ov, fail={"/matsuya/menu/drink/index.html"}), d)
                self.assertFalse(st["list_ok"])
            p = crawl.load_product("matsuya", "teishoku-tei_kuromabo_hp_260929")
            self.assertEqual(p["status"], "on_sale")
            self.assertFalse(p.get("missed_runs"))

    def test_zero_item_category_is_not_success(self):
        with TempData():
            run_matsuya(getter(), "2026-10-11")
            ov = {"/matsuya/menu/don/index.html": "<html>構造が変わった</html>"}
            _, st = run_matsuya(getter(overrides=ov), "2026-10-12")
            self.assertFalse(st["list_ok"])

    def test_new_item_after_baseline_is_recorded(self):
        with TempData():
            run_matsuya(getter(), "2026-10-11")
            ov = {
                # おすすめには載らない新メニューと、トッピング・セット類だけに現れた商品
                "/matsuya/menu/gyumeshi/index.html": clone_item(
                    fx("matsuya/cat_gyumeshi.html"), "gyu3_hp_250422", "gyu_new_hp_261020", "新牛めし"),
                "/matsuya/menu/topping/index.html": clone_item(
                    fx("matsuya/cat_topping.html"), "set_kimuchi_h_hp_250422", "set_new_hp_261020", "新セット"),
            }
            _, st = run_matsuya(getter(overrides=ov), "2026-10-21")
            self.assertEqual(st["new_products"], 1)
            p = crawl.load_product("matsuya", "hp-gyu_new_hp_261020")
            self.assertEqual(p["name"], "新牛めし")
            self.assertEqual(p["limited_kind"], "新規掲載")
            self.assertFalse(p["in_recommended"])
            self.assertIsNone(crawl.load_product("matsuya", "topping-set_new_hp_261020"))

    def test_same_name_with_new_slug_is_new_record_with_link(self):
        with TempData():
            run_matsuya(getter(), "2026-10-11")
            ov = {"/matsuya/menu/gyumeshi/index.html": clone_item(
                fx("matsuya/cat_gyumeshi.html"), "gyu3_hp_250422", "gyu_re_hp_261101", "ネオカシミールカレー")}
            run_matsuya(getter(overrides=ov), "2026-11-02")
            p = crawl.load_product("matsuya", "hp-gyu_re_hp_261101")
            self.assertEqual(p["same_name_as"], ["curry-kashmir_hp_261006"])

    def test_price_change_is_logged(self):
        with TempData():
            run_matsuya(getter(), "2026-10-11")
            page = fx("matsuya/cat_limited.html").replace("680円", "700円")
            run_matsuya(getter(overrides={"/matsuya/menu/limited/index.html": page}), "2026-10-12")
            p = crawl.load_product("matsuya", "curry-kashmir_hp_261006")
            self.assertEqual(p["price"], 700)
            self.assertEqual([h["price"] for h in p["price_history"]], [680, 700])

    def test_blocked_stops_matsuya_only(self):
        def blocked(chain, url):
            raise BlockedError("HTTP 403")
        with TempData():
            pending, stats = {}, {}
            crawl.crawl_matsuya(blocked, pending, stats, "2026-10-11")
            self.assertTrue(stats["matsuya"]["blocked"])
            self.assertEqual(crawl.load_chain_products("matsuya"), [])
            self.assertIsNone(crawl.baseline_for("matsuya"))

    def test_robots_disallow_is_respected(self):
        with TempData():
            ov = {"/robots.txt": "User-agent: *\nDisallow: /matsuya/menu/limited/\n"}
            g1 = getter(overrides=ov)
            _, st = run_matsuya(g1, "2026-10-11")
            self.assertFalse(st["list_ok"])
            self.assertFalse(any("/limited/" in u for u in g1.calls))
            self.assertIsNone(crawl.baseline_for("matsuya"))

    def test_main_rejects_unknown_chain(self):
        self.assertEqual(crawl.main(["nosuchchain"]), 2)

    def test_request_limit_and_interval_configured(self):
        self.assertGreaterEqual(crawl.INTERVALS["matsuya"], 3)
        self.assertLessEqual(crawl.MAX_REQUESTS["matsuya"], 60)


def matsuya_item(**kw):
    nut_set = {"kcal": 711.0, "protein_g": 12.4, "fat_g": 21.9, "carbs_g": 111.6, "sugar_g": None,
               "fiber_g": None, "salt_g": 6.3, "other": {}}
    nut_one = {"kcal": 322.0, "protein_g": 5.4, "fat_g": 19.9, "carbs_g": 29.8, "sugar_g": None,
               "fiber_g": None, "salt_g": 4.3, "other": {}}
    base = {
        "chain": "matsuya", "product_id": "curry-kashmir_hp_261006", "name": "ネオカシミールカレー",
        "category": "カレー", "price": 680, "price_note": "税込。表示は「セット（みそ汁・福神漬を含む）」の価格。",
        "price_variants": [{"label": "セット（みそ汁・福神漬を含む）", "price": 680}, {"label": "単品", "price": 560}],
        "launch_date": "2026-10-06", "launch_date_source": "news_article", "planned_end_text": None,
        "news_url": "https://www.matsuyafoods.co.jp/matsuya/whatsnew/menu/178231.html",
        "description": "本格的な辛さはそのままに、食べやすさまで追求した\"ネオ\"カシミール。",
        "nutrition": nut_set,
        "nutrition_variants": [{"label": "セット（みそ汁・福神漬を含む）", "nutrition": nut_set},
                               {"label": "単品", "nutrition": nut_one}],
        "nutrition_as_of": "2026-10-06",
        "nutrition_note": "特定原材料・栄養成分の数値には、みそ汁・福神漬も含まれております。",
        "allergens": {"contains": ["小麦", "乳"], "partial": [{"name": "鶏肉", "mark": "△"}]},
        "origin": None, "limited_kind": "新メニュー",
        "official_url": "https://www.matsuyafoods.co.jp/matsuya/menu/curry/kashmir_hp_261006.html",
        "first_seen_at": "2026-10-11T07:00:00+09:00", "last_seen_at": "2026-10-11",
        "status": "on_sale", "ended_observed_at": None,
        "price_history": [{"date": "2026-10-11", "price": 680}], "image": None,
    }
    base.update(kw)
    return base


class TestSite(unittest.TestCase):
    def test_title_and_description(self):
        it = matsuya_item()
        self.assertEqual(g.seo_title(it), "松屋「ネオカシミールカレー」のカロリー・価格・栄養成分")
        self.assertEqual(g.seo_title(matsuya_item(nutrition=None)), "松屋「ネオカシミールカレー」の価格・販売期間")
        d = g.seo_description(it)
        self.assertIn("松屋のメニュー", d)
        self.assertNotIn("限定メニュー", d)
        self.assertIn("711kcal", d)

    def test_item_page_shows_both_values(self):
        h = g.render_item_page(matsuya_item())
        self.assertIn("<title>松屋「ネオカシミールカレー」のカロリー・価格・栄養成分｜ファストフードポチ</title>", h)
        self.assertIn("680円", h)
        self.assertIn("560円（税込）", h)  # 単品の価格
        self.assertIn("711kcal", h)
        self.assertIn("322kcal", h)  # 単品の栄養
        self.assertIn("みそ汁・福神漬", h)
        self.assertIn("2026年10月6日現在", h)
        self.assertIn("鶏肉△", h)
        # 説明文はニュース記事から引用している
        self.assertIn('cite="https://www.matsuyafoods.co.jp/matsuya/whatsnew/menu/178231.html"', h)
        self.assertIn("松屋公式サイトのニュース", h)
        self.assertIn("「期間限定」の表示も販売期間も載せていません", h)
        self.assertIn("var(--matsuya)", h)

    def test_single_value_item_has_no_multi_table(self):
        h = g.render_item_page(matsuya_item(nutrition_variants=None, price_variants=None))
        self.assertNotIn("class='multi'", h)
        self.assertIn("711kcal", h)

    def test_ended_note(self):
        it = matsuya_item(status="ended", ended_observed_at="2026-11-30")
        self.assertIn("消えたことを確認した日", g.period_text(it, detailed=True))

    def test_chain_in_config(self):
        self.assertIn("matsuya", g.CHAIN_NAME)
        self.assertIn("松屋", g.CONFIG["site_description"])
        self.assertTrue(any("松屋" in p for p in g.CONFIG["about"]))


if __name__ == "__main__":
    unittest.main()
