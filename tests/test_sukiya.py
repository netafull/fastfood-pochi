"""すき家: メニュー入口・カテゴリ一覧・詳細・お知らせ見出しのパーサ、お知らせの照合(完全一致・90日窓)、
baseline、販売状況の追跡、価格の複数表示と取り直し、再登場、title生成、ネタフル記事の振り分けのテスト。
フィクスチャ(tests/fixtures/sukiya/)は2026-10-11に取得した公式ページのスナップショット
(入口・全13カテゴリの一覧・詳細3件・お知らせ一覧)。PDFは取得していない。"""

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

ITEM = '<li class="page_menu_list_li">'
DAY1 = "2026-10-11"


def run_s(getter, day, pending=None):
    pending = {} if pending is None else pending
    stats = {}
    crawl.crawl_sukiya(getter, pending, stats, day)
    return pending, stats["sukiya"]


def getter(**kw):
    return FakeGetter(fallback_detail=True, **kw)


def list_path(cat: str) -> str:
    return f"/menu/in/{cat}/"


def drop_item(page: str, num: str) -> str:
    head, *items = page.split(ITEM)
    keep = [i for i in items if f"/{num}/index.html" not in i.split("</a>")[0]]
    return head + "".join(ITEM + i for i in keep)


def drop_everywhere(num: str) -> dict:
    return {list_path(c): drop_item(fx(f"sukiya/list_{c}.html"), num) for c in helpers.SUKIYA_CATS}


def clone_item(page: str, from_num: str, new_num: str, new_name: str, plain: bool = True) -> str:
    """from_num の商品欄を複製して新しい番号・名前の商品を足す(plain なら公式アイコンを外す)。"""
    head, *items = page.split(ITEM)
    src = next(i for i in items if f"/{from_num}/index.html" in i.split("</a>")[0])
    new = src.replace(from_num, new_num)
    new = re.sub(r'(<div class="pro_name">)[^<]*(</div>)', rf"\g<1>{new_name}\g<2>", new)
    if plain:
        new = re.sub(r'<ul class="icn_list">.*?</ul>', '<ul class="icn_list"></ul>', new, flags=re.S)
    return head + "".join(ITEM + i + (ITEM + new if i is src else "") for i in items)


def news_with(title: str, date: str, fname: str = "Press_sukiya_20261012_x.pdf") -> str:
    """お知らせ一覧の先頭に1件足したページ。date は 2026年10月12日 の形。"""
    page = fx("sukiya/news.html")
    unit = (f'<div class="news_list press_release"><time>{date}</time>'
            f'<a href="/news/{fname}"><span class="cate">プレスリリース</span>{title}</a></div>')
    return page.replace('<div id="new_list" class="tab_contents tab_content">',
                        '<div id="new_list" class="tab_contents tab_content">' + unit, 1)


