"""幸楽苑・日高屋(11・12チェーン目)のテスト: パーサ(一覧・詳細・お知らせ)、発売日の抽出と見出しの照合、
記録しない商品の除外、baseline・定番・販売状況の追跡、日付形式IDの付け替えと歯止め、robots.txt、サイトの表示。
フィクスチャは tests/synthetic_ramen.py の合成データ(公式ページのコピーはリポジトリに入れない)。
手元に実物のスナップショット(tests/fixtures/kourakuen/・hidakaya/)があるときだけ、実物でも確かめる補助テストを走らせる。"""

from __future__ import annotations

import json
import unittest

import helpers  # noqa: F401
from helpers import TempData, fx
import crawl
import fetch_netaful_reviews as nr
import generate_site as g
import parsers as P
import synthetic_ramen as S

D1, D2, D3, D4 = "2026-11-18", "2026-11-19", "2026-11-20", "2026-11-21"
KMENU = S.KOU_STORES + "/grandmenu/"
KNEWS = S.KOU_WWW + "/news"
HNEWS = S.HID + "/json/news.json"


class FakeRamen:
    """URLからページを返す偽の取得器。呼ばれたURLを記録する。pages にあるURLだけ返し、fail のURLは失敗させる。"""

    def __init__(self, pages: dict, fail=(), robots_stores=S.KOU_ROBOTS_STORES_HTML, robots_www=S.KOU_ROBOTS_WWW,
                 robots_hid=S.HID_ROBOTS):
        self.pages = dict(pages)
        self.fail = set(fail)
        self.calls: list[str] = []
        self.robots = {S.KOU_STORES + "/robots.txt": robots_stores, S.KOU_WWW + "/robots.txt": robots_www,
                       S.HID + "/robots.txt": robots_hid}

    def __call__(self, chain: str, url: str) -> str:
        self.calls.append(url)
        if url in self.fail:
            raise OSError("simulated failure")
        if url in self.robots:
            return self.robots[url]
        return self.pages[url]


def kou(menu=None, news=None, details=None, **kw):
    pages = {KMENU: menu if menu is not None else S.kou_menu_default(), KNEWS: news if news is not None else S.kou_news()}
    for pid in ("101", "102", "111", "112", "121", "131", "141", "151", "154", "161"):
        pages[f"{S.KOU_STORES}/grandmenu/info.html?id={pid}"] = S.kou_detail(name=f"合成{pid}", price=100)
    pages.update(details or {})
    return FakeRamen(pages, **kw)


def hid(pages=None, news=None, details=None, **kw):
    p = {S.HID + path: html for path, html in (pages or S.hid_pages()).items()}
    p[HNEWS] = news if news is not None else S.hid_news()
    for pid in ("1", "2", "20261009", "20261201", "808-1", "18", "151", "30", "801"):
        p[f"{S.HID}/hits/ja/menu/1/detail/{pid}.html"] = S.hid_detail(name=f"合成{pid}")
    p.update(details or {})
    return FakeRamen(p, **kw)


def run(chain, getter, day, pending=None):
    pending = {} if pending is None else pending
    stats = {}
    crawl.CRAWLERS[chain](getter, pending, stats, day)
    return pending, stats[chain]


def prod(chain, pid):
    return crawl.load_product(chain, pid)


class TestKourakuenParsers(unittest.TestCase):
    def setUp(self):
        self.m = P.parse_kourakuen_menu(S.kou_menu_default())
        self.by = {e["product_id"]: e for e in self.m}

    def test_dedupes_ids_and_reads_prices(self):
        self.assertEqual(len(self.m), 15)  # 「おすすめ」と醤油らーめんに出る 111 は1件
        self.assertEqual(self.by["131"]["price"], 1230)  # 「1,230」のカンマ
        self.assertEqual(self.by["111"]["category"], "醤油らーめん")  # 「おすすめ」ではなく名前のあるタブ
        self.assertTrue(self.by["111"]["in_recommended"])
        self.assertIsNone(self.by["101"]["category"])  # 「おすすめ」だけ
        self.assertEqual(self.by["161"]["notes"], ["15:00からの販売となります。"])
        self.assertEqual(self.by["111"]["official_url"], "https://stores.kourakuen.co.jp/grandmenu/info.html?id=111")

    def test_exclusions(self):
        ex = {pid: P.kourakuen_excluded(e) for pid, e in self.by.items()}
        self.assertEqual(ex["103"], "set")        # 名前に「セット」
        self.assertEqual(ex["142"], "set")
        self.assertEqual(ex["171"], "category")   # アルコール
        self.assertEqual(ex["152"], "topping")    # もう一品の130円以下
        self.assertEqual(ex["153"], "topping")
        self.assertIsNone(ex["151"])              # 餃子280円は残る
        self.assertIsNone(ex["141"])              # キッズのお子様らーめん(セットではない)は残る
        self.assertIsNone(ex["161"])              # 時間帯限定の単品は残る

    def test_structure_change_gives_no_items(self):
        self.assertEqual(P.parse_kourakuen_menu("<html><body>メンテナンス中</body></html>"), [])
        self.assertEqual(P.parse_kourakuen_menu(S.kou_menu_default().replace("c-menuListItem", "x")), [])

    def test_detail(self):
        d = P.parse_kourakuen_detail(S.kou_detail())
        self.assertEqual(d["name"], "合成中華そば")
        self.assertEqual(d["nutrition"]["kcal"], 639.0)
        self.assertEqual((d["nutrition"]["protein_g"], d["nutrition"]["fat_g"], d["nutrition"]["carbs_g"], d["nutrition"]["salt_g"]),
                         (24.4, 16.6, 89.1, 9.8))
        self.assertEqual(d["allergens"], {"contains": ["卵", "小麦"], "partial": []})  # ●のある品目だけ
        self.assertEqual(P.parse_kourakuen_detail(S.kou_detail(kcal="1,120Kcal"))["nutrition"]["kcal"], 1120.0)  # カンマ区切り
        self.assertEqual(P.parse_hidakaya_detail(S.hid_detail(kcal="1,050"))["nutrition"]["kcal"], 1050.0)
        self.assertIsNone(P.parse_kourakuen_detail("<html><body>x</body></html>"))
        self.assertEqual(P.parse_kourakuen_detail(S.kou_detail(allergens=()))["allergens"]["contains"], [])

    def test_news(self):
        n = P.parse_kourakuen_news(S.kou_news())
        self.assertEqual([a["date"] for a in n], sorted((a["date"] for a in n), reverse=True))
        self.assertEqual(n[0]["date"], "2026-11-16")
        self.assertEqual(len(n), 4)
        self.assertEqual(P.parse_kourakuen_news("<html></html>"), [])


