"""かつや・からやま(アークランド型): メニュー一覧・お知らせ見出しのパーサ、発売日の抽出(年またぎ)と完全一致の照合、
baseline、販売状況の追跡(一覧が空・極端に少ない回は何も更新しない)、再登場、同名商品、価格の変化、
title・商品ページ、ネタフル記事の振り分けのテスト。
フィクスチャは tests/synthetic_arcland.py の合成データ(公式ページのコピーはリポジトリに入れない)。
手元に実物のスナップショット(tests/fixtures/arcland/)があるときだけ、実物でも確かめる補助テストを走らせる。"""

from __future__ import annotations

import json
import re
import unittest

import helpers  # noqa: F401
from helpers import TempData, fx
import crawl
import fetch_netaful_reviews as nr
import generate_site as g
import parsers as P
import synthetic_arcland as S

BASE = "https://www.arclandservice.co.jp"
D1, D2, D3, D4 = "2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14"


class FakeArc:
    """パスからページを返す偽の取得器。呼ばれたURLを記録する。"""

    def __init__(self, pages: dict, fail: set[str] = frozenset()):
        self.pages = dict(pages)
        self.fail = set(fail)
        self.calls: list[str] = []

    def __call__(self, chain: str, url: str) -> str:
        self.calls.append(url)
        assert url.startswith(BASE), url  # ホストはここだけ
        path = url[len(BASE):]
        if path in self.fail:
            raise OSError("simulated failure")
        if path == "/robots.txt":
            return S.ROBOTS
        return self.pages[path]


def kat(menu=None, news=None, **kw):
    return FakeArc({"/katsuya/menu/": menu if menu is not None else S.katsuya_menu(),
                    "/katsuya/information/": news if news is not None else S.katsuya_news()}, **kw)


def kara(menu=None, news=None, **kw):
    return FakeArc({"/karayama/menu/": menu if menu is not None else S.karayama_menu(),
                    "/karayama/information/": news if news is not None else S.karayama_news()}, **kw)


def run(chain, getter, day, pending=None):
    pending = {} if pending is None else pending
    stats = {}
    crawl.CRAWLERS[chain](getter, pending, stats, day)
    return pending, stats[chain]


def prod(chain, pid):
    return crawl.load_product(chain, pid)


class TestMenuParser(unittest.TestCase):
    def setUp(self):
        self.m = P.parse_arcland_menu(S.katsuya_menu(), "katsuya")
        self.by = {e["product_id"]: e for e in self.m["items"]}

    def test_items_prices_ids(self):
        self.assertEqual(len(self.m["items"]), 10)
        e = self.by["90001"]
        self.assertEqual((e["name"], e["price"], e["price_ex_tax"]), ("合成ロース丼", 979, 890))
        self.assertEqual(self.by["90002"]["price"], 1089)  # 「1,089」のカンマ
        self.assertEqual(e["official_url"], f"{BASE}/katsuya/menu/90001/")  # ?identifier= を除く

    def test_badges_and_category(self):
        self.assertTrue(self.by["90001"]["is_limited"] and not self.by["90001"]["is_new"])
        self.assertTrue(self.by["2607001"]["is_new"] and not self.by["2607001"]["is_limited"])
        self.assertFalse(self.by["2510001"]["is_limited"] or self.by["2510001"]["is_new"])  # 「おすすめ」は印にしない
        self.assertEqual(self.by["2510001"]["badges"], ["おすすめ"])
        self.assertIsNone(self.by["90001"]["category"])  # 「期間限定」セクションはカテゴリにしない
        self.assertEqual(self.by["2510002"]["category"], "丼")
        self.assertEqual(self.by["90003"]["category"], "テイクアウト")
        self.assertEqual(self.by["90003"]["name"], "合成ロース丼弁当")

    def test_allergen_pdf_is_the_allergen_link_not_the_menu_pdf(self):
        self.assertEqual(self.m["allergen_pdf_url"], f"{BASE}/katsuya/wp-content/uploads/sites/2/2026/09/20260904_allergy.pdf")
        no = P.parse_arcland_menu(S.katsuya_menu(allergy=None), "katsuya")
        self.assertIsNone(no["allergen_pdf_url"])

    def test_same_item_in_two_sections_is_merged(self):
        a = S.menu_item("katsuya", "5", "合成X", 100, "110", ["NEW"])
        b = S.menu_item("katsuya", "5", "合成X", 100, "110", ["期間限定"])
        m = P.parse_arcland_menu(S.menu_page("katsuya", [("丼", [a]), ("期間限定", [b])]), "katsuya")
        self.assertEqual(len(m["items"]), 1)
        self.assertTrue(m["items"][0]["is_new"] and m["items"][0]["is_limited"])
        self.assertEqual(m["items"][0]["category"], "丼")

    def test_structure_change_gives_no_items(self):
        self.assertEqual(P.parse_arcland_menu("<html><body><p>メンテナンス中</p></body></html>")["items"], [])
        self.assertEqual(P.parse_arcland_menu(S.katsuya_menu().replace("menu-list__item", "menu-card"))["items"], [])


