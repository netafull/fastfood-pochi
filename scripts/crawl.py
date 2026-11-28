#!/usr/bin/env python3
"""マクドナルド・モスバーガー・松屋・松のや・吉野家・すき家・かつや・からやま・丸亀製麺・バーガーキング・幸楽苑・日高屋・ゼッテリア・ケンタッキー・やよい軒・CoCo壱番屋の限定メニュー・新メニューを巡回し、
data/products/{mcdonalds|mos|matsuya|matsunoya|yoshinoya|sukiya|katsuya|karayama|marugame|burgerking|kourakuen|hidakaya|zetteria|kfc|yayoiken|cocoichi}/{id}.json に1商品1ファイルで蓄積する。

このサイトの核は「販売状況の追跡」。公式サイトは販売が終わると商品を消すので、
一覧に居た商品が消えたら終了とみなして記録を残す。

・既存ファイルは丸ごと上書きしない(初回取得が正)。更新するのは last_seen_at /
  status / price_history 等の追跡用の項目だけ。
・終了判定は「そのチェーンの一覧取得が全部成功した回」だけ行う。一時的な取得失敗で
  商品が一覧から消えて見えても、終了扱いにしない。
・さらに「連続2回(別の日)見つからなかった」ときに初めて status=ended にする。
・取得失敗はチェーン単位で隔離し(data/pending.json)、他のチェーンへ波及させない。
・ブロック(403・チャレンジ)されたらUA偽装などに切り替えず、止まって報告する。
"""

from __future__ import annotations

import datetime
import json
import re
import sys
import time
import urllib.robotparser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import parsers  # noqa: E402
from fetch import BlockedError, NotFoundError, fetch, fetch_binary, fetch_post  # noqa: E402
import pdf_update  # noqa: E402

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA = ROOT / "data"
PRODUCTS = DATA / "products"
PENDING_PATH = DATA / "pending.json"
BASELINE_PATH = DATA / "baseline_ids.json"
RUN_SUMMARY = DATA / "run_summary.json"
PDF_STATE_PATH = DATA / "pdf_state.json"  # 吉野家・すき家の公式PDFの版・指紋(PDFの内容は入れない)
JST = datetime.timezone(datetime.timedelta(hours=9))

UA = CONFIG.get("user_agent", "FastfoodPochi/1.0 (+https://fastfood.netaful.jp/)")
INTERVALS = CONFIG.get("request_interval_seconds", {})
MAX_REQUESTS = CONFIG.get("max_requests_per_run", {})
MCD_BASE = parsers.MCD_BASE
MOS_BASE = parsers.MOS_BASE
MATSUYA_BASE = parsers.MATSUYA_BASE
# 連続何回(別の日)見つからなければ終了扱いにするか
MISSES_TO_END = 2


def now_iso() -> str:
    return datetime.datetime.now(JST).isoformat(timespec="seconds")


def today_jst() -> str:
    return datetime.datetime.now(JST).date().isoformat()


# ---------------------------------------------------------------------------
# 保存まわり
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj, **kw) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, **kw), encoding="utf-8")


def product_path(chain: str, product_id: str) -> Path:
    return PRODUCTS / chain / f"{product_id}.json"


def save_product(chain: str, product: dict) -> None:
    # 定番として記録した商品が後から限定・新メニューに変わったら(limited_kind が「定番」でなくなったら)、定番の印を外す
    if product.get("is_regular") and product.get("limited_kind") != REGULAR_KIND:
        product["is_regular"] = False
    save_json(product_path(chain, product["product_id"]), product)


def load_product(chain: str, product_id: str) -> dict | None:
    return load_json(product_path(chain, product_id), None)


def load_chain_products(chain: str) -> list[dict]:
    out = []
    d = PRODUCTS / chain
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            p = load_json(f, None)
            if p:
                out.append(p)
    return out


class Throttle:
    """チェーンごとに最後のリクエスト時刻を覚え、間隔を守って待つ。"""

    def __init__(self):
        self.last: dict[str, float] = {}

    def wait(self, chain: str) -> None:
        interval = max(INTERVALS.get(chain, 3), 3)  # 3秒未満にはしない
        last = self.last.get(chain)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < interval:
                time.sleep(interval - elapsed)
        self.last[chain] = time.monotonic()


class Getter:
    """リクエスト数の上限つきの取得器。テストでは差し替える。"""

    def __init__(self):
        self.throttle = Throttle()
        self.counts: dict[str, int] = {}

    def _tick(self, chain: str) -> None:
        limit = MAX_REQUESTS.get(chain)
        if limit is not None and self.counts.get(chain, 0) >= limit:
            raise RuntimeError(f"{chain}: 1回の実行あたりのリクエスト上限({limit})に達しました")
        self.throttle.wait(chain)
        self.counts[chain] = self.counts.get(chain, 0) + 1

    def __call__(self, chain: str, url: str) -> str:
        self._tick(chain)
        return fetch(url, UA)

    # バーガーキング用。公式サイトが使う内部API(POST)。他と同じ間隔・上限・カウントに従う。応答の本文(JSON文字列)を返す
    def post_json(self, chain: str, trcode: str, body: dict) -> str:
        self._tick(chain)
        return fetch_post(parsers.bk_url(trcode), parsers.bk_request_form(trcode, body), UA)

    # 公式PDF(吉野家・すき家の栄養成分)用。HEAD(版の確認)とGET(本体)。どちらも同じ間隔・上限・カウントに従う
    def pdf_head(self, chain: str, url: str) -> dict:
        self._tick(chain)
        return fetch_binary(url, UA, method="HEAD")[1]

    def pdf_get(self, chain: str, url: str, allow_html: bool = False) -> bytes:
        self._tick(chain)
        return fetch_binary(url, UA, allow_html=allow_html)[0]


# ---------------------------------------------------------------------------
# 販売状況の追跡(チェーン共通)
# ---------------------------------------------------------------------------

def record_price(product: dict, price, today: str, st: dict | None = None) -> bool:
    """価格を price_history に記録する。前回と違えば追記して True。価格が取れなかった(None)回は何も書かない。
    初回(履歴が空)の記録は「変更」に数えない。変更なら stats の price_changes を1つ増やす。"""
    if price is None:
        return False
    hist = product.setdefault("price_history", [])
    last = hist[-1].get("price") if hist else None
    if last == price:
        return False
    hist.append({"date": today, "price": price})
    product["price"] = price
    if hist[:-1] and st is not None:
        st["price_changes"] = st.get("price_changes", 0) + 1
    return True


def touch_seen(product: dict, entry_price: int | None, today: str, now: str, st: dict | None = None) -> bool:
    """一覧に居た既存商品の追跡項目を更新する。変更があれば True。

    ・last_seen_at を更新、status=on_sale、見失った回数をリセット
    ・終了後に再登場したら on_sale に戻し、relaunches に記録
    ・一覧の価格が前回と違えば price_history に追記(価格が取れなかった回は書かない。st があれば価格変更を数える)
    """
    changed = False
    if product.get("status") == "ended":
        product.setdefault("relaunches", []).append({
            "ended_observed_at": product.get("ended_observed_at"),
            "reappeared_at": today,
        })
        product["status"] = "on_sale"
        product["ended_observed_at"] = None
        changed = True
    elif product.get("status") != "on_sale":
        product["status"] = "on_sale"
        changed = True
    if product.get("missed_runs"):
        product["missed_runs"] = 0
        product["last_miss_date"] = None
        changed = True
    product["last_seen_at"] = today  # 日付だけ(同じ日に複数回実行してもgitの差分が増えないように)
    changed = True

    product.setdefault("price_history", [])
    record_price(product, entry_price, today, st)
    return changed


def apply_missing(chain: str, seen_ids: set[str], today: str, stats: dict) -> None:
    """一覧取得が全部成功した回にだけ呼ぶ。一覧に居なかった販売中の商品を数え、
    連続 MISSES_TO_END 回(別の日)見つからなければ status=ended にする。"""
    for p in load_chain_products(chain):
        if p["product_id"] in seen_ids:
            continue
        if p.get("status") != "on_sale":
            continue
        if p.get("last_miss_date") == today:
            continue  # 同じ日に何度実行しても1回と数える
        p["missed_runs"] = int(p.get("missed_runs") or 0) + 1
        p["last_miss_date"] = today
        if p["missed_runs"] >= MISSES_TO_END:
            p["status"] = "ended"
            p["ended_observed_at"] = today
            stats["ended"] = stats.get("ended", 0) + 1
        save_product(chain, p)


def new_stats() -> dict:
    return {"list_requests": 0, "detail_requests": 0, "new_products": 0, "ended": 0,
            "failed": 0, "blocked": False, "list_ok": False, "limited_seen": 0,
            "regular_created": 0, "regular_deferred": 0, "price_changes": 0}


# ---------------------------------------------------------------------------
# 定番メニューの記録と価格の変遷
# ---------------------------------------------------------------------------
# 限定・新メニューに加えて、定番(初回の巡回で baseline に入った既存メニュー)も商品レコードにする。目的は価格の変遷を
# 毎日たどれるようにすること。区別は limited_kind="定番" と is_regular=True。launch_date は null のまま、first_seen_at が記録開始日。
#  ・トッピング・セット類・料理でない案内など、各チェーンが元々記録から外しているものは定番にも入れない。
#  ・詳細ページを取らないと作れないチェーンは、1回の巡回で作る定番を regular_backfill_per_run 件までにする(残りは翌日以降)。
#    一覧だけで作れるチェーン(モス・かつや・からやま)は一度に作る。
#  ・定番は baseline に居る項目なので burst_filter の「新規候補」には数えない。通知の「新メニュー」にも数えない(regular_created)。
#  ・サイズ別価格(price_variants)が詳細ページにしか無いチェーンは、1回の巡回で variant_recheck_per_run 件ずつ、
#    最後に確認した日(price_checked_at)の古い順に詳細を取り直し、変わっていれば price_variants_history に追記する。
#  ・config の record_regular を false にすると、定番の記録は止まる(既存の記録の追跡は続く)。
# 注意: 定番の発売日・お知らせ(ニュース)の照合はしない。launch_date は null のまま。
REGULAR_KIND = "定番"


def regular_enabled() -> bool:
    return bool(CONFIG.get("record_regular", True))


def regular_limit() -> int:
    return int(CONFIG.get("regular_backfill_per_run", 20))


def variant_recheck_limit(chain: str | None = None) -> int:
    """数値ならどのチェーンも同じ件数。辞書ならチェーン名の値、無ければ "default"(既定5)。
    すき家は基本の価格が詳細にしか無いので多めにする(全商品の一巡を約1週間に収めるため)。"""
    v = CONFIG.get("variant_recheck_per_run", 5)
    if isinstance(v, dict):
        return int(v.get(chain, v.get("default", 5)))
    return int(v)


class RegularBudget:
    """定番の新規作成の枠。detail_needed=True(詳細ページを取るチェーン)は regular_backfill_per_run 件まで。
    失敗した試行も枠を使う(1回の巡回のリクエスト数に上限をかけるため)。"""

    def __init__(self, st: dict, detail_needed: bool = True):
        self.st = st
        self.enabled = regular_enabled()
        self.left = regular_limit() if detail_needed else None

    def take(self) -> bool:
        if not self.enabled:
            return False
        if self.left is None:
            return True
        if self.left <= 0:
            self.st["regular_deferred"] = self.st.get("regular_deferred", 0) + 1
            return False
        self.left -= 1
        return True


def list_price_for_history(st: dict, price):
    """複数ページの一覧を読むチェーン用。一覧の取得が全部成功した回だけ価格を履歴に使う。
    一部のページが取れなかった回は、別のページに載った(別の表示の)価格を拾ってしまい、実際には変わっていない価格が
    「変更」として残るため、その回は価格を書かない(None を渡す)。"""
    return price if st.get("list_ok") else None


def use_list_price(product: dict, entry_price, today: str) -> dict:
    """定番の最初の価格を、毎日見ている一覧の価格にそろえる。詳細ページの価格(サイズ別の代表など)を起点にすると、
    翌日の一覧の価格との差が「価格の変更」として残ってしまうため。"""
    if entry_price is not None:
        product["price"] = entry_price
        product["price_history"] = [{"date": today, "price": entry_price}]
    return product


def mark_regular(product: dict) -> dict:
    """新しく作った商品レコードを定番にする(限定・新メニューとして組み立てた項目を打ち消す)。"""
    product["limited_kind"] = REGULAR_KIND
    product["is_regular"] = True
    product["launch_date"] = None
    if "launch_date_source" in product:
        product["launch_date_source"] = "not_found"
    for k in ("planned_end_text", "news_url", "news_title", "news_date", "announced_at"):
        if k in product:
            product[k] = None
    if "in_recommended" in product:
        product["in_recommended"] = False
    return product


def upgrade_regular(product: dict, kind: str) -> None:
    """定番として記録済みの商品が、限定・新メニューとして一覧に載った。種別を上げて定番の印を外す。"""
    if product.get("limited_kind") == REGULAR_KIND:
        product["limited_kind"] = kind
        product["is_regular"] = False


def norm_variants(variants) -> list[dict]:
    return [{"label": v.get("label") or "", "price": v["price"]} for v in variants or [] if v.get("price") is not None]


def record_variants(product: dict, variants, today: str, st: dict | None = None) -> bool:
    """サイズ別の価格(2件以上)を price_variants_history に記録する。前回と違えば追記して price_variants も更新し True。
    取れなかった(None・空・1件以下)回は何も書かない。初回の記録は「変更」に数えない。"""
    new = norm_variants(variants)
    if len(new) < 2:
        return False
    hist = product.setdefault("price_variants_history", [])
    old = norm_variants(product.get("price_variants"))
    if not hist and old:
        hist.append({"date": (product.get("first_seen_at") or today)[:10], "variants": old})  # 記録済みの値を起点にする
    if hist and hist[-1]["variants"] == new:
        return False
    hist.append({"date": today, "variants": new})
    product["price_variants"] = [dict(v) for v in variants] if variants else new
    if len(hist) > 1 and st is not None:
        st["price_changes"] = st.get("price_changes", 0) + 1
    return len(hist) > 1


def seed_variants(product: dict, today: str) -> dict:
    """新しく作った商品のサイズ別価格を、履歴の最初の1件にする。確認日も入れる。"""
    product["price_checked_at"] = today
    new = norm_variants(product.get("price_variants"))
    if len(new) >= 2:
        product["price_variants_history"] = [{"date": today, "variants": new}]
    return product


def due_for_recheck(products: list[dict], entries: dict, need_variants: bool, today: str) -> list[dict]:
    """価格の取り直しの対象を、最後に確認した日(price_checked_at)の古い順に variant_recheck_per_run 件まで選ぶ。
    今の一覧に居る販売中の商品だけ(今日すでに詳細を取った商品は除く)。need_variants=True はサイズ別の価格を持つ商品だけ
    (基本の価格が一覧にある松屋・吉野家。すき家は価格が詳細にしか無いので全商品)。"""
    due = [p for p in products
           if p.get("status") == "on_sale" and p["product_id"] in entries
           and p.get("price_checked_at") != today
           and (not need_variants or p.get("price_variants"))]
    due.sort(key=lambda p: (p.get("price_checked_at") or "", str(p["product_id"])))
    chain = due[0].get("chain") if due else None
    return due[:variant_recheck_limit(chain)]


def baseline_for(chain: str) -> set[str] | None:
    data = load_json(BASELINE_PATH, {})
    ids = data.get(chain)
    return set(ids) if ids is not None else None


def save_baseline(chain: str, ids: set[str]) -> None:
    data = load_json(BASELINE_PATH, {})
    data[chain] = sorted(ids)
    save_json(BASELINE_PATH, data)


# ---------------------------------------------------------------------------
# 歯止め: 1回で「新規候補」が極端に多いときは、公式のページ構造・ID体系の変更とみなして記録しない
# ---------------------------------------------------------------------------
# 2026-10-13 に松屋が商品URLの日付(_YYMMDD)を一斉に更新し、定番メニューまで37件が「新規」になった。
# 識別キーを日付抜きにして根本は直したが、同種の事故(IDの付け替え・ページ構造の変更)を他チェーンでも
# 自動で止めるため、新規候補(baselineにも既存レコードにも無い項目)が max(new_burst_min, 一覧の項目数×new_burst_ratio)
# を超えたら、その回はそのチェーンの新規を記録せず、終了判定もしない。
# 初回(baselineがまだ無い)は対象外。保留は翌日以降に候補が閾値以下に戻れば自然に再開する。

BURST_KEY = "_suspected_restructure"
BURST_ESCALATE_DAYS = 3


def burst_threshold(list_size: int) -> int:
    lo = int(CONFIG.get("new_burst_min", 10))
    ratio = float(CONFIG.get("new_burst_ratio", 0.25))
    return max(lo, int(list_size * ratio))


def burst_filter(chain: str, items: dict, baseline: set[str] | None, pending: dict, stats: dict,
                 today: str) -> dict:
    """items({商品ID: 一覧の項目})を受け取り、歯止めが働かなければそのまま返す。
    働いたら、既存レコードのある項目だけを返し(最終確認日の更新だけは続ける)、
    stats[chain]["list_ok"]=False(baseline作成・終了判定を止める)と、保留の記録を残す。"""
    st = stats[chain]
    chain_pending = pending.setdefault(chain, {})
    if baseline is None:  # 初回は対象外
        chain_pending.pop(BURST_KEY, None)
        return items
    candidates = [pid for pid in items if pid not in baseline and load_product(chain, pid) is None]
    limit = burst_threshold(len(items))
    if len(candidates) <= limit:
        chain_pending.pop(BURST_KEY, None)
        return items
    prev = chain_pending.get(BURST_KEY) or {}
    if prev.get("date") == today:
        days = int(prev.get("consecutive_days") or 1)
    else:
        days = int(prev.get("consecutive_days") or 0) + 1
    chain_pending[BURST_KEY] = {
        "reason": "suspected_restructure", "count": len(candidates), "threshold": limit,
        "list_size": len(items), "date": today, "consecutive_days": days,
        "first_date": prev.get("first_date") or today,
        "sample": sorted(candidates)[:5],
    }
    st["suspected_restructure"] = {"count": len(candidates), "threshold": limit, "consecutive_days": days}
    st["list_ok"] = False
    msg = (f"[WARNING] {chain}: 新規候補が一度に{len(candidates)}件(閾値{limit}件・一覧{len(items)}件)。"
           "公式のサイト構造変更の疑い。この回は新規を記録せず、終了判定も行わない")
    if days >= BURST_ESCALATE_DAYS:
        msg += f" [{days}日連続で保留中。運営者の判断が必要]"
    print(msg, file=sys.stderr)
    return {pid: e for pid, e in items.items() if load_product(chain, pid) is not None}


# ---------------------------------------------------------------------------
# マクドナルド
# ---------------------------------------------------------------------------

def mcd_allowed_checker(robots_text: str | None):
    """robots.txt の Disallow に当たるパスは取得しない。robots.txt自体が取れなかった
    ときは、安全側に既知のDisallow(config)で判定する。"""
    rp = urllib.robotparser.RobotFileParser()
    if robots_text:
        rp.parse(robots_text.splitlines())
    else:
        rp.parse(["User-agent: *"] + [f"Disallow: {p}" for p in CONFIG.get("mcdonalds_fallback_disallow", [])])

    def allowed(path: str) -> bool:
        return rp.can_fetch(UA, MCD_BASE + path)
    return allowed


def build_mcd_product(entry: dict, detail: dict, today: str, now: str, source_pages: list[str]) -> dict:
    short, full_len = parsers.shorten_description(detail.get("description"))
    # 発売日: 詳細ページのモバイルオーダー受付開始日が実際の発売日に一致する。一覧の
    # started-at は告知用の日付で、数日〜2週間ほど早いことがある(2026-10-07に確認)
    launch = detail.get("mop_start") or entry.get("list_started_at")
    price = detail.get("price") if detail.get("price") is not None else entry.get("price")
    end_text = None
    if detail.get("mop_end"):
        end_text = f"{detail['mop_end']}（モバイルオーダー受付期間の終了日）"
    kind = "期間限定" if entry.get("limited") else "新メニュー"
    return {
        "chain": "mcdonalds",
        "product_id": entry["product_id"],
        "name": entry["name"] or detail.get("name"),
        "category": None,
        "price": price,
        "price_note": "税込。「~」は最低価格表記。店舗・デリバリーで価格が異なる場合があります",
        "launch_date": launch,
        "planned_end_text": end_text,
        "mop_ended_at": detail.get("mop_end"),
        "list_started_at": entry.get("list_started_at"),
        "description": short,
        "description_full_len": full_len,
        "nutrition": detail.get("nutrition"),
        "allergens": detail.get("allergens"),
        "origin": detail.get("origin"),
        "limited_kind": kind,
        "official_url": MCD_BASE + entry["detail_url"],
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [{"date": today, "price": price}] if price is not None else [],
        "source_list": source_pages,
        "image": None,
    }


def crawl_mcdonalds(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = "mcdonalds"
    today = today or today_jst()
    now = now_iso()
    pending.setdefault(chain, {})
    stats.setdefault(chain, new_stats())
    st = stats[chain]

    # robots.txt(1リクエスト)。Disallowのパスには行かない
    robots_text = None
    try:
        robots_text = getter(chain, MCD_BASE + "/robots.txt")
        st["list_requests"] += 1
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] mcdonalds robots.txt: {e}", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] mcdonalds robots.txt 取得失敗(既知のDisallowで代替): {e}", file=sys.stderr)
    allowed = mcd_allowed_checker(robots_text)

    pages = [p["path"] for p in CONFIG["mcdonalds_pages"]]
    page_labels = {p["path"]: p.get("label") for p in CONFIG["mcdonalds_pages"]}
    entries: dict[str, dict] = {}
    sources: dict[str, list[str]] = {}
    all_ok = True
    for path in pages:
        if not allowed(path):
            print(f"[skip] robots.txtでDisallow: {path}", file=sys.stderr)
            all_ok = False
            continue
        try:
            html = getter(chain, MCD_BASE + path)
            st["list_requests"] += 1
        except BlockedError as e:
            st["blocked"] = True
            print(f"[blocked] mcdonalds list {path}: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            print(f"[warn] mcdonalds list {path} 取得失敗: {e}", file=sys.stderr)
            all_ok = False
            continue
        page_entries = parsers.parse_mcd_list(html)
        if not page_entries:
            # 想定外の構造(カードが0件)は成功扱いにしない。商品が消えたと誤認しないため
            print(f"[warn] mcdonalds list {path}: 商品カードが0件(構造変更の疑い)", file=sys.stderr)
            all_ok = False
            continue
        for e in page_entries:
            sources.setdefault(e["product_id"], []).append(path)
            cur = entries.get(e["product_id"])
            if cur is None:
                entries[e["product_id"]] = e
            else:
                # 同じ商品が複数ページに出る。限定判定・開始日はどれか1つでも有れば採用
                cur["limited"] = cur["limited"] or e["limited"]
                cur["list_started_at"] = cur["list_started_at"] or e["list_started_at"]
                if cur.get("price") is None:
                    cur["price"] = e.get("price")
    st["list_ok"] = all_ok

    # セット商品は記録の対象外(名前で判定)
    singles = {pid: e for pid, e in entries.items() if not parsers.is_mcd_set(e["name"])}
    st["limited_seen"] = sum(1 for e in singles.values() if e["limited"])

    baseline = baseline_for(chain)
    singles = burst_filter(chain, singles, baseline, pending, stats, today)
    all_ok = all_ok and st["list_ok"]
    regular_budget = RegularBudget(st)
    for pid, e in singles.items():
        existing = load_product(chain, pid)
        if existing is not None:
            if touch_seen(existing, list_price_for_history(st, e.get("price")), today, now, st):
                existing["source_list"] = sorted(set(existing.get("source_list") or []) | set(sources[pid]))
                if e["limited"]:
                    upgrade_regular(existing, "期間限定")
                save_product(chain, existing)
            pending[chain].pop(pid, None)
            continue
        is_new_menu = baseline is not None and pid not in baseline
        is_regular = not (e["limited"] or is_new_menu)
        if is_regular and not regular_budget.take():
            continue  # 定番は詳細を取るので、1回の巡回で作る件数に上限がある。残りは翌日以降
        detail_path = e["detail_url"]
        if not allowed(detail_path):
            print(f"[skip] robots.txtでDisallow: {detail_path}", file=sys.stderr)
            continue
        try:
            html = getter(chain, MCD_BASE + detail_path)
            st["detail_requests"] += 1
        except BlockedError as ex:
            st["blocked"] = True
            print(f"[blocked] mcdonalds detail {detail_path}: {ex}", file=sys.stderr)
            return
        except NotFoundError:
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_path, "reason": "404", "checked_at": now}
            continue
        except Exception as ex:  # noqa: BLE001
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_path, "reason": f"fetch_error: {ex}", "checked_at": now}
            continue
        detail = parsers.parse_mcd_detail(html)
        if detail is None:
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_path, "reason": "empty_detail", "checked_at": now}
            continue
        product = build_mcd_product(e, detail, today, now, sorted(set(sources[pid])))
        # /menu/(おすすめ)以外で最初に出たページのラベルをカテゴリにする
        product["category"] = next(
            (page_labels[p] for p in sources[pid] if page_labels.get(p)), None)
        if is_regular:
            mark_regular(product)
            use_list_price(product, e.get("price"), today)
        save_product(chain, product)
        st["regular_created" if is_regular else "new_products"] += 1
        pending[chain].pop(pid, None)

    # 一覧取得が全部成功した回だけ、baseline作成と終了判定を行う
    if all_ok:
        if baseline is None:
            save_baseline(chain, set(singles))
        apply_missing(chain, set(singles) | set(entries), today, st)


# ---------------------------------------------------------------------------
# モスバーガー
# ---------------------------------------------------------------------------

def build_mos_product(entry: dict, category_name: str | None, c_id: str, allergy_map: dict,
                      is_limited_category: bool, today: str, now: str) -> dict:
    launch, end_text = parsers.parse_mos_period(entry.get("explain"))
    short, full_len = parsers.shorten_description(entry.get("explain"))
    kinds = entry.get("limited_kinds") or []
    if kinds:
        kind = "・".join(dict.fromkeys(kinds))
    elif is_limited_category:
        kind = "限定メニュー"
    else:
        kind = "新メニュー"
    price = entry.get("price")
    return {
        "chain": "mos",
        "product_id": entry["product_id"],
        "name": entry["name"],
        "category": category_name,
        "price": price,
        "price_note": None,
        "launch_date": launch,
        "planned_end_text": end_text,
        "description": short,
        "description_full_len": full_len,
        "nutrition": parsers.mos_nutrition(entry),
        "allergens": parsers.mos_allergens(entry, allergy_map),
        "origin": parsers.mos_origin(entry),
        "limited_kind": kind,
        "official_url": f"{MOS_BASE}/menu/detail/?menu_id={entry['product_id']}&c_id={c_id}",
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [{"date": today, "price": price}] if price is not None else [],
        "source_list": [f"menu_category/{c_id}"],
        "image": None,
    }


def crawl_mos(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = "mos"
    today = today or today_jst()
    now = now_iso()
    pending.setdefault(chain, {})
    stats.setdefault(chain, new_stats())
    st = stats[chain]
    all_ok = True

    def get(path: str) -> str:
        return getter(chain, MOS_BASE + path)

    # カテゴリ一覧。新しいカテゴリが増えても拾えるよう毎回取る
    cat_names: dict[str, str] = {}
    try:
        navi = json.loads(get("/data/menu/mst_navi.json"))
        st["list_requests"] += 1
        cat_names = {str(n["id"]): n.get("name") or "" for n in navi}
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] mos mst_navi: {e}", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] mos mst_navi 取得失敗(設定の固定リストで代替): {e}", file=sys.stderr)
        all_ok = False
    if not cat_names:
        cat_names = {str(c): "" for c in CONFIG["mos_category_ids_fallback"]}
    # 限定メニュー(26)を先に見る(公式ページURLの c_id に使うため)
    cat_ids = sorted(cat_names, key=lambda c: (c != parsers.LIMITED_CATEGORY_ID, c))

    entries: dict[str, dict] = {}
    categories: dict[str, list[str]] = {}
    for c_id in cat_ids:
        try:
            text = get(f"/data/menu/menu_category/{c_id}.json")
            st["list_requests"] += 1
            es = parsers.parse_mos_list(text, c_id)
        except BlockedError as e:
            st["blocked"] = True
            print(f"[blocked] mos category {c_id}: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            print(f"[warn] mos category {c_id} 取得失敗: {e}", file=sys.stderr)
            all_ok = False
            continue
        if not es and c_id == parsers.LIMITED_CATEGORY_ID:
            pass  # 限定メニューが0件の時期は有りうる
        for e in es:
            categories.setdefault(e["product_id"], []).append(c_id)
            if e["product_id"] not in entries:
                entries[e["product_id"]] = e
            else:
                cur = entries[e["product_id"]]
                for k in e["limited_kinds"]:
                    if k not in cur["limited_kinds"]:
                        cur["limited_kinds"].append(k)
    if not entries:
        all_ok = False  # 全体で0件は異常(構造変更・取得失敗)
    st["list_ok"] = all_ok

    baseline = baseline_for(chain)
    entries = burst_filter(chain, entries, baseline, pending, stats, today)
    all_ok = all_ok and st["list_ok"]
    allergy_map: dict[str, str] | None = None
    regular_budget = RegularBudget(st, detail_needed=False)  # モスは一覧だけで作れるので件数の上限は無い
    for pid, e in entries.items():
        cats = categories[pid]
        in_limited = parsers.LIMITED_CATEGORY_ID in cats
        limited = in_limited or bool(e["limited_kinds"])
        if limited:
            st["limited_seen"] += 1
        existing = load_product(chain, pid)
        if existing is not None:
            touch_seen(existing, list_price_for_history(st, e.get("price")), today, now, st)
            if limited:
                kinds = e.get("limited_kinds") or []
                upgrade_regular(existing, "・".join(dict.fromkeys(kinds)) if kinds else "限定メニュー")
            save_product(chain, existing)
            continue
        is_new_menu = baseline is not None and pid not in baseline
        is_regular = not (limited or is_new_menu)
        if is_regular and not regular_budget.take():
            continue  # 定番の記録を止めているとき
        if allergy_map is None:
            try:
                allergy_map = parsers.parse_mos_allergy_map(get("/data/menu/mst_allergy.json"))
                st["detail_requests"] += 1
            except BlockedError as ex:
                st["blocked"] = True
                print(f"[blocked] mos mst_allergy: {ex}", file=sys.stderr)
                return
            except Exception as ex:  # noqa: BLE001
                # 対応表が取れなくてもアレルゲンの品目名が「品目N」になるだけで記録は進める
                print(f"[warn] mos mst_allergy 取得失敗: {ex}", file=sys.stderr)
                allergy_map = {}
        c_id = parsers.LIMITED_CATEGORY_ID if in_limited else cats[0]
        non_limited_cats = [c for c in cats if c != parsers.LIMITED_CATEGORY_ID]
        category_name = cat_names.get(non_limited_cats[0]) if non_limited_cats else cat_names.get(c_id)
        product = build_mos_product(e, category_name, c_id, allergy_map, in_limited, today, now)
        if is_regular:
            mark_regular(product)
        save_product(chain, product)
        st["regular_created" if is_regular else "new_products"] += 1

    if all_ok:
        if baseline is None:
            save_baseline(chain, set(entries))
        apply_missing(chain, set(entries), today, st)


# ---------------------------------------------------------------------------
# 松屋
# ---------------------------------------------------------------------------
# 松屋には「期間限定」「NEW」の公式ラベルも販売期間も無い。記録の対象は
#  (a) 「おすすめ」一覧(/matsuya/menu/limited/)に載ったことがある商品
#  (b) baseline(初回巡回時の全メニューID)に無く、後から現れた商品
# 終了判定は「おすすめ」ではなく全カテゴリの一覧からの消失で行う。
# 発売日と説明文はニュースJSON(whatsnew.json)の category=menu の記事から取る。

# 見出しに商品名が無いとき、記事本文を探しに行くニュース記事の最大数(1回の実行あたり)。
# 「ネオカシミール」のように1本の記事が複数商品をまとめて紹介することがあり、
# 見出しには代表名しか出ないため
MATSUYA_FALLBACK_ARTICLE_FETCHES = 4


def matsuya_category_labels(chain: str = "matsuya") -> dict[str, str | None]:
    return {c["slug"]: c.get("label") for c in CONFIG.get(f"{chain}_categories", [])}


class MatsuyaNews:
    """ニュースJSONと記事ページの取得・照合。新規商品があるときだけ取りに行き、
    同じ記事は1回の実行で1回しか取らない。"""

    def __init__(self, getter, st: dict, chain: str = "matsuya"):
        self.getter = getter
        self.st = st
        self.chain = chain
        self.news: list[dict] | None = None
        self.news_failed = False
        self.articles: dict[str, dict | None] = {}
        self.fallback_fetches = 0

    def _get(self, url: str) -> str:
        text = self.getter(self.chain, url)
        self.st["detail_requests"] += 1
        return text

    def load(self) -> list[dict]:
        if self.news is None and not self.news_failed:
            try:
                self.news = parsers.parse_matsuya_news(self._get(MATSUYA_BASE + parsers.matsuya_news_json_path(self.chain)))
            except BlockedError:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"[warn] {self.chain} whatsnew.json 取得失敗: {e}", file=sys.stderr)
                self.news_failed = True
        return self.news or []

    def article(self, url: str) -> dict | None:
        if url not in self.articles:
            try:
                self.articles[url] = parsers.parse_matsuya_article(self._get(url))
            except BlockedError:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"[warn] {self.chain} 記事取得失敗 {url}: {e}", file=sys.stderr)
                self.articles[url] = None
        return self.articles[url]

    def resolve(self, name: str, product_id: str, today: str) -> dict | None:
        """商品に対応するニュース記事を探す。戻り値は
        {"news": {...}, "article": {...}|None, "how": "title"|"body"} または None。"""
        news = self.load()
        if not news:
            return None
        center = parsers.matsuya_slug_date(product_id) or today
        hit = parsers.match_matsuya_news_title(name, news, center, self.chain)
        if hit:
            return {"news": hit, "article": self.article(hit["url"]), "how": "title"}
        # 見出しに無い: 掲載日付近のニュース記事の本文に商品名があるか調べる
        try:
            c = datetime.date.fromisoformat(center)
        except ValueError:
            return None
        lo, hi = c - datetime.timedelta(days=14), c + datetime.timedelta(days=7)
        cands = parsers.matsuya_name_variants(name, self.chain)
        for a in news:
            try:
                d = datetime.date.fromisoformat(a["date"])
            except ValueError:
                continue
            if not (lo <= d <= hi) or not parsers.matsuya_news_launch_words(a["title"]):
                continue
            if a["url"] not in self.articles:
                if self.fallback_fetches >= MATSUYA_FALLBACK_ARTICLE_FETCHES:
                    continue
                self.fallback_fetches += 1
            art = self.article(a["url"])
            if art and any(cn in parsers.normalize_for_match(art["text"]) for cn in cands):
                return {"news": a, "article": art, "how": "body"}
        return None