class TestHidakayaParsers(unittest.TestCase):
    def test_list_and_exclusions(self):
        pages = S.hid_pages()
        e = {x["product_id"]: x for x in P.parse_hidakaya_list(pages["/hits/ja/menu/1/list/5.html"], "おつまみ")}
        self.assertEqual(set(e), {"30", "808-1", "64"})
        self.assertEqual(e["808-1"]["price"], 210)
        self.assertIsNone(e["64"]["price"])  # 商品名に価格が入っていて価格欄が無い
        self.assertEqual(P.hidakaya_excluded(e["64"]), "no_price")
        self.assertIsNone(P.hidakaya_excluded(e["30"]))
        s = {x["product_id"]: x for x in P.parse_hidakaya_list(pages["/hits/ja/menu/1/list/4.html"], "単品")}
        self.assertEqual(P.hidakaya_excluded(s["197"]), "set")
        self.assertEqual(s["151"]["official_url"], "https://hidakaya.hiday.co.jp/hits/ja/menu/1/detail/151.html")
        self.assertEqual(P.parse_hidakaya_list("<html><body>x</body></html>"), [])

    def test_dated_id(self):
        self.assertTrue(P.hidakaya_is_dated_id("20261009"))
        for pid in ("1", "9_196", "808-1", "299_001", "162_01"):
            self.assertFalse(P.hidakaya_is_dated_id(pid))

    def test_detail_with_note(self):
        d = P.parse_hidakaya_detail(S.hid_detail(kcal="896", salt="7.3"))
        self.assertEqual((d["nutrition"]["kcal"], d["nutrition"]["salt_g"]), (896.0, 7.3))
        self.assertEqual(d["nutrition_note"], "スープを含む")
        self.assertEqual(d["allergens"]["contains"], ["大豆", "卵", "鶏肉", "小麦粉"])
        self.assertEqual(d["price"], 420)
        plain = P.parse_hidakaya_detail(S.hid_detail(note=""))
        self.assertIsNone(plain["nutrition_note"])
        self.assertIsNone(P.parse_hidakaya_detail("<html><body>x</body></html>"))

    def test_news(self):
        n = P.parse_hidakaya_news(S.hid_news())
        self.assertEqual(n[0]["date"], "2026-10-28")
        self.assertEqual(len(n), 3)
        self.assertIn("期間限定", n[1]["title"])
        with self.assertRaises(ValueError):
            P.parse_hidakaya_news("{}")


