import json
import re
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
import generate_site as g


def item(**kw):
    base = {
        "chain": "mcdonalds", "product_id": "9001", "name": "テストバーガー", "category": "バーガー",
        "price": 460, "price_note": "税込。「~」は最低価格表記", "launch_date": "2026-09-02",
        "planned_end_text": "2026-10-21（モバイルオーダー受付期間の終了日）",
        "description": "テストの説明文です。",
        "nutrition": {"kcal": 401.0, "protein_g": 21.0, "fat_g": 22.3, "carbs_g": 28.5, "sugar_g": 5.0,
                      "fiber_g": None, "salt_g": 1.8, "other": {"飽和脂肪酸": "6.54g"}},
        "allergens": {"contains": ["卵", "乳"], "partial": []},
        "origin": [{"name": "バンズ", "origin": "主要原料：小麦粉／アメリカ"}],
        "limited_kind": "期間限定", "official_url": "https://www.mcdonalds.co.jp/products/9001/",
        "first_seen_at": "2026-10-07T07:00:00+09:00", "last_seen_at": "2026-10-07",
        "status": "on_sale", "ended_observed_at": None,
        "price_history": [{"date": "2026-10-07", "price": 460}], "image": None,
    }
    base.update(kw)
    return base


class TestSeo(unittest.TestCase):
    def test_title_with_kcal(self):
        self.assertEqual(g.seo_title(item()), "マクドナルド「テストバーガー」のカロリー・価格・栄養成分")

    def test_title_without_nutrition(self):
        self.assertEqual(g.seo_title(item(nutrition=None, chain="mos", name="新作")),
                         "モスバーガー「新作」の価格・販売期間")

    def test_description_has_chain_kcal_price(self):
        d = g.seo_description(item())
        self.assertIn("マクドナルド", d)
        self.assertIn("401kcal", d)
        self.assertIn("460円", d)
        self.assertLessEqual(len(d), 200)

    def test_page_title_has_site_name(self):
        h = g.render_item_page(item())
        self.assertIn("<title>マクドナルド「テストバーガー」のカロリー・価格・栄養成分｜ファストフードポチ</title>", h)


class TestPeriod(unittest.TestCase):
    def test_on_sale(self):
        self.assertEqual(g.period_text(item()), "2026-09-02〜 販売中")
        self.assertIn("終了予定", g.period_text(item(), detailed=True))

    def test_ended(self):
        it = item(status="ended", ended_observed_at="2026-10-21")
        self.assertEqual(g.period_text(it), "2026-09-02〜2026-10-21")
        self.assertIn("消えたことを確認した日", g.period_text(it, detailed=True))

    def test_unknown_launch(self):
        self.assertIn("記録開始日", g.period_text(item(launch_date=None)))


class TestItemPage(unittest.TestCase):
    def test_quote_block(self):
        h = g.render_item_page(item())
        self.assertIn('<blockquote cite="https://www.mcdonalds.co.jp/products/9001/">テストの説明文です。</blockquote>', h)
        self.assertIn("出典：マクドナルド公式サイト（2026-10-07取得時点）", h)
        self.assertIn("現在は公式ページが削除されている場合があります", h)

    def test_nutrition_inline_for_few_other(self):
        h = g.render_item_page(item())
        self.assertIn("401kcal", h)
        self.assertIn("飽和脂肪酸", h)
        self.assertNotIn("<details><summary>そのほかの栄養成分", h)

    def test_nutrition_details_for_many_other(self):
        other = {f"項目{i}": f"{i}mg" for i in range(10)}
        h = g.render_item_page(item(chain="mos", nutrition={"kcal": 520.0, "other": other}))
        self.assertIn("そのほかの栄養成分（10項目）", h)

    def test_no_nutrition_note(self):
        h = g.render_item_page(item(nutrition=None, allergens=None, origin=None))
        self.assertIn("栄養成分は公式サイトに掲載がありませんでした", h)

    def test_price_history_only_when_changed(self):
        self.assertNotIn("価格の履歴", g.render_item_page(item()))
        hist = [{"date": "2026-10-07", "price": 460}, {"date": "2026-10-20", "price": 480}]
        h = g.render_item_page(item(price_history=hist, price=480))
        self.assertIn("価格の履歴", h)
        self.assertIn("2026-10-20〜", h)

    def test_no_image(self):
        h = g.render_item_page(item())
        for src in re.findall(r'<img[^>]*src="([^"]*)"', h):
            self.assertNotIn("mcdonalds.co.jp", src)  # 公式画像は表示しない
            self.assertNotIn("mos.jp", src)
            self.assertNotIn("product_images", src)

    def test_no_adsense_when_unset(self):
        self.assertEqual(g.CONFIG["adsense_client_id"], "")
        h = g.render_item_page(item())
        self.assertNotIn("adsbygoogle", h)
        self.assertNotIn("googletagmanager", h)  # GA4も空の間は何も出さない


