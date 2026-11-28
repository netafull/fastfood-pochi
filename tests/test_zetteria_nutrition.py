"""ゼッテリアの栄養成分PDF (pdf_nutrition.parse_zetteria_nutrition と
pdf_update の zetteria 対応) のテスト。合成フィクスチャだけで検証する。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import pdf_fixtures as fx  # noqa: E402
import pdf_nutrition as pn  # noqa: E402
import pdf_update as pu  # noqa: E402

URLS = {"nutrition": "https://example.test/zetteria_nutrition.pdf"}
LABELS = {"nutrition": "栄養成分一覧表"}
NOW = "2026-11-27T22:00:00+09:00"


def pages():
    return [fx.zetteria_page([
        ("ひだまりバーガー", "", [550, 18.2, 35.6, 36.9, 2.5]),
        ("わかばコーヒー", "S", [0, 0.0, 0.0, 0.1, 0.0]),
        (None, "M", [1, 0.1, 0.0, 0.2, 0.0]),
        (None, "L", [2, 0.2, 0.1, 0.2, 0.0]),
        ("つきみポテト", "", [178, 2.0, 8.0, 24.0, 0.8]),
        (None, "S", [255, 2.9, 12.0, 34.0, 1.1]),
        ("そよかぜサラダ", "", [90, 3.0, 4.0, 10.0, 0.5]),
    ])]


class ZetteriaParseTests(unittest.TestCase):
    def test_parse_groups_by_centered_name(self):
        parsed = pn.parse_zetteria_nutrition(pages())
        self.assertEqual(parsed["updated"], "2026-11-18")
        self.assertEqual(parsed["problems"], [])
        coffee = pn.rows_for_name(parsed["rows"], "わかばコーヒー")
        self.assertEqual([(r["size"], r["kcal"]) for r in coffee], [("S", 0), ("M", 1), ("L", 2)])
        potato = pn.rows_for_name(parsed["rows"], "つきみポテト")
        self.assertEqual([(r["size"], r["kcal"]) for r in potato], [("", 178), ("S", 255)])
        burger = pn.rows_for_name(parsed["rows"], "ひだまりバーガー")
        self.assertEqual(len(burger), 1)
        self.assertIsNone(burger[0]["sugar_g"])   # この表には糖質の列が無い
        self.assertTrue(burger[0]["ok"])

    def test_offset_name_does_not_steal_row(self):
        # 実物で起きた取り違えの再現 (2026-11-28のレビュー指摘): 次の商品の名前の
        # 印字位置が下にずれていると、中点で割る方式では先頭の行 (S) を前の商品が
        # 取ってしまう。行の並びで区切る方式では取り違えない。
        parsed = pn.parse_zetteria_nutrition([fx.zetteria_page([
            ("ハッシュポテト", "", [178, 2.0, 8.0, 24.0, 0.8]),
            ("フレンチフライポテト", "S", [255, 2.9, 12.0, 34.0, 1.1]),
            (None, "M", [357, 4.1, 17.0, 48.0, 1.4]),
            (None, "L", [459, 5.3, 22.0, 61.0, 1.8]),
        ], name_dy={"フレンチフライポテト": -5})])
        self.assertEqual(parsed["problems"], [])
        hashed = pn.rows_for_name(parsed["rows"], "ハッシュポテト")
        self.assertEqual([(r["size"], r["kcal"]) for r in hashed], [("", 178)])
        fry = pn.rows_for_name(parsed["rows"], "フレンチフライポテト")
        self.assertEqual([(r["size"], r["kcal"]) for r in fry],
                         [("S", 255), ("M", 357), ("L", 459)])

    def test_validate(self):
        parsed = pn.parse_zetteria_nutrition(pages())
        self.assertIsNone(pn.validate_source("zetteria_nutrition", parsed, min_rows=3))
        bad = pn.parse_zetteria_nutrition([fx.zetteria_page([], updated="2026/11/18")])
        self.assertIsNotNone(pn.validate_source("zetteria_nutrition", bad, min_rows=3))


class FakePdfFetcher:
    def __init__(self, pages_by_url, sig="v1"):
        self.pages = dict(pages_by_url)
        self.sig = sig
        self.heads = []
        self.gets = []

    def pdf_head(self, chain, url):
        self.heads.append(url)
        return {"etag": self.sig}

    def pdf_get(self, chain, url):
        self.gets.append(url)
        return url.encode()


def product(name, **kw):
    d = {"chain": "zetteria", "product_id": name, "name": name, "nutrition": None,
         "nutrition_variants": None, "allergens": None}
    d.update(kw)
    return d


class ZetteriaUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)
        self.state = {}
        self.pending = {}

    def tearDown(self):
        self.tmp.cleanup()

    def run_update(self, products, fetcher):
        def extract(data, with_chars=False):
            return fetcher.pages[data.decode()]
        return pu.update_chain("zetteria", products, fetcher, self.state, self.pending, URLS, LABELS,
                               "2026-11-27", NOW, extract=extract, cache_dir=self.cache, min_rows=3)

    def test_first_run_fills_nutrition(self):
        fetcher = FakePdfFetcher({URLS["nutrition"]: pages()})
        prods = [product("ひだまりバーガー"), product("わかばコーヒー"), product("PDFに無い商品")]
        report = self.run_update(prods, fetcher)
        self.assertEqual(prods[0]["nutrition"]["kcal"], 550)
        self.assertEqual(prods[0]["nutrition_source"]["version"], "2026-11-18")
        self.assertIn("日本食品標準成分表", prods[0]["nutrition_note"])
        coffee = prods[1]
        self.assertEqual(coffee["nutrition"]["kcal"], 0)   # 代表は先頭のサイズ (S)
        self.assertEqual([v["label"] for v in coffee["nutrition_variants"]], ["S", "M", "L"])
        self.assertIsNone(prods[2]["nutrition"])
        self.assertIn("PDFに無い商品", report["unmatched"])

    def test_no_refetch_when_settled(self):
        fetcher = FakePdfFetcher({URLS["nutrition"]: pages()})
        prods = [product("ひだまりバーガー")]
        self.run_update(prods, fetcher)
        gets = len(fetcher.gets)
        self.run_update(prods, fetcher)
        self.assertEqual(len(fetcher.gets), gets)


if __name__ == "__main__":
    unittest.main()