class TestHidakayaPeriodTag(unittest.TestCase):
    """一覧の名前の先頭の【期間限定】(お知らせに出ない限定品がある。2025年のチゲ味噌ラーメン)。"""

    def test_split_name(self):
        f = P.hidakaya_split_name
        self.assertEqual(f("【期間限定】チゲ味噌ラーメン"), ("チゲ味噌ラーメン", True, False))
        self.assertEqual(f(" 【 期間限定 】 チゲ味噌ラーメン"), ("チゲ味噌ラーメン", True, False))  # 空白の揺れ
        self.assertEqual(f("[期間限定]チゲ味噌ラーメン"), ("チゲ味噌ラーメン", True, False))  # 半角括弧
        self.assertEqual(f("カタヤキソバ【店舗限定】"), ("カタヤキソバ", False, True))
        self.assertEqual(f("【期間限定】【店舗限定】合成X"), ("合成X", True, True))
        self.assertEqual(f("合成中華そば"), ("合成中華そば", False, False))

    def test_list_flags_and_id(self):
        page = S.hid_list("ラーメン", [S.hid_item("801", "【期間限定】合成チゲ味噌ラーメン", 790),
                                   S.hid_item("12", "合成カタヤキソバ【店舗限定】", 720)])
        e = {x["product_id"]: x for x in P.parse_hidakaya_list(page, "ラーメン")}
        self.assertEqual((e["801"]["name"], e["801"]["period_limited"], e["801"]["store_limited"]), ("合成チゲ味噌ラーメン", True, False))
        self.assertEqual((e["12"]["name"], e["12"]["store_limited"]), ("合成カタヤキソバ", True))
        self.assertEqual(e["801"]["official_url"], S.HID + "/hits/ja/menu/1/detail/801.html")

    def pages(self, tag=True, store=False):
        name = ("【期間限定】" if tag else "") + "合成チゲ味噌ラーメン" + ("【店舗限定】" if store else "")
        return S.hid_pages(extra={1: [S.hid_item("801", name, 790)]})

    def test_recorded_as_limited_without_news_and_name_stripped(self):
        with TempData():  # 定番は既定でオフ。【期間限定】はその枠に頼らず記録される
            _, st = run("hidakaya", hid(pages=self.pages(), details={f"{S.HID}/hits/ja/menu/1/detail/801.html": S.hid_detail(name="合成チゲ味噌ラーメン", price=790)}), D1)
            p = prod("hidakaya", "801")
            self.assertEqual((p["name"], p["limited_kind"], p["launch_date"], p["is_regular"] if "is_regular" in p else False),
                             ("合成チゲ味噌ラーメン", "期間限定", None, False))
            self.assertIn("801", crawl.baseline_for("hidakaya"))
            self.assertEqual(st["new_products"], 2)  # 日付形式IDの1件 + 801

    def test_launch_date_from_news_when_both(self):
        news = S.hid_news(extra=[{"news_id": 400, "news_kbn": "2", "news_title": "【期間限定】合成チゲ味噌ラーメン発売開始",
                                  "news_body": "11月10日(火)より販売します。", "display_date_from": "2026/11/10", "link": ""}])
        with TempData():
            run("hidakaya", hid(pages=self.pages(), news=news), D1)
            p = prod("hidakaya", "801")
            self.assertEqual((p["limited_kind"], p["launch_date"], p["announced_at"]), ("期間限定", "2026-11-10", "2026-11-10"))

    def test_regular_is_upgraded_but_never_goes_back(self):
        with TempData(regular=True):
            run("hidakaya", hid(pages=self.pages(tag=False)), D1)
            p = prod("hidakaya", "801")
            self.assertEqual((p["limited_kind"], p["is_regular"]), ("定番", True))
            run("hidakaya", hid(pages=self.pages(tag=True)), D2)
            p = prod("hidakaya", "801")
            self.assertEqual((p["limited_kind"], p["is_regular"]), ("期間限定", False))
            run("hidakaya", hid(pages=self.pages(tag=False)), D3)  # 印が外れても定番に戻さない
            p = prod("hidakaya", "801")
            self.assertEqual((p["limited_kind"], p["is_regular"], p["name"]), ("期間限定", False, "合成チゲ味噌ラーメン"))
            self.assertEqual(sorted(x["product_id"] for x in crawl.load_chain_products("hidakaya")).count("801"), 1)  # IDは変わらない

    def test_store_limited_is_kept_as_information(self):
        with TempData(regular=True):
            run("hidakaya", hid(pages=self.pages(tag=False, store=True)), D1)
            p = prod("hidakaya", "801")
            self.assertEqual((p["name"], p["store_limited"], p["limited_kind"]), ("合成チゲ味噌ラーメン", True, "定番"))
            self.assertIn("店舗限定", p["price_note"])
            self.assertIn("店舗限定", g.render_item_page(p))
            self.assertNotIn("【店舗限定】", g.render_item_page(p))