class TestParsers(unittest.TestCase):
    def test_index_follows_links_and_skips_commented_out_categories(self):
        cats = P.parse_sukiya_index(fx("sukiya/menu_index.html"))
        self.assertEqual([c["slug"] for c in cats], helpers.SUKIYA_CATS)
        self.assertNotIn("gyusuki", [c["slug"] for c in cats])  # HTMLコメント内の旧カテゴリ
        self.assertNotIn("udon", [c["slug"] for c in cats])
        self.assertEqual(cats[0]["label"], "牛丼")

    def test_path_resolution_uses_page_host(self):
        self.assertEqual(P.sukiya_normalize_path("/news/Press_sukiya_20261007_rostepork.pdf"),
                         "/news/Press_sukiya_20261007_rostepork.pdf")
        self.assertEqual(P.sukiya_normalize_path("https://www.sukiya.jp/menu/in/special/"), "/menu/in/special/")
        self.assertIsNone(P.sukiya_normalize_path("https://news.sukiya.jp/special/cm/index.html"))
        self.assertIsNone(P.sukiya_normalize_path("https://maps.sukiya.jp/jp/index.html"))

    def test_product_id(self):
        self.assertEqual(P.sukiya_product_id("/menu/in/special/400100/index.html"), "special-400100")
        self.assertIsNone(P.sukiya_product_id("/menu/in/special/"))
        self.assertIsNone(P.sukiya_product_id("/menu/"))

    def test_list_icons_and_names(self):
        es = {e["product_id"]: e for e in P.parse_sukiya_list(fx("sukiya/list_drink.html"))}
        self.assertEqual(len(es), 19)
        shake = es["drink-810500"]
        self.assertEqual(shake["name"], "Sukiシェイク焼き芋")  # <br> は消す
        self.assertTrue(shake["is_new"] and shake["is_limited"] and shake["is_store_limited"])
        self.assertFalse(es["drink-806500"]["is_limited"])
        self.assertEqual(es["drink-810500"]["detail_path"], "/menu/in/drink/810500/index.html")
        self.assertIsNone(es["drink-810500"]["price"])  # 一覧に価格は無い

    def test_all_lists_parse_and_ids_are_unique(self):
        seen = {}
        for c in helpers.SUKIYA_CATS:
            es = P.parse_sukiya_list(fx(f"sukiya/list_{c}.html"))
            self.assertTrue(es, c)
            for e in es:
                self.assertEqual(e["category"], c)
                self.assertNotIn(e["product_id"], seen)
                seen[e["product_id"]] = e
        self.assertEqual(len(seen), 174)
        # 数字だけでも一意(別カテゴリに同じ数字は出ない)
        nums = [pid.split("-", 1)[1] for pid in seen]
        self.assertEqual(len(nums), len(set(nums)))

    def test_same_name_in_different_categories_has_different_ids(self):
        names = {}
        for c in ("morning", "side"):
            for e in P.parse_sukiya_list(fx(f"sukiya/list_{c}.html")):
                names.setdefault(e["name"], set()).add(e["product_id"])
        self.assertEqual(names["季節の小鉢"], {"morning-513500", "side-7004000"})

    def test_detail_with_several_prices(self):
        d = P.parse_sukiya_detail(fx("sukiya/detail_special-400100.html"))
        self.assertEqual(d["name"], "牛皿定食")
        self.assertEqual([r["price"] for r in d["prices"]], [700, 900, 1070])
        self.assertEqual(d["prices"][0]["label"], "（肉）並盛")
        d = P.parse_sukiya_detail(fx("sukiya/detail_gyudon-112700.html"))
        self.assertEqual([r["label"] for r in d["prices"]], ["ミニ", "並盛", "中盛", "大盛", "特盛", "メガ"])
        self.assertEqual(P.sukiya_pick_primary(d)["price"], 780)  # 並盛を代表に(ミニ720ではない)
        d = P.parse_sukiya_detail(fx("sukiya/detail_drink-810500.html"))
        self.assertEqual(d["name"], "Sukiシェイク焼き芋")
        self.assertEqual(P.sukiya_pick_primary(d)["price"], 210)  # 並盛が無ければ先頭

    def test_detail_without_price_is_none(self):
        self.assertIsNone(P.parse_sukiya_detail("<html><body><h2 class='x'>x</h2></body></html>"))

    def test_detail_does_not_read_description_or_nutrition(self):
        d = P.parse_sukiya_detail(fx("sukiya/detail_gyudon-112700.html"))
        self.assertEqual(set(d), {"name", "prices"})

    def test_news_list(self):
        news = P.parse_sukiya_news(fx("sukiya/news.html"))
        self.assertEqual(len(news), 667)  # 「新着」と「全件」の重複は除く
        self.assertEqual(news[0]["date"], "2026-10-08")
        self.assertEqual(news[0]["category"], "プレスリリース")
        self.assertEqual(news[1]["category"], "お知らせ")
        self.assertEqual(sorted(a["date"] for a in news), [a["date"] for a in reversed(news)])
        pr = next(a for a in news if "rostepork" in a["url"])
        self.assertEqual(pr["url"], "https://www.sukiya.jp/news/Press_sukiya_20261007_rostepork.pdf")
        self.assertEqual(pr["title"], "10月14日（水）朝9:00より『ローストポーク丼』販売開始！")
        self.assertTrue(P.sukiya_is_menu_press(pr))
        self.assertFalse(P.sukiya_is_menu_press(news[1]))