def build_matsuya_product(entry: dict, detail: dict, sources: list[str], in_limited: bool,
                          resolved: dict | None, same_name: list[str], today: str, now: str,
                          chain: str = "matsuya") -> dict:
    labels = matsuya_category_labels(chain)
    limited_cat = parsers.MATSUYA_FAMILY[chain]["limited"]
    note = detail.get("nutrition_note")
    prices = parsers.matsuya_variant_labels(detail.get("prices") or [], note)
    nutr = parsers.matsuya_variant_labels(detail.get("nutrition_variants") or [], note)
    primary_price = parsers.matsuya_pick_primary(prices)
    primary_nut = parsers.matsuya_pick_primary(nutr)
    price = primary_price["price"] if primary_price else entry.get("price")

    news = resolved["news"] if resolved else None
    article = resolved.get("article") if resolved else None
    launch = article.get("launch_date") if article else None
    if news is None:
        launch_source = "not_found"
        kind = "おすすめ掲載" if in_limited else "新規掲載"
    else:
        launch_source = ("news_article" if resolved["how"] == "title" else "news_article_body") if launch \
            else "news_without_date"
        kind = "新メニュー" if parsers.matsuya_news_launch_words(news["title"]) else (
            "おすすめ掲載" if in_limited else "新規掲載")
    short, full_len = parsers.shorten_description(article.get("description_src") if article else None)

    # 複数の価格(セット/単品、小盛/並盛/大盛)の内訳は price_variants に持ち、ページでは表にする。
    # 表示する price は ラベル無し(セット等)→並盛→先頭 の順で選んだもの
    price_note = "税込。"
    if len(prices) > 1 and primary_price["label"]:
        price_note += f"表示は「{primary_price['label']}」の価格。"
    price_note += "店舗により価格・取り扱いが異なる場合があります。"

    category = next((labels.get(c) for c in sources if c != limited_cat and labels.get(c)), None)
    product = {
        "chain": chain,
        "product_id": entry["product_id"],
        "name": entry["name"] or detail.get("name"),
        "category": category,
        "price": price,
        "price_note": price_note,
        "price_variants": prices if len(prices) > 1 else None,
        "launch_date": launch,
        "launch_date_source": launch_source,
        "planned_end_text": None,
        "news_url": news["url"] if news else None,
        "news_title": news["title"] if news else None,
        "news_date": news["date"] if news else None,
        "description": short,
        "description_full_len": full_len,
        "nutrition": primary_nut["nutrition"] if primary_nut else None,
        "nutrition_variants": ([{"label": v["label"], "nutrition": v["nutrition"]} for v in nutr]
                               if len(nutr) > 1 else None),
        "nutrition_as_of": detail.get("nutrition_as_of"),
        "nutrition_note": note,
        "allergens": detail.get("allergens"),
        "origin": None,
        "limited_kind": kind,
        "in_recommended": in_limited,
        "official_url": MATSUYA_BASE + entry["detail_path"],
        "current_slug": entry.get("current_slug"),
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [{"date": today, "price": price}] if price is not None else [],
        "source_list": sorted(set(sources)),
        "image": None,
    }
    seed_variants(product, today)
    if same_name:
        # 同じ名前の商品が別のslugで再登場した(slug末尾の日付は更新で変わりうる)。別レコードとして扱う
        product["same_name_as"] = same_name
    return product


def matsuya_discover_categories(index_html: str, chain: str = "matsuya") -> list[str]:
    slugs = []
    for m in re.finditer(rf'href="/{chain}/menu/([a-z0-9_]+)/index\.html"', index_html):
        if m.group(1) not in slugs:
            slugs.append(m.group(1))
    return slugs


def crawl_matsuya(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    crawl_matsuya_family("matsuya", getter, pending, stats, today)


def crawl_matsunoya(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    crawl_matsuya_family("matsunoya", getter, pending, stats, today)


def crawl_matsuya_family(chain: str, getter, pending: dict, stats: dict, today: str | None = None) -> None:
    """松屋フーズ系(松屋・松のや)の巡回。構造は同じで、チェーンごとに接頭辞・おすすめカテゴリが違う。"""
    fam = parsers.MATSUYA_FAMILY[chain]
    limited_cat = fam["limited"]
    no_record = fam["no_record"]
    menu_root = parsers.matsuya_menu_root(chain)
    today = today or today_jst()
    now = now_iso()
    pending.setdefault(chain, {})
    stats.setdefault(chain, new_stats())
    st = stats[chain]
    all_ok = True

    # robots.txt(1リクエスト)。Disallowのパスには行かない(2026-10-11時点は全許可)
    robots_text = None
    try:
        robots_text = getter(chain, MATSUYA_BASE + "/robots.txt")
        st["list_requests"] += 1
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] {chain} robots.txt: {e}", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {chain} robots.txt 取得失敗(全許可として続行): {e}", file=sys.stderr)
    rp = urllib.robotparser.RobotFileParser()
    rp.parse((robots_text or "User-agent: *\nAllow: /").splitlines())

    def allowed(path: str) -> bool:
        return rp.can_fetch(UA, MATSUYA_BASE + path)

    # カテゴリの入口。新しいカテゴリが増えても拾えるよう毎回取る
    config_cats = [c["slug"] for c in CONFIG[f"{chain}_categories"]]
    cats: list[str] = []
    try:
        idx = getter(chain, MATSUYA_BASE + menu_root)
        st["list_requests"] += 1
        cats = matsuya_discover_categories(idx, chain)
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] {chain} menu index: {e}", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {chain} メニュー入口 取得失敗(設定の固定リストで代替): {e}", file=sys.stderr)
    if not cats:
        cats = list(config_cats)
        all_ok = False
    cats = sorted(cats, key=lambda c: c != limited_cat)  # おすすめを先頭に(安定ソート)

    entries: dict[str, dict] = {}
    sources: dict[str, list[str]] = {}
    for cat in cats:
        path = f"{menu_root}{cat}/index.html"
        if not allowed(path):
            print(f"[skip] robots.txtでDisallow: {path}", file=sys.stderr)
            all_ok = False
            continue
        try:
            html = getter(chain, MATSUYA_BASE + path)
            st["list_requests"] += 1
        except BlockedError as e:
            st["blocked"] = True
            print(f"[blocked] {chain} list {path}: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {chain} list {path} 取得失敗: {e}", file=sys.stderr)
            all_ok = False
            continue
        page_entries = parsers.parse_matsuya_list(html, chain)
        if not page_entries:
            print(f"[warn] {chain} list {path}: 商品が0件(構造変更の疑い)", file=sys.stderr)
            all_ok = False
            continue
        for e in page_entries:
            sources.setdefault(e["product_id"], []).append(cat)
            if e["product_id"] not in entries:
                entries[e["product_id"]] = e
            else:
                cur = entries[e["product_id"]]
                # 日付違いのslugが同時に載っている場合は、新しいほうのURLを現在のものとする
                if (parsers.matsuya_slug_date(e["current_slug"]) or "") > (parsers.matsuya_slug_date(cur["current_slug"]) or ""):
                    cur["current_slug"] = e["current_slug"]
                    cur["detail_path"] = e["detail_path"]
                if cur.get("price") is None:
                    cur["price"] = e.get("price")
    st["list_ok"] = all_ok and bool(entries)

    singles = {pid: e for pid, e in entries.items() if not e["option"]}
    st["limited_seen"] = sum(1 for pid in singles if limited_cat in sources[pid])

    baseline = baseline_for(chain)
    singles = burst_filter(chain, singles, baseline, pending, stats, today)
    news = MatsuyaNews(getter, st, chain)
    existing_all = None  # 同名商品の検出用(新規に記録するときだけ読む)
    regular_budget = RegularBudget(st)

    def fetch_detail(pid: str, detail_path: str):
        """詳細を取得して解析する。失敗は pending に記録して None。ブロックは呼び出し元へ投げる。"""
        if not allowed(detail_path):
            print(f"[skip] robots.txtでDisallow: {detail_path}", file=sys.stderr)
            return None
        try:
            html = getter(chain, MATSUYA_BASE + detail_path)
            st["detail_requests"] += 1
        except NotFoundError:
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_path, "reason": "404", "checked_at": now}
            return None
        except BlockedError:
            raise
        except Exception as ex:  # noqa: BLE001
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_path, "reason": f"fetch_error: {ex}", "checked_at": now}
            return None
        detail = parsers.parse_matsuya_detail(html)
        if detail is None:
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_path, "reason": "empty_detail", "checked_at": now}
        return detail

    try:
        for pid, e in singles.items():
            existing = load_product(chain, pid)
            if existing is not None:
                was_ended = existing.get("status") == "ended"
                old_ended = existing.get("ended_observed_at")
                touch_seen(existing, list_price_for_history(st, e.get("price")), today, now, st)
                if was_ended:
                    # 日付抜きの基本キーなので、本当に終了した商品の再登場は同じレコードに戻ってくる。
                    # 昨季の期間は seasons に残し(relaunches は touch_seen が記録済み)、今季の発売日は未確定に戻す
                    yoshinoya_start_new_season(existing, old_ended, today)
                    st["relaunched"] = st.get("relaunched", 0) + 1
                existing["source_list"] = sorted(set(existing.get("source_list") or []) | set(sources[pid]))
                if limited_cat in sources[pid]:
                    existing["in_recommended"] = True
                    upgrade_regular(existing, "おすすめ掲載")
                # 公式の日付つきURLは更新で変わる。リンクは常に最新に保つ(識別は日付抜きの基本キー)
                existing["current_slug"] = e["current_slug"]
                existing["official_url"] = MATSUYA_BASE + e["detail_path"]
                save_product(chain, existing)
                pending[chain].pop(pid, None)
                continue
            in_limited = limited_cat in sources[pid]
            only_unrecorded = all(c in no_record for c in sources[pid])
            is_new_menu = baseline is not None and pid not in baseline
            if only_unrecorded and not in_limited:
                continue  # トッピング・セット類は、定番としても記録しない
            is_regular = not (in_limited or is_new_menu)
            if is_regular and not regular_budget.take():
                continue  # 定番は詳細を取るので、1回の巡回で作る件数に上限がある。残りは翌日以降
            detail = fetch_detail(pid, e["detail_path"])
            if detail is None:
                continue
            name = e["name"] or detail["name"]
            # 定番はニュースの照合をしない(発売日は付けない。ニュースJSON・記事の取得も増やさない)
            resolved = None if is_regular else news.resolve(name, e["current_slug"], today)
            if existing_all is None:
                existing_all = load_chain_products(chain)
            norm = parsers.normalize_for_match(name)
            same = [p["product_id"] for p in existing_all
                    if p["product_id"] != pid and parsers.normalize_for_match(p.get("name")) == norm]
            product = build_matsuya_product(e, detail, sources[pid], in_limited, resolved, same, today, now, chain)
            if is_regular:
                mark_regular(product)
                use_list_price(product, e.get("price"), today)
            save_product(chain, product)
            existing_all.append(product)
            st["regular_created" if is_regular else "new_products"] += 1
            if not is_regular:
                if resolved is None:
                    st["news_unmatched"] = st.get("news_unmatched", 0) + 1
                else:
                    st["news_matched"] = st.get("news_matched", 0) + 1
            pending[chain].pop(pid, None)

        # サイズ別の価格(詳細ページにしか無い)を、古い順に少数だけ取り直す。基本の価格は一覧で毎日見ているので触らない
        if st["list_ok"]:
            for p in due_for_recheck(load_chain_products(chain), singles, True, today):
                detail = fetch_detail(p["product_id"], singles[p["product_id"]]["detail_path"])
                if detail is None:
                    continue
                prices = parsers.matsuya_variant_labels(detail.get("prices") or [], detail.get("nutrition_note"))
                record_variants(p, prices, today, st)
                p["price_checked_at"] = today
                save_product(chain, p)
    except BlockedError as ex:
        st["blocked"] = True
        print(f"[blocked] {chain}: {ex}", file=sys.stderr)
        return

    # 一覧取得が全部成功した回だけ、baseline作成と終了判定を行う
    if st["list_ok"]:
        if baseline is None:
            save_baseline(chain, set(entries))
        apply_missing(chain, set(entries), today, st)


# ---------------------------------------------------------------------------
# 吉野家 (www.yoshinoya.com)
# ---------------------------------------------------------------------------
# 調査・方針(2026-10-11):
#  ・robots.txt はステータス404だが、本文は全許可(Yoast形式の「User-agent: * / Disallow:」空)。
#    404のときは全許可として扱い、取得できて本文があればその Disallow に従う。ボット対策なし(nginx)。
#  ・サイト規約 /sitepolicy/ は営利・非営利を問わず複製・転用を禁じ、営利目的の行為を禁じている
#    (自動取得への言及なし)。→ 記録するのは事実情報(価格・日付・販売状況)だけ。説明文・画像は
#    保存も表示もしない。広告は入れない。
#  ・栄養成分・アレルゲンは別PDF(/pdf/allergy/)。巡回のあと run_pdf_step() が pdf_update.py 経由で、
#    更新があったときだけ取得して読み取る(ここの crawl_yoshinoya では取得しない)。
#    お知らせの見出しのリンクもPDFだが同じく取得しない
#    (見出しの文言だけを読む)。
#  ・記録するのは (a) 「期間限定」一覧(/menu/gentei/)に載った商品、(b) baseline(初回巡回の全メニュー)に
#    無く後から現れた商品。定番の既存メニューは詳細を取らない。
#  ・商品IDは「カテゴリ-slug」。季節商品(月見牛とじ丼など)は年をまたいで同じslug・URLで再登場するので、
#    終了後に同じIDが一覧に戻ったら relaunches に記録して on_sale に戻し、昨季は seasons に残す。

YOSHINOYA_BASE = parsers.YOSHINOYA_BASE


def yoshinoya_category_labels(index_cats: list[dict] | None = None) -> dict[str, str | None]:
    labels = {c["slug"]: c.get("label") for c in CONFIG.get("yoshinoya_categories", [])}
    for c in index_cats or []:
        if c.get("label"):
            labels[c["slug"]] = c["label"]
    return labels


class YoshinoyaNews:
    """お知らせ一覧 /news/{年}/ の取得。年別ページなので、必要な年だけ、1回の実行で1回ずつ取る。
    見出しの文言だけを読み、リンク先のPDFは取得しない。"""

    def __init__(self, getter, st: dict):
        self.getter = getter
        self.st = st
        self.years: dict[int, list[dict]] = {}

    def ensure(self, lo: str, hi: str, today: str) -> None:
        """lo〜hi(ISO日付)にかかる年(今年まで)のお知らせを揃える。"""
        try:
            y0 = datetime.date.fromisoformat(lo).year
            y1 = min(datetime.date.fromisoformat(hi).year, datetime.date.fromisoformat(today).year)
        except ValueError:
            return
        for y in range(y0, y1 + 1):
            if y in self.years:
                continue
            self.years[y] = []
            try:
                text = self.getter("yoshinoya", f"{YOSHINOYA_BASE}/news/{y}/")
                self.st["detail_requests"] += 1
                self.years[y] = parsers.parse_yoshinoya_news(text)
                if not self.years[y]:
                    print(f"[warn] yoshinoya お知らせ {y}: 0件(構造変更の疑い)", file=sys.stderr)
            except BlockedError:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"[warn] yoshinoya お知らせ {y} 取得失敗: {e}", file=sys.stderr)

    def all(self) -> list[dict]:
        merged = [a for y in sorted(self.years) for a in self.years[y]]
        merged.sort(key=lambda a: a["date"], reverse=True)
        return merged


def yoshinoya_season_ref(product: dict) -> str:
    """その季の観測の起点日。再登場していれば再登場を確認した日、なければ初回に見つけた日。"""
    rel = product.get("relaunches") or []
    if rel and rel[-1].get("reappeared_at"):
        return rel[-1]["reappeared_at"]
    return (product.get("first_seen_at") or "")[:10]


def yoshinoya_apply_news(product: dict, news: YoshinoyaNews, today: str) -> bool:
    """発売日が未確定の商品はお知らせの見出しから探し直し、終了告知(早期販売終了など)があれば付記する。
    お知らせはすでに取得した分だけを使うので、追加のリクエストは年またぎ(1〜4月は前年分)のときだけ。変更があれば True。"""
    if product.get("is_regular"):
        return False  # 定番は発売日・終了告知の照合をしない
    ref = yoshinoya_season_ref(product)
    if not ref:
        return False
    changed = False
    name = product.get("name") or ""
    if product.get("launch_date") is None:
        c = datetime.date.fromisoformat(ref)
        lo = (c - datetime.timedelta(days=parsers.YOSHINOYA_NEWS_WINDOW_BEFORE_DAYS)).isoformat()
        hi = (c + datetime.timedelta(days=parsers.YOSHINOYA_NEWS_WINDOW_AFTER_DAYS)).isoformat()
        news.ensure(lo, hi, today)
        hit = parsers.match_yoshinoya_launch(name, news.all(), ref)
        if hit:
            product["launch_date"] = hit["launch_date"]
            product["launch_date_source"] = "news_headline"
            product["news_url"] = hit["news"]["url"]
            product["news_title"] = hit["news"]["title"]
            product["news_date"] = hit["news"]["date"]
            changed = True
    if not product.get("early_end_notice"):
        since = product.get("launch_date") or ref
        news.ensure(since, today, today)
        hit = parsers.match_yoshinoya_end_notice(name, news.all(), since, today)
        if hit:
            product["early_end_notice"] = {"date": hit["date"], "title": hit["title"], "url": hit["url"]}
            changed = True
    return changed


def yoshinoya_start_new_season(product: dict, old_ended: str | None, today: str) -> None:
    """終了後に同じIDが一覧へ戻ってきた(季節商品の再登場)。昨季の期間を seasons に残し、
    今季の発売日は未確定に戻す(お知らせの見出しから探し直す)。"""
    product.setdefault("seasons", []).append({
        "launch_date": product.get("launch_date"),
        "launch_date_source": product.get("launch_date_source"),
        "ended_observed_at": old_ended,
        "news_url": product.get("news_url"),
        "news_title": product.get("news_title"),
        "early_end_notice": product.get("early_end_notice"),
    })
    product["launch_date"] = None
    product["launch_date_source"] = "not_found"
    for k in ("news_url", "news_title", "news_date", "early_end_notice"):
        product[k] = None
    hist = product.setdefault("price_history", [])
    price = product.get("price")
    if price is not None and not (hist and hist[-1].get("date") == today):
        hist.append({"date": today, "price": price, "relaunch": True})


def build_yoshinoya_product(entry: dict, detail: dict, sources: list[str], flags: dict,
                            same_name: list[str], labels: dict, today: str, now: str) -> dict:
    variants = parsers.yoshinoya_price_variants(detail)
    primary = parsers.yoshinoya_pick_primary(detail)
    price = entry.get("price") if entry.get("price") is not None else primary["price"]
    limited = flags["limited"] or detail.get("is_limited")
    is_new = flags["new"] or detail.get("is_new")
    kind = "期間限定" if limited else ("新メニュー" if is_new else "新規掲載")

    note = "税込・店内の価格。"
    if len(detail["eatin"]) > 1 and primary["label"]:
        note += f"表示は「{primary['label']}」の価格。"
    if detail["takeout_prices"]:
        note += "テイクアウトは税率が違うため税込額が異なる場合があります（内訳は下の表）。"
    if detail["takeout"] is False:
        note += "テイクアウトはできません。"
    note += "店舗により価格・取り扱いが異なる場合があります。"

    cat_slug = entry["category"]
    category = next((labels.get(c) for c in sources if c != parsers.YOSHINOYA_LIMITED_CATEGORY and labels.get(c)), None)
    if category is None and cat_slug != parsers.YOSHINOYA_LIMITED_CATEGORY:
        category = labels.get(cat_slug)
    pdf = CONFIG.get("yoshinoya_pdf", {})
    product = {
        "chain": "yoshinoya",
        "product_id": entry["product_id"],
        "name": parsers.strip_yoshinoya_notice(entry["name"] or detail["name"]),
        "category": category,
        "price": price,
        "price_note": note,
        "price_variants": variants if len(variants) > 1 else None,
        "takeout": detail["takeout"],
        "launch_date": None,
        "launch_date_source": "not_found",
        "planned_end_text": None,
        "news_url": None,
        "news_title": None,
        "news_date": None,
        "description": None,
        "description_full_len": 0,
        "nutrition": None,
        "nutrition_variants": None,
        "nutrition_pdf_url": pdf.get("nutrition"),
        "nutrition_pdf_label": pdf.get("nutrition_label"),
        "origin_pdf_url": pdf.get("origin"),
        "allergens": None,
        "origin": None,
        "limited_kind": kind,
        "badges": [b for b, on in (("NEW", is_new), ("期間限定", limited)) if on],
        "in_limited_list": parsers.YOSHINOYA_LIMITED_CATEGORY in sources,
        "official_url": YOSHINOYA_BASE + entry["detail_path"],
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [{"date": today, "price": price}] if price is not None else [],
        "source_list": sorted(set(sources)),
        "image": None,
    }
    seed_variants(product, today)
    if same_name:
        product["same_name_as"] = same_name
    return product


def crawl_yoshinoya(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = "yoshinoya"
    limited_cat = parsers.YOSHINOYA_LIMITED_CATEGORY
    # 日付を指定した実行(テスト)では、初回観測の時刻もその日にそろえる(お知らせの照合の起点になるため)
    now = now_iso() if today is None else f"{today}T07:00:00+09:00"
    today = today or today_jst()
    pending.setdefault(chain, {})
    stats.setdefault(chain, new_stats())
    st = stats[chain]
    all_ok = True

    # robots.txt(1リクエスト)。404は「無い」ではなく本文付きの全許可なので、全許可として続行する
    robots_text = None
    try:
        robots_text = getter(chain, YOSHINOYA_BASE + "/robots.txt")
        st["list_requests"] += 1
    except NotFoundError:
        st["list_requests"] += 1
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] {chain} robots.txt: {e}", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {chain} robots.txt 取得失敗(全許可として続行): {e}", file=sys.stderr)
    rp = urllib.robotparser.RobotFileParser()
    rp.parse((robots_text or "User-agent: *\nAllow: /").splitlines())

    def allowed(path: str) -> bool:
        return rp.can_fetch(UA, YOSHINOYA_BASE + path)

    # カテゴリの入口。リンクをたどって特定する(推測でURLを作らない)。取れなければ設定の固定リストで代替
    index_cats: list[dict] = []
    try:
        idx = getter(chain, YOSHINOYA_BASE + parsers.YOSHINOYA_MENU_ROOT)
        st["list_requests"] += 1
        index_cats = parsers.parse_yoshinoya_index(idx)
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] {chain} menu index: {e}", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {chain} メニュー入口 取得失敗(設定の固定リストで代替): {e}", file=sys.stderr)
    cats = [c["slug"] for c in index_cats]
    if not cats:
        cats = [c["slug"] for c in CONFIG["yoshinoya_categories"]]
        all_ok = False
    cats = sorted(cats, key=lambda c: c != limited_cat)  # 期間限定を先頭に(安定ソート)
    labels = yoshinoya_category_labels(index_cats)

    entries: dict[str, dict] = {}
    sources: dict[str, list[str]] = {}
    flags: dict[str, dict] = {}
    for cat in cats:
        path = f"{parsers.YOSHINOYA_MENU_ROOT}{cat}/"
        if not allowed(path):
            print(f"[skip] robots.txtでDisallow: {path}", file=sys.stderr)
            all_ok = False
            continue
        try:
            html = getter(chain, YOSHINOYA_BASE + path)
            st["list_requests"] += 1
        except BlockedError as e:
            st["blocked"] = True
            print(f"[blocked] {chain} list {path}: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {chain} list {path} 取得失敗: {e}", file=sys.stderr)
            all_ok = False
            continue
        page_entries = parsers.parse_yoshinoya_list(html)
        if not page_entries:
            print(f"[warn] {chain} list {path}: 商品が0件(構造変更の疑い)", file=sys.stderr)
            all_ok = False
            continue
        for e in page_entries:
            if e["price"] is None:
                continue  # 「温め直し方」などの案内。料理ではないので商品として扱わない
            pid = e["product_id"]
            sources.setdefault(pid, [])
            if cat not in sources[pid]:
                sources[pid].append(cat)
            f = flags.setdefault(pid, {"new": False, "limited": False})
            f["new"] = f["new"] or e["is_new"]
            f["limited"] = f["limited"] or e["is_limited"]
            entries.setdefault(pid, e)
    st["list_ok"] = all_ok and bool(entries)
    st["limited_seen"] = sum(1 for pid in entries if limited_cat in sources[pid])

    baseline = baseline_for(chain)
    entries = burst_filter(chain, entries, baseline, pending, stats, today)
    news = YoshinoyaNews(getter, st)
    existing_all = None
    regular_budget = RegularBudget(st)

    def fetch_detail(pid: str, detail_path: str):
        """詳細を取得して解析する。失敗は pending に記録して None。ブロックは呼び出し元へ投げる。"""
        if not allowed(detail_path):
            print(f"[skip] robots.txtでDisallow: {detail_path}", file=sys.stderr)
            return None
        try:
            html = getter(chain, YOSHINOYA_BASE + detail_path)
            st["detail_requests"] += 1
        except NotFoundError:
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_path, "reason": "404", "checked_at": now}
            return None
        except BlockedError:
            raise
        except Exception as ex:  # noqa: BLE001
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_path, "reason": f"fetch_error: {ex}", "checked_at": now}
            return None
        detail = parsers.parse_yoshinoya_detail(html)
        if detail is None:
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_path, "reason": "empty_detail", "checked_at": now}
        return detail

    try:
        for pid, e in entries.items():
            existing = load_product(chain, pid)
            if existing is not None:
                was_ended = existing.get("status") == "ended"
                old_ended = existing.get("ended_observed_at")
                touch_seen(existing, list_price_for_history(st, e.get("price")), today, now, st)
                if was_ended:
                    yoshinoya_start_new_season(existing, old_ended, today)
                    st["relaunched"] = st.get("relaunched", 0) + 1
                if (existing.get("name") or "").lstrip().startswith("【予告】"):
                    existing["name"] = parsers.strip_yoshinoya_notice(existing["name"])  # 発売されたので【予告】を外す
                existing["source_list"] = sorted(set(existing.get("source_list") or []) | set(sources[pid]))
                if limited_cat in sources[pid]:
                    existing["in_limited_list"] = True
                    upgrade_regular(existing, "期間限定")
                if flags[pid]["limited"] and existing.get("limited_kind") not in ("期間限定", REGULAR_KIND):
                    existing["limited_kind"] = "期間限定"  # 定番は、期間限定の一覧(gentei)に載ったときだけ上で種別を上げる
                badges = set(existing.get("badges") or [])
                badges |= {b for b, on in (("NEW", flags[pid]["new"]), ("期間限定", flags[pid]["limited"])) if on}
                existing["badges"] = [b for b in ("NEW", "期間限定") if b in badges]
                save_product(chain, existing)
                pending[chain].pop(pid, None)
                continue
            in_limited = limited_cat in sources[pid]
            is_new_menu = baseline is not None and pid not in baseline
            is_regular = not (in_limited or is_new_menu)
            if is_regular and not regular_budget.take():
                continue  # 定番は詳細を取るので、1回の巡回で作る件数に上限がある。残りは翌日以降
            detail = fetch_detail(pid, e["detail_path"])
            if detail is None:
                continue
            if existing_all is None:
                existing_all = load_chain_products(chain)
            norm = parsers.normalize_for_match(parsers.strip_yoshinoya_notice(e["name"] or detail["name"]))
            same = [p["product_id"] for p in existing_all
                    if p["product_id"] != pid
                    and parsers.normalize_for_match(parsers.strip_yoshinoya_notice(p.get("name"))) == norm]
            product = build_yoshinoya_product(e, detail, sources[pid], flags[pid], same, labels, today, now)
            if is_regular:
                mark_regular(product)
            save_product(chain, product)
            existing_all.append(product)
            st["regular_created" if is_regular else "new_products"] += 1
            pending[chain].pop(pid, None)

        # サイズ別の価格(詳細ページにしか無い)を、古い順に少数だけ取り直す。基本の価格は一覧で毎日見ているので触らない
        if st["list_ok"]:
            for p in due_for_recheck(load_chain_products(chain), entries, True, today):
                detail = fetch_detail(p["product_id"], entries[p["product_id"]]["detail_path"])
                if detail is None:
                    continue
                record_variants(p, parsers.yoshinoya_price_variants(detail), today, st)
                p["price_checked_at"] = today
                save_product(chain, p)

        # 発売日の確定・終了告知の付記(お知らせ見出し)。一覧の更新と終了判定のあとに行う
        if st["list_ok"]:
            if baseline is None:
                save_baseline(chain, set(entries))
            apply_missing(chain, set(entries), today, st)
        cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=14)).isoformat()
        for p in load_chain_products(chain):
            if p.get("status") == "ended" and (p.get("ended_observed_at") or "") < cutoff:
                continue  # 終わって久しい商品はお知らせを探し直さない
            if yoshinoya_apply_news(p, news, today):
                save_product(chain, p)
        recorded = load_chain_products(chain)
        st["news_matched"] = sum(1 for p in recorded if p.get("launch_date"))
        st["news_unmatched"] = sum(1 for p in recorded if not p.get("launch_date"))
    except BlockedError as ex:
        st["blocked"] = True
        print(f"[blocked] {chain}: {ex}", file=sys.stderr)
        return