class TestMatching(unittest.TestCase):
    sd = staticmethod(P.ramen_start_date)

    def test_start_date_forms(self):
        self.assertEqual(self.sd("「トムヤムクンらーめん」が11月4日（水）より、登場！", "2026-10-29"), "2026-11-04")
        self.assertEqual(self.sd("2026年6月30日（火）より「魚介豚骨つけめん」期間限定で販売開始！", "2026-06-23"), "2026-06-30")
        self.assertEqual(self.sd("【6/2より販売開始】期間限定「冷し味噌らーめん」", "2026-06-02"), "2026-06-02")
        self.assertEqual(self.sd("10月９日(金)より「X」を期間限定で発売開始します。", "2026-10-09"), "2026-10-09")  # 全角数字
        self.assertIsNone(self.sd("「X」期間限定販売！", "2026-08-19"))

    def test_year_rollover(self):
        self.assertEqual(self.sd("1月8日(木)より販売開始", "2026-12-25"), "2027-01-08")
        self.assertEqual(self.sd("12月20日(日)より販売開始", "2027-01-05"), "2026-12-20")

    def test_name_in_title(self):
        t = P.ramen_name_in_title
        self.assertTrue(t("メガたんめん", "「メガたんめん（味噌・塩）」11月4日（水）より、期間限定販売！"))  # 括弧書きを外す
        self.assertTrue(t("担担麺", "期間限定「鶏白湯らーめん」「担担麺」「麻辣担担麺」の3種が登場！"))
        self.assertFalse(t("つけめん", "「エビ味噌つけめん」期間限定販売！"))  # 部分一致は認めない(括弧のある見出し)
        self.assertTrue(t("濃厚豚骨醤油ラーメン", "【期間限定】濃厚豚骨醤油ラーメン発売開始しました"))  # 括弧が無い見出しは部分一致(4文字以上)
        self.assertFalse(t("ねぎ", "【期間限定】ねぎ塩ラーメン発売"))  # 短い名前は括弧が無い見出しでは認めない

    def test_match_window_and_non_launch(self):
        news = [{"date": "2026-10-01", "title": "「合成X」販売終了のお知らせ", "url": "u", "category": ""},
                {"date": "2026-10-02", "title": "幸楽苑 合成店 グランドオープン！", "url": "u", "category": ""},
                {"date": "2026-05-01", "title": "期間限定「合成Y」販売開始！", "url": "u", "category": ""},
                {"date": "2026-10-03", "title": "期間限定「合成Z」10月10日より販売開始！", "url": "u", "category": ""}]
        self.assertIsNone(P.match_ramen_launch("合成X", news, D1))
        self.assertIsNone(P.match_ramen_launch("合成Y", news, D1))  # 120日より前
        self.assertIsNone(P.match_ramen_launch("合成Y", news, D1, before=P.RAMEN_NEWS_RECORD_DAYS))
        hit = P.match_ramen_launch("合成Z", news, D1, before=P.RAMEN_NEWS_RECORD_DAYS)
        self.assertEqual((hit["launch_date"], hit["limited"]), ("2026-10-10", True))

    def test_body_date_used_when_headline_has_none(self):
        hit = P.match_ramen_launch("合成濃厚豚骨ラーメン", P.parse_hidakaya_news(S.hid_news()), D1)
        self.assertEqual(hit["launch_date"], "2026-10-09")
        self.assertTrue(hit["limited"])
        # 商品名が見出しに無く本文だけにあるお知らせは使わない
        news = [{"date": "2026-10-09", "title": "新商品発売のお知らせ", "body": "「合成豚骨」を発売します", "url": "", "category": ""}]
        self.assertIsNone(P.match_ramen_launch("合成豚骨", news, D1))