class TestNewsHeadings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.news = P.parse_sukiya_news(fx("sukiya/news.html"))

    def test_start_date_extraction(self):
        f = P.sukiya_start_date
        self.assertEqual(f("10月14日（水）朝9:00より『ローストポーク丼』販売開始！", "2026-10-07"), "2026-10-14")
        self.assertEqual(f("9月29日（火）朝9:00 すき家に『とん汁みそらーめん』が復活！", "2026-09-24"), "2026-09-29")
        self.assertEqual(f("1月5日（月）朝9:00より『新商品』販売開始！", "2026-12-28"), "2027-01-05")  # 年またぎ
        self.assertIsNone(f("『新商品』販売開始！", "2026-10-07"))  # 日付なし
        self.assertIsNone(f("新TVCM 10月2日(金）より、順次放送開始", "2026-10-02"))  # 販売・登場の語が無い

    def test_quoted_name_must_match_exactly(self):
        m = lambda name, ref="2026-10-11": P.match_sukiya_launch(name, self.news, ref)
        self.assertIsNotNone(m("ビーフカレー"))
        self.assertIsNone(m("ソーセージビーフカレー"))  # 「ビーフカレー」の見出しに部分一致させない
        self.assertIsNone(m("牛あいがけビーフカレー"))
        self.assertIsNone(m("すきやき牛丼"))  # 「月見すきやき牛丼」の見出しに部分一致させない
        self.assertIsNone(m("辛旨すきやき牛丼"))
        self.assertIsNotNone(m("月見すきやき牛丼"))
        self.assertIsNone(m("おんたまシビ辛麻婆茄子牛丼"))
        self.assertIsNotNone(m("シビ辛麻婆茄子牛丼"))
        self.assertIsNone(m("牛丼"))

    def test_real_headings(self):
        hit = P.match_sukiya_launch("Sukiシェイク焼き芋", self.news, DAY1)
        self.assertEqual(hit["launch_date"], "2026-09-08")
        self.assertEqual(hit["news"]["date"], "2026-09-03")
        self.assertEqual(hit["news"]["url"], "https://www.sukiya.jp/news/Press_sukiya_20260903_shakeyakiimo.pdf")
        # 見出しの全角スペース・記号の違いは吸収する
        hit = P.match_sukiya_launch("Sukiシェイク アサイー・バナナ", self.news, "2026-10-11")
        self.assertEqual(hit["launch_date"], "2026-10-14")
        self.assertEqual(P.match_sukiya_launch("とん汁みそらーめん", self.news, DAY1)["launch_date"], "2026-09-29")

    def test_only_menu_press_releases_are_matched(self):
        news = [{"date": "2026-10-10", "title": "10月14日（水）朝9:00より『新作ごはん丼』販売開始！",
                 "url": "https://www.sukiya.jp/news/2026/10/x.html", "category": "お知らせ", "kind": "info"}]
        self.assertIsNone(P.match_sukiya_launch("新作ごはん丼", news, DAY1))
        news[0]["url"] = "https://www.sukiya.jp/news/Press_sukiya_20261010_x.pdf"
        self.assertIsNotNone(P.match_sukiya_launch("新作ごはん丼", news, DAY1))
        news[0]["title"] = "『新作ごはん丼』販売終了のお知らせ"
        self.assertIsNone(P.match_sukiya_launch("新作ごはん丼", news, DAY1))
        news[0]["title"] = "『新作ごはん丼』の価格改定について。販売価格を変更"
        self.assertIsNone(P.match_sukiya_launch("新作ごはん丼", news, DAY1))

    def test_window_is_90_days_before_and_14_after(self):
        # 月見すきやき牛丼の見出しは2026-09-01
        self.assertIsNotNone(P.match_sukiya_launch("月見すきやき牛丼", self.news, "2026-11-29"))  # 89日後
        self.assertIsNone(P.match_sukiya_launch("月見すきやき牛丼", self.news, "2026-12-05"))  # 95日後
        self.assertIsNotNone(P.match_sukiya_launch("月見すきやき牛丼", self.news, "2026-08-20"))  # 12日前(発売前の記録)
        self.assertIsNone(P.match_sukiya_launch("月見すきやき牛丼", self.news, "2026-08-10"))

    def test_headline_without_date_gives_none_launch_date(self):
        news = [{"date": "2026-10-10", "title": "『新作ごはん丼』販売開始！",
                 "url": "https://www.sukiya.jp/news/Press_sukiya_20261010_x.pdf", "category": "プレスリリース", "kind": ""}]
        hit = P.match_sukiya_launch("新作ごはん丼", news, DAY1)
        self.assertIsNone(hit["launch_date"])
        self.assertEqual(hit["news"]["date"], "2026-10-10")


