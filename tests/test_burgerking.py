"""バーガーキング: 電文のリクエスト形式、メニュー・詳細・ニュース・イベントのパーサ、見出しと商品名の照合、
告知日・終了日のひも付け、販売状況の追跡(一覧が空・極端に少ない回は何も更新しない)、ブロック、再登場、
商品ページ・設定・通知のテスト。フィクスチャは tests/synthetic_burgerking.py の縮めたデータ。"""

from __future__ import annotations

import json
import unittest
import urllib.parse

import helpers  # noqa: F401
from helpers import TempData
import crawl
import fetch
import generate_site as g
import parsers as P
import synthetic_burgerking as S
from fetch import BlockedError

D1, D2, D3, D4 = "2026-11-08", "2026-11-09", "2026-11-10", "2026-11-11"
CHAIN = "burgerking"


class FakeBK:
    """電文コードごとに応答を返す偽の取得器。呼ばれた (電文, body) を記録する。"""

    def __init__(self, menu=None, news=None, open_ev=None, ended_ev=None, details=None, fail=(), block=()):
        self.menu = menu if menu is not None else S.menu()
        self.news = news if news is not None else S.news()
        self.open_ev = open_ev if open_ev is not None else S.events(S.EV_OPEN)
        self.ended_ev = ended_ev if ended_ev is not None else S.events(S.EV_ENDED)
        self.details = details or {}
        self.fail, self.block = set(fail), set(block)
        self.calls: list[tuple[str, dict]] = []

    def post_json(self, chain: str, trcode: str, body: dict) -> str:
        assert chain == CHAIN
        self.calls.append((trcode, body))
        key = trcode if trcode != "BKJ0501" else f"BKJ0501{body['tpStatusEvent']}"
        if key in self.block:
            raise BlockedError("HTTP 403 (simulated)")
        if key in self.fail:
            raise OSError("simulated failure")
        if trcode == "BKJ0508":
            return self.menu
        if trcode == "BKJ0506":
            return self.news
        if trcode == "BKJ0501":
            return self.open_ev if body["tpStatusEvent"] == "C" else self.ended_ev
        if trcode == "BKJ0510":
            cd = body["menuCd"]
            return self.details.get(cd) or S.detail(cd, f"商品{cd}", 500, [("セット", 800), ("LLセット", 900)])
        raise AssertionError(trcode)

    def codes(self):
        return [c for c, _ in self.calls]


def run(getter, day, pending=None):
    pending = {} if pending is None else pending
    stats = {}
    crawl.crawl_burgerking(getter, pending, stats, day)
    return pending, stats[CHAIN]


def prod(pid):
    return crawl.load_product(CHAIN, pid)