class TestCrawlKourakuen(unittest.TestCase):
    def test_first_run_records_only_news_items_and_baseline(self):
        with TempData():
            g1 = kou()
            _, st = run("kourakuen", g1, D1)
            self.assertEqual(g1.calls[:4], [S.KOU_STORES + "/robots.txt", S.KOU_WWW + "/robots.txt", KMENU, KNEWS])
            self.assertEqual(len(g1.calls), 6)  # robots2 + メニュー + お知らせ + 詳細2(限定・新メニューのみ。定番は既定で止めている)
            self.assertEqual({p["product_id"] for p in crawl.load_chain_products("kourakuen")}, {"101", "102"})
            self.assertEqual(sorted(crawl.baseline_for("kourakuen")),
                             sorted(["101", "102", "111", "112", "121", "131", "141", "151", "154", "161"]))  # 除外した商品はbaselineに入れない
            self.assertEqual((st["new_products"], st["regular_created"], st["limited_seen"]), (2, 0, 2))
            a, b = prod("kourakuen", "101"), prod("kourakuen", "102")
            self.assertEqual((a["limited_kind"], a["launch_date"], a["announced_at"]), ("新メニュー", "2026-11-04", "2026-11-10"))
            self.assertEqual((b["limited_kind"], b["launch_date"]), ("期間限定", "2026-11-04"))
            self.assertEqual(b["launch_date_source"], "news_headline")
            self.assertTrue(b["news_url"].endswith("/news/901"))
            self.assertEqual(b["price"], 860)  # 一覧の価格
            self.assertEqual(b["nutrition"]["kcal"], 639.0)
            self.assertEqual(b["allergens"]["contains"], ["卵", "小麦"])
            self.assertEqual(b["allergen_pdf_url"], "https://www.kourakuen.co.jp/upload/pdf/allergy.pdf")
            self.assertIsNone(b["description"])
            self.assertIsNone(b["image"])
            self.assertIn("一部店舗では販売していません", b["price_note"])
            self.assertEqual(b["price_history"], [{"date": D1, "price": 860}])

    def test_headline_older_than_90_days_is_not_a_limited_item(self):
        with TempData():
            run("kourakuen", kou(), D1)
            self.assertIsNone(prod("kourakuen", "131"))  # 6月のお知らせの「合成つけめん」は定番扱い(既定では記録しない)

    def test_regular_items_with_budget_and_deferral(self):
        with TempData(regular=True, regular_per_run=3):
            g1 = kou()
            _, st = run("kourakuen", g1, D1)
            self.assertEqual((st["new_products"], st["regular_created"], st["regular_deferred"]), (2, 3, 5))
            self.assertEqual(len(g1.calls), 4 + 2 + 3)
            r = [p for p in crawl.load_chain_products("kourakuen") if p.get("is_regular")]
            self.assertEqual(len(r), 3)
            self.assertTrue(all(p["limited_kind"] == "定番" and p["launch_date"] is None for p in r))
            _, st2 = run("kourakuen", kou(), D2)  # 翌日に続きを作る(baselineにあるので「新メニュー」には数えない)
            self.assertEqual((st2["new_products"], st2["regular_created"]), (0, 3))

    def test_excluded_items_are_never_recorded(self):
        with TempData(regular=True):
            run("kourakuen", kou(), D1)
            ids = {p["product_id"] for p in crawl.load_chain_products("kourakuen")}
            self.assertEqual(ids, {"101", "102", "111", "112", "121", "131", "141", "151", "154", "161"})
            self.assertTrue(ids.isdisjoint({"103", "142", "152", "153", "171"}))  # セット・トッピング・アルコール
            self.assertEqual(prod("kourakuen", "161")["sale_notes"], ["15:00からの販売となります。"])
            self.assertIn("15:00からの販売となります。", prod("kourakuen", "161")["price_note"])

    def test_new_item_after_baseline_is_recorded_as_new_listing(self):
        with TempData():
            run("kourakuen", kou(), D1)
            extra = [("もう一品", [S.kou_item("901", "合成新メニュー", 700)])]
            g2 = kou(menu=S.kou_menu_default(extra=extra),
                     details={f"{S.KOU_STORES}/grandmenu/info.html?id=901": S.kou_detail(name="合成新メニュー", price=700)})
            _, st = run("kourakuen", g2, D2)
            p = prod("kourakuen", "901")
            self.assertEqual((p["limited_kind"], st["new_products"]), ("新規掲載", 1))

    def test_price_change_goes_to_history(self):
        with TempData():
            run("kourakuen", kou(), D1)
            run("kourakuen", kou(menu=S.kou_menu_default().replace(">860<", ">880<")), D2)
            p = prod("kourakuen", "102")
            self.assertEqual((p["price"], p["price_history"]), (880, [{"date": D1, "price": 860}, {"date": D2, "price": 880}]))

    def test_ended_after_two_separate_misses_and_not_for_one(self):
        with TempData():
            run("kourakuen", kou(), D1)
            gone = S.kou_menu_default(drop=("102",))
            run("kourakuen", kou(menu=gone), D2)
            self.assertEqual(prod("kourakuen", "102")["status"], "on_sale")
            run("kourakuen", kou(menu=gone), D2)  # 同じ日の再実行は数えない
            self.assertEqual(prod("kourakuen", "102")["status"], "on_sale")
            _, st = run("kourakuen", kou(menu=gone), D3)
            p = prod("kourakuen", "102")
            self.assertEqual((p["status"], p["ended_observed_at"], st["ended"]), ("ended", D3, 1))
            run("kourakuen", kou(), D4)  # 再登場
            self.assertEqual(prod("kourakuen", "102")["status"], "on_sale")

    def test_empty_or_failed_list_updates_nothing(self):
        with TempData():
            run("kourakuen", kou(), D1)
            for getter in (kou(menu="<html><body>メンテナンス中</body></html>"), kou(fail={KMENU})):
                _, st = run("kourakuen", getter, D3)
                self.assertFalse(st["list_ok"])
                self.assertEqual(prod("kourakuen", "102")["missed_runs"], 0)
                self.assertEqual(prod("kourakuen", "102")["status"], "on_sale")

    def test_extremely_short_list_is_not_trusted(self):
        with TempData():
            run("kourakuen", kou(), D1)
            tiny = S.kou_menu([("醤油らーめん", [S.kou_item("111", "合成中華そば", 490), S.kou_item("112", "合成ねぎらーめん", 630)])])
            _, st = run("kourakuen", kou(menu=tiny), D3)
            self.assertFalse(st["list_ok"])
            self.assertEqual(prod("kourakuen", "102")["missed_runs"], 0)

    def test_news_failure_does_not_break_the_menu(self):
        with TempData():
            _, st = run("kourakuen", kou(fail={KNEWS}), D1)
            self.assertTrue(st["list_ok"])
            self.assertIsNotNone(crawl.baseline_for("kourakuen"))

    def test_detail_failure_is_retried_next_day(self):
        url = f"{S.KOU_STORES}/grandmenu/info.html?id=102"
        with TempData():
            pending, st = run("kourakuen", kou(fail={url}), D1)
            self.assertEqual(st["failed"], 1)
            self.assertIsNone(prod("kourakuen", "102"))
            self.assertIn("102", pending["kourakuen"])
            pending, st = run("kourakuen", kou(), D2, pending)
            self.assertEqual(prod("kourakuen", "102")["limited_kind"], "期間限定")
            self.assertNotIn("102", pending["kourakuen"])

    def test_stores_robots_html_is_treated_as_no_restriction_and_www_disallow_is_obeyed(self):
        with TempData():
            _, st = run("kourakuen", kou(), D1)  # stores の robots.txt は404ページのHTML
            self.assertTrue(st["list_ok"])
        with TempData():
            g1 = kou(robots_www="User-agent: *\nDisallow: /news\n")
            _, st = run("kourakuen", g1, D1)
            self.assertNotIn(KNEWS, g1.calls)
            self.assertTrue(st["list_ok"])
            self.assertEqual(crawl.load_chain_products("kourakuen"), [])  # お知らせが無いので限定は見つからない
        with TempData():
            g1 = kou(robots_stores="User-agent: *\nDisallow: /grandmenu/\n")
            _, st = run("kourakuen", g1, D1)
            self.assertNotIn(KMENU, g1.calls)
            self.assertFalse(st["list_ok"])

    def test_blocked_stops_without_updating(self):
        class Blocked(FakeRamen):
            def __call__(self, chain, url):
                if url == KMENU:
                    raise crawl.BlockedError("HTTP 403")
                return super().__call__(chain, url)
        with TempData():
            _, st = run("kourakuen", Blocked({}), D1)
            self.assertTrue(st["blocked"])
            self.assertIsNone(crawl.baseline_for("kourakuen"))


