"""定番メニューの記録と価格の変遷: 定番の作成(除外・1回あたりの上限・歯止めに掛からない・通知に数えない)、
価格の履歴(変更で追記・取れなかった回は書かない)、サイズ別価格の取り直しと履歴、
サイトの表示(トップに混ぜない・チェーン別の定番の節・商品ページ・価格改定の記録)、通知の数え方のテスト。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from helpers import TempData
import crawl
import generate_site as g
import notify
import test_arcland as TA
import test_crawl as TC
import test_marugame as TMa
import test_matsuya as TM
import test_sukiya as TS
import test_yoshinoya as TY
from test_generate_site import item

D1, D2, D3, D4 = "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"


def mcd_cards(n_regular: int, limited: int = 1, extra: list[str] | None = None) -> list[str]:
    cards = [TC.card(f"L{i}", f"限定バーガー{i}", 500, limited=True, started="2026-11-01") for i in range(limited)]
    cards += [TC.card(f"R{i:02d}", f"定番バーガー{i:02d}", 300 + i) for i in range(n_regular)]
    return cards + (extra or [])


class TestRegularCreation(unittest.TestCase):
    """マクドナルド(詳細ページが要る→1回あたりの上限がある)の合成サイトで調べる。"""

    def test_regular_created_with_kind_and_first_price(self):
        with TempData(regular=True):
            g_ = TC.SynthMcd(mcd_cards(3))
            _, st = TC.run_mcd(g_, D1)
            self.assertEqual((st["new_products"], st["regular_created"]), (1, 3))
            p = crawl.load_product("mcdonalds", "R01")
            self.assertEqual(p["limited_kind"], "定番")
            self.assertIs(p["is_regular"], True)
            self.assertIsNone(p["launch_date"])
            self.assertTrue(p["first_seen_at"].startswith(("20",)))
            self.assertEqual(p["price_history"][0]["price"], p["price"])
            self.assertEqual(len(p["price_history"]), 1)
            lim = crawl.load_product("mcdonalds", "L0")
            self.assertEqual(lim["limited_kind"], "期間限定")
            self.assertNotIn("is_regular", lim)

    def test_excluded_things_are_not_recorded(self):
        # セット商品は限定でも定番でも記録しない
        with TempData(regular=True):
            g_ = TC.SynthMcd(mcd_cards(2, extra=[TC.card("S1", "ビッグマック セット", 800)]))
            TC.run_mcd(g_, D1)
            names = {p["name"] for p in crawl.load_chain_products("mcdonalds")}
            self.assertFalse(any("セット" in n for n in names))
            self.assertEqual(len(names), 3)

    def test_switch_off_records_no_regular(self):
        with TempData(regular=False):
            _, st = TC.run_mcd(TC.SynthMcd(mcd_cards(5)), D1)
            self.assertEqual((st["new_products"], st["regular_created"]), (1, 0))
            self.assertEqual(len(crawl.load_chain_products("mcdonalds")), 1)

    def test_backfill_limit_per_run_and_carry_over(self):
        with TempData(regular=True, regular_per_run=4):
            g1 = TC.SynthMcd(mcd_cards(10))
            _, st = TC.run_mcd(g1, D1)
            self.assertEqual(st["regular_created"], 4)
            self.assertEqual(st["regular_deferred"], 6)
            details = [u for u in g1.calls if "/products/" in u]
            self.assertEqual(len(details), 1 + 4)  # 限定1件 + 定番4件
            # 翌日以降に残りを作る(上限ずつ)
            _, st2 = TC.run_mcd(TC.SynthMcd(mcd_cards(10)), D2)
            self.assertEqual((st2["new_products"], st2["regular_created"]), (0, 4))
            _, st3 = TC.run_mcd(TC.SynthMcd(mcd_cards(10)), D3)
            self.assertEqual(st3["regular_created"], 2)
            self.assertEqual(st3["regular_deferred"], 0)
            self.assertEqual(len(crawl.load_chain_products("mcdonalds")), 11)

    def test_regular_backlog_does_not_trigger_burst_filter(self):
        # 定番は baseline に居る項目なので「新規候補」に数えない。上限が小さくても保留(歯止め)にならない
        with TempData(regular=True, regular_per_run=2):
            pend, stats = {}, {}
            for day in (D1, D2, D3):
                stats = {}
                crawl.crawl_mcdonalds(TC.SynthMcd(mcd_cards(40)), pend, stats, day)
                self.assertNotIn("suspected_restructure", stats["mcdonalds"])
                self.assertTrue(stats["mcdonalds"]["list_ok"])
            self.assertEqual(len(crawl.load_chain_products("mcdonalds")), 1 + 6)
            # 本当に新しい項目(baselineに無い)は、定番ではなく新メニューとして記録される
            _, st = TC.run_mcd(TC.SynthMcd(mcd_cards(40, extra=[TC.card("N1", "新作バーガー", 450)])), D4)
            self.assertEqual(st["new_products"], 1)
            self.assertEqual(crawl.load_product("mcdonalds", "N1")["limited_kind"], "新メニュー")

    def test_burst_still_works_for_real_restructure(self):
        with TempData(regular=True):
            TC.run_mcd(TC.SynthMcd(mcd_cards(12)), D1)
            # 商品IDが一斉に付け替わった(baselineにも記録にも無いID)→ 歯止め
            renamed = [TC.card(f"X{i:02d}", f"定番バーガー{i:02d}", 300 + i) for i in range(12)]
            _, st = TC.run_mcd(TC.SynthMcd(renamed + [TC.card("L0", "限定バーガー0", 500, limited=True)]), D2)
            self.assertIn("suspected_restructure", st)
            self.assertEqual(st["regular_created"], 0)

    def test_regular_that_leaves_the_list_ends_after_two_days(self):
        with TempData(regular=True):
            TC.run_mcd(TC.SynthMcd(mcd_cards(3)), D1)
            fewer = mcd_cards(2)
            TC.run_mcd(TC.SynthMcd(fewer), D2)
            self.assertEqual(crawl.load_product("mcdonalds", "R02")["status"], "on_sale")
            _, st = TC.run_mcd(TC.SynthMcd(fewer), D3)
            p = crawl.load_product("mcdonalds", "R02")
            self.assertEqual((p["status"], p["ended_observed_at"]), ("ended", D3))
            self.assertEqual(st["ended"], 1)
            self.assertIs(p["is_regular"], True)

    def test_regular_becoming_limited_is_upgraded(self):
        with TempData(regular=True):
            TC.run_mcd(TC.SynthMcd(mcd_cards(2)), D1)
            cards = [TC.card("L0", "限定バーガー0", 500, limited=True, started="2026-11-01"),
                     TC.card("R00", "定番バーガー00", 300, limited=True, started="2026-11-10"),
                     TC.card("R01", "定番バーガー01", 301)]
            TC.run_mcd(TC.SynthMcd(cards), D2)
            p = crawl.load_product("mcdonalds", "R00")
            self.assertEqual(p["limited_kind"], "期間限定")
            self.assertIs(p["is_regular"], False)
            self.assertIs(crawl.load_product("mcdonalds", "R01")["is_regular"], True)


class TestPriceHistory(unittest.TestCase):
    def test_price_change_appends_history_and_counts(self):
        with TempData(regular=True):
            TC.run_mcd(TC.SynthMcd(mcd_cards(3, limited=0)), D1)
            cards = mcd_cards(3, limited=0)
            cards[1] = TC.card("R01", "定番バーガー01", 350)
            _, st = TC.run_mcd(TC.SynthMcd(cards), D2)
            self.assertEqual(st["price_changes"], 1)
            p = crawl.load_product("mcdonalds", "R01")
            self.assertEqual([h["price"] for h in p["price_history"]], [301, 350])
            self.assertEqual(p["price"], 350)
            # 同じ価格の再巡回では増えない
            _, st2 = TC.run_mcd(TC.SynthMcd(cards), D3)
            self.assertEqual(st2["price_changes"], 0)
            self.assertEqual(len(crawl.load_product("mcdonalds", "R01")["price_history"]), 2)

    def test_none_price_writes_nothing(self):
        p = {"price": 300, "price_history": [{"date": D1, "price": 300}]}
        st = {}
        self.assertFalse(crawl.record_price(p, None, D2, st))
        self.assertEqual(p["price_history"], [{"date": D1, "price": 300}])
        self.assertEqual(st.get("price_changes", 0), 0)
        # 初回(履歴が空)の記録は変更に数えない
        q = {"price_history": []}
        self.assertTrue(crawl.record_price(q, 100, D1, st))
        self.assertEqual(st.get("price_changes", 0), 0)
        self.assertTrue(crawl.record_price(q, 120, D2, st))
        self.assertEqual(st["price_changes"], 1)

    def test_no_price_in_list_keeps_history(self):
        with TempData(regular=True):
            TC.run_mcd(TC.SynthMcd(mcd_cards(2, limited=0)), D1)
            nop = TC.card("R00", "定番バーガー00", 300).replace("300~", "")
            _, st = TC.run_mcd(TC.SynthMcd([nop, mcd_cards(2, limited=0)[1]]), D2)
            p = crawl.load_product("mcdonalds", "R00")
            self.assertEqual(len(p["price_history"]), 1)
            self.assertEqual(st["price_changes"], 0)


class TestVariantHistory(unittest.TestCase):
    V1 = [{"label": "並盛", "price": 400}, {"label": "大盛", "price": 500}]
    V2 = [{"label": "並盛", "price": 420}, {"label": "大盛", "price": 520}]

    def test_record_variants_seeds_old_value_then_appends_change(self):
        p = {"first_seen_at": "2026-10-01T07:00:00+09:00", "price_variants": self.V1}
        st = {}
        self.assertFalse(crawl.record_variants(p, self.V1, D1, st))  # 変わっていない
        self.assertEqual(p["price_variants_history"], [{"date": "2026-10-01", "variants": self.V1}])
        self.assertTrue(crawl.record_variants(p, self.V2, D2, st))
        self.assertEqual([h["date"] for h in p["price_variants_history"]], ["2026-10-01", D2])
        self.assertEqual(p["price_variants"], self.V2)
        self.assertEqual(st["price_changes"], 1)

    def test_record_variants_ignores_missing_or_single(self):
        p = {"price_variants": self.V1, "price_variants_history": [{"date": D1, "variants": self.V1}]}
        for bad in (None, [], [{"label": "", "price": 100}], [{"label": "並盛", "price": None}, {"label": "大盛", "price": 5}]):
            self.assertFalse(crawl.record_variants(p, bad, D2, {}))
        self.assertEqual(len(p["price_variants_history"]), 1)

    def test_due_for_recheck_orders_oldest_first_and_limits(self):
        with TempData(recheck_per_run=2):
            ps = [{"product_id": str(i), "status": "on_sale", "price_variants": self.V1,
                   "price_checked_at": f"2026-11-0{i}"} for i in range(1, 6)]
            ps.append({"product_id": "x", "status": "on_sale", "price_variants": None})
            ps.append({"product_id": "e", "status": "ended", "price_variants": self.V1})
            entries = {p["product_id"]: {} for p in ps}
            due = crawl.due_for_recheck(ps, entries, True, D4)
            self.assertEqual([p["product_id"] for p in due], ["1", "2"])
            # 今日すでに確認した商品・一覧から消えた商品は対象外
            ps[0]["price_checked_at"] = D4
            del entries["2"]
            self.assertEqual([p["product_id"] for p in crawl.due_for_recheck(ps, entries, True, D4)], ["3", "4"])
            # すき家のように価格が詳細にしか無いチェーンは、サイズ別の価格が無い商品も対象
            self.assertIn("x", [p["product_id"] for p in crawl.due_for_recheck(
                [ps[5]], {"x": {}}, False, D4)])

    def test_yoshinoya_recheck_updates_variants_history_only_on_change(self):
        path = "/menu/yoshinoyanodon/tsukimi_gyutoji_don/"
        changed = helpers.fx("yoshinoya/detail_tsukimi_gyutoji_don.html")
        changed = changed.replace('taxin">699<', 'taxin">729<').replace('taxin">875<', 'taxin">905<')
        with TempData(recheck_per_run=3):
            TY.run_y(TY.getter(), "2026-10-11")
            pid = "yoshinoyanodon-tsukimi_gyutoji_don"
            p = crawl.load_product("yoshinoya", pid)
            self.assertEqual(len(p["price_variants_history"]), 1)
            self.assertEqual(p["price_checked_at"], "2026-10-11")
            # 変わっていない詳細を取り直しても、履歴は増えず、確認日だけが進む
            g2 = TY.getter()
            _, st2 = TY.run_y(g2, "2026-10-12")
            self.assertEqual(sum(1 for u in g2.calls if u.endswith(path)), 1)
            p = crawl.load_product("yoshinoya", pid)
            self.assertEqual((len(p["price_variants_history"]), p["price_checked_at"]), (1, "2026-10-12"))
            self.assertEqual(st2["price_changes"], 0)
            # 変わった詳細: 履歴に追記し、変更を1件数える。一覧の価格(基本)には触らない
            _, st3 = TY.run_y(TY.getter(overrides={path: changed}), "2026-10-13")
            p = crawl.load_product("yoshinoya", pid)
            self.assertEqual(len(p["price_variants_history"]), 2)
            self.assertEqual(p["price_variants_history"][-1]["variants"][0], {"label": "並盛", "price": 729})
            self.assertEqual(p["price_variants"][0]["price"], 729)
            self.assertEqual(st3["price_changes"], 1)
            self.assertEqual(len(p["price_history"]), 1)

    def test_recheck_never_exceeds_limit(self):
        with TempData(recheck_per_run=0):
            TY.run_y(TY.getter(), "2026-10-11")
            g2 = TY.getter()
            TY.run_y(g2, "2026-10-12")
            self.assertEqual(sum(1 for u in g2.calls if "tsukimi_gyutoji_don/" in u and "/menu/yoshinoyanodon/" in u
                                 and u.count("/") > 5), 0)


class TestMos(unittest.TestCase):
    def test_mos_creates_all_regulars_at_once_from_the_list(self):
        def cat(items):
            return json.dumps([{"id": i, "name": n, "price": pr, "tags": "", "kcal": "300", "explain": "説明",
                                "nutrition": [], "allergy": [], "produce": []} for i, n, pr in items], ensure_ascii=False)
        regs = [("900001", "定番モス1", 400), ("900002", "定番モス2", 410)]
        navi = json.dumps([{"id": 1, "name": "バーガー"}, {"id": 26, "name": "限定"}])

        def getter(regs_):
            g_ = helpers.FakeGetter(overrides={"/data/menu/mst_navi.json": navi,
                                               "/data/menu/menu_category/1.json": cat(regs_)})
            return g_

        with TempData(regular=True, regular_per_run=1):
            pend, stats = {}, {}
            crawl.crawl_mos(getter(regs), pend, stats, D1)
            st = stats["mos"]
            self.assertEqual(st["regular_created"], 2)  # 一覧だけで作れるので1回あたりの上限は掛からない
            self.assertEqual(crawl.load_product("mos", "900001")["limited_kind"], "定番")
            self.assertEqual(st["new_products"], 13)  # 限定カテゴリ(26)のフィクスチャ
            # 価格の変更が履歴に残る
            stats2 = {}
            crawl.crawl_mos(getter([("900001", "定番モス1", 430), regs[1]]), pend, stats2, D2)
            self.assertEqual(stats2["mos"]["price_changes"], 1)
            self.assertEqual([h["price"] for h in crawl.load_product("mos", "900001")["price_history"]], [400, 430])


class TestMatsuyaAndSukiya(unittest.TestCase):
    def test_matsuya_regular_backfill_excludes_topping_and_skips_news(self):
        with TempData(regular=True, regular_per_run=5):
            g_ = TM.getter()
            _, st = TM.run_matsuya(g_, "2026-10-11")
            self.assertEqual(st["regular_created"], 5)
            self.assertGreater(st["regular_deferred"], 0)
            regs = [p for p in crawl.load_chain_products("matsuya") if p.get("is_regular")]
            self.assertEqual(len(regs), 5)
            for p in regs:
                self.assertEqual(p["limited_kind"], "定番")
                self.assertIsNone(p["launch_date"])
                self.assertIsNone(p["news_url"])
                self.assertNotEqual(p["source_list"], ["topping"])
            # 2日目に残りを5件ずつ。トッピング・セット類(optionと topping カテゴリだけの項目)は作らない
            TM.run_matsuya(TM.getter(), "2026-10-12")
            for p in crawl.load_chain_products("matsuya"):
                self.assertFalse(p["source_list"] and all(c == "topping" for c in p["source_list"]), p["product_id"])

    def test_sukiya_regular_has_price_and_is_rechecked_without_news(self):
        with TempData(regular=True, regular_per_run=3, recheck_per_run=2):
            _, st = TS.run_s(TS.getter(), "2026-10-11")
            self.assertEqual(st["regular_created"], 3)
            regs = [p for p in crawl.load_chain_products("sukiya") if p.get("is_regular")]
            for p in regs:
                self.assertIsNotNone(p["price"])
                self.assertEqual(len(p["price_history"]), 1)
                self.assertIsNone(p["launch_date"])
            _, st2 = TS.run_s(TS.getter(), "2026-10-12")
            self.assertEqual(st2["regular_created"], 3)
            self.assertEqual(st2["new_products"], 0)
            # 定番は発売日の照合をしない
            self.assertTrue(all(p["launch_date"] is None for p in crawl.load_chain_products("sukiya") if p.get("is_regular")))


class TestArclandAndMarugame(unittest.TestCase):
    def test_arcland_creates_all_regulars_at_once(self):
        with TempData(regular=True, regular_per_run=1):  # 上限は一覧だけで作れるチェーンには掛からない
            g_ = TA.kat()
            _, st = TA.run("katsuya", g_, "2026-10-11")
            self.assertEqual(st["regular_created"], 5)
            self.assertEqual(st["regular_deferred"], 0)
            self.assertEqual(len(g_.calls), 3)  # 追加のリクエストは無い
            _, st2 = TA.run("katsuya", TA.kat(), "2026-10-12")
            self.assertEqual((st2["new_products"], st2["regular_created"]), (0, 0))

    def test_marugame_regular_skips_topping(self):
        with TempData(regular=True, regular_per_run=50):
            g_ = TMa.FakeMaru(TMa.S.default_pages())
            _, st = TMa.run(g_, "2026-10-17")
            self.assertEqual(st["regular_created"] + st["new_products"], len(TMa.ids()))
            for p in TMa.crawl.load_chain_products("marugame"):
                if p.get("is_regular"):
                    self.assertNotEqual(p.get("category_id"), "topping")
                    self.assertFalse(p["product_id"].startswith("topping-"))


class TestSite(unittest.TestCase):
    def setUp(self):
        self.lim = item(product_id="1", name="限定A")
        self.reg = item(product_id="2", name="定番ポテト", limited_kind="定番", is_regular=True, launch_date=None,
                        category="サイド", nutrition=None, description=None, planned_end_text=None,
                        first_seen_at="2026-11-09T07:00:00+09:00",
                        price_history=[{"date": "2026-11-09", "price": 270}], price=270)

    def test_regular_item_page_shows_record_start_and_single_history(self):
        h = g.render_item_page(self.reg, [self.reg])
        self.assertIn(">定番<", h)
        self.assertIn("記録開始 2026-11-09〜 販売中", h)
        self.assertIn("2026-11-09 時点 270円（税込）", h)
        self.assertIn("記録開始より前の価格は分かりません", h)
        self.assertIn("価格の推移", g.seo_title(self.reg))
        self.assertNotIn("限定メニュー", g.seo_description(self.reg))

    def test_limited_item_with_one_history_row_is_unchanged(self):
        self.assertNotIn("価格の履歴", g.render_item_page(self.lim, [self.lim]))

    def test_two_row_history_and_variants_history(self):
        reg = dict(self.reg, price=300, price_history=[{"date": "2026-11-09", "price": 270}, {"date": "2026-12-01", "price": 300}],
                   price_variants=[{"label": "並盛", "price": 300}, {"label": "大盛", "price": 400}],
                   price_variants_history=[
                       {"date": "2026-11-09", "variants": [{"label": "並盛", "price": 270}, {"label": "大盛", "price": 370}]},
                       {"date": "2026-12-01", "variants": [{"label": "並盛", "price": 300}, {"label": "大盛", "price": 400}]}])
        h = g.render_item_page(reg, [reg])
        self.assertIn("2026-12-01〜", h)
        self.assertIn("サイズ別の価格の履歴", h)
        self.assertIn("370円", h)

    def test_top_rss_archive_exclude_regular_but_search_and_sitemap_include(self):
        all_items = [self.lim, self.reg]
        limited = [i for i in all_items if not g.is_regular(i)]
        top = g.render_top(limited)
        self.assertNotIn("定番ポテト", top)
        self.assertNotIn("定番ポテト", g.generate_rss(limited))
        idx = g.build_search_index(all_items)
        self.assertIn("定番ポテト", [e["n"] for e in idx])
        xml = g.generate_sitemap(limited, [], [self.reg])
        self.assertIn("items/mcdonalds-2.html", xml)
        self.assertIn("/prices/</loc>", xml)
        self.assertIn("/prices/mcdonalds/</loc>", xml)

    def test_chain_page_has_regular_section_by_category_with_absolute_links(self):
        chain = {"slug": "mcdonalds", "name": "マクドナルド"}
        regs = [self.reg, dict(self.reg, product_id="3", name="定番バーガー", category="バーガー")]
        sec = g.render_regular_section(chain, regs)
        self.assertIn("定番メニュー（2品）", sec)
        self.assertLess(sec.index("バーガー（1品）"), sec.index("サイド（1品）"))  # 公式の並び(バーガー→…→サイド)
        self.assertIn('href="/items/mcdonalds-2.html"', sec)
        self.assertNotIn('href="items/', sec)
        page = g.render_grid_page("マクドナルドの限定メニュー", "d", [self.lim], "https://x/", extra_body=sec)
        self.assertLess(page.index("限定A"), page.index("定番メニュー（2品）"))
        # 限定が0件でも定番の節は出る
        empty = g.render_grid_page("マクドナルドの限定メニュー", "d", [], "https://x/", extra_body=sec)
        self.assertIn("定番メニュー（2品）", empty)

    def test_prices_page_orders_newest_first_and_links_are_absolute(self):
        a = item(product_id="1", name="限定A", price_history=[{"date": "2026-11-01", "price": 400},
                                                               {"date": "2026-11-20", "price": 450}])
        b = dict(self.reg, price_history=[{"date": "2026-11-09", "price": 270}, {"date": "2026-12-02", "price": 250},
                                          {"date": "2026-12-02", "price": 250, "relaunch": True}])
        c = item(chain="mos", product_id="9", name="モス商品", price_history=[{"date": "2026-11-15", "price": 300},
                                                                              {"date": "2026-11-20", "price": None}])
        html_all = g.render_prices([a, b, c])
        rows = [r for r in html_all.split("<tr>") if r.startswith("<td")]
        self.assertEqual(len(rows), 2)  # None・同額の再掲は変更に数えない
        self.assertLess(html_all.index("2026-12-02"), html_all.index("2026-11-20"))
        self.assertIn("270円 → 250円", html_all)
        self.assertIn("−20円", html_all)
        self.assertIn("+50円", html_all)
        self.assertIn('href="/items/mcdonalds-2.html"', html_all)
        self.assertIn('href="/prices/mos/"', html_all)
        only_mos = g.render_prices([a, b, c], {"slug": "mos", "name": "モスバーガー"})
        self.assertIn("まだ価格の変更はありません", only_mos)

    def test_prices_page_empty_state_names_record_start(self):
        h = g.render_prices([self.lim, self.reg])
        self.assertIn("まだ価格の変更はありません。記録は2026年10月7日から。", h)
        self.assertIn("/prices/", h)

    def test_variant_change_listed_once_when_it_equals_price_change(self):
        it = item(product_id="5", name="丼", price_history=[{"date": "2026-11-01", "price": 400},
                                                           {"date": "2026-11-20", "price": 420}],
                  price_variants_history=[
                      {"date": "2026-11-01", "variants": [{"label": "並", "price": 400}, {"label": "大", "price": 500}]},
                      {"date": "2026-11-20", "variants": [{"label": "並", "price": 420}, {"label": "大", "price": 530}]}])
        ev = g.price_change_events([it])
        self.assertEqual(sorted((e["label"], e["old"], e["new"]) for e in ev), [("", 400, 420), ("大", 500, 530)])

    def test_footer_links_to_prices(self):
        self.assertIn('href="/prices/"', g.render_top([self.lim]))


class TestNotifyCounts(unittest.TestCase):
    def test_price_changes_are_summarised_and_regular_creation_is_not(self):
        s = {"new": {"mcdonalds": 0}, "regular": {"mcdonalds": 20}, "price_changes": {"mos": 2, "sukiya": 1},
             "ended": {}}
        title, body = notify.build_message(s)
        self.assertIn("価格の変更 3件", title)
        self.assertIn("モスバーガー 2件", body)
        self.assertNotIn("新メニュー", title)
        self.assertNotIn("定番", title + body)

    def test_regular_only_day_sends_nothing(self):
        self.assertIsNone(notify.build_message({"new": {"mcdonalds": 0}, "regular": {"mcdonalds": 20},
                                                "price_changes": {"mcdonalds": 0}, "ended": {}}))

    def test_old_format_without_price_changes_still_works(self):
        self.assertIsNone(notify.build_message({"new": {}, "ended": {}}))
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "r.json"
            p.write_text(json.dumps({"new": {"mos": 1}, "ended": {}}), encoding="utf-8")
            self.assertEqual(notify.read_run_summary(p)["price_changes"], {})

    def test_run_summary_separates_regular_from_new(self):
        with TempData(regular=True):
            _, st = TC.run_mcd(TC.SynthMcd(mcd_cards(3)), D1)
            crawl.save_run_summary({"mcdonalds": st})
            out = json.loads(crawl.RUN_SUMMARY.read_text(encoding="utf-8"))
            self.assertEqual(out["new"]["mcdonalds"], 1)
            self.assertEqual(out["regular"]["mcdonalds"], 3)
            self.assertEqual(out["price_changes"]["mcdonalds"], 0)


if __name__ == "__main__":
    unittest.main()