class TestNewsParser(unittest.TestCase):
    def test_sorted_newest_first_with_absolute_urls_and_linkless(self):
        n = P.parse_arcland_news(S.katsuya_news(), "katsuya")
        self.assertEqual([a["date"] for a in n], sorted((a["date"] for a in n), reverse=True))
        self.assertEqual(n[0]["date"], "2026-10-06")
        self.assertTrue(n[0]["url"].startswith("https://prtimes.jp/"))
        last = n[-1]
        self.assertEqual((last["date"], last["url"]), ("2023-05-26", ""))  # リンクの無い古いお知らせ
        self.assertEqual(len(n), 4)

    def test_relative_link_resolves_on_the_page_own_host(self):
        page = S.news_page([S.news_item("katsuya", "2026-10-01", "x", url="/katsuya/foo/")])
        self.assertEqual(P.parse_arcland_news(page, "katsuya")[0]["url"], f"{BASE}/katsuya/foo/")

    def test_zero_when_structure_changes(self):
        self.assertEqual(P.parse_arcland_news("<html></html>"), [])


class TestStartDate(unittest.TestCase):
    sd = staticmethod(P.arcland_start_date)

    def test_normal(self):
        self.assertEqual(self.sd("【期間限定】10月9日(金)～期間限定で「X」販売開始！", "2026-10-06"), "2026-10-09")
        self.assertEqual(self.sd("【期間限定】7月3日（金）～期間限定で「X」販売開始！", "2026-06-30"), "2026-07-03")

    def test_date_after_the_quoted_names(self):
        self.assertEqual(self.sd("【新商品】「A」「B」など新メニューを7月27日(月)販売開始！", "2026-07-23"), "2026-07-27")

    def test_year_rollover(self):
        self.assertEqual(self.sd("【期間限定】1月8日(金)～期間限定で「X」販売開始！", "2026-12-28"), "2027-01-08")
        self.assertEqual(self.sd("【期間限定】12月26日(金)～期間限定で「X」販売開始！", "2027-01-05"), "2026-12-26")
        self.assertEqual(self.sd("【期間限定】3月6日(金)～期間限定で「X」販売開始！", "2026-03-02"), "2026-03-06")

    def test_slash_form_and_no_launch_word_and_no_date(self):
        self.assertEqual(self.sd("【期間限定】9/25(金)～「X」販売開始", "2026-09-24"), "2026-09-25")
        self.assertIsNone(self.sd("【10/2(金)オープン】かつや 店", "2026-09-25"))
        self.assertIsNone(self.sd("【期間限定】近日「X」販売開始", "2026-09-25"))
        self.assertIsNone(self.sd("【お得情報】9/18(金)～9/23(水)は「X」が登場", "2026-09-15"))
        self.assertIsNone(self.sd("1月40日より販売", "2026-12-01"))


