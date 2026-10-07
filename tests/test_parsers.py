import json
import unittest

import helpers  # noqa: F401  (sys.path設定)
import parsers
from helpers import fx


class TestShortenDescription(unittest.TestCase):
    def test_first_sentence_only(self):
        s, n = parsers.shorten_description("一文目です。二文目です。三文目です。")
        self.assertEqual(s, "一文目です。")
        self.assertEqual(n, len("一文目です。二文目です。三文目です。"))

    def test_max_100_chars(self):
        long = "あ" * 150 + "。続き。"
        s, n = parsers.shorten_description(long)
        self.assertEqual(len(s), 100)
        self.assertTrue(s.endswith("…"))

    def test_no_period_is_capped(self):
        s, _ = parsers.shorten_description("い" * 130)
        self.assertEqual(len(s), 100)

    def test_skips_leading_period_note_and_html(self):
        s, _ = parsers.shorten_description("【2026年9月9日（水）〜】<br>本文です。<br>※注記")
        self.assertEqual(s, "本文です。")

    def test_mos_explain_period_text_not_stored(self):
        text = "説明の一文目です。二文目。 \n<br>\n<br>\n【2026年9月9日（水）〜2026年11月上旬頃までの販売予定】"
        s, _ = parsers.shorten_description(text)
        self.assertEqual(s, "説明の一文目です。")
        self.assertNotIn("販売予定", s)

    def test_empty(self):
        self.assertEqual(parsers.shorten_description(None), (None, 0))
        self.assertEqual(parsers.shorten_description("  "), (None, 0))


class TestNumbers(unittest.TestCase):
    def test_to_number(self):
        self.assertEqual(parsers.to_number("401kcal"), 401.0)
        self.assertEqual(parsers.to_number("21.0g"), 21.0)
        self.assertEqual(parsers.to_number("0"), 0.0)
        self.assertIsNone(parsers.to_number("-"))  # 未分析
        self.assertIsNone(parsers.to_number("－"))
        self.assertIsNone(parsers.to_number(""))

    def test_build_nutrition_zero_is_not_none(self):
        n = parsers.build_nutrition([("エネルギー", "10kcal"), ("糖類", "0"), ("食物繊維", "-")])
        self.assertEqual(n["sugar_g"], 0.0)
        self.assertIsNone(n["fiber_g"])
        self.assertIsNone(parsers.build_nutrition([]))


class TestMcdList(unittest.TestCase):
    def setUp(self):
        self.menu = parsers.parse_mcd_list(fx("mcdonalds/menu.html"))
        self.burger = parsers.parse_mcd_list(fx("mcdonalds/cat_burger.html"))

    def test_counts(self):
        self.assertEqual(len(self.menu), 32)  # おすすめ。重複(セット)を含む生の件数
        self.assertEqual(len(self.burger), 49)

    def test_product_code_matches_detail_url(self):
        e = {x["product_id"]: x for x in self.burger}["6900"]
        self.assertEqual(e["detail_url"], "/products/6900/")
        self.assertEqual(e["name"], "月見バーガー")
        self.assertEqual(e["price"], 460)
        self.assertTrue(e["limited"])
        self.assertEqual(e["list_started_at"], "2026-08-18")

    def test_hidden_badge_is_not_limited(self):
        e = {x["product_id"]: x for x in self.burger}["1210"]
        self.assertFalse(e["limited"])
        self.assertIsNone(e["list_started_at"])

    def test_limited_in_menu_page(self):
        lim = {x["product_id"] for x in self.menu if x["limited"]}
        self.assertIn("2520", lim)
        self.assertIn("801705", lim)
        self.assertNotIn("2810", lim)

    def test_entity_decoded_name(self):
        e = {x["product_id"]: x for x in self.menu}["2530"]
        self.assertIn("&", e["name"])
        self.assertNotIn("&amp;", e["name"])

    def test_set_detection(self):
        self.assertTrue(parsers.is_mcd_set("月見バーガー セット"))
        self.assertTrue(parsers.is_mcd_set("月見マフィンコンビ"))
        self.assertTrue(parsers.is_mcd_set("ひるまック ビッグマック® (50円)募金付きセット"))
        self.assertFalse(parsers.is_mcd_set("月見バーガー"))
        self.assertFalse(parsers.is_mcd_set("食べくらべポテナゲ大"))


