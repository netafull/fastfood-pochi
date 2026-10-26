"""pdf_nutrition.py の純粋関数のテスト。入力は tests/pdf_fixtures.py の合成データ(名前・数値は創作)。"""

import unittest

import helpers  # noqa: F401  (scripts/ を import パスに入れる)
import pdf_fixtures as fx
import pdf_nutrition as pn


def sukiya_pages():
    p1 = fx.sukiya_nutrition_page([
        ("ひだまり牛丼", [("ミニ", 10.0, 12.0, 60.0, 2.0, None), ("並盛", 15.0, 18.0, 90.0, 3.0, None),
                       ("大盛", 20.0, 22.0, 120.0, 4.0, None)]),
        (("v", "ラ", "イ", "ト"), [("ミニ", 8.0, 7.0, 50.0, 1.5, None), ("並盛", 12.0, 10.0, 70.0, 2.5, None),
                          ("大盛", 16.0, 13.0, 95.0, 3.0, None), ("特盛", 20.0, 16.0, 120.0, 3.5, None)]),
        ("こもれびカレー", [(None, 20.0, 25.0, 100.0, 5.0, None)]),
        (("multi", ["すずらんジュース", "あさつゆ茶"], [2, 1]),
         [("M", 0.0, 0.0, 12.0, 0.0, None), ("L", 0.0, 0.0, 18.0, 0.0, None), (None, 0.0, 0.0, 0.0, 0.0, 0)]),
        ("ひがわり定食", [("並盛", 30.0, 20.0, 90.0, 4.0, 900)]),   # kcalが検算に合わない(創作)
    ])
    p2 = fx.sukiya_nutrition_page([
        ("はるかぜ丼", [("並盛", 22.0, 14.0, 100.0, 3.5, None)]),
    ], updated=None)
    return [p1, p2]


class TestSukiyaNutrition(unittest.TestCase):
    def setUp(self):
        self.parsed = pn.parse_sukiya_nutrition(sukiya_pages())
        self.by = {}
        for r in self.parsed["rows"]:
            self.by.setdefault(r["name"], []).append(r)

    def test_updated_and_pages(self):
        self.assertEqual(self.parsed["updated"], "2026-09-29")
        self.assertEqual(self.parsed["pages_table"], 2)
        self.assertEqual(self.parsed["problems"], [])

    def test_single_line_name_with_sizes(self):
        rows = self.by["ひだまり牛丼"]
        self.assertEqual([r["size"] for r in rows], ["ミニ", "並盛", "大盛"])
        self.assertEqual(rows[1]["protein_g"], 15.0)
        self.assertEqual(rows[1]["kcal"], 4 * 15 + 9 * 18 + 4 * 90)
        self.assertTrue(all(r["ok"] for r in rows))

    def test_vertically_split_name_is_joined(self):
        # 縦書き風に1文字ずつ分割された名前が、上から順につながる
        self.assertIn("ライト", self.by)
        self.assertEqual([r["size"] for r in self.by["ライト"]], ["ミニ", "並盛", "大盛", "特盛"])

    def test_row_without_size(self):
        r = self.by["こもれびカレー"][0]
        self.assertIsNone(r["size"])
        self.assertTrue(r["ok"])

    def test_multi_menu_cell_assigns_names_by_position(self):
        # 1つのセルに複数のメニュー名が並ぶブロックは、名前をサイズ行の中央の高さで割り当てる
        self.assertEqual([r["size"] for r in self.by["すずらんジュース"]], ["M", "L"])
        self.assertEqual(len(self.by["あさつゆ茶"]), 1)
        self.assertFalse(any(r["ambiguous"] for r in self.parsed["rows"]))

    def test_energy_check_rejects_wrong_kcal(self):
        self.assertFalse(self.by["ひがわり定食"][0]["ok"])
        self.assertTrue(self.by["はるかぜ丼"][0]["ok"])

    def test_second_page_header_is_found(self):
        self.assertIn("はるかぜ丼", self.by)

    def test_pass_rate(self):
        rows = self.parsed["rows"]
        self.assertAlmostEqual(pn.pass_rate(rows), (len(rows) - 1) / len(rows))

    def test_page_without_header_is_a_problem_not_a_crash(self):
        pages = sukiya_pages() + [fx.sukiya_nutrition_page([("よそ見", [("並盛", 1.0, 1.0, 1.0, 0.1, None)])],
                                                          updated=None, header=False)]
        parsed = pn.parse_sukiya_nutrition(pages)
        self.assertEqual(parsed["pages_table"], 2)
        self.assertEqual(len(parsed["problems"]), 1)
        self.assertNotIn("よそ見", {r["name"] for r in parsed["rows"]})


