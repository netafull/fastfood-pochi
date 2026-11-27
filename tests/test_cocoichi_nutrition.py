"""CoCo壱番屋の栄養成分PDF (pdf_nutrition.parse_cocoichi_nutrition と
pdf_update の cocoichi 対応) のテスト。合成フィクスチャだけで検証する。"""

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

URLS = {"nutrition": "https://example.test/cocoichi_nutrition.pdf"}
LABELS = {"nutrition": "栄養成分情報"}
NOW = "2026-11-27T22:10:00+09:00"


def pages():
    return [fx.cocoichi_page([
        ("ひだまりカレー", [1115, 32.3, 43.7, 151.6, 4.8], "*ポークソース、ライス量「普通(300g)」の場合"),
        ("わかばカレー(2026年12月1日販売開始)", [761, 12.4, 18.5, 140.9, 3.5], None),
        ("つきのわポテト", [279, 3.7, 12.9, 36.6, 1.1], None),
        ("つきのわポテト", [559, 7.4, 25.8, 73.2, 2.2], None),   # 同名で値が違う (量違い)
    ])]


class CocoichiParseTests(unittest.TestCase):
    def test_parse(self):
        parsed = pn.parse_cocoichi_nutrition(pages())
        self.assertEqual(parsed["updated"], "2026-11-27")
        self.assertEqual(parsed["problems"], [])
        curry = pn.rows_for_name(parsed["rows"], "ひだまりカレー")
        self.assertEqual(curry[0]["kcal"], 1115)   # カンマつきの熱量も読む。注記行は名前にしない
        wakaba = pn.rows_for_name(parsed["rows"], "わかばカレー")
        self.assertEqual(wakaba[0]["kcal"], 761)   # (…販売開始) の注記は外して照合する

    def test_ambiguous_duplicate_is_not_matched(self):
        parsed = pn.parse_cocoichi_nutrition(pages())
        self.assertIsNone(pn.rows_for_name(parsed["rows"], "つきのわポテト"))

    def test_validate(self):
        parsed = pn.parse_cocoichi_nutrition(pages())
        self.assertIsNone(pn.validate_source("cocoichi_nutrition", parsed, min_rows=3))
        bad = pn.parse_cocoichi_nutrition([fx.cocoichi_page([])])
        self.assertIsNotNone(pn.validate_source("cocoichi_nutrition", bad, min_rows=3))


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
    d = {"chain": "cocoichi", "product_id": name, "name": name, "nutrition": None,
         "nutrition_variants": None, "allergens": None}
    d.update(kw)
    return d


class CocoichiUpdateTests(unittest.TestCase):
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
        return pu.update_chain("cocoichi", products, fetcher, self.state, self.pending, URLS, LABELS,
                               "2026-11-27", NOW, extract=extract, cache_dir=self.cache, min_rows=3)

    def test_first_run_fills_nutrition(self):
        fetcher = FakePdfFetcher({URLS["nutrition"]: pages()})
        prods = [product("ひだまりカレー"), product("つきのわポテト"), product("PDFに無い商品")]
        report = self.run_update(prods, fetcher)
        self.assertEqual(prods[0]["nutrition"]["kcal"], 1115)
        self.assertEqual(prods[0]["nutrition_source"]["version"], "2026-11-27")
        self.assertEqual(prods[0]["nutrition_source"]["version_text"], "2026年11月27日現在")
        self.assertIn("標準値", prods[0]["nutrition_note"])
        self.assertIsNone(prods[1]["nutrition"])   # 同名で値が食い違うものは当てない
        self.assertIn("つきのわポテト", report["unmatched"])
        self.assertIsNone(prods[2]["nutrition"])

    def test_no_refetch_when_settled(self):
        fetcher = FakePdfFetcher({URLS["nutrition"]: pages()})
        prods = [product("ひだまりカレー")]
        self.run_update(prods, fetcher)
        gets = len(fetcher.gets)
        self.run_update(prods, fetcher)
        self.assertEqual(len(fetcher.gets), gets)


if __name__ == "__main__":
    unittest.main()