class TestRequestForm(unittest.TestCase):
    def test_url_and_form(self):
        self.assertEqual(P.bk_url("BKJ0508"), "https://www.burgerking.co.jp/burgerking/BKJ0508.json")
        form = P.bk_request_form("BKJ0508", crawl.BK_MENU_BODY)
        self.assertEqual(list(form), ["message"])
        msg = json.loads(form["message"])
        self.assertEqual(msg["header"], {"trcode": "BKJ0508", "cdCallChnn": "02"})
        self.assertEqual(msg["body"]["menuKeywordList"], [{"menuKeyword": "期間限定"}])
        # urlencode しても元のJSONに戻る(form-urlencoded で送るため)
        enc = urllib.parse.urlencode(form)
        self.assertEqual(json.loads(urllib.parse.parse_qs(enc)["message"][0]), msg)

    def test_fetch_post_sends_form_with_honest_ua_and_maps_errors(self):
        import urllib.error
        import urllib.request
        from unittest import mock

        seen = {}

        class Res:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return b'{"header":{"result":true},"body":{}}'

        def fake_open(req, timeout=0):
            seen["req"] = req
            return Res()
        with mock.patch.object(urllib.request, "urlopen", fake_open):
            out = fetch.fetch_post(P.bk_url("BKJ0506"), {"message": "{}"}, "FastfoodPochi/1.0")
        self.assertIn('"result":true', out)
        r = seen["req"]
        self.assertEqual(r.get_method(), "POST")
        self.assertEqual(r.headers["User-agent"], "FastfoodPochi/1.0")
        self.assertEqual(r.headers["Content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(r.data, b"message=%7B%7D")

        def deny(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
        with mock.patch.object(urllib.request, "urlopen", deny):
            with self.assertRaises(BlockedError):
                fetch.fetch_post(P.bk_url("BKJ0506"), {"message": "{}"}, "ua")


class TestParsers(unittest.TestCase):
    def test_menu_dedupes_prefers_non_recommended_category_and_drops_sets(self):
        items = {e["product_id"]: e for e in P.parse_bk_menu(S.menu())}
        self.assertEqual(len(items), 6)  # アボカドワッパーJr.セットは除く
        self.assertNotIn("7001726", items)
        e = items["8000391"]
        self.assertEqual((e["name"], e["price"], e["has_sets"]), ("ギルティ ザ・ワンパウンダー", 2490, True))
        self.assertEqual(e["category"], "バーガー他")  # 「おすすめ」より具体的なカテゴリ
        self.assertEqual(e["official_url"], "https://www.burgerking.co.jp/menu/X000000006/8000391")
        self.assertEqual(items["2000341"]["has_sets"], False)
        self.assertEqual(items["2000341"]["category"], "サイド")

    def test_recommended_only_item_has_no_category_name(self):
        m = S.menu({"おすすめ": [S.FRIES]})
        e = P.parse_bk_menu(m)[0]
        self.assertIsNone(e["category"])
        self.assertEqual(e["official_url"], "https://www.burgerking.co.jp/menu/X000000001/2000341")

    def test_bad_responses(self):
        with self.assertRaises(ValueError):
            P.parse_bk_menu("<html>not json</html>")
        with self.assertRaises(ValueError):
            P.parse_bk_menu(json.dumps({"header": {"result": False}, "body": {}}))
        self.assertEqual(P.parse_bk_menu(S.ok({"allMenuList": []}, "BKJ0508")), [])  # 構造は正しいが0件

    def test_detail_price_variants(self):
        d = P.parse_bk_detail(S.detail("8000391", "ギルティ ザ・ワンパウンダー", 2490, [("LLセット", 2940), ("セット", 2790)]), "8000391")
        self.assertEqual(d["price_variants"], [{"label": "単品", "price": 2490}, {"label": "LLセット", "price": 2940},
                                               {"label": "セット", "price": 2790}][:1] + [
            {"label": "セット", "price": 2790}, {"label": "LLセット", "price": 2940}])
        self.assertTrue(d["limited"])
        only = P.parse_bk_detail(S.detail("1", "単品だけ", 100, [], limited=False), "1")
        self.assertIsNone(only["price_variants"])
        self.assertFalse(only["limited"])

    def test_news_and_events(self):
        n = P.parse_bk_news(S.news())
        self.assertEqual(n[0]["id"], "208")
        self.assertEqual(n[1]["date"], "2026-11-05")
        self.assertEqual(n[1]["url"], "https://www.burgerking.co.jp/notice/207")
        ev = P.parse_bk_events(S.events(S.EV_OPEN + S.EV_ENDED))
        by = {e["id"]: e for e in ev}
        self.assertIsNone(by["115"]["end"])  # 99991231 は開催中=終了日なし
        self.assertEqual((by["110"]["end"], by["110"]["status"]), ("2026-10-22", "E"))
        self.assertEqual(by["110"]["url"], "https://www.burgerking.co.jp/event/detail/110")
        self.assertNotIn("imageUrlWeb", json.dumps(ev))  # 画像URLは持たない


class TestTitleMatch(unittest.TestCase):
    def test_strict_needs_launch_word_right_after_the_quote(self):
        t = "大人気「マッシュルームワッパー」に新作！「オリジナルハーブソース」を使用！「オータムスペシャル マッシュルームワッパー」新発売"
        self.assertTrue(P.bk_title_matches("オータムスペシャル マッシュルームワッパー", t))
        self.assertFalse(P.bk_title_matches("マッシュルームワッパー", t))  # 元になった商品の名前
        self.assertFalse(P.bk_title_matches("オリジナルハーブソース", t))
        self.assertTrue(P.bk_title_matches("マッシュルームワッパー", t, strict=False))  # 広い照合では出ている扱い

    def test_no_brackets_never_matches(self):
        self.assertFalse(P.bk_title_matches("ギルティ ザ・ワンパウンダー", "ギルティ ザ・ワンパウンダー新発売"))

    def test_tilde_and_space_normalization(self):
        self.assertTrue(P.bk_title_matches("キングフュージョン~made with KITKAT®~",
                                           "新作サンデーが登場！『キングフュージョン ～made with KITKAT®～』新発売！"))

    def test_nakaguro_lists(self):
        self.assertTrue(P.bk_title_matches("Bバーガー", "「Aバーガー・Bバーガー」が新登場"))
        self.assertTrue(P.bk_title_matches("Aバーガー・Bバーガー", "「Aバーガー・Bバーガー」が新登場"))


class TestNewsAndEventMatch(unittest.TestCase):
    def setUp(self):
        self.news = P.parse_bk_news(S.news())
        self.events = P.parse_bk_events(S.events(S.EV_OPEN + S.EV_ENDED))

    def test_news_announcement_date(self):
        hit = P.match_bk_news("ギルティ ザ・ワンパウンダー", self.news, D1)
        self.assertEqual((hit["date"], hit["id"]), ("2026-11-05", "207"))
        self.assertEqual(P.match_bk_news("キングフュージョン~made with KITKAT®~", self.news, D1)["date"], "2026-10-21")
        self.assertIsNone(P.match_bk_news("マッシュルームワッパー", self.news, D1))  # 別商品の見出しに名前が出るだけ
        self.assertIsNone(P.match_bk_news("My Premiumショコラ チョコ＆キャラメル", self.news, D1))  # 表記が違う=付けない

    def test_news_window(self):
        self.assertIsNone(P.match_bk_news("ギルティ ザ・ワンパウンダー", self.news, "2027-06-01"))

    def test_non_launch_titles_are_ignored(self):
        news = [{"id": "1", "date": "2026-11-01", "title": "『A商品』が当たる！無料プレゼント！発売記念", "url": "u", "category": ""}]
        self.assertIsNone(P.match_bk_news("A商品", news, D1))

    def test_event_end_when_clearly_linked(self):
        ev = P.find_bk_event_end("オン ザ ビーフ", self.events, D1)
        self.assertEqual((ev["end"], ev["id"]), ("2026-10-22", "110"))

    def test_open_event_vetoes_end(self):
        self.assertIsNone(P.find_bk_event_end("ギルティ ザ・ワンパウンダー", self.events, D1))
        # マッシュルームワッパー: 終了済みのクーポンイベント(106)はあるが、開催中の「復活発売」(105)があるので終了日は付けない
        self.assertIsNone(P.find_bk_event_end("マッシュルームワッパー", self.events, D1))

    def test_coupon_and_discount_events_are_not_end_dates(self):
        ev = P.parse_bk_events(S.events([("9", "20261001", "20261008", "E", "『Aバーガー』を300円引きクーポンで！新発売記念")]))
        self.assertIsNone(P.find_bk_event_end("Aバーガー", ev, D1))

    def test_ambiguous_or_stale_events_give_nothing(self):
        two = P.parse_bk_events(S.events([
            ("1", "20261001", "20261010", "E", "『Xバーガー』新発売！"),
            ("2", "20261012", "20261020", "E", "『Xバーガー』が復活！")]))
        self.assertIsNone(P.find_bk_event_end("Xバーガー", two, D1))  # 終了日が2通り
        old = P.parse_bk_events(S.events([("3", "20250101", "20250110", "E", "『Yバーガー』新発売！")]))
        self.assertIsNone(P.find_bk_event_end("Yバーガー", old, D1))  # 去年の販売の名残


class TestCrawl(unittest.TestCase):
    def test_first_run_requests_records_and_details(self):
        with TempData():
            g_ = FakeBK()
            pending, st = run(g_, D1)
            # メニュー1+ニュース1+イベント2 + 詳細(セットのある商品は5件=上限6以内)
            self.assertEqual(g_.codes()[:4], ["BKJ0508", "BKJ0506", "BKJ0501", "BKJ0501"])
            self.assertEqual([b["tpStatusEvent"] for c, b in g_.calls if c == "BKJ0501"], ["C", "E"])
            self.assertEqual(g_.codes().count("BKJ0510"), 3)  # ギルティ・オータム・マッシュルーム
            self.assertEqual(len(g_.calls), 7)
            self.assertEqual(g_.calls[0][1]["menuKeywordList"], [{"menuKeyword": "期間限定"}])
            self.assertTrue(st["list_ok"])
            self.assertFalse(st["blocked"])
            self.assertEqual(st["new_products"], 6)
            self.assertEqual(st["limited_seen"], 6)
            self.assertIsNone(prod("7001726"))  # セット商品は記録しない
            p = prod("8000391")
            self.assertEqual((p["name"], p["price"], p["status"], p["limited_kind"]), ("ギルティ ザ・ワンパウンダー", 2490, "on_sale", "期間限定"))
            self.assertEqual(p["chain"], CHAIN)
            self.assertEqual(p["first_seen_at"], f"{D1}T07:00:00+09:00")
            self.assertEqual(p["price_history"], [{"date": D1, "price": 2490}])
            self.assertIsNone(p["launch_date"])
            self.assertEqual(p["launch_date_source"], "not_found")
            # 告知日(ニュースの公開日)
            self.assertEqual((p["announced_at"], p["news_url"]), ("2026-11-05", "https://www.burgerking.co.jp/notice/207"))
            # セット価格の内訳
            self.assertTrue(p["detail_checked"])
            self.assertEqual(len(p["price_variants"]), 3)
            # 画像・説明文・栄養は持たない
            self.assertIsNone(p["image"])
            self.assertIsNone(p["description"])
            self.assertIsNone(p["nutrition"])
            self.assertNotIn("menuImgPath", json.dumps(p))
            # 商品名の表記違いは告知日を付けない
            self.assertIsNone(prod("4000133")["announced_at"])
            self.assertEqual(crawl.load_json(crawl.BASELINE_PATH, {})[CHAIN], sorted(["8000391", "8000388", "8000376", "2000341", "4000131", "4000133"]))
            self.assertEqual(crawl.load_json(crawl.DATA / "burgerking_state.json", {})["menu_count"], 6)

    def test_detail_requests_are_capped_per_run_and_finish_next_day(self):
        many = [S.mi(f"80003{i:02d}", f"限定バーガー{i}", 500 + i, "Y") for i in range(10)]
        with TempData():
            g1 = FakeBK(menu=S.menu({"バーガー他": many}))
            _, st = run(g1, D1)
            self.assertEqual(g1.codes().count("BKJ0510"), crawl.BK_DETAIL_PER_RUN)
            self.assertEqual(st["new_products"], 10)
            g2 = FakeBK(menu=S.menu({"バーガー他": many}))
            run(g2, D2)
            self.assertEqual(g2.codes().count("BKJ0510"), 4)  # 残り
            g3 = FakeBK(menu=S.menu({"バーガー他": many}))
            run(g3, D3)
            self.assertEqual(g3.codes().count("BKJ0510"), 0)
            self.assertEqual(len(g3.calls), 4)  # 通常日は4リクエスト

    def test_detail_failure_is_retried_later_and_not_fatal(self):
        with TempData():
            g1 = FakeBK(fail={"BKJ0510"})
            _, st = run(g1, D1)
            self.assertTrue(st["list_ok"])
            self.assertEqual(st["failed"], 3)
            self.assertFalse(prod("8000391")["detail_checked"])
            run(FakeBK(), D2)
            self.assertTrue(prod("8000391")["detail_checked"])

    def test_event_end_is_linked_only_when_certain(self):
        menu = S.menu({"バーガー他": [S.ONBEEF, S.GUILTY]})
        with TempData():
            run(FakeBK(menu=menu), D1)
            p = prod("8000399")  # オン ザ ビーフ: 終了済みイベント110だけ
            self.assertEqual(p["official_end_date"], "2026-10-22")
            self.assertEqual(p["official_end_url"], "https://www.burgerking.co.jp/event/detail/110")
            self.assertIn("2026-10-22", p["planned_end_text"])
            self.assertIn("一覧には載っています", p["planned_end_text"])  # 終了日を過ぎても一覧にある
            self.assertIsNone(prod("8000391")["official_end_date"])  # 開催中のイベントがある

    def test_event_end_is_cleared_when_an_open_event_appears(self):
        menu = S.menu({"バーガー他": [S.ONBEEF]})
        reopened = S.events(S.EV_OPEN + [("120", "20261110", "99991231", "C", "『オン ザ ビーフ』が再び登場！")])
        with TempData():
            run(FakeBK(menu=menu), D1)
            self.assertIsNotNone(prod("8000399")["official_end_date"])
            run(FakeBK(menu=menu, open_ev=reopened), D2)
            p = prod("8000399")
            self.assertIsNone(p["official_end_date"])
            self.assertIsNone(p["planned_end_text"])

    def test_event_fetch_failure_keeps_what_was_found(self):
        menu = S.menu({"バーガー他": [S.ONBEEF]})
        with TempData():
            run(FakeBK(menu=menu), D1)
            _, st = run(FakeBK(menu=menu, fail={"BKJ0501E"}), D2)
            self.assertTrue(st["list_ok"])
            self.assertEqual(prod("8000399")["official_end_date"], "2026-10-22")

    def test_news_failure_does_not_break_the_menu(self):
        with TempData():
            _, st = run(FakeBK(fail={"BKJ0506"}), D1)
            self.assertTrue(st["list_ok"])
            self.assertEqual(st["new_products"], 6)
            self.assertIsNone(prod("8000391")["announced_at"])
            run(FakeBK(), D2)  # 翌日取れれば付く
            self.assertEqual(prod("8000391")["announced_at"], "2026-11-05")

    def test_ended_after_two_separate_misses(self):
        less = S.menu({"サイド": [S.FRIES, S.KING, S.CHOCO, S.MUSH]})  # 4件(6件の半分以上)
        with TempData():
            run(FakeBK(), D1)
            run(FakeBK(menu=less), D2)
            self.assertEqual(prod("8000391")["status"], "on_sale")
            self.assertEqual(prod("8000391")["missed_runs"], 1)
            run(FakeBK(menu=less), D2)  # 同じ日は1回と数える
            self.assertEqual(prod("8000391")["status"], "on_sale")
            _, st = run(FakeBK(menu=less), D3)
            p = prod("8000391")
            self.assertEqual((p["status"], p["ended_observed_at"]), ("ended", D3))
            self.assertEqual(prod("2000341")["status"], "on_sale")
            self.assertGreaterEqual(st["ended"], 1)

    def test_empty_bad_or_failed_list_updates_nothing(self):
        with TempData():
            run(FakeBK(), D1)
            for menu in (S.ok({"allMenuList": []}, "BKJ0508"), "<html>blocked page</html>"):
                _, st = run(FakeBK(menu=menu), D2)
                self.assertFalse(st["list_ok"])
            _, st = run(FakeBK(fail={"BKJ0508"}), D2)
            self.assertFalse(st["list_ok"])
            for pid in ("8000391", "2000341"):
                self.assertEqual(prod(pid)["status"], "on_sale")
                self.assertFalse(prod(pid).get("missed_runs"))
                self.assertEqual(prod(pid)["last_seen_at"], D1)

    def test_extremely_short_list_is_not_trusted(self):
        with TempData():
            run(FakeBK(), D1)  # 6件
            _, st = run(FakeBK(menu=S.menu({"サイド": [S.FRIES]})), D2)  # 1件(6件の半分未満)
            self.assertFalse(st["list_ok"])
            self.assertFalse(prod("8000391").get("missed_runs"))
            self.assertEqual(crawl.load_json(crawl.DATA / "burgerking_state.json", {})["menu_count"], 6)

    def test_block_stops_and_records_nothing(self):
        with TempData():
            g1 = FakeBK(block={"BKJ0508"})
            _, st = run(g1, D1)
            self.assertTrue(st["blocked"])
            self.assertEqual(g1.codes(), ["BKJ0508"])  # 以降のリクエストは送らない
            self.assertEqual(crawl.load_chain_products(CHAIN), [])
            g2 = FakeBK(block={"BKJ0506"})
            _, st = run(g2, D1)
            self.assertTrue(st["blocked"])
            self.assertEqual(g2.codes(), ["BKJ0508", "BKJ0506"])
            self.assertEqual(crawl.load_chain_products(CHAIN), [])

    def test_blocked_is_reported_in_run_summary(self):
        with TempData() as base:
            stats = {}
            crawl.crawl_burgerking(FakeBK(block={"BKJ0508"}), {}, stats, D1)
            self.assertEqual(crawl.collect_problems(stats)["blocked"], [CHAIN])
            crawl.save_run_summary(stats, {})
            self.assertEqual(json.loads((base / "run_summary.json").read_text())["blocked"], [CHAIN])

    def test_price_change_goes_to_history(self):
        changed = S.menu({"サイド": [S.mi("2000341", "ギルティ・フレンチフライ", 450)]})
        with TempData():
            run(FakeBK(), D1)
            run(FakeBK(menu=S.menu({"サイド": [S.FRIES, S.GUILTY]})), D2)
            run(FakeBK(menu=changed), D3)  # 件数が減っても半分未満でなければ判定は続く
            p = prod("2000341")
            self.assertEqual(p["price"], 450)
            self.assertEqual([h["price"] for h in p["price_history"]], [400, 450])

    def test_relaunch_starts_a_new_season(self):
        less = S.menu({"サイド": [S.FRIES, S.KING, S.CHOCO]})
        with TempData():
            run(FakeBK(menu=S.menu({"バーガー他": [S.ONBEEF], "サイド": [S.FRIES, S.KING, S.CHOCO]})), D1)
            self.assertEqual(prod("8000399")["official_end_date"], "2026-10-22")
            run(FakeBK(menu=less), D2)
            run(FakeBK(menu=less), D3)
            self.assertEqual(prod("8000399")["status"], "ended")
            back = S.menu({"バーガー他": [S.ONBEEF], "サイド": [S.FRIES, S.KING, S.CHOCO]})
            _, st = run(FakeBK(menu=back), D4)
            p = prod("8000399")
            self.assertEqual(p["status"], "on_sale")
            self.assertEqual(st["relaunched"], 1)
            self.assertEqual(p["relaunches"][-1]["reappeared_at"], D4)
            self.assertEqual(p["seasons"][-1]["observed_start"], D1)
            self.assertEqual(p["seasons"][-1]["official_end_date"], "2026-10-22")
            self.assertEqual(p["seasons"][-1]["ended_observed_at"], D3)

    def test_only_the_official_host_is_used(self):
        self.assertTrue(P.bk_url("BKJ0508").startswith("https://www.burgerking.co.jp/"))
        self.assertEqual(P.BK_HOST, "www.burgerking.co.jp")

    def test_suspected_restructure_when_filter_stops_working(self):
        everything = [S.mi(f"1{i:06d}", f"定番{i}", 300) for i in range(40)]
        with TempData():
            run(FakeBK(), D1)
            _, st = run(FakeBK(menu=S.menu({"サイド": everything + [S.FRIES]})), D2)
            self.assertFalse(st["list_ok"])
            self.assertIn("suspected_restructure", st)
            self.assertIsNone(prod("1000000"))  # 定番を限定として記録しない


class TestSite(unittest.TestCase):
    def make(self, pid="8000391", day=D1, **over):
        with TempData():
            run(FakeBK(menu=S.menu({"バーガー他": [S.ONBEEF, S.GUILTY], "サイド": [S.FRIES]})), day)
            p = prod(pid)
        p.update(over)
        return p

    def test_title_and_description(self):
        p = self.make()
        self.assertEqual(g.seo_title(p), "バーガーキング「ギルティ ザ・ワンパウンダー」の価格・販売期間")
        d = g.seo_description(p)
        self.assertIn("バーガーキングの限定メニュー「ギルティ ザ・ワンパウンダー」", d)
        self.assertIn("2490円（税込）", d)

    def test_item_page_shows_facts_only(self):
        p = self.make()
        h = g.render_item_page(p)
        self.assertIn("バーガーキング", h)
        self.assertIn("2490円（税込）", h)
        self.assertIn("単品", h)  # セット価格の内訳
        self.assertIn("2026-11-08（記録開始日）〜 販売中", h)
        self.assertIn("発売日ではありません", h)
        self.assertIn("告知日：2026-11-05", h)
        self.assertIn("https://www.burgerking.co.jp/notice/207", h)
        self.assertIn("https://www.burgerking.co.jp/menu/X000000001/8000391", h)
        self.assertIn("栄養成分・アレルギー情報は当サイトでは掲載していません", h)
        self.assertNotIn("<blockquote", h)  # 説明文の引用は無い
        self.assertNotIn("<img", h.split("<main>")[1])  # 画像は出さない
        self.assertIn("「期間限定」の一覧から消えたのを確認した日", h)  # 公式が終了日を載せない旨

    def test_ended_with_official_end_date(self):
        p = self.make("8000399", status="ended", ended_observed_at=D3)
        self.assertEqual(g.period_text(p), "2026-11-08（記録開始日）〜2026-10-22")
        self.assertIn("終了日は公式イベントの終了日", g.period_text(p, detailed=True))
        h = g.render_item_page(p)
        self.assertIn("公式イベントの終了日（2026-10-22）", h)
        self.assertIn("https://www.burgerking.co.jp/event/detail/110", h)

    def test_ended_without_official_date_uses_disappearance_with_clear_wording(self):
        p = self.make(status="ended", ended_observed_at=D3)
        self.assertIn("「期間限定」の一覧から消えたことを確認した日", g.period_text(p, detailed=True))

    def test_chain_config(self):
        self.assertEqual(g.CHAIN_NAME[CHAIN], "バーガーキング")
        self.assertEqual(g.CHAIN_SOURCE[CHAIN], "バーガーキング公式サイト")
        self.assertTrue(g.CHAIN_LIST_TEXT.endswith("・丸亀製麺・バーガーキング"))
        self.assertEqual(len(g.CHAINS), 10)
        for k in ("site_description", "site_tagline"):
            self.assertIn("バーガーキング", g.CONFIG[k])
        about = g.CONFIG["about"]
        self.assertEqual(len(about), 4)
        self.assertLess(sum(len(x) for x in about), 700)
        self.assertTrue(any("バーガーキング" in x for x in about))
        self.assertIn("--burgerking:", g.CSS)
        self.assertIn('data-chain="burgerking"', g.CSS)
        colors = [c["color"].lower() for c in g.CHAINS]
        self.assertEqual(len(colors), len(set(colors)))
        shell = g.page_shell("x", "d", "<p>b</p>", "https://fastfood.netaful.jp/")
        self.assertIn("https://www.burgerking.co.jp/", shell)
        self.assertIn("丸亀製麺、バーガーキング各社", shell)

    def test_notification_label_and_limits(self):
        import notify
        self.assertEqual(notify.CHAIN_LABEL[CHAIN], "バーガーキング")
        self.assertIn(CHAIN, crawl.CRAWLERS)
        self.assertEqual(crawl.MAX_REQUESTS[CHAIN], 12)
        self.assertGreaterEqual(crawl.MAX_REQUESTS[CHAIN], 4 + crawl.BK_DETAIL_PER_RUN)
        self.assertGreaterEqual(crawl.INTERVALS[CHAIN], 3)

    def test_getter_enforces_limit_and_interval(self):
        from unittest import mock
        gt = crawl.Getter()
        with mock.patch.object(crawl, "fetch_post", return_value="{}") as fp, mock.patch.object(gt.throttle, "wait"):
            for _ in range(crawl.MAX_REQUESTS[CHAIN]):
                gt.post_json(CHAIN, "BKJ0506", crawl.BK_NEWS_BODY)
            with self.assertRaises(RuntimeError):
                gt.post_json(CHAIN, "BKJ0506", crawl.BK_NEWS_BODY)
            self.assertEqual(fp.call_count, crawl.MAX_REQUESTS[CHAIN])
            url, form, ua = fp.call_args[0]
            self.assertEqual(url, "https://www.burgerking.co.jp/burgerking/BKJ0506.json")
            self.assertEqual(ua, crawl.UA)


class TestNetaful(unittest.TestCase):
    def test_title_filter(self):
        import fetch_netaful_reviews as nr
        self.assertEqual(nr.TAGS[CHAIN], 1017)
        self.assertIn(CHAIN, nr.TITLE_ALWAYS)
        self.assertIn("バーガーキング", nr.TITLE_FILTERS[CHAIN])


if __name__ == "__main__":
    unittest.main()