class TestTitleMatch(unittest.TestCase):
    m = staticmethod(P.arcland_title_matches)

    def test_exact_inside_quotes_only(self):
        self.assertTrue(self.m("海老トリプル丼", "【期間限定】10月9日(金)～期間限定で「海老トリプル丼」販売開始！"))
        self.assertFalse(self.m("海老トリプル", "【期間限定】で「海老トリプル丼」販売開始！"))  # 部分一致は誤爆
        self.assertFalse(self.m("さんま定食", "【期間限定】で「牛さんま定食」販売開始！"))
        self.assertFalse(self.m("海老トリプル丼弁当", "で「海老トリプル丼」販売開始！"))

    def test_multiple_quotes_and_nakaguro_lists(self):
        t = "【新商品】「カツ丼ライト」「豚丼」など新メニューを7月27日(月)販売開始！"
        self.assertTrue(self.m("豚丼", t))
        self.assertTrue(self.m("カツ丼ライト", t))
        t2 = "【期間限定】7月17日(金)～ 期間限定で「うな玉丼・うな玉とロースカツの合い盛り丼」販売開始！"
        self.assertTrue(self.m("うな玉丼", t2))
        self.assertTrue(self.m("うな玉とロースカツの合い盛り丼", t2))
        self.assertTrue(self.m("うな玉丼・うな玉とロースカツの合い盛り丼", t2))

    def test_without_quotes_needs_four_chars(self):
        self.assertTrue(self.m("合成ロース丼", "合成ロース丼を販売"))
        self.assertFalse(self.m("丼", "丼を販売"))


class TestMatchLaunch(unittest.TestCase):
    def setUp(self):
        self.news = P.parse_arcland_news(S.katsuya_news(), "katsuya")

    def test_hit_and_window(self):
        h = P.match_arcland_launch("合成ロース丼", self.news, D1)
        self.assertEqual((h["launch_date"], h["news"]["date"]), ("2026-10-09", "2026-10-06"))
        self.assertIsNone(P.match_arcland_launch("合成ロース丼", self.news, "2027-03-01"))  # 窓の外
        self.assertIsNone(P.match_arcland_launch("合成ロース丼弁当", self.news, D1))

    def test_record_window_is_shorter(self):
        # 7/23 のお知らせは10/11の90日前(7/13)より後 → 入る。8/1ならさらに範囲外になる条件で確認
        self.assertIsNotNone(P.match_arcland_launch("合成豚丼", self.news, D1, before=P.ARCLAND_NEWS_RECORD_DAYS))
        self.assertIsNone(P.match_arcland_launch("合成豚丼", self.news, "2026-11-01", before=P.ARCLAND_NEWS_RECORD_DAYS))
        self.assertIsNotNone(P.match_arcland_launch("合成豚丼", self.news, "2026-11-01"))  # 照合の窓(120日)なら入る

    def test_non_launch_headlines_are_ignored(self):
        news = [{"date": "2026-10-01", "title": "【お得情報】「合成豚丼」販売終了のお知らせ", "url": "u", "category": ""}]
        self.assertIsNone(P.match_arcland_launch("合成豚丼", news, D1))


