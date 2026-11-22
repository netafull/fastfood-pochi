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
        self.assertIn("出典：マクドナルド公式サイト（2026年10月7日時点）", h)
        self.assertIn("現在は公式ページが削除されている場合があります", h)

    def test_facts_table_and_block_order(self):
        h = g.render_item_page(item(category="バーガー"))
        self.assertIn("<table class='facts'>", h)
        self.assertIn("<th>販売期間</th>", h)
        self.assertIn("<th>カテゴリ</th><td>バーガー</td>", h)
        self.assertNotIn("<strong>販売期間：</strong>", h)
        self.assertLess(h.index("table class='facts'"), h.index("<blockquote"))
        self.assertLess(h.index("<blockquote"), h.index("<p class=\"source\">"))
        self.assertLess(h.index("<p class=\"source\">"), h.index("<h2>栄養成分"))

    def test_source_line_without_description(self):
        h = g.render_item_page(item(description=None))
        self.assertNotIn("<blockquote", h)
        self.assertIn("出典：マクドナルド公式サイト（2026年10月7日時点）", h)
        self.assertIn("公式ページを見る", h)
        self.assertNotIn("取得時点）", h)
        self.assertNotIn("<p class=\"warn\">", h)

    def test_notes_under_hosoku_heading(self):
        h = g.render_item_page(item(chain="matsuya"))
        self.assertIn("<h2>補足</h2>", h)
        self.assertGreater(h.index("<h2>補足</h2>"), h.index("<h2>栄養成分"))
        self.assertGreater(h.index("「期間限定」の表示も販売期間も載せていません"), h.index("<h2>補足</h2>"))
        self.assertNotIn("<h2>補足</h2>", g.render_item_page(item()))

    def test_regular_item_has_source_line(self):
        h = g.render_item_page(item(description=None, limited_kind=None, is_regular=True))
        self.assertIn("出典：マクドナルド公式サイト（", h)

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

    def _run_ad_plan(self, cases):
        import json, os, shutil, subprocess
        node = shutil.which("node") or "/opt/homebrew/opt/node@22/bin/node"
        if not os.path.exists(node):
            self.skipTest("node がない")
        script = (
            g.AD_PLAN_JS
            + "\nconsole.log(JSON.stringify(%s.map(function (c) { return computeAdPlan(c, %d, %d); })));"
            % (json.dumps(cases), g.ADS_EVERY, g.ADS_MAX_PER_GRID)
        )
        r = subprocess.run([node, "-e", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_compute_ad_plan(self):
        cases = [[0], [11], [12], [13], [30], [42, 65, 34, 11, 10],
                 [13, 13, 13, 13, 13], [212], [], [13, 5, 20]]
        got = self._run_ad_plan(cases)
        self.assertEqual(got[0], [[]])
        self.assertEqual(got[1], [[]])
        self.assertEqual(got[2], [[]])          # ちょうどAD_EVERY件は置かない
        self.assertEqual(got[3], [[12]])        # 13件: 末尾の1件手前
        self.assertEqual(got[4], [[12, 24]])    # 30件: 36は範囲外
        self.assertEqual(got[5], [[12], [12], [12], [], []])  # 置ける3社に1枠ずつ
        # 13件以上が5社: 等間隔に3社(0,1,3番目)を選び1枠ずつ
        self.assertEqual(got[6], [[12], [12], [], [12], []])
        self.assertEqual(got[7], [[12, 24, 36]])  # 単一グリッドは3枠
        self.assertEqual(got[8], [])
        self.assertEqual(got[9], [[12], [], [12]])
        for plan, counts in zip(got, cases):
            self.assertLessEqual(sum(len(p) for p in plan), g.ADS_MAX_PER_GRID)
            for p, n in zip(plan, counts):
                self.assertEqual(p, sorted(set(p)))
                for i in p:
                    self.assertTrue(0 < i < n)

    def test_place_ads_never_detaches_ad_slots(self):
        # 枠をDOMから外してよいのは「余った枠を捨てる」1か所だけ。並べ替えは.itemしか動かさない
        js = g.SORT_JS
        self.assertEqual(js.count("removeChild("), 1)
        self.assertIn("slots.pop()", js)
        self.assertNotIn("innerHTML", js)
        self.assertIn("el.classList.contains('ad-slot')", js)
        self.assertNotIn("__AD_PLAN__", js)
        self.assertIn("function computeAdPlan", js)


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
        # 運営者の一文（「運営はブログ『ネタフル』のコグレです」）は2026-11-16にフッター・aboutから外した
        self.assertNotIn("運営はブログ", h)
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

    def test_sitemap_and_no_rss(self):
        self.assertFalse(hasattr(g, "generate_rss"))
        self.assertNotIn("rss", g.render_top([item()]).lower())
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


class TestVisibleBreadcrumbs(unittest.TestCase):
    """見えるパンくず（タイトルの下）。JSON-LD と同じ trail を渡したページだけに出す（2026-11-13追加）。"""

    TRAIL = [("トップ", "https://fastfood.netaful.jp/"),
             ("マクドナルド", "https://fastfood.netaful.jp/chains/mcdonalds/")]

    def test_grid_page_with_crumbs_shows_nav_under_title(self):
        page = g.render_grid_page("マクドナルドの限定メニュー", "説明", [item()],
                                  "https://fastfood.netaful.jp/chains/mcdonalds/", crumbs=self.TRAIL)
        nav = ('<nav class="crumbs" aria-label="パンくずリスト"><a data-google-vignette="false" href="/">トップ</a>'
               ' &gt; <span aria-current="page">マクドナルド</span></nav>')
        self.assertIn(nav, page)
        self.assertLess(page.index('<h1 class="pt">'), page.index(nav))

    def test_grid_page_without_crumbs_has_no_nav(self):
        page = g.render_grid_page("2026年9月に始まった限定メニュー", "説明", [item()],
                                  "https://fastfood.netaful.jp/archive/2026-09/")
        self.assertNotIn('aria-label="パンくずリスト"', page)

    def test_empty_grid_page_with_crumbs_shows_nav(self):
        page = g.render_grid_page("モスバーガーの限定メニュー", "説明", [],
                                  "https://fastfood.netaful.jp/chains/mos/",
                                  crumbs=[("トップ", "https://fastfood.netaful.jp/"),
                                          ("モスバーガー", "https://fastfood.netaful.jp/chains/mos/")])
        self.assertIn('<span aria-current="page">モスバーガー</span>', page)


class TestCardLinksAreRootAbsolute(unittest.TestCase):
    """カードのリンクが /items/… になっている(階層のあるページ /chains/x/ などで404にならない)。"""

    def test_card_href_starts_with_slash(self):
        card = g.render_card(item())
        self.assertIn('href="/items/', card)
        self.assertNotIn('href="items/', card)

    def test_grid_page_has_no_relative_item_links(self):
        html = g.render_grid_page("チェーン", "d", [item()], "https://x/")
        self.assertNotIn('href="items/', html)


class TestSameNameVariants(unittest.TestCase):
    def pair(self):
        old = item(product_id="801706", name="食べくらべポテナゲ特大", launch_date="2026-09-16", status="ended",
                   ended_observed_at="2026-10-22", price=990, nutrition=None)
        new = item(product_id="801784", name="食べくらべポテナゲ特大", launch_date="2026-10-21", price=1000,
                   nutrition=None)
        return old, new

    def test_period_labels(self):
        old, new = self.pair()
        self.assertEqual(g.period_label(old), "2026年9〜10月販売")
        self.assertEqual(g.period_label(new), "2026年10月〜販売中")
        self.assertEqual(g.period_label(item(launch_date=None)), "2026年10月に記録開始")

    def test_variant_labels_use_period_then_price_then_kcal(self):
        old, new = self.pair()
        self.assertEqual(g.variant_labels([old, new]),
                         {"801706": "2026年9〜10月販売", "801784": "2026年10月〜販売中"})
        a = item(product_id="1", launch_date=None, price=429)
        b = item(product_id="2", launch_date=None, price=421, category="テイクアウト")
        self.assertEqual(g.variant_labels([a, b]), {"1": "429円", "2": "テイクアウト用・421円"})
        c = item(product_id="3", launch_date=None, price=250, nutrition=None)
        d = item(product_id="4", launch_date=None, price=250, nutrition={"kcal": 517.0})
        self.assertEqual(g.variant_labels([c, d]), {})      # kcalが片方しか無い=区別できない
        c["nutrition"] = {"kcal": 410.0}
        self.assertEqual(g.variant_labels([c, d]), {"3": "410kcal", "4": "517kcal"})

    def test_page_shows_variant_and_links(self):
        old, new = self.pair()
        h = g.render_item_page(new, [old, new])
        self.assertIn("「食べくらべポテナゲ特大」（2026年10月〜販売中）の価格・販売期間", h)
        self.assertIn('<span class="variant">（2026年10月〜販売中）</span></h1>', h)
        self.assertIn("<h2>同じ名前の商品</h2>", h)
        self.assertIn('href="/items/mcdonalds-801706.html"', h)

    def test_no_variant_without_same_name(self):
        h = g.render_item_page(item(), [item()])
        self.assertIn("<h1>テストバーガー</h1>", h)
        self.assertNotIn("同じ名前の商品", h)
        self.assertIn("<title>マクドナルド「テストバーガー」のカロリー・価格・栄養成分｜", h)


class TestRelatedItems(unittest.TestCase):
    def test_nearest_date_same_category_first_and_excludes_same_name(self):
        me = item(product_id="1", launch_date="2026-10-10")
        near_other_cat = item(product_id="2", name="A", launch_date="2026-10-10", category="サイド")
        far_same_cat = item(product_id="3", name="B", launch_date="2026-05-01")
        same_name = item(product_id="4", launch_date="2026-10-10")
        other_chain = item(product_id="5", name="C", chain="mos", launch_date="2026-10-10")
        got = g.related_items(me, [me, near_other_cat, far_same_cat, same_name, other_chain])
        self.assertEqual([o["product_id"] for o in got], ["3", "2"])

    def test_max_six_and_deterministic(self):
        items = [item(product_id=str(i), name=f"商品{i}", launch_date=f"2026-09-{i:02d}") for i in range(1, 15)]
        a = g.related_items(items[6], items)
        self.assertEqual(len(a), 6)
        self.assertEqual(a, g.related_items(items[6], list(reversed(items))))


class TestLastmodAndChain(unittest.TestCase):
    def test_item_lastmod_ignores_daily_fields(self):
        it = item(first_seen_at="2026-10-07T07:00:00+09:00", last_seen_at="2026-11-08",
                  price_checked_at="2026-11-08",
                  price_history=[{"date": "2026-10-07", "price": 460}, {"date": "2026-10-20", "price": 480}])
        self.assertEqual(g.item_lastmod(it), "2026-10-20")
        it["status"] = "ended"
        it["ended_observed_at"] = "2026-11-01"
        self.assertEqual(g.item_lastmod(it), "2026-11-01")

    def test_sitemap_lastmod_is_not_today(self):
        xml = g.generate_sitemap([item()], [])
        self.assertIn("<lastmod>2026-10-07</lastmod>", xml)

    def test_chain_description_matsuya_not_limited(self):
        c = {"slug": "matsuya", "name": "松屋"}
        its = [item(chain="matsuya", product_id=str(i), name=f"メニュー{i}", launch_date=f"2026-10-0{i}")
               for i in range(1, 5)]
        d = g.chain_seo_description(c, its)
        self.assertNotIn("期間限定", d)
        self.assertIn("「メニュー4」「メニュー3」「メニュー2」", d)
        self.assertLessEqual(len(d), 125)
        self.assertNotIn("期間限定", g.chain_seo_title(c))


class TestHeadings(unittest.TestCase):
    def test_only_top_has_site_name_h1(self):
        top = g.render_top([item()])
        self.assertEqual(top.count("<h1"), 1)
        self.assertIn('<h1><a ', top)
        for page in (g.render_item_page(item(), [item()]), g.render_grid_page("チェーン", "d", [item()], "https://x/"),
                     g.render_archive_index([item(status="ended", ended_observed_at="2026-10-08")]),
                     g.render_chains_top()):
            self.assertEqual(page.count("<h1"), 1)
            self.assertNotIn('<h1><a ', page)

    def test_breadcrumb_json_ld(self):
        h = g.render_item_page(item(), [item()])
        self.assertIn('"@type": "BreadcrumbList"', h)
        self.assertIn('"item": "https://fastfood.netaful.jp/items/mcdonalds-9001.html"', h)


class TestCategoryIcons(unittest.TestCase):
    """カードのカテゴリアイコン: 表にあるカテゴリはその絵、無ければ商品名から推定、決まらなければ出さない。"""

    def test_mapped_category(self):
        h = g.render_card(item(chain="mcdonalds", category="バーガー", name="テストバーガー"))
        self.assertIn('<img class="cat-icon" src="/assets/icons/cat/burger.svg"', h)
        self.assertIn('alt="ハンバーガー"', h)
        self.assertIn('<div class="ib">', h)

    def test_item_page_shows_icon_left_of_h1(self):
        it = item(chain="mcdonalds", category="バーガー", name="テストバーガー")
        h = g.render_item_page(it, [it])
        slug = g.category_icon(it)[0]
        self.assertIn(f'<div class="h1row"><img class="cat-icon" src="/assets/icons/cat/{slug}.svg"', h)
        self.assertLess(h.index('class="cat-icon"'), h.index("<h1>"))
        self.assertIn(f'/assets/icons/cat/{slug}.svg', g.render_card(it))
        self.assertEqual(h.count('class="cat-icon"'), 1)

    def test_item_page_without_icon(self):
        it = item(chain="marugame", category="薬味・トッピング", name="すだち")
        h = g.render_item_page(it, [it])
        self.assertNotIn('class="cat-icon"', h)
        self.assertNotIn("h1row", h.split("</style>")[-1])

    def test_same_category_name_differs_by_chain(self):
        self.assertEqual(g.category_icon(item(chain="matsuya", category="定食", name="x"))[0], "donburi")
        self.assertEqual(g.category_icon(item(chain="marugame", category="うどん", name="x"))[0], "noodles")

    def test_no_category_is_guessed_from_name(self):
        self.assertEqual(g.category_icon(item(chain="katsuya", category=None, name="海老トリプル丼"))[0], "bento")

    def test_mixed_category_uses_name_then_fallback(self):
        self.assertEqual(g.category_icon(item(chain="mcdonalds", category="マックカフェ", name="アイスカフェラテ"))[0], "drink")
        self.assertEqual(g.category_icon(item(chain="mcdonalds", category="マックカフェ", name="エスプレッソ"))[0], "drink")
        self.assertEqual(g.category_icon(item(chain="mcdonalds", category="マックカフェ", name="マカロン バニラ"))[0], "sweets")

    def test_unguessable_has_no_icon(self):
        i = item(chain="marugame", category="薬味・トッピング", name="すだち")
        self.assertIsNone(g.category_icon(i))
        h = g.render_card(i)
        self.assertNotIn("cat-icon", h)
        self.assertNotIn('class="ib"', h)

    def test_never_falls_back_to_other(self):
        for it in [item(category=None, name="zzz"), item(chain="katsuya", category="未知", name="zzz")]:
            ic = g.category_icon(it)
            self.assertTrue(ic is None or ic[0] != "other")

    def test_guess_is_not_shown_as_category_text(self):
        h = g.render_card(item(chain="katsuya", category=None, name="海老トリプル丼"))
        self.assertNotIn(">弁当・ごはん<", h)

    def test_every_slug_used_has_a_file(self):
        slugs = {s for m in g.CATEGORY_ICONS.values() for s in m.values()}
        slugs |= {s for s in g.CATEGORY_FALLBACK.values()} | {s for _, s in g.NAME_RULES if s}
        for s in slugs:
            self.assertTrue((g.DOCS / "assets" / "icons" / "cat" / f"{s}.svg").is_file(), s)

    def test_every_real_product_icon_file_exists(self):
        for it in g.load_all_products():
            ic = g.category_icon(it)
            if ic:
                self.assertTrue((g.DOCS / "assets" / "icons" / "cat" / f"{ic[0]}.svg").is_file(), it["name"])


if __name__ == "__main__":
    unittest.main()