class TestEnergy(unittest.TestCase):
    def test_tolerance(self):
        self.assertTrue(pn.energy_ok(803, 29.3, 29.8, 105.2))
        self.assertTrue(pn.energy_ok(37, 1.0, 0.1, 8.0))      # 小さい値は絶対誤差6kcalまで
        self.assertFalse(pn.energy_ok(400, 10, 10, 50))       # 計算上330kcal
        self.assertFalse(pn.energy_ok(None, 1, 1, 1))
        self.assertTrue(pn.energy_ok(0, 0.0, 0.0, 0.1))

    def test_to_number(self):
        self.assertEqual(pn.to_number("1,006"), 1006.0)
        self.assertEqual(pn.to_number("４２"), 42.0)
        self.assertIsNone(pn.to_number("-"))


class TestSukiyaAllergen(unittest.TestCase):
    def setUp(self):
        a = "●-△△" + "-" * 24
        b = "-●" + "△" * 3 + "-" * 23
        pages = [fx.sukiya_allergen_page([(1, "ひだまり牛丼", a), (2, "こもれびカレー", b), (3, "はるかぜ丼", a)])]
        self.parsed = pn.parse_sukiya_allergen(pages)

    def test_names_updated_and_legend(self):
        self.assertEqual(self.parsed["updated"], "2026-09-29")
        self.assertEqual(self.parsed["allergen_names"], fx.AL_NAMES)
        self.assertIn("同じライン", self.parsed["legend"]["△"])

    def test_rows_including_merged_cells(self):
        rows = {r["name"]: r for r in self.parsed["rows"]}
        self.assertEqual(set(rows), {"ひだまり牛丼", "こもれびカレー", "はるかぜ丼"})
        # 番号と名前が1行・名前に最初の記号が続く行も、28個の記号がそろう
        self.assertTrue(all(r["complete"] for r in rows.values()))
        self.assertEqual(rows["こもれびカレー"]["id"], 2)
        self.assertEqual(rows["はるかぜ丼"]["marks"][0], "●")

    def test_match(self):
        al = pn.match_sukiya_allergen(self.parsed, "ひだまり　牛丼")  # 空白の揺れは吸収
        self.assertEqual(al["contains"], ["小麦"])
        self.assertEqual([p["name"] for p in al["partial"]], ["卵", "乳"])
        self.assertEqual(al["partial"][0]["mark"], "△")
        self.assertIsNone(pn.match_sukiya_allergen(self.parsed, "ひだまり"))   # 部分一致はしない

    def test_same_name_with_different_marks_is_ambiguous(self):
        pages = [fx.sukiya_allergen_page([(1, "ふたご丼", "●" + "-" * 27), (2, "ふたご丼", "-" * 28)])]
        self.assertIsNone(pn.match_sukiya_allergen(pn.parse_sukiya_allergen(pages), "ふたご丼"))


def yoshinoya_pages(scale=1.0, issue=262, date="2026年10月8日現在"):
    blocks = [
        ("ひだまり丼", ["※この丼の小麦は醤油由来です。"],
         [("小盛", 14.0, 18.0, 60.0, 1.9, {0: "○", 2: "○"}), ("並盛", 19.0, 23.0, 88.0, 2.5, {0: "○", 2: "○", 5: "▲"}),
          ("大盛", 24.0, 29.0, 119.0, 3.1, {0: "○", 2: "○"})]),
        ("こもれび定食", [], [(None, 33.0, 30.0, 110.0, 4.0, {1: "○"})]),
        ("あさつゆ茶", [], [("100ml", 0.0, 0.0, 0.0, 0.0, {})]),
    ]
    ja = fx.yoshinoya_page(blocks, lang="ja", scale=scale, issue=issue, date=date)
    en = fx.yoshinoya_page([("Hidamari bowl", [], [("Small", 14.0, 18.0, 60.0, 1.9, {})])], lang="en", scale=scale,
                           issue=issue, date=date)
    return [fx.yoshinoya_intro_page(), ja, en]