class TestCrawlKatsuya(unittest.TestCase):
    def test_first_run_records_marks_only_and_makes_three_requests(self):
        with TempData():
            g1 = kat()
            _, st = run("katsuya", g1, D1)
            self.assertEqual(g1.calls, [f"{BASE}/robots.txt", f"{BASE}/katsuya/menu/", f"{BASE}/katsuya/information/"])
            self.assertEqual((st["list_requests"], st["detail_requests"]), (2, 1))  # 詳細ページは取らない
            ids = {p["product_id"] for p in crawl.load_chain_products("katsuya")}
            self.assertEqual(ids, {"90001", "90002", "90003", "2607001", "2607002"})  # 期間限定3 + NEW2。定番・おすすめは記録しない
            self.assertEqual(st["new_products"], 5)
            self.assertEqual(sorted(crawl.baseline_for("katsuya")),
                             sorted(["90001", "90002", "90003", "2510001", "2510002", "2607001", "2607002",
                                     "1341", "1343", "1443"]))
            p = prod("katsuya", "90001")
            self.assertEqual((p["chain"], p["price"], p["limited_kind"], p["status"]), ("katsuya", 979, "期間限定", "on_sale"))
            self.assertEqual(p["badges"], ["期間限定"])
            self.assertEqual(p["official_url"], f"{BASE}/katsuya/menu/90001/")
            self.assertIsNone(p["nutrition"])
            self.assertEqual(p["allergen_pdf_url"], f"{BASE}/katsuya/wp-content/uploads/sites/2/2026/09/20260904_allergy.pdf")
            self.assertEqual(p["price_history"], [{"date": D1, "price": 979}])
            self.assertIsNone(p["description"])
            self.assertEqual(prod("katsuya", "2607001")["limited_kind"], "新メニュー")

    def test_launch_date_from_headline(self):
        with TempData():
            _, st = run("katsuya", kat(), D1)
            p = prod("katsuya", "90001")
            self.assertEqual(p["launch_date"], "2026-10-09")
            self.assertEqual(p["launch_date_source"], "news_headline")
            self.assertEqual(p["announced_at"], "2026-10-06")
            self.assertTrue(p["news_url"].startswith("https://prtimes.jp/"))
            self.assertIn("合成ロース丼", p["news_title"])
            q = prod("katsuya", "2607002")  # 複数商品の見出し。発売日は商品名の後ろに書かれている
            self.assertEqual(q["launch_date"], "2026-07-27")
            self.assertIsNone(prod("katsuya", "90002")["launch_date"])  # 見出しに無い商品
            self.assertEqual(prod("katsuya", "90002")["launch_date_source"], "not_found")
            self.assertEqual((st["news_matched"], st["news_unmatched"]), (3, 2))

    def test_no_pdf_or_prtimes_fetch(self):
        with TempData():
            g1 = kat()
            run("katsuya", g1, D1)
            self.assertTrue(all(c.startswith(BASE) for c in g1.calls))
            self.assertFalse(any(c.endswith(".pdf") or "prtimes" in c for c in g1.calls))

    def test_new_item_after_baseline_is_recorded_as_new_listing(self):
        with TempData():
            run("katsuya", kat(), D1)
            extra = [S.menu_item("katsuya", "2610099", "合成新定番", 800, "880")]
            _, st = run("katsuya", kat(menu=S.katsuya_menu(extra=extra)), D2)
            p = prod("katsuya", "2610099")
            self.assertEqual((p["limited_kind"], p["badges"], st["new_products"]), ("新規掲載", [], 1))

    def test_headline_match_in_90_days_records_a_menu_item(self):
        with TempData():
            # 印の無い定番(2510002)を見出しに載せる。baselineにはあるが、見出しで完全一致するので記録する
            news = S.katsuya_news(extra=[S.news_item("katsuya", "2026-10-10", "【期間限定】10月14日(水)～期間限定で「合成親子丼」販売開始！")])
            run("katsuya", kat(news=news), D1)
            p = prod("katsuya", "2510002")
            self.assertEqual((p["limited_kind"], p["launch_date"]), ("新メニュー", "2026-10-14"))

    def test_price_change_goes_to_history(self):
        with TempData():
            run("katsuya", kat(), D1)
            changed = S.katsuya_menu().replace("税込￥979", "税込￥1,078")
            run("katsuya", kat(menu=changed), D2)
            p = prod("katsuya", "90001")
            self.assertEqual(p["price"], 1078)
            self.assertEqual(p["price_history"], [{"date": D1, "price": 979}, {"date": D2, "price": 1078}])

    def test_allergen_link_follows_the_latest_month(self):
        with TempData():
            run("katsuya", kat(), D1)
            run("katsuya", kat(menu=S.katsuya_menu(allergy="2026/10/20261002_allergy.pdf")), D2)
            self.assertTrue(prod("katsuya", "90001")["allergen_pdf_url"].endswith("2026/10/20261002_allergy.pdf"))

    def test_same_name_across_takeout_and_eatin(self):
        with TempData():
            run("katsuya", kat(), D1)
            extra = [S.menu_item("katsuya", "90077", "合成ロース丼", 900, "990", ["NEW"])]
            run("katsuya", kat(menu=S.katsuya_menu(extra=extra)), D2)
            self.assertEqual(prod("katsuya", "90077")["same_name_as"], ["90001"])
            self.assertNotIn("same_name_as", prod("katsuya", "90001"))