class TestCrawlHidakaya(unittest.TestCase):
    def test_first_run(self):
        with TempData():
            g1 = hid()
            _, st = run("hidakaya", g1, D1)
            self.assertEqual(g1.calls[0], S.HID + "/robots.txt")
            self.assertEqual(sorted(c.replace(S.HID, "") for c in g1.calls[1:6]), sorted(S.HID_LIST_PATHS.values()))
            self.assertEqual(len(g1.calls), 8)  # robots + 一覧5 + お知らせ + 詳細1(限定)
            self.assertFalse(any("/list/2.html" in c or "/list/6.html" in c or "/list/7.html" in c or "/list/8.html" in c for c in g1.calls))
            ids = {p["product_id"] for p in crawl.load_chain_products("hidakaya")}
            self.assertEqual(ids, {"20261009"})
            self.assertEqual(sorted(crawl.baseline_for("hidakaya")), sorted(["1", "2", "20261009", "808-1", "18", "151", "30"]))
            p = prod("hidakaya", "20261009")
            self.assertEqual((p["limited_kind"], p["launch_date"], p["announced_at"], p["category"]),
                             ("期間限定", "2026-10-09", "2026-10-09", "ラーメン"))
            self.assertEqual(p["nutrition"]["kcal"], 636.0)
            self.assertEqual(p["nutrition_note"], "スープを含む")
            self.assertTrue(p["dated_id"])
            self.assertEqual(p["official_url"], S.HID + "/hits/ja/menu/1/detail/20261009.html")
            self.assertIsNone(p["description"])
            self.assertEqual(st["limited_seen"], 1)

    def test_dated_id_without_news_is_recorded_as_new_listing(self):
        with TempData():
            _, st = run("hidakaya", hid(news="[]"), D1)
            self.assertEqual(prod("hidakaya", "20261009")["limited_kind"], "新規掲載")  # お知らせが消えた後でも落とさない
            self.assertIsNone(prod("hidakaya", "20261009")["launch_date"])
            self.assertEqual(st["new_products"], 1)

    def test_regular_items_use_list_price(self):
        with TempData(regular=True):
            _, st = run("hidakaya", hid(), D1)
            self.assertEqual(st["regular_created"], 6)
            p = prod("hidakaya", "808-1")
            self.assertEqual((p["limited_kind"], p["is_regular"], p["price"], p["category"]), ("定番", True, 210, "おつまみ"))
            self.assertEqual(p["price_history"], [{"date": D1, "price": 210}])  # 詳細ページの価格(420)ではなく一覧の価格
            ids = {p["product_id"] for p in crawl.load_chain_products("hidakaya")}
            self.assertTrue(ids.isdisjoint({"197", "139", "64"}))  # セット・価格欄なしは記録しない

    def test_price_not_written_when_a_list_page_failed(self):
        with TempData(regular=True):
            run("hidakaya", hid(), D1)
            changed = S.hid_pages()
            changed = {k: v.replace(">420<", ">450<") for k, v in changed.items()}  # 「おすすめ」とラーメンの両方に出る
            g2 = hid(pages=changed, fail={S.HID + "/hits/ja/menu/1/list/5.html"})
            _, st = run("hidakaya", g2, D2)
            self.assertFalse(st["list_ok"])
            self.assertEqual(prod("hidakaya", "1")["price_history"], [{"date": D1, "price": 420}])  # 一部失敗の回は価格を書かない
            self.assertEqual(prod("hidakaya", "30")["missed_runs"], 0)  # 終了判定もしない
            run("hidakaya", hid(pages=changed), D3)
            self.assertEqual(prod("hidakaya", "1")["price_history"][-1], {"date": D3, "price": 450})

    def test_renumbered_dated_id_does_not_spread(self):
        with TempData(regular=True):
            run("hidakaya", hid(), D1)
            before = {p["product_id"] for p in crawl.load_chain_products("hidakaya")}
            renamed = S.hid_pages(dated="20261201")
            run("hidakaya", hid(pages=renamed), D2)
            self.assertEqual(prod("hidakaya", "20261201")["limited_kind"], "期間限定")  # 同名商品がお知らせにも合う
            self.assertEqual(prod("hidakaya", "20261009")["status"], "on_sale")  # まだ1回目の欠け
            _, st = run("hidakaya", hid(pages=renamed), D3)
            self.assertEqual(prod("hidakaya", "20261009")["status"], "ended")
            self.assertEqual(st["ended"], 1)  # 巻き込まれて終わった商品は他に無い
            self.assertEqual({p["product_id"] for p in crawl.load_chain_products("hidakaya")}, before | {"20261201"})
            self.assertTrue(all(prod("hidakaya", pid)["status"] == "on_sale" for pid in before - {"20261009"}))

    def test_burst_filter_stops_mass_renumbering(self):
        with TempData(regular=True):
            run("hidakaya", hid(), D1)
            many = [S.hid_item(f"2026{i:04d}", f"合成新{i}", 500) for i in range(1101, 1113)]  # 12件が一度に新ID
            pages = S.hid_pages(extra={4: many})
            stats = {}
            pending = {}
            crawl.CRAWLERS["hidakaya"](hid(pages=pages), pending, stats, D2)
            st = stats["hidakaya"]
            self.assertIn("suspected_restructure", st)
            self.assertFalse(st["list_ok"])
            self.assertEqual(st["new_products"], 0)
            self.assertTrue(all(prod("hidakaya", f"2026{i:04d}") is None for i in range(1101, 1113)))
            self.assertIn(crawl.BURST_KEY, pending["hidakaya"])
            self.assertEqual(prod("hidakaya", "30")["missed_runs"], 0)  # 終了判定もしない

    def test_robots_disallow_is_obeyed_and_named_bots_do_not_apply(self):
        with TempData():
            _, st = run("hidakaya", hid(), D1)  # GPTBot等の名指しの拒否は、このサイトのUAには当たらない
            self.assertTrue(st["list_ok"])
        with TempData():
            g1 = hid(robots_hid="User-agent: *\nDisallow: /hits/ja/menu/1/list/3.html\n")
            _, st = run("hidakaya", g1, D1)
            self.assertNotIn(S.HID + "/hits/ja/menu/1/list/3.html", g1.calls)
            self.assertFalse(st["list_ok"])
            self.assertIsNone(crawl.baseline_for("hidakaya"))

    def test_news_failure_and_empty_list(self):
        with TempData():
            _, st = run("hidakaya", hid(fail={HNEWS}), D1)
            self.assertTrue(st["list_ok"])
        with TempData():
            run("hidakaya", hid(), D1)
            empty = {path: S.hid_list("x", []) for path in S.hid_pages()}
            _, st = run("hidakaya", hid(pages=empty), D3)
            self.assertFalse(st["list_ok"])
            self.assertEqual(prod("hidakaya", "20261009")["missed_runs"], 0)