# ---------------------------------------------------------------------------
# すき家 (www.sukiya.jp)
# ---------------------------------------------------------------------------
# 調査・方針(2026-10-11):
#  ・robots.txt は200で、古い /news/2020/10/nabe.html など数ページの Disallow だけ。ボット対策なし。
#  ・サイト規約 /aboutsite.html は「文書による許諾なく使用および転載を禁止」と他社より強い。
#    → 記録するのは事実情報(価格・日付・販売状況)だけ。説明文・画像は保存も表示もしない。広告は入れない。
#    お知らせは見出し(出典表示)とPDFへのリンクだけ。
#  ・栄養成分・アレルゲンは別PDF(images.zensho.co.jp。robots.txtは403だがPDF本体は200)。巡回のあと
#    run_pdf_step() が pdf_update.py 経由で、更新があったときだけ取得して読み取る(ここの crawl_sukiya では
#    取得しない)。お知らせのPDFは取得しない(見出しの文言だけを読む)。
#  ・ページのリンクは、そのページ自身のホスト(www.sukiya.jp)で解決する。相対リンクを news.sukiya.jp と取り違えない。
#  ・限定だけの一覧HTMLは無い。カテゴリ一覧(13個)の各商品に公式の「New」「期間限定」アイコンが付くので、
#    記録するのは (a) 「期間限定」または「New」のアイコンが付いた商品、(b) 直近90日のプレスリリース
#    (/news/Press_sukiya_*.pdf)の見出しに商品名が出る商品、(c) baseline(初回巡回の全メニュー)に無く後から
#    現れた商品。定番の既存メニューは詳細を取らない。発売前の告知(まだメニューに載らない商品)は記録しない。
#  ・一覧に価格は無く詳細にある。詳細URLの数字は商品ごとに一意(別カテゴリに同じ数字は出ない)で、og:url は
#    全ページ共通の /menu/gyudon/index.html を指す(canonical は無い)ので、商品IDは「カテゴリ-数字」にする。
#  ・価格が変わったかは詳細を取り直さないと分からないので、記録済みの販売中商品(定番を含む)を price_checked_at の古い順に
#    config の variant_recheck_per_run 件ずつ取り直す(共通の仕組み。due_for_recheck / sukiya_recheck_price)。
#  ・終了日は公式が「販売終了時期は未定」とするため、一覧から消えたのを確認した日。

SUKIYA_BASE = parsers.SUKIYA_BASE


def sukiya_category_labels(index_cats: list[dict] | None = None) -> dict[str, str | None]:
    labels = {c["slug"]: c.get("label") for c in index_cats or [] if c.get("label")}
    labels.update({c["slug"]: c.get("label") for c in CONFIG.get("sukiya_categories", [])})
    return labels


def sukiya_price_note(variants: list[dict], primary: dict | None) -> str:
    note = "税込・店内の価格。"
    if len(variants) > 1:
        note += "サイズや組み合わせにより価格が異なります（内訳は下の表）。"
        if primary and primary.get("label"):
            note += f"表示は「{primary['label']}」の価格。"
    note += "店舗により価格・取り扱いが異なる場合があります。"
    return note


def sukiya_apply_news(product: dict, news: list[dict]) -> bool:
    """お知らせに載っていないか、発売日が未確定の商品を探し直す(取得済みの一覧を使うので追加のリクエストは無い)。
    見つかれば announced_at / news_* / 発売日(見出しに「○月○日」があるとき)を埋める。変更があれば True。"""
    if product.get("launch_date") is not None or product.get("is_regular"):
        return False  # 定番は発売日の照合をしない
    ref = (product.get("first_seen_at") or "")[:10]
    rel = product.get("relaunches") or []
    if rel and rel[-1].get("reappeared_at"):
        ref = rel[-1]["reappeared_at"]
    if not ref:
        return False
    hit = parsers.match_sukiya_launch(product.get("name") or "", news, ref)
    if not hit:
        return False
    a = hit["news"]
    changed = (product.get("news_url") != a["url"] or product.get("launch_date") != hit["launch_date"])
    product["announced_at"] = a["date"]
    product["news_url"] = a["url"]
    product["news_title"] = a["title"]
    product["news_date"] = a["date"]
    product["launch_date"] = hit["launch_date"]
    product["launch_date_source"] = "news_headline" if hit["launch_date"] else "news_without_date"
    if product.get("limited_kind") in (None, "新規掲載"):
        product["limited_kind"] = "新メニュー"  # 照合は発売・登場・復活を告げる見出しに限るので、常に「新メニュー」
    return changed


def build_sukiya_product(entry: dict, detail: dict, source: str, same_name: list[str],
                         labels: dict, today: str, now: str) -> dict:
    variants = parsers.sukiya_price_variants(detail)
    primary = parsers.sukiya_pick_primary(detail)
    price = primary["price"]
    badges = [b for b, on in (("NEW", entry["is_new"]), ("期間限定", entry["is_limited"]),
                              ("店舗限定", entry["is_store_limited"])) if on]
    if entry["is_limited"]:
        kind = "期間限定"
    elif entry["is_new"]:
        kind = "新メニュー"
    else:
        kind = "新規掲載"
    pdf = CONFIG.get("sukiya_pdf", {})
    product = {
        "chain": "sukiya",
        "product_id": entry["product_id"],
        "name": entry["name"] or detail["name"],
        "category": labels.get(source),
        "price": price,
        "price_note": sukiya_price_note(variants, primary),
        "price_variants": variants if len(variants) > 1 else None,
        "price_checked_at": today,
        "launch_date": None,
        "launch_date_source": "not_found",
        "announced_at": None,
        "planned_end_text": None,
        "news_url": None,
        "news_title": None,
        "news_date": None,
        "description": None,
        "description_full_len": 0,
        "nutrition": None,
        "nutrition_variants": None,
        "nutrition_pdf_url": pdf.get("nutrition"),
        "nutrition_pdf_label": pdf.get("nutrition_label"),
        "allergen_pdf_url": pdf.get("allergen"),
        "allergen_pdf_label": pdf.get("allergen_label"),
        "allergens": None,
        "origin": None,
        "limited_kind": kind,
        "badges": badges,
        "official_url": SUKIYA_BASE + entry["detail_path"],
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [{"date": today, "price": price}] if price is not None else [],
        "source_list": [source],
        "image": None,
    }
    seed_variants(product, today)
    if same_name:
        product["same_name_as"] = same_name
    return product


def sukiya_recheck_price(product: dict, detail: dict, today: str, st: dict | None = None) -> bool:
    """詳細を取り直した価格を反映する。代表の価格が変われば price_history に、サイズ別の内訳が変われば
    price_variants_history に追記する(取れなかった回は何も書かない)。確認した日は常に更新する。"""
    variants = parsers.sukiya_price_variants(detail)
    primary = parsers.sukiya_pick_primary(detail)
    product["price_checked_at"] = today
    price = primary["price"] if primary else None
    price_changed = record_price(product, price, today, st)
    # 内訳の変更は、代表の価格も変わった回は1件として数える(通知の「価格の変更 N件」が二重にならないように)
    before = (st or {}).get("price_changes", 0)
    variants_changed = record_variants(product, variants if len(variants) > 1 else None, today, st)
    if st is not None and price_changed and st.get("price_changes", 0) > before:
        st["price_changes"] = before
    if price_changed or variants_changed:
        product["price_note"] = sukiya_price_note(variants, primary)
        if len(variants) > 1:
            product["price_variants"] = variants
        elif price_changed:
            product["price_variants"] = None
    return True


def crawl_sukiya(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = "sukiya"
    # 日付を指定した実行(テスト)では、初回観測の時刻もその日にそろえる(お知らせの照合の起点になるため)
    now = now_iso() if today is None else f"{today}T07:00:00+09:00"
    today = today or today_jst()
    pending.setdefault(chain, {})
    stats.setdefault(chain, new_stats())
    st = stats[chain]
    all_ok = True

    robots_text = None
    try:
        robots_text = getter(chain, SUKIYA_BASE + "/robots.txt")
        st["list_requests"] += 1
    except NotFoundError:
        st["list_requests"] += 1
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] {chain} robots.txt: {e}", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {chain} robots.txt 取得失敗(全許可として続行): {e}", file=sys.stderr)
    rp = urllib.robotparser.RobotFileParser()
    rp.parse((robots_text or "User-agent: *\nAllow: /").splitlines())

    def allowed(path: str) -> bool:
        return rp.can_fetch(UA, SUKIYA_BASE + path)

    # カテゴリの入口。リンクをたどって特定する(推測でURLを作らない)。取れなければ設定の固定リストで代替
    index_cats: list[dict] = []
    try:
        idx = getter(chain, SUKIYA_BASE + parsers.SUKIYA_MENU_ROOT)
        st["list_requests"] += 1
        index_cats = parsers.parse_sukiya_index(idx)
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] {chain} menu index: {e}", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {chain} メニュー入口 取得失敗(設定の固定リストで代替): {e}", file=sys.stderr)
    cats = [c["slug"] for c in index_cats]
    if not cats:
        cats = [c["slug"] for c in CONFIG["sukiya_categories"]]
        all_ok = False
    labels = sukiya_category_labels(index_cats)

    entries: dict[str, dict] = {}
    source_of: dict[str, str] = {}
    for cat in cats:
        path = f"{parsers.SUKIYA_MENU_ROOT}in/{cat}/"
        if not allowed(path):
            print(f"[skip] robots.txtでDisallow: {path}", file=sys.stderr)
            all_ok = False
            continue
        try:
            html = getter(chain, SUKIYA_BASE + path)
            st["list_requests"] += 1
        except BlockedError as e:
            st["blocked"] = True
            print(f"[blocked] {chain} list {path}: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {chain} list {path} 取得失敗: {e}", file=sys.stderr)
            all_ok = False
            continue
        page_entries = parsers.parse_sukiya_list(html)
        if not page_entries:
            print(f"[warn] {chain} list {path}: 商品が0件(構造変更の疑い)", file=sys.stderr)
            all_ok = False
            continue
        for e in page_entries:
            pid = e["product_id"]
            if pid in entries:  # 商品IDは「カテゴリ-数字」なので、通常は重複しない
                for k in ("is_new", "is_limited", "is_store_limited", "is_recommend"):
                    entries[pid][k] = entries[pid][k] or e[k]
                continue
            entries[pid] = e
            source_of[pid] = cat
    st["list_ok"] = all_ok and bool(entries)
    st["limited_seen"] = sum(1 for e in entries.values() if e["is_limited"])

    # お知らせ一覧(1リクエスト)。見出しの文言だけを読み、リンク先のPDFは取得しない
    news: list[dict] = []
    try:
        news = parsers.parse_sukiya_news(getter(chain, SUKIYA_BASE + parsers.SUKIYA_NEWS_PATH))
        st["detail_requests"] += 1
        if not news:
            print(f"[warn] {chain} お知らせ: 0件(構造変更の疑い)", file=sys.stderr)
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] {chain} news: {e}", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {chain} お知らせ 取得失敗: {e}", file=sys.stderr)

    baseline = baseline_for(chain)
    entries = burst_filter(chain, entries, baseline, pending, stats, today)
    existing_all = None

    def fetch_detail(pid: str, detail_path: str):
        """詳細を取得して解析する。失敗は pending に記録して None。"""
        if not allowed(detail_path):
            print(f"[skip] robots.txtでDisallow: {detail_path}", file=sys.stderr)
            return None
        try:
            html = getter(chain, SUKIYA_BASE + detail_path)
            st["detail_requests"] += 1
        except NotFoundError:
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_path, "reason": "404", "checked_at": now}
            return None
        except BlockedError:
            raise
        except Exception as ex:  # noqa: BLE001
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_path, "reason": f"fetch_error: {ex}", "checked_at": now}
            return None
        detail = parsers.parse_sukiya_detail(html)
        if detail is None:
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_path, "reason": "empty_detail", "checked_at": now}
        return detail

    regular_budget = RegularBudget(st)
    try:
        for pid, e in entries.items():
            existing = load_product(chain, pid)
            if existing is not None:
                was_ended = existing.get("status") == "ended"
                old_ended = existing.get("ended_observed_at")
                touch_seen(existing, None, today, now, st)
                if was_ended:
                    yoshinoya_start_new_season(existing, old_ended, today)
                    st["relaunched"] = st.get("relaunched", 0) + 1
                if e["is_limited"] and existing.get("limited_kind") != "期間限定":
                    existing["limited_kind"] = "期間限定"
                elif e["is_new"]:
                    upgrade_regular(existing, "新メニュー")
                badges = set(existing.get("badges") or [])
                badges |= {b for b, on in (("NEW", e["is_new"]), ("期間限定", e["is_limited"]),
                                           ("店舗限定", e["is_store_limited"])) if on}
                existing["badges"] = [b for b in ("NEW", "期間限定", "店舗限定") if b in badges]
                save_product(chain, existing)
                pending[chain].pop(pid, None)
                continue
            in_news = bool(news) and parsers.match_sukiya_launch(e["name"], news, today) is not None
            is_new_menu = baseline is not None and pid not in baseline
            is_regular = not (e["is_limited"] or e["is_new"] or in_news or is_new_menu)
            if is_regular and not regular_budget.take():
                continue  # 定番は詳細を取るので、1回の巡回で作る件数に上限がある。残りは翌日以降
            detail = fetch_detail(pid, e["detail_path"])
            if detail is None:
                continue
            if existing_all is None:
                existing_all = load_chain_products(chain)
            norm = parsers.normalize_for_match(e["name"] or detail["name"])
            same = [p["product_id"] for p in existing_all
                    if p["product_id"] != pid and parsers.normalize_for_match(p.get("name")) == norm]
            product = build_sukiya_product(e, detail, source_of[pid], same, labels, today, now)
            if is_regular:
                mark_regular(product)
            else:
                sukiya_apply_news(product, news)
            save_product(chain, product)
            existing_all.append(product)
            st["regular_created" if is_regular else "new_products"] += 1
            pending[chain].pop(pid, None)

        # 記録済みの販売中商品の価格を、古い順に少数だけ取り直す(価格改定の追跡)
        if st["list_ok"]:
            if baseline is None:
                save_baseline(chain, set(entries))
            apply_missing(chain, set(entries), today, st)
            for p in due_for_recheck(load_chain_products(chain), entries, False, today):
                detail = fetch_detail(p["product_id"], entries[p["product_id"]]["detail_path"])
                if detail is not None:
                    sukiya_recheck_price(p, detail, today, st)
                    save_product(chain, p)

        # 発売日の確定(お知らせ見出し)。一覧の更新と終了判定のあとに行う。追加のリクエストは無い
        cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=14)).isoformat()
        for p in load_chain_products(chain):
            if p.get("status") == "ended" and (p.get("ended_observed_at") or "") < cutoff:
                continue  # 終わって久しい商品は探し直さない
            if news and sukiya_apply_news(p, news):
                save_product(chain, p)
        recorded = load_chain_products(chain)
        st["news_matched"] = sum(1 for p in recorded if p.get("news_url"))
        st["news_unmatched"] = sum(1 for p in recorded if not p.get("news_url"))
    except BlockedError as ex:
        st["blocked"] = True
        print(f"[blocked] {chain}: {ex}", file=sys.stderr)
        return


# ---------------------------------------------------------------------------
# かつや・からやま (www.arclandservice.co.jp。アークランドサービスホールディングスの共通テンプレート)
# ---------------------------------------------------------------------------
# 調査・方針(2026-10-11)。パーサ側(parsers.py の同名の節)にも経緯がある。
#  ・ホストは www.arclandservice.co.jp だけ。robots.txt は200で Disallow は /cms/wp-admin/ のみ。ボット対策なし。
#  ・メニュー一覧 /{brand}/menu/ は1ページに全商品(カテゴリ別ページは無い)。名前・税込価格・公式アイコン・URLが
#    一覧だけで揃うので、商品の詳細ページは取らない。アレルゲン一覧PDFのリンクも一覧ページにある(月ごとにURLが変わる
#    ため、販売中の商品には毎回最新のリンクを反映する。PDF自体は取得しない)。
#  ・お知らせ /{brand}/information/ も1ページ。見出しの文言・日付・リンク先URLだけを使い、リンク先(PR TIMES)は取得しない。
#  ・1回の巡回は、robots.txt + メニュー一覧 + お知らせ一覧の3リクエスト(1ブランドあたり)。
#  ・栄養成分は公式サイトのHTMLにもPDFにも載っていない。扱わない。
#  ・終了判定は1ページの一覧からの消失で行う。一覧が空・構造変更、または項目数が前回の半分未満(ページの欠落の疑い)の回は、
#    消えた商品を数えない(ARCLAND_MIN_RATIO)。その回の項目数は data/arcland_state.json に残す。
#  ・同名の商品が別の番号で並ぶ(店内用とテイクアウト用、期間限定と通常の再掲載)ので same_name_as を残す。
ARCLAND_BASE = parsers.ARCLAND_BASE
ARCLAND_MIN_RATIO = 0.5
ARCLAND_NAMES = {"katsuya": "かつや", "karayama": "からやま"}


def arcland_state_path() -> Path:
    return DATA / "arcland_state.json"


def arcland_price_note(entry: dict) -> str:
    note = "税込。公式サイトの一覧に表示された価格。"
    if entry.get("category") == "テイクアウト":
        note += "テイクアウト用の商品です。"
    note += "店舗により価格・取り扱いが異なる場合があります。"
    return note


def arcland_apply_news(product: dict, news: list[dict]) -> bool:
    """発売日が未確定の商品をお知らせの見出しから探し直す(取得済みの一覧を使うので追加のリクエストは無い)。
    見つかれば announced_at / news_* / 発売日(見出しに「○月○日」があるとき)を埋める。変更があれば True。"""
    if product.get("launch_date") is not None or product.get("is_regular"):
        return False  # 定番は発売日の照合をしない
    ref = (product.get("first_seen_at") or "")[:10]
    rel = product.get("relaunches") or []
    if rel and rel[-1].get("reappeared_at"):
        ref = rel[-1]["reappeared_at"]
    if not ref:
        return False
    hit = parsers.match_arcland_launch(product.get("name") or "", news, ref)
    if not hit:
        return False
    a = hit["news"]
    changed = (product.get("news_url") != a["url"] or product.get("launch_date") != hit["launch_date"])
    product["announced_at"] = a["date"]
    product["news_url"] = a["url"]
    product["news_title"] = a["title"]
    product["news_date"] = a["date"]
    product["launch_date"] = hit["launch_date"]
    product["launch_date_source"] = "news_headline" if hit["launch_date"] else "news_without_date"
    if product.get("limited_kind") in (None, "新規掲載"):
        product["limited_kind"] = "新メニュー"  # 照合は販売・発売を告げる見出しに限るので、公式の印が無ければ「新メニュー」
    return changed


def build_arcland_product(chain: str, entry: dict, allergen_pdf: str | None, same_name: list[str],
                          today: str, now: str) -> dict:
    kind = "期間限定" if entry["is_limited"] else ("新メニュー" if entry["is_new"] else "新規掲載")
    price = entry.get("price")
    product = {
        "chain": chain,
        "product_id": entry["product_id"],
        "name": entry["name"],
        "category": entry.get("category"),
        "price": price,
        "price_note": arcland_price_note(entry),
        "price_variants": None,
        "launch_date": None,
        "launch_date_source": "not_found",
        "announced_at": None,
        "planned_end_text": None,
        "news_url": None,
        "news_title": None,
        "news_date": None,
        "description": None,
        "description_full_len": 0,
        "nutrition": None,
        "nutrition_variants": None,
        "allergen_pdf_url": allergen_pdf,
        "allergen_pdf_label": "アレルゲン一覧",
        "allergens": None,
        "origin": None,
        "limited_kind": kind,
        "badges": [b for b in ("NEW", "期間限定") if b in entry["badges"]],
        "official_url": entry["official_url"],
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [{"date": today, "price": price}] if price is not None else [],
        "source_list": ["menu"],
        "image": None,
    }
    if same_name:
        product["same_name_as"] = same_name
    return product


def crawl_katsuya(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    crawl_arcland_family("katsuya", getter, pending, stats, today)


def crawl_karayama(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    crawl_arcland_family("karayama", getter, pending, stats, today)


def crawl_arcland_family(chain: str, getter, pending: dict, stats: dict, today: str | None = None) -> None:
    # 日付を指定した実行(テスト)では、初回観測の時刻もその日にそろえる(お知らせの照合の起点になるため)
    now = now_iso() if today is None else f"{today}T07:00:00+09:00"
    today = today or today_jst()
    pending.setdefault(chain, {})
    stats.setdefault(chain, new_stats())
    st = stats[chain]
    all_ok = True
    menu_path = parsers.arcland_menu_path(chain)
    news_path = parsers.arcland_news_path(chain)

    robots_text = None
    try:
        robots_text = getter(chain, ARCLAND_BASE + "/robots.txt")
        st["list_requests"] += 1
    except NotFoundError:
        st["list_requests"] += 1
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] {chain} robots.txt: {e}", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {chain} robots.txt 取得失敗(全許可として続行): {e}", file=sys.stderr)
    rp = urllib.robotparser.RobotFileParser()
    rp.parse((robots_text or "User-agent: *\nAllow: /").splitlines())

    def allowed(path: str) -> bool:
        return rp.can_fetch(UA, ARCLAND_BASE + path)

    # メニュー一覧(1リクエスト)。取れなければ何も更新しない
    entries: dict[str, dict] = {}
    allergen_pdf = None
    if not allowed(menu_path):
        print(f"[skip] robots.txtでDisallow: {menu_path}", file=sys.stderr)
        all_ok = False
    else:
        try:
            page = getter(chain, ARCLAND_BASE + menu_path)
            st["list_requests"] += 1
            parsed = parsers.parse_arcland_menu(page, chain)
            allergen_pdf = parsed["allergen_pdf_url"]
            entries = {e["product_id"]: e for e in parsed["items"]}
            if not entries:
                print(f"[warn] {chain} メニュー一覧: 商品が0件(構造変更の疑い)", file=sys.stderr)
                all_ok = False
        except BlockedError as e:
            st["blocked"] = True
            print(f"[blocked] {chain} menu: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {chain} メニュー一覧 取得失敗: {e}", file=sys.stderr)
            all_ok = False
    state = load_json(arcland_state_path(), {})
    prev_count = (state.get(chain) or {}).get("menu_count")
    if entries and prev_count and len(entries) < prev_count * ARCLAND_MIN_RATIO:
        print(f"[warn] {chain} メニュー一覧が極端に少ない({len(entries)}件。前回{prev_count}件)。"
              "消えた商品の判定は行わない", file=sys.stderr)
        all_ok = False
    st["list_ok"] = all_ok and bool(entries)
    st["limited_seen"] = sum(1 for e in entries.values() if e["is_limited"])

    # お知らせ一覧(1リクエスト)。見出しの文言・日付・リンク先URLだけを読み、リンク先は取得しない
    news: list[dict] = []
    if allowed(news_path):
        try:
            news = parsers.parse_arcland_news(getter(chain, ARCLAND_BASE + news_path), chain)
            st["detail_requests"] += 1
            if not news:
                print(f"[warn] {chain} お知らせ: 0件(構造変更の疑い)", file=sys.stderr)
        except BlockedError as e:
            st["blocked"] = True
            print(f"[blocked] {chain} news: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {chain} お知らせ 取得失敗: {e}", file=sys.stderr)
    else:
        print(f"[skip] robots.txtでDisallow: {news_path}", file=sys.stderr)

    baseline = baseline_for(chain)
    entries = burst_filter(chain, entries, baseline, pending, stats, today)
    existing_all = None
    regular_budget = RegularBudget(st, detail_needed=False)  # 一覧だけで作れるので件数の上限は無い
    for pid, e in entries.items():
        existing = load_product(chain, pid)
        if existing is not None:
            was_ended = existing.get("status") == "ended"
            old_ended = existing.get("ended_observed_at")
            touch_seen(existing, e.get("price"), today, now, st)
            if was_ended:
                yoshinoya_start_new_season(existing, old_ended, today)
                existing["announced_at"] = None
                st["relaunched"] = st.get("relaunched", 0) + 1
            if e["is_limited"] and existing.get("limited_kind") != "期間限定":
                existing["limited_kind"] = "期間限定"
            elif e["is_new"]:
                upgrade_regular(existing, "新メニュー")
            badges = set(existing.get("badges") or [])
            badges |= {b for b in ("NEW", "期間限定") if b in e["badges"]}
            existing["badges"] = [b for b in ("NEW", "期間限定") if b in badges]
            if allergen_pdf:
                existing["allergen_pdf_url"] = allergen_pdf  # 月ごとに変わる最新のリンク
            save_product(chain, existing)
            pending[chain].pop(pid, None)
            continue
        in_news = bool(news) and parsers.match_arcland_launch(
            e["name"], news, today, before=parsers.ARCLAND_NEWS_RECORD_DAYS) is not None
        is_new_menu = baseline is not None and pid not in baseline
        is_regular = not (e["is_limited"] or e["is_new"] or in_news or is_new_menu)
        if is_regular and not regular_budget.take():
            continue  # 定番の記録を止めているとき
        if existing_all is None:
            existing_all = load_chain_products(chain)
        norm = parsers.normalize_for_match(e["name"])
        same = [p["product_id"] for p in existing_all
                if p["product_id"] != pid and parsers.normalize_for_match(p.get("name")) == norm]
        product = build_arcland_product(chain, e, allergen_pdf, same, today, now)
        if is_regular:
            mark_regular(product)
        else:
            arcland_apply_news(product, news)
        save_product(chain, product)
        existing_all.append(product)
        st["regular_created" if is_regular else "new_products"] += 1
        pending[chain].pop(pid, None)

    if st["list_ok"]:
        if baseline is None:
            save_baseline(chain, set(entries))
        apply_missing(chain, set(entries), today, st)
        state[chain] = {"menu_count": len(entries), "checked_at": today}
        save_json(arcland_state_path(), state, sort_keys=True)

    # 発売日の確定(お知らせ見出し)。一覧の更新と終了判定のあとに行う。追加のリクエストは無い
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=14)).isoformat()
    for p in load_chain_products(chain):
        if p.get("status") == "ended" and (p.get("ended_observed_at") or "") < cutoff:
            continue  # 終わって久しい商品は探し直さない
        if news and arcland_apply_news(p, news):
            save_product(chain, p)
    recorded = load_chain_products(chain)
    st["news_matched"] = sum(1 for p in recorded if p.get("news_url"))
    st["news_unmatched"] = sum(1 for p in recorded if not p.get("news_url"))


# ---------------------------------------------------------------------------
# 幸楽苑・日高屋 (ラーメンチェーン。2026-11-18追加、11・12チェーン目。実物の構造は parsers.py の同名のコメントにまとめた)
# ---------------------------------------------------------------------------
# 方針:
#  ・巡回は robots.txt + メニュー一覧(幸楽苑1ページ・日高屋5ページ) + お知らせ(幸楽苑1ページ・日高屋のJSON1本) + 新しく記録する商品の詳細ページ。
#    詳細ページは栄養とアレルゲンのためだけに取る(一覧に価格・名前がある)。
#  ・公式に「期間限定」の印が無いので、記録するのは (a) お知らせの見出しに商品名があり発売・登場・期間限定を告げるもの(直近90日)、
#    (b) baseline(初回巡回の全メニュー)に無く後から現れた商品、(c) 日高屋の8桁の日付形式ID(限定品ごとの別ページ)、
#    (d) 定番(それ以外。詳細を取るので1回の巡回で regular_backfill_per_run 件まで)。
#  ・セット・トッピング・アルコールなどは記録しない(parsers の *_excluded)。説明文・画像は保存も表示もしない。
#  ・日高屋の日付形式のID(8桁、または 20261127-1 のように末尾に -数字・_数字 の付くもの)は、ID の日付がそのまま発売日
#    (実測: 20261009=2026-10-09、20261127=2026-11-27)。お知らせから発売日が取れなかった商品に限り、ID の日付を
#    launch_date_source="dated_id" として使う。ただし初めて見た日の前120日〜後30日に収まらない日付は使わない(取り違えの保険)。
#    手で入れた日付(manual)・お知らせの日付は上書きしない。お知らせの日付は発売日の代用にしない(オーナー決定 2026-11-28)。
#  ・日高屋は公式がIDを付け替える(2026-11-27: 20261127-1 → 20261127_1、20261127-2 → 20261127_2。単品→おつまみへのカテゴリ移動も同時)。
#    放っておくと新IDが新規として二重に記録され、旧IDが誤って「終了」になる。そこで一覧が全部読めた回だけ
#    hidakaya_resolve_reassigned_ids が、今日の一覧に新しく現れたID(未記録・baseline外)を、一覧から消えた記録済み商品
#    (販売中、または終了から14日以内)に、名前と価格が同じで1対1のときに限って付け替える(曖昧なら何もしない)。
#    記録のキー(product_id)は変えず、official_url・official_id・カテゴリを今日の一覧に合わせ、見たものとして扱う
#    (終了判定に数えない。終了から14日以内なら販売中に戻すだけで relaunches には積まない)。
#    付け替えた後の official_id は記録に残り、翌日以降は名前・価格が変わっても同じ対応で見つける。
#  ・一覧が空・取得失敗・前回の半分未満の回は、何も更新せず終了判定もしない。
RAMEN_CHAINS = ("kourakuen", "hidakaya")
RAMEN_MIN_RATIO = 0.5
RAMEN_NAMES = {"kourakuen": "幸楽苑", "hidakaya": "日高屋"}
KOURAKUEN_ALLERGEN_PDF = "https://www.kourakuen.co.jp/upload/pdf/allergy.pdf"
HIDAKAYA_ALLERGEN_PAGE = "https://hidakaya.hiday.co.jp/hits/menu/allergy.html"


STORE_LIMITED_NOTE = "公式の一覧で「店舗限定」の表示がある商品です(取り扱いのある店舗は限られます)。"


def ramen_state_path() -> Path:
    return DATA / "ramen_state.json"


def ramen_price_note(chain: str, entry: dict) -> str:
    note = "税込。公式サイトの一覧に表示された価格。"
    if chain == "kourakuen":
        note += "一部店舗では販売していません。"
        if entry.get("notes"):
            note += "".join(n if n.endswith("。") else n + "。" for n in entry["notes"])
    else:
        note += "店舗により価格・取り扱いが異なる場合があります。"
    return note


def ramen_apply_news(product: dict, news: list[dict]) -> bool:
    """発売日が未確定の限定・新メニューを、お知らせの見出しから探し直す(取得済みのお知らせを使うので追加のリクエストは無い)。
    見つかれば announced_at / news_* / 発売日を埋める。変更があれば True。定番は照合しない。"""
    if product.get("launch_date") is not None or product.get("is_regular"):
        return False
    ref = (product.get("first_seen_at") or "")[:10]
    rel = product.get("relaunches") or []
    if rel and rel[-1].get("reappeared_at"):
        ref = rel[-1]["reappeared_at"]
    if not ref:
        return False
    hit = parsers.match_ramen_launch(product.get("name") or "", news, ref)
    if not hit:
        return False
    a = hit["news"]
    changed = (product.get("news_url") != a["url"] or product.get("launch_date") != hit["launch_date"])
    product["announced_at"] = a["date"]
    product["news_url"] = a["url"]
    product["news_title"] = a["title"]
    product["news_date"] = a["date"]
    product["launch_date"] = hit["launch_date"]
    product["launch_date_source"] = "news_headline" if hit["launch_date"] else "news_without_date"
    if hit["limited"]:
        product["limited_kind"] = "期間限定"
    elif product.get("limited_kind") in (None, "新規掲載"):
        product["limited_kind"] = "新メニュー"
    return changed


HIDAKAYA_DATED_ID_BEFORE_DAYS = 120   # ID の日付が初めて見た日より何日前まで発売日として認めるか
HIDAKAYA_DATED_ID_AFTER_DAYS = 30     # 同じく何日後まで
HIDAKAYA_REASSIGN_RECENT_DAYS = 14    # 終了から何日以内の商品なら、付け替え先として販売中に戻すか