class TestTracking(unittest.TestCase):
    def test_ended_after_two_separate_misses(self):
        with TempData():
            run("katsuya", kat(), D1)
            m = S.katsuya_menu(drop={"90002"})
            run("katsuya", kat(menu=m), D2)
            p = prod("katsuya", "90002")
            self.assertEqual((p["status"], p["missed_runs"]), ("on_sale", 1))
            run("katsuya", kat(menu=m), D2)  # 同じ日の再実行は数えない
            self.assertEqual(prod("katsuya", "90002")["missed_runs"], 1)
            _, st = run("katsuya", kat(menu=m), D3)
            p = prod("katsuya", "90002")
            self.assertEqual((p["status"], p["ended_observed_at"], st["ended"]), ("ended", D3, 1))
            self.assertEqual(prod("katsuya", "90001")["status"], "on_sale")

    def test_empty_or_changed_list_updates_nothing(self):
        with TempData():
            run("katsuya", kat(), D1)
            for bad in ("<html><body>メンテナンス中</body></html>", S.katsuya_menu().replace("menu-list__item", "menu-card")):
                _, st = run("katsuya", kat(menu=bad), D2)
                self.assertFalse(st["list_ok"])
            for _ in range(3):
                run("katsuya", kat(menu=S.menu_page("katsuya", [])), D3)
            for p in crawl.load_chain_products("katsuya"):
                self.assertEqual((p["status"], p.get("missed_runs") or 0), ("on_sale", 0), p["product_id"])

    def test_fetch_failure_updates_nothing(self):
        with TempData():
            run("katsuya", kat(), D1)
            pend, st = run("katsuya", kat(fail={"/katsuya/menu/"}), D2)
            self.assertFalse(st["list_ok"])
            self.assertEqual(sorted(p["last_seen_at"] for p in crawl.load_chain_products("katsuya")), [D1] * 5)

    def test_news_failure_does_not_break_the_menu(self):
        with TempData():
            _, st = run("katsuya", kat(fail={"/katsuya/information/"}), D1)
            self.assertTrue(st["list_ok"])
            self.assertEqual(st["new_products"], 5)
            self.assertIsNone(prod("katsuya", "90001")["launch_date"])

    def test_extremely_short_list_is_not_trusted(self):
        with TempData():
            run("katsuya", kat(), D1)
            self.assertEqual(json.loads(crawl.arcland_state_path().read_text())["katsuya"]["menu_count"], 10)
            few = S.menu_page("katsuya", [("丼", [S.menu_item("katsuya", "2510002", "合成親子丼", 650, "715")])])  # 10件 -> 1件
            for d in (D2, D3, D4):
                _, st = run("katsuya", kat(menu=few), d)
                self.assertFalse(st["list_ok"])
            for p in crawl.load_chain_products("katsuya"):
                self.assertEqual(p["status"], "on_sale", p["product_id"])
            # 前回の半分以上なら信頼する(6件 / 10件)
            six = S.katsuya_menu(drop={"1341", "1343", "1443", "90002"})
            _, st = run("katsuya", kat(menu=six), D2)
            self.assertTrue(st["list_ok"])
            self.assertEqual(json.loads(crawl.arcland_state_path().read_text())["katsuya"]["menu_count"], 6)

    def test_relaunch_starts_a_new_season(self):
        with TempData():
            run("katsuya", kat(), D1)
            m = S.katsuya_menu(drop={"90001"})
            run("katsuya", kat(menu=m), D2)
            run("katsuya", kat(menu=m), D3)
            self.assertEqual(prod("katsuya", "90001")["status"], "ended")
            # 再登場。見出しは新しい日付のものに変わる(昨季の見出しは窓の外か、新しい季の起点に合わない)
            news = S.news_page([S.news_item("katsuya", "2026-12-26", "【期間限定】1月8日(金)～期間限定で「合成ロース丼」販売開始！")])
            _, st = run("katsuya", kat(news=news), "2026-12-28")
            p = prod("katsuya", "90001")
            self.assertEqual(p["status"], "on_sale")
            self.assertEqual(p["relaunches"], [{"ended_observed_at": D3, "reappeared_at": "2026-12-28"}])
            self.assertEqual(len(p["seasons"]), 1)
            self.assertEqual(p["seasons"][0]["launch_date"], "2026-10-09")
            self.assertEqual(p["seasons"][0]["ended_observed_at"], D3)
            self.assertEqual(p["launch_date"], "2027-01-08")  # 年またぎ
            self.assertEqual(st["relaunched"], 1)
            self.assertEqual(p["price_history"][-1], {"date": "2026-12-28", "price": 979, "relaunch": True})