@helpers.needs_fixtures
class TestCrawl(unittest.TestCase):
    def test_first_run_records_icons_and_news_and_makes_baseline(self):
        with TempData():
            g1 = getter()
            _, st = run_s(g1, DAY1)
            self.assertTrue(st["list_ok"])
            self.assertFalse(st["blocked"])
            self.assertEqual(st["list_requests"], 15)  # robots + 入口 + 13カテゴリ
            self.assertEqual(st["limited_seen"], 33)
            self.assertEqual(st["new_products"], 33)  # 「期間限定」アイコンの商品。定番の既存メニューは記録しない
            self.assertEqual(st["news_matched"], 7)
            self.assertEqual(st["news_unmatched"], 26)
            self.assertEqual(len(crawl.load_chain_products("sukiya")), 33)
            self.assertEqual(crawl.load_chain_products("yoshinoya"), [])
            base = crawl.baseline_for("sukiya")
            self.assertEqual(len(base), 174)
            self.assertIn("special-400100", base)
            self.assertEqual(sum(1 for u in g1.calls if u.endswith("/news/")), 1)
            self.assertTrue(all(u.startswith(crawl.SUKIYA_BASE) for u in g1.calls))
            self.assertFalse(any(u.endswith(".pdf") for u in g1.calls))  # PDFは取得しない
            self.assertFalse(any("news.sukiya.jp" in u for u in g1.calls))
            self.assertLessEqual(len(g1.calls), crawl.MAX_REQUESTS["sukiya"])

    def test_product_fields_with_announcement(self):
        with TempData():
            run_s(getter(), DAY1)
            p = crawl.load_product("sukiya", "gyudon-112700")
            self.assertEqual(p["chain"], "sukiya")
            self.assertEqual(p["name"], "月見すきやき牛丼")
            self.assertEqual(p["status"], "on_sale")
            self.assertEqual(p["limited_kind"], "期間限定")
            self.assertEqual(p["badges"], ["NEW", "期間限定"])
            self.assertEqual(p["category"], "牛丼")
            self.assertEqual(p["price"], 780)  # 並盛
            self.assertEqual(len(p["price_variants"]), 6)
            self.assertEqual(p["price_variants"][0], {"label": "ミニ", "price": 720})
            self.assertIn("表示は「並盛」の価格", p["price_note"])
            self.assertEqual(p["launch_date"], "2026-09-08")
            self.assertEqual(p["launch_date_source"], "news_headline")
            self.assertEqual(p["announced_at"], "2026-09-01")
            self.assertEqual(p["news_url"], "https://www.sukiya.jp/news/Press_sukiya_20260901_thukimi.pdf")
            self.assertEqual(p["news_title"], "9月8日（火）朝9:00より『月見すきやき牛丼』販売開始！")
            # 事実情報だけ: 説明文・画像・栄養は持たない。栄養・アレルゲンはPDFへのリンク
            self.assertIsNone(p["description"])
            self.assertIsNone(p["image"])
            self.assertIsNone(p["nutrition"])
            self.assertIsNone(p["allergens"])
            self.assertEqual(p["nutrition_pdf_url"], "https://images.zensho.co.jp/materials/sukiya/allergen/nutrition.pdf")
            self.assertEqual(p["allergen_pdf_url"], "https://images.zensho.co.jp/materials/sukiya/allergen/allergen.pdf")
            self.assertEqual(p["official_url"], "https://www.sukiya.jp/menu/in/gyudon/112700/index.html")
            self.assertEqual(p["price_history"], [{"date": DAY1, "price": 780}])
            self.assertEqual(p["price_checked_at"], DAY1)

    def test_product_without_announcement_has_no_launch_date(self):
        with TempData():
            run_s(getter(), DAY1)
            p = crawl.load_product("sukiya", "gyudon-110100")  # すきやき牛丼(期間限定アイコン、見出しは月見のみ)
            self.assertEqual(p["limited_kind"], "期間限定")
            self.assertIsNone(p["launch_date"])
            self.assertEqual(p["launch_date_source"], "not_found")
            self.assertIsNone(p["announced_at"])
            self.assertIsNone(p["news_url"])

    def test_multiple_prices_from_real_detail(self):
        ov = {"/menu/in/drink/810500/index.html": fx("sukiya/detail_special-400100.html")}
        with TempData():
            run_s(getter(overrides=ov), DAY1)
            p = crawl.load_product("sukiya", "drink-810500")
            self.assertEqual(p["price"], 700)
            self.assertEqual([v["price"] for v in p["price_variants"]], [700, 900, 1070])
            self.assertIn("サイズや組み合わせにより価格が異なります", p["price_note"])

    def test_robots_disallow_is_respected(self):
        ov = {"/robots.txt": "User-agent: *\nDisallow: /menu/in/curry/\n"}
        with TempData():
            g1 = getter(overrides=ov)
            _, st = run_s(g1, DAY1)
            self.assertFalse(st["list_ok"])
            self.assertFalse(any("/menu/in/curry/" in u for u in g1.calls))
            self.assertIsNone(crawl.baseline_for("sukiya"))  # 一覧が揃わない回はbaselineを作らない

    def test_second_run_only_refetches_lists_and_news(self):
        with TempData():
            run_s(getter(), DAY1)
            g2 = getter()
            _, st = run_s(g2, "2026-10-12")
            self.assertEqual(st["new_products"], 0)
            self.assertEqual(st["detail_requests"], 1)  # お知らせ一覧だけ
            self.assertEqual(len(g2.calls), 16)
            self.assertEqual(crawl.load_product("sukiya", "gyudon-112700")["last_seen_at"], "2026-10-12")

    def test_new_item_after_baseline_is_recorded_as_new_listing(self):
        with TempData():
            run_s(getter(), DAY1)
            ov = {list_path("side"): clone_item(fx("sukiya/list_side.html"), "7004000", "7099900", "新おかず")}
            _, st = run_s(getter(overrides=ov), "2026-10-12")
            self.assertEqual(st["new_products"], 1)
            p = crawl.load_product("sukiya", "side-7099900")
            self.assertEqual(p["limited_kind"], "新規掲載")  # 公式の表示もお知らせも無ければ「期間限定」と書かない
            self.assertEqual(p["badges"], [])
            self.assertIsNone(p["launch_date"])

    def test_new_item_named_in_press_release_gets_dates(self):
        with TempData():
            run_s(getter(), DAY1)
            num = P.parse_sukiya_list(fx("sukiya/list_don.html"))[0]["detail_path"].split("/")[-2]
            ov = {list_path("don"): clone_item(fx("sukiya/list_don.html"), num, "6099900", "ローストポーク丼")}
            _, st = run_s(getter(overrides=ov), "2026-10-15")
            self.assertEqual(st["new_products"], 1)
            p = crawl.load_product("sukiya", "don-6099900")
            self.assertEqual(p["limited_kind"], "新メニュー")
            self.assertEqual(p["launch_date"], "2026-10-14")
            self.assertEqual(p["announced_at"], "2026-10-07")
            self.assertTrue(p["news_url"].endswith("Press_sukiya_20261007_rostepork.pdf"))

    def test_regular_menu_named_in_press_release_is_recorded_without_icon(self):
        # 定番の見た目(アイコンなし)で並んでいても、直近90日のプレスリリースに名前が出れば記録する
        base = fx("sukiya/list_don.html")
        first = P.parse_sukiya_list(base)[0]
        num = first["detail_path"].split("/")[-2]
        renamed = re.sub(r'(<a href="/menu/in/don/%s/index.html">.*?<div class="pro_name">)[^<]*(</div>)' % num,
                         r"\g<1>ローストポーク丼\g<2>", base, count=1, flags=re.S)
        self.assertIn("ローストポーク丼", renamed)
        with TempData():
            _, st = run_s(getter(overrides={list_path("don"): renamed}), "2026-10-15")
            p = crawl.load_product("sukiya", first["product_id"])
            self.assertIsNotNone(p)
            self.assertEqual(p["launch_date"], "2026-10-14")

    def test_existing_regular_menu_not_recorded(self):
        with TempData():
            run_s(getter(), DAY1)
            self.assertIsNone(crawl.load_product("sukiya", "special-400100"))

    def test_end_needs_two_misses_on_different_days(self):
        pid = "gyudon-112700"
        ov = drop_everywhere("112700")
        with TempData():
            run_s(getter(), DAY1)
            run_s(getter(overrides=ov), "2026-10-12")
            self.assertEqual(crawl.load_product("sukiya", pid)["status"], "on_sale")
            run_s(getter(overrides=ov), "2026-10-12")  # 同じ日の再実行は1回と数える
            self.assertEqual(crawl.load_product("sukiya", pid)["status"], "on_sale")
            _, st = run_s(getter(overrides=ov), "2026-10-13")
            p = crawl.load_product("sukiya", pid)
            self.assertEqual(p["status"], "ended")
            self.assertEqual(p["ended_observed_at"], "2026-10-13")
            self.assertEqual(st["ended"], 1)

    def test_no_end_when_a_category_failed(self):
        ov = drop_everywhere("112700")
        with TempData():
            run_s(getter(), DAY1)
            run_s(getter(overrides=ov, fail={list_path("kids")}), "2026-10-12")
            _, st = run_s(getter(overrides=ov, fail={list_path("kids")}), "2026-10-13")
            self.assertFalse(st["list_ok"])
            self.assertEqual(crawl.load_product("sukiya", "gyudon-112700")["status"], "on_sale")

    def test_no_end_when_a_category_is_empty(self):
        ov = drop_everywhere("112700")
        ov[list_path("kids")] = "<html></html>"  # 構造変更の疑い(0件)
        with TempData():
            run_s(getter(), DAY1)
            run_s(getter(overrides=ov), "2026-10-12")
            _, st = run_s(getter(overrides=ov), "2026-10-13")
            self.assertFalse(st["list_ok"])
            self.assertEqual(crawl.load_product("sukiya", "gyudon-112700")["status"], "on_sale")

    def test_block_stops_chain_and_no_ending(self):
        class Blocking(FakeGetter):
            def __call__(self, chain, url):
                if url.endswith("/menu/in/side/"):
                    raise BlockedError("HTTP 403")
                return super().__call__(chain, url)
        ov = drop_everywhere("112700")
        with TempData():
            run_s(getter(), DAY1)
            for d in ("2026-10-12", "2026-10-13"):
                _, st = run_s(Blocking(fallback_detail=True, overrides=ov), d)
                self.assertTrue(st["blocked"])
            self.assertEqual(crawl.load_product("sukiya", "gyudon-112700")["status"], "on_sale")

    def test_relaunch_same_id_is_recorded_as_new_season(self):
        pid = "gyudon-112700"
        gone = drop_everywhere("112700")
        with TempData():
            run_s(getter(), DAY1)
            run_s(getter(overrides=gone), "2026-10-12")
            run_s(getter(overrides=gone), "2026-10-13")
            self.assertEqual(crawl.load_product("sukiya", pid)["status"], "ended")
            _, st = run_s(getter(), "2027-09-01")
            self.assertEqual(st["relaunched"], 1)
            p = crawl.load_product("sukiya", pid)
            self.assertEqual(p["status"], "on_sale")
            self.assertEqual(p["relaunches"], [{"ended_observed_at": "2026-10-13", "reappeared_at": "2027-09-01"}])
            self.assertEqual(p["seasons"][0]["launch_date"], "2026-09-08")
            self.assertEqual(p["seasons"][0]["ended_observed_at"], "2026-10-13")
            # 今季の発売日は未確定に戻り、昨季のお知らせ(窓の外)には結びつけない
            self.assertIsNone(p["launch_date"])
            self.assertIsNone(p["news_url"])
            self.assertEqual(p["price_history"][-1], {"date": "2027-09-01", "price": 780, "relaunch": True})

    def test_headline_found_later_fills_null_launch_date(self):
        with TempData():
            run_s(getter(), DAY1)
            self.assertIsNone(crawl.load_product("sukiya", "unagi-900100")["launch_date"])
            ov = {"/news/": news_with("10月20日（火）朝9:00より『うな丼』販売開始！", "2026年10月12日")}
            run_s(getter(overrides=ov), "2026-10-12")
            p = crawl.load_product("sukiya", "unagi-900100")
            self.assertEqual(p["launch_date"], "2026-10-20")
            self.assertEqual(p["announced_at"], "2026-10-12")
            self.assertEqual(p["launch_date_source"], "news_headline")

    def test_headline_without_date_records_announcement_only(self):
        with TempData():
            run_s(getter(), DAY1)
            ov = {"/news/": news_with("『うな丼』販売開始！", "2026年10月12日")}
            run_s(getter(overrides=ov), "2026-10-12")
            p = crawl.load_product("sukiya", "unagi-900100")
            self.assertIsNone(p["launch_date"])
            self.assertEqual(p["launch_date_source"], "news_without_date")
            self.assertEqual(p["announced_at"], "2026-10-12")

    def test_news_failure_does_not_stop_recording(self):
        with TempData():
            _, st = run_s(getter(fail={"/news/"}), DAY1)
            self.assertEqual(st["new_products"], 33)  # 「期間限定」アイコンだけで記録できる
            self.assertEqual(st["news_matched"], 0)
            self.assertIsNone(crawl.load_product("sukiya", "gyudon-112700")["launch_date"])

    def test_price_recheck_records_price_change(self):
        changed = fx("sukiya/detail_drink-810500.html").replace("<span>210</span>", "<span>230</span>")
        with TempData():
            run_s(getter(), DAY1)
            # 7日たっていなければ取り直さない
            g_early = getter()
            run_s(g_early, "2026-10-17")
            self.assertEqual(sum(1 for u in g_early.calls if "/index.html" in u), 0)
            g_late = getter(overrides={"/menu/in/drink/810500/index.html": changed})
            _, st = run_s(g_late, "2026-10-19")
            rechecked = [u for u in g_late.calls if "/index.html" in u]
            self.assertEqual(len(rechecked), crawl.SUKIYA_PRICE_RECHECK_MAX)  # 1回の上限
            self.assertEqual(st["new_products"], 0)
            done = [p for p in crawl.load_chain_products("sukiya") if p["price_checked_at"] == "2026-10-19"]
            self.assertEqual(len(done), crawl.SUKIYA_PRICE_RECHECK_MAX)
            # 残りは翌日以降に回る(古い順)
            g_next = getter(overrides={"/menu/in/drink/810500/index.html": changed})
            run_s(g_next, "2026-10-20")
            run_s(getter(overrides={"/menu/in/drink/810500/index.html": changed}), "2026-10-21")
            run_s(getter(overrides={"/menu/in/drink/810500/index.html": changed}), "2026-10-22")
            p = crawl.load_product("sukiya", "drink-810500")
            self.assertEqual(p["price_checked_at"] >= "2026-10-19", True)
            self.assertEqual(p["price"], 230)
            self.assertEqual(p["price_history"], [{"date": DAY1, "price": 210}, {"date": p["price_history"][1]["date"], "price": 230}])
            # 変わっていない商品の履歴は増えない
            q = crawl.load_product("sukiya", "gyudon-112700")
            self.assertEqual(len(q["price_history"]), 1)

    def test_detail_failure_goes_to_pending(self):
        with TempData():
            pend, st = run_s(getter(fail={"/menu/in/gyudon/112700/index.html"}), DAY1)
            self.assertEqual(st["failed"], 1)
            self.assertIn("gyudon-112700", pend["sukiya"])
            self.assertIsNone(crawl.load_product("sukiya", "gyudon-112700"))
            self.assertEqual(st["new_products"], 32)

    def test_chain_selectable_from_cli_and_configured(self):
        self.assertIn("sukiya", crawl.CRAWLERS)
        self.assertGreaterEqual(crawl.INTERVALS["sukiya"], 3)
        self.assertLessEqual(crawl.MAX_REQUESTS["sukiya"], 80)
        worst = sum(crawl.MAX_REQUESTS[c] * max(crawl.INTERVALS[c], 3) for c in crawl.CRAWLERS)
        self.assertLess(worst, 60 * 60)  # update.yml の timeout 60分に収まる


