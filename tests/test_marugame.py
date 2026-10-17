"""丸亀製麺(jp.marugame.com。__NEXT_DATA__ のJSON): 一覧・詳細のパーサ(価格のサイズ別/no_size、期間限定の判定、
栄養のサイズ別、アレルゲンの対応)、baseline、新規の記録、販売状況の追跡(2日連続の別日欠落で終了)、
一覧が壊れたときの挙動、歯止め(burst guard)、詳細ページの403(ソフト404)の扱い、サイト表示、設定のテスト。
フィクスチャは tests/synthetic_marugame.py の合成データ(公式ページのコピーはリポジトリに入れない)。
手元に実物のスナップショット(tests/fixtures/marugame/)があるときだけ、実物でも確かめる補助テストを走らせる。"""

from __future__ import annotations

import json
import unittest

import helpers  # noqa: F401
from helpers import TempData, fx
import crawl
import fetch_netaful_reviews as nr
import generate_site as g
import parsers as P
import synthetic_marugame as S
from fetch import BlockedError, NotFoundError

BASE = "https://jp.marugame.com"
D1, D2, D3, D4 = "2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20"


class FakeMaru:
    """パスからページを返す偽の取得器。詳細ページは指定が無ければ合成で作る。呼ばれたURLを記録する。"""

    def __init__(self, pages: dict, details: dict | None = None, fail: set[str] = frozenset(),
                 forbidden: set[str] = frozenset(), missing: set[str] = frozenset()):
        self.pages = dict(pages)
        self.details = details or {}
        self.fail, self.forbidden, self.missing = set(fail), set(forbidden), set(missing)
        self.calls: list[str] = []

    def __call__(self, chain: str, url: str) -> str:
        self.calls.append(url)
        assert url.startswith(BASE), url  # ホストはここだけ(www.marugame.co.jp は別会社)
        path = url[len(BASE):]
        if path in self.fail:
            raise OSError("simulated failure")
        if path in self.forbidden:
            raise BlockedError(f"HTTP 403: {url}")  # 存在しないパスは403のソフト404
        if path in self.missing:
            raise NotFoundError(f"HTTP 404: {url}")
        if path == "/robots.txt":
            return S.ROBOTS
        if path in self.pages:
            return self.pages[path]
        if path in self.details:
            return self.details[path]
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "menu":
            return S.detail_page(parts[1], parts[2], parts[2])
        raise KeyError(path)


def run(getter, day, pending=None):
    pending = {} if pending is None else pending
    stats = {}
    crawl.crawl_marugame(getter, pending, stats, day)
    return pending, stats["marugame"]


def prod(pid):
    return crawl.load_product("marugame", pid)


def ids():
    return {p["product_id"] for p in crawl.load_chain_products("marugame")}


