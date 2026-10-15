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
        # 本物のconfigに依存しない(GA4のIDを入れるとこのテストが落ちていた)。
        # 空にした状態を明示的に作って、何も出ないことを確かめる
        from unittest import mock
        with mock.patch.dict(g.CONFIG, {"adsense_client_id": "", "adsense_ad_slot": "",
                                         "ga_measurement_id": ""}):
            h = g.render_item_page(item())
        self.assertNotIn("adsbygoogle", h)
        self.assertNotIn("googletagmanager", h)

    def test_ga4_tag_present_when_set(self):
        from unittest import mock
        with mock.patch.dict(g.CONFIG, {"ga_measurement_id": "G-TEST123"}):
            h = g.render_item_page(item())
        self.assertIn("googletagmanager.com/gtag/js?id=G-TEST123", h)


class TestAdSlots(unittest.TestCase):
    """広告(AdSense)。ヘッダー下1枠＋カード一覧の中(JSが差し込む。最大ADS_MAX_PER_GRID枠)、商品ページは1枠。"""

    def setUp(self):
        from unittest import mock
        p = mock.patch.dict(g.CONFIG, {"adsense_client_id": "ca-pub-1", "adsense_ad_slot": "2"})
        p.start()
        self.addCleanup(p.stop)
        self.items = [item(chain=c, product_id=f"{c}{i}") for c in ("mcdonalds", "mos", "matsuya") for i in range(3)]

    def pages(self):
        return {
            "grid": g.render_grid_page("週", "d", self.items, "https://x/"),
            "top": g.render_top(self.items),
            "item": g.render_item_page(self.items[0]),
        }

    def test_config_has_adsense_values(self):
        real = json.loads((Path(g.ROOT) / "config.json").read_text(encoding="utf-8"))
        self.assertTrue(real["adsense_client_id"].startswith("ca-pub-"))
        self.assertTrue(real["adsense_ad_slot"])

    def test_head_script_and_vignette_opt_out(self):
        for name, h in self.pages().items():
            self.assertIn("adsbygoogle.js?client=ca-pub-1", h.split("</head>")[0], name)
            self.assertNotIn("<a href", h, name)
            self.assertNotIn("<a class", h, name)

    def test_no_server_side_ad_inside_sortable_grid(self):
        for name, h in self.pages().items():
            for m in re.finditer(r'<div class="grid" data-sortable>(.*?)\n</div>\n', h, re.S):
                self.assertNotIn("ad-slot", m.group(1), name)
                self.assertNotIn("adsbygoogle", m.group(1), name)

    def test_grid_pages_have_ad_js_template_and_constants(self):
        for name in ("grid", "top"):
            h = self.pages()[name]
            self.assertIn("function syncAds()", h, name)
            self.assertIn(f"var AD_EVERY = {g.ADS_EVERY};", h, name)
            self.assertIn(f"var AD_MAX = {g.ADS_MAX_PER_GRID};", h, name)
            self.assertEqual(h.count('<template id="ad-template">'), 1, name)
            self.assertNotIn("__ADS_", h, name)
            self.assertGreater(h.rindex("syncAds();"), h.index("var AD_EVERY"), name)
            # ヘッダー下1枠 + 雛形1つ。実際の差し込み(最大3枠)はJS
            self.assertEqual(h.split("<body>")[1].count('<ins class="adsbygoogle"'), 2, name)
        self.assertEqual((g.ADS_EVERY, g.ADS_MAX_PER_GRID), (12, 3))

    def test_item_page_has_one_ad(self):
        h = self.pages()["item"]
        self.assertEqual(h.split("<body>")[1].count('<ins class="adsbygoogle"'), 1)
        self.assertNotIn("<template", h)
        self.assertEqual(h.count("adsbygoogle || []).push"), 1)  # syncAdsの1回だけ(枠ごとのinline pushなし)
        h2 = g.render_item_page(item(nutrition=None, allergens=None, price_history=[]))
        self.assertEqual(h2.split("<body>")[1].count('<ins class="adsbygoogle"'), 1)

    def test_nothing_when_unset(self):
        from unittest import mock
        with mock.patch.dict(g.CONFIG, {"adsense_client_id": "", "adsense_ad_slot": ""}):
            for name, h in self.pages().items():
                self.assertNotIn("adsbygoogle", h.replace(g.CSS, ""), name)
                self.assertNotIn("function syncAds", h, name)
                self.assertNotIn("<template", h, name)
                self.assertNotIn('class="ad-slot"', h, name)
                self.assertNotIn("data-google-vignette", h, name)

    def test_one_side_unset_outputs_no_slot(self):
        from unittest import mock
        for key in ("adsense_client_id", "adsense_ad_slot"):
            with mock.patch.dict(g.CONFIG, {key: ""}):
                for name, h in self.pages().items():
                    self.assertNotIn('class="ad-slot"', h, name)

    def test_no_ads_on_empty_pages(self):
        self.assertNotIn('class="ad-slot"', g.render_grid_page("週", "d", [], "https://x/"))

    def test_scripts_are_valid_javascript(self):
        import os, shutil, subprocess, tempfile
        node = shutil.which("node") or "/opt/homebrew/opt/node@22/bin/node"
        if not os.path.exists(node):
            self.skipTest("node がない")
        for m in re.finditer(r"<script>(.*?)</script>", self.pages()["top"], re.S):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
                f.write(m.group(1))
            try:
                r = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
            finally:
                os.unlink(f.name)


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
        # 広告設定済みだと全<a>にdata-google-vignetteが付くので、外してから見る
        h = g.render_top([item(), item(product_id="2")]).replace(' data-google-vignette="false"', "")
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
        h = g.render_top([item()]).replace(' data-google-vignette="false"', "")
        self.assertIn("ファスト飯", h)
        self.assertIn('<a href="https://netaful.jp/">ネタフル</a>', h)
        self.assertIn("非公式サイト", h)
        # 「ケンタッキーなど対象外」の1文は、aboutを短くするため2026-10-11に削除した


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


class TestUpdatedLabel(unittest.TestCase):
    """トップだけに「最終更新: 2026年10月14日 07:37」(日本時間・電書/家電/林檎ポチと同じ書式)を出す。"""

    def test_label_format_is_jst(self):
        import datetime
        utc = datetime.datetime(2026, 10, 13, 22, 37, tzinfo=datetime.timezone.utc)  # 日本時間 10/14 07:37
        self.assertEqual(g.updated_label(utc.astimezone(g.JST)), "最終更新: 2026年10月14日 07:37")

    def test_top_has_label_but_other_pages_do_not(self):
        top = g.render_top([item()])
        self.assertIn("最終更新: ", top)
        shell = g.page_shell("x", "d", "<p>b</p>", "https://example.invalid/")
        self.assertNotIn("最終更新", shell)  # 全ページに入れると毎日すべてのファイルが変わる


class TestCardLinksAreRootAbsolute(unittest.TestCase):
    """カードのリンクが /items/… になっている(階層のあるページ /chains/x/ などで404にならない)。"""

    def test_card_href_starts_with_slash(self):
        card = g.render_card(item())
        self.assertIn('href="/items/', card)
        self.assertNotIn('href="items/', card)

    def test_grid_page_has_no_relative_item_links(self):
        html = g.render_grid_page("チェーン", "d", [item()], "https://x/")
        self.assertNotIn('href="items/', html)


if __name__ == "__main__":
    unittest.main()