class TestCrawlKarayama(unittest.TestCase):
    def test_first_run_and_same_name_relisting(self):
        with TempData():
            g1 = kara()
            _, st = run("karayama", g1, D1)
            self.assertEqual(len(g1.calls), 3)
            ids = {p["product_id"] for p in crawl.load_chain_products("karayama")}
            self.assertEqual(ids, {"19096", "19100"})  # NEW の印は無い。期間限定だけ
            p = prod("karayama", "19096")
            self.assertEqual((p["price"], p["launch_date"], p["limited_kind"]), (1089, "2026-09-25", "期間限定"))
            self.assertTrue(p["allergen_pdf_url"].endswith("allergy-260828.pdf"))

    def test_same_name_when_both_recorded(self):
        with TempData():
            run("karayama", kara(), D1)
            extra_menu = S.karayama_menu().replace("合成とり天弁当", "合成とり天定食")  # 19100 を 19096 と同名に
            run("karayama", kara(menu=extra_menu), D2)
            self.assertEqual(prod("karayama", "19100").get("same_name_as"), None)  # 19100 は初回に記録済みなので追加はしない
            run("karayama", kara(menu=extra_menu.replace("19100", "19101")), D3)  # 新しい番号で同名が登場
            self.assertEqual(prod("karayama", "19101")["same_name_as"], ["19096"])


class TestSite(unittest.TestCase):
    def make(self, chain="katsuya", pid="90001", **over):
        with TempData():
            if chain == "katsuya":
                run("katsuya", kat(), D1)
            else:
                run("karayama", kara(), D1)
            p = prod(chain, pid)
        p.update(over)
        return p

    def test_title_and_description_without_nutrition(self):
        p = self.make()
        self.assertEqual(g.seo_title(p), "かつや「合成ロース丼」の価格・販売期間")
        d = g.seo_description(p)
        self.assertIn("かつやの限定メニュー「合成ロース丼」", d)
        self.assertIn("979円（税込）", d)
        q = self.make(pid="2607001")
        self.assertIn("かつやのメニュー「合成ライト丼」", g.seo_description(q))  # 「NEW」は限定と呼ばない

    def test_item_page(self):
        h = g.render_item_page(self.make())
        self.assertIn("栄養成分は公式サイトに掲載がありません", h)
        self.assertNotIn("栄養成分は公式サイトに掲載がありませんでした", h)  # 他チェーンの文言とは別
        self.assertIn("アレルゲン情報は公式PDFをご覧ください", h)
        self.assertIn("20260904_allergy.pdf", h)
        self.assertIn("発売日は公式のお知らせ（2026-10-06）の見出しによります", h)
        self.assertIn("公式お知らせ（PR TIMES）", h)
        self.assertIn("店舗により価格・取り扱いが異なる場合があります", h)
        self.assertIn(f"{BASE}/katsuya/menu/90001/", h)
        self.assertIn("2026-10-09", h)

    def test_item_page_without_allergen_link_and_without_launch_date(self):
        h = g.render_item_page(self.make(pid="90002", allergen_pdf_url=None))
        self.assertIn("アレルゲンは公式サイトをご覧ください", h)
        self.assertNotIn("公式お知らせ（PR TIMES）", h)

    def test_karayama_page(self):
        h = g.render_item_page(self.make("karayama", "19096"))
        self.assertIn("からやま", h)
        self.assertIn("allergy-260828.pdf", h)

    def test_chain_config(self):
        self.assertEqual(g.CHAIN_NAME["katsuya"], "かつや")
        self.assertEqual(g.CHAIN_NAME["karayama"], "からやま")
        self.assertIn("・かつや・からやま", g.CHAIN_LIST_TEXT)
        for k in ("site_description", "site_tagline"):
            self.assertIn("かつや", g.CONFIG[k])
            self.assertIn("からやま", g.CONFIG[k])
        about = g.CONFIG["about"]
        self.assertEqual(len(about), 4)
        self.assertLess(sum(len(p) for p in about), 700)
        self.assertTrue(any("かつや" in p and "からやま" in p for p in about))
        for c in ("katsuya", "karayama"):
            self.assertIn(f"--{c}:", g.CSS)
            self.assertIn(f'data-chain="{c}"', g.CSS)
        colors = [c["color"].lower() for c in g.CHAINS]
        self.assertEqual(len(colors), len(set(colors)))
        self.assertIn("arclandservice.co.jp/katsuya/", g.page_shell("x", "d", "<p>b</p>", "https://fastfood.netaful.jp/"))

    def test_notification_labels(self):
        import notify
        self.assertEqual((notify.CHAIN_LABEL["katsuya"], notify.CHAIN_LABEL["karayama"]), ("かつや", "からやま"))

    def test_config_limits_are_small(self):
        self.assertLessEqual(crawl.MAX_REQUESTS["katsuya"], 10)
        self.assertLessEqual(crawl.MAX_REQUESTS["karayama"], 10)
        self.assertGreaterEqual(crawl.INTERVALS["katsuya"], 3)