class TestYoshinoya(unittest.TestCase):
    def setUp(self):
        self.parsed = pn.parse_yoshinoya(yoshinoya_pages())
        self.by = {}
        for r in self.parsed["rows"]:
            self.by.setdefault(r["name"], []).append(r)

    def test_issue_date_legend(self):
        self.assertEqual(self.parsed["issue"], 262)
        self.assertEqual(self.parsed["date"], "2026-10-08")
        self.assertEqual(self.parsed["legend"]["○"], "原材料に含みます。")
        self.assertIn("あおのり", self.parsed["legend"]["▲"])
        self.assertIn("採取されています", self.parsed["legend"]["▲"])

    def test_only_japanese_pages_are_used(self):
        self.assertEqual(self.parsed["pages_table"], 1)
        self.assertNotIn("Hidamari bowl", self.by)

    def test_allergen_names_from_vertical_header(self):
        self.assertEqual(self.parsed["allergen_names"], fx.YO_AL)

    def test_rows_and_marks(self):
        rows = self.by["ひだまり丼"]
        self.assertEqual([r["size"] for r in rows], ["小盛", "並盛", "大盛"])
        mid = rows[1]
        self.assertEqual(mid["protein_g"], 19.0)
        self.assertEqual(mid["salt_g"], 2.5)  # 数値に記号が続く文字行から数値だけを取る
        self.assertEqual([i for i, m in enumerate(mid["marks"]) if m == "○"], [0, 2])
        self.assertEqual(mid["marks"][5], "▲")
        self.assertTrue(all(r["ok"] for r in self.parsed["rows"]))

    def test_note_lines_are_not_part_of_name(self):
        self.assertIn("ひだまり丼", self.by)
        self.assertEqual(len(self.by), 3)

    def test_kcal_with_thousands_separator(self):
        pages = [fx.yoshinoya_intro_page(), fx.yoshinoya_page([("どっしり丼", [], [("特盛", 60.0, 80.0, 190.0, 5.0, {})])])]
        rows = pn.parse_yoshinoya(pages)["rows"]
        self.assertEqual(rows[0]["kcal"], pn.to_number("1,720"))
        self.assertTrue(rows[0]["ok"])


class TestYoshinoyaSmallPage(unittest.TestCase):
    """263号: 同じ表の構造のまま、PDFのページ寸法が約1/3.85(A4相当)に変わった。"""

    def setUp(self):
        self.pages = yoshinoya_pages(fx.YO_SCALE_263, issue=263, date="2026年10月23日現在")
        self.parsed = pn.parse_yoshinoya(self.pages)

    def test_parses_same_as_old_size(self):
        old = pn.parse_yoshinoya(yoshinoya_pages())
        self.assertEqual(self.parsed["issue"], 263)
        self.assertEqual(self.parsed["date"], "2026-10-23")
        self.assertEqual(self.parsed["pages_table"], 1)
        self.assertEqual(self.parsed["problems"], [])
        self.assertEqual(self.parsed["allergen_names"], fx.YO_AL)
        key = ("name", "size", "kcal", "protein_g", "fat_g", "carbs_g", "salt_g", "ok", "marks")
        self.assertEqual([tuple(r[k] for k in key) for r in self.parsed["rows"]],
                         [tuple(r[k] for k in key) for r in old["rows"]])
        self.assertIsNone(pn.validate_source("yoshinoya", self.parsed, min_rows=3))

    def test_small_page_without_size_info_is_held_back(self):
        # ページ寸法が無いと旧寸法とみなす。小さいページは読み取れず、関門(表が見つからない)で保留になる
        pages = [dict(p) for p in self.pages]
        for p in pages:
            p.pop("w", None)
            p.pop("h", None)
        parsed = pn.parse_yoshinoya(pages)
        self.assertEqual(parsed["pages_table"], 0)
        self.assertIn("表の見出し行", pn.validate_source("yoshinoya", parsed, min_rows=3))

    def test_unrelated_layout_is_held_back(self):
        pages = [{"texts": [[10.0, 10.0, 80.0, 20.0, "吉野家メニュー情報\u3000263号"],
                            [10.0, 30.0, 80.0, 40.0, "牛丼"], [90.0, 30.0, 110.0, 40.0, "633"]],
                  "rules": [[10, 5, 500, 5.5]], "chars": [], "w": 595.3, "h": 841.9}]
        parsed = pn.parse_yoshinoya(pages)
        self.assertEqual(parsed["pages_table"], 0)
        self.assertIsNotNone(pn.validate_source("yoshinoya", parsed, min_rows=3))