def hidakaya_apply_dated_id(product: dict) -> bool:
    """日付形式のIDの商品に dated_id の印を付け、お知らせから発売日が取れていなければ ID の日付を発売日にする。
    変更があれば True。定番・発売日が既にある商品(manual・お知らせ・取得済みの dated_id)の発売日は書き換えない。
    ID の日付が初めて見た日の前後の範囲(120日前〜30日後)に収まらなければ使わない。"""
    if product.get("chain") != "hidakaya":
        return False
    dated = next((x for x in (product.get("product_id"), product.get("official_id")) if parsers.hidakaya_is_dated_id(x)), None)
    if dated is None:
        return False
    changed = False
    if not product.get("dated_id"):
        product["dated_id"] = True
        changed = True
    if product.get("launch_date") is not None or product.get("is_regular"):
        return changed
    iso = parsers.hidakaya_dated_id_date(dated)
    first = (product.get("first_seen_at") or "")[:10]
    if not iso or not first:
        return changed
    d, f = datetime.date.fromisoformat(iso), datetime.date.fromisoformat(first)
    if not (f - datetime.timedelta(days=HIDAKAYA_DATED_ID_BEFORE_DAYS) <= d <= f + datetime.timedelta(days=HIDAKAYA_DATED_ID_AFTER_DAYS)):
        return changed
    product["launch_date"] = iso
    product["launch_date_source"] = "dated_id"
    return True


def hidakaya_resolve_reassigned_ids(entries: dict, recorded: list[dict], baseline: set[str] | None, today: str):
    """公式が商品IDを付け替えたとき(2026-11-27: 20261127-1 → 20261127_1)に、新規と終了の二重記録を防ぐ。純粋関数。
    一覧が全部読めた回にだけ呼ぶこと(読めないページに旧IDが隠れていると、別商品を付け替えと取り違えるため)。

    entries: {今日の一覧の商品ID: 項目}。recorded: 記録済みの商品。baseline: 基準線のID集合(None可。書き換えない)。
    ①記録済みの official_id に当たる今日のIDは、その商品の続きとする(前回までに付け替えた分。名前・価格が変わっても追える)。
    ②今日の一覧に新しく現れた(記録済みでも official_id でも基準線でもない)IDを、今日の一覧に無い記録済み商品
      (販売中、または終了から14日以内)に、名前と価格が同じで1対1のときだけ付け替える。複数に当たる・当たらないときは何もしない。
    戻り値: (キーを記録済みの商品IDに付け替え official_id に今日のIDを入れた entries, {今日のID: 記録済みID})。"""
    baseline = baseline or set()
    by_pid = {p["product_id"]: p for p in recorded}
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=HIDAKAYA_REASSIGN_RECENT_DAYS)).isoformat()
    renames: dict[str, str] = {}
    claimed: set[str] = set()
    for p in recorded:
        oid, pid = p.get("official_id"), p["product_id"]
        if oid and oid != pid and oid in entries and oid not in by_pid and pid not in entries and pid not in claimed:
            renames[oid] = pid
            claimed.add(pid)

    def key(name, price):
        return (name, price) if name and price is not None else None

    fresh: dict[tuple, list[str]] = {}
    for pid, e in entries.items():
        if pid in by_pid or pid in baseline or pid in renames:
            continue
        k = key(e.get("name"), e.get("price"))
        if k:
            fresh.setdefault(k, []).append(pid)
    gone: dict[tuple, list[str]] = {}
    for p in recorded:
        pid = p["product_id"]
        if pid in entries or pid in claimed:
            continue
        recent_end = p.get("status") == "ended" and (p.get("ended_observed_at") or "") >= cutoff
        if p.get("status") != "on_sale" and not recent_end:
            continue
        k = key(p.get("name"), p.get("price"))
        if k:
            gone.setdefault(k, []).append(pid)
    for k, news in fresh.items():
        if len(news) == 1 and len(gone.get(k, [])) == 1:
            renames[news[0]] = gone[k][0]
    out = {}
    for pid, e in entries.items():
        if pid in renames:
            out[renames[pid]] = dict(e, product_id=renames[pid], official_id=pid)
        else:
            out[pid] = e
    return out, renames


def hidakaya_apply_reassign(existing: dict, e: dict, today: str) -> bool:
    """付け替えとみなした記録済み商品に、今日の一覧の公式ID・URL・カテゴリを反映する(記録のキーは変えない)。
    終了から14日以内なら販売中に戻す(再登場ではなく付け替えなので relaunches には積まない)。変えたら True。"""
    new_id = e.get("official_id")
    if not new_id:
        return False
    if existing.get("status") == "ended":
        cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=HIDAKAYA_REASSIGN_RECENT_DAYS)).isoformat()
        if (existing.get("ended_observed_at") or "") >= cutoff:
            existing["status"] = "on_sale"
            existing["ended_observed_at"] = None
            existing["missed_runs"] = 0
            existing.pop("last_miss_date", None)
    existing["official_id"] = new_id
    existing["official_url"] = e["official_url"]
    if e.get("category"):
        existing["category"] = e["category"]
    return True


def build_ramen_product(chain: str, entry: dict, detail: dict, kind: str, today: str, now: str) -> dict:
    price = entry.get("price") if entry.get("price") is not None else detail.get("price")
    product = {
        "chain": chain,
        "product_id": entry["product_id"],
        "name": entry["name"],
        "category": entry.get("category"),
        "price": price,
        "price_note": ramen_price_note(chain, entry),
        "price_variants": None,
        "launch_date": None,
        "launch_date_source": "not_found",
        "announced_at": None,
        "planned_end_text": None,
        "news_url": None,
        "news_title": None,
        "news_date": None,
        "description": None,
        "description_full_len": 0,
        "nutrition": detail.get("nutrition"),
        "nutrition_variants": None,
        "nutrition_note": detail.get("nutrition_note"),
        "allergen_pdf_url": KOURAKUEN_ALLERGEN_PDF if chain == "kourakuen" else HIDAKAYA_ALLERGEN_PAGE,
        "allergen_pdf_label": "アレルギー情報（全商品）" if chain == "kourakuen" else "アレルギー情報（全商品の表）",
        "allergens": detail.get("allergens"),
        "origin": None,
        "limited_kind": kind,
        "badges": [],
        "official_url": entry["official_url"],
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [{"date": today, "price": price}] if price is not None else [],
        "source_list": ["menu"],
        "image": None,
    }
    if chain == "hidakaya" and parsers.hidakaya_is_dated_id(entry["product_id"]):
        product["dated_id"] = True
    if entry.get("notes"):
        product["sale_notes"] = entry["notes"]
    if entry.get("store_limited"):
        product["store_limited"] = True
        product["price_note"] += STORE_LIMITED_NOTE
    return product


def crawl_kourakuen(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    crawl_ramen_chain("kourakuen", getter, pending, stats, today)


def crawl_hidakaya(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    crawl_ramen_chain("hidakaya", getter, pending, stats, today)


def crawl_ramen_chain(chain: str, getter, pending: dict, stats: dict, today: str | None = None) -> None:
    now = now_iso() if today is None else f"{today}T07:00:00+09:00"
    today = today or today_jst()
    pending.setdefault(chain, {})
    stats.setdefault(chain, new_stats())
    st = stats[chain]
    kou = chain == "kourakuen"
    bases = [parsers.KOURAKUEN_STORES_BASE, parsers.KOURAKUEN_WWW_BASE] if kou else [parsers.HIDAKAYA_BASE]

    # robots.txt(ホストごとに1リクエスト)。取れない・404・HTMLが返る(幸楽苑のstoresは404ページへ飛ばす)ときは制限なしとして扱う
    robots: dict[str, urllib.robotparser.RobotFileParser] = {}
    for base in bases:
        text = None
        try:
            text = getter(chain, base + "/robots.txt")
        except NotFoundError:
            pass
        except BlockedError as e:
            st["list_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] {chain} robots.txt: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {chain} robots.txt 取得失敗(全許可として続行): {e}", file=sys.stderr)
        st["list_requests"] += 1
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(text.splitlines() if text and not parsers.looks_like_html(text) else ["User-agent: *", "Allow: /"])
        robots[base] = rp

    def allowed(url: str) -> bool:
        for base, rp in robots.items():
            if url.startswith(base):
                return rp.can_fetch(UA, url)
        return True

    # メニュー一覧。1ページでも取れない・0件の回は list_ok を立てない(何も更新せず終了判定もしない)
    entries: dict[str, dict] = {}
    all_ok = True
    pages = ([(parsers.KOURAKUEN_STORES_BASE + parsers.KOURAKUEN_MENU_PATH, None)] if kou
             else [(parsers.HIDAKAYA_BASE + p, c) for p, c in parsers.HIDAKAYA_LISTS])
    for url, cat in pages:
        if not allowed(url):
            print(f"[skip] robots.txtでDisallow: {url}", file=sys.stderr)
            all_ok = False
            continue
        try:
            page = getter(chain, url)
            st["list_requests"] += 1
        except BlockedError as e:
            st["list_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] {chain} menu {url}: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            st["list_requests"] += 1
            print(f"[warn] {chain} メニュー一覧 取得失敗 {url}: {e}", file=sys.stderr)
            all_ok = False
            continue
        got = parsers.parse_kourakuen_menu(page) if kou else parsers.parse_hidakaya_list(page, cat)
        if not got:
            print(f"[warn] {chain} メニュー一覧 {url}: 商品が0件(構造変更の疑い)", file=sys.stderr)
            all_ok = False
            continue
        for e in got:
            cur = entries.get(e["product_id"])
            if cur is None:
                entries[e["product_id"]] = e
            else:  # 「おすすめ」と各カテゴリに重複して出る。カテゴリは名前のあるほうを採る
                cur["category"] = cur.get("category") or e.get("category")
                for k in ("period_limited", "store_limited"):
                    cur[k] = bool(cur.get(k) or e.get(k))
                if cur.get("price") is None:
                    cur["price"] = e.get("price")
    # 記録しない商品(セット・トッピング・アルコールなど)を除く
    excluded = parsers.kourakuen_excluded if kou else parsers.hidakaya_excluded
    entries = {pid: e for pid, e in entries.items() if not excluded(e)}
    state = load_json(ramen_state_path(), {})
    prev_count = (state.get(chain) or {}).get("menu_count")
    if entries and prev_count and len(entries) < prev_count * RAMEN_MIN_RATIO:
        print(f"[warn] {chain} メニュー一覧が極端に少ない({len(entries)}件。前回{prev_count}件)。"
              "消えた商品の判定は行わない", file=sys.stderr)
        all_ok = False
    st["list_ok"] = all_ok and bool(entries)

    # お知らせ(1リクエスト)。見出し・日付・リンク先URLだけを読み、リンク先は取得しない。取れなくてもメニューの記録は続ける
    news: list[dict] = []
    news_url = (parsers.KOURAKUEN_WWW_BASE + parsers.KOURAKUEN_NEWS_PATH) if kou else (parsers.HIDAKAYA_BASE + parsers.HIDAKAYA_NEWS_PATH)
    if allowed(news_url):
        try:
            text = getter(chain, news_url)
            st["detail_requests"] += 1
            news = parsers.parse_kourakuen_news(text) if kou else parsers.parse_hidakaya_news(text)
            if not news:
                print(f"[warn] {chain} お知らせ: 0件(構造変更の疑い)", file=sys.stderr)
        except BlockedError as e:
            st["detail_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] {chain} news: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            st["detail_requests"] += 1
            print(f"[warn] {chain} お知らせ 取得失敗: {e}", file=sys.stderr)
    else:
        print(f"[skip] robots.txtでDisallow: {news_url}", file=sys.stderr)

    baseline = baseline_for(chain)
    # 日高屋: 公式がIDを付け替えた商品を、新規+終了の二重記録にしない。一覧が全部読めた回だけ(burst_filter の前に行い、
    # 付け替え分を新規候補に数えない)。基準線のファイルは書き換えない
    if not kou and st["list_ok"]:
        entries, moves = hidakaya_resolve_reassigned_ids(entries, load_chain_products(chain), baseline, today)
        for new_pid, old_pid in moves.items():
            print(f"[info] {chain} IDの付け替えとみなす: {old_pid} -> {new_pid}", file=sys.stderr)
    entries = burst_filter(chain, entries, baseline, pending, stats, today)
    regular_budget = RegularBudget(st)
    limited_seen = 0
    for pid, e in entries.items():
        existing = load_product(chain, pid)
        if existing is not None:
            if not kou:
                hidakaya_apply_reassign(existing, e, today)  # 付け替えの反映は touch_seen の前(終了→販売中に戻すため)
            touch_seen(existing, e.get("price") if kou else list_price_for_history(st, e.get("price")), today, now, st)
            if e.get("period_limited"):  # 後から【期間限定】が付いたら上げる。外れても定番には戻さない
                if existing.get("limited_kind") == REGULAR_KIND:
                    upgrade_regular(existing, "期間限定")
                else:
                    existing["limited_kind"] = "期間限定"
            if e.get("store_limited") and not existing.get("store_limited"):
                existing["store_limited"] = True
                existing["price_note"] = (existing.get("price_note") or "") + STORE_LIMITED_NOTE
            if e.get("name") and existing.get("name") != e["name"]:
                existing["name"] = e["name"]  # 名前の印(【…】)を外した形にそろえる。IDは詳細のファイル名なので変わらない
            save_product(chain, existing)
            pending[chain].pop(pid, None)
            continue
        hit = parsers.match_ramen_launch(e["name"], news, today, before=parsers.RAMEN_NEWS_RECORD_DAYS) if news else None
        limited_seen += 1 if hit else 0
        is_new_menu = baseline is not None and pid not in baseline
        dated = (not kou) and parsers.hidakaya_is_dated_id(pid)
        if e.get("period_limited"):
            kind = "期間限定"  # 一覧の名前の【期間限定】。お知らせに出ない限定品もあるので、お知らせの照合に頼らない
        elif hit:
            kind = "期間限定" if hit["limited"] else "新メニュー"
        elif is_new_menu or dated:
            kind = "新規掲載"
        else:
            kind = REGULAR_KIND
        is_regular = kind == REGULAR_KIND
        if is_regular and not regular_budget.take():
            continue  # 定番は詳細を取るので、1回の巡回で作る件数に上限がある。残りは翌日以降
        detail_url = e["official_url"]
        if not allowed(detail_url):
            print(f"[skip] robots.txtでDisallow: {detail_url}", file=sys.stderr)
            continue
        try:
            html = getter(chain, detail_url)
            st["detail_requests"] += 1
        except BlockedError as ex:
            st["detail_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] {chain} detail {detail_url}: {ex}", file=sys.stderr)
            return
        except NotFoundError:
            st["detail_requests"] += 1
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_url, "reason": "404", "checked_at": now}
            continue
        except Exception as ex:  # noqa: BLE001
            st["detail_requests"] += 1
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_url, "reason": f"fetch_error: {ex}", "checked_at": now}
            continue
        detail = parsers.parse_kourakuen_detail(html) if kou else parsers.parse_hidakaya_detail(html)
        if detail is None:
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_url, "reason": "empty_detail", "checked_at": now}
            continue
        product = build_ramen_product(chain, e, detail, kind, today, now)
        if is_regular:
            mark_regular(product)
        else:
            ramen_apply_news(product, news)
        if not kou:
            hidakaya_apply_dated_id(product)  # お知らせから発売日が取れなかった日付形式IDの商品は、ID の日付を使う
        save_product(chain, product)
        st["regular_created" if is_regular else "new_products"] += 1
        pending[chain].pop(pid, None)
    st["limited_seen"] = limited_seen

    if st["list_ok"]:
        if baseline is None:
            save_baseline(chain, set(entries))
        apply_missing(chain, set(entries), today, st)
        state[chain] = {"menu_count": len(entries), "checked_at": today}
        save_json(ramen_state_path(), state, sort_keys=True)

    # 発売日の確定(お知らせ見出し)。追加のリクエストは無い
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=14)).isoformat()
    for p in load_chain_products(chain):
        if p.get("status") == "ended" and (p.get("ended_observed_at") or "") < cutoff:
            continue  # 終わって久しい商品は探し直さない
        changed = bool(news and ramen_apply_news(p, news))
        if not kou and hidakaya_apply_dated_id(p):  # お知らせの照合のあと(お知らせの日付を優先)。既存の記録にも反映する
            changed = True
        if changed:
            save_product(chain, p)
    recorded = [p for p in load_chain_products(chain) if not p.get("is_regular")]
    st["news_matched"] = sum(1 for p in recorded if p.get("news_url"))
    st["news_unmatched"] = sum(1 for p in recorded if not p.get("news_url"))


# ---------------------------------------------------------------------------
# ゼッテリア (www.zetteria.jp。2026-11-25追加、13チェーン目。実物の構造は parsers.py の同名のコメントにまとめた)
# ---------------------------------------------------------------------------
# 方針:
#  ・巡回は robots.txt + メニュー一覧9ページ + お知らせ1ページ + 新しく記録する商品の詳細ページ +
#    限定品の発売日を取るプレスリリースPDF(1回の実行で最大 ZETTERIA_PRESS_MAX_FETCHES 件まで)。
#  ・期間限定の根拠は fair.html(フェアの一覧)に載っていること。ほかに、お知らせの見出しに商品名があり
#    発売・登場・期間限定を告げるものも拾う(ラーメン型と同じ照合)。カテゴリは fair 以外のページのものを優先する
#    (fair は横断の一覧。ただし sweets_drink の「フェア」の節よりは fair.html の見出しのほうが詳しいので除く)。
#  ・発売日はプレスリリースPDF本文から取る(お知らせの発表日とは約1週間ずれる)。PDFの【商品概要】と
#    商品名・価格で照合できれば launch_date_source="press_pdf"。見出しでしか照合できなければ、見出しに
#    日付があれば "news_headline"、無ければ発表日で代用して "news_date_fallback" とし、PDFの値と区別する。
#  ・セット商品も記録する(オーナー決定。モーニング・ランチ・ディナー・わくわくセットはセットが中心の
#    カテゴリで、外すと丸ごと空になる。ラーメン型の「セット除外」はゼッテリアには適用しない)。
#  ・説明文は詳細ページの冒頭の1文だけ保存する(他チェーンと同じ短縮)。アレルゲンは取り込まない。
#    栄養成分は公式PDF (images.zensho.co.jp の nutrition.pdf) から、巡回のあと run_pdf_step() が
#    pdf_update.py 経由で、更新があったときだけ取得して取り込む (2026-11-27追加。吉野家・すき家方式)。
#  ・一覧が空・取得失敗・前回の半分未満の回は、何も更新せず終了判定もしない。
ZETTERIA_MIN_RATIO = 0.5
ZETTERIA_PRESS_MAX_FETCHES = 5
ZETTERIA_PRESS_WINDOW_BEFORE_DAYS = 120
ZETTERIA_PRESS_WINDOW_AFTER_DAYS = 14
ZETTERIA_PRESS_RECHECK_DAYS = 14   # 発売日が発表日代用・未確定の商品を、PDFで探し直す期間(初めて見つけた日から)


def zetteria_state_path() -> Path:
    return DATA / "zetteria_state.json"


def zetteria_price_note(entry: dict) -> str:
    note = "税込。公式サイトの一覧に表示された価格。"
    if entry.get("price_tilde"):
        note += "一部店舗・デリバリーでは価格が異なります。"
    return note


def fetch_zetteria_press(getter, url: str) -> dict | None:
    """プレスリリースPDFを取得して解析する。pdfminer が入っていない環境では None
    (呼び出し側はお知らせの発表日で代用する)。取得の失敗は例外として呼び出し側へ返す。"""
    if not pdf_update.have_pdfminer():
        return None
    data = getter.pdf_get("zetteria", url)
    pages = pdf_update.pn.extract_geometry(data)
    return parsers.parse_zetteria_press(pages)


class ZetteriaPress:
    """プレスリリースPDFの取得を1回の実行ぶんだけ覚える(同じPDFは1回だけ取る)。上限を超えたら取らない。"""

    def __init__(self, getter, st: dict):
        self.getter = getter
        self.st = st
        self.cache: dict[str, dict | None] = {}
        self.fetches = 0

    def get(self, url: str) -> dict | None:
        if url in self.cache:
            return self.cache[url]
        info = None
        if self.fetches < ZETTERIA_PRESS_MAX_FETCHES:
            self.fetches += 1
            try:
                info = fetch_zetteria_press(self.getter, url)
            except BlockedError as e:
                self.st["blocked"] = True
                print(f"[blocked] zetteria press PDF {url}: {e}", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] zetteria プレスリリースPDF 取得・解析失敗 {url}: {e}", file=sys.stderr)
            self.st["detail_requests"] += 1
        self.cache[url] = info
        return info


def zetteria_apply_launch(product: dict, news: list[dict], press: ZetteriaPress, allow_press: bool = True) -> bool:
    """限定・新メニューの発売日を確定する。プレスリリースPDF(商品名・価格で照合)→お知らせ見出しの順に探す。
    変更があれば True。定番と、PDFで確定済みの商品は照合しない。"""
    if product.get("is_regular") or product.get("launch_date_source") == "press_pdf":
        return False
    ref = (product.get("first_seen_at") or "")[:10]
    rel = product.get("relaunches") or []
    if rel and rel[-1].get("reappeared_at"):
        ref = rel[-1]["reappeared_at"]
    if not ref:
        return False
    before = (product.get("news_url"), product.get("launch_date"), product.get("launch_date_source"),
              product.get("planned_end_text"), product.get("limited_kind"))
    if allow_press:
        try:
            c = datetime.date.fromisoformat(ref)
        except ValueError:
            c = None
        if c is not None:
            lo = (c - datetime.timedelta(days=ZETTERIA_PRESS_WINDOW_BEFORE_DAYS)).isoformat()
            hi = (c + datetime.timedelta(days=ZETTERIA_PRESS_WINDOW_AFTER_DAYS)).isoformat()
            for a in news:
                if a.get("category") != "プレスリリース" or not (a.get("url") or "").endswith(".pdf"):
                    continue
                if not (lo <= a["date"] <= hi):
                    continue
                info = press.get(a["url"])
                if not info or not parsers.match_zetteria_press(product.get("name") or "", product.get("price"), info):
                    continue
                product["announced_at"] = a["date"]
                product["news_url"] = a["url"]
                product["news_title"] = a["title"]
                product["news_date"] = a["date"]
                if info.get("launch_date"):
                    product["launch_date"] = info["launch_date"]
                    product["launch_date_source"] = "press_pdf"
                else:
                    product["launch_date"] = a["date"]   # PDFに発売日が読み取れなかったので発表日で代用する
                    product["launch_date_source"] = "news_date_fallback"
                if info.get("planned_end_text"):
                    product["planned_end_text"] = info["planned_end_text"]
                if "期間限定" in a["title"]:
                    product["limited_kind"] = "期間限定"
                elif product.get("limited_kind") in (None, "新規掲載"):
                    product["limited_kind"] = "新メニュー"
                break
    if product.get("launch_date_source") != "press_pdf":
        hit = parsers.match_ramen_launch(product.get("name") or "", news, ref)
        if hit:
            a = hit["news"]
            product["announced_at"] = a["date"]
            product["news_url"] = a["url"]
            product["news_title"] = a["title"]
            product["news_date"] = a["date"]
            if hit["launch_date"]:
                product["launch_date"] = hit["launch_date"]
                product["launch_date_source"] = "news_headline"
            elif product.get("launch_date_source") != "news_date_fallback":
                product["launch_date"] = a["date"]   # 見出しに発売日が無いので発表日で代用する
                product["launch_date_source"] = "news_date_fallback"
            if hit["limited"]:
                product["limited_kind"] = "期間限定"
            elif product.get("limited_kind") in (None, "新規掲載"):
                product["limited_kind"] = "新メニュー"
    after = (product.get("news_url"), product.get("launch_date"), product.get("launch_date_source"),
             product.get("planned_end_text"), product.get("limited_kind"))
    return before != after


def build_zetteria_product(entry: dict, detail: dict, kind: str, today: str, now: str) -> dict:
    price = entry.get("price") if entry.get("price") is not None else detail.get("price")
    variants = entry.get("price_variants") or detail.get("price_variants")
    short, full_len = parsers.shorten_description(detail.get("description"))
    product = {
        "chain": "zetteria",
        "product_id": entry["product_id"],
        "name": entry["name"],
        "category": entry.get("category"),
        "price": price,
        "price_note": zetteria_price_note(entry),
        "price_variants": variants if variants and len(variants) >= 2 else None,
        "launch_date": None,
        "launch_date_source": "not_found",
        "announced_at": None,
        "planned_end_text": None,
        "news_url": None,
        "news_title": None,
        "news_date": None,
        "description": short,
        "description_full_len": full_len,
        "nutrition": None,
        "nutrition_variants": None,
        "nutrition_note": None,
        "nutrition_pdf_url": parsers.ZETTERIA_NUTRITION_PDF_URL,
        "nutrition_pdf_label": "栄養成分一覧表",
        "allergen_pdf_url": None,
        "allergen_pdf_label": None,
        "allergen_page_url": parsers.ZETTERIA_ALLERGEN_URL,
        "allergens": None,
        "origin": None,
        "limited_kind": kind,
        "badges": [],
        "official_url": entry["official_url"],
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [{"date": today, "price": price}] if price is not None else [],
        "source_list": ["menu"],
        "image": None,
    }
    if entry.get("in_fair"):
        product["in_fair"] = True
    seed_variants(product, today)
    return product


def crawl_zetteria(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = "zetteria"
    now = now_iso() if today is None else f"{today}T07:00:00+09:00"
    today = today or today_jst()
    pending.setdefault(chain, {})
    stats.setdefault(chain, new_stats())
    st = stats[chain]
    base = parsers.ZETTERIA_BASE

    # robots.txt(1リクエスト)。404(ファイル自体が無い)ときは制限なしとして扱う
    robots_text = None
    try:
        robots_text = getter(chain, base + "/robots.txt")
    except NotFoundError:
        pass
    except BlockedError as e:
        st["list_requests"] += 1
        st["blocked"] = True
        print(f"[blocked] zetteria robots.txt: {e}", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] zetteria robots.txt 取得失敗(全許可として続行): {e}", file=sys.stderr)
    st["list_requests"] += 1
    rp = urllib.robotparser.RobotFileParser()
    rp.parse((robots_text or "User-agent: *\nAllow: /").splitlines())

    def allowed(url: str) -> bool:
        return rp.can_fetch(UA, url)

    # メニュー一覧(9ページ)。1ページでも取れない・0件の回は list_ok を立てない(何も更新せず終了判定もしない)
    entries: dict[str, dict] = {}
    all_ok = True
    for path, label in parsers.ZETTERIA_MENU_PAGES:
        url = base + path
        if not allowed(url):
            print(f"[skip] robots.txtでDisallow: {url}", file=sys.stderr)
            all_ok = False
            continue
        try:
            page = getter(chain, url)
            st["list_requests"] += 1
        except BlockedError as e:
            st["list_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] zetteria menu {url}: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            st["list_requests"] += 1
            print(f"[warn] zetteria メニュー一覧 取得失敗 {url}: {e}", file=sys.stderr)
            all_ok = False
            continue
        got = parsers.parse_zetteria_menu(page, label)
        if not got:
            print(f"[warn] zetteria メニュー一覧 {url}: 商品が0件(構造変更の疑い)", file=sys.stderr)
            all_ok = False
            continue
        for e in got:
            pid = e["product_id"]
            cur = entries.get(pid)
            if cur is None:
                e["in_fair"] = path == "/menu/fair.html"
                e["_from_fair"] = path == "/menu/fair.html"
                entries[pid] = e
                continue
            cur["in_fair"] = bool(cur.get("in_fair") or path == "/menu/fair.html")
            if cur.get("_from_fair") and path != "/menu/fair.html" and e.get("category") != "フェア":
                cur["category"] = e.get("category")   # カテゴリは fair 以外のページのものを優先する
                cur["_from_fair"] = False
            if cur.get("price") is None:
                cur["price"] = e.get("price")
            if not cur.get("price_variants") and e.get("price_variants"):
                cur["price_variants"] = e["price_variants"]
            cur["price_tilde"] = bool(cur.get("price_tilde") or e.get("price_tilde"))
    for e in entries.values():
        e.pop("_from_fair", None)
    # 記録しない商品(価格が無いもの)を除く。セット商品は記録する
    entries = {pid: e for pid, e in entries.items() if not parsers.zetteria_excluded(e)}
    state = load_json(zetteria_state_path(), {})
    prev_count = state.get("menu_count")
    if entries and prev_count and len(entries) < prev_count * ZETTERIA_MIN_RATIO:
        print(f"[warn] zetteria メニュー一覧が極端に少ない({len(entries)}件。前回{prev_count}件)。"
              "消えた商品の判定は行わない", file=sys.stderr)
        all_ok = False
    st["list_ok"] = all_ok and bool(entries)
    st["limited_seen"] = sum(1 for e in entries.values() if e.get("in_fair"))

    # お知らせ(1リクエスト)。見出し・日付・リンク先URLだけを読み、リンク先は取得しない。取れなくてもメニューの記録は続ける
    news: list[dict] = []
    news_url = base + parsers.ZETTERIA_NEWS_PATH
    if allowed(news_url):
        try:
            text = getter(chain, news_url)
            st["detail_requests"] += 1
            news = parsers.parse_zetteria_news(text)
            if not news:
                print("[warn] zetteria お知らせ: 0件(構造変更の疑い)", file=sys.stderr)
        except BlockedError as e:
            st["detail_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] zetteria news: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            st["detail_requests"] += 1
            print(f"[warn] zetteria お知らせ 取得失敗: {e}", file=sys.stderr)
    else:
        print(f"[skip] robots.txtでDisallow: {news_url}", file=sys.stderr)

    baseline = baseline_for(chain)
    entries = burst_filter(chain, entries, baseline, pending, stats, today)
    press = ZetteriaPress(getter, st)
    regular_budget = RegularBudget(st)
    for pid, e in entries.items():
        existing = load_product(chain, pid)
        if existing is not None:
            touch_seen(existing, list_price_for_history(st, e.get("price")), today, now, st)
            if e.get("in_fair"):
                if existing.get("limited_kind") == REGULAR_KIND:
                    upgrade_regular(existing, "期間限定")
                existing["in_fair"] = True
            if e.get("name") and existing.get("name") != e["name"]:
                existing["name"] = e["name"]
            save_product(chain, existing)
            pending[chain].pop(pid, None)
            continue
        hit = parsers.match_ramen_launch(e["name"], news, today, before=parsers.RAMEN_NEWS_RECORD_DAYS) if news else None
        is_new_menu = baseline is not None and pid not in baseline
        if e.get("in_fair"):
            kind = "期間限定"   # フェアの一覧に載っていること自体が限定の根拠
        elif hit:
            kind = "期間限定" if hit["limited"] else "新メニュー"
        elif is_new_menu:
            kind = "新規掲載"
        else:
            kind = REGULAR_KIND
        is_regular = kind == REGULAR_KIND
        if is_regular and not regular_budget.take():
            continue   # 定番は詳細を取るので、1回の巡回で作る件数に上限がある。残りは翌日以降
        detail_url = e["official_url"]
        if not allowed(detail_url):
            print(f"[skip] robots.txtでDisallow: {detail_url}", file=sys.stderr)
            continue
        try:
            html = getter(chain, detail_url)
            st["detail_requests"] += 1
        except BlockedError as ex:
            st["detail_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] zetteria detail {detail_url}: {ex}", file=sys.stderr)
            return
        except NotFoundError:
            st["detail_requests"] += 1
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_url, "reason": "404", "checked_at": now}
            continue
        except Exception as ex:  # noqa: BLE001
            st["detail_requests"] += 1
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_url, "reason": f"fetch_error: {ex}", "checked_at": now}
            continue
        detail = parsers.parse_zetteria_detail(html)
        if detail is None:
            st["failed"] += 1
            pending[chain][pid] = {"url": detail_url, "reason": "empty_detail", "checked_at": now}
            continue
        product = build_zetteria_product(e, detail, kind, today, now)
        if is_regular:
            mark_regular(product)
        else:
            zetteria_apply_launch(product, news, press)
        save_product(chain, product)
        st["regular_created" if is_regular else "new_products"] += 1
        pending[chain].pop(pid, None)
        if st["blocked"]:
            return

    if st["list_ok"]:
        if baseline is None:
            save_baseline(chain, set(entries))
        apply_missing(chain, set(entries), today, st)
        save_json(zetteria_state_path(), {"menu_count": len(entries), "checked_at": today}, sort_keys=True)

    # 発売日の確定(プレスリリースPDF・お知らせ見出し)。PDFの取得は上限があり、探し直しは初めて見つけてから
    # ZETTERIA_PRESS_RECHECK_DAYS 日までの商品だけにする
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=14)).isoformat()
    press_from = (datetime.date.fromisoformat(today) - datetime.timedelta(days=ZETTERIA_PRESS_RECHECK_DAYS)).isoformat()
    for p in load_chain_products(chain):
        if p.get("status") == "ended" and (p.get("ended_observed_at") or "") < cutoff:
            continue   # 終わって久しい商品は探し直さない
        if zetteria_apply_launch(p, news, press, allow_press=(p.get("first_seen_at") or "")[:10] >= press_from):
            save_product(chain, p)
        if st["blocked"]:
            break
    recorded = [p for p in load_chain_products(chain) if not p.get("is_regular")]
    st["news_matched"] = sum(1 for p in recorded if p.get("news_url"))
    st["news_unmatched"] = sum(1 for p in recorded if not p.get("news_url"))