class TestNetaful(unittest.TestCase):
    def test_title_filter_keeps_only_own_chain(self):
        self.assertEqual((nr.TAGS["katsuya"], nr.TAGS["karayama"]), (874, 877))
        self.assertTrue(nr.title_passes_filter("katsuya", "【かつや】海老トリプル丼を食べた"))
        self.assertFalse(nr.title_passes_filter("katsuya", "【からやま】とり天定食を食べた"))
        self.assertFalse(nr.title_passes_filter("katsuya", "【松のや】ロースカツ定食を食べた"))
        self.assertTrue(nr.title_passes_filter("karayama", "【からやま】とり天定食を食べた"))
        self.assertFalse(nr.title_passes_filter("karayama", "【かつや】海老トリプル丼を食べた"))
        self.assertIn("katsuya", nr.TITLE_ALWAYS)
        self.assertIn("karayama", nr.TITLE_ALWAYS)

    def test_related_keywords_prefer_own_chain(self):
        arts = {1: {"id": 1, "chain": "matsunoya", "title": "松のやのロースカツ", "date": "2026-02-01"},
                2: {"id": 2, "chain": "katsuya", "title": "かつやのロースカツ", "date": "2025-02-01"}}
        it = {"chain": "katsuya", "product_id": "9", "name": "合成ロースカツ丼"}
        label, got = g.select_related_articles(it, arts)
        self.assertEqual(got[0]["id"], 2)


@helpers.needs_fixtures
class TestRealSnapshot(unittest.TestCase):
    """手元に実物のスナップショット(tests/fixtures/arcland/。コミットされない)があるときだけ走る補助テスト。"""

    def test_menus_parse(self):
        for brand, n in (("katsuya", 107), ("karayama", 77)):
            m = P.parse_arcland_menu(fx(f"arcland/{brand}_menu.html"), brand)
            self.assertEqual(len(m["items"]), n)
            self.assertTrue(all(e["price"] and e["name"] for e in m["items"]))
            self.assertIn("allergy", m["allergen_pdf_url"])

    def test_news_parse(self):
        self.assertGreater(len(P.parse_arcland_news(fx("arcland/katsuya_info.html"), "katsuya")), 200)
        self.assertGreater(len(P.parse_arcland_news(fx("arcland/karayama_info.html"), "karayama")), 100)


if __name__ == "__main__":
    unittest.main()