class TestTop(unittest.TestCase):
    def test_website_jsonld(self):
        h = g.render_top([item()])
        m = re.search(r'<script type="application/ld\+json">(.*?)</script>', h, re.S)
        ld = json.loads(m.group(1))
        self.assertEqual(ld["@type"], "WebSite")
        self.assertEqual(ld["name"], "ファストフードポチ")
        self.assertEqual(h.count("application/ld+json"), 1)

    def test_favicon_sizes(self):
        h = g.render_top([item()])
        self.assertIn('<link rel="icon" type="image/png" sizes="96x96" href="/assets/favicon.png">', h)

    def test_favicon_file_is_96px(self):
        import struct
        data = (Path(g.ROOT) / "docs/assets/favicon.png").read_bytes()
        w, h = struct.unpack(">II", data[16:24])
        self.assertEqual((w, h), (96, 96))

    def test_groups_by_chain_and_order(self):
        a = item(product_id="1", name="古い", launch_date="2026-09-01")
        b = item(product_id="2", name="新しい", launch_date="2026-10-01")
        c = item(chain="mos", product_id="3", name="モスの", launch_date="2026-09-15")
        h = g.render_top([a, b, c])
        self.assertLess(h.index("新しい"), h.index("古い"))
        self.assertLess(h.index('data-chain="mcdonalds"'), h.index('data-chain="mos"'))
        self.assertNotIn("社別", h)

    def test_sortable_grid_contains_only_cards(self):
        h = g.render_top([item(), item(product_id="2")])
        for m in re.finditer(r'<div class="grid" data-sortable>\n(.*?)\n</div>\n', h, re.S):
            inner = m.group(1)
            self.assertNotIn("ad-slot", inner)
            self.assertNotIn("<h", inner)
            # 直下の要素は <a class="item"> だけ
            self.assertEqual(len(re.findall(r'<a class="item"', inner)), 2)

    def test_status_filter_default_on_sale_and_ended_hidden_count(self):
        ended = item(product_id="2", status="ended", ended_observed_at="2026-10-06")
        h = g.render_top([item(), ended], today=__import__("datetime").date(2026, 10, 8))
        self.assertIn('<option value="on_sale" selected>', h)
        self.assertIn("販売中の限定メニュー (1件)", h)

    def test_old_ended_not_on_top(self):
        old = item(product_id="2", status="ended", ended_observed_at="2026-01-01", name="昔のメニュー")
        h = g.render_top([item(), old], today=__import__("datetime").date(2026, 10, 8))
        self.assertNotIn("昔のメニュー", h)

    def test_empty_site_has_no_empty_headings(self):
        h = g.render_top([])
        self.assertNotIn("<h3>", h)

    def test_about_mentions_fast_meshi_with_link(self):
        h = g.render_top([item()])
        self.assertIn("ファスト飯", h)
        self.assertIn('<a href="https://netaful.jp/">ネタフル</a>', h)
        self.assertIn("非公式サイト", h)
        self.assertIn("ケンタッキー", h)  # 対象外であることの1文


