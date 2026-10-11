"""pdf_update.py(取得・更新検知・検証ゲート・商品への反映)のテスト。PDFは偽物、ページは合成フィクスチャ。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401
import pdf_fixtures as fx
import pdf_nutrition as pn
import pdf_update as pu
from fetch import BlockedError

SK_URLS = {"nutrition": "https://example.test/nutrition.pdf", "allergen": "https://example.test/allergen.pdf"}
SK_LABELS = {"nutrition": "栄養成分一覧", "allergen": "アレルゲン一覧"}
YO_URLS = {"nutrition": "https://example.test/menu/"}
YO_LABELS = {"nutrition": "吉野家メニュー情報", "allergen": ""}
NOW = "2026-10-12T07:30:00+09:00"


def sk_nutrition_pages(updated="2026年9月29日", kcal_override=None):
    rows = [("ミニ", 10.0, 12.0, 60.0, 2.0, kcal_override), ("並盛", 15.0, 18.0, 90.0, 3.0, kcal_override)]
    return [fx.sukiya_nutrition_page([
        ("ひだまり牛丼", rows),
        ("こもれびカレー", [("並盛", 20.0, 25.0, 100.0, 5.0, kcal_override)]),
        ("はるかぜ丼", [("並盛", 22.0, 14.0, 100.0, 3.5, kcal_override)]),
    ], updated=updated)]


def sk_allergen_pages(updated="2026/9/29更新"):
    a = "●-△△" + "-" * 24
    return [fx.sukiya_allergen_page([(1, "ひだまり牛丼", a), (2, "こもれびカレー", "-●" + "-" * 26),
                                    (3, "はるかぜ丼", a)], updated=updated)]


def yo_pages(date="2026年10月8日現在", issue=262, kcal_wrong=False):
    rows = [("並盛", 19.0, 23.0, 88.0, 2.5, {0: "○", 2: "○", 5: "▲"}),
            ("大盛", 24.0, 29.0, 119.0, 3.1, {0: "○", 2: "○"})]
    return [fx.yoshinoya_intro_page(), fx.yoshinoya_page([
        ("ひだまり丼", ["※この丼の小麦は醤油由来です。"], rows),
        ("こもれび定食", [], [(None, 33.0, 30.0, 110.0, 4.0, {1: "○"})]),
    ], issue=issue, date=date)]


class FakePdfFetcher:
    """data(URL -> ページ辞書のリスト)と、URLごとの指紋(ETag)を持つ偽の取得器。"""

    def __init__(self, pages_by_url, sigs=None, fail_head=None):
        self.pages = dict(pages_by_url)
        self.sigs = dict(sigs or {u: "v1" for u in pages_by_url})
        self.heads = []
        self.gets = []
        self.fail_head = fail_head

    def pdf_head(self, chain, url):
        self.heads.append(url)
        if self.fail_head:
            raise self.fail_head
        return {"etag": self.sigs[url]}

    def pdf_get(self, chain, url):
        self.gets.append(url)
        return url.encode()  # extract が URL からページを引く


def extractor(fetcher):
    def extract(data, with_chars=False):
        return fetcher.pages[data.decode()]
    return extract


def product(name, pid=None, **kw):
    d = {"chain": "sukiya", "product_id": pid or name, "name": name, "nutrition": None, "nutrition_variants": None,
         "allergens": None}
    d.update(kw)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)
        self.state = {}
        self.pending = {}

    def tearDown(self):
        self.tmp.cleanup()

    def run_sukiya(self, products, fetcher, **kw):
        return pu.update_chain("sukiya", products, fetcher, self.state, self.pending, SK_URLS, SK_LABELS,
                               "2026-10-12", NOW, extract=extractor(fetcher), cache_dir=self.cache, min_rows=3, **kw)

    def run_yoshinoya(self, products, fetcher, **kw):
        return pu.update_chain("yoshinoya", products, fetcher, self.state, self.pending, YO_URLS, YO_LABELS,
                               "2026-10-12", NOW, extract=extractor(fetcher), cache_dir=self.cache, min_rows=3, **kw)

    def sk_fetcher(self, **kw):
        return FakePdfFetcher({SK_URLS["nutrition"]: sk_nutrition_pages(), SK_URLS["allergen"]: sk_allergen_pages()}, **kw)


class TestSukiyaUpdate(Base):
    def test_first_run_fills_matched_products_only(self):
        fetcher = self.sk_fetcher()
        prods = [product("ひだまり牛丼"), product("はるかぜ丼"), product("どこにもない丼")]
        rep = self.run_sukiya(prods, fetcher)
        a, b, c = prods
        self.assertEqual(a["nutrition"]["protein_g"], 15.0)               # 並盛が代表
        self.assertEqual([v["label"] for v in a["nutrition_variants"]], ["並盛", "ミニ"])
        self.assertIsNone(b["nutrition_variants"])                          # 1サイズのみ
        self.assertEqual(a["nutrition_source"]["version"], "2026-09-29")
        self.assertEqual(a["nutrition_source"]["version_text"], "2026年9月29日更新")
        self.assertEqual(a["nutrition_source"]["url"], SK_URLS["nutrition"])
        self.assertEqual(a["nutrition_source"]["fetched_at"], NOW)
        self.assertIn("調味料", a["nutrition_note"])
        self.assertEqual(a["allergens"]["contains"], ["小麦"])
        self.assertEqual(a["allergens_conditional"]["mark"], "△")
        self.assertEqual(a["allergen_source"]["url"], SK_URLS["allergen"])
        self.assertIsNone(c["nutrition"])
        self.assertIsNone(c["allergens"])
        self.assertEqual(rep["get_requests"], 2)
        self.assertEqual(rep["head_requests"], 2)
        self.assertEqual(rep["unmatched"], ["どこにもない丼"])
        self.assertEqual({p["product_id"] for p in rep["changed_products"]}, {"ひだまり牛丼", "はるかぜ丼", "どこにもない丼"})
        self.assertEqual(self.state["sukiya"]["nutrition"]["version"], "2026-09-29")
        self.assertEqual(self.pending, {})

    def test_other_fields_are_not_touched(self):
        p = product("ひだまり牛丼", price=500, status="on_sale", official_url="https://x/")
        self.run_sukiya([p], self.sk_fetcher())
        self.assertEqual((p["price"], p["status"], p["official_url"]), (500, "on_sale", "https://x/"))

    def test_no_request_when_nothing_changed_and_nothing_to_try(self):
        fetcher = self.sk_fetcher()
        prods = [product("ひだまり牛丼"), product("はるかぜ丼")]
        self.run_sukiya(prods, fetcher)
        fetcher2 = self.sk_fetcher()
        fetcher2.pages = fetcher.pages
        rep = self.run_sukiya(prods, fetcher2)
        self.assertEqual(fetcher2.gets, [])                                 # HEADだけ
        self.assertEqual(len(fetcher2.heads), 2)
        self.assertEqual(rep["changed"], [])

    def test_unmatched_product_is_tried_once_per_version(self):
        fetcher = self.sk_fetcher()
        prods = [product("ひだまり牛丼"), product("どこにもない丼")]
        self.run_sukiya(prods, fetcher)
        self.assertEqual(len(fetcher.gets), 2)
        again = self.sk_fetcher()
        self.run_sukiya(prods, again)
        self.assertEqual(again.gets, [])                                    # 同じ版では再試行しない

    def test_new_product_triggers_one_fetch_then_settles(self):
        fetcher = self.sk_fetcher()
        prods = [product("ひだまり牛丼")]
        self.run_sukiya(prods, fetcher)
        prods.append(product("こもれびカレー"))                              # 新しく記録された商品
        for f in self.cache.glob("*"):                                       # CIのように、キャッシュが無い状態
            f.unlink()
        again = self.sk_fetcher()
        rep = self.run_sukiya(prods, again)
        self.assertEqual(len(again.gets), 2)
        self.assertEqual(prods[1]["nutrition"]["protein_g"], 20.0)
        self.assertEqual(prods[1]["nutrition_source"]["version"], "2026-09-29")
        self.assertEqual(rep["get_requests"], 2)
        third = self.sk_fetcher()
        self.run_sukiya(prods, third)
        self.assertEqual(third.gets, [])

    def test_cache_avoids_refetch_when_signature_same(self):
        fetcher = self.sk_fetcher()
        self.run_sukiya([product("ひだまり牛丼")], fetcher)
        self.assertTrue((self.cache / "sukiya_nutrition.pdf").exists())
        # 状態を消して(=初回と同じ条件)再実行しても、指紋が同じキャッシュがあればGETしない
        self.state.clear()
        again = self.sk_fetcher()
        rep = self.run_sukiya([product("ひだまり牛丼")], again)
        self.assertEqual(again.gets, [])
        self.assertEqual(rep["get_requests"], 0)

    def test_same_version_does_not_overwrite_but_new_version_updates(self):
        fetcher = self.sk_fetcher()
        p = product("ひだまり牛丼")
        self.run_sukiya([p], fetcher)
        p["nutrition"]["kcal"] = 12345                                       # 手で書き換えた印
        # 同じ版が別ファイルとして再公開された(指紋だけ変わる)
        re_up = self.sk_fetcher(sigs={SK_URLS["nutrition"]: "v2", SK_URLS["allergen"]: "v2"})
        re_up.pages = fetcher.pages
        self.run_sukiya([p], re_up)
        self.assertEqual(p["nutrition"]["kcal"], 12345)                     # 上書きしない
        # 版が新しくなった
        pages = {SK_URLS["nutrition"]: [fx.sukiya_nutrition_page(
            [("ひだまり牛丼", [("ミニ", 11.0, 12.0, 60.0, 2.0, None), ("並盛", 16.0, 18.0, 90.0, 3.0, None)]),
             ("こもれびカレー", [("並盛", 20.0, 25.0, 100.0, 5.0, None)])],
            updated="2026年11月5日")], SK_URLS["allergen"]: sk_allergen_pages("2026/11/5更新")}
        newer = FakePdfFetcher(pages, sigs={SK_URLS["nutrition"]: "v3", SK_URLS["allergen"]: "v3"})
        self.run_sukiya([p], newer)
        self.assertEqual(p["nutrition"]["protein_g"], 16.0)
        self.assertEqual(p["nutrition_source"]["version"], "2026-11-05")
        self.assertEqual(p["allergen_source"]["version"], "2026-11-05")

    def test_energy_failure_drops_the_row_only(self):
        pages = [fx.sukiya_nutrition_page([
            ("ひだまり牛丼", [("ミニ", 10.0, 12.0, 60.0, 2.0, 999), ("並盛", 15.0, 18.0, 90.0, 3.0, None)]),
            ("こもれびカレー", [("並盛", 20.0, 25.0, 100.0, 5.0, None)]),
            ("はるかぜ丼", [("並盛", 22.0, 14.0, 100.0, 3.5, None)]),
            ("ゆうなぎ丼", [("並盛", 22.0, 14.0, 100.0, 3.5, None)]),
        ])]
        fetcher = FakePdfFetcher({SK_URLS["nutrition"]: pages, SK_URLS["allergen"]: sk_allergen_pages()})
        p = product("ひだまり牛丼")
        others = [product("こもれびカレー"), product("はるかぜ丼"), product("ゆうなぎ丼")]
        self.run_sukiya([p] + others, fetcher)
        self.assertEqual(p["nutrition"]["protein_g"], 15.0)
        self.assertIsNone(p["nutrition_variants"])                           # ミニは検算に合わず採用されない

    def test_gate_aborts_when_half_of_matched_products_fail(self):
        pages = {SK_URLS["nutrition"]: sk_nutrition_pages(kcal_override=1), SK_URLS["allergen"]: sk_allergen_pages()}
        fetcher = FakePdfFetcher(pages)
        # 先にあった値は変わらない
        old = product("ひだまり牛丼", nutrition={"kcal": 111}, nutrition_source={"version": "2026-08-01"}, allergens=None)
        new = product("はるかぜ丼")
        self.run_sukiya([old, new], fetcher)
        self.assertEqual(old["nutrition"], {"kcal": 111})
        self.assertIsNone(new["nutrition"])
        self.assertIn("pdf:nutrition", self.pending["sukiya"])
        self.assertIn("validation", self.pending["sukiya"]["pdf:nutrition"]["reason"])
        # 同じ壊れたファイルは、翌日に取り直さない
        again = FakePdfFetcher(pages)
        self.run_sukiya([old, new], again)
        self.assertNotIn(SK_URLS["nutrition"], again.gets)

    def test_gate_aborts_when_half_of_matched_products_fail_even_if_pdf_looks_fine(self):
        # PDF全体の検算通過率は80%(基準ぎりぎり)でも、記録している商品の半分が落ちるなら、この回は更新しない
        pages = [fx.sukiya_nutrition_page([
            ("ひだまり牛丼", [("並盛", 15.0, 18.0, 90.0, 3.0, 999)]),
            ("こもれびカレー", [("並盛", 20.0, 25.0, 100.0, 5.0, None)]),
            ("ほか丼A", [("並盛", 22.0, 14.0, 100.0, 3.5, None)]),
            ("ほか丼B", [("並盛", 22.0, 14.0, 100.0, 3.5, None)]),
            ("ほか丼C", [("並盛", 22.0, 14.0, 100.0, 3.5, None)]),
        ])]
        fetcher = FakePdfFetcher({SK_URLS["nutrition"]: pages, SK_URLS["allergen"]: sk_allergen_pages()})
        a, b = product("ひだまり牛丼"), product("こもれびカレー")
        rep = self.run_sukiya([a, b], fetcher)
        self.assertIn("検算で落ちた", rep["aborted"])
        self.assertIsNone(a["nutrition"])
        self.assertIsNone(b["nutrition"])                                   # 検算に通った商品も、この回は入れない
        self.assertIn("validation", self.pending["sukiya"]["pdf:nutrition"]["reason"])

    def test_structure_change_updates_nothing_and_continues(self):
        broken = [fx.sukiya_nutrition_page([("ひだまり牛丼", [("並盛", 15.0, 18.0, 90.0, 3.0, None)])], header=False)]
        fetcher = FakePdfFetcher({SK_URLS["nutrition"]: broken, SK_URLS["allergen"]: sk_allergen_pages()})
        p = product("ひだまり牛丼")
        rep = self.run_sukiya([p], fetcher)
        self.assertIsNone(p["nutrition"])                                    # 栄養は更新されない
        self.assertIsNotNone(p["allergens"])                                 # もう1本のPDFは影響されない
        self.assertIn("見出し", self.pending["sukiya"]["pdf:nutrition"]["reason"])
        self.assertNotIn("version", self.state["sukiya"]["nutrition"])        # 読み取れなかった版は「取り込んだ版」に記録しない
        self.assertFalse(rep["blocked"])

    def test_parse_exception_is_recorded_not_raised(self):
        fetcher = self.sk_fetcher()

        def boom(data, with_chars=False):
            raise ValueError("壊れたPDF")
        rep = pu.update_chain("sukiya", [product("ひだまり牛丼")], fetcher, self.state, self.pending, SK_URLS, SK_LABELS,
                              "2026-10-12", NOW, extract=boom, cache_dir=self.cache, min_rows=3)
        self.assertIn("parse_error", self.pending["sukiya"]["pdf:nutrition"]["reason"])
        self.assertEqual(rep["changed"], [])

    def test_blocked_stops_without_changes(self):
        fetcher = self.sk_fetcher(fail_head=BlockedError("HTTP 403"))
        p = product("ひだまり牛丼")
        rep = self.run_sukiya([p], fetcher)
        self.assertTrue(rep["blocked"])
        self.assertIsNone(p["nutrition"])
        self.assertEqual(fetcher.gets, [])

    def test_head_failure_still_fetches_for_new_products(self):
        fetcher = self.sk_fetcher(fail_head=OSError("timeout"))
        p = product("ひだまり牛丼")
        self.run_sukiya([p], fetcher)
        self.assertIsNotNone(p["nutrition"])

    def test_missing_pdfminer_skips_without_requests(self):
        fetcher = self.sk_fetcher()
        with mock.patch.object(pu, "have_pdfminer", return_value=False):
            rep = pu.update_chain("sukiya", [product("ひだまり牛丼")], fetcher, self.state, self.pending, SK_URLS,
                                  SK_LABELS, "2026-10-12", NOW, cache_dir=self.cache)
        self.assertIn("pdfminer", rep["skipped"])
        self.assertEqual((fetcher.heads, fetcher.gets), ([], []))

    def test_partial_match_nutrition_without_allergen(self):
        pages = {SK_URLS["nutrition"]: sk_nutrition_pages(),
                 SK_URLS["allergen"]: [fx.sukiya_allergen_page([(1, "ほかの丼", "●" + "-" * 27)])]}
        p = product("ひだまり牛丼")
        self.run_sukiya([p], FakePdfFetcher(pages))
        self.assertIsNotNone(p["nutrition"])
        self.assertIsNone(p["allergens"])


class TestYoshinoyaUpdate(Base):
    def test_fills_nutrition_and_allergens_from_one_pdf(self):
        fetcher = FakePdfFetcher({YO_URLS["nutrition"]: yo_pages()})
        a, b, c = product("ひだまり丼", chain="yoshinoya"), product("こもれび定食"), product("おぼえのない丼")
        rep = self.run_yoshinoya([a, b, c], fetcher)
        self.assertEqual(a["nutrition"]["protein_g"], 19.0)
        self.assertEqual([v["label"] for v in a["nutrition_variants"]], ["並盛", "大盛"])
        self.assertEqual(a["allergens"]["contains"], ["小麦", "卵"])
        self.assertEqual(a["allergens"]["partial"], [{"name": "えび", "mark": "▲"}])
        self.assertIn("あおのり", a["allergens_conditional"]["meaning"])
        self.assertEqual(a["nutrition_source"]["version"], "262|2026-10-08")
        self.assertEqual(a["nutrition_source"]["version_text"], "262号・2026年10月8日現在")
        self.assertEqual(a["nutrition_source"]["issue"], 262)
        self.assertEqual(b["allergens"]["contains"], ["乳"])
        self.assertIsNone(c["nutrition"])
        self.assertEqual((rep["head_requests"], rep["get_requests"]), (1, 1))

    def test_new_issue_updates_old_values(self):
        fetcher = FakePdfFetcher({YO_URLS["nutrition"]: yo_pages()})
        a = product("ひだまり丼")
        self.run_yoshinoya([a], fetcher)
        newer = FakePdfFetcher({YO_URLS["nutrition"]: yo_pages(date="2026年11月1日現在", issue=263)},
                               sigs={YO_URLS["nutrition"]: "v2"})
        self.run_yoshinoya([a], newer)
        self.assertEqual(a["nutrition_source"]["version"], "263|2026-11-01")

    def test_validation_failure_keeps_old_values(self):
        fetcher = FakePdfFetcher({YO_URLS["nutrition"]: yo_pages()})
        a = product("ひだまり丼")
        self.run_yoshinoya([a], fetcher)
        before = dict(a["nutrition"])
        bad = [fx.yoshinoya_intro_page()]  # 表のページが無い(構造変更の疑い)
        self.run_yoshinoya([a], FakePdfFetcher({YO_URLS["nutrition"]: bad}, sigs={YO_URLS["nutrition"]: "v2"}))
        self.assertEqual(a["nutrition"], before)
        self.assertIn("pdf:menu", self.pending["yoshinoya"])


class NoSigFetcher(FakePdfFetcher):
    def pdf_head(self, chain, url):
        self.heads.append(url)
        return {"content-type": "application/pdf"}   # ETag等が無い(動的配信)


class TestNoSignature(Base):
    def run_on(self, day, products, fetcher):
        return pu.update_chain("yoshinoya", products, fetcher, self.state, self.pending, YO_URLS, YO_LABELS,
                               day, NOW, extract=extractor(fetcher), cache_dir=self.cache, min_rows=3)

    def test_rechecked_only_after_a_week(self):
        a = product("ひだまり丼")
        f1 = NoSigFetcher({YO_URLS["nutrition"]: yo_pages()})
        self.run_on("2026-10-12", [a], f1)
        self.assertEqual(len(f1.gets), 1)
        f2 = NoSigFetcher({YO_URLS["nutrition"]: yo_pages()})
        self.run_on("2026-10-13", [a, product("おぼえのない丼")], f2)      # 未照合の商品が増えても、指紋が無いPDFは週1回まで
        self.assertEqual(f2.gets, [])
        f3 = NoSigFetcher({YO_URLS["nutrition"]: yo_pages(date="2026年10月20日現在", issue=263)})
        self.run_on("2026-10-19", [a], f3)
        self.assertEqual(len(f3.gets), 1)
        self.assertEqual(a["nutrition_source"]["issue"], 263)             # 新しい号を検知して更新

    def test_failed_parse_is_not_retried_daily(self):
        f1 = NoSigFetcher({YO_URLS["nutrition"]: [fx.yoshinoya_intro_page()]})
        self.run_on("2026-10-12", [product("ひだまり丼")], f1)
        f2 = NoSigFetcher({YO_URLS["nutrition"]: [fx.yoshinoya_intro_page()]})
        self.run_on("2026-10-13", [product("ひだまり丼")], f2)
        self.assertEqual((len(f1.gets), len(f2.gets)), (1, 0))


class TestHelpers(unittest.TestCase):
    def test_head_signature(self):
        self.assertIsNone(pu.head_signature(None))
        self.assertIsNone(pu.head_signature({}))
        self.assertEqual(pu.head_signature({"etag": "a", "content-length": "5"}), "a||5")

    def test_load_pdf_uses_cache_only_when_signature_matches(self):
        with tempfile.TemporaryDirectory() as d:
            f = FakePdfFetcher({"u": []})
            data, got = pu.load_pdf(f, "sukiya", "k", "u", "s1", Path(d))
            self.assertTrue(got)
            data, got = pu.load_pdf(f, "sukiya", "k", "u", "s1", Path(d))
            self.assertFalse(got)
            data, got = pu.load_pdf(f, "sukiya", "k", "u", "s2", Path(d))
            self.assertTrue(got)
            data, got = pu.load_pdf(f, "sukiya", "k", "u", None, Path(d))
            self.assertTrue(got)


if __name__ == "__main__":
    unittest.main()
