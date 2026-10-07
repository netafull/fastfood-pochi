#!/usr/bin/env python3
"""マクドナルドとモスバーガーの限定メニュー・新メニューを巡回し、
data/products/{mcdonalds|mos}/{id}.json に1商品1ファイルで蓄積する。

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
import sys
import time
import urllib.robotparser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import parsers  # noqa: E402
from fetch import BlockedError, NotFoundError, fetch  # noqa: E402

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA = ROOT / "data"
PRODUCTS = DATA / "products"
PENDING_PATH = DATA / "pending.json"
BASELINE_PATH = DATA / "baseline_ids.json"
RUN_SUMMARY = DATA / "run_summary.json"
JST = datetime.timezone(datetime.timedelta(hours=9))

UA = CONFIG.get("user_agent", "FastfoodPochi/1.0 (+https://fastfood.netaful.jp/)")
INTERVALS = CONFIG.get("request_interval_seconds", {})
MAX_REQUESTS = CONFIG.get("max_requests_per_run", {})
MCD_BASE = parsers.MCD_BASE
MOS_BASE = parsers.MOS_BASE
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

    def __call__(self, chain: str, url: str) -> str:
        limit = MAX_REQUESTS.get(chain)
        if limit is not None and self.counts.get(chain, 0) >= limit:
            raise RuntimeError(f"{chain}: 1回の実行あたりのリクエスト上限({limit})に達しました")
        self.throttle.wait(chain)
        self.counts[chain] = self.counts.get(chain, 0) + 1
        return fetch(url, UA)


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
# 実行
# ---------------------------------------------------------------------------

def save_run_summary(stats: dict[str, dict]) -> None:
    """この実行で新規に記録した商品数・終了を観測した商品数を書き出す。notify.py が読む。

    日付(UTC/JSTのずれ)で数えると朝の実行で通知が出ない・手動実行で二重に通知が出る
    不具合が姉妹サイト(コンビニポチ)で起きたため、実行ごとに上書きする一時ファイル
    (gitには入れない)にする。
    """
    out = {
        "new": {chain: s.get("new_products", 0) for chain, s in stats.items()},
        "ended": {chain: s.get("ended", 0) for chain, s in stats.items()},
    }
    RUN_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    RUN_SUMMARY.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    pending = load_json(PENDING_PATH, {})
    stats: dict[str, dict] = {}
    getter = Getter()
    started = time.monotonic()

    for fn in (crawl_mcdonalds, crawl_mos):
        try:
            fn(getter, pending, stats)
        except Exception as e:  # noqa: BLE001
            print(f"[error] {fn.__name__} で例外: {e}", file=sys.stderr)

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
    print(f"所要時間: {time.monotonic() - started:.0f}秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