# ---------------------------------------------------------------------------
# ケンタッキー (www.kfc.co.jp / japan.kfc.co.jp。2026-11-25追加、14チェーン目。実物の構造は parsers.py の同名のコメントにまとめた)
# ---------------------------------------------------------------------------
# 方針:
#  ・巡回は robots.txt(消費者向け・法人の2ドメイン) + カテゴリ別メニュー6ページ + リリース一覧1ページ +
#    未照合の商品リリースの詳細(1回の実行で最大 KFC_RELEASE_MAX_FETCHES 件まで)。商品詳細ページは無いので、
#    商品レコードは一覧の情報だけで作る(定番の作成にも詳細取得は要らない)。
#    期間限定の商品はカテゴリのセクションに載らず「おすすめ」のセクションにだけ載るため、そちらも取り込む
#    (parsers.py の同名のコメントに実測の内訳)。
#  ・商品の正本は消費者向けメニュー一覧。発売日は法人リリース(japan.kfc.co.jp)の【商品概要】が正本で、
#    照合できたものは launch_date_source="press_release"(新設。ゼッテリアの press_pdf はPDF専用の含意のため
#    流用しない)。リリースの解析結果は data/kfc_state.json に残し、後から現れた商品とも照合できるようにする。
#    【商品概要】の無いリリース(CM・新店など)は照合済みとして記録し、二度と取得しない。
#  ・リリースに「数量限定」「期間限定」の記載があり【商品概要】に名前がある商品は 期間限定(badges にも付ける)。
#    リリースに載ったが限定の記載が無い商品は 新メニュー。baseline に無い新規は 新規掲載、それ以外は定番。
#  ・セット商品も記録する(セットが独立したカテゴリで、セット自体が別商品としてメニューに載るため)。
#    リリースのセット内訳〔…〕は保存しない。説明文・栄養・アレルゲンは第1段階では対象外(すべて null)。
#  ・一覧が空・取得失敗・前回の半分未満の回は、何も更新せず終了判定もしない。
KFC_MIN_RATIO = 0.5
KFC_RELEASE_MAX_FETCHES = 8
KFC_RELEASE_WINDOW_BEFORE_DAYS = 120
KFC_RELEASE_WINDOW_AFTER_DAYS = 14


def kfc_state_path() -> Path:
    return DATA / "kfc_state.json"


def kfc_price_note(entry: dict) -> str:
    return "税込。公式サイトの一覧に表示された価格。"


def _kfc_robots(getter, base: str, st: dict):
    """robots.txt を読んで RobotFileParser を返す。ブロックされたら None(呼び出し側がその回を止める)。
    404・その他の失敗は全許可として扱う(既存チェーンと同じ作法)。"""
    st["list_requests"] += 1
    try:
        text = getter("kfc", base + "/robots.txt")
    except NotFoundError:
        text = None
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] kfc robots.txt {base}: {e}", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[warn] kfc robots.txt 取得失敗(全許可として続行) {base}: {e}", file=sys.stderr)
        text = None
    rp = urllib.robotparser.RobotFileParser()
    rp.parse((text or "User-agent: *\nAllow: /").splitlines())
    return rp


def kfc_release_infos(state: dict) -> list[dict]:
    """state に残した商品リリースの解析結果を、新しい順の [{url, date, title, launch_date,
    planned_end_text, limited, items}] で返す。"""
    infos = [{"url": url, **info} for url, info in (state.get("releases") or {}).items()]
    infos.sort(key=lambda x: x.get("date") or "", reverse=True)
    return infos


def kfc_find_release(name: str, price: int | None, infos: list[dict], ref: str) -> dict | None:
    """商品名・価格で照合できる商品リリースを返す(ref の前120日〜後14日に発表されたもの)。"""
    try:
        c = datetime.date.fromisoformat(ref)
    except (ValueError, TypeError):
        return None
    lo = (c - datetime.timedelta(days=KFC_RELEASE_WINDOW_BEFORE_DAYS)).isoformat()
    hi = (c + datetime.timedelta(days=KFC_RELEASE_WINDOW_AFTER_DAYS)).isoformat()
    for info in infos:
        if not (lo <= (info.get("date") or "") <= hi):
            continue
        if parsers.match_kfc_release(name, price, info):
            return info
    return None


def kfc_apply_release(product: dict, infos: list[dict]) -> bool:
    """商品リリースの発売日・終了予定・限定種別を反映する。変更があれば True。
    定番と、リリースで確定済み(launch_date_source="press_release")の商品は照合しない。"""
    if product.get("is_regular") or product.get("launch_date_source") == "press_release":
        return False
    ref = (product.get("first_seen_at") or "")[:10]
    rel = product.get("relaunches") or []
    if rel and rel[-1].get("reappeared_at"):
        ref = rel[-1]["reappeared_at"]
    if not ref:
        return False
    info = kfc_find_release(product.get("name") or "", product.get("price"), infos, ref)
    if not info:
        return False
    before = (product.get("news_url"), product.get("launch_date"), product.get("launch_date_source"),
              product.get("planned_end_text"), product.get("limited_kind"), tuple(product.get("badges") or []))
    product["announced_at"] = info.get("date")
    product["news_url"] = info["url"]
    product["news_title"] = info.get("title")
    product["news_date"] = info.get("date")
    if info.get("launch_date"):
        product["launch_date"] = info["launch_date"]
        product["launch_date_source"] = "press_release"
    if info.get("planned_end_text"):
        product["planned_end_text"] = info["planned_end_text"]
    if info.get("limited"):
        product["limited_kind"] = "期間限定"
        badges = list(product.get("badges") or [])
        if "期間限定" not in badges:
            badges.append("期間限定")
        product["badges"] = badges
    elif product.get("limited_kind") in (None, "新規掲載"):
        product["limited_kind"] = "新メニュー"
    after = (product.get("news_url"), product.get("launch_date"), product.get("launch_date_source"),
             product.get("planned_end_text"), product.get("limited_kind"), tuple(product.get("badges") or []))
    return before != after


def build_kfc_product(entry: dict, kind: str, today: str, now: str) -> dict:
    price = entry.get("price")
    return {
        "chain": "kfc",
        "product_id": entry["product_id"],
        "name": entry["name"],
        "category": entry.get("category"),
        "price": price,
        "price_note": kfc_price_note(entry),
        "price_variants": None,
        "launch_date": None,
        "launch_date_source": "not_found",
        "announced_at": None,
        "planned_end_text": None,
        "news_url": None,
        "news_title": None,
        "news_date": None,
        "description": None,
        "description_full_len": 0,
        "nutrition": None,
        "nutrition_variants": None,
        "nutrition_note": None,
        "nutrition_pdf_url": None,
        "nutrition_pdf_label": None,
        "allergen_pdf_url": None,
        "allergen_pdf_label": None,
        "allergens": None,
        "origin": None,
        "limited_kind": kind,
        "badges": [],
        "official_url": entry["official_url"],
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [{"date": today, "price": price}] if price is not None else [],
        "source_list": ["menu"],
        "image": None,
    }


def crawl_kfc(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = "kfc"
    now = now_iso() if today is None else f"{today}T07:00:00+09:00"
    today = today or today_jst()
    pending.setdefault(chain, {})
    stats.setdefault(chain, new_stats())
    st = stats[chain]
    base = parsers.KFC_BASE
    corp = parsers.KFC_CORP_BASE

    # robots.txt(2ドメイン)。www は /api/ 等が Disallow(使うのは /menu だけ)。法人は全許可
    rp = _kfc_robots(getter, base, st)
    if rp is None:
        return
    rp_corp = _kfc_robots(getter, corp, st)
    if rp_corp is None:
        return

    def allowed(url: str) -> bool:
        return rp.can_fetch(UA, url)

    def corp_allowed(url: str) -> bool:
        return rp_corp.can_fetch(UA, url)

    # メニュー一覧(カテゴリ別6ページ)。1ページでも取れない・0件の回は list_ok を立てない(何も更新せず終了判定もしない)。
    # 期間限定の商品はカテゴリのセクションに載らず「おすすめ」のセクションにだけ載るので、そちらも取り込む
    # (カテゴリのセクションにある商品はそちらのカテゴリが正で、「おすすめ」側では上書きしない)
    entries: dict[str, dict] = {}
    all_ok = True
    first_page: str | None = None
    for category in parsers.KFC_CATEGORIES:
        url = parsers.kfc_menu_url(category)
        if not allowed(url):
            print(f"[skip] robots.txtでDisallow: {url}", file=sys.stderr)
            all_ok = False
            continue
        try:
            page = getter(chain, url)
            st["list_requests"] += 1
        except BlockedError as e:
            st["list_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] kfc menu {url}: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            st["list_requests"] += 1
            print(f"[warn] kfc メニュー一覧 取得失敗 {url}: {e}", file=sys.stderr)
            all_ok = False
            continue
        if first_page is None:
            first_page = page
        got = parsers.parse_kfc_menu(page, category)
        if not got:
            print(f"[warn] kfc メニュー一覧 {url}: 商品が0件(構造変更の疑い)", file=sys.stderr)
            all_ok = False
            continue
        for e in got:
            entries.setdefault(e["product_id"], e)
    if first_page is not None:
        for e in parsers.parse_kfc_menu(first_page, parsers.KFC_RECOMMENDED):
            entries.setdefault(e["product_id"], e)
    # 記録しない商品(名前が無い・価格が無いもの)を除く。セット商品は記録する
    entries = {pid: e for pid, e in entries.items() if not parsers.kfc_excluded(e)}
    state = load_json(kfc_state_path(), {})
    prev_count = state.get("menu_count")
    if entries and prev_count and len(entries) < prev_count * KFC_MIN_RATIO:
        print(f"[warn] kfc メニュー一覧が極端に少ない({len(entries)}件。前回{prev_count}件)。"
              "消えた商品の判定は行わない", file=sys.stderr)
        all_ok = False
    st["list_ok"] = all_ok and bool(entries)
    if not st["list_ok"]:
        return   # 一覧が揃わない回は何も更新しない(終了判定もしない)

    # リリース一覧(1リクエスト)。取れなくてもメニューの記録は続ける
    releases_list: list[dict] = []
    list_url = corp + parsers.KFC_RELEASE_LIST_PATH
    if corp_allowed(list_url):
        try:
            text = getter(chain, list_url)
            st["detail_requests"] += 1
            releases_list = parsers.parse_kfc_release_list(text)
            if not releases_list:
                print("[warn] kfc リリース一覧: 0件(構造変更の疑い)", file=sys.stderr)
        except BlockedError as e:
            st["detail_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] kfc release list: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            st["detail_requests"] += 1
            print(f"[warn] kfc リリース一覧 取得失敗: {e}", file=sys.stderr)
    else:
        print(f"[skip] robots.txtでDisallow: {list_url}", file=sys.stderr)

    # 未照合のリリースだけ詳細を取る(上限つき)。【商品概要】のあるものは解析結果を state に残し、
    # 無いものは照合済みの印だけ残す。取得に失敗したものは照合済みにしない(次回取り直す)
    stored = dict(state.get("releases") or {})
    checked = set(state.get("release_checked") or [])
    fetches = 0
    for a in releases_list:
        url = a["url"]
        if url in stored or url in checked:
            continue
        if fetches >= KFC_RELEASE_MAX_FETCHES:
            break
        if not corp_allowed(url):
            checked.add(url)
            continue
        fetches += 1
        try:
            html = getter(chain, url)
            st["detail_requests"] += 1
        except BlockedError as e:
            st["detail_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] kfc release detail {url}: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            st["detail_requests"] += 1
            print(f"[warn] kfc リリース詳細 取得失敗 {url}: {e}", file=sys.stderr)
            continue
        info = parsers.parse_kfc_release_detail(html)
        if info is None:
            checked.add(url)   # 商品リリースではない(CM・新店など)
            continue
        stored[url] = {"date": a["date"], "title": a["title"], "launch_date": info["launch_date"],
                       "planned_end_text": info["planned_end_text"], "limited": info["limited"],
                       "items": info["items"]}
    infos = kfc_release_infos({"releases": stored})
    limited_seen = 0
    for e in entries.values():
        hit = kfc_find_release(e["name"], e.get("price"), infos, today)
        if hit and hit.get("limited"):
            limited_seen += 1
    st["limited_seen"] = limited_seen

    baseline = baseline_for(chain)
    entries = burst_filter(chain, entries, baseline, pending, stats, today)
    regular_budget = RegularBudget(st)
    for pid, e in entries.items():
        existing = load_product(chain, pid)
        if existing is not None:
            touch_seen(existing, list_price_for_history(st, e.get("price")), today, now, st)
            if e.get("name") and existing.get("name") != e["name"]:
                existing["name"] = e["name"]
            save_product(chain, existing)
            pending[chain].pop(pid, None)
            continue
        info = kfc_find_release(e["name"], e.get("price"), infos, today)
        is_new_menu = baseline is not None and pid not in baseline
        if info and info.get("limited"):
            kind = "期間限定"   # リリースの【商品概要】に載り、数量限定・期間限定の記載がある
        elif info:
            kind = "新メニュー"
        elif is_new_menu:
            kind = "新規掲載"
        else:
            kind = REGULAR_KIND
        is_regular = kind == REGULAR_KIND
        if is_regular and not regular_budget.take():
            continue   # 定番は1回の巡回で作る件数に上限がある。残りは翌日以降
        product = build_kfc_product(e, kind, today, now)
        if is_regular:
            mark_regular(product)
        else:
            kfc_apply_release(product, infos)
        save_product(chain, product)
        st["regular_created" if is_regular else "new_products"] += 1
        pending[chain].pop(pid, None)

    if st["list_ok"]:
        if baseline is None:
            save_baseline(chain, set(entries))
        apply_missing(chain, set(entries), today, st)
        state["menu_count"] = len(entries)
        state["checked_at"] = today
        state["releases"] = stored
        state["release_checked"] = sorted(checked)
        save_json(kfc_state_path(), state, sort_keys=True)

    # 発売日の確定(この回に処理したリリースとの照合)と、定番として記録済みの商品の限定への格上げ。
    # 終わって久しい商品は探し直さない
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=14)).isoformat()
    for p in load_chain_products(chain):
        if p.get("status") == "ended" and (p.get("ended_observed_at") or "") < cutoff:
            continue
        changed = False
        if p.get("is_regular"):
            info = kfc_find_release(p.get("name") or "", p.get("price"), infos, (p.get("first_seen_at") or today)[:10])
            if info and info.get("limited"):
                upgrade_regular(p, "期間限定")
                changed = True
        if kfc_apply_release(p, infos):
            changed = True
        if changed:
            save_product(chain, p)
    recorded = [p for p in load_chain_products(chain) if not p.get("is_regular")]
    st["news_matched"] = sum(1 for p in recorded if p.get("news_url"))
    st["news_unmatched"] = sum(1 for p in recorded if not p.get("news_url"))


# ---------------------------------------------------------------------------
# やよい軒 (www.yayoiken.com。2026-11-25追加、15チェーン目。実物の構造は parsers.py の同名のコメントにまとめた)
# ---------------------------------------------------------------------------
# 方針:
#  ・巡回は robots.txt + メニュー一覧(東京)1ページ + ニュース一覧2ページ + 未照合のニュース記事の詳細
#    (1回の実行で最大 YAYOIKEN_ARTICLE_MAX_FETCHES 件まで)。商品詳細ページは第1段階では取らないので、
#    商品レコードは一覧の情報だけで作る(定番の作成にも詳細取得は要らない)。
#  ・商品の正本はメニュー一覧(東京。都道府県差は実質なし)。発売日は自社サイトのニュース記事の
#    ■商品概要 が正本で、照合できたものは launch_date_source="news_article"(新設。KFCの press_release は
#    法人サイトのニュースリリース専用の含意のため流用しない)。記事の解析結果は data/yayoiken_state.json に
#    残し、後から現れた商品とも照合できるようにする(発売前の商品は一覧に載らないため、発表だけでは
#    記録しない)。■商品概要 の無い記事(出店情報など)は照合済みとして記録し、二度と取得しない。
#    発表日と発売日は約1週間ずれるので、発表日での代用はしない。照合できたが発売日が読めない場合は、
#    launch_date は null のまま news_title / news_url / news_date(発表日)だけ残す。
#  ・種別: 「おすすめメニュー(期間限定)」セクションに載った商品は 期間限定(badges にも付ける)。
#    NEWバッジ、またはニュース記事と照合して発売日が付いた商品は 新メニュー。baseline に無い新規は
#    新規掲載、それ以外は定番。フェア名(NABE FAIR等)は保存しない。テイクアウト・アルコールも記録する。
#  ・説明文・アレルゲンは対象外。栄養成分は店舗情報ページ (/menu_list/info/13) の栄養成分表から
#    取る (2026-11-27追加)。表は形態別 (白米・もち麦ごはん等) で、代表は先頭の形態。
#    巡回のたびに1リクエストで取り直し、値が変わった商品だけ更新する。表の商品数が前回の
#    半分未満の回は反映しない。
#  ・一覧が空・取得失敗・前回の半分未満の回は、何も更新せず終了判定もしない。
YAYOIKEN_MIN_RATIO = 0.5
YAYOIKEN_ARTICLE_MAX_FETCHES = 6
YAYOIKEN_ARTICLE_WINDOW_BEFORE_DAYS = 120
YAYOIKEN_ARTICLE_WINDOW_AFTER_DAYS = 14
YAYOIKEN_NEWS_PAGES = 2   # 照合範囲はニュース一覧2ページ(20件・約1か月分)


def yayoiken_state_path() -> Path:
    return DATA / "yayoiken_state.json"


def yayoiken_price_note(entry: dict) -> str:
    return "税込。公式サイトの一覧に表示された価格。"


def _yayoiken_robots(getter, st: dict):
    """robots.txt を読んで RobotFileParser を返す。ブロックされたら None(呼び出し側がその回を止める)。
    404・その他の失敗は全許可として扱う(やよい軒の robots.txt は404=ファイルなし)。"""
    st["list_requests"] += 1
    try:
        text = getter("yayoiken", parsers.YAYOIKEN_BASE + "/robots.txt")
    except NotFoundError:
        text = None
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] yayoiken robots.txt: {e}", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[warn] yayoiken robots.txt 取得失敗(全許可として続行): {e}", file=sys.stderr)
        text = None
    rp = urllib.robotparser.RobotFileParser()
    rp.parse((text or "User-agent: *\nAllow: /").splitlines())
    return rp


def yayoiken_article_infos(state: dict) -> list[dict]:
    """state に残した商品記事の解析結果を、新しい順の [{url, date, title, launch_date, items}] で返す。"""
    infos = [{"url": url, **info} for url, info in (state.get("articles") or {}).items()]
    infos.sort(key=lambda x: x.get("date") or "", reverse=True)
    return infos


def yayoiken_find_article(name: str, price: int | None, infos: list[dict], ref: str) -> dict | None:
    """商品名・価格で照合できるニュース記事を返す(ref の前120日〜後14日に発表されたもの)。"""
    try:
        c = datetime.date.fromisoformat(ref)
    except (ValueError, TypeError):
        return None
    lo = (c - datetime.timedelta(days=YAYOIKEN_ARTICLE_WINDOW_BEFORE_DAYS)).isoformat()
    hi = (c + datetime.timedelta(days=YAYOIKEN_ARTICLE_WINDOW_AFTER_DAYS)).isoformat()
    for info in infos:
        if not (lo <= (info.get("date") or "") <= hi):
            continue
        if parsers.match_yayoiken_article(name, price, info):
            return info
    return None


def yayoiken_apply_article(product: dict, infos: list[dict]) -> bool:
    """ニュース記事の発売日・お知らせ情報を反映する。変更があれば True。
    定番と、記事で確定済み(launch_date_source="news_article")の商品は照合しない。
    発売日が付いたら、新規掲載だった商品は新メニューに上げる。"""
    if product.get("is_regular") or product.get("launch_date_source") == "news_article":
        return False
    ref = (product.get("first_seen_at") or "")[:10]
    rel = product.get("relaunches") or []
    if rel and rel[-1].get("reappeared_at"):
        ref = rel[-1]["reappeared_at"]
    if not ref:
        return False
    info = yayoiken_find_article(product.get("name") or "", product.get("price"), infos, ref)
    if not info:
        return False
    before = (product.get("news_url"), product.get("launch_date"), product.get("launch_date_source"),
              product.get("limited_kind"))
    product["announced_at"] = info.get("date")
    product["news_url"] = info["url"]
    product["news_title"] = info.get("title")
    product["news_date"] = info.get("date")
    if info.get("launch_date"):
        product["launch_date"] = info["launch_date"]
        product["launch_date_source"] = "news_article"
        if product.get("limited_kind") in (None, "新規掲載"):
            product["limited_kind"] = "新メニュー"
    after = (product.get("news_url"), product.get("launch_date"), product.get("launch_date_source"),
             product.get("limited_kind"))
    return before != after


def yayoiken_apply_nutrition(product: dict, forms: list[dict]) -> bool:
    """店舗情報ページの栄養成分表の形態別栄養を反映する。代表は先頭の形態 (白米)。
    形態が2つ以上なら nutrition_variants に全形態を入れる。変更があれば True。"""
    if not forms:
        return False
    before = (product.get("nutrition"), product.get("nutrition_variants"))
    product["nutrition"] = forms[0]["nutrition"]
    product["nutrition_variants"] = forms if len(forms) > 1 else None
    return before != (product.get("nutrition"), product.get("nutrition_variants"))


def build_yayoiken_product(entry: dict, kind: str, today: str, now: str) -> dict:
    price = entry.get("price")
    return {
        "chain": "yayoiken",
        "product_id": entry["product_id"],
        "name": entry["name"],
        "category": entry.get("category"),
        "price": price,
        "price_note": yayoiken_price_note(entry),
        "price_variants": None,
        "launch_date": None,
        "launch_date_source": "not_found",
        "announced_at": None,
        "planned_end_text": None,
        "news_url": None,
        "news_title": None,
        "news_date": None,
        "description": None,
        "description_full_len": 0,
        "nutrition": None,
        "nutrition_variants": None,
        "nutrition_note": None,
        "nutrition_pdf_url": None,
        "nutrition_pdf_label": None,
        "allergen_pdf_url": None,
        "allergen_pdf_label": None,
        "allergens": None,
        "origin": None,
        "limited_kind": kind,
        "badges": ["期間限定"] if kind == "期間限定" else [],
        "official_url": entry["official_url"],
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [{"date": today, "price": price}] if price is not None else [],
        "source_list": ["menu"],
        "image": None,
    }


def crawl_yayoiken(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = "yayoiken"
    now = now_iso() if today is None else f"{today}T07:00:00+09:00"
    today = today or today_jst()
    pending.setdefault(chain, {})
    stats.setdefault(chain, new_stats())
    st = stats[chain]
    base = parsers.YAYOIKEN_BASE

    rp = _yayoiken_robots(getter, st)
    if rp is None:
        return

    def allowed(url: str) -> bool:
        return rp.can_fetch(UA, url)

    # メニュー一覧(東京の1ページに全タブ・全商品)。取れない・0件の回は list_ok を立てない
    menu_url = parsers.yayoiken_menu_url()
    entries: dict[str, dict] = {}
    if not allowed(menu_url):
        print(f"[skip] robots.txtでDisallow: {menu_url}", file=sys.stderr)
        return
    try:
        page = getter(chain, menu_url)
        st["list_requests"] += 1
    except BlockedError as e:
        st["list_requests"] += 1
        st["blocked"] = True
        print(f"[blocked] yayoiken menu {menu_url}: {e}", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        st["list_requests"] += 1
        print(f"[warn] yayoiken メニュー一覧 取得失敗 {menu_url}: {e}", file=sys.stderr)
        return
    for e in parsers.parse_yayoiken_menu(page):
        entries.setdefault(e["product_id"], e)
    if not entries:
        print("[warn] yayoiken メニュー一覧: 商品が0件(構造変更の疑い)", file=sys.stderr)
        return
    # 記録しない商品(名前が無い・価格が無いもの)を除く。テイクアウト・セット・アルコールは記録する
    entries = {pid: e for pid, e in entries.items() if not parsers.yayoiken_excluded(e)}
    state = load_json(yayoiken_state_path(), {})
    prev_count = state.get("menu_count")
    if entries and prev_count and len(entries) < prev_count * YAYOIKEN_MIN_RATIO:
        print(f"[warn] yayoiken メニュー一覧が極端に少ない({len(entries)}件。前回{prev_count}件)。"
              "消えた商品の判定は行わない", file=sys.stderr)
        return
    st["list_ok"] = bool(entries)
    if not st["list_ok"]:
        return

    # ニュース一覧(2ページ)。取れなくてもメニューの記録は続ける
    news_list: list[dict] = []
    seen_urls: set[str] = set()
    for pno in range(1, YAYOIKEN_NEWS_PAGES + 1):
        url = parsers.yayoiken_news_url(pno)
        if not allowed(url):
            print(f"[skip] robots.txtでDisallow: {url}", file=sys.stderr)
            continue
        try:
            text = getter(chain, url)
            st["detail_requests"] += 1
        except BlockedError as e:
            st["detail_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] yayoiken news list: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            st["detail_requests"] += 1
            print(f"[warn] yayoiken ニュース一覧 取得失敗 {url}: {e}", file=sys.stderr)
            continue
        for a in parsers.parse_yayoiken_news_list(text):
            if a["url"] not in seen_urls:
                seen_urls.add(a["url"])
                news_list.append(a)
    if not news_list:
        print("[warn] yayoiken ニュース一覧: 0件(構造変更の疑い)", file=sys.stderr)

    # 未照合の記事だけ詳細を取る(上限つき)。■商品概要 のあるものは解析結果を state に残し、
    # 無いものは照合済みの印だけ残す。取得に失敗したものは照合済みにしない(次回取り直す)
    stored = dict(state.get("articles") or {})
    checked = set(state.get("article_checked") or [])
    fetches = 0
    for a in news_list:
        url = a["url"]
        if url in stored or url in checked:
            continue
        if fetches >= YAYOIKEN_ARTICLE_MAX_FETCHES:
            break
        if not allowed(url):
            checked.add(url)
            continue
        fetches += 1
        try:
            html = getter(chain, url)
            st["detail_requests"] += 1
        except BlockedError as e:
            st["detail_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] yayoiken news article {url}: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            st["detail_requests"] += 1
            print(f"[warn] yayoiken ニュース記事 取得失敗 {url}: {e}", file=sys.stderr)
            continue
        info = parsers.parse_yayoiken_news_article(html)
        if info is None:
            checked.add(url)   # 商品記事ではない(出店情報など)
            continue
        stored[url] = {"date": a["date"], "title": a["title"],
                       "launch_date": info["launch_date"], "items": info["items"]}
    infos = yayoiken_article_infos({"articles": stored})
    st["limited_seen"] = sum(1 for e in entries.values() if e.get("in_limited_section") or e.get("is_new"))

    baseline = baseline_for(chain)
    entries = burst_filter(chain, entries, baseline, pending, stats, today)
    regular_budget = RegularBudget(st)
    for pid, e in entries.items():
        existing = load_product(chain, pid)
        if existing is not None:
            touch_seen(existing, list_price_for_history(st, e.get("price")), today, now, st)
            if e.get("name") and existing.get("name") != e["name"]:
                existing["name"] = e["name"]
            if e.get("in_limited_section") and existing.get("limited_kind") == REGULAR_KIND:
                upgrade_regular(existing, "期間限定")   # 定番が期間限定セクションに載ったら格上げ
                badges = list(existing.get("badges") or [])
                if "期間限定" not in badges:
                    badges.append("期間限定")
                existing["badges"] = badges
            save_product(chain, existing)
            pending[chain].pop(pid, None)
            continue
        info = yayoiken_find_article(e["name"], e.get("price"), infos, today)
        is_new_menu = baseline is not None and pid not in baseline
        if e.get("in_limited_section"):
            kind = "期間限定"   # 「おすすめメニュー(期間限定)」セクションに載った商品
        elif e.get("is_new") or (info and info.get("launch_date")):
            kind = "新メニュー"   # NEWバッジ、またはニュース記事で発売日が付いた商品
        elif is_new_menu:
            kind = "新規掲載"
        else:
            kind = REGULAR_KIND
        is_regular = kind == REGULAR_KIND
        if is_regular and not regular_budget.take():
            continue   # 定番は1回の巡回で作る件数に上限がある。残りは翌日以降
        product = build_yayoiken_product(e, kind, today, now)
        if is_regular:
            mark_regular(product)
        else:
            yayoiken_apply_article(product, infos)
        save_product(chain, product)
        st["regular_created" if is_regular else "new_products"] += 1
        pending[chain].pop(pid, None)

    # 栄養成分 (店舗情報ページの栄養成分表。2026-11-27追加。1回の実行で1リクエスト)。
    # 表が取れない・商品数が前回の半分未満の回は反映しない (既存の値はそのまま)
    if st["list_ok"]:
        info_url = parsers.yayoiken_info_url()
        nutrition_map = None
        if not allowed(info_url):
            print(f"[skip] robots.txtでDisallow: {info_url}", file=sys.stderr)
        else:
            try:
                info_html = getter(chain, info_url)
                st["detail_requests"] += 1
                nutrition_map = parsers.parse_yayoiken_nutrition(info_html)
            except BlockedError as e:
                st["detail_requests"] += 1
                st["blocked"] = True
                print(f"[blocked] yayoiken info {info_url}: {e}", file=sys.stderr)
                return
            except Exception as e:  # noqa: BLE001
                st["detail_requests"] += 1
                print(f"[warn] yayoiken 店舗情報ページ 取得失敗 {info_url}: {e}", file=sys.stderr)
        if nutrition_map is not None:
            prev_n = state.get("nutrition_count")
            if prev_n and len(nutrition_map) < prev_n * YAYOIKEN_MIN_RATIO:
                print(f"[warn] yayoiken 栄養成分表が極端に少ない({len(nutrition_map)}件。前回{prev_n}件)。"
                      "栄養成分の反映は行わない", file=sys.stderr)
            else:
                state["nutrition_count"] = len(nutrition_map)
                by_norm = {parsers.yayoiken_name_key(k): v for k, v in nutrition_map.items()}
                for p in load_chain_products(chain):
                    forms = by_norm.get(parsers.yayoiken_name_key(p.get("name") or ""))
                    if forms and yayoiken_apply_nutrition(p, forms):
                        save_product(chain, p)

    if st["list_ok"]:
        if baseline is None:
            save_baseline(chain, set(entries))
        apply_missing(chain, set(entries), today, st)
        state["menu_count"] = len(entries)
        state["checked_at"] = today
        state["articles"] = stored
        state["article_checked"] = sorted(checked)
        save_json(yayoiken_state_path(), state, sort_keys=True)

    # 発売日の確定(この回に処理した記事との照合)。終わって久しい商品は探し直さない
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=14)).isoformat()
    for p in load_chain_products(chain):
        if p.get("status") == "ended" and (p.get("ended_observed_at") or "") < cutoff:
            continue
        if yayoiken_apply_article(p, infos):
            save_product(chain, p)
    recorded = [p for p in load_chain_products(chain) if not p.get("is_regular")]
    st["news_matched"] = sum(1 for p in recorded if p.get("news_url"))
    st["news_unmatched"] = sum(1 for p in recorded if not p.get("news_url"))


# ---------------------------------------------------------------------------
# CoCo壱番屋 (www.ichibanya.co.jp。2026-11-27追加、16チェーン目。実物の構造は parsers.py の同名のコメントにまとめた)
# ---------------------------------------------------------------------------
# 方針:
#  ・巡回は robots.txt + カテゴリー別一覧16ページ + 【予告】商品の詳細ページ
#    (1回の実行で最大 COCOICHI_DETAIL_MAX_FETCHES 件まで。発売日が取れた商品は
#    state に残して二度取らない)。
#  ・商品の正本はカテゴリー別一覧。期間限定は「期間/数量限定メニュー」(cid=2)の
#    一覧に載った商品。発売日は【予告】商品の詳細ページの「○月○日より販売開始」が
#    正本で、launch_date_source は新値 "menu_detail"。販売開始後に初めて現れた
#    商品は発売日の記載がもう無いため、発売日は null のままにする (発表日等での
#    代用はしない)。ニュース記事は使わない。
#  ・商品名は【予告】を外して記録する (販売開始後に名前が変わって見えないように)。
#  ・説明文・アレルゲンは対象外。栄養成分は公式PDF (/menu/pdf/nutrition.pdf) から、巡回のあと
#    run_pdf_step() が pdf_update.py 経由で、更新があったときだけ取得して取り込む
#    (2026-11-27追加。吉野家・すき家方式)。値の基準 (ライス量「普通(300g)」等) は商品ごとに
#    PDFの注記に書かれるが、商品データには共通の注記だけを入れる。
#  ・一覧が全部取れない・0件・前回の半分未満の回は、何も更新せず終了判定もしない。
#    一部のカテゴリーだけ失敗した回は、取れた分で記録を続けるが終了判定はしない。
COCOICHI_MIN_RATIO = 0.5
COCOICHI_DETAIL_MAX_FETCHES = 12


def cocoichi_state_path() -> Path:
    return DATA / "cocoichi_state.json"


def cocoichi_price_note(entry: dict) -> str:
    return "税込。公式サイトの一覧に表示された価格。"


def _cocoichi_robots(getter, st: dict):
    """robots.txt を読んで RobotFileParser を返す。ブロックされたら None (呼び出し側がその回を止める)。
    404・その他の失敗は全許可として扱う。"""
    st["list_requests"] += 1
    try:
        text = getter("cocoichi", parsers.COCOICHI_BASE + "/robots.txt")
    except NotFoundError:
        text = None
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] cocoichi robots.txt: {e}", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[warn] cocoichi robots.txt 取得失敗(全許可として続行): {e}", file=sys.stderr)
        text = None
    rp = urllib.robotparser.RobotFileParser()
    rp.parse((text or "User-agent: *\nAllow: /").splitlines())
    return rp