class TestMcdDetail(unittest.TestCase):
    def test_full_detail(self):
        d = parsers.parse_mcd_detail(fx("mcdonalds/p_6900.html"))
        self.assertEqual(d["name"], "月見バーガー")
        self.assertEqual(d["price"], 460)
        self.assertEqual(d["mop_start"], "2026-09-02")
        self.assertEqual(d["mop_end"], "2026-10-21")
        n = d["nutrition"]
        self.assertEqual(n["kcal"], 401.0)
        self.assertEqual(n["protein_g"], 21.0)
        self.assertEqual(n["fat_g"], 22.3)
        self.assertEqual(n["carbs_g"], 28.5)
        self.assertEqual(n["sugar_g"], 5.0)
        self.assertEqual(n["salt_g"], 1.8)
        self.assertEqual(n["other"]["飽和脂肪酸"], "6.54g")
        self.assertIn("卵", d["allergens"]["contains"])
        self.assertIn("牛肉", d["allergens"]["contains"])
        self.assertNotIn("えび", d["allergens"]["contains"])
        self.assertEqual(d["origin"][0]["name"], "バンズ")
        self.assertIn("製造所所在地", d["origin"][0]["origin"])

    def test_no_nutrition_is_null(self):
        d = parsers.parse_mcd_detail(fx("mcdonalds/p_801705.html"))
        self.assertIsNone(d["nutrition"])
        self.assertIsNone(d["allergens"])
        self.assertIsNone(d["origin"])
        self.assertIsNone(d["description"])
        self.assertEqual(d["price"], 630)

    def test_not_a_product_page(self):
        self.assertIsNone(parsers.parse_mcd_detail("<html><body>not found</body></html>"))


class TestMos(unittest.TestCase):
    def setUp(self):
        self.entries = parsers.parse_mos_list(fx("mos/data_menu_menu_category_26.json"), "26")
        self.by_id = {e["product_id"]: e for e in self.entries}
        self.amap = parsers.parse_mos_allergy_map(fx("mos/mst_allergy.json"))

    def test_ids_are_strings_with_letters(self):
        self.assertEqual(len(self.entries), 13)
        self.assertIn("010488", self.by_id)  # 先頭のゼロを落とさない
        self.assertIn("P00060", self.by_id)

    def test_limited_kinds(self):
        self.assertEqual(self.by_id["010488"]["limited_kinds"], ["期間限定"])
        self.assertEqual(self.by_id["483208"]["limited_kinds"], ["期間限定"])  # tags="1,20"。20は限定ではない
        self.assertEqual(self.by_id["710169"]["limited_kinds"], ["数量限定"])  # tags="21,14"
        self.assertEqual(self.by_id["011293"]["limited_kinds"], [])  # tags が None

    def test_period(self):
        launch, end = parsers.parse_mos_period(self.by_id["010488"]["explain"])
        self.assertEqual(launch, "2026-09-09")
        self.assertEqual(end, "2026年11月上旬頃まで")

    def test_period_quantity_limited(self):
        launch, end = parsers.parse_mos_period(self.by_id["011576"]["explain"])
        self.assertEqual(launch, "2026-07-15")
        self.assertIn("数量限定", end)

    def test_period_missing(self):
        self.assertEqual(parsers.parse_mos_period(self.by_id["011293"]["explain"]), (None, None))
        self.assertEqual(parsers.parse_mos_period(None), (None, None))

    def test_nutrition(self):
        n = parsers.mos_nutrition(self.by_id["010488"])
        self.assertEqual(n["kcal"], 520.0)
        self.assertEqual(n["salt_g"], 3.7)
        self.assertEqual(n["other"]["ナトリウム"], "1445mg")
        self.assertGreaterEqual(len(n["other"]), 10)

    def test_no_nutrition(self):
        self.assertIsNone(parsers.mos_nutrition(self.by_id["P00060"]))

    def test_allergy_map_and_contains(self):
        self.assertEqual(self.amap["5"], "卵")
        a = parsers.mos_allergens(self.by_id["010488"], self.amap)
        self.assertEqual(set(a["contains"]), {"乳", "小麦", "卵", "大豆", "鶏肉", "豚肉", "りんご"})

    def test_origin(self):
        o = parsers.mos_origin(self.by_id["010488"])
        self.assertEqual(o[0]["name"], "フォカッチャ：小麦粉（小麦）")


if __name__ == "__main__":
    unittest.main()