def sukiya_item(**kw):
    base = {
        "chain": "sukiya", "product_id": "gyudon-112700", "name": "月見すきやき牛丼", "category": "牛丼",
        "price": 780, "price_note": "税込・店内の価格。",
        "price_variants": [{"label": "ミニ", "price": 720}, {"label": "並盛", "price": 780}],
        "launch_date": "2026-09-08", "launch_date_source": "news_headline", "announced_at": "2026-09-01",
        "planned_end_text": None,
        "news_url": "https://www.sukiya.jp/news/Press_sukiya_20260901_thukimi.pdf",
        "news_title": "9月8日（火）朝9:00より『月見すきやき牛丼』販売開始！", "news_date": "2026-09-01",
        "description": None, "nutrition": None, "nutrition_variants": None,
        "nutrition_pdf_url": "https://images.zensho.co.jp/materials/sukiya/allergen/nutrition.pdf",
        "nutrition_pdf_label": "栄養成分一覧",
        "allergen_pdf_url": "https://images.zensho.co.jp/materials/sukiya/allergen/allergen.pdf",
        "allergen_pdf_label": "アレルゲン一覧",
        "allergens": None, "origin": None, "limited_kind": "期間限定", "badges": ["NEW", "期間限定"],
        "official_url": "https://www.sukiya.jp/menu/in/gyudon/112700/index.html",
        "first_seen_at": "2026-10-11T07:00:00+09:00", "last_seen_at": "2026-10-11",
        "status": "on_sale", "ended_observed_at": None,
        "price_history": [{"date": "2026-10-11", "price": 780}], "image": None,
    }
    base.update(kw)
    return base