def cocoichi_apply_launch(product: dict, teaser_launch: dict) -> bool:
    """state に残した【予告】詳細ページの発売日を反映する。変更があれば True。
    発売日が付いたら、新規掲載だった商品は新メニューに上げる。"""
    if product.get("launch_date"):
        return False
    info = teaser_launch.get(product.get("product_id") or "")
    if not info:
        return False
    product["launch_date"] = info
    product["launch_date_source"] = "menu_detail"
    if product.get("limited_kind") in (None, "新規掲載"):
        product["limited_kind"] = "新メニュー"
    return True


def build_cocoichi_product(entry: dict, kind: str, today: str, now: str) -> dict:
    price = entry.get("price")
    return {
        "chain": "cocoichi",
        "product_id": entry["product_id"],
        "name": entry["name"],
        "category": entry.get("category"),
        "price": price,
        "price_note": cocoichi_price_note(entry),
        "price_variants": None,
        "launch_date": None,
        "launch_date_source": "not_found",
        "announced_at": None,
        "planned_end_text": None,
        "news_url": None,
        "news_title": None,
        "news_date": None,
        "description": None,
        "description_full_len": 0,
        "nutrition": None,
        "nutrition_variants": None,
        "nutrition_note": None,
        "nutrition_pdf_url": None,
        "nutrition_pdf_label": None,
        "allergen_pdf_url": None,
        "allergen_pdf_label": None,
        "allergens": None,
        "origin": None,
        "limited_kind": kind,
        "badges": ["期間限定"] if kind == "期間限定" else [],
        "official_url": entry["official_url"],
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [{"date": today, "price": price}] if price is not None else [],
        "source_list": ["menu"],
        "image": None,
    }


def crawl_cocoichi(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = "cocoichi"
    now = now_iso() if today is None else f"{today}T07:00:00+09:00"
    today = today or today_jst()
    pending.setdefault(chain, {})
    stats.setdefault(chain, new_stats())
    st = stats[chain]

    rp = _cocoichi_robots(getter, st)
    if rp is None:
        return

    def allowed(url: str) -> bool:
        return rp.can_fetch(UA, url)

    # カテゴリー別一覧 (16ページ)。全部取れない・0件の回は list_ok を立てない。
    # 一部だけ失敗した回は記録を続けるが、終了判定はしない (partial フラグ)。
    entries: dict[str, dict] = {}
    partial = False
    for cid in parsers.COCOICHI_CATEGORY_CIDS:
        url = parsers.cocoichi_list_url(cid)
        if not allowed(url):
            print(f"[skip] robots.txtでDisallow: {url}", file=sys.stderr)
            partial = True
            continue
        try:
            page = getter(chain, url)
            st["list_requests"] += 1
        except BlockedError as e:
            st["list_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] cocoichi list {url}: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            st["list_requests"] += 1
            partial = True
            print(f"[warn] cocoichi 一覧 取得失敗 {url}: {e}", file=sys.stderr)
            continue
        parsed = parsers.parse_cocoichi_list(page)
        for item in parsed["items"]:
            if item["product_id"] in entries:
                continue
            entries[item["product_id"]] = {
                **item,
                "category": parsed["category"],
                "is_limited": cid == parsers.COCOICHI_LIMITED_CID,
            }
    if not entries:
        print("[warn] cocoichi 一覧: 商品が0件(構造変更の疑い)", file=sys.stderr)
        return
    entries = {pid: e for pid, e in entries.items() if not parsers.cocoichi_excluded(e)}
    state = load_json(cocoichi_state_path(), {})
    prev_count = state.get("menu_count")
    if prev_count and len(entries) < prev_count * COCOICHI_MIN_RATIO:
        print(f"[warn] cocoichi 一覧が極端に少ない({len(entries)}件。前回{prev_count}件)。"
              "消えた商品の判定は行わない", file=sys.stderr)
        return
    st["list_ok"] = not partial

    # 【予告】商品の詳細ページから発売日を取る (上限つき)。取れた日付は state に残し、
    # 販売開始後に一覧へ現れた商品にも付けられるようにする。失敗したものは次回取り直す。
    teaser_launch = dict(state.get("teaser_launch") or {})
    today_date = datetime.date.fromisoformat(today)
    fetches = 0
    for pid, e in entries.items():
        if not e.get("is_teaser") or pid in teaser_launch:
            continue
        if fetches >= COCOICHI_DETAIL_MAX_FETCHES:
            break
        url = parsers.cocoichi_detail_url(pid)
        if not allowed(url):
            continue
        fetches += 1
        try:
            html = getter(chain, url)
            st["detail_requests"] += 1
        except BlockedError as e2:
            st["detail_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] cocoichi detail {url}: {e2}", file=sys.stderr)
            return
        except Exception as e2:  # noqa: BLE001
            st["detail_requests"] += 1
            print(f"[warn] cocoichi 詳細 取得失敗 {url}: {e2}", file=sys.stderr)
            continue
        launch = parsers.parse_cocoichi_detail_launch(html, today_date)
        if launch:
            teaser_launch[pid] = launch
    st["limited_seen"] = sum(1 for e in entries.values() if e.get("is_limited") or e.get("is_teaser"))

    baseline = baseline_for(chain)
    entries = burst_filter(chain, entries, baseline, pending, stats, today)
    regular_budget = RegularBudget(st)
    for pid, e in entries.items():
        existing = load_product(chain, pid)
        if existing is not None:
            touch_seen(existing, list_price_for_history(st, e.get("price")), today, now, st)
            if e.get("name") and existing.get("name") != e["name"]:
                existing["name"] = e["name"]
            if e.get("is_limited") and existing.get("limited_kind") == REGULAR_KIND:
                upgrade_regular(existing, "期間限定")   # 定番が期間限定一覧に載ったら格上げ
                badges = list(existing.get("badges") or [])
                if "期間限定" not in badges:
                    badges.append("期間限定")
                existing["badges"] = badges
            cocoichi_apply_launch(existing, teaser_launch)
            save_product(chain, existing)
            pending[chain].pop(pid, None)
            continue
        is_new_menu = baseline is not None and pid not in baseline
        if e.get("is_limited"):
            kind = "期間限定"   # 「期間/数量限定メニュー」一覧に載った商品
        elif e.get("is_teaser") or pid in teaser_launch:
            kind = "新メニュー"   # 【予告】で載った商品、または予告時に発売日が取れた商品
        elif is_new_menu:
            kind = "新規掲載"
        else:
            kind = REGULAR_KIND
        is_regular = kind == REGULAR_KIND
        if is_regular and not regular_budget.take():
            continue   # 定番は1回の巡回で作る件数に上限がある。残りは翌日以降
        product = build_cocoichi_product(e, kind, today, now)
        if is_regular:
            mark_regular(product)
        else:
            cocoichi_apply_launch(product, teaser_launch)
        save_product(chain, product)
        st["regular_created" if is_regular else "new_products"] += 1
        pending[chain].pop(pid, None)

    if st["list_ok"]:
        if baseline is None:
            save_baseline(chain, set(entries))
        apply_missing(chain, set(entries), today, st)
    if not partial:
        state["menu_count"] = len(entries)
    state["checked_at"] = today
    state["teaser_launch"] = teaser_launch
    save_json(cocoichi_state_path(), state, sort_keys=True)


# ---------------------------------------------------------------------------
# はなまるうどん (www.hanamaruudon.com。2026-11-27追加、17チェーン目。実物の構造は parsers.py の同名のコメントにまとめた)
# ---------------------------------------------------------------------------
# 方針:
#  ・巡回は robots.txt + メニュー1ページ + ニュース一覧1ページ + 未照合のニュース記事の詳細
#    (1回の実行で最大 HANAMARU_ARTICLE_MAX_FETCHES 件まで) + 新しく記録する商品の詳細ページ。
#  ・商品の正本はメニュー1ページ (セクション7つ)。価格はサイズ別 (小・中・大) で、代表価格は
#    「小」。全サイズの価格とサイズ別 kcal は、初回記録のときだけ詳細ページから取る (丸亀方式)。
#    期間限定の商品は一覧に全サイズの価格が載るので詳細は取らない (詳細ページ自体が無い)。
#    2回目以降、定番のサイズ別価格は一覧に「小」しか出ないため更新できない (既知の制限)。
#  ・発売日は自社ニュース記事の「商品価格」+「販売期間」が正本で、照合できたものは
#    launch_date_source="news_article" (やよい軒と同じ)。記事の解析結果は
#    data/hanamaru_state.json に残し、後から現れた商品とも照合できるようにする。
#    発表日と発売日はずれるので、発表日での代用はしない。メニューページの期間限定
#    セクションに出る開始日は発売日の出どころにしない (オーナー決定 2026-11-27)。
#  ・種別: 期間限定セクションに載った商品は 期間限定 (badges にも付ける)。特集商品の
#    「新」バッジ、またはニュース記事と照合して発売日が付いた商品は 新メニュー。
#    baseline に無い新規は 新規掲載、それ以外は定番。テイクアウト専用も記録する。
#  ・説明文・アレルゲンは第1段階では対象外。栄養は kcal のみ (詳細ページのサイズ別)。
#  ・メニューが空・取得失敗・前回の半分未満の回は、何も更新せず終了判定もしない。
HANAMARU_MIN_RATIO = 0.5
HANAMARU_ARTICLE_MAX_FETCHES = 6
HANAMARU_ARTICLE_WINDOW_BEFORE_DAYS = 120
HANAMARU_ARTICLE_WINDOW_AFTER_DAYS = 14


def hanamaru_state_path() -> Path:
    return DATA / "hanamaru_state.json"


def hanamaru_price_note(prices: list[dict]) -> str:
    note = "税込。"
    if len(prices) > 1:
        note += "表示は「小」の価格。"
    note += "店舗により販売メニューや価格が異なる場合があります。"
    return note


def _hanamaru_robots(getter, st: dict):
    """robots.txt を読んで RobotFileParser を返す。ブロックされたら None (呼び出し側がその回を止める)。
    404・その他の失敗は全許可として扱う。"""
    st["list_requests"] += 1
    try:
        text = getter("hanamaru", parsers.HANAMARU_BASE + "/robots.txt")
    except NotFoundError:
        text = None
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] hanamaru robots.txt: {e}", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[warn] hanamaru robots.txt 取得失敗(全許可として続行): {e}", file=sys.stderr)
        text = None
    rp = urllib.robotparser.RobotFileParser()
    rp.parse((text or "User-agent: *\nAllow: /").splitlines())
    return rp


def hanamaru_article_infos(state: dict) -> list[dict]:
    """state に残した商品記事の解析結果を、新しい順の [{url, date, title, launch_date,
    planned_end_text, items}] で返す。"""
    infos = [{"url": url, **info} for url, info in (state.get("articles") or {}).items()]
    infos.sort(key=lambda x: x.get("date") or "", reverse=True)
    return infos


def hanamaru_find_article(name: str, price: int | None, infos: list[dict], ref: str) -> dict | None:
    """商品名・価格で照合できるニュース記事を返す (ref の前120日〜後14日に発表されたもの)。"""
    try:
        c = datetime.date.fromisoformat(ref)
    except (ValueError, TypeError):
        return None
    lo = (c - datetime.timedelta(days=HANAMARU_ARTICLE_WINDOW_BEFORE_DAYS)).isoformat()
    hi = (c + datetime.timedelta(days=HANAMARU_ARTICLE_WINDOW_AFTER_DAYS)).isoformat()
    for info in infos:
        if not (lo <= (info.get("date") or "") <= hi):
            continue
        if parsers.match_hanamaru_article(name, price, info):
            return info
    return None


def hanamaru_apply_article(product: dict, infos: list[dict]) -> bool:
    """ニュース記事の発売日・お知らせ情報を反映する。変更があれば True。
    定番と、記事で確定済み (launch_date_source="news_article") の商品は照合しない。
    発売日が付いたら、新規掲載だった商品は新メニューに上げる。"""
    if product.get("is_regular") or product.get("launch_date_source") == "news_article":
        return False
    ref = (product.get("first_seen_at") or "")[:10]
    rel = product.get("relaunches") or []
    if rel and rel[-1].get("reappeared_at"):
        ref = rel[-1]["reappeared_at"]
    if not ref:
        return False
    info = hanamaru_find_article(product.get("name") or "", product.get("price"), infos, ref)
    if not info:
        return False
    before = (product.get("news_url"), product.get("launch_date"), product.get("launch_date_source"),
              product.get("limited_kind"), product.get("planned_end_text"))
    product["announced_at"] = info.get("date")
    product["news_url"] = info["url"]
    product["news_title"] = info.get("title")
    product["news_date"] = info.get("date")
    if info.get("planned_end_text") and not product.get("planned_end_text"):
        product["planned_end_text"] = info["planned_end_text"]
    if info.get("launch_date"):
        product["launch_date"] = info["launch_date"]
        product["launch_date_source"] = "news_article"
        if product.get("limited_kind") in (None, "新規掲載"):
            product["limited_kind"] = "新メニュー"
    after = (product.get("news_url"), product.get("launch_date"), product.get("launch_date_source"),
             product.get("limited_kind"), product.get("planned_end_text"))
    return before != after


def build_hanamaru_product(entry: dict, kind: str, detail: dict | None, today: str, now: str) -> dict:
    price = entry.get("price")
    sizes = (detail or {}).get("sizes") or []
    if sizes:
        prices = [{"label": s["label"], "price": s["price"]} for s in sizes]
        nut_variants = []
        for s in sizes:
            if s.get("kcal") is None:
                continue
            nut = parsers.build_nutrition([("エネルギー", str(s["kcal"]))])
            if nut is not None:
                nut_variants.append({"label": s["label"], "nutrition": nut})
    else:
        prices = [{"label": p["label"], "price": p["price"]} for p in entry.get("prices") or []]
        nut_variants = []
    primary_nut = None
    for v in nut_variants:
        if v["label"] == "小":
            primary_nut = v
            break
    if primary_nut is None and nut_variants:
        primary_nut = nut_variants[0]
    product = {
        "chain": "hanamaru",
        "product_id": entry["product_id"],
        "name": entry["name"],
        "category": entry.get("category"),
        "price": price,
        "price_note": hanamaru_price_note(prices),
        "price_variants": prices if len(prices) > 1 else None,
        "launch_date": None,
        "launch_date_source": "not_found",
        "announced_at": None,
        "planned_end_text": None,
        "news_url": None,
        "news_title": None,
        "news_date": None,
        "description": None,
        "description_full_len": 0,
        "nutrition": primary_nut["nutrition"] if primary_nut else None,
        "nutrition_variants": nut_variants if len(nut_variants) > 1 else None,
        "nutrition_note": None,
        "nutrition_pdf_url": None,
        "nutrition_pdf_label": None,
        "allergen_pdf_url": None,
        "allergen_pdf_label": None,
        "allergens": None,
        "origin": None,
        "limited_kind": kind,
        "badges": ["期間限定"] if kind == "期間限定" else [],
        "official_url": entry["official_url"],
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [{"date": today, "price": price}] if price is not None else [],
        "source_list": ["menu"],
        "image": None,
    }
    seed_variants(product, today)
    return product


def crawl_hanamaru(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = "hanamaru"
    now = now_iso() if today is None else f"{today}T07:00:00+09:00"
    today = today or today_jst()
    pending.setdefault(chain, {})
    stats.setdefault(chain, new_stats())
    st = stats[chain]
    base = parsers.HANAMARU_BASE

    rp = _hanamaru_robots(getter, st)
    if rp is None:
        return

    def allowed(url: str) -> bool:
        return rp.can_fetch(UA, url)

    # メニュー1ページ (セクション7つ・全商品)。取れない・0件の回は list_ok を立てない
    menu_url = parsers.hanamaru_menu_url()
    entries: dict[str, dict] = {}
    if not allowed(menu_url):
        print(f"[skip] robots.txtでDisallow: {menu_url}", file=sys.stderr)
        return
    try:
        page = getter(chain, menu_url)
        st["list_requests"] += 1
    except BlockedError as e:
        st["list_requests"] += 1
        st["blocked"] = True
        print(f"[blocked] hanamaru menu {menu_url}: {e}", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        st["list_requests"] += 1
        print(f"[warn] hanamaru メニュー 取得失敗 {menu_url}: {e}", file=sys.stderr)
        return
    for e in parsers.parse_hanamaru_menu(page)["items"]:
        entries.setdefault(e["product_id"], e)
    if not entries:
        print("[warn] hanamaru メニュー: 商品が0件(構造変更の疑い)", file=sys.stderr)
        return
    entries = {pid: e for pid, e in entries.items() if not parsers.hanamaru_excluded(e)}
    state = load_json(hanamaru_state_path(), {})
    prev_count = state.get("menu_count")
    if prev_count and len(entries) < prev_count * HANAMARU_MIN_RATIO:
        print(f"[warn] hanamaru メニューが極端に少ない({len(entries)}件。前回{prev_count}件)。"
              "消えた商品の判定は行わない", file=sys.stderr)
        return
    st["list_ok"] = bool(entries)
    if not st["list_ok"]:
        return

    # ニュース一覧 (1ページ)。取れなくてもメニューの記録は続ける
    news_list: list[dict] = []
    news_url = parsers.hanamaru_news_url()
    if not allowed(news_url):
        print(f"[skip] robots.txtでDisallow: {news_url}", file=sys.stderr)
    else:
        try:
            text = getter(chain, news_url)
            st["detail_requests"] += 1
            news_list = parsers.parse_hanamaru_news_list(text)
        except BlockedError as e:
            st["detail_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] hanamaru news list: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            st["detail_requests"] += 1
            print(f"[warn] hanamaru ニュース一覧 取得失敗 {news_url}: {e}", file=sys.stderr)
    if not news_list:
        print("[warn] hanamaru ニュース一覧: 0件(構造変更の疑い)", file=sys.stderr)

    # 未照合の記事だけ詳細を取る (上限つき)。「商品価格」のあるものは解析結果を state に残し、
    # 無いものは照合済みの印だけ残す。取得に失敗したものは照合済みにしない (次回取り直す)
    stored = dict(state.get("articles") or {})
    checked = set(state.get("article_checked") or [])
    fetches = 0
    for a in news_list:
        url = a["url"]
        if url in stored or url in checked:
            continue
        if fetches >= HANAMARU_ARTICLE_MAX_FETCHES:
            break
        if not allowed(url):
            checked.add(url)
            continue
        fetches += 1
        try:
            html = getter(chain, url)
            st["detail_requests"] += 1
        except BlockedError as e:
            st["detail_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] hanamaru news article {url}: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            st["detail_requests"] += 1
            print(f"[warn] hanamaru ニュース記事 取得失敗 {url}: {e}", file=sys.stderr)
            continue
        info = parsers.parse_hanamaru_news_article(html)
        if info is None:
            checked.add(url)   # 商品記事ではない (キャンペーン・お知らせなど)
            continue
        stored[url] = {"date": a["date"], "title": a["title"],
                       "launch_date": info["launch_date"],
                       "planned_end_text": info["planned_end_text"], "items": info["items"]}
    infos = hanamaru_article_infos({"articles": stored})
    st["limited_seen"] = sum(1 for e in entries.values() if e.get("is_limited") or e.get("is_new"))

    baseline = baseline_for(chain)
    entries = burst_filter(chain, entries, baseline, pending, stats, today)
    regular_budget = RegularBudget(st)
    for pid, e in entries.items():
        existing = load_product(chain, pid)
        if existing is not None:
            touch_seen(existing, list_price_for_history(st, e.get("price")), today, now, st)
            # サイズ別価格が一覧に出る商品 (期間限定) は履歴も更新する。定番は「小」だけ
            # なので record_variants 側で何も書かれない
            if st["list_ok"]:
                record_variants(existing, [{"label": p["label"], "price": p["price"]}
                                           for p in e.get("prices") or []], today, st)
            if e.get("name") and existing.get("name") != e["name"]:
                existing["name"] = e["name"]
            if e.get("is_limited") and existing.get("limited_kind") == REGULAR_KIND:
                upgrade_regular(existing, "期間限定")   # 定番が期間限定セクションに載ったら格上げ
                badges = list(existing.get("badges") or [])
                if "期間限定" not in badges:
                    badges.append("期間限定")
                existing["badges"] = badges
            hanamaru_apply_article(existing, infos)
            save_product(chain, existing)
            pending[chain].pop(pid, None)
            continue
        info = hanamaru_find_article(e["name"], e.get("price"), infos, today)
        is_new_menu = baseline is not None and pid not in baseline
        if e.get("is_limited"):
            kind = "期間限定"   # 期間限定セクションに載った商品
        elif e.get("is_new") or (info and info.get("launch_date")):
            kind = "新メニュー"   # 「新」バッジ、またはニュース記事で発売日が付いた商品
        elif is_new_menu:
            kind = "新規掲載"
        else:
            kind = REGULAR_KIND
        is_regular = kind == REGULAR_KIND
        if is_regular and not regular_budget.take():
            continue   # 定番は1回の巡回で作る件数に上限がある。残りは翌日以降
        # 初回記録のときだけ詳細ページから全サイズの価格と kcal を取る (期間限定は詳細なし)
        detail = None
        if e.get("detail_path"):
            durl = base + e["detail_path"]
            if allowed(durl):
                try:
                    dhtml = getter(chain, durl)
                    st["detail_requests"] += 1
                    detail = parsers.parse_hanamaru_detail(dhtml)
                except BlockedError as e2:
                    st["detail_requests"] += 1
                    st["blocked"] = True
                    print(f"[blocked] hanamaru detail {durl}: {e2}", file=sys.stderr)
                    return
                except Exception as e2:  # noqa: BLE001
                    st["detail_requests"] += 1
                    print(f"[warn] hanamaru 詳細 取得失敗 {durl}: {e2}", file=sys.stderr)
                    detail = None
        product = build_hanamaru_product(e, kind, detail, today, now)
        if is_regular:
            mark_regular(product)
        else:
            hanamaru_apply_article(product, infos)
        save_product(chain, product)
        st["regular_created" if is_regular else "new_products"] += 1
        pending[chain].pop(pid, None)

    if st["list_ok"]:
        if baseline is None:
            save_baseline(chain, set(entries))
        apply_missing(chain, set(entries), today, st)
        state["menu_count"] = len(entries)
        state["checked_at"] = today
        state["articles"] = stored
        state["article_checked"] = sorted(checked)
        save_json(hanamaru_state_path(), state, sort_keys=True)

    # 発売日の確定 (この回に処理した記事との照合)。終わって久しい商品は探し直さない
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=14)).isoformat()
    for p in load_chain_products(chain):
        if p.get("status") == "ended" and (p.get("ended_observed_at") or "") < cutoff:
            continue
        if hanamaru_apply_article(p, infos):
            save_product(chain, p)
    recorded = [p for p in load_chain_products(chain) if not p.get("is_regular")]
    st["news_matched"] = sum(1 for p in recorded if p.get("news_url"))
    st["news_unmatched"] = sum(1 for p in recorded if not p.get("news_url"))


# ---------------------------------------------------------------------------
# 大戸屋 (www.ootoya.com。2026-11-27追加、18チェーン目。実物の構造は parsers.py の同名のコメントにまとめた)
# ---------------------------------------------------------------------------
# 方針:
#  ・巡回は robots.txt + 基準店舗 (新宿センタービル店) のメニュー1ページ + ニュース一覧1ページ +
#    未照合の「商品情報」記事の詳細 (1回の実行で最大 OOTOYA_ARTICLE_MAX_FETCHES 件まで)。
#  ・栄養成分は商品詳細ページの形態別 (ご飯・五穀ご飯・単品) の表から取る (2026-11-27夜に
#    オーナーが取り込みを決定。第1段階では取らないという同日の決定を変更)。代表は先頭の形態
#    (代表価格が定食価格なので「ご飯」) で、形態が複数あれば nutrition_variants に全形態を入れる。
#    詳細の取得は新規商品と、栄養がまだ無い既存商品の補完で、1回の実行で最大
#    OOTOYA_DETAIL_MAX_FETCHES 件まで。栄養の表が無い商品は state の nutrition_checked に
#    残して再取得しない。取得済みの栄養は取り直さない (丸亀・はなまる方式)。
#  ・商品の正本は基準店舗のメニュー (全カテゴリー・191件)。価格は定食価格が代表で、
#    単品価格は価格バリエーションとして記録する。一覧に両方の価格が毎回出るので、
#    バリエーションの履歴も毎回更新できる。
#  ・発売日は公式ニュースの「商品情報」記事が正本で、照合できたものは
#    launch_date_source="news_article" (やよい軒・はなまるうどんと同じ)。記事の解析結果は
#    data/ootoya_state.json に残す。販売期間の終了日は planned_end_text に入れる。
#    ニュース一覧は過去数年分が1ページに載るため、古い記事 (150日より前) は取得せずに
#    照合済みの印だけ付ける。「商品情報」以外の記事も取得しない。
#  ・種別: 「限定メニュー」カテゴリーの商品は 期間限定 (badges にも付ける)。ニュース記事と
#    照合して発売日が付いた商品は 新メニュー。baseline に無い新規は 新規掲載、それ以外は定番。
#  ・説明文・アレルゲンは対象外。
#  ・メニューが空・取得失敗・前回の半分未満の回は、何も更新せず終了判定もしない。
OOTOYA_MIN_RATIO = 0.5
OOTOYA_ARTICLE_MAX_FETCHES = 6
OOTOYA_DETAIL_MAX_FETCHES = 12
OOTOYA_ARTICLE_WINDOW_BEFORE_DAYS = 120
OOTOYA_ARTICLE_WINDOW_AFTER_DAYS = 14
OOTOYA_ARTICLE_OLDEST_DAYS = 150


def ootoya_state_path() -> Path:
    return DATA / "ootoya_state.json"


def ootoya_price_note(prices: list[dict]) -> str:
    note = "税込。"
    if len(prices) > 1:
        note += "表示は「定食」の価格。"
    note += "新宿センタービル店のメニュー・価格です。店舗により販売メニューや価格が異なる場合があります。"
    return note


def _ootoya_robots(getter, st: dict):
    """robots.txt を読んで RobotFileParser を返す。ブロックされたら None (呼び出し側がその回を止める)。
    404・その他の失敗は全許可として扱う (実物は空ファイル)。"""
    st["list_requests"] += 1
    try:
        text = getter("ootoya", parsers.OOTOYA_BASE + "/robots.txt")
    except NotFoundError:
        text = None
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] ootoya robots.txt: {e}", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[warn] ootoya robots.txt 取得失敗(全許可として続行): {e}", file=sys.stderr)
        text = None
    rp = urllib.robotparser.RobotFileParser()
    rp.parse((text or "User-agent: *\nAllow: /").splitlines())
    return rp


def ootoya_article_infos(state: dict) -> list[dict]:
    """state に残した商品記事の解析結果を、新しい順の [{url, date, title, launch_date,
    planned_end_text, items}] で返す。"""
    infos = [{"url": url, **info} for url, info in (state.get("articles") or {}).items()]
    infos.sort(key=lambda x: x.get("date") or "", reverse=True)
    return infos


def ootoya_find_article(name: str, price: int | None, infos: list[dict], ref: str) -> dict | None:
    """商品名・価格で照合できるニュース記事を返す (ref の前120日〜後14日に発表されたもの)。"""
    try:
        c = datetime.date.fromisoformat(ref)
    except (ValueError, TypeError):
        return None
    lo = (c - datetime.timedelta(days=OOTOYA_ARTICLE_WINDOW_BEFORE_DAYS)).isoformat()
    hi = (c + datetime.timedelta(days=OOTOYA_ARTICLE_WINDOW_AFTER_DAYS)).isoformat()
    for info in infos:
        if not (lo <= (info.get("date") or "") <= hi):
            continue
        if parsers.match_ootoya_article(name, price, info):
            return info
    return None


def ootoya_apply_article(product: dict, infos: list[dict]) -> bool:
    """ニュース記事の発売日・お知らせ情報を反映する。変更があれば True。
    定番と、記事で確定済み (launch_date_source="news_article") の商品は照合しない。
    発売日が付いたら、新規掲載だった商品は新メニューに上げる。"""
    if product.get("is_regular") or product.get("launch_date_source") == "news_article":
        return False
    ref = (product.get("first_seen_at") or "")[:10]
    rel = product.get("relaunches") or []
    if rel and rel[-1].get("reappeared_at"):
        ref = rel[-1]["reappeared_at"]
    if not ref:
        return False
    info = ootoya_find_article(product.get("name") or "", product.get("price"), infos, ref)
    if not info:
        return False
    before = (product.get("news_url"), product.get("launch_date"), product.get("launch_date_source"),
              product.get("limited_kind"), product.get("planned_end_text"))
    product["announced_at"] = info.get("date")
    product["news_url"] = info["url"]
    product["news_title"] = info.get("title")
    product["news_date"] = info.get("date")
    if info.get("planned_end_text") and not product.get("planned_end_text"):
        product["planned_end_text"] = info["planned_end_text"]
    if info.get("launch_date"):
        product["launch_date"] = info["launch_date"]
        product["launch_date_source"] = "news_article"
        if product.get("limited_kind") in (None, "新規掲載"):
            product["limited_kind"] = "新メニュー"
    after = (product.get("news_url"), product.get("launch_date"), product.get("launch_date_source"),
             product.get("limited_kind"), product.get("planned_end_text"))
    return before != after


def ootoya_apply_nutrition(product: dict, forms: list[dict]) -> bool:
    """詳細ページの形態別栄養を反映する。代表は先頭の形態 (定食価格が代表なので「ご飯」)。
    形態が2つ以上なら nutrition_variants に全形態を入れる。変更があれば True。"""
    if not forms:
        return False
    before = (product.get("nutrition"), product.get("nutrition_variants"))
    product["nutrition"] = forms[0]["nutrition"]
    product["nutrition_variants"] = forms if len(forms) > 1 else None
    return before != (product.get("nutrition"), product.get("nutrition_variants"))