class TestMatchingAndBuild(unittest.TestCase):
    def setUp(self):
        self.rows = pn.parse_yoshinoya(yoshinoya_pages())["rows"]

    def test_exact_match_only(self):
        self.assertIsNotNone(pn.rows_for_name(self.rows, "ひだまり丼"))
        self.assertIsNotNone(pn.rows_for_name(self.rows, "ひだまり　丼"))
        self.assertIsNone(pn.rows_for_name(self.rows, "ひだまり"))
        self.assertIsNone(pn.rows_for_name(self.rows, "ひだまり丼大盛"))

    def test_wave_dash_variants_match(self):
        rows = [{"name": "とろ～り丼", "size": None, "ok": True, "block": "1-0", "kcal": 1, "protein_g": 0,
                 "fat_g": 0, "carbs_g": 0, "salt_g": 0}]
        self.assertIsNotNone(pn.rows_for_name(rows, "とろ〜り丼"))

    def test_same_name_in_two_blocks_with_different_values_is_ambiguous(self):
        r1 = {"name": "ふたご", "size": "並盛", "ok": True, "block": "1-0", "kcal": 500, "protein_g": 1.0,
              "fat_g": 1.0, "carbs_g": 1.0, "salt_g": 1.0}
        r2 = dict(r1, block="1-1", kcal=600)
        self.assertIsNone(pn.rows_for_name([r1, r2], "ふたご"))
        self.assertIsNotNone(pn.rows_for_name([r1, dict(r1, block="1-1")], "ふたご"))

    def test_build_nutrition_representative_is_namimori_first(self):
        rows = pn.rows_for_name(self.rows, "ひだまり丼")
        nut, variants, dropped = pn.build_nutrition(rows)
        self.assertEqual(dropped, 0)
        self.assertEqual(nut["protein_g"], 19.0)           # 並盛が代表
        self.assertEqual([v["label"] for v in variants], ["並盛", "小盛", "大盛"])  # 代表が先頭
        self.assertEqual(variants[0]["nutrition"], nut)

    def test_build_nutrition_without_namimori_uses_first(self):
        rows = pn.rows_for_name(self.rows, "こもれび定食")
        nut, variants, _ = pn.build_nutrition(rows)
        self.assertEqual(nut["protein_g"], 33.0)
        self.assertIsNone(variants)                         # 1行だけなら一覧は作らない

    def test_build_nutrition_drops_failed_rows(self):
        rows = [dict(r) for r in pn.rows_for_name(self.rows, "ひだまり丼")]
        rows[2]["ok"] = False
        nut, variants, dropped = pn.build_nutrition(rows)
        self.assertEqual(dropped, 1)
        self.assertEqual(len(variants), 2)
        rows[0]["ok"] = rows[1]["ok"] = False
        self.assertEqual(pn.build_nutrition(rows), (None, None, 3))

    def test_allergens_union_is_safe_side(self):
        names = ["小麦", "乳", "卵"]
        al = pn.allergens_from_marks(names, [["○", None, "▲"], [None, "▲", "○"]], "○", "▲")
        self.assertEqual(al["contains"], ["小麦", "卵"])                  # どれかのサイズで含むなら含む
        self.assertEqual(al["partial"], [{"name": "乳", "mark": "▲"}])


class TestValidate(unittest.TestCase):
    def test_sukiya_ok_and_failures(self):
        parsed = pn.parse_sukiya_nutrition(sukiya_pages())
        self.assertIsNone(pn.validate_source("sukiya_nutrition", parsed, min_rows=3))
        self.assertIn("少なすぎ", pn.validate_source("sukiya_nutrition", parsed, min_rows=100))
        self.assertIn("見出し", pn.validate_source("sukiya_nutrition", {"pages_table": 0, "rows": []}))
        bad = dict(parsed, rows=[dict(r, ok=False) for r in parsed["rows"]])
        self.assertIn("検算", pn.validate_source("sukiya_nutrition", bad, min_rows=3))
        self.assertIn("更新日", pn.validate_source("sukiya_nutrition", dict(parsed, updated=None), min_rows=3))

    def test_yoshinoya_needs_issue_and_date(self):
        parsed = pn.parse_yoshinoya(yoshinoya_pages())
        self.assertIsNone(pn.validate_source("yoshinoya", parsed, min_rows=3))
        self.assertIn("号数", pn.validate_source("yoshinoya", dict(parsed, issue=None), min_rows=3))

    def test_allergen_validation(self):
        pages = [fx.sukiya_allergen_page([(1, "ひだまり牛丼", "●" + "-" * 27)])]
        parsed = pn.parse_sukiya_allergen(pages)
        self.assertIsNone(pn.validate_source("sukiya_allergen", parsed, min_rows=1))
        self.assertIn("少なすぎ", pn.validate_source("sukiya_allergen", parsed, min_rows=50))


class TestExtractGeometryWithoutPdfminer(unittest.TestCase):
    def test_import_error_is_clear(self):
        import importlib.util
        if importlib.util.find_spec("pdfminer") is not None:
            self.skipTest("pdfminer.six が入っている環境では確かめられない")
        with self.assertRaises(ImportError):
            pn.extract_geometry(b"%PDF-1.4")


if __name__ == "__main__":
    unittest.main()