class TestSite(unittest.TestCase):
    def test_title_and_description(self):
        it = sukiya_item()
        self.assertEqual(g.seo_title(it), "すき家「月見すきやき牛丼」の価格・販売期間")
        d = g.seo_description(it)
        self.assertIn("すき家の限定メニュー", d)
        self.assertIn("780円（税込）", d)
        self.assertNotIn("kcal", d)
        d2 = g.seo_description(sukiya_item(limited_kind="新規掲載"))
        self.assertIn("すき家のメニュー", d2)
        self.assertNotIn("限定メニュー", d2)

    def test_item_page_links_to_both_pdfs(self):
        h = g.render_item_page(sukiya_item())
        self.assertIn("<title>すき家「月見すきやき牛丼」の価格・販売期間｜ファストフードポチ</title>", h)
        self.assertIn("nutrition.pdf", h)
        self.assertIn("allergen.pdf", h)
        self.assertIn("栄養成分・アレルギー情報は公式PDF（栄養成分一覧／アレルゲン一覧）をご覧ください。当サイトでは取り込んでいません", h)
        self.assertNotIn("栄養成分は公式サイトに掲載がありませんでした", h)
        self.assertIn("var(--sukiya)", h)
        self.assertIn("発売日は公式のお知らせ（2026-09-01）の見出しによります", h)
        self.assertIn("<th>ミニ</th><td>720円（税込）</td>", h)
        self.assertNotIn("<blockquote", h)  # 説明文は載せない
        self.assertIn("2026-09-08〜 販売中", h)

    def test_item_page_announcement_without_launch_date(self):
        h = g.render_item_page(sukiya_item(launch_date=None, launch_date_source="news_without_date"))
        self.assertIn("お知らせ日：2026-09-01（発売日は公式のお知らせPDFに記載）", h)
        self.assertIn("2026-10-11（記録開始日）〜 販売中", h)

    def test_item_page_without_announcement_and_seasons(self):
        h = g.render_item_page(sukiya_item(
            launch_date=None, news_url=None, news_title=None, news_date=None, announced_at=None,
            seasons=[{"launch_date": "2025-10-01", "ended_observed_at": "2025-11-20"}],
            relaunches=[{"ended_observed_at": "2025-11-20", "reappeared_at": "2026-10-11"}]))
        self.assertNotIn("お知らせ日", h)
        self.assertIn("昨季の販売：2025-10-01〜2025-11-20", h)
        self.assertIn("再び登場しました", h)
        self.assertIn("販売期間は公式サイトに載っていない", h)

    def test_yoshinoya_pdf_wording_unchanged(self):
        h = g.render_nutrition({"chain": "yoshinoya", "nutrition": None,
                                "nutrition_pdf_url": "https://www.yoshinoya.com/pdf/allergy/",
                                "nutrition_pdf_label": "吉野家メニュー情報"})
        self.assertIn("公式の「吉野家メニュー情報」PDFをご覧ください", h)

    def test_chain_in_config_and_text(self):
        self.assertEqual(g.CHAIN_NAME["sukiya"], "すき家")
        self.assertIn("すき家", g.CONFIG["site_description"])
        self.assertIn("すき家", g.CONFIG["site_tagline"])
        # aboutは短く保つ方針(2026-10-11)。チェーン別の細かい説明は書かず、全チェーン名が入っていることだけ確かめる
        self.assertTrue(any("すき家" in p for p in g.CONFIG["about"]))
        self.assertLess(sum(len(p) for p in g.CONFIG["about"]), 700)
        self.assertIn("--sukiya:", g.CSS)
        self.assertIn('data-chain="sukiya"', g.CSS)
        colors = {c["slug"]: c["color"].lower() for c in g.CHAINS}
        self.assertEqual(len(set(colors.values())), len(colors))
        self.assertEqual(g.CHAIN_LIST_TEXT, "マクドナルド・モスバーガー・松屋・松のや・吉野家・すき家・かつや・からやま・丸亀製麺")

    def test_chain_list_footer_and_sections(self):
        self.assertIn("/chains/sukiya/", g.render_chains_top())
        h = g.page_shell("x", "d", "<p>b</p>", "https://fastfood.netaful.jp/").replace(
            ' data-google-vignette="false"', "")  # 広告設定済みだと全<a>に付く
        self.assertIn('<a href="https://www.sukiya.jp/">すき家</a>', h)

    def test_card_and_grouping(self):
        it = sukiya_item()
        self.assertIn('data-chain="sukiya"', g.render_card(it))
        self.assertIn("すき家", g.render_card(it))
        idx = g.build_search_index([it])
        self.assertIn("すき家", str(idx))