def build_ootoya_product(entry: dict, kind: str, today: str, now: str) -> dict:
    price = entry.get("price")
    prices = [{"label": p["label"], "price": p["price"]} for p in entry.get("prices") or []]
    product = {
        "chain": "ootoya",
        "product_id": entry["product_id"],
        "name": entry["name"],
        "category": entry.get("category"),
        "price": price,
        "price_note": ootoya_price_note(prices),
        "price_variants": prices if len(prices) > 1 else None,
        "launch_date": None,
        "launch_date_source": "not_found",
        "announced_at": None,
        "planned_end_text": None,
        "news_url": None,
        "news_title": None,
        "news_date": None,
        "description": None,
        "description_full_len": 0,
        "nutrition": None,
        "nutrition_variants": None,
        "nutrition_note": None,
        "nutrition_pdf_url": None,
        "nutrition_pdf_label": None,
        "allergen_pdf_url": None,
        "allergen_pdf_label": None,
        "allergens": None,
        "origin": None,
        "limited_kind": kind,
        "badges": ["期間限定"] if kind == "期間限定" else [],
        "official_url": entry["official_url"],
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [{"date": today, "price": price}] if price is not None else [],
        "source_list": ["menu"],
        "image": None,
    }
    seed_variants(product, today)
    return product


def crawl_ootoya(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = "ootoya"
    now = now_iso() if today is None else f"{today}T07:00:00+09:00"
    today = today or today_jst()
    pending.setdefault(chain, {})
    stats.setdefault(chain, new_stats())
    st = stats[chain]

    rp = _ootoya_robots(getter, st)
    if rp is None:
        return

    def allowed(url: str) -> bool:
        return rp.can_fetch(UA, url)

    # 基準店舗のメニュー1ページ (全カテゴリー)。取れない・0件の回は list_ok を立てない
    menu_url = parsers.ootoya_menu_url()
    entries: dict[str, dict] = {}
    if not allowed(menu_url):
        print(f"[skip] robots.txtでDisallow: {menu_url}", file=sys.stderr)
        return
    try:
        page = getter(chain, menu_url)
        st["list_requests"] += 1
    except BlockedError as e:
        st["list_requests"] += 1
        st["blocked"] = True
        print(f"[blocked] ootoya menu {menu_url}: {e}", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        st["list_requests"] += 1
        print(f"[warn] ootoya メニュー 取得失敗 {menu_url}: {e}", file=sys.stderr)
        return
    for e in parsers.parse_ootoya_menu(page)["items"]:
        entries.setdefault(e["product_id"], e)
    if not entries:
        print("[warn] ootoya メニュー: 商品が0件(構造変更の疑い)", file=sys.stderr)
        return
    entries = {pid: e for pid, e in entries.items() if not parsers.ootoya_excluded(e)}
    state = load_json(ootoya_state_path(), {})
    prev_count = state.get("menu_count")
    if prev_count and len(entries) < prev_count * OOTOYA_MIN_RATIO:
        print(f"[warn] ootoya メニューが極端に少ない({len(entries)}件。前回{prev_count}件)。"
              "消えた商品の判定は行わない", file=sys.stderr)
        return
    st["list_ok"] = bool(entries)
    if not st["list_ok"]:
        return

    # ニュース一覧 (1ページ・過去数年分)。取れなくてもメニューの記録は続ける
    news_list: list[dict] = []
    news_url = parsers.ootoya_news_url()
    if not allowed(news_url):
        print(f"[skip] robots.txtでDisallow: {news_url}", file=sys.stderr)
    else:
        try:
            text = getter(chain, news_url)
            st["detail_requests"] += 1
            news_list = parsers.parse_ootoya_news_list(text)
        except BlockedError as e:
            st["detail_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] ootoya news list: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            st["detail_requests"] += 1
            print(f"[warn] ootoya ニュース一覧 取得失敗 {news_url}: {e}", file=sys.stderr)
    if not news_list:
        print("[warn] ootoya ニュース一覧: 0件(構造変更の疑い)", file=sys.stderr)

    # 未照合の「商品情報」記事だけ詳細を取る (上限つき)。商品記事は解析結果を state に残し、
    # 商品記事でないものは照合済みの印だけ残す。古い記事と「商品情報」以外は取得しない
    stored = dict(state.get("articles") or {})
    checked = set(state.get("article_checked") or [])
    oldest = (datetime.date.fromisoformat(today)
              - datetime.timedelta(days=OOTOYA_ARTICLE_OLDEST_DAYS)).isoformat()
    fetches = 0
    for a in news_list:
        url = a["url"]
        if url in stored or url in checked:
            continue
        if a.get("category") != parsers.OOTOYA_PRODUCT_CATEGORY or (a.get("date") or "") < oldest:
            checked.add(url)
            continue
        if fetches >= OOTOYA_ARTICLE_MAX_FETCHES:
            break
        if not allowed(url):
            checked.add(url)
            continue
        fetches += 1
        try:
            html = getter(chain, url)
            st["detail_requests"] += 1
        except BlockedError as e:
            st["detail_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] ootoya news article {url}: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            st["detail_requests"] += 1
            print(f"[warn] ootoya ニュース記事 取得失敗 {url}: {e}", file=sys.stderr)
            continue
        info = parsers.parse_ootoya_news_article(html)
        if info is None:
            checked.add(url)   # 商品記事ではない (価格・発売日の読み取れない記事)
            continue
        stored[url] = {"date": a["date"], "title": a["title"],
                       "launch_date": info["launch_date"],
                       "planned_end_text": info["planned_end_text"], "items": info["items"]}
    infos = ootoya_article_infos({"articles": stored})
    st["limited_seen"] = sum(1 for e in entries.values() if e.get("is_limited"))

    # 栄養成分の補完 (商品詳細ページ)。上限に達したらこの回は打ち切る
    nutrition_checked = set(state.get("nutrition_checked") or [])
    detail_state = {"fetches": 0, "blocked": False}

    def fetch_nutrition(pid: str) -> list[dict] | None:
        """詳細ページから形態別栄養を取る。取得失敗は None (次回に回す)、
        取れたが栄養の表が無ければ [] (nutrition_checked に残して再取得しない)。"""
        if detail_state["fetches"] >= OOTOYA_DETAIL_MAX_FETCHES or detail_state["blocked"]:
            return None
        url = parsers.ootoya_detail_url(pid)
        if not allowed(url):
            return []
        detail_state["fetches"] += 1
        try:
            html = getter(chain, url)
            st["detail_requests"] += 1
        except BlockedError as e:
            st["detail_requests"] += 1
            st["blocked"] = True
            detail_state["blocked"] = True
            print(f"[blocked] ootoya detail {url}: {e}", file=sys.stderr)
            return None
        except Exception as e:  # noqa: BLE001
            st["detail_requests"] += 1
            print(f"[warn] ootoya 詳細 取得失敗 {url}: {e}", file=sys.stderr)
            return None
        return parsers.parse_ootoya_detail(html)

    baseline = baseline_for(chain)
    entries = burst_filter(chain, entries, baseline, pending, stats, today)
    regular_budget = RegularBudget(st)
    for pid, e in entries.items():
        existing = load_product(chain, pid)
        if existing is not None:
            touch_seen(existing, list_price_for_history(st, e.get("price")), today, now, st)
            # 一覧に定食・単品の両価格が毎回出るので、バリエーションの履歴も更新する
            if st["list_ok"]:
                record_variants(existing, [{"label": p["label"], "price": p["price"]}
                                           for p in e.get("prices") or []], today, st)
            if e.get("name") and existing.get("name") != e["name"]:
                existing["name"] = e["name"]
            if e.get("is_limited") and existing.get("limited_kind") == REGULAR_KIND:
                upgrade_regular(existing, "期間限定")   # 定番が限定メニューに載ったら格上げ
                badges = list(existing.get("badges") or [])
                if "期間限定" not in badges:
                    badges.append("期間限定")
                existing["badges"] = badges
            ootoya_apply_article(existing, infos)
            if existing.get("nutrition") is None and pid not in nutrition_checked:
                forms = fetch_nutrition(pid)
                if forms is not None:
                    if forms:
                        ootoya_apply_nutrition(existing, forms)
                    else:
                        nutrition_checked.add(pid)
            save_product(chain, existing)
            pending[chain].pop(pid, None)
            continue
        info = ootoya_find_article(e["name"], e.get("price"), infos, today)
        is_new_menu = baseline is not None and pid not in baseline
        if e.get("is_limited"):
            kind = "期間限定"   # 「限定メニュー」カテゴリーに載った商品
        elif info and info.get("launch_date"):
            kind = "新メニュー"   # ニュース記事で発売日が付いた商品
        elif is_new_menu:
            kind = "新規掲載"
        else:
            kind = REGULAR_KIND
        is_regular = kind == REGULAR_KIND
        if is_regular and not regular_budget.take():
            continue   # 定番は1回の巡回で作る件数に上限がある。残りは翌日以降
        product = build_ootoya_product(e, kind, today, now)
        if is_regular:
            mark_regular(product)
        else:
            ootoya_apply_article(product, infos)
        forms = fetch_nutrition(pid)
        if forms is not None:
            if forms:
                ootoya_apply_nutrition(product, forms)
            else:
                nutrition_checked.add(pid)
        save_product(chain, product)
        st["regular_created" if is_regular else "new_products"] += 1
        pending[chain].pop(pid, None)

    if st["list_ok"]:
        if baseline is None:
            save_baseline(chain, set(entries))
        apply_missing(chain, set(entries), today, st)
        state["menu_count"] = len(entries)
        state["checked_at"] = today
        state["articles"] = stored
        state["article_checked"] = sorted(checked)
        state["nutrition_checked"] = sorted(nutrition_checked)
        save_json(ootoya_state_path(), state, sort_keys=True)

    # 発売日の確定 (この回に処理した記事との照合)。終わって久しい商品は探し直さない
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=14)).isoformat()
    for p in load_chain_products(chain):
        if p.get("status") == "ended" and (p.get("ended_observed_at") or "") < cutoff:
            continue
        if ootoya_apply_article(p, infos):
            save_product(chain, p)
    recorded = [p for p in load_chain_products(chain) if not p.get("is_regular")]
    st["news_matched"] = sum(1 for p in recorded if p.get("news_url"))
    st["news_unmatched"] = sum(1 for p in recorded if not p.get("news_url"))


# ---------------------------------------------------------------------------
# フレッシュネスバーガー (www.freshnessburger.co.jp)
# ---------------------------------------------------------------------------
# 方針:
#  ・巡回は robots.txt + メニュー一覧3ページ + ブランドニュース一覧1ページ + 未照合の
#    プレスリリースPDF (1回の実行で最大 FRESHNESS_RELEASE_MAX_FETCHES 件まで)。
#  ・商品の正本はメニュー一覧 (バーガー・サイドメニュー・ドリンク)。セット・モーニング・
#    キッズ・フレバル・スイーツは名前・価格が画像の中だけなので記録せず、「店舗限定」
#    セクションも記録しない (オーナー決定)。サイズ別の価格は全サイズを記録し、代表価格は
#    先頭のサイズ (はなまるうどんの「小」と同じ)。
#  ・発売日はブランドニュースのプレスリリースPDFから取る (launch_date_source="press_pdf")。
#    販売期間の終わりは planned_end_text に入れる。販売期間のある商品は期間限定にし、
#    定番として記録済みの商品でも、販売期間のあるリリースと照合できれば期間限定へ上げる。
#  ・説明文は取らない。栄養成分は公式PDF (/pdf/seibun.pdf) から、巡回のあと run_pdf_step() が
#    pdf_update.py 経由で、更新があったときだけ取得して取り込む (2026-11-27追加。吉野家・すき家方式)。
#  ・一覧が取れない・0件の回は終了判定をしない。一部のページだけ取れた回は記録するが、
#    終了判定は全ページ取れた回だけ行う。
FRESHNESS_MIN_RATIO = 0.5
FRESHNESS_RELEASE_MAX_FETCHES = 4
FRESHNESS_RELEASE_WINDOW_BEFORE_DAYS = 120
FRESHNESS_RELEASE_WINDOW_AFTER_DAYS = 14
FRESHNESS_RELEASE_OLDEST_DAYS = 150


def freshness_state_path() -> Path:
    return DATA / "freshness_state.json"


def freshness_price_note(entry: dict) -> str:
    prices = entry.get("prices") or []
    note = "税込。"
    if len(prices) > 1 and prices[0].get("label"):
        note += f"表示は「{prices[0]['label']}」の価格。"
    note += "店舗により販売メニューや価格が異なる場合があります。"
    return note


def _freshness_robots(getter, st: dict):
    """robots.txt を読んで RobotFileParser を返す。ブロックされたら None (呼び出し側がその回を止める)。
    フレッシュネスは robots.txt が無くトップページのHTMLが返るので、HTMLが返ったら全許可として扱う。"""
    st["list_requests"] += 1
    try:
        text = getter("freshness", parsers.FRESHNESS_BASE + "/robots.txt")
    except NotFoundError:
        text = None
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] freshness robots.txt: {e}", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[warn] freshness robots.txt 取得失敗(全許可として続行): {e}", file=sys.stderr)
        text = None
    if text and text.lstrip().startswith("<"):
        text = None
    rp = urllib.robotparser.RobotFileParser()
    rp.parse((text or "User-agent: *\nAllow: /").splitlines())
    return rp


def freshness_release_infos(state: dict) -> list[dict]:
    """state に残したプレスリリースの解析結果を、新しい順の [{url, date, title, launch_date,
    planned_end_text, items, text}] で返す。"""
    infos = [{"url": url, **info} for url, info in (state.get("releases") or {}).items()]
    infos.sort(key=lambda x: x.get("date") or "", reverse=True)
    return infos


def freshness_find_release(name: str, price: int | None, infos: list[dict], ref: str) -> dict | None:
    """商品名・価格で照合できるプレスリリースを返す (ref の前120日〜後14日に発表されたもの)。"""
    try:
        c = datetime.date.fromisoformat(ref)
    except (ValueError, TypeError):
        return None
    lo = (c - datetime.timedelta(days=FRESHNESS_RELEASE_WINDOW_BEFORE_DAYS)).isoformat()
    hi = (c + datetime.timedelta(days=FRESHNESS_RELEASE_WINDOW_AFTER_DAYS)).isoformat()
    for info in infos:
        if not (lo <= (info.get("date") or "") <= hi):
            continue
        if parsers.match_freshness_press(name, price, info):
            return info
    return None


def freshness_apply_release(product: dict, infos: list[dict]) -> bool:
    """プレスリリースの発売日・販売期間を反映する。変更があれば True。
    PDFで確定済み (launch_date_source="press_pdf") の商品は照合しない。
    販売期間のあるリリースと照合できたら、定番の商品も期間限定へ上げる。"""
    if product.get("launch_date_source") == "press_pdf":
        return False
    ref = (product.get("first_seen_at") or "")[:10]
    rel = product.get("relaunches") or []
    if rel and rel[-1].get("reappeared_at"):
        ref = rel[-1]["reappeared_at"]
    if not ref:
        return False
    info = freshness_find_release(product.get("name") or "", product.get("price"), infos, ref)
    if not info:
        return False
    before = (product.get("news_url"), product.get("launch_date"), product.get("launch_date_source"),
              product.get("limited_kind"), product.get("planned_end_text"), product.get("is_regular"))
    product["announced_at"] = info.get("date")
    product["news_url"] = info["url"]
    product["news_title"] = info.get("title")
    product["news_date"] = info.get("date")
    if info.get("launch_date"):
        product["launch_date"] = info["launch_date"]
        product["launch_date_source"] = "press_pdf"
    if info.get("planned_end_text"):
        if not product.get("planned_end_text"):
            product["planned_end_text"] = info["planned_end_text"]
        if product.get("is_regular"):
            upgrade_regular(product, "期間限定")
            badges = list(product.get("badges") or [])
            if "期間限定" not in badges:
                badges.append("期間限定")
            product["badges"] = badges
        elif product.get("limited_kind") in (None, "新規掲載"):
            product["limited_kind"] = "期間限定"
            badges = list(product.get("badges") or [])
            if "期間限定" not in badges:
                badges.append("期間限定")
            product["badges"] = badges
    elif info.get("launch_date") and product.get("limited_kind") in (None, "新規掲載"):
        product["limited_kind"] = "新メニュー"
    after = (product.get("news_url"), product.get("launch_date"), product.get("launch_date_source"),
             product.get("limited_kind"), product.get("planned_end_text"), product.get("is_regular"))
    return before != after


def fetch_freshness_press(getter, url: str) -> dict | None:
    """プレスリリースPDFを取得して解析する。pdfminer が無い環境と、PDFでないURL
    (HTMLのお知らせ) では None。取得の失敗は例外として呼び出し側へ返す。

    お知らせは brandnews の一覧に PDF (商品発表) と HTML (通常のお知らせ) が混ざる。
    HTML は正常な応答なので、allow_html でブロック扱いにせず受け取り、%PDF でない
    ことを見て None にする (呼び出し側が「照合済み」の印を付ける)。チャレンジの
    兆候がある応答は allow_html でも BlockedError になる (fetch_binary 側)。
    """
    if not pdf_update.have_pdfminer():
        return None
    data = getter.pdf_get("freshness", url, allow_html=True)
    if not data.startswith(b"%PDF"):
        return None
    return parsers.parse_freshness_press(pdf_update.pn.extract_geometry(data))


def build_freshness_product(entry: dict, kind: str, today: str, now: str) -> dict:
    price = entry.get("price")
    prices = [{"label": p["label"], "price": p["price"]} for p in entry.get("prices") or []]
    product = {
        "chain": "freshness",
        "product_id": entry["product_id"],
        "name": entry["name"],
        "category": entry.get("category"),
        "price": price,
        "price_note": freshness_price_note(entry),
        "price_variants": prices if len(prices) > 1 else None,
        "launch_date": None,
        "launch_date_source": "not_found",
        "announced_at": None,
        "planned_end_text": None,
        "news_url": None,
        "news_title": None,
        "news_date": None,
        "description": None,
        "description_full_len": 0,
        "nutrition": None,
        "nutrition_variants": None,
        "nutrition_note": None,
        "nutrition_pdf_url": None,
        "nutrition_pdf_label": None,
        "allergen_pdf_url": None,
        "allergen_pdf_label": None,
        "allergens": None,
        "origin": None,
        "limited_kind": kind,
        "badges": ["期間限定"] if kind == "期間限定" else [],
        "official_url": entry["official_url"],
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [{"date": today, "price": price}] if price is not None else [],
        "source_list": ["menu"],
        "image": None,
    }
    seed_variants(product, today)
    return product


def crawl_freshness(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = "freshness"
    now = now_iso() if today is None else f"{today}T07:00:00+09:00"
    today = today or today_jst()
    pending.setdefault(chain, {})
    stats.setdefault(chain, new_stats())
    st = stats[chain]

    rp = _freshness_robots(getter, st)
    if rp is None:
        return

    def allowed(url: str) -> bool:
        return rp.can_fetch(UA, url)

    # メニュー一覧3ページ (バーガー・サイドメニュー・ドリンク)。全ページ取れた回だけ終了判定する
    entries: dict[str, dict] = {}
    pages_ok = 0
    for page_name in parsers.FRESHNESS_MENU_PAGES:
        menu_url = parsers.freshness_menu_url(page_name)
        if not allowed(menu_url):
            print(f"[skip] robots.txtでDisallow: {menu_url}", file=sys.stderr)
            continue
        try:
            page = getter(chain, menu_url)
            st["list_requests"] += 1
        except BlockedError as e:
            st["list_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] freshness menu {menu_url}: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            st["list_requests"] += 1
            print(f"[warn] freshness メニュー 取得失敗 {menu_url}: {e}", file=sys.stderr)
            continue
        pages_ok += 1
        for e in parsers.parse_freshness_menu(page, page_name)["items"]:
            entries.setdefault(e["product_id"], e)
    if not entries:
        print("[warn] freshness メニュー: 商品が0件(構造変更の疑い)", file=sys.stderr)
        return
    state = load_json(freshness_state_path(), {})
    prev_count = state.get("menu_count")
    if prev_count and len(entries) < prev_count * FRESHNESS_MIN_RATIO:
        print(f"[warn] freshness メニューが極端に少ない({len(entries)}件。前回{prev_count}件)。"
              "消えた商品の判定は行わない", file=sys.stderr)
        return
    all_pages_ok = pages_ok == len(parsers.FRESHNESS_MENU_PAGES)
    st["list_ok"] = all_pages_ok

    # ブランドニュース一覧 (1ページ・全件)。取れなくてもメニューの記録は続ける
    news_list: list[dict] = []
    news_url = parsers.freshness_news_url()
    if not allowed(news_url):
        print(f"[skip] robots.txtでDisallow: {news_url}", file=sys.stderr)
    else:
        try:
            text = getter(chain, news_url)
            st["detail_requests"] += 1
            news_list = parsers.parse_freshness_news_list(text)
        except BlockedError as e:
            st["detail_requests"] += 1
            st["blocked"] = True
            print(f"[blocked] freshness news list: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            st["detail_requests"] += 1
            print(f"[warn] freshness ニュース一覧 取得失敗 {news_url}: {e}", file=sys.stderr)
    if not news_list:
        print("[warn] freshness ニュース一覧: 0件(構造変更の疑い)", file=sys.stderr)

    # 未照合のプレスリリースPDFだけ取る (上限つき)。PDFでないお知らせ (HTML) と、発売日も
    # 商品も読み取れないPDFは、照合済みの印だけ残す。150日より古いものは取得しない
    stored = dict(state.get("releases") or {})
    checked = set(state.get("release_checked") or [])
    oldest = (datetime.date.fromisoformat(today)
              - datetime.timedelta(days=FRESHNESS_RELEASE_OLDEST_DAYS)).isoformat()
    fetches = 0
    if news_list and pdf_update.have_pdfminer():
        for a in news_list:
            url = a["url"]
            if url in stored or url in checked:
                continue
            if (a.get("date") or "") < oldest:
                checked.add(url)
                continue
            if fetches >= FRESHNESS_RELEASE_MAX_FETCHES:
                break
            if not allowed(url):
                checked.add(url)
                continue
            fetches += 1
            try:
                info = fetch_freshness_press(getter, url)
                st["detail_requests"] += 1
            except BlockedError as e:
                st["detail_requests"] += 1
                st["blocked"] = True
                print(f"[blocked] freshness press PDF {url}: {e}", file=sys.stderr)
                return
            except Exception as e:  # noqa: BLE001
                st["detail_requests"] += 1
                print(f"[warn] freshness プレスリリース 取得・解析失敗 {url}: {e}", file=sys.stderr)
                continue
            if info is None:
                checked.add(url)   # HTMLのお知らせ (障害情報など)
                continue
            if info.get("launch_date") or info.get("items"):
                stored[url] = {"date": a["date"], "title": a["title"],
                               "launch_date": info["launch_date"],
                               "planned_end_text": info["planned_end_text"],
                               "items": info["items"], "text": info["text"]}
            else:
                checked.add(url)
    infos = freshness_release_infos({"releases": stored})

    baseline = baseline_for(chain)
    entries = burst_filter(chain, entries, baseline, pending, stats, today)
    regular_budget = RegularBudget(st)
    for pid, e in entries.items():
        existing = load_product(chain, pid)
        if existing is not None:
            touch_seen(existing, list_price_for_history(st, e.get("price")), today, now, st)
            # サイズ別の価格が一覧に毎回出るので、バリエーションの履歴も更新する
            if st["list_ok"]:
                record_variants(existing, [{"label": p["label"], "price": p["price"]}
                                           for p in e.get("prices") or []], today, st)
            if e.get("name") and existing.get("name") != e["name"]:
                existing["name"] = e["name"]
            freshness_apply_release(existing, infos)
            save_product(chain, existing)
            pending[chain].pop(pid, None)
            continue
        info = freshness_find_release(e["name"], e.get("price"), infos, today)
        is_new_menu = baseline is not None and pid not in baseline
        if info and info.get("planned_end_text"):
            kind = "期間限定"   # 販売期間のあるプレスリリースで発表された商品
        elif info and info.get("launch_date"):
            kind = "新メニュー"   # プレスリリースで発売日が付いた商品
        elif is_new_menu:
            kind = "新規掲載"
        else:
            kind = REGULAR_KIND
        is_regular = kind == REGULAR_KIND
        if is_regular and not regular_budget.take():
            continue   # 定番は1回の巡回で作る件数に上限がある。残りは翌日以降
        product = build_freshness_product(e, kind, today, now)
        if is_regular:
            mark_regular(product)
        else:
            freshness_apply_release(product, infos)
        save_product(chain, product)
        st["regular_created" if is_regular else "new_products"] += 1
        pending[chain].pop(pid, None)

    if all_pages_ok and baseline is None:
        save_baseline(chain, set(entries))
    if all_pages_ok:
        apply_missing(chain, set(entries), today, st)
    state["menu_count"] = len(entries)
    state["checked_at"] = today
    state["releases"] = stored
    state["release_checked"] = sorted(checked)
    save_json(freshness_state_path(), state, sort_keys=True)

    # 発売日の確定 (この回に処理したプレスリリースとの照合)。終わって久しい商品は探し直さない
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=14)).isoformat()
    for p in load_chain_products(chain):
        if p.get("status") == "ended" and (p.get("ended_observed_at") or "") < cutoff:
            continue
        if freshness_apply_release(p, infos):
            save_product(chain, p)
    recorded = [p for p in load_chain_products(chain) if not p.get("is_regular")]
    st["news_matched"] = sum(1 for p in recorded if p.get("news_url"))
    st["news_unmatched"] = sum(1 for p in recorded if not p.get("news_url"))


# ---------------------------------------------------------------------------
# 丸亀製麺 (jp.marugame.com)
# ---------------------------------------------------------------------------
# 調査・方針(2026-10-17。実物の構造は parsers.py の「丸亀製麺」のコメントにまとめた):
#  ・巡回は robots.txt + カテゴリ一覧5ページ(udon を入口に、そのナビから他のカテゴリを毎回拾う) + 新しく記録する商品の詳細ページ。
#    一覧のJSON(__NEXT_DATA__)に名前・価格(サイズ別)・限定の印・冒頭の説明が入っているので、詳細ページは栄養とアレルゲンのためだけに取る。
#  ・記録するのは (a) 期間限定の印(icon_select / attributes)のある商品、(b) baseline(初回巡回の全メニュー)に無く後から現れた商品。
#    「新定番」(期間限定から定番に加わった印)は記録の対象にしない。記録済みの商品が新定番になっても記録は残す(badges に足すだけ)。
#  ・商品IDは「カテゴリ-slug」(slug はカテゴリをまたいで重複する)。日付・連番は含まれない。
#  ・発売日は公式に無いので launch_date は null のまま。「記録開始日」(初めて見つけた日)から表示する。CMSの更新日時は使わない。
#  ・存在しないパスは HTTP 403 のソフト404。一覧ページの403はブロックとして止まるが、詳細ページの403は
#    「取得できない」として pending に残して続行する(連続 MARUGAME_DETAIL_BLOCK_STREAK 回続いたらブロックとみなして止まる)。
#  ・エネルギー検算(kcal ≈ 4P+9F+4C)は公式のJSON値には採用ゲートとして使わない。外れたら警告を出し、値は保存する。
#  ・説明文は冒頭の1文だけ(公式の description の最初の1文。無ければ lead)。画像は保存も表示もしない。
MARUGAME_BASE = parsers.MARUGAME_BASE
MARUGAME_MIN_RATIO = 0.5
MARUGAME_DETAIL_BLOCK_STREAK = 3
MARUGAME_NO_REGULAR_CATEGORIES = ("topping",)  # 定番として記録しないカテゴリ(トッピング)
MARUGAME_NUTRITION_NOTE = "栄養成分値は商品のレシピに基づく推定値で、店内の手づくりのため実際の商品と差が出ることがあります"


def marugame_state_path() -> Path:
    return DATA / "marugame_state.json"


def marugame_price_note(entry: dict) -> str:
    prices = entry.get("prices") or []
    primary = parsers.marugame_pick_primary_price(prices)
    note = "税込。"
    if len(prices) > 1 and primary and primary["label"]:
        note += f"表示は「{primary['label']}」の価格。"
    note += "店舗により販売メニューや価格が異なる場合があります。"
    return note


def marugame_description(entry: dict) -> tuple[str | None, int]:
    short, full_len = parsers.shorten_description(entry.get("description"))
    if short is None:
        short, full_len = parsers.shorten_description(entry.get("lead"))
    return short, full_len


def marugame_energy_mismatches(variants: list[dict]) -> list[str]:
    bad = []
    for v in variants:
        n = v["nutrition"]
        if None in (n.get("kcal"), n.get("protein_g"), n.get("fat_g"), n.get("carbs_g")):
            continue
        if not pdf_update.pn.energy_ok(n["kcal"], n["protein_g"], n["fat_g"], n["carbs_g"]):
            bad.append(v["label"] or "標準")
    return bad


def build_marugame_product(entry: dict, detail: dict | None, category_name: str | None, same_name: list[str],
                           today: str, now: str) -> dict:
    kind = "期間限定" if entry["is_limited"] else "新規掲載"
    variants = (detail or {}).get("nutrition_variants") or []
    primary_nut = parsers.marugame_pick_primary_nutrition(variants)
    short, full_len = marugame_description(entry)
    price = entry.get("price")
    prices = [{"label": p["label"], "price": p["price"]} for p in entry.get("prices") or []]
    product = {
        "chain": "marugame",
        "product_id": entry["product_id"],
        "slug": entry.get("slug"),
        "category_id": entry.get("category_id"),
        "name": entry["name"],
        "category": category_name,
        "price": price,
        "price_note": marugame_price_note(entry),
        "price_variants": prices if len(prices) > 1 else None,
        "launch_date": None,
        "launch_date_source": "not_found",
        "announced_at": None,
        "planned_end_text": None,
        "description": short,
        "description_full_len": full_len,
        "nutrition": primary_nut["nutrition"] if primary_nut else None,
        "nutrition_variants": ([{"label": v["label"], "nutrition": v["nutrition"]} for v in variants]
                               if len(variants) > 1 else None),
        "nutrition_note": MARUGAME_NUTRITION_NOTE if variants else None,
        "allergens": (detail or {}).get("allergens"),
        "origin": None,
        "limited_kind": kind,
        "badges": entry["badges"],
        "channels": entry["channels"],
        "takeout_only": entry["takeout_only"],
        "official_url": entry["official_url"],
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [{"date": today, "price": price}] if price is not None else [],
        "source_list": [f"menu/{entry['category_id']}"],
        "image": None,
    }
    seed_variants(product, today)
    if same_name:
        product["same_name_as"] = same_name
    return product


def marugame_slug(product_id: str | None, slug: str | None = None) -> str | None:
    """商品の slug。保存されていなければ「カテゴリ-slug」の最初の「-」以降から補う(2026-10-17の既存3件は slug を持たない)。"""
    if slug:
        return slug
    if product_id and "-" in product_id:
        return product_id.split("-", 1)[1] or None
    return None


def marugame_category_id(product: dict) -> str | None:
    """記録済み商品の現在のカテゴリID。保存されていなければ product_id の先頭(最初に記録したときのカテゴリ)。"""
    if product.get("category_id"):
        return product["category_id"]
    pid = product.get("product_id") or ""
    return pid.split("-", 1)[0] if "-" in pid else None


def marugame_resolve_moves(entries: dict, recorded: list[dict], baseline: set[str] | None):
    """公式が商品を別カテゴリへ移したとき(商品ID「カテゴリ-slug」が変わる)に、新規と終了の二重記録を防ぐ。純粋関数。

    entries: {今日の一覧の商品ID: 項目}。recorded: 記録済みの商品。baseline: 基準線のID集合(None可。書き換えない)。
    今日の一覧に無い商品ID(記録済み or 基準線)と、同じ slug で今日の一覧に新しく現れた商品IDが
    1対1で対応するときだけ「移動した」とみなす。同じ slug の記録済み商品が今日の一覧にも出ている(mentaiko のような
    真の重複・移動元と移動先に同時に出る)ときは別商品として扱い、何も付け替えない。
    戻り値: (キーを記録済みの商品IDに付け替えた entries, 付け替えた {今日のID: 記録済みID},
             基準線の定番が移動したとみなす今日のID集合)。
    """
    baseline = baseline or set()
    recorded_by_pid = {p["product_id"]: p for p in recorded}
    # 今日の一覧に新しく現れた(記録済みでも基準線でもない)ID を slug ごとに
    fresh: dict[str, list[str]] = {}
    for pid, e in entries.items():
        if pid in recorded_by_pid or pid in baseline:
            continue
        slug = marugame_slug(pid, e.get("slug"))
        if slug:
            fresh.setdefault(slug, []).append(pid)

    def vanished(pids) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for pid in pids:
            if pid in entries:
                continue
            slug = marugame_slug(pid, (recorded_by_pid.get(pid) or {}).get("slug"))
            if slug:
                out.setdefault(slug, []).append(pid)
        return out

    renames: dict[str, str] = {}
    gone_recorded = vanished(recorded_by_pid)
    for slug, news in fresh.items():
        if len(news) == 1 and len(gone_recorded.get(slug, [])) == 1:
            renames[news[0]] = gone_recorded[slug][0]
    # 記録済みに当たらなかったものは、基準線の定番が移動したのか見る
    gone_baseline = vanished(p for p in baseline if p not in recorded_by_pid)
    baseline_moved = set()
    for slug, news in fresh.items():
        news = [n for n in news if n not in renames]
        if len(news) == 1 and len(gone_baseline.get(slug, [])) == 1 and len(fresh[slug]) == 1:
            baseline_moved.add(news[0])
    out = {}
    for pid, e in entries.items():
        if pid in renames:
            out[renames[pid]] = dict(e, product_id=renames[pid], moved_from_pid=pid)
        else:
            out[pid] = e
    return out, renames, baseline_moved


def marugame_apply_move(existing: dict, e: dict, new_category_name: str | None, today: str) -> bool:
    """記録済み商品のカテゴリが変わっていたら、カテゴリ・URLを更新して履歴を1件残す(IDは変えない)。変えたら True。"""
    old = marugame_category_id(existing)
    new = e.get("category_id")
    if not new or new == old:
        return False
    existing.setdefault("category_history", []).append({
        "date": today, "from_category_id": old, "to_category_id": new,
        "from_category": existing.get("category"), "to_category": new_category_name,
    })
    existing["category_id"] = new
    existing["slug"] = marugame_slug(existing.get("product_id"), e.get("slug") or existing.get("slug"))
    if new_category_name:
        existing["category"] = new_category_name
    return True


def crawl_marugame(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = "marugame"
    # 日付を指定した実行(テスト)では、初回観測の時刻もその日にそろえる
    now = now_iso() if today is None else f"{today}T07:00:00+09:00"
    today = today or today_jst()
    pending.setdefault(chain, {})
    stats.setdefault(chain, new_stats())
    st = stats[chain]
    all_ok = True

    robots_text = None
    try:
        robots_text = getter(chain, MARUGAME_BASE + "/robots.txt")
        st["list_requests"] += 1
    except NotFoundError:
        st["list_requests"] += 1
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] {chain} robots.txt: {e}", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {chain} robots.txt 取得失敗(全許可として続行): {e}", file=sys.stderr)
    rp = urllib.robotparser.RobotFileParser()
    rp.parse((robots_text or "User-agent: *\nAllow: /").splitlines())

    def allowed(path: str) -> bool:
        return rp.can_fetch(UA, MARUGAME_BASE + path)

    def get_list(cat: str) -> dict | None:
        """カテゴリ一覧を1ページ取る。取れない・商品が0件(構造変更の疑い)のときは None(終了判定を止める)。"""
        path = parsers.marugame_menu_path(cat)
        if not allowed(path):
            print(f"[skip] robots.txtでDisallow: {path}", file=sys.stderr)
            return None
        try:
            page = getter(chain, MARUGAME_BASE + path)
            st["list_requests"] += 1
        except BlockedError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {chain} 一覧 {path} 取得失敗: {e}", file=sys.stderr)
            return None
        parsed = parsers.parse_marugame_list(page)
        if not parsed["items"]:
            print(f"[warn] {chain} 一覧 {path}: 商品が0件(構造変更の疑い)", file=sys.stderr)
            return None
        return parsed

    # 入口(udon)のナビから他のカテゴリを拾う。新しいカテゴリが増えても拾える
    pages: dict[str, dict] = {}
    cat_names: dict[str, str] = {}
    try:
        entry_page = get_list(parsers.MARUGAME_ENTRY_CATEGORY)
        cats = [parsers.MARUGAME_ENTRY_CATEGORY]
        if entry_page is not None:
            pages[parsers.MARUGAME_ENTRY_CATEGORY] = entry_page
            cat_names.update({c["id"]: c["name"] for c in entry_page["nav"]})
            if entry_page["category"]:
                cat_names[entry_page["category"]["id"]] = entry_page["category"]["name"]
            cats += [c["id"] for c in entry_page["nav"] if c["id"] != parsers.MARUGAME_ENTRY_CATEGORY]
        else:
            all_ok = False
        if len(cats) == 1:  # ナビが読めなかった: 設定の固定リストで代替(この回は終了判定をしない)
            cats += [c for c in CONFIG["marugame_categories"] if c != parsers.MARUGAME_ENTRY_CATEGORY]
            all_ok = False
        for cat in cats:
            if cat in pages:
                continue
            parsed = get_list(cat)
            if parsed is None:
                all_ok = False
                continue
            pages[cat] = parsed
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] {chain} list: {e}", file=sys.stderr)
        return

    entries: dict[str, dict] = {}
    for cat, parsed in pages.items():
        for e in parsed["items"]:
            cur = entries.get(e["product_id"])
            if cur is None:
                entries[e["product_id"]] = e
                continue
            cur["is_limited"] = cur["is_limited"] or e["is_limited"]
            cur["is_new_classic"] = cur["is_new_classic"] or e["is_new_classic"]
            cur["badges"] += [b for b in e["badges"] if b not in cur["badges"]]
            cur["channels"] = [c for c in ("店内", "お持ち帰り") if c in cur["channels"] or c in e["channels"]]
            if cur["price"] is None:
                cur["price"], cur["prices"] = e["price"], e["prices"]

    state = load_json(marugame_state_path(), {})
    prev_count = (state.get(chain) or {}).get("menu_count")
    if entries and prev_count and len(entries) < prev_count * MARUGAME_MIN_RATIO:
        print(f"[warn] {chain} メニューが極端に少ない({len(entries)}件。前回{prev_count}件)。"
              "消えた商品の判定は行わない", file=sys.stderr)
        all_ok = False
    st["list_ok"] = all_ok and bool(entries)
    st["limited_seen"] = sum(1 for e in entries.values() if e["is_limited"])

    baseline = baseline_for(chain)
    # 商品が別カテゴリへ移ったときの付け替え。一覧5ページが全部読めた回だけ(読めないページに旧IDが
    # 隠れていると、真の重複を移動と取り違えるため)。基準線のファイルは書き換えず、この回の判定にだけ足す
    if all_ok and entries:
        entries, moves, baseline_moved = marugame_resolve_moves(entries, load_chain_products(chain), baseline)
        for new_pid, old_pid in moves.items():
            print(f"[info] {chain} カテゴリ移動とみなす: {old_pid} -> {new_pid}", file=sys.stderr)
        if baseline is not None:
            baseline = baseline | baseline_moved
    entries = burst_filter(chain, entries, baseline, pending, stats, today)
    existing_all = None
    detail_block_streak = 0
    regular_budget = RegularBudget(st)
    for pid, e in entries.items():
        existing = load_product(chain, pid)
        if existing is not None:
            marugame_apply_move(existing, e, cat_names.get(e.get("category_id")), today)
            was_ended = existing.get("status") == "ended"
            old_ended = existing.get("ended_observed_at")
            ref = yoshinoya_season_ref(existing)
            touch_seen(existing, list_price_for_history(st, e.get("price")), today, now, st)
            # サイズ別の価格は一覧のJSONに入っているので、毎日追加のリクエストなしで履歴を更新できる
            if st["list_ok"]:
                record_variants(existing, [{"label": p["label"], "price": p["price"]} for p in e.get("prices") or []],
                                today, st)
            if was_ended:
                yoshinoya_start_new_season(existing, old_ended, today)
                existing["seasons"][-1]["observed_start"] = ref  # 発売日が無いので、昨季は記録開始日で残す
                st["relaunched"] = st.get("relaunched", 0) + 1
            if e["is_limited"] and existing.get("limited_kind") != "期間限定":
                existing["limited_kind"] = "期間限定"
            existing["badges"] = list(dict.fromkeys((existing.get("badges") or []) + e["badges"]))
            existing["channels"] = e["channels"]
            existing["takeout_only"] = e["takeout_only"]
            existing["official_url"] = e["official_url"]
            save_product(chain, existing)
            pending[chain].pop(pid, None)
            continue
        is_new_menu = baseline is not None and pid not in baseline
        is_regular = not (e["is_limited"] or is_new_menu)
        if is_regular and (e.get("category_id") in MARUGAME_NO_REGULAR_CATEGORIES
                           or (e.get("category_id") is None and pid.split("-", 1)[0] in MARUGAME_NO_REGULAR_CATEGORIES)):
            continue  # トッピングは料理ではないので、定番としては記録しない
        if is_regular and not regular_budget.take():
            continue  # 定番は詳細を取るので、1回の巡回で作る件数に上限がある。残りは翌日以降
        if not allowed(e["href"]):
            print(f"[skip] robots.txtでDisallow: {e['href']}", file=sys.stderr)
            continue
        try:
            html = getter(chain, MARUGAME_BASE + e["href"])
            st["detail_requests"] += 1
            detail_block_streak = 0
        except NotFoundError:
            st["failed"] += 1
            pending[chain][pid] = {"url": e["href"], "reason": "404", "checked_at": now}
            continue
        except BlockedError as ex:
            # 存在しないパスは403を返す(ソフト404)。詳細1件の403は「取得できない」として続行し、連続したらブロックとみなす
            st["failed"] += 1
            detail_block_streak += 1
            pending[chain][pid] = {"url": e["href"], "reason": f"unavailable: {ex}", "checked_at": now}
            if detail_block_streak >= MARUGAME_DETAIL_BLOCK_STREAK:
                st["blocked"] = True
                print(f"[blocked] {chain} detail が{detail_block_streak}回連続で拒否: {ex}", file=sys.stderr)
                break
            continue
        except Exception as ex:  # noqa: BLE001
            st["failed"] += 1
            pending[chain][pid] = {"url": e["href"], "reason": f"fetch_error: {ex}", "checked_at": now}
            continue
        detail = parsers.parse_marugame_detail(html)
        if detail is None:
            st["failed"] += 1
            pending[chain][pid] = {"url": e["href"], "reason": "empty_detail", "checked_at": now}
            continue
        bad = marugame_energy_mismatches(detail["nutrition_variants"])
        if bad:
            st["energy_mismatch"] = st.get("energy_mismatch", 0) + 1
            print(f"[warn] {chain} {pid}: エネルギーが 4P+9F+4C と合わない({'・'.join(bad)})。公式の値のまま保存", file=sys.stderr)
        if existing_all is None:
            existing_all = load_chain_products(chain)
        norm = parsers.normalize_for_match(e["name"])
        same = [p["product_id"] for p in existing_all
                if p["product_id"] != pid and parsers.normalize_for_match(p.get("name")) == norm]
        product = build_marugame_product(e, detail, cat_names.get(e["category_id"]), same, today, now)
        if is_regular:
            mark_regular(product)
        save_product(chain, product)
        existing_all.append(product)
        st["regular_created" if is_regular else "new_products"] += 1
        pending[chain].pop(pid, None)

    if st["list_ok"] and not st["blocked"]:
        if baseline is None:
            save_baseline(chain, set(entries))
        apply_missing(chain, set(entries), today, st)
        state[chain] = {"menu_count": len(entries), "checked_at": today}
        save_json(marugame_state_path(), state, sort_keys=True)