class TestListParser(unittest.TestCase):
    def setUp(self):
        self.pages = S.default_pages()

    def test_items_prices_and_ids(self):
        r = P.parse_marugame_list(self.pages["/menu/udon/"])
        self.assertEqual(r["category"], {"id": "udon", "name": "うどん"})
        by = {e["product_id"]: e for e in r["items"]}
        self.assertEqual(set(by), {"udon-genteiudon", "udon-shinteiban", "udon-kake", "udon-kama"})  # 店内とお持ち帰りは1件にまとまる
        e = by["udon-genteiudon"]
        self.assertEqual((e["price"], e["slug"], e["official_url"]), (800, "genteiudon", f"{BASE}/menu/udon/genteiudon/"))
        self.assertEqual([(p["label"], p["price"]) for p in e["prices"]], [("並", 800), ("大", 990), ("得", 1180)])
        self.assertEqual(by["udon-kake"]["price"], 440)  # 代表は並(小・並・大・得の並び)
        self.assertEqual([p["label"] for p in by["udon-kake"]["prices"]], ["小", "並", "大", "得"])

    def test_no_size_and_piece_prices(self):
        by = {e["product_id"]: e for e in P.parse_marugame_list(self.pages["/menu/udon/"])["items"]}
        self.assertEqual((by["udon-shinteiban"]["price"], by["udon-shinteiban"]["prices"][0]["label"]), (900, ""))
        t = P.parse_marugame_list(self.pages["/menu/tempura/"])["items"][0]
        self.assertEqual((t["price"], t["prices"][0]["label"]), (200, "1個"))

    def test_prices_fall_back_to_the_price_dict(self):
        e = S.entry("udon", "x", "合成", {"regular": 500, "large": 700}, with_prices_list=False)
        page = S.list_page("udon", [e])
        it = P.parse_marugame_list(page)["items"][0]
        self.assertEqual([(p["label"], p["price"]) for p in it["prices"]], [("並", 500), ("大", 700)])
        self.assertEqual(it["price"], 500)

    def test_channels(self):
        by = {e["product_id"]: e for e in P.parse_marugame_list(self.pages["/menu/udon/"])["items"]}
        self.assertEqual(by["udon-genteiudon"]["channels"], ["店内", "お持ち帰り"])
        self.assertEqual(by["udon-kama"]["channels"], ["店内"])
        b = P.parse_marugame_list(self.pages["/menu/udonbento/"])["items"][0]
        self.assertEqual((b["channels"], b["takeout_only"]), (["お持ち帰り"], True))

    def test_limited_from_icon_select_attributes_or_limited_flag(self):
        def lim(**kw):
            return P.parse_marugame_list(S.list_page("udon", [S.entry("udon", "x", "合成", {"regular": 500}, **kw)]))["items"][0]
        self.assertTrue(lim(icons=["期間限定（limited_time_item）"])["is_limited"])
        self.assertTrue(lim(attrs={"limited_time_item": True})["is_limited"])
        self.assertTrue(lim(attrs={"limited": "期間限定"})["is_limited"])  # icon_select が空でも立つことがある(すだち)
        self.assertFalse(lim()["is_limited"])
        self.assertEqual(lim(attrs={"limited": "期間限定"})["badges"], ["期間限定"])
        self.assertEqual(lim(icons=["期間限定（limited_time_item）"])["badges"], ["期間限定"])

    def test_new_classic_is_not_limited(self):
        by = {e["product_id"]: e for e in P.parse_marugame_list(self.pages["/menu/udon/"])["items"]}
        e = by["udon-shinteiban"]
        self.assertEqual((e["is_limited"], e["is_new_classic"], e["badges"]), (False, True, ["新定番"]))
        k = P.parse_marugame_list(S.list_page("udon", [S.entry("udon", "k", "合成", {"no_size": 290}, ["小学６年生まで限定（kids_12_and_under）"])]))["items"][0]
        self.assertEqual((k["is_limited"], k["badges"]), (False, ["小学６年生まで限定"]))

    def test_same_slug_in_two_categories_stays_distinct(self):
        a = P.parse_marugame_list(self.pages["/menu/topping/"])["items"][0]
        b = P.parse_marugame_list(self.pages["/menu/gohanmono/"])["items"][0]
        self.assertEqual((a["product_id"], b["product_id"]), ("topping-mentaiko", "gohanmono-mentaiko"))

    def test_navigation_collects_categories_and_strips_newline(self):
        r = P.parse_marugame_list(self.pages["/menu/udon/"])
        self.assertEqual([c["id"] for c in r["nav"]], ["udon", "tempura", "gohanmono", "topping", "udonbento"])
        self.assertEqual(r["nav"][3]["name"], "薬味・トッピング")

    def test_broken_pages_give_no_items(self):
        for bad in ("", "<html><body>メンテナンス中</body></html>",
                    '<script id="__NEXT_DATA__" type="application/json">{broken</script>',
                    S.next_page({"menus": "x"}), S.next_page({}),
                    '<script id="__NEXT_DATA__" type="application/json">[1,2]</script>'):
            self.assertEqual(P.parse_marugame_list(bad)["items"], [], bad[:40])
        self.assertIsNone(P.parse_marugame_detail("<html></html>"))


class TestDetailParser(unittest.TestCase):
    def test_nutrition_variants_are_floats_with_labels_and_sodium(self):
        d = P.parse_marugame_detail(S.detail_page("udon", "x", "合成"))
        v = d["nutrition_variants"]
        self.assertEqual([x["label"] for x in v], ["温・並", "温・大"])
        n = v[0]["nutrition"]
        self.assertEqual((n["kcal"], n["protein_g"], n["fat_g"], n["carbs_g"], n["salt_g"]), (400.0, 10.0, 5.0, 75.0, 3.0))
        self.assertEqual(n["other"], {"ナトリウム": "1200mg"})
        self.assertEqual(P.marugame_pick_primary_nutrition(v)["label"], "温・並")

    def test_primary_nutrition_prefers_regular_size(self):
        items = [{"temperature": "温", "size": "大", "energy": "560", "protein": "14", "fat": "7", "carbohydrate": "105", "saltEquivalent": "4"},
                 {"temperature": "温", "size": "並", "energy": "400", "protein": "10", "fat": "5", "carbohydrate": "75", "saltEquivalent": "3"}]
        v = P.parse_marugame_detail(S.detail_page("udon", "x", "合成", nutrition_items=items))["nutrition_variants"]
        self.assertEqual(P.marugame_pick_primary_nutrition(v)["nutrition"]["kcal"], 400.0)

    def test_single_row_without_size_or_temperature(self):
        items = [{"temperature": "", "size": "", "energy": "143", "protein": "12.4", "fat": "7.6", "carbohydrate": "6.2", "sodium": "148", "saltEquivalent": "0.4"}]
        v = P.parse_marugame_detail(S.detail_page("tempura", "x", "合成", nutrition_items=items))["nutrition_variants"]
        self.assertEqual((len(v), v[0]["label"], v[0]["nutrition"]["kcal"]), (1, "", 143.0))

    def test_missing_or_unreadable_nutrition(self):
        self.assertEqual(P.parse_marugame_detail(S.detail_page("udon", "x", "合成", no_nutrition=True))["nutrition_variants"], [])
        junk = [{"temperature": "", "size": "", "energy": "", "protein": "-", "fat": "", "carbohydrate": ""}]
        self.assertEqual(P.parse_marugame_detail(S.detail_page("udon", "x", "合成", nutrition_items=junk))["nutrition_variants"], [])

    def test_allergens_1_is_used_and_5_is_not(self):
        d = P.parse_marugame_detail(S.detail_page("udon", "x", "合成", allergen_uses=("wheat", "shrimp", "sesame")))
        self.assertEqual(d["allergens"], {"contains": ["小麦", "えび", "ごま"], "partial": []})  # 公式の並び(order)順
        none = P.parse_marugame_detail(S.detail_page("udon", "x", "合成", allergen_uses=()))
        self.assertEqual(none["allergens"], {"contains": [], "partial": []})

    def test_unknown_allergen_value_means_unknown(self):
        for odd in ("3", "", "◆"):
            d = P.parse_marugame_detail(S.detail_page("udon", "x", "合成", odd_value=odd))
            self.assertIsNone(d["allergens"], repr(odd))  # 1と5以外が1つでもあれば出さない
        self.assertIsNotNone(P.parse_marugame_detail(S.detail_page("udon", "x", "合成"))["allergens"])

    def test_allergens_need_the_header_list(self):
        self.assertIsNone(P.marugame_allergens({"wheat": "1"}, []))
        self.assertIsNone(P.marugame_allergens(None, [{"id": "wheat", "name": "小麦", "order": "1"}]))