class TestSiteAndConfig(unittest.TestCase):
    def make(self, chain, pid, day=D1, regular=False, **over):
        with TempData(regular=regular):
            if chain == "kourakuen":
                run(chain, kou(), day)
            else:
                run(chain, hid(), day)
            p = prod(chain, pid)
        p.update(over)
        return p

    def test_chain_config(self):
        self.assertEqual(g.CHAIN_NAME["kourakuen"], "幸楽苑")
        self.assertEqual(g.CHAIN_NAME["hidakaya"], "日高屋")
        self.assertEqual(g.CHAIN_SOURCE["hidakaya"], "日高屋公式サイト")
        self.assertTrue(g.CHAIN_LIST_TEXT.endswith("・バーガーキング・幸楽苑・日高屋・ゼッテリア"))
        self.assertEqual([c["slug"] for c in g.CHAINS][-3:], ["kourakuen", "hidakaya", "zetteria"])
        for k in ("site_description", "site_tagline"):
            for n in ("幸楽苑", "日高屋"):
                self.assertIn(n, g.CONFIG[k])
        about = g.CONFIG["about"]
        self.assertLess(sum(len(p) for p in about), 700)
        self.assertTrue(any("幸楽苑" in p and "日高屋" in p for p in about))
        for c in ("kourakuen", "hidakaya"):
            self.assertIn(f"--{c}:", g.CSS)
            self.assertIn(f'data-chain="{c}"', g.CSS)
        colors = [c["color"].lower() for c in g.CHAINS]
        self.assertEqual(len(colors), len(set(colors)))
        h = g.page_shell("x", "d", "<p>b</p>", "https://fastfood.netaful.jp/")
        self.assertNotIn("https://www.kourakuen.co.jp/", h)
        self.assertNotIn("https://hidakaya.hiday.co.jp/", h)
        self.assertIn("バーガーキング、幸楽苑、日高屋、ゼッテリア各社", h)

    def test_config_limits_and_intervals(self):
        for c in ("kourakuen", "hidakaya"):
            self.assertGreaterEqual(crawl.CONFIG["request_interval_seconds"][c], 3)
            self.assertLessEqual(crawl.CONFIG["max_requests_per_run"][c], 45)
        self.assertEqual(nr.TAGS["hidakaya"], 842)
        self.assertNotIn("kourakuen", nr.TAGS)  # 幸楽苑のタグはネタフルに無い(2026-11-18確認)
        self.assertEqual(sorted(crawl.CRAWLERS)[:0], [])
        self.assertIn("kourakuen", crawl.CRAWLERS)
        self.assertIn("hidakaya", crawl.CRAWLERS)

    def test_title_filter_for_hidakaya(self):
        self.assertTrue(nr.title_passes_filter("hidakaya", "【日高屋】「ネギタワー」"))
        self.assertFalse(nr.title_passes_filter("hidakaya", "【ラーメン】他店の話"))

    def test_limited_item_page(self):
        p = self.make("kourakuen", "102")
        h = g.render_item_page(p)
        self.assertIn("幸楽苑", h)
        self.assertIn("639kcal", h)
        self.assertIn("アレルゲン（含む）：</strong>卵、小麦", h)
        self.assertIn("公式のお知らせに「期間限定」と載ったメニューです", h)
        self.assertIn("発売日は公式のお知らせ（2026-10-28）によります", h)
        self.assertIn("一部店舗では販売していません", h)
        self.assertIn("upload/pdf/allergy.pdf", h)
        self.assertIn("2026-11-04", h)
        self.assertIn("info.html?id=102", h)
        self.assertEqual(g.seo_title(p), "幸楽苑「合成担担麺」のカロリー・価格・栄養成分")
        self.assertIn("幸楽苑の限定メニュー「合成担担麺」", g.seo_description(p))

    def test_hidakaya_page_with_soup_note(self):
        h = g.render_item_page(self.make("hidakaya", "20261009"))
        self.assertIn("日高屋", h)
        self.assertIn("636kcal", h)
        self.assertIn("公式の注記：スープを含む", h)
        self.assertIn("店舗により価格・取り扱いが異なる場合があります", h)
        self.assertIn("hits/menu/allergy.html", h)
        self.assertIn("大豆、卵、鶏肉、小麦粉", h)

    def test_regular_item_page_and_card(self):
        p = self.make("hidakaya", "808-1", regular=True)
        h = g.render_item_page(p)
        self.assertIn("定番メニュー", h)
        self.assertNotIn("公式のお知らせに", h)
        self.assertIn('data-chain="hidakaya"', g.render_card(p))

    def test_not_limited_wording_for_non_period_limited(self):
        p = self.make("kourakuen", "101")
        self.assertEqual(p["limited_kind"], "新メニュー")
        self.assertIn("発売・登場が告知されたメニュー", g.render_item_page(p))
        self.assertIn("幸楽苑のメニュー「合成トマトらーめん」", g.seo_description(p))  # 「期間限定」でないものは限定と呼ばない

    def test_chain_page_description_and_icons(self):
        c = [x for x in g.CHAINS if x["slug"] == "hidakaya"][0]
        self.assertIn("期間限定・New表示・新メニュー", g.chain_seo_description(c, [self.make("hidakaya", "20261009")]))
        self.assertEqual(g.category_icon({"chain": "hidakaya", "category": "ラーメン", "name": "x"})[0], "noodles")
        self.assertEqual(g.category_icon({"chain": "kourakuen", "category": "もう一品", "name": "餃子"})[0], "sozai")

    def test_notification_labels(self):
        import notify
        self.assertEqual(notify.CHAIN_LABEL["kourakuen"], "幸楽苑")
        self.assertEqual(notify.CHAIN_LABEL["hidakaya"], "日高屋")