# ---------------------------------------------------------------------------
# バーガーキング (www.burgerking.co.jp。画面が使う内部API(POST)。仕様は parsers.py のバーガーキングの節)
# ---------------------------------------------------------------------------
#  ・1回の巡回は4リクエスト: メニュー(期間限定で絞り込み)・ニュース・イベント(開催中)・イベント(終了済み)。
#    robots.txt には実体が無い(SPAのトップが返る)ので取らない。新しく記録した「セットのある」商品だけ、詳細(BKJ0510)を
#    1回の巡回あたり BK_DETAIL_PER_RUN 件まで足す(セット価格の内訳のため。残りは翌日以降)。
#  ・記録するのは「期間限定」で絞り込んだ一覧に居る商品だけ(定番は記録しない。NEWの印は全商品で空なので使わない)。
#    説明文・画像・栄養は扱わない(規約が複製・転載を禁じている)。
#  ・発売日は公式のAPIに無い(ニュースのPDFの画像の中)ので launch_date は null。ニュースの公開日を告知日 announced_at に入れる。
#  ・終了日: イベントの dtEnd が商品名と確実にひも付く(parsers.find_bk_event_end)ときだけ official_end_date に入れる。
#    それ以外は他と同じ「別の日に連続2回、一覧から消えた」で ended にする。
#  ・一覧が空・形式不正・取得失敗・項目数が前回の半分未満の回は何も更新しない(BK_MIN_RATIO)。ブロック(403・チャレンジ)は止まって通知する。
BK_CHAIN = "burgerking"
BK_MIN_RATIO = 0.5
BK_DETAIL_PER_RUN = 6
BK_MENU_BODY = {"menuKeywordList": [{"menuKeyword": parsers.BK_LIMITED_KEYWORD}], "callChn": "W"}
BK_NEWS_BODY = {"noticeCategory": "A", "noticePageNum": "1", "noticePageSize": "30"}


def bk_state_path() -> Path:
    return DATA / "burgerking_state.json"


def bk_event_body(status: str) -> dict:
    return {"cdTypeEvent": "01", "tpStatusEvent": status, "page": "1", "pageCount": "30"}


def build_bk_product(entry: dict, today: str, now: str) -> dict:
    price = entry.get("price")
    return {
        "chain": BK_CHAIN,
        "product_id": entry["product_id"],
        "name": entry["name"],
        "category": entry.get("category"),
        "category_cd": entry.get("category_cd"),
        "price": price,
        "price_note": "税込。公式サイトのメニューに表示された価格。",
        "price_variants": None,
        "has_sets": bool(entry.get("has_sets")),
        "detail_checked": False,
        "launch_date": None,
        "launch_date_source": "not_found",
        "announced_at": None,
        "planned_end_text": None,
        "official_end_date": None,
        "official_end_title": None,
        "official_end_url": None,
        "news_url": None,
        "news_title": None,
        "news_date": None,
        "description": None,
        "description_full_len": 0,
        "nutrition": None,
        "allergens": None,
        "origin": None,
        "limited_kind": "期間限定",
        "official_url": entry["official_url"],
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [{"date": today, "price": price}] if price is not None else [],
        "source_list": ["menu"],
        "image": None,
    }


def bk_start_new_season(product: dict, old_ended: str | None, today: str) -> None:
    """終了後に同じIDが一覧へ戻ってきた。昨季の期間を seasons に残し、今季の告知日・終了日は未確定に戻す。"""
    old_end = product.get("official_end_date")
    rel = product.get("relaunches") or []  # 今回の再登場は touch_seen が追記済み。昨季の起点はその1つ前(無ければ初回)
    observed = rel[-2]["reappeared_at"] if len(rel) >= 2 else (product.get("first_seen_at") or "")[:10]
    yoshinoya_start_new_season(product, old_ended, today)
    product["seasons"][-1]["observed_start"] = observed
    if old_end:
        product["seasons"][-1]["official_end_date"] = old_end
    product["announced_at"] = None
    for k in ("planned_end_text", "official_end_date", "official_end_title", "official_end_url"):
        product[k] = None


def bk_planned_end_text(end: str, status: str | None, today: str) -> str:
    text = f"{end}（公式イベントの終了日）"
    if status == "on_sale" and end < today:
        text = f"{end}（公式イベントの終了日。その後も公式の一覧には載っています）"
    return text


def bk_apply_news_and_events(product: dict, news: list[dict] | None, events: list[dict] | None, today: str) -> bool:
    """告知日(ニュース)と終了日(イベント)をひも付ける。取得済みの一覧を使うので追加のリクエストは無い。変更があれば True。
    ニュース・イベントの取得に失敗した回(None)は、その分を触らない。終了日は、同名の開催中イベントが現れたら取り消す。"""
    changed = False
    ref = (product.get("first_seen_at") or "")[:10]
    rel = product.get("relaunches") or []
    if rel and rel[-1].get("reappeared_at"):
        ref = rel[-1]["reappeared_at"]
    name = product.get("name") or ""
    if news and ref and not product.get("news_url"):
        hit = parsers.match_bk_news(name, news, ref)
        if hit:
            product.update({"announced_at": hit["date"], "news_url": hit["url"], "news_title": hit["title"],
                            "news_date": hit["date"]})
            changed = True
    if events is not None and ref:
        if parsers.has_open_bk_event(name, events):
            if product.get("official_end_date"):
                for k in ("planned_end_text", "official_end_date", "official_end_title", "official_end_url"):
                    product[k] = None
                changed = True
        else:
            ev = parsers.find_bk_event_end(name, events, ref)
            if ev:
                text = bk_planned_end_text(ev["end"], product.get("status"), today)
                if product.get("official_end_date") != ev["end"] or product.get("planned_end_text") != text:
                    product.update({"official_end_date": ev["end"], "official_end_title": ev["title"],
                                    "official_end_url": ev["url"], "planned_end_text": text})
                    changed = True
    return changed


def crawl_burgerking(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = BK_CHAIN
    now = now_iso() if today is None else f"{today}T07:00:00+09:00"
    today = today or today_jst()
    pending.setdefault(chain, {})
    stats.setdefault(chain, new_stats())
    st = stats[chain]

    def post(trcode: str, body: dict) -> str:
        return getter.post_json(chain, trcode, body)

    # メニュー(期間限定で絞り込み)。取れなければ何も更新しない
    entries: dict[str, dict] = {}
    all_ok = True
    try:
        text = post("BKJ0508", BK_MENU_BODY)
        st["list_requests"] += 1
        entries = {e["product_id"]: e for e in parsers.parse_bk_menu(text)}
        if not entries:
            print(f"[warn] {chain} メニュー一覧: 期間限定の商品が0件(構造変更の疑い)", file=sys.stderr)
            all_ok = False
    except BlockedError as e:
        st["blocked"] = True
        print(f"[blocked] {chain} menu: {e}", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {chain} メニュー一覧 取得失敗: {e}", file=sys.stderr)
        all_ok = False
    state = load_json(bk_state_path(), {})
    prev_count = state.get("menu_count")
    if entries and prev_count and len(entries) < prev_count * BK_MIN_RATIO:
        print(f"[warn] {chain} メニュー一覧が極端に少ない({len(entries)}件。前回{prev_count}件)。"
              "消えた商品の判定は行わない", file=sys.stderr)
        all_ok = False
    st["list_ok"] = all_ok and bool(entries)
    st["limited_seen"] = len(entries)

    # ニュース・イベント(開催中・終了済み)。失敗しても一覧の更新は続ける(告知日・終了日が付かないだけ)
    def side(trcode: str, body: dict, parse):
        try:
            res = parse(post(trcode, body))
            st["detail_requests"] += 1
            return res
        except BlockedError as e:
            st["blocked"] = True
            print(f"[blocked] {chain} {trcode}: {e}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {chain} {trcode} 取得失敗: {e}", file=sys.stderr)
        return None

    news = side("BKJ0506", BK_NEWS_BODY, parsers.parse_bk_news)
    events = None
    if not st["blocked"]:
        open_ev = side("BKJ0501", bk_event_body("C"), parsers.parse_bk_events)
        end_ev = side("BKJ0501", bk_event_body("E"), parsers.parse_bk_events) if not st["blocked"] else None
        if open_ev is not None and end_ev is not None:  # 片方だけでは「開催中ではない」と言い切れないので、両方取れた回だけ使う
            events = open_ev + end_ev
    if st["blocked"]:
        return
    if news is not None and not news:
        print(f"[warn] {chain} ニュース: 0件(構造変更の疑い)", file=sys.stderr)
    if events is not None and not events:
        print(f"[warn] {chain} イベント: 0件(構造変更の疑い)", file=sys.stderr)
        events = None

    baseline = baseline_for(chain)
    entries = burst_filter(chain, entries, baseline, pending, stats, today)
    new_ids: list[str] = []
    for pid, e in entries.items():
        existing = load_product(chain, pid)
        if existing is not None:
            was_ended = existing.get("status") == "ended"
            old_ended = existing.get("ended_observed_at")
            touch_seen(existing, e.get("price"), today, now, st)
            if was_ended:
                bk_start_new_season(existing, old_ended, today)
                st["relaunched"] = st.get("relaunched", 0) + 1
            if e.get("category") and existing.get("category") != e["category"]:
                existing["category"], existing["category_cd"] = e["category"], e.get("category_cd")
                existing["official_url"] = e["official_url"]
            existing["has_sets"] = bool(e.get("has_sets"))
            save_product(chain, existing)
            pending[chain].pop(pid, None)
            continue
        product = build_bk_product(e, today, now)
        save_product(chain, product)
        new_ids.append(pid)
        st["new_products"] += 1
        pending[chain].pop(pid, None)

    if st["list_ok"]:
        if baseline is None:
            save_baseline(chain, set(entries))
        apply_missing(chain, set(entries), today, st)
        save_json(bk_state_path(), {"menu_count": len(entries), "checked_at": today}, sort_keys=True)

    # セット価格の内訳(詳細1件=1リクエスト)。セットのある商品だけ、1回の巡回で BK_DETAIL_PER_RUN 件まで
    if st["list_ok"]:
        todo = [p for p in load_chain_products(chain)
                if p.get("status") == "on_sale" and p.get("has_sets") and not p.get("detail_checked")]
        for p in todo[:BK_DETAIL_PER_RUN]:
            try:
                text = post("BKJ0510", {"menuCd": p["product_id"], "menuCategoryCd": p.get("category_cd") or "X000000002"})
                st["detail_requests"] += 1
                d = parsers.parse_bk_detail(text, p["product_id"])
            except BlockedError as e:
                st["blocked"] = True
                print(f"[blocked] {chain} detail: {e}", file=sys.stderr)
                break
            except Exception as e:  # noqa: BLE001
                st["failed"] += 1
                print(f"[warn] {chain} 詳細 取得失敗 {p['product_id']}: {e}", file=sys.stderr)
                continue
            p["price_variants"] = d["price_variants"]
            p["detail_checked"] = True
            save_product(chain, p)

    # 告知日・終了日のひも付け(追加のリクエストは無い)。終わって久しい商品は探し直さない
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=14)).isoformat()
    for p in load_chain_products(chain):
        if p.get("status") == "ended" and (p.get("ended_observed_at") or "") < cutoff:
            continue
        if bk_apply_news_and_events(p, news, events, today):
            save_product(chain, p)
    recorded = load_chain_products(chain)
    st["news_matched"] = sum(1 for p in recorded if p.get("news_url"))
    st["event_end_matched"] = sum(1 for p in recorded if p.get("official_end_date"))


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------

def run_pdf_step(chain: str, getter, pending: dict, stats: dict) -> dict | None:
    """吉野家・すき家・フレッシュネス・ゼッテリア・CoCo壱番屋の公式PDFから栄養・アレルゲンを取り込む(巡回のあとに1回)。

    PDFを読めない(pdfminer未導入・取得失敗・構造変更の疑い)ときも、警告とpending.jsonへの記録だけで
    クロール全体は止めない。取得器がPDF用のメソッドを持たないとき(テスト)・一覧の取得でブロックされた
    ときは何もしない。
    """
    st = stats.get(chain)
    if st is None or st.get("blocked") or not hasattr(getter, "pdf_head"):
        return None
    cfg = CONFIG.get(f"{chain}_pdf") or {}
    if not cfg.get("nutrition"):
        return None
    labels = {"nutrition": cfg.get("nutrition_label") or "", "allergen": cfg.get("allergen_label") or ""}
    state = load_json(PDF_STATE_PATH, {})
    products = load_chain_products(chain)
    try:
        report = pdf_update.update_chain(chain, products, getter, state, pending, cfg, labels,
                                         today_jst(), now_iso())
    except Exception as e:  # noqa: BLE001  PDFの失敗でクロール全体を止めない
        print(f"[warn] {chain} のPDF取り込みで例外: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    for p in report["changed_products"]:
        save_product(chain, p)
    save_json(PDF_STATE_PATH, state, sort_keys=True)
    st["pdf"] = {k: report[k] for k in ("head_requests", "get_requests", "matched", "unmatched", "aborted",
                                        "skipped", "blocked")}
    st["pdf"]["changed"] = len(report["changed"])
    return report


def collect_problems(stats: dict[str, dict]) -> dict[str, list[str]]:
    """この実行で問題があったチェーンを種別ごとに集める(通知用)。

    list_failed は「一覧の取得・解析が完全には成功しなかった」チェーン。取得失敗・商品0件(構造変更の疑い)・
    ナビが読めない・丸亀の件数急減・robots.txtで弾いた一覧など、list_ok が False で終わった経路すべてを含む。
    ただし、ブロック(blocked)・例外(errors)・歯止め(suspected_restructure。既存の「保留」通知がある)は
    重複させないため除く。詳細ページ単発の失敗(stats["failed"])は一時的なことが多く翌日に自動で再試行されるので、
    問題には数えない。"""
    return {
        "blocked": [c for c, s in stats.items() if s.get("blocked")],
        "list_failed": [c for c, s in stats.items()
                        if not s.get("list_ok") and not s.get("blocked") and not s.get("error")
                        and not s.get("suspected_restructure")],
        "errors": [c for c, s in stats.items() if s.get("error")],
    }


def update_health(pending: dict, stats: dict[str, dict], today: str) -> dict[str, int]:
    """チェーンごとの「連続で問題が出ている日数」を pending["_health"] に持ち、今回問題があった
    チェーンの日数を返す。日付で数えるので同じ日の再実行では増えず、1日でも空けば数え直す。
    今回巡回しなかったチェーン(チェーン指定の実行)は触らない。歯止めの保留は別に consecutive_days を持つ。"""
    health = pending.setdefault("_health", {})
    bad = {c for names in collect_problems(stats).values() for c in names}
    yesterday = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
    streaks: dict[str, int] = {}
    for chain in stats:
        if chain not in bad:
            health.pop(chain, None)
            continue
        prev = health.get(chain) or {}
        n = int(prev.get("days", 0))
        if prev.get("last_date") != today:
            n = n + 1 if prev.get("last_date") == yesterday else 1
        health[chain] = {"days": n, "last_date": today}
        streaks[chain] = n
    if not health:
        pending.pop("_health", None)
    return streaks


def save_run_summary(stats: dict[str, dict], streaks: dict[str, int] | None = None) -> None:
    """この実行で新規に記録した商品数・終了を観測した商品数・問題があったチェーンを書き出す。
    notify.py が読む。

    日付(UTC/JSTのずれ)で数えると朝の実行で通知が出ない・手動実行で二重に通知が出る
    不具合が姉妹サイト(コンビニポチ)で起きたため、実行ごとに上書きする一時ファイル
    (gitには入れない)にする。

    定番の新規作成(regular)は「新メニュー」(new)に混ぜない。価格の変更(price_changes)は定番も含めて数える。

    形式: {"new": {...}, "regular": {...}, "price_changes": {...}, "ended": {...}, "suspected": {...},
           "blocked": [チェーン], "list_failed": [チェーン], "errors": [チェーン],
           "streaks": {チェーン: 連続日数}, "detail_failed": {チェーン: 詳細取得失敗件数}}
    """
    out = {
        "new": {chain: s.get("new_products", 0) for chain, s in stats.items()},
        "regular": {chain: s.get("regular_created", 0) for chain, s in stats.items()},
        "price_changes": {chain: s.get("price_changes", 0) for chain, s in stats.items()},
        "ended": {chain: s.get("ended", 0) for chain, s in stats.items()},
        "suspected": {chain: s["suspected_restructure"] for chain, s in stats.items()
                      if s.get("suspected_restructure")},
    }
    problems = collect_problems(stats)
    out.update(problems)
    out["streaks"] = streaks or {}
    listed = set(problems["list_failed"]) | set(problems["blocked"]) | set(problems["errors"])
    out["detail_failed"] = {c: s["failed"] for c, s in stats.items() if s.get("failed") and c not in listed}
    RUN_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    RUN_SUMMARY.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


CRAWLERS = {"mcdonalds": crawl_mcdonalds, "mos": crawl_mos, "matsuya": crawl_matsuya,
            "matsunoya": crawl_matsunoya, "yoshinoya": crawl_yoshinoya, "sukiya": crawl_sukiya,
            "katsuya": crawl_katsuya, "karayama": crawl_karayama,
            "marugame": crawl_marugame, "burgerking": crawl_burgerking,
            "kourakuen": crawl_kourakuen, "hidakaya": crawl_hidakaya,
            "zetteria": crawl_zetteria, "kfc": crawl_kfc, "yayoiken": crawl_yayoiken,
            "cocoichi": crawl_cocoichi, "hanamaru": crawl_hanamaru, "ootoya": crawl_ootoya,
            "freshness": crawl_freshness}


def main(argv: list[str] | None = None) -> int:
    # 引数でチェーンを指定すると、そのチェーンだけ巡回する(例: crawl.py matsuya)。
    # 引数なしは全チェーン(Actionsの定期実行)
    argv = sys.argv[1:] if argv is None else argv
    unknown = [a for a in argv if a not in CRAWLERS]
    if unknown:
        print(f"不明なチェーン: {unknown} (指定できるのは {list(CRAWLERS)})", file=sys.stderr)
        return 2
    targets = [CRAWLERS[a] for a in argv] if argv else list(CRAWLERS.values())
    pending = load_json(PENDING_PATH, {})
    stats: dict[str, dict] = {}
    getter = Getter()
    started = time.monotonic()

    for fn in targets:
        try:
            fn(getter, pending, stats)
        except Exception as e:  # noqa: BLE001
            print(f"[error] {fn.__name__} で例外: {e}", file=sys.stderr)
            err_chain = {v: k for k, v in CRAWLERS.items()}.get(fn)
            if err_chain:
                stats.setdefault(err_chain, new_stats())["error"] = f"{type(e).__name__}: {e}"
        chain = {v: k for k, v in CRAWLERS.items()}.get(fn)
        if chain in ("yoshinoya", "sukiya", "freshness", "zetteria", "cocoichi"):
            run_pdf_step(chain, getter, pending, stats)

    streaks = update_health(pending, stats, today_jst())
    save_json(PENDING_PATH, pending, sort_keys=True)
    save_run_summary(stats, streaks)

    print("=== クロール結果 ===")
    for chain, s in stats.items():
        print(
            f"{chain}: 一覧等{s.get('list_requests', 0)}件 / 詳細{s.get('detail_requests', 0)}件 "
            f"(リクエスト計{getter.counts.get(chain, 0)}) / 限定として一覧に居た{s.get('limited_seen', 0)}件 "
            f"/ 新規{s.get('new_products', 0)}件 / 定番の記録{s.get('regular_created', 0)}件"
            f"(持ち越し{s.get('regular_deferred', 0)}件) / 価格の変更{s.get('price_changes', 0)}件 / 終了観測{s.get('ended', 0)}件 / 失敗{s.get('failed', 0)}件 "
            f"/ 一覧取得={'全部成功' if s.get('list_ok') else '一部失敗'} "
            f"/ ブロック={'あり' if s.get('blocked') else 'なし'}"
            + (f" / 例外={s['error']}" if s.get("error") else "")
        )
        if s.get("pdf"):
            print(f"  公式PDF: HEAD{s['pdf']['head_requests']}件・GET{s['pdf']['get_requests']}件 / "
                  f"照合{s['pdf']['matched']}件・更新{s['pdf']['changed']}件 / 栄養が入らなかった商品{len(s['pdf']['unmatched'])}件"
                  + (f" / 中止: {s['pdf']['aborted']}" if s['pdf']['aborted'] else "")
                  + (f" / 飛ばした: {s['pdf']['skipped']}" if s['pdf']['skipped'] else ""))
    print(f"所要時間: {time.monotonic() - started:.0f}秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
