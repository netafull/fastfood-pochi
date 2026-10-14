#!/usr/bin/env python3
"""マクドナルド・モスバーガー・松屋・松のや・吉野家・すき家・かつや・からやまの限定メニュー・新メニューを巡回し、
data/products/{mcdonalds|mos|matsuya|matsunoya|yoshinoya|sukiya|katsuya|karayama}/{id}.json に1商品1ファイルで蓄積する。

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
from fetch import BlockedError, NotFoundError, fetch, fetch_binary  # noqa: E402
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

    # 公式PDF(吉野家・すき家の栄養成分)用。HEAD(版の確認)とGET(本体)。どちらも同じ間隔・上限・カウントに従う
    def pdf_head(self, chain: str, url: str) -> dict:
        self._tick(chain)
        return fetch_binary(url, UA, method="HEAD")[1]

    def pdf_get(self, chain: str, url: str) -> bytes:
        self._tick(chain)
        return fetch_binary(url, UA)[0]


# ---------------------------------------------------------------------------
# 販売状況の追跡(チェーン共通)
# ---------------------------------------------------------------------------

def touch_seen(product: dict, entry_price: int | None, today: str, now: str) -> bool:
    """一覧に居た既存商品の追跡項目を更新する。変更があれば True。

    ・last_seen_at を更新、status=on_sale、見失った回数をリセット
    ・終了後に再登場したら on_sale に戻し、relaunches に記録
    ・一覧の価格が前回と違えば price_history に追記
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

    hist = product.setdefault("price_history", [])
    if entry_price is not None:
        last_price = hist[-1]["price"] if hist else None
        if last_price != entry_price:
            hist.append({"date": today, "price": entry_price})
            product["price"] = entry_price
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
            "failed": 0, "blocked": False, "list_ok": False, "limited_seen": 0}


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
    for pid, e in singles.items():
        existing = load_product(chain, pid)
        if existing is not None:
            if touch_seen(existing, e.get("price"), today, now):
                existing["source_list"] = sorted(set(existing.get("source_list") or []) | set(sources[pid]))
                save_product(chain, existing)
            pending[chain].pop(pid, None)
            continue
        is_new_menu = baseline is not None and pid not in baseline
        if not (e["limited"] or is_new_menu):
            continue  # 定番の既存メニューは記録しない(baselineに入るだけ)
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
        save_product(chain, product)
        st["new_products"] += 1
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
    for pid, e in entries.items():
        cats = categories[pid]
        in_limited = parsers.LIMITED_CATEGORY_ID in cats
        limited = in_limited or bool(e["limited_kinds"])
        if limited:
            st["limited_seen"] += 1
        existing = load_product(chain, pid)
        if existing is not None:
            touch_seen(existing, e.get("price"), today, now)
            save_product(chain, existing)
            continue
        is_new_menu = baseline is not None and pid not in baseline
        if not (limited or is_new_menu):
            continue
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
        save_product(chain, product)
        st["new_products"] += 1

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
    try:
        for pid, e in singles.items():
            existing = load_product(chain, pid)
            if existing is not None:
                was_ended = existing.get("status") == "ended"
                old_ended = existing.get("ended_observed_at")
                touch_seen(existing, e.get("price"), today, now)
                if was_ended:
                    # 日付抜きの基本キーなので、本当に終了した商品の再登場は同じレコードに戻ってくる。
                    # 昨季の期間は seasons に残し(relaunches は touch_seen が記録済み)、今季の発売日は未確定に戻す
                    yoshinoya_start_new_season(existing, old_ended, today)
                    st["relaunched"] = st.get("relaunched", 0) + 1
                existing["source_list"] = sorted(set(existing.get("source_list") or []) | set(sources[pid]))
                if limited_cat in sources[pid]:
                    existing["in_recommended"] = True
                # 公式の日付つきURLは更新で変わる。リンクは常に最新に保つ(識別は日付抜きの基本キー)
                existing["current_slug"] = e["current_slug"]
                existing["official_url"] = MATSUYA_BASE + e["detail_path"]
                save_product(chain, existing)
                pending[chain].pop(pid, None)
                continue
            in_limited = limited_cat in sources[pid]
            only_unrecorded = all(c in no_record for c in sources[pid])
            is_new_menu = baseline is not None and pid not in baseline
            if not (in_limited or (is_new_menu and not only_unrecorded)):
                continue  # 定番の既存メニュー、トッピング・セット類は記録しない(baselineに入るだけ)
            detail_path = e["detail_path"]
            if not allowed(detail_path):
                print(f"[skip] robots.txtでDisallow: {detail_path}", file=sys.stderr)
                continue
            try:
                html = getter(chain, MATSUYA_BASE + detail_path)
                st["detail_requests"] += 1
            except NotFoundError:
                st["failed"] += 1
                pending[chain][pid] = {"url": detail_path, "reason": "404", "checked_at": now}
                continue
            except BlockedError:
                raise
            except Exception as ex:  # noqa: BLE001
                st["failed"] += 1
                pending[chain][pid] = {"url": detail_path, "reason": f"fetch_error: {ex}", "checked_at": now}
                continue
            detail = parsers.parse_matsuya_detail(html)
            if detail is None:
                st["failed"] += 1
                pending[chain][pid] = {"url": detail_path, "reason": "empty_detail", "checked_at": now}
                continue
            name = e["name"] or detail["name"]
            resolved = news.resolve(name, e["current_slug"], today)
            if existing_all is None:
                existing_all = load_chain_products(chain)
            norm = parsers.normalize_for_match(name)
            same = [p["product_id"] for p in existing_all
                    if p["product_id"] != pid and parsers.normalize_for_match(p.get("name")) == norm]
            product = build_matsuya_product(e, detail, sources[pid], in_limited, resolved, same, today, now, chain)
            save_product(chain, product)
            existing_all.append(product)
            st["new_products"] += 1
            if resolved is None:
                st["news_unmatched"] = st.get("news_unmatched", 0) + 1
            else:
                st["news_matched"] = st.get("news_matched", 0) + 1
            pending[chain].pop(pid, None)
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
        "name": entry["name"] or detail["name"],
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
    try:
        for pid, e in entries.items():
            existing = load_product(chain, pid)
            if existing is not None:
                was_ended = existing.get("status") == "ended"
                old_ended = existing.get("ended_observed_at")
                touch_seen(existing, e.get("price"), today, now)
                if was_ended:
                    yoshinoya_start_new_season(existing, old_ended, today)
                    st["relaunched"] = st.get("relaunched", 0) + 1
                existing["source_list"] = sorted(set(existing.get("source_list") or []) | set(sources[pid]))
                if limited_cat in sources[pid]:
                    existing["in_limited_list"] = True
                if flags[pid]["limited"] and existing.get("limited_kind") != "期間限定":
                    existing["limited_kind"] = "期間限定"
                badges = set(existing.get("badges") or [])
                badges |= {b for b, on in (("NEW", flags[pid]["new"]), ("期間限定", flags[pid]["limited"])) if on}
                existing["badges"] = [b for b in ("NEW", "期間限定") if b in badges]
                save_product(chain, existing)
                pending[chain].pop(pid, None)
                continue
            in_limited = limited_cat in sources[pid]
            is_new_menu = baseline is not None and pid not in baseline
            if not (in_limited or is_new_menu):
                continue  # 定番の既存メニューは記録しない(baselineに入るだけ)
            detail_path = e["detail_path"]
            if not allowed(detail_path):
                print(f"[skip] robots.txtでDisallow: {detail_path}", file=sys.stderr)
                continue
            try:
                html = getter(chain, YOSHINOYA_BASE + detail_path)
                st["detail_requests"] += 1
            except NotFoundError:
                st["failed"] += 1
                pending[chain][pid] = {"url": detail_path, "reason": "404", "checked_at": now}
                continue
            except BlockedError:
                raise
            except Exception as ex:  # noqa: BLE001
                st["failed"] += 1
                pending[chain][pid] = {"url": detail_path, "reason": f"fetch_error: {ex}", "checked_at": now}
                continue
            detail = parsers.parse_yoshinoya_detail(html)
            if detail is None:
                st["failed"] += 1
                pending[chain][pid] = {"url": detail_path, "reason": "empty_detail", "checked_at": now}
                continue
            if existing_all is None:
                existing_all = load_chain_products(chain)
            norm = parsers.normalize_for_match(e["name"] or detail["name"])
            same = [p["product_id"] for p in existing_all
                    if p["product_id"] != pid and parsers.normalize_for_match(p.get("name")) == norm]
            product = build_yoshinoya_product(e, detail, sources[pid], flags[pid], same, labels, today, now)
            save_product(chain, product)
            existing_all.append(product)
            st["new_products"] += 1
            pending[chain].pop(pid, None)

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
#  ・価格が変わったかは詳細を取り直さないと分からないので、記録済みの販売中商品を古い順に
#    SUKIYA_PRICE_RECHECK_MAX 件まで、SUKIYA_PRICE_RECHECK_DAYS 日ごとに取り直す。
#  ・終了日は公式が「販売終了時期は未定」とするため、一覧から消えたのを確認した日。

