"""PDFから読み取った栄養・アレルゲンの表示(generate_site)と、crawl.run_pdf_step の結線のテスト。
実PDFが .cache/ にあるときだけ走る結合テストも含む(無ければskip。CIでは走らない)。"""

import json
import unittest
from pathlib import Path

import helpers  # noqa: F401
from helpers import TempData
import crawl
import generate_site as g
import pdf_nutrition as pn
import pdf_update as pu
import pdf_fixtures as fx
from test_pdf_update import FakePdfFetcher, SK_LABELS, SK_URLS, YO_URLS, sk_allergen_pages, sk_nutrition_pages, yo_pages, extractor
from test_sukiya import sukiya_item

NUT = {"kcal": 582.0, "protein_g": 15.0, "fat_g": 18.0, "carbs_g": 90.0, "sugar_g": None, "fiber_g": None,
       "salt_g": 3.0, "other": {}}
NUT_L = dict(NUT, kcal=700.0, protein_g=20.0)
SRC = {"url": SK_URLS["nutrition"], "label": "栄養成分一覧", "version": "2026-09-29", "version_text": "2026年9月29日更新",
       "fetched_at": "2026-10-12T07:30:00+09:00"}
ASRC = dict(SRC, url=SK_URLS["allergen"], label="アレルゲン一覧")


def with_nutrition(**kw):
    base = dict(nutrition=NUT, nutrition_variants=[{"label": "並盛", "nutrition": NUT}, {"label": "大盛", "nutrition": NUT_L}],
                nutrition_source=SRC, nutrition_note="表示した数値には、お好みでお使いいただく調味料は含まれていません。",
                allergens={"contains": ["小麦", "乳"], "partial": [{"name": "卵", "mark": "△"}]},
                allergens_conditional={"mark": "△", "meaning": "原材料の製造工場で同じラインを使う"},
                allergen_source=ASRC)
    base.update(kw)
    return sukiya_item(**base)


class TestRender(unittest.TestCase):
    def test_nutrition_table_allergens_note_and_source(self):
        h = g.render_item_page(with_nutrition())
        self.assertIn("<h3>栄養成分</h3>", h)
        self.assertLess(h.index("<th>並盛</th>"), h.index("<th>大盛</th>"))      # 代表サイズが先頭
        self.assertIn("<td>582kcal</td>", h)
        self.assertIn("アレルゲン（含む）：</strong>小麦、乳", h)
        self.assertIn("卵△（△：原材料の製造工場で同じラインを使う）", h)
        self.assertIn("公式の注記：表示した数値には", h)
        self.assertIn("出典：公式PDF", h)
        self.assertIn("2026年9月29日更新", h)
        self.assertIn("nutrition.pdf", h)
        self.assertNotIn("取り込んでいません", h)
        self.assertIn("<title>すき家「月見すきやき牛丼」のカロリー・価格・栄養成分", h)

    def test_unmatched_product_keeps_pdf_link_wording(self):
        h = g.render_item_page(sukiya_item())
        self.assertIn("栄養成分・アレルギー情報は公式PDF（栄養成分一覧／アレルゲン一覧）をご覧ください。当サイトでは取り込んでいません", h)
        self.assertIn("の価格・販売期間", h)

    def test_nutrition_without_allergen_points_to_pdf(self):
        h = g.render_item_page(with_nutrition(allergens=None, allergens_conditional=None, allergen_source=None))
        self.assertIn("アレルギー情報は公式PDF", h)
        self.assertIn("allergen.pdf", h)

    def test_allergen_without_nutrition(self):
        h = g.render_item_page(with_nutrition(nutrition=None, nutrition_variants=None, nutrition_source=None))
        self.assertIn("栄養成分は公式PDF", h)
        self.assertIn("アレルゲン（含む）", h)

    def test_yoshinoya_single_pdf(self):
        it = with_nutrition(chain="yoshinoya", nutrition_source=dict(SRC, url="https://www.yoshinoya.com/pdf/allergy/",
                            label="吉野家メニュー情報", version_text="262号・2026年10月8日現在"),
                            allergen_source=None, allergen_pdf_url=None,
                            allergens={"contains": ["小麦"], "partial": [{"name": "えび", "mark": "▲"}]},
                            allergens_conditional={"mark": "▲", "meaning": "原材料「あおのり」には『えび』が混ざる"})
        h = g.render_item_page(it)
        self.assertIn("262号・2026年10月8日現在", h)
        self.assertIn("えび▲（▲：原材料「あおのり」には『えび』が混ざる）", h)

    def test_about_text(self):
        about = "".join(g.CONFIG["about"])
        self.assertIn("吉野家とすき家の栄養成分は、公式PDFから読み取っています。", about)
        self.assertNotIn("リンクで案内", about)
        self.assertLess(len(about), 700)


