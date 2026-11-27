"""フレッシュネスバーガーの栄養成分PDF (pdf_nutrition.parse_freshness_nutrition と
pdf_update の freshness 対応) のテスト。合成フィクスチャだけで検証する。"""

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

URLS = {"nutrition": "https://example.test/seibun.pdf"}
LABELS = {"nutrition": "栄養成分表"}
NOW = "2026-11-27T21:00:00+09:00"


def food_rows():
    # (名前, [熱量, たんぱく質, 脂質, 炭水化物, 糖質, 食物繊維, 食塩相当量])
    return [
        ("ひだまりバーガー", [431, 20.0, 19.0, 45.0, 37.0, 2.0, 2.0]),
        ("フレンチフライS", [232, 2.8, 11.0, 30.0, 0.5, 3.0, 0.8]),
        ("フレンチフライR", [280, 3.4, 13.0, 36.0, 0.6, 3.6, 1.0]),
        ("フレンチフライL", [375, 4.6, 18.0, 48.0, 0.8, 4.8, 1.3]),
        ("【サイドメニュー】", []),
        ("わかばサラダ", [150, 5.0, 8.0, 15.0, 6.0, 3.0, 0.5]),
    ]


def drink_rows():
    # (名前, [熱量, たんぱく質, 脂質, 炭水化物, 食物繊維, 食塩相当量])
    return [
        ("ブレンドコーヒーS", [5, 0.3, 0.0, 1.0, 0.0, 0.0]),
        ("ブレンドコーヒーR", [8, 0.5, 0.0, 1.6, 0.0, 0.0]),
        ("ゆずレモネード", [120, 0.2, 0.0, 30.0, 0.0, 0.3]),
    ]


def pages():
    return [
        fx.freshness_page(food_rows(), kind="food", updated="2026年11月9日"),
        fx.freshness_page([("つきのわスープ", [134, 2.7, 5.9, 17.3, 8.0, 1.0, 1.4])],
                          kind="food", header=False, title=True),
        fx.freshness_page(drink_rows(), kind="drink", updated="2026年10月21日"),
    ]


class FreshnessParseTests(unittest.TestCase):
    def test_parse_food_and_drink(self):
        parsed = pn.parse_freshness_nutrition(pages())
        self.assertEqual(parsed["updated"], "2026-11-09")
        self.assertEqual(parsed["drink_updated"], "2026-10-21")
        self.assertEqual(parsed["pages_table"], 3)
        names = [r["name"] for r in parsed["rows"]]
        self.assertIn("ひだまりバーガー", names)
        self.assertIn("つきのわスープ", names)   # 見出しの無い継続ページも読む
        self.assertNotIn("【サイドメニュー】", names)
        burger = next(r for r in parsed["rows"] if r["name"] == "ひだまりバーガー")
        self.assertEqual(burger["kcal"], 431)
        self.assertEqual(burger["sugar_g"], 37.0)
        self.assertEqual(burger["fiber_g"], 2.0)
        self.assertTrue(burger["ok"])
        lemonade = next(r for r in parsed["rows"] if r["name"] == "ゆずレモネード")
        self.assertIsNone(lemonade["sugar_g"])   # ドリンクの表には糖質の列が無い
        self.assertEqual(lemonade["fiber_g"], 0.0)

    def test_size_split_only_when_multiple(self):
        parsed = pn.parse_freshness_nutrition(pages())
        fry = [r for r in parsed["rows"] if r["name"] == "フレンチフライ"]
        self.assertEqual([(r["size"], r["kcal"]) for r in fry], [("S", 232), ("R", 280), ("L", 375)])
        coffee = [r for r in parsed["rows"] if r["name"] == "ブレンドコーヒー"]
        self.assertEqual([r["size"] for r in coffee], ["S", "R"])
        lemonade = next(r for r in parsed["rows"] if r["name"] == "ゆずレモネード")
        self.assertEqual(lemonade["size"], "")

    def test_validate(self):
        parsed = pn.parse_freshness_nutrition(pages())
        self.assertIsNone(pn.validate_source("freshness_nutrition", parsed, min_rows=3))
        bad = pn.parse_freshness_nutrition([fx.freshness_page([], kind="food")])
        self.assertIsNotNone(pn.validate_source("freshness_nutrition", bad, min_rows=3))


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
    d = {"chain": "freshness", "product_id": name, "name": name, "nutrition": None,
         "nutrition_variants": None, "allergens": None}
    d.update(kw)
    return d


class FreshnessUpdateTests(unittest.TestCase):
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
        return pu.update_chain("freshness", products, fetcher, self.state, self.pending, URLS, LABELS,
                               "2026-11-27", NOW, extract=extract, cache_dir=self.cache, min_rows=3)

    def test_first_run_fills_nutrition(self):
        fetcher = FakePdfFetcher({URLS["nutrition"]: pages()})
        prods = [product("ひだまりバーガー"), product("フレンチフライ"), product("ゆずレモネード"),
                 product("PDFに無い商品")]
        report = self.run_update(prods, fetcher)
        burger = prods[0]
        self.assertEqual(burger["nutrition"]["kcal"], 431)
        self.assertEqual(burger["nutrition"]["sugar_g"], 37.0)
        self.assertEqual(burger["nutrition_source"]["version"], "2026-11-09|2026-10-21")
        self.assertIn("検査機関", burger["nutrition_note"])
        fry = prods[1]
        self.assertEqual(fry["nutrition"]["kcal"], 232)   # 代表は先頭のサイズ (S)
        self.assertEqual([v["label"] for v in fry["nutrition_variants"]], ["S", "R", "L"])
        self.assertIsNone(prods[2]["nutrition"]["sugar_g"])
        self.assertIsNone(prods[3]["nutrition"])
        self.assertIn("PDFに無い商品", report["unmatched"])
        self.assertEqual(self.state["freshness"]["nutrition"]["version"], "2026-11-09|2026-10-21")

    def test_no_refetch_when_settled(self):
        fetcher = FakePdfFetcher({URLS["nutrition"]: pages()})
        prods = [product("ひだまりバーガー")]
        self.run_update(prods, fetcher)
        gets = len(fetcher.gets)
        self.run_update(prods, fetcher)
        self.assertEqual(len(fetcher.gets), gets)   # 版も試行状況も変わらなければ取得しない


if __name__ == "__main__":
    unittest.main()