class TestCrawl(unittest.TestCase):
    def test_first_run_makes_six_list_requests_and_records_only_limited(self):
        with TempData():
            g1 = FakeMaru(S.default_pages())
            _, st = run(g1, D1)
            self.assertEqual(g1.calls[:6], [f"{BASE}{p}" for p in (
                "/robots.txt", "/menu/udon/", "/menu/tempura/", "/menu/gohanmono/", "/menu/topping/", "/menu/udonbento/")])
            self.assertEqual(g1.calls[6:], [f"{BASE}/menu/udon/genteiudon/"])  # 詳細は記録する商品だけ
            self.assertEqual((st["list_requests"], st["detail_requests"], st["new_products"]), (6, 1, 1))
            self.assertEqual(ids(), {"udon-genteiudon"})  # 新定番・定番は記録しない
            self.assertTrue(st["list_ok"])
            self.assertEqual(sorted(crawl.baseline_for("marugame")), sorted([
                "udon-genteiudon", "udon-shinteiban", "udon-kake", "udon-kama", "tempura-ebi", "topping-mentaiko",
                "gohanmono-mentaiko", "udonbento-teiban"]))
            p = prod("udon-genteiudon")
            self.assertEqual((p["chain"], p["price"], p["limited_kind"], p["status"], p["category"]),
                             ("marugame", 800, "期間限定", "on_sale", "うどん"))
            self.assertEqual([(v["label"], v["price"]) for v in p["price_variants"]], [("並", 800), ("大", 990), ("得", 1180)])
            self.assertEqual(p["price_history"], [{"date": D1, "price": 800}])
            self.assertEqual(p["official_url"], f"{BASE}/menu/udon/genteiudon/")
            self.assertEqual(p["description"], "合成の説明文です。")  # 冒頭の1文だけ
            self.assertEqual(p["badges"], ["期間限定"])
            self.assertEqual(p["channels"], ["店内", "お持ち帰り"])
            self.assertIsNone(p["launch_date"])  # 公式に発売日は無い(CMSの更新日時は使わない)
            self.assertEqual(p["launch_date_source"], "not_found")
            self.assertEqual(p["nutrition"]["kcal"], 400.0)
            self.assertEqual([v["label"] for v in p["nutrition_variants"]], ["温・並", "温・大"])
            self.assertEqual(p["allergens"], {"contains": ["小麦"], "partial": []})
            self.assertIsNone(p["image"])
            self.assertNotIn("2026-01-03", json.dumps(p))  # publishedAt などは保存しない
            self.assertEqual(json.loads(crawl.marugame_state_path().read_text())["marugame"]["menu_count"], 8)

    def test_description_falls_back_to_lead(self):
        with TempData():
            pages = S.default_pages()
            pages["/menu/udon/"] = S.list_page("udon", [S.entry("udon", "genteiudon", "合成限定うどん", {"no_size": 900},
                                                                  ["期間限定（limited_time_item）"], description="", lead="キャッチコピー")])
            run(FakeMaru(pages), D1)
            self.assertEqual(prod("udon-genteiudon")["description"], "キャッチコピー")

    def test_second_run_same_day_is_idempotent_and_makes_no_detail_request(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            g2 = FakeMaru(S.default_pages())
            _, st = run(g2, D1)
            self.assertEqual(len(g2.calls), 6)  # 既存商品の詳細は再取得しない
            self.assertEqual(st["new_products"], 0)
            self.assertEqual(prod("udon-genteiudon")["missed_runs"], 0)

    def test_new_item_after_baseline_is_recorded_as_new_listing(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            extra = S.entry("udon", "shinsakuudon", "合成新作うどん", {"regular": 700, "large": 890})
            g2 = FakeMaru(S.default_pages(extra_udon=[extra]))
            _, st = run(g2, D2)
            self.assertEqual(st["new_products"], 1)
            p = prod("udon-shinsakuudon")
            self.assertEqual((p["limited_kind"], p["badges"], p["price"]), ("新規掲載", [], 700))
            self.assertIn(f"{BASE}/menu/udon/shinsakuudon/", g2.calls)
            self.assertNotIn("udon-shinsakuudon", crawl.baseline_for("marugame"))  # baselineは初回のまま

    def test_new_classic_item_added_later_is_still_a_new_listing(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            extra = S.entry("udon", "shin2", "合成新定番2", {"regular": 700}, ["新定番（new_classic）"])
            run(FakeMaru(S.default_pages(extra_udon=[extra])), D2)
            self.assertEqual(prod("udon-shin2")["badges"], ["新定番"])

    def test_recorded_item_keeps_its_record_when_it_becomes_a_new_classic(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            pages = S.default_pages(drop=("genteiudon",))
            pages["/menu/udon/"] = S.list_page("udon", [
                S.entry("udon", "genteiudon", "合成限定うどん", {"regular": 800, "large": 990, "xlarge": 1180}, ["新定番（new_classic）"]),
                S.entry("udon", "kake", "合成かけうどん", {"regular": 440}), S.entry("udon", "kama", "合成釜揚げ", {"regular": 390}),
                S.entry("udon", "shinteiban", "合成新定番うどん", {"no_size": 900}, ["新定番（new_classic）"])])
            run(FakeMaru(pages), D2)
            p = prod("udon-genteiudon")
            self.assertEqual((p["status"], p["limited_kind"], p["badges"]), ("on_sale", "期間限定", ["期間限定", "新定番"]))

    def test_item_that_turns_limited_later_is_upgraded(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            extra = S.entry("udon", "shin3", "合成3", {"regular": 700})
            run(FakeMaru(S.default_pages(extra_udon=[extra])), D2)
            extra2 = S.entry("udon", "shin3", "合成3", {"regular": 700}, ["期間限定（limited_time_item）"])
            run(FakeMaru(S.default_pages(extra_udon=[extra2])), D3)
            self.assertEqual(prod("udon-shin3")["limited_kind"], "期間限定")

    def test_price_change_goes_to_history(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            pages = S.default_pages(drop=("genteiudon",))
            pages["/menu/udon/"] = S.list_page("udon", [S.entry("udon", "genteiudon", "合成限定うどん", {"regular": 850, "large": 1040},
                                                                  ["期間限定（limited_time_item）"])] + [
                S.entry("udon", s, s, {"regular": 440}) for s in ("kake", "kama", "shinteiban")])
            run(FakeMaru(pages), D2)
            p = prod("udon-genteiudon")
            self.assertEqual(p["price"], 850)
            self.assertEqual([h["price"] for h in p["price_history"]], [800, 850])

    def test_same_name_in_two_ids_is_linked(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            e = S.entry("udon", "genteiudon2", "合成限定うどん", {"regular": 800}, ["期間限定（limited_time_item）"])
            run(FakeMaru(S.default_pages(extra_udon=[e])), D2)
            self.assertEqual(prod("udon-genteiudon2")["same_name_as"], ["udon-genteiudon"])

    def test_no_request_to_the_other_company_host(self):
        with TempData():
            g1 = FakeMaru(S.default_pages())
            run(g1, D1)
            self.assertTrue(all(u.startswith("https://jp.marugame.com/") for u in g1.calls))
            self.assertFalse(any("marugame.co.jp" in u for u in g1.calls))


class TestTracking(unittest.TestCase):
    def test_ended_after_two_separate_misses_and_file_is_kept(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            pages = S.default_pages(drop=("genteiudon",))
            run(FakeMaru(pages), D2)
            p = prod("udon-genteiudon")
            self.assertEqual((p["status"], p["missed_runs"]), ("on_sale", 1))
            run(FakeMaru(pages), D2)  # 同じ日の再実行は数えない
            self.assertEqual(prod("udon-genteiudon")["missed_runs"], 1)
            _, st = run(FakeMaru(pages), D3)
            p = prod("udon-genteiudon")
            self.assertEqual((p["status"], p["ended_observed_at"], st["ended"]), ("ended", D3, 1))
            self.assertEqual(p["name"], "合成限定うどん")  # ファイルは消さない
            run(FakeMaru(pages), D4)
            self.assertEqual(prod("udon-genteiudon")["status"], "ended")

    def test_one_missing_category_page_stops_ending(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            pages = S.default_pages(drop=("genteiudon",))
            for d in (D2, D3, D4):
                _, st = run(FakeMaru(pages, fail={"/menu/tempura/"}), d)
                self.assertFalse(st["list_ok"])
            self.assertEqual((prod("udon-genteiudon")["status"], prod("udon-genteiudon")["missed_runs"]), ("on_sale", 0))

    def test_broken_or_empty_list_updates_nothing(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            for bad in ("<html><body>メンテナンス中</body></html>", S.next_page({}),
                        S.list_page("udon", [], []), '<script id="__NEXT_DATA__" type="application/json">{x</script>'):
                pages = S.default_pages()
                pages["/menu/udon/"] = bad
                pend, st = run(FakeMaru(pages), D2)
                self.assertFalse(st["list_ok"], bad[:30])
            for _ in range(3):
                pages = {k: S.next_page({}) for k in S.default_pages()}
                run(FakeMaru(pages), D3)
            for p in crawl.load_chain_products("marugame"):
                self.assertEqual((p["status"], p.get("missed_runs") or 0), ("on_sale", 0), p["product_id"])
            self.assertEqual(len(crawl.baseline_for("marugame")), 8)

    def test_broken_entry_page_falls_back_to_configured_categories_without_ending(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            pages = S.default_pages(drop=("genteiudon",))
            pages["/menu/udon/"] = "<html></html>"
            g2 = FakeMaru(pages)
            _, st = run(g2, D2)
            self.assertFalse(st["list_ok"])
            self.assertEqual(prod("udon-genteiudon")["missed_runs"], 0)

    def test_first_run_with_failed_list_does_not_make_a_baseline(self):
        with TempData():
            _, st = run(FakeMaru(S.default_pages(), fail={"/menu/topping/"}), D1)
            self.assertFalse(st["list_ok"])
            self.assertIsNone(crawl.baseline_for("marugame"))

    def test_extremely_short_list_is_not_trusted(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            few = {k: S.list_page(k.split("/")[2], [], []) for k in S.default_pages()}
            few["/menu/udon/"] = S.list_page("udon", [S.entry("udon", "kake", "合成かけうどん", {"regular": 440})])
            for d in (D2, D3, D4):
                _, st = run(FakeMaru(few), d)
                self.assertFalse(st["list_ok"])
            self.assertEqual(prod("udon-genteiudon")["status"], "on_sale")

    def test_relaunch_starts_a_new_season(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            pages = S.default_pages(drop=("genteiudon",))
            run(FakeMaru(pages), D2)
            run(FakeMaru(pages), D3)
            self.assertEqual(prod("udon-genteiudon")["status"], "ended")
            _, st = run(FakeMaru(S.default_pages()), "2026-12-28")
            p = prod("udon-genteiudon")
            self.assertEqual(p["status"], "on_sale")
            self.assertEqual(p["relaunches"], [{"ended_observed_at": D3, "reappeared_at": "2026-12-28"}])
            self.assertEqual(p["seasons"][0]["observed_start"], D1)
            self.assertEqual(p["seasons"][0]["ended_observed_at"], D3)
            self.assertEqual(st["relaunched"], 1)
            self.assertEqual(p["price_history"][-1], {"date": "2026-12-28", "price": 800, "relaunch": True})


class TestCategoryMove(unittest.TestCase):
    """公式が商品を別カテゴリへ移しても、新規と終了を二重に記録しない(商品IDは「カテゴリ-slug」のまま)。"""

    @staticmethod
    def E(cat, slug):
        return {"product_id": f"{cat}-{slug}", "slug": slug, "category_id": cat}

    @staticmethod
    def rec(pid, **kw):
        return dict({"product_id": pid}, **kw)

    def test_recorded_item_moved_is_renamed_to_the_recorded_id(self):
        entries = {"udon-kake": self.E("udon", "kake"), "tempura-nikuudon": self.E("tempura", "nikuudon")}
        out, moves, bl = crawl.marugame_resolve_moves(entries, [self.rec("udon-nikuudon")], {"udon-kake"})
        self.assertEqual(moves, {"tempura-nikuudon": "udon-nikuudon"})
        self.assertEqual(list(out), ["udon-kake", "udon-nikuudon"])
        self.assertEqual((out["udon-nikuudon"]["category_id"], out["udon-nikuudon"]["moved_from_pid"]),
                         ("tempura", "tempura-nikuudon"))
        self.assertEqual(bl, set())

    def test_baseline_staple_moved_is_not_new(self):
        entries = {"tempura-kake": self.E("tempura", "kake")}
        out, moves, bl = crawl.marugame_resolve_moves(entries, [], {"udon-kake", "udon-kama"})
        self.assertEqual((moves, bl), ({}, {"tempura-kake"}))
        self.assertEqual(list(out), ["tempura-kake"])

    def test_simultaneous_duplicate_slug_is_not_renamed(self):
        # mentaiko がご飯ものとトッピングの両方にある(どちらも今日の一覧にある): 別商品
        entries = {"gohanmono-mentaiko": self.E("gohanmono", "mentaiko"), "topping-mentaiko": self.E("topping", "mentaiko"),
                   "tempura-mentaiko": self.E("tempura", "mentaiko")}
        out, moves, bl = crawl.marugame_resolve_moves(entries, [self.rec("gohanmono-mentaiko")],
                                                      {"gohanmono-mentaiko", "topping-mentaiko"})
        self.assertEqual((moves, bl), ({}, set()))
        self.assertEqual(out, entries)

    def test_recorded_item_listed_at_both_old_and_new_place_is_not_renamed(self):
        entries = {"udon-x": self.E("udon", "x"), "tempura-x": self.E("tempura", "x")}
        out, moves, bl = crawl.marugame_resolve_moves(entries, [self.rec("udon-x")], {"udon-x"})
        self.assertEqual((moves, bl), ({}, set()))
        self.assertEqual(out, entries)

    def test_ambiguous_candidates_are_left_alone(self):
        # 消えた記録済みが同じ slug で2件、または新しく出たのが2件: 1対1でないので付け替えない
        entries = {"tempura-x": self.E("tempura", "x")}
        _, moves, _ = crawl.marugame_resolve_moves(entries, [self.rec("udon-x"), self.rec("topping-x")], set())
        self.assertEqual(moves, {})
        entries = {"tempura-x": self.E("tempura", "x"), "topping-x": self.E("topping", "x")}
        _, moves, bl = crawl.marugame_resolve_moves(entries, [self.rec("udon-x")], {"udon-x"})
        self.assertEqual((moves, bl), ({}, set()))

    def test_slug_is_filled_from_product_id_when_missing(self):
        self.assertEqual(crawl.marugame_slug("udon-nikukaramaze"), "nikukaramaze")
        self.assertEqual(crawl.marugame_slug("udonbento-mentaitororoudonbento"), "mentaitororoudonbento")
        self.assertEqual(crawl.marugame_slug("udon-a", "stored"), "stored")
        self.assertIsNone(crawl.marugame_slug("noprefix"))
        self.assertEqual(crawl.marugame_category_id({"product_id": "udon-a"}), "udon")
        self.assertEqual(crawl.marugame_category_id({"product_id": "udon-a", "category_id": "tempura"}), "tempura")
        # slug を持たない既存データ(旧形式)でも移動を検出できる
        entries = {"tempura-nikukaramaze": self.E("tempura", "nikukaramaze")}
        _, moves, _ = crawl.marugame_resolve_moves(entries, [self.rec("udon-nikukaramaze")], set())
        self.assertEqual(moves, {"tempura-nikukaramaze": "udon-nikukaramaze"})

    def _moved_pages(self, moved_slug):
        pages = S.default_pages(drop=(moved_slug,))
        if moved_slug == "genteiudon":
            moved = S.entry("tempura", "genteiudon", "合成限定うどん", {"regular": 800}, ["期間限定（limited_time_item）"])
        else:
            moved = S.entry("tempura", moved_slug, "合成かけうどん", {"regular": 440})
        tempura = [S.entry("tempura", "ebi", "合成えび天", {"piece": 200}), moved]
        pages["/menu/tempura/"] = S.list_page("tempura", tempura, tempura)
        return pages

    def test_crawl_recorded_item_moved_keeps_id_and_updates_category(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            self.assertEqual(ids(), {"udon-genteiudon"})
            g2 = FakeMaru(self._moved_pages("genteiudon"))
            for day in (D2, D3, D4):
                _, st = run(g2 if day == D2 else FakeMaru(self._moved_pages("genteiudon")), day)
                self.assertEqual((st["new_products"], st["ended"]), (0, 0))
            self.assertEqual(ids(), {"udon-genteiudon"})
            p = prod("udon-genteiudon")
            self.assertEqual((p["status"], p["missed_runs"], p["category_id"], p["slug"]), ("on_sale", 0, "tempura", "genteiudon"))
            self.assertEqual(p["official_url"], f"{BASE}/menu/tempura/genteiudon/")
            self.assertEqual(len(p["category_history"]), 1)
            self.assertEqual((p["category_history"][0]["from_category_id"], p["category_history"][0]["to_category_id"]),
                             ("udon", "tempura"))
            self.assertNotIn(f"{BASE}/menu/tempura/genteiudon/", g2.calls)  # 詳細は取り直さない

    def test_crawl_baseline_staple_moved_is_not_recorded_as_new(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            g2 = FakeMaru(self._moved_pages("kake"))
            _, st = run(g2, D2)
            self.assertEqual(st["new_products"], 0)
            self.assertEqual(ids(), {"udon-genteiudon"})
            self.assertNotIn("tempura-kake", crawl.baseline_for("marugame"))  # 基準線のファイルは書き換えない
            self.assertTrue(st["list_ok"])

    def test_no_move_resolution_when_a_list_page_failed(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            pages = self._moved_pages("genteiudon")
            run(FakeMaru(pages, fail={"/menu/gohanmono/"}), D2)
            self.assertNotIn("category_history", prod("udon-genteiudon"))


class TestBurstGuard(unittest.TestCase):
    def extras(self, n):
        return [S.entry("udon", f"bulk{i}", f"合成大量{i}", {"regular": 500}) for i in range(n)]

    def test_many_new_candidates_are_not_recorded_and_warn(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            pending, st = run(FakeMaru(S.default_pages(extra_udon=self.extras(12))), D2)
            self.assertEqual(st["new_products"], 0)
            self.assertFalse(st["list_ok"])
            self.assertEqual(st["suspected_restructure"]["count"], 12)
            self.assertEqual(pending["marugame"][crawl.BURST_KEY]["count"], 12)
            self.assertEqual(ids(), {"udon-genteiudon"})
            self.assertEqual(prod("udon-genteiudon")["last_seen_at"], D2)  # 既存の最終確認日は更新を続ける

    def test_burst_also_stops_ending(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            pages = S.default_pages(extra_udon=self.extras(12), drop=("genteiudon",))
            for d in (D2, D3):
                run(FakeMaru(pages), d)
            self.assertEqual(prod("udon-genteiudon")["status"], "on_sale")

    def test_within_threshold_is_recorded(self):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            pending, st = run(FakeMaru(S.default_pages(extra_udon=self.extras(3))), D2)
            self.assertEqual(st["new_products"], 3)
            self.assertNotIn(crawl.BURST_KEY, pending["marugame"])

    def test_first_run_is_never_a_burst(self):
        with TempData():
            _, st = run(FakeMaru(S.default_pages(extra_udon=self.extras(40))), D1)
            self.assertNotIn("suspected_restructure", st)
            self.assertEqual(len(crawl.baseline_for("marugame")), 48)


class TestDetailFailures(unittest.TestCase):
    def two_limited(self):
        pages = S.default_pages()
        pages["/menu/udon/"] = S.list_page("udon", [
            S.entry("udon", f"gen{i}", f"合成限定{i}", {"regular": 800}, ["期間限定（limited_time_item）"]) for i in range(4)])
        return pages

    def test_detail_403_is_unavailable_not_a_block(self):
        with TempData():
            pending, st = run(FakeMaru(self.two_limited(), forbidden={"/menu/udon/gen0/"}), D1)
            self.assertFalse(st["blocked"])
            self.assertEqual((st["failed"], st["new_products"]), (1, 3))
            self.assertIn("udon-gen0", pending["marugame"])
            self.assertTrue(pending["marugame"]["udon-gen0"]["reason"].startswith("unavailable"))
            self.assertTrue(st["list_ok"])
            # 次の回に取れれば記録される(baselineには入っているが期間限定なので対象)
            _, st2 = run(FakeMaru(self.two_limited()), D2)
            self.assertEqual(st2["new_products"], 1)
            self.assertIn("udon-gen0", ids())

    def test_three_detail_403_in_a_row_is_a_block(self):
        with TempData():
            fb = {f"/menu/udon/gen{i}/" for i in range(4)}
            _, st = run(FakeMaru(self.two_limited(), forbidden=fb), D1)
            self.assertTrue(st["blocked"])
            self.assertEqual(st["failed"], 3)
            self.assertIsNone(crawl.baseline_for("marugame"))  # ブロック中は基準線を作らない
            self.assertEqual(ids(), set())

    def test_list_403_is_a_block_and_stops(self):
        with TempData():
            g1 = FakeMaru(S.default_pages(), forbidden={"/menu/tempura/"})
            _, st = run(g1, D1)
            self.assertTrue(st["blocked"])
            self.assertEqual(ids(), set())
            self.assertIsNone(crawl.baseline_for("marugame"))

    def test_detail_404_and_empty_detail_go_to_pending(self):
        with TempData():
            pend, st = run(FakeMaru(self.two_limited(), missing={"/menu/udon/gen0/"}, details={"/menu/udon/gen1/": "<html></html>"}), D1)
            self.assertEqual(pend["marugame"]["udon-gen0"]["reason"], "404")
            self.assertEqual(pend["marugame"]["udon-gen1"]["reason"], "empty_detail")
            self.assertEqual(st["new_products"], 2)

    def test_detail_without_nutrition_still_records(self):
        with TempData():
            run(FakeMaru(S.default_pages(), details={"/menu/udon/genteiudon/": S.detail_page("udon", "genteiudon", "x", no_nutrition=True)}), D1)
            p = prod("udon-genteiudon")
            self.assertIsNone(p["nutrition"])
            self.assertIsNone(p["nutrition_note"])
            self.assertIsNotNone(p["allergens"])

    def test_energy_mismatch_warns_but_keeps_the_official_value(self):
        bad = [{"temperature": "温", "size": "並", "energy": "900", "protein": "10", "fat": "5", "carbohydrate": "75", "saltEquivalent": "3"}]
        with TempData():
            _, st = run(FakeMaru(S.default_pages(), details={"/menu/udon/genteiudon/": S.detail_page("udon", "genteiudon", "x", nutrition_items=bad)}), D1)
            self.assertEqual(st["energy_mismatch"], 1)
            self.assertEqual(prod("udon-genteiudon")["nutrition"]["kcal"], 900.0)

    def test_unknown_allergen_value_is_not_shown(self):
        with TempData():
            run(FakeMaru(S.default_pages(), details={"/menu/udon/genteiudon/": S.detail_page("udon", "genteiudon", "x", odd_value="9")}), D1)
            self.assertIsNone(prod("udon-genteiudon")["allergens"])


class TestMainSingleChain(unittest.TestCase):
    def test_single_chain_argument_is_accepted(self):
        self.assertIn("marugame", crawl.CRAWLERS)
        self.assertIs(crawl.CRAWLERS["marugame"], crawl.crawl_marugame)


class TestSite(unittest.TestCase):
    def make(self, pid="udon-genteiudon", **over):
        with TempData():
            run(FakeMaru(S.default_pages()), D1)
            p = prod(pid)
        p.update(over)
        return p

    def test_title_and_description(self):
        p = self.make()
        self.assertEqual(g.seo_title(p), "丸亀製麺「合成限定うどん」のカロリー・価格・栄養成分")
        d = g.seo_description(p)
        self.assertIn("丸亀製麺の限定メニュー「合成限定うどん」", d)
        self.assertIn("400kcal", d)
        self.assertIn("800円（税込）", d)
        self.assertIn("（記録開始日）", d)
        self.assertEqual(g.seo_title(self.make(nutrition=None)), "丸亀製麺「合成限定うどん」の価格・販売期間")
        self.assertIn("丸亀製麺のメニュー", g.seo_description(self.make(limited_kind="新規掲載")))

    def test_item_page(self):
        h = g.render_item_page(self.make())
        self.assertIn("公式サイトで「期間限定」の表示があるメニューです", h)
        self.assertIn("発売日ではありません", h)
        self.assertIn("2026-10-17（記録開始日）〜 販売中", h)
        self.assertIn("合成の説明文です。", h)
        self.assertIn("出典：丸亀製麺公式サイト", h)
        self.assertIn(f"{BASE}/menu/udon/genteiudon/", h)
        self.assertIn("温・並", h)
        self.assertIn("アレルゲン（含む）：</strong>小麦", h)
        self.assertIn("天ぷら・フライ類は同じフライヤー", h)
        self.assertIn("1180円（税込）", h)
        self.assertIn("推定値", h)
        self.assertNotIn("microcms-assets", h)  # 公式の画像は載せない

    def test_item_page_without_allergens_and_takeout_only(self):
        h = g.render_item_page(self.make(allergens=None, takeout_only=True, channels=["お持ち帰り"], badges=["期間限定", "新定番"]))
        self.assertNotIn("天ぷら・フライ類は同じフライヤー", h)
        self.assertIn("お持ち帰り専用のメニューです", h)
        self.assertIn("「新定番」の表示があります", h)

    def test_shop_only_note_and_season_note(self):
        h = g.render_item_page(self.make(channels=["店内"], seasons=[{"launch_date": None, "observed_start": "2026-01-05", "ended_observed_at": "2026-03-01"}]))
        self.assertIn("店内メニューにだけ載っています", h)
        self.assertIn("昨季の販売：2026-01-05（記録開始日）〜2026-03-01", h)

    def test_chain_config(self):
        self.assertEqual(g.CHAIN_NAME["marugame"], "丸亀製麺")
        self.assertEqual(g.CHAIN_SOURCE["marugame"], "丸亀製麺公式サイト")
        self.assertTrue(g.CHAIN_LIST_TEXT.endswith("・かつや・からやま・丸亀製麺"))
        self.assertEqual(len(g.CHAINS), 9)
        for k in ("site_description", "site_tagline"):
            self.assertIn("丸亀製麺", g.CONFIG[k])
        about = g.CONFIG["about"]
        self.assertEqual(len(about), 4)
        self.assertLess(sum(len(p) for p in about), 700)
        self.assertTrue(any("丸亀製麺" in p for p in about))
        self.assertIn("--marugame:", g.CSS)
        self.assertIn('data-chain="marugame"', g.CSS)
        colors = [c["color"].lower() for c in g.CHAINS]
        self.assertEqual(len(colors), len(set(colors)))
        shell = g.page_shell("x", "d", "<p>b</p>", "https://fastfood.netaful.jp/")
        self.assertIn("https://jp.marugame.com/", shell)
        self.assertNotIn("marugame.co.jp", shell)

    def test_card_link_is_root_absolute(self):
        p = self.make()
        self.assertIn('href="/items/marugame-udon-genteiudon.html"', g.render_card(p))

    def test_notification_labels(self):
        import notify
        self.assertEqual(notify.CHAIN_LABEL["marugame"], "丸亀製麺")

    def test_config_limits(self):
        self.assertEqual(crawl.MAX_REQUESTS["marugame"], 30)
        self.assertGreaterEqual(crawl.INTERVALS["marugame"], 3)
        self.assertEqual(crawl.CONFIG["marugame_categories"], ["udon", "tempura", "gohanmono", "topping", "udonbento"])


class TestNetaful(unittest.TestCase):
    def test_tag_and_title_filter(self):
        self.assertEqual(nr.TAGS["marugame"], 890)
        self.assertTrue(nr.title_passes_filter("marugame", "【丸亀製麺】「丸亀シェイクうどん」を食べた"))
        self.assertFalse(nr.title_passes_filter("marugame", "「うどんチェーンランキング」2位ははなまるうどん"))
        self.assertIn("marugame", nr.TITLE_ALWAYS)
        self.assertEqual(nr.SEARCH_TERMS["marugame"], "丸亀製麺")


@helpers.needs_fixtures
class TestRealSnapshot(unittest.TestCase):
    """手元に実物のスナップショット(tests/fixtures/marugame/。コミットされない)があるときだけ走る補助テスト。"""

    def test_udon_list(self):
        r = P.parse_marugame_list(fx("marugame/list_udon.html"))
        self.assertEqual(len(r["items"]), 20)
        by = {e["product_id"]: e for e in r["items"]}
        self.assertTrue(by["udon-nikukaramaze"]["is_limited"])  # attributes.limited_time_item は false のまま icon_select だけが立つ
        self.assertEqual(by["udon-butatsukejiru"]["price"], 990)
        self.assertEqual([c["id"] for c in r["nav"]], ["udon", "tempura", "gohanmono", "topping", "udonbento"])

    def test_other_categories(self):
        counts = {"gohanmono": 8, "tempura": 10, "topping": 14, "udonbento": 5}
        for cat, n in counts.items():
            self.assertEqual(len(P.parse_marugame_list(fx(f"marugame/list_{cat}.html"))["items"]), n, cat)
        top = {e["product_id"]: e for e in P.parse_marugame_list(fx("marugame/list_topping.html"))["items"]}
        self.assertTrue(top["topping-sudachi"]["is_limited"])  # icon_select は空で attributes.limited だけ

    def test_detail(self):
        d = P.parse_marugame_detail(fx("marugame/detail_udon-nikukaramaze.html"))
        self.assertEqual([v["label"] for v in d["nutrition_variants"]], ["温・並", "温・大", "温・得"])
        self.assertEqual(d["nutrition_variants"][0]["nutrition"]["kcal"], 672.0)
        self.assertEqual(d["allergens"]["contains"], ["小麦", "卵", "えび", "さば", "大豆", "鶏肉", "豚肉", "ごま"])
        k = P.parse_marugame_detail(fx("marugame/detail_tempura-kashiwa.html"))
        self.assertEqual((len(k["nutrition_variants"]), k["nutrition_variants"][0]["label"]), (1, ""))


if __name__ == "__main__":
    unittest.main()