SUKIYA_BASE = parsers.SUKIYA_BASE
SUKIYA_PRICE_RECHECK_DAYS = 7
SUKIYA_PRICE_RECHECK_MAX = 10


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
    if product.get("launch_date") is not None:
        return False
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
        "price_history": [{"date": today, "price": price}],
        "source_list": [source],
        "image": None,
    }
    if same_name:
        product["same_name_as"] = same_name
    return product


def sukiya_recheck_price(product: dict, detail: dict, today: str) -> bool:
    """詳細を取り直した価格を反映する。価格(代表または内訳)が変われば price_history に追記する。"""
    variants = parsers.sukiya_price_variants(detail)
    primary = parsers.sukiya_pick_primary(detail)
    product["price_checked_at"] = today
    new_pv = variants if len(variants) > 1 else None
    changed = (product.get("price") != primary["price"]) or (product.get("price_variants") != new_pv)
    if changed:
        product["price_variants"] = new_pv
        product["price_note"] = sukiya_price_note(variants, primary)
        if product.get("price") != primary["price"]:
            product["price"] = primary["price"]
            product.setdefault("price_history", []).append({"date": today, "price": primary["price"]})
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

    try:
        for pid, e in entries.items():
            existing = load_product(chain, pid)
            if existing is not None:
                was_ended = existing.get("status") == "ended"
                old_ended = existing.get("ended_observed_at")
                touch_seen(existing, None, today, now)
                if was_ended:
                    yoshinoya_start_new_season(existing, old_ended, today)
                    st["relaunched"] = st.get("relaunched", 0) + 1
                if e["is_limited"] and existing.get("limited_kind") != "期間限定":
                    existing["limited_kind"] = "期間限定"
                badges = set(existing.get("badges") or [])
                badges |= {b for b, on in (("NEW", e["is_new"]), ("期間限定", e["is_limited"]),
                                           ("店舗限定", e["is_store_limited"])) if on}
                existing["badges"] = [b for b in ("NEW", "期間限定", "店舗限定") if b in badges]
                save_product(chain, existing)
                pending[chain].pop(pid, None)
                continue
            in_news = bool(news) and parsers.match_sukiya_launch(e["name"], news, today) is not None
            is_new_menu = baseline is not None and pid not in baseline
            if not (e["is_limited"] or e["is_new"] or in_news or is_new_menu):
                continue  # 定番の既存メニューは記録しない(baselineに入るだけ)
            detail = fetch_detail(pid, e["detail_path"])
            if detail is None:
                continue
            if existing_all is None:
                existing_all = load_chain_products(chain)
            norm = parsers.normalize_for_match(e["name"] or detail["name"])
            same = [p["product_id"] for p in existing_all
                    if p["product_id"] != pid and parsers.normalize_for_match(p.get("name")) == norm]
            product = build_sukiya_product(e, detail, source_of[pid], same, labels, today, now)
            sukiya_apply_news(product, news)
            save_product(chain, product)
            existing_all.append(product)
            st["new_products"] += 1
            pending[chain].pop(pid, None)

        # 記録済みの販売中商品の価格を、古い順に少数だけ取り直す(価格改定の追跡)
        if st["list_ok"]:
            if baseline is None:
                save_baseline(chain, set(entries))
            apply_missing(chain, set(entries), today, st)
            cutoff_price = (datetime.date.fromisoformat(today)
                            - datetime.timedelta(days=SUKIYA_PRICE_RECHECK_DAYS)).isoformat()
            due = sorted((p for p in load_chain_products(chain)
                          if p.get("status") == "on_sale" and p["product_id"] in entries
                          and (p.get("price_checked_at") or "") <= cutoff_price),
                         key=lambda p: p.get("price_checked_at") or "")
            for p in due[:SUKIYA_PRICE_RECHECK_MAX]:
                detail = fetch_detail(p["product_id"], entries[p["product_id"]]["detail_path"])
                if detail is not None:
                    sukiya_recheck_price(p, detail, today)
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
    if product.get("launch_date") is not None:
        return False
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
    for pid, e in entries.items():
        existing = load_product(chain, pid)
        if existing is not None:
            was_ended = existing.get("status") == "ended"
            old_ended = existing.get("ended_observed_at")
            touch_seen(existing, e.get("price"), today, now)
            if was_ended:
                yoshinoya_start_new_season(existing, old_ended, today)
                existing["announced_at"] = None
                st["relaunched"] = st.get("relaunched", 0) + 1
            if e["is_limited"] and existing.get("limited_kind") != "期間限定":
                existing["limited_kind"] = "期間限定"
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
        if not (e["is_limited"] or e["is_new"] or in_news or is_new_menu):
            continue  # 定番の既存メニューは記録しない(baselineに入るだけ)
        if existing_all is None:
            existing_all = load_chain_products(chain)
        norm = parsers.normalize_for_match(e["name"])
        same = [p["product_id"] for p in existing_all
                if p["product_id"] != pid and parsers.normalize_for_match(p.get("name")) == norm]
        product = build_arcland_product(chain, e, allergen_pdf, same, today, now)
        arcland_apply_news(product, news)
        save_product(chain, product)
        existing_all.append(product)
        st["new_products"] += 1
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
# 実行
# ---------------------------------------------------------------------------

