"""バーガーキングの栄養成分PDF (pdf_nutrition.parse_burgerking_nutrition と
pdf_update の burgerking 対応) のテスト。合成フィクスチャだけで検証する。

取り込むのは栄養の数値だけ (オーナー決定 2026-11-28)。アレルゲンの記号は
フィクスチャにも入れるが、読み取り対象外であることを確かめる。
"""

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

URLS = {"nutrition": "https://example.test/burgerking_nutrition.pdf"}
LABELS = {"nutrition": "栄養成分及びアレルゲン情報"}
NOW = "2026-11-28T12:50:00+09:00"


def pages():
    return [fx.burgerking_page([
        ("ひだまりワッパー", [283, 672, 28.2, 39.9, 50.8, 3.0, 1053]),
        ("ビーフ＆ダブルハッシュ ビッグマウスギルティ", [351, 1072, 37.1, 75.4, 60.7, 4.7, 1816]),
        ("ほしくずチキン 4ピース", [120, 445, 37.0, 30.8, 4.8, 3.0, 1200]),
    ])]


class BurgerkingParseTests(unittest.TestCase):
    def test_parse(self):
        parsed = pn.parse_burgerking_nutrition(pages())
        self.assertEqual(parsed["updated"], "2026-11-06")
        self.assertEqual(parsed["pages_table"], 1)
        self.assertEqual(parsed["problems"], [])
        self.assertEqual(len(parsed["rows"]), 3)
        row = pn.rows_for_name(parsed["rows"], "ビーフ＆ダブルハッシュ ビッグマウスギルティ")
        self.assertEqual(row[0]["kcal"], 1072)   # カンマつきの熱量も読む
        self.assertEqual(row[0]["protein_g"], 37.1)
        self.assertEqual(row[0]["salt_g"], 4.7)
        self.assertTrue(row[0]["ok"])

    def test_weight_and_sodium_go_to_other(self):
        parsed = pn.parse_burgerking_nutrition(pages())
        rs = pn.rows_for_name(parsed["rows"], "ひだまりワッパー")
        nut, variants, dropped = pn.build_nutrition(rs)
        self.assertEqual(dropped, 0)
        self.assertEqual(nut["kcal"], 672)
        self.assertEqual(nut["other"], {"製品重量": "283g", "ナトリウム": "1053mg"})
        self.assertIsNone(variants)   # サイズ別が無いので単一形

    def test_validate(self):
        parsed = pn.parse_burgerking_nutrition(pages())
        self.assertIsNone(pn.validate_source("burgerking_nutrition", parsed, min_rows=3))
        bad = pn.parse_burgerking_nutrition([fx.burgerking_page([])])
        self.assertIsNotNone(pn.validate_source("burgerking_nutrition", bad, min_rows=3))


class BurgerkingAliasTests(unittest.TestCase):
    def test_alias(self):
        f = pn.burgerking_alias_name
        self.assertEqual(f("ビーフ＆ダブルハッシュ Big Mouthギルティ"),
                         "ビーフ＆ダブルハッシュ ビッグマウスギルティ")
        self.assertEqual(f("オータムSpecial ダブルMushroomワッパーJr."),
                         "オータムスペシャル ダブルマッシュルームワッパージュニア")
        self.assertEqual(f("ギルティ・アメリカンスモーキーチキン4pc"),
                         "ギルティ・アメリカンスモーキーチキン4ピース")
        # 変換が要らない名前はそのまま
        self.assertEqual(f("マッシュルームワッパー"), "マッシュルームワッパー")
        self.assertEqual(f("ギルティ・チキンナゲット5ピース"), "ギルティ・チキンナゲット5ピース")


class FakePdfFetcher:
    def __init__(self, sig="v1"):
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
    d = {"chain": "burgerking", "product_id": name, "name": name, "nutrition": None,
         "nutrition_variants": None, "allergens": None}
    d.update(kw)
    return d


class BurgerkingUpdateTests(unittest.TestCase):
    def run_update(self, products, fetcher=None):
        fetcher = fetcher or FakePdfFetcher()
        state, pending = {}, {}
        with tempfile.TemporaryDirectory() as td:
            report = pu.update_chain("burgerking", products, fetcher, state, pending,
                                     URLS, LABELS, "2026-11-28", NOW,
                                     extract=lambda data, with_chars=False: pages(),
                                     cache_dir=Path(td), min_rows=3)
        return report, state, pending

    def test_update_fills_numbers_only(self):
        products = [product("ひだまりワッパー"),
                    product("ビーフ＆ダブルハッシュ Big Mouthギルティ"),   # 別名で一致
                    product("ギルティ・アメリカンスモーキーチキン4pc"),     # PDF側は「ほしくずチキン」なので一致しない
                    product("ほしくずチキン4pc")]                          # 別名 (4pc -> 4ピース) で一致
        report, state, pending = self.run_update(products)
        self.assertEqual(report["matched"], 3)
        # report["changed"] には pdf_tried の記録だけの商品も入る (保存が必要なため)
        filled_names = {p["name"] for p in products if p["nutrition"] is not None}
        self.assertEqual(filled_names, {"ひだまりワッパー",
                                        "ビーフ＆ダブルハッシュ Big Mouthギルティ",
                                        "ほしくずチキン4pc"})
        self.assertEqual(report["unmatched"], ["ギルティ・アメリカンスモーキーチキン4pc"])
        filled = products[0]
        self.assertEqual(filled["nutrition"]["kcal"], 672)
        self.assertEqual(filled["nutrition_source"]["version"], "2026-11-06")
        self.assertEqual(filled["nutrition_source"]["version_text"], "2026年11月6日更新")
        self.assertIsNone(filled["allergens"])   # アレルゲンは取り込まない
        self.assertEqual(pending, {})

    def test_same_version_does_not_overwrite(self):
        products = [product("ひだまりワッパー")]
        report1, state, _ = self.run_update(products)
        self.assertEqual(len(report1["changed"]), 1)
        products[0]["nutrition"] = {"kcal": 1}   # 同じ版の間は守られる
        fetcher2 = FakePdfFetcher()
        with tempfile.TemporaryDirectory() as td:
            report2 = pu.update_chain("burgerking", products, fetcher2, state, {},
                                      URLS, LABELS, "2026-11-29", NOW,
                                      extract=lambda data, with_chars=False: pages(),
                                      cache_dir=Path(td), min_rows=3)
        self.assertEqual(products[0]["nutrition"], {"kcal": 1})
        self.assertEqual(report2["changed"], [])


if __name__ == "__main__":
    unittest.main()