class TestArchive(unittest.TestCase):
    def test_empty_archive(self):
        h = g.render_archive_index([item()])
        self.assertIn("まだ販売が終わったメニューはありません", h)
        self.assertNotIn("/archive/2026", h)

    def test_months_grouped_by_start_month(self):
        e1 = item(product_id="1", status="ended", ended_observed_at="2026-10-01", launch_date="2026-09-02")
        e2 = item(product_id="2", status="ended", ended_observed_at="2026-10-02", launch_date="2026-09-20")
        e3 = item(product_id="3", status="ended", ended_observed_at="2026-10-03", launch_date="2026-08-18")
        h = g.render_archive_index([e1, e2, e3, item(product_id="4")])
        self.assertIn('/archive/2026-09/', h)
        self.assertIn('/archive/2026-08/', h)
        self.assertLess(h.index("2026-09"), h.index("2026-08"))
        self.assertIn("2026年9月に始まった限定メニュー<span class=\"n\">2件", h)

    def test_card_shows_period(self):
        e = item(status="ended", ended_observed_at="2026-10-21")
        self.assertIn("2026-09-02〜2026-10-21", g.render_card(e))
        self.assertIn('data-status="ended"', g.render_card(e))


class TestOutputs(unittest.TestCase):
    def test_robots(self):
        r = g.generate_robots()
        for bot in ["GPTBot", "CCBot", "Google-Extended", "Applebot-Extended", "anthropic-ai", "ClaudeBot",
                    "Bytespider", "meta-externalagent", "cohere-training-data-crawler", "Diffbot", "Omgilibot"]:
            self.assertIn(f"User-agent: {bot}", r)
        self.assertIn("User-agent: *\nAllow: /", r)
        self.assertNotIn("Googlebot", r)
        self.assertIn("Sitemap: https://fastfood.netaful.jp/sitemap.xml", r)

    def test_rss_and_sitemap(self):
        rss = g.generate_rss([item()])
        self.assertIn("【マクドナルド】テストバーガー", rss)
        sm = g.generate_sitemap([item()], ["2026-09"])
        self.assertIn("https://fastfood.netaful.jp/items/mcdonalds-9001.html", sm)
        self.assertIn("https://fastfood.netaful.jp/archive/2026-09/", sm)

    def test_search_index(self):
        idx = g.build_search_index([item(status="ended")])
        self.assertEqual(idx[0]["s"], "ended")
        self.assertEqual(idx[0]["cn"], "マクドナルド")

    def test_main_generates_and_cleans(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            saved = (g.PRODUCTS, g.DOCS)
            g.PRODUCTS, g.DOCS = tmp / "products", tmp / "docs"
            try:
                (g.PRODUCTS / "mcdonalds").mkdir(parents=True)
                (g.PRODUCTS / "mcdonalds/9001.json").write_text(json.dumps(item()), encoding="utf-8")
                ended = item(product_id="9002", status="ended", ended_observed_at="2026-10-01")
                (g.PRODUCTS / "mcdonalds/9002.json").write_text(json.dumps(ended), encoding="utf-8")
                (g.DOCS / "items").mkdir(parents=True)
                (g.DOCS / "items/mcdonalds-old.html").write_text("x")
                (g.DOCS / "archive/2025-01").mkdir(parents=True)
                g.main()
                self.assertTrue((g.DOCS / "items/mcdonalds-9001.html").exists())
                self.assertFalse((g.DOCS / "items/mcdonalds-old.html").exists())  # 古いページは掃除
                self.assertFalse((g.DOCS / "archive/2025-01").exists())
                self.assertTrue((g.DOCS / "archive/2026-09/index.html").exists())
                self.assertEqual((g.DOCS / "CNAME").read_text().strip(), "fastfood.netaful.jp")
                self.assertTrue((g.DOCS / ".nojekyll").exists())
                # モスは0件でも空の見出しだけのページは出さない(「まだ記録がありません」の文だけ)
                mos = (g.DOCS / "chains/mos/index.html").read_text(encoding="utf-8")
                self.assertNotIn('class="chain-group"', mos)
            finally:
                g.PRODUCTS, g.DOCS = saved


if __name__ == "__main__":
    unittest.main()