class TestNetaful(unittest.TestCase):
    def test_tag_and_filter(self):
        self.assertEqual(nr.TAGS["sukiya"], 869)
        self.assertIn("sukiya", nr.TITLE_ALWAYS)
        self.assertTrue(nr.title_passes_filter("sukiya", "【すき家】「ローストポーク丼」を食べてみた"))
        self.assertFalse(nr.title_passes_filter("sukiya", "【吉野家】牛丼の新商品"))

    def test_other_chains_unchanged(self):
        self.assertEqual(nr.TAGS["yoshinoya"], 765)
        self.assertTrue(nr.title_passes_filter("mcdonalds", "なんでも"))

    def test_related_keywords(self):
        k = lambda n: (g.match_keyword_for_name(n) or {}).get("label")
        self.assertEqual(k("ローストポーク丼"), "ローストポーク")
        self.assertEqual(k("すきやき牛丼"), "すき焼き")
        self.assertEqual(k("とん汁みそらーめん"), "ラーメン")
        self.assertEqual(k("ビーフカレー"), "カレー")
        self.assertEqual(k("Sukiシェイク焼き芋"), "シェイク")
        # 既存の判定は変えない
        self.assertEqual(k("月見バーガー"), "月見")
        self.assertEqual(k("牛丼"), "牛丼")


if __name__ == "__main__":
    unittest.main()
