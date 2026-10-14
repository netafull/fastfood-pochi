"""松屋・松のやの基本キー(URLの日付抜きID)、移行スクリプト、「新規が極端に多い回は構造変更とみなして保留」
する歯止め、通知の文面のテスト。公式ページのスナップショットは使わず、合成データだけで動く
(tests/fixtures/ が無い環境でもスキップされない)。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from helpers import TempData
import crawl
import migrate_matsuya_keys as mig
import notify
import parsers as P
from fetch import NotFoundError

ROBOTS = "User-agent: *\nAllow: /\n"


def item_html(cat: str, slug: str, name: str, price: int = 500, brand: str = "matsuya") -> str:
    return (f'<li class="item"><a href="/menu/../{brand}/menu/{cat}/{slug}.html">'
            f'<span class="txt">{name}</span><span class="price">{price}円</span></a><!-- 1 --></li>'
            "<!-- //.item -->")


class SynthMatsuya:
    """合成した松屋(または松のや)の取得器。cats = {カテゴリ: [(slug, 名前), …]}。詳細ページは404を返す
    (詳細を取りに行った回数は calls で見る)。"""

    def __init__(self, cats: dict[str, list[tuple[str, str]]], brand: str = "matsuya"):
        self.cats = cats
        self.brand = brand
        self.calls: list[str] = []

    def __call__(self, chain: str, url: str) -> str:
        self.calls.append(url)
        path = url.replace(crawl.MATSUYA_BASE, "")
        root = f"/{self.brand}/menu/"
        if path == "/robots.txt":
            return ROBOTS
        if path == root:
            return "".join(f'<a href="{root}{c}/index.html">{c}</a>' for c in self.cats)
        for c, items in self.cats.items():
            if path == f"{root}{c}/index.html":
                return "".join(item_html(c, s, n, brand=self.brand) for s, n in items)
        if path.endswith("whatsnew.json"):
            return '{"list_whatsnew": []}'
        raise NotFoundError(f"404 {path}")

    def detail_calls(self) -> list[str]:
        return [u for u in self.calls if u.endswith(".html") and "/index.html" not in u]


def seed(chain: str, pid: str, name: str, slug_id: str, status: str = "on_sale", **extra) -> dict:
    p = {"chain": chain, "product_id": pid, "name": name, "price": 500, "status": status,
         "first_seen_at": "2026-10-01T07:00:00+09:00", "last_seen_at": "2026-10-12", "missed_runs": 0,
         "price_history": [{"date": "2026-10-01", "price": 500}], "launch_date": "2026-10-01",
         "current_slug": slug_id, "official_url": f"{crawl.MATSUYA_BASE}/x/{slug_id}.html",
         "in_recommended": False, "source_list": ["gyumeshi"]}
    p.update(extra)
    crawl.save_product(chain, p)
    return p


def run(chain_fn, getter, day):
    pending = crawl.load_json(crawl.PENDING_PATH, {})
    stats: dict = {}
    chain_fn(getter, pending, stats, day)
    crawl.save_json(crawl.PENDING_PATH, pending)
    return pending, stats[next(iter(stats))]


class TestBaseKey(unittest.TestCase):
    def test_strips_trailing_six_digit_date(self):
        self.assertEqual(P.matsuya_base_key("curry-kashmir_gyuu_hp_261013"), "curry-kashmir_gyuu_hp")
        self.assertEqual(P.matsuya_base_key("curry-kashmir_gyuu_hp_261006"), "curry-kashmir_gyuu_hp")

    def test_no_date_stays(self):
        self.assertEqual(P.matsuya_base_key("morning-kobachi_gyuu_hp"), "morning-kobachi_gyuu_hp")

    def test_digits_only_slug_and_non_six_digits(self):
        # 日付ではない数字(6桁でない・直前が「_」でない)は消さない
        self.assertEqual(P.matsuya_base_key("hp-gyu3_hp"), "hp-gyu3_hp")
        self.assertEqual(P.matsuya_base_key("a-b_12345"), "a-b_12345")
        self.assertEqual(P.matsuya_base_key("a-b_1234567"), "a-b_1234567")
        self.assertEqual(P.matsuya_base_key("a-b123456"), "a-b123456")
        # 数字だけのスラッグ(カテゴリ-数字)。「_」が無ければそのまま
        self.assertEqual(P.matsuya_base_key("a-123456"), "a-123456")

    def test_only_trailing_date_removed_with_many_underscores(self):
        self.assertEqual(P.matsuya_base_key("souzai-souzai_set_nebiki_rosu1_kaki2_hp_261007"),
                         "souzai-souzai_set_nebiki_rosu1_kaki2_hp")
        self.assertEqual(P.matsuya_base_key("x-a_260101_b_261013"), "x-a_260101_b")

    def test_category_keeps_products_apart(self):
        a = P.matsuya_product_id("/matsuya/menu/curry/foo_hp_261013.html")
        b = P.matsuya_product_id("/matsuya/menu/don/foo_hp_261013.html")
        self.assertEqual((a, b), ("curry-foo_hp", "don-foo_hp"))

    def test_list_entry_has_base_id_and_current_slug(self):
        page = item_html("curry", "foo_hp_261013", "カレー")
        e = P.parse_matsuya_list(page)[0]
        self.assertEqual(e["product_id"], "curry-foo_hp")
        self.assertEqual(e["current_slug"], "curry-foo_hp_261013")
        self.assertEqual(e["detail_path"], "/matsuya/menu/curry/foo_hp_261013.html")


class TestDateOnlyChange(unittest.TestCase):
    def _items(self, suffix: str, n: int = 12):
        return [(f"item{i}_hp_{suffix}", f"商品{i}") for i in range(n)]

    def _prepare(self, brand: str):
        old = self._items("261006")
        base_ids = {f"gyumeshi-item{i}_hp" for i in range(12)}
        for i in range(12):
            seed(brand, f"gyumeshi-item{i}_hp", f"商品{i}", f"gyumeshi-item{i}_hp_261006")
        crawl.save_baseline(brand, base_ids)
        return old

    def test_date_update_is_neither_new_nor_ended(self):
        for brand, fn in (("matsuya", crawl.crawl_matsuya), ("matsunoya", crawl.crawl_matsunoya)):
            with self.subTest(brand=brand), TempData():
                self._prepare(brand)
                new_list = self._items("261013")
                for day in ("2026-10-14", "2026-10-15", "2026-10-16"):
                    g = SynthMatsuya({"gyumeshi": new_list}, brand)
                    _, st = run(fn, g, day)
                    self.assertTrue(st["list_ok"])
                    self.assertEqual(st["new_products"], 0)
                    self.assertEqual(st["ended"], 0)
                    self.assertEqual(g.detail_calls(), [])  # 詳細は取りに行かない
                    self.assertNotIn("suspected_restructure", st)
                ps = crawl.load_chain_products(brand)
                self.assertEqual(len(ps), 12)
                for p in ps:
                    self.assertEqual(p["status"], "on_sale")
                    self.assertEqual(p["missed_runs"], 0)
                    self.assertTrue(p["current_slug"].endswith("_261013"))
                    self.assertTrue(p["official_url"].endswith("_261013.html"))
                    self.assertEqual(p["first_seen_at"], "2026-10-01T07:00:00+09:00")
                    self.assertEqual(p["launch_date"], "2026-10-01")
                    self.assertEqual(p["last_seen_at"], "2026-10-16")

    def test_real_end_and_relaunch_use_same_record(self):
        with TempData():
            self._prepare("matsuya")
            full = self._items("261013")
            gone = full[1:]  # item0 が消える
            run(crawl.crawl_matsuya, SynthMatsuya({"gyumeshi": gone}), "2026-10-14")
            self.assertEqual(crawl.load_product("matsuya", "gyumeshi-item0_hp")["status"], "on_sale")
            _, st = run(crawl.crawl_matsuya, SynthMatsuya({"gyumeshi": gone}), "2026-10-15")
            p = crawl.load_product("matsuya", "gyumeshi-item0_hp")
            self.assertEqual((p["status"], st["ended"]), ("ended", 1))
            # 日付違いのslugで再登場 → 同じレコードが販売中に戻り、relaunches と seasons に残る
            _, st = run(crawl.crawl_matsuya, SynthMatsuya({"gyumeshi": self._items("261120")}), "2026-11-20")
            p = crawl.load_product("matsuya", "gyumeshi-item0_hp")
            self.assertEqual(p["status"], "on_sale")
            self.assertEqual(p["relaunches"][0]["reappeared_at"], "2026-11-20")
            self.assertEqual(len(p["seasons"]), 1)
            self.assertEqual(p["seasons"][0]["launch_date"], "2026-10-01")
            self.assertEqual(p["current_slug"], "gyumeshi-item0_hp_261120")
            self.assertEqual(st["new_products"], 0)
            self.assertEqual(len(crawl.load_chain_products("matsuya")), 12)

    def test_both_dated_slugs_listed_keep_newer_url(self):
        with TempData():
            self._prepare("matsuya")
            items = self._items("261013") + [("item0_hp_261006", "商品0")]
            _, st = run(crawl.crawl_matsuya, SynthMatsuya({"gyumeshi": items}), "2026-10-14")
            self.assertEqual(st["new_products"], 0)
            self.assertEqual(crawl.load_product("matsuya", "gyumeshi-item0_hp")["current_slug"],
                             "gyumeshi-item0_hp_261013")

    def test_migrated_data_then_tomorrows_crawl_is_zero_new_zero_ended(self):
        """今朝の誤新規を移行したあとの状態から、明日の巡回(全部 _261013)を再現する。"""
        with TempData() as base:
            # 移行前: 旧slugのレコード6件(10/11〜)と、今朝誤って作られたレコード(重複1件・誤新規1件・本当の新規1件)
            for i in range(6):
                seed("matsuya", f"gyumeshi-item{i}_hp_261006", f"商品{i}", f"gyumeshi-item{i}_hp_261006",
                     missed_runs=1 if i == 0 else 0, last_miss_date="2026-10-14" if i == 0 else None)
            seed("matsuya", "gyumeshi-item0_hp_261013", "商品0", "gyumeshi-item0_hp_261013",
                 first_seen_at="2026-10-14T07:31:19+09:00", last_seen_at="2026-10-14",
                 same_name_as=["gyumeshi-item0_hp_261006"], in_recommended=True)
            seed("matsuya", "gyumeshi-item6_hp_261013", "商品6", "gyumeshi-item6_hp_261013",
                 first_seen_at="2026-10-14T07:31:19+09:00", last_seen_at="2026-10-14")
            seed("matsuya", "limited-real_new_hp_261013", "本当の新商品", "limited-real_new_hp_261013",
                 first_seen_at="2026-10-14T07:31:19+09:00", last_seen_at="2026-10-14", in_recommended=True)
            old_ids = [f"gyumeshi-item{i}_hp_261006" for i in range(8)] + ["limited-dummy_hp_261006"]
            (base / "baseline_ids.json").write_text(json.dumps({"matsuya": old_ids}), encoding="utf-8")
            mig.migrate(base)
            self.assertEqual(sorted(p["product_id"] for p in crawl.load_chain_products("matsuya")),
                             [f"gyumeshi-item{i}_hp" for i in range(6)] + ["limited-real_new_hp"])
            cats = {"gyumeshi": [(f"item{i}_hp_261013", f"商品{i}") for i in range(8)],
                    "limited": [("real_new_hp_261013", "本当の新商品"), ("dummy_hp_261013", "ダミー")]}
            # seed の dummy は baseline のみ(記録なし)。おすすめに載るダミーは詳細404で pending になるだけ
            for day in ("2026-10-15", "2026-10-16"):
                g = SynthMatsuya(cats)
                _, st = run(crawl.crawl_matsuya, g, day)
                self.assertTrue(st["list_ok"])
                self.assertEqual((st["new_products"], st["ended"]), (0, 0))
                self.assertNotIn("suspected_restructure", st)
            for p in crawl.load_chain_products("matsuya"):
                self.assertEqual(p["status"], "on_sale")
                self.assertEqual(p["missed_runs"], 0)


class TestBurstGuard(unittest.TestCase):
    def _setup(self, n_existing: int, n_baseline_only: int = 0):
        for i in range(n_existing):
            seed("matsuya", f"gyumeshi-old{i}_hp", f"旧{i}", f"gyumeshi-old{i}_hp_261006")
        ids = {f"gyumeshi-old{i}_hp" for i in range(n_existing)} | {f"gyumeshi-base{i}_hp" for i in range(n_baseline_only)}
        crawl.save_baseline("matsuya", ids)

    def _list(self, n_existing: int, n_baseline_only: int, n_new: int):
        items = [(f"old{i}_hp_261013", f"旧{i}") for i in range(n_existing)]
        items += [(f"base{i}_hp_261013", f"基{i}") for i in range(n_baseline_only)]
        items += [(f"new{i}_hp_261013", f"新{i}") for i in range(n_new)]
        return {"gyumeshi": items}

    def test_over_threshold_skips_records_and_judgement(self):
        with TempData():
            self._setup(40)  # 一覧54件 → 閾値 max(10, 13)=13
            g = SynthMatsuya(self._list(40, 0, 14))  # 14件 > 13
            pending, st = run(crawl.crawl_matsuya, g, "2026-10-14")
            self.assertEqual(st["new_products"], 0)
            self.assertEqual(st["ended"], 0)
            self.assertFalse(st["list_ok"])
            self.assertEqual(g.detail_calls(), [])  # 新規の詳細を取りに行かない
            rec = pending["matsuya"][crawl.BURST_KEY]
            self.assertEqual((rec["reason"], rec["count"], rec["date"], rec["consecutive_days"]),
                             ("suspected_restructure", 14, "2026-10-14", 1))
            self.assertEqual(st["suspected_restructure"]["count"], 14)
            # 既存の商品は見えた扱いのまま。終了判定も行わない
            self.assertEqual(len(crawl.load_chain_products("matsuya")), 40)
            self.assertEqual(crawl.baseline_for("matsuya"), {f"gyumeshi-old{i}_hp" for i in range(40)})

    def test_no_end_judgement_while_held(self):
        with TempData():
            self._setup(12)
            # 既存12件のうち2件が一覧から消え、同時に新規候補が大量に出た日 → 終了判定をしない
            lst = self._list(10, 0, 14)
            for day in ("2026-10-14", "2026-10-15", "2026-10-16"):
                run(crawl.crawl_matsuya, SynthMatsuya(lst), day)
            for p in crawl.load_chain_products("matsuya"):
                self.assertEqual(p["status"], "on_sale")
                self.assertFalse(p.get("missed_runs"))

    def test_under_threshold_works_normally(self):
        with TempData():
            self._setup(40)
            g = SynthMatsuya(self._list(40, 0, 13))  # 13件 = 閾値(52→13)。超えていない
            pending, st = run(crawl.crawl_matsuya, g, "2026-10-14")
            self.assertTrue(st["list_ok"])
            self.assertNotIn("suspected_restructure", st)
            self.assertEqual(len(g.detail_calls()), 13)  # 通常どおり新規の詳細を取りに行く(合成では404)
            self.assertNotIn(crawl.BURST_KEY, pending["matsuya"])

    def test_min_threshold_for_small_lists(self):
        self.assertEqual(crawl.burst_threshold(10), 10)
        self.assertEqual(crawl.burst_threshold(40), 10)
        self.assertEqual(crawl.burst_threshold(174), 43)

    def test_first_run_without_baseline_is_exempt(self):
        with TempData():
            g = SynthMatsuya(self._list(0, 0, 60))
            pending, st = run(crawl.crawl_matsuya, g, "2026-10-11")
            self.assertTrue(st["list_ok"])
            self.assertNotIn("suspected_restructure", st)
            self.assertEqual(len(crawl.baseline_for("matsuya")), 60)
            self.assertNotIn(crawl.BURST_KEY, pending["matsuya"])

    def test_consecutive_days_and_recovery(self):
        with TempData():
            self._setup(40)
            lst = self._list(40, 0, 15)
            days = []
            for day in ("2026-10-14", "2026-10-14", "2026-10-15", "2026-10-16"):
                pending, st = run(crawl.crawl_matsuya, SynthMatsuya(lst), day)
                days.append(st["suspected_restructure"]["consecutive_days"])
            self.assertEqual(days, [1, 1, 2, 3])  # 同じ日の再実行は数え直さない
            self.assertEqual(pending["matsuya"][crawl.BURST_KEY]["first_date"], "2026-10-14")
            # 閾値以下に戻れば保留の記録は消え、通常動作に戻る
            pending, st = run(crawl.crawl_matsuya, SynthMatsuya(self._list(40, 0, 2)), "2026-10-17")
            self.assertNotIn(crawl.BURST_KEY, pending["matsuya"])
            self.assertTrue(st["list_ok"])

    def test_summary_file_carries_suspected(self):
        with TempData() as base:
            self._setup(40)
            stats: dict = {}
            crawl.crawl_matsuya(SynthMatsuya(self._list(40, 0, 20)), {}, stats, "2026-10-14")
            crawl.save_run_summary(stats)
            data = json.loads((base / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(data["new"]["matsuya"], 0)
            self.assertEqual(data["suspected"]["matsuya"]["count"], 20)

    def test_burst_filter_unit(self):
        with TempData():
            for i in range(5):
                seed("sukiya", f"s{i}", f"s{i}", f"s{i}")
            items = {f"s{i}": {} for i in range(5)} | {f"n{i}": {} for i in range(11)}
            stats = {"sukiya": crawl.new_stats()}
            stats["sukiya"]["list_ok"] = True
            pending: dict = {}
            # baseline が無ければ対象外
            self.assertEqual(crawl.burst_filter("sukiya", items, None, pending, stats, "2026-10-14"), items)
            out = crawl.burst_filter("sukiya", items, {f"s{i}" for i in range(5)}, pending, stats, "2026-10-14")
            self.assertEqual(set(out), {f"s{i}" for i in range(5)})
            self.assertFalse(stats["sukiya"]["list_ok"])
            # 通常の日(数件)は掛からない
            stats["sukiya"]["list_ok"] = True
            few = {f"s{i}": {} for i in range(5)} | {f"n{i}": {} for i in range(3)}
            self.assertEqual(crawl.burst_filter("sukiya", few, {f"s{i}" for i in range(5)}, pending, stats, "2026-10-15"), few)
            self.assertTrue(stats["sukiya"]["list_ok"])


@helpers.needs_fixtures
class TestBurstGuardDoesNotHitNormalFixtures(unittest.TestCase):
    """実物のスナップショットで、初回の件数(吉野家・すき家・かつや)が歯止めに掛からないこと。"""

    def test_first_runs_and_repeat_runs_are_not_held(self):
        import test_sukiya
        import test_yoshinoya
        for mod, fn, chain in ((test_sukiya, "run_s", "sukiya"), (test_yoshinoya, "run_y", "yoshinoya")):
            run_fn = getattr(mod, fn, None)
            if run_fn is None:
                continue
            with TempData():
                _, st1 = run_fn(mod.getter(), "2026-10-11")
                self.assertNotIn("suspected_restructure", st1)
                self.assertGreater(st1["new_products"], 0)
                _, st2 = run_fn(mod.getter(), "2026-10-12")
                self.assertNotIn("suspected_restructure", st2)
                self.assertTrue(st2["list_ok"])


class TestMigration(unittest.TestCase):
    def _data(self, base: Path):
        for pid, kw in (
            ("curry-a_hp_261006", {"launch_date": "2026-10-06", "news_url": "u", "nutrition": {"kcal": 1},
                                   "nutrition_as_of": "2026-10-06", "missed_runs": 1, "last_miss_date": "2026-10-14"}),
            ("curry-b_hp_261006", {}),
        ):
            seed("matsuya", pid, pid, pid, **kw)
        seed("matsuya", "curry-a_hp_261013", "A", "curry-a_hp_261013",
             first_seen_at="2026-10-14T07:31:00+09:00", last_seen_at="2026-10-14", launch_date=None,
             same_name_as=["curry-a_hp_261006"], nutrition={"kcal": 2}, nutrition_as_of="2026-10-13",
             official_url="https://example.invalid/new.html", in_recommended=True)
        seed("matsuya", "curry-b_hp_261013", "B", "curry-b_hp_261013",
             first_seen_at="2026-10-14T07:31:00+09:00", last_seen_at="2026-10-14", in_recommended=False)  # 旧あり→統合
        seed("matsuya", "don-fake_hp_261013", "誤新規", "don-fake_hp_261013",
             first_seen_at="2026-10-14T07:31:00+09:00", last_seen_at="2026-10-14")  # baselineにある→削除
        seed("matsuya", "don-fakerec_hp_261013", "おすすめ中", "don-fakerec_hp_261013",
             first_seen_at="2026-10-14T07:31:00+09:00", last_seen_at="2026-10-14", in_recommended=True)  # 残す
        seed("matsuya", "limited-real_hp_261013", "本物", "limited-real_hp_261013",
             first_seen_at="2026-10-14T07:31:00+09:00", last_seen_at="2026-10-14")  # baselineに無い→残す
        seed("matsuya", "don-older_hp_260811", "古い", "don-older_hp_260811",
             first_seen_at="2026-10-11T07:31:00+09:00", last_seen_at="2026-10-14", in_recommended=False)  # 日付更新前から→残す
        seed("matsunoya", "teishoku-x_hp_260916", "松のや", "teishoku-x_hp_260916",
             first_seen_at="2026-10-14T07:31:00+09:00")  # 松のやは削除しない
        (base / "baseline_ids.json").write_text(json.dumps({
            "matsuya": ["curry-a_hp_261006", "curry-b_hp_261006", "don-fake_hp_261006", "don-fakerec_hp_261006",
                        "don-older_hp_260811", "gyumeshi-gyumeshi_hp_261006"],
            "matsunoya": ["teishoku-x_hp_260916", "teishoku-y_hp_260916"],
            "sukiya": ["1", "2"]}), encoding="utf-8")
        (base / "pending.json").write_text(json.dumps({
            "matsuya": {"gyumeshi-gyumeshi_hp_261013": {"reason": "404"}, "limited-real2_hp_261013": {"reason": "404"}},
            "yoshinoya": {"x": {"reason": "404"}}}), encoding="utf-8")

    def _ids(self, base, chain):
        return sorted(p.stem for p in (base / "products" / chain).glob("*.json"))

    def test_migrate(self):
        with TempData() as base:
            self._data(base)
            rep = mig.migrate(base, since="2026-10-14")
            self.assertEqual(self._ids(base, "matsuya"),
                             ["curry-a_hp", "curry-b_hp", "don-fakerec_hp", "don-older_hp", "limited-real_hp"])
            self.assertEqual(self._ids(base, "matsunoya"), ["teishoku-x_hp"])
            self.assertEqual([d["old_id"] for d in rep["chains"]["matsuya"]["deleted"]], ["don-fake_hp_261013"])
            self.assertEqual(len(rep["chains"]["matsuya"]["merged"]), 2)
            a = json.loads((base / "products/matsuya/curry-a_hp.json").read_text(encoding="utf-8"))
            self.assertEqual(a["product_id"], "curry-a_hp")
            self.assertEqual(a["first_seen_at"], "2026-10-01T07:00:00+09:00")  # 古いほう
            self.assertEqual(a["launch_date"], "2026-10-06")
            self.assertEqual(a["news_url"], "u")
            self.assertEqual(a["price_history"], [{"date": "2026-10-01", "price": 500}])
            self.assertEqual(a["current_slug"], "curry-a_hp_261013")  # 新しいほう
            self.assertEqual(a["official_url"], "https://example.invalid/new.html")
            self.assertEqual(a["nutrition"], {"kcal": 2})
            self.assertEqual((a["status"], a["missed_runs"], a["last_miss_date"]), ("on_sale", 0, None))
            self.assertEqual(a["last_seen_at"], "2026-10-14")
            self.assertTrue(a["in_recommended"])
            self.assertNotIn("same_name_as", a)
            # 他チェーンの baseline は触らない。松屋は基本キーに
            b = json.loads((base / "baseline_ids.json").read_text(encoding="utf-8"))
            self.assertEqual(b["sukiya"], ["1", "2"])
            self.assertIn("curry-a_hp", b["matsuya"])
            self.assertTrue(all(not i[-6:].isdigit() or "_" not in i[-7:] for i in b["matsuya"]))
            # pending: baselineにある定番の404は消し、本当の新規候補のキーは基本キーに。他チェーンはそのまま
            pe = json.loads((base / "pending.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(pe["matsuya"]), ["limited-real2_hp"])
            self.assertEqual(pe["yoshinoya"], {"x": {"reason": "404"}})

    def test_idempotent(self):
        with TempData() as base:
            self._data(base)
            mig.migrate(base, since="2026-10-14")
            snap = {p.name: p.read_text(encoding="utf-8")
                    for p in list(base.rglob("*.json"))}
            rep2 = mig.migrate(base, since="2026-10-14")
            snap2 = {p.name: p.read_text(encoding="utf-8") for p in list(base.rglob("*.json"))}
            self.assertEqual(snap, snap2)
            for rep in rep2["chains"].values():
                self.assertEqual((rep["merged"], rep["deleted"], rep["renamed"]), ([], [], 0))

    def test_dry_run_writes_nothing(self):
        with TempData() as base:
            self._data(base)
            before = self._ids(base, "matsuya")
            mig.migrate(base, since="2026-10-14", dry_run=True)
            self.assertEqual(self._ids(base, "matsuya"), before)

    def test_no_data_dir_is_fine(self):
        with tempfile.TemporaryDirectory() as t:
            rep = mig.migrate(Path(t))
            self.assertEqual(rep["chains"]["matsuya"]["before"], 0)


class TestNotify(unittest.TestCase):
    def test_held_summary_says_zero_new_with_hold(self):
        s = {"new": {"matsuya": 0}, "ended": {}, "suspected": {"matsuya": {"count": 37, "threshold": 25, "consecutive_days": 1}}}
        title, body = notify.build_message(s)
        self.assertIn("新規0件", title)
        self.assertIn("保留あり", title)
        self.assertIn("松屋", body)

    def test_held_plus_new_elsewhere(self):
        s = {"new": {"matsuya": 0, "mos": 2}, "ended": {}, "suspected": {"matsuya": {"count": 37, "consecutive_days": 1}}}
        title, body = notify.build_message(s)
        self.assertIn("新メニュー 2件", title)
        self.assertIn("保留あり", title)
        self.assertNotIn("新規0件", title)

    def test_no_hold_unchanged(self):
        self.assertIsNone(notify.build_message({"new": {"mos": 0}, "ended": {}, "suspected": {}}))
        self.assertIsNone(notify.build_message({"new": {"mos": 0}, "ended": {}}))

    def test_warning_and_escalation(self):
        w = notify.build_warnings({"suspected": {"matsuya": {"count": 37, "threshold": 25, "consecutive_days": 1}}})
        self.assertEqual(len(w), 1)
        self.assertIn("新規候補が一度に37件", w[0][1])
        self.assertIn("記録を保留", w[0][1])
        w3 = notify.build_warnings({"suspected": {"matsuya": {"count": 37, "threshold": 25, "consecutive_days": 3}}})
        self.assertIn("3日連続", w3[0][0])
        self.assertIn("判断", w3[0][1])
        self.assertEqual(notify.build_warnings({"new": {}, "ended": {}}), [])

    def test_read_summary_suspected(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "s.json"
            p.write_text(json.dumps({"new": {}, "ended": {}, "suspected": {"mos": {"count": 20}}}), encoding="utf-8")
            self.assertEqual(notify.read_run_summary(p)["suspected"], {"mos": {"count": 20}})
            self.assertEqual(notify.read_run_summary(Path(t) / "none.json")["suspected"], {})


if __name__ == "__main__":
    unittest.main()