def run_pdf_step(chain: str, getter, pending: dict, stats: dict) -> dict | None:
    """吉野家・すき家の公式PDFから栄養・アレルゲンを取り込む(巡回のあとに1回)。

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


def save_run_summary(stats: dict[str, dict]) -> None:
    """この実行で新規に記録した商品数・終了を観測した商品数を書き出す。notify.py が読む。

    日付(UTC/JSTのずれ)で数えると朝の実行で通知が出ない・手動実行で二重に通知が出る
    不具合が姉妹サイト(コンビニポチ)で起きたため、実行ごとに上書きする一時ファイル
    (gitには入れない)にする。
    """
    out = {
        "new": {chain: s.get("new_products", 0) for chain, s in stats.items()},
        "ended": {chain: s.get("ended", 0) for chain, s in stats.items()},
        "suspected": {chain: s["suspected_restructure"] for chain, s in stats.items()
                      if s.get("suspected_restructure")},
    }
    RUN_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    RUN_SUMMARY.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


CRAWLERS = {"mcdonalds": crawl_mcdonalds, "mos": crawl_mos, "matsuya": crawl_matsuya,
            "matsunoya": crawl_matsunoya, "yoshinoya": crawl_yoshinoya, "sukiya": crawl_sukiya,
            "katsuya": crawl_katsuya, "karayama": crawl_karayama}


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
        chain = {v: k for k, v in CRAWLERS.items()}.get(fn)
        if chain in ("yoshinoya", "sukiya"):
            run_pdf_step(chain, getter, pending, stats)

    save_json(PENDING_PATH, pending, sort_keys=True)
    save_run_summary(stats)

    print("=== クロール結果 ===")
    for chain, s in stats.items():
        print(
            f"{chain}: 一覧等{s.get('list_requests', 0)}件 / 詳細{s.get('detail_requests', 0)}件 "
            f"(リクエスト計{getter.counts.get(chain, 0)}) / 限定として一覧に居た{s.get('limited_seen', 0)}件 "
            f"/ 新規{s.get('new_products', 0)}件 / 終了観測{s.get('ended', 0)}件 / 失敗{s.get('failed', 0)}件 "
            f"/ 一覧取得={'全部成功' if s.get('list_ok') else '一部失敗'} "
            f"/ ブロック={'あり' if s.get('blocked') else 'なし'}"
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