@helpers.needs_fixtures
class TestRealSnapshots(unittest.TestCase):
    """手元に実物のスナップショットがあるときだけ走る(2026-11-18に取得)。"""

    def test_kourakuen_snapshot(self):
        m = P.parse_kourakuen_menu(fx("kourakuen/grandmenu.html"))
        self.assertEqual(len(m), 102)
        self.assertEqual(sum(1 for e in m if not P.kourakuen_excluded(e)), 54)
        d = P.parse_kourakuen_detail(fx("kourakuen/info_15145.html"))
        self.assertEqual((d["name"], d["price"], d["nutrition"]["kcal"]), ("中華そば", 490, 639.0))
        n = P.parse_kourakuen_news(fx("kourakuen/news.html"))
        hit = P.match_ramen_launch("メガたんめん", n, D1)
        self.assertEqual((hit["launch_date"], hit["limited"]), ("2026-11-04", True))

    def test_hidakaya_snapshot(self):
        items = {}
        for path, cat in P.HIDAKAYA_LISTS:
            k = "rec" if cat is None else {"ラーメン": 1, "定食": 3, "単品": 4, "おつまみ": 5}[cat]
            for e in P.parse_hidakaya_list(fx(f"hidakaya/list_{k}.html"), cat):
                items.setdefault(e["product_id"], e)
        self.assertEqual(len(items), 85)
        self.assertIn("20261009", items)
        self.assertEqual(sum(1 for e in items.values() if not P.hidakaya_excluded(e)), 83)
        d = P.parse_hidakaya_detail(fx("hidakaya/detail_20261009.html"))
        self.assertEqual((d["nutrition"]["kcal"], d["nutrition_note"]), (896.0, "スープを含む"))
        n = P.parse_hidakaya_news(fx("hidakaya/news.json"))
        self.assertEqual(P.match_ramen_launch("濃厚豚骨醤油ラーメン", n, D1)["launch_date"], "2026-10-09")


if __name__ == "__main__":
    unittest.main()