class TestCrawlStep(unittest.TestCase):
    def test_noop_with_fake_getter_without_pdf_methods(self):
        self.assertIsNone(crawl.run_pdf_step("sukiya", object(), {}, {"sukiya": {}}))

    def test_noop_when_list_was_blocked(self):
        self.assertIsNone(crawl.run_pdf_step("sukiya", FakePdfFetcher({}), {}, {"sukiya": {"blocked": True}}))

    def test_saves_products_and_state(self):
        cfg = {"nutrition": SK_URLS["nutrition"], "nutrition_label": "栄養成分一覧",
               "allergen": SK_URLS["allergen"], "allergen_label": "アレルゲン一覧"}
        fetcher = FakePdfFetcher({SK_URLS["nutrition"]: sk_nutrition_pages(), SK_URLS["allergen"]: sk_allergen_pages()})
        with TempData() as base:
            crawl.PDF_STATE_PATH = base / "pdf_state.json"
            saved_cfg, saved_extract, saved_cache, saved_min = (crawl.CONFIG.get("sukiya_pdf"), pn.extract_geometry,
                                                                 pu.CACHE_DIR, pn.validate_source)
            try:
                crawl.CONFIG["sukiya_pdf"] = cfg
                pn.extract_geometry = extractor(fetcher)
                pu.CACHE_DIR = base / "cache"
                saved_have, pu.have_pdfminer = pu.have_pdfminer, lambda: True   # 実物のpdfminerが無い環境でも結線を確かめる
                pn.validate_source = lambda kind, parsed, min_rows=80, min_rate=0.8: saved_min(kind, parsed, 3, min_rate)
                crawl.save_product("sukiya", {"chain": "sukiya", "product_id": "a", "name": "ひだまり牛丼",
                                              "nutrition": None, "allergens": None})
                pending, stats = {}, {"sukiya": {}}
                rep = crawl.run_pdf_step("sukiya", fetcher, pending, stats)
                saved = crawl.load_product("sukiya", "a")
                state = json.loads(crawl.PDF_STATE_PATH.read_text(encoding="utf-8"))
            finally:
                crawl.CONFIG["sukiya_pdf"] = saved_cfg
                pn.extract_geometry, pu.CACHE_DIR, pn.validate_source = saved_extract, saved_cache, saved_min
                pu.have_pdfminer = saved_have
                crawl.PDF_STATE_PATH = crawl.DATA / "pdf_state.json"
        self.assertEqual(saved["nutrition"]["protein_g"], 15.0)
        self.assertEqual(state["sukiya"]["nutrition"]["version"], "2026-09-29")
        self.assertNotIn("rows", json.dumps(state))             # 状態にはPDFの内容を入れない
        self.assertEqual(stats["sukiya"]["pdf"]["get_requests"], 2)
        self.assertEqual(rep["matched"], 1)


ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
HAVE_REAL = all((CACHE / n).exists() for n in ("sukiya_nutrition.pdf", "sukiya_allergen.pdf", "yoshinoya.pdf"))


@unittest.skipUnless(HAVE_REAL and pu.have_pdfminer(), "実PDF(.cache/)かpdfminer.sixが無い(CIでは走らない)")
class TestRealPdf(unittest.TestCase):
    """実物のPDFを読み取れるかの結合テスト。実データの中身は検証に使わず、構造と割合だけを確かめる。"""

    def test_sukiya_nutrition(self):
        parsed = pn.parse_sukiya_nutrition(pn.extract_geometry((CACHE / "sukiya_nutrition.pdf").read_bytes()))
        self.assertIsNone(pn.validate_source("sukiya_nutrition", parsed))
        self.assertGreater(pn.pass_rate(parsed["rows"]), 0.9)

    def test_sukiya_allergen(self):
        parsed = pn.parse_sukiya_allergen(pn.extract_geometry((CACHE / "sukiya_allergen.pdf").read_bytes()))
        self.assertIsNone(pn.validate_source("sukiya_allergen", parsed))
        self.assertEqual(len(parsed["allergen_names"]), 28)

    def test_yoshinoya(self):
        parsed = pn.parse_yoshinoya(pn.extract_geometry((CACHE / "yoshinoya.pdf").read_bytes(), with_chars=True))
        self.assertIsNone(pn.validate_source("yoshinoya", parsed))
        self.assertEqual(len(parsed["allergen_names"]), 28)
        self.assertGreater(pn.pass_rate(parsed["rows"]), 0.9)


if __name__ == "__main__":
    unittest.main()
