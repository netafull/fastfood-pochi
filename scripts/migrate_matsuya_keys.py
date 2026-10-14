#!/usr/bin/env python3
"""松屋・松のやの識別キーを、URLスラッグ末尾の日付(_YYMMDD)抜きの「基本キー」へ移行する(1回限り・冪等)。

背景: 2026-10-13 に松屋の公式サイトがほぼ全商品のURLの日付を `_261013` に一斉更新し、翌朝の巡回で
定番メニューまで37件が「新規」と判定された。product_id を基本キーにして根本を直す(crawl.py 側)。
このスクリプトは既存のデータを新しいキーに合わせる。

やること(松屋・松のや):
  1. data/products/{chain}/*.json を基本キーに改名。同じ基本キーのレコードが複数あれば1件に統合する
     (first_seen_at・launch_date・news_*・price_history は古いほう、official_url・current_slug・価格・栄養などの
     中身は新しいslugのほう、status は on_sale 優先)。
  2. 松屋のみ: 日付更新の朝(--since 以降)に誤って「新規」として作られたレコードのうち、基本キーが baseline にあり
     (=既存の定番メニュー)、旧レコードが無く、現在「おすすめ」にも載っていないものを削除する。
  3. data/baseline_ids.json の該当チェーンを基本キーに変換(重複は1つ)。
  4. data/pending.json の該当チェーンのキーを基本キーにする(baselineにある定番の404は不要になるので消す)。

再実行しても安全(日付つきのIDが残っていなければ何もしない)。

使い方:
  python3 scripts/migrate_matsuya_keys.py [--dry-run] [--data DIR] [--since 2026-10-14]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import parsers  # noqa: E402

CHAINS = ("matsuya", "matsunoya")
DELETE_FALSE_NEW_CHAINS = ("matsuya",)
DEFAULT_SINCE = "2026-10-14"
DATE_RE = re.compile(r"_\d{6}$")

# 統合のとき、新しいslugのレコードの値で上書きする「中身」の項目
OVERLAY_KEYS = ("name", "category", "price", "price_note", "price_variants", "nutrition", "nutrition_variants",
                "nutrition_as_of", "nutrition_note", "allergens", "official_url", "current_slug")


def base_key(pid: str) -> str:
    return parsers.matsuya_base_key(pid)


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def _slug_sort_key(p: dict):
    return (parsers.matsuya_slug_date(p["product_id"]) or "", p.get("last_seen_at") or "", p.get("first_seen_at") or "")


def merge_records(group: list[dict]) -> dict:
    """同じ基本キーのレコード群を1件にまとめる。group は product_id が旧slug(日付つき)のまま。"""
    group = sorted(group, key=lambda p: p.get("first_seen_at") or "")
    older = group[0]
    newest = max(group, key=_slug_sort_key)  # 現在のURLを持つほう
    merged = json.loads(json.dumps(older))
    if len(group) > 1:
        for p in group:
            if p is older:
                continue
            for k in ("launch_date", "launch_date_source", "news_url", "news_title", "news_date", "description",
                      "description_full_len", "limited_kind"):
                # 古いほうに無い(未確定の)項目は、新しいほうの確定値で埋める
                if merged.get(k) in (None, "", "not_found", "news_without_date") and p.get(k) not in (None, ""):
                    if k == "launch_date_source" and merged.get("launch_date") is not None:
                        continue
                    merged[k] = p[k]
        for k in OVERLAY_KEYS:
            if k in newest:
                merged[k] = newest[k]
        hist = list(merged.get("price_history") or [])
        for p in sorted(group, key=lambda q: q.get("first_seen_at") or ""):
            if p is older:
                continue
            for h in p.get("price_history") or []:
                if (not hist or h["date"] > hist[-1]["date"]) and (not hist or hist[-1]["price"] != h["price"]):
                    hist.append(h)
        merged["price_history"] = hist
        merged["source_list"] = sorted({s for p in group for s in (p.get("source_list") or [])})
        merged["in_recommended"] = any(p.get("in_recommended") for p in group)
        merged["relaunches"] = [r for p in group for r in (p.get("relaunches") or [])] or merged.get("relaunches")
        seasons = [s for p in group for s in (p.get("seasons") or [])]
        if seasons:
            merged["seasons"] = seasons
        if merged["relaunches"] is None:
            merged.pop("relaunches", None)
        # 追跡状態: 最後に見えたレコードに合わせる。on_sale が1つでもあれば on_sale
        seen = max(group, key=lambda p: (p.get("last_seen_at") or "", p.get("status") == "on_sale"))
        merged["last_seen_at"] = max((p.get("last_seen_at") or "") for p in group) or merged.get("last_seen_at")
        if any(p.get("status") == "on_sale" for p in group):
            merged["status"] = "on_sale"
            merged["ended_observed_at"] = None
            merged["missed_runs"] = 0
            merged["last_miss_date"] = None
        else:
            merged["status"] = seen.get("status")
            merged["ended_observed_at"] = seen.get("ended_observed_at")
            merged["missed_runs"] = seen.get("missed_runs", 0)
            merged["last_miss_date"] = seen.get("last_miss_date")
    else:
        merged["current_slug"] = merged.get("current_slug") or older["product_id"]
    if not merged.get("current_slug"):
        merged["current_slug"] = newest["product_id"]
    if len(group) > 1 and not newest.get("current_slug"):
        merged["current_slug"] = newest["product_id"]
    return merged


def migrate(data_dir: Path, since: str = DEFAULT_SINCE, dry_run: bool = False) -> dict:
    """data_dir(= data/)を移行して、結果の報告(dict)を返す。"""
    report: dict = {"chains": {}, "baseline": {}, "pending": {}}
    baseline_path = data_dir / "baseline_ids.json"
    pending_path = data_dir / "pending.json"
    baseline = _load(baseline_path) if baseline_path.exists() else {}
    pending = _load(pending_path) if pending_path.exists() else {}

    for chain in CHAINS:
        cdir = data_dir / "products" / chain
        files = sorted(cdir.glob("*.json")) if cdir.is_dir() else []
        records = [_load(f) for f in files]
        before = len(records)
        base_ids = {base_key(i) for i in baseline.get(chain, [])}
        rep = {"before": before, "merged": [], "deleted": [], "renamed": 0, "kept_new": [], "after": 0}

        groups: dict[str, list[dict]] = {}
        for r in records:
            groups.setdefault(base_key(r["product_id"]), []).append(r)

        result: dict[str, dict] = {}
        for key, group in groups.items():
            dated = [r for r in group if DATE_RE.search(r["product_id"])]
            if not dated and len(group) == 1:
                # すでに基本キー(移行済み)。何もしない
                result[key] = group[0]
                continue
            # 誤新規の削除: 日付更新の朝に新しく作られ、旧レコードが無く、基本キーがbaselineにあり、おすすめに載っていない
            if (chain in DELETE_FALSE_NEW_CHAINS and len(group) == 1 and key in base_ids
                    and (group[0].get("first_seen_at") or "")[:10] >= since
                    and not group[0].get("in_recommended")):
                rep["deleted"].append({"old_id": group[0]["product_id"], "name": group[0].get("name")})
                continue
            if len(group) > 1:
                rep["merged"].append({"key": key, "from": sorted(r["product_id"] for r in group),
                                      "name": group[0].get("name")})
            else:
                rep["renamed"] += 1
                if chain in DELETE_FALSE_NEW_CHAINS and (group[0].get("first_seen_at") or "")[:10] >= since:
                    rep["kept_new"].append({"id": key, "name": group[0].get("name"),
                                            "reason": "baselineに無い本当の新メニュー" if key not in base_ids
                                            else "おすすめ一覧に掲載中"})
            m = merge_records(group)
            m["product_id"] = key
            result[key] = m

        # same_name_as を基本キーにそろえる(自分自身・存在しないものは落とす)
        for key, m in result.items():
            if m.get("same_name_as"):
                names = sorted({base_key(i) for i in m["same_name_as"]} - {key})
                names = [n for n in names if n in result]
                if names:
                    m["same_name_as"] = names
                else:
                    m.pop("same_name_as", None)
        rep["after"] = len(result)
        report["chains"][chain] = rep

        if not dry_run:
            for f in files:
                f.unlink()
            for key, m in result.items():
                _save(cdir / f"{key}.json", m)

        # baseline
        if chain in baseline:
            new_b = sorted({base_key(i) for i in baseline[chain]})
            report["baseline"][chain] = {"before": len(baseline[chain]), "after": len(new_b)}
            baseline[chain] = new_b

        # pending
        pc = pending.get(chain)
        if isinstance(pc, dict):
            new_p, dropped, renamed = {}, [], []
            for k, v in pc.items():
                if k.startswith("_") or k.startswith("pdf:"):
                    new_p[k] = v
                    continue
                bk = base_key(k)
                if bk in base_ids and bk in result:
                    dropped.append(k)  # すでに記録済み
                elif bk in base_ids and chain in DELETE_FALSE_NEW_CHAINS:
                    dropped.append(k)  # 定番メニュー(日付更新で新規扱いされただけ)の404は不要
                else:
                    new_p[bk] = v
                    if bk != k:
                        renamed.append(k)
            pending[chain] = new_p
            report["pending"][chain] = {"dropped": dropped, "renamed": renamed}

    if not dry_run:
        if baseline:
            _save(baseline_path, baseline)
        if pending:
            pending_path.write_text(json.dumps(pending, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    return report


def print_report(report: dict) -> None:
    for chain, rep in report["chains"].items():
        print(f"== {chain}: {rep['before']}件 → {rep['after']}件 "
              f"(統合{len(rep['merged'])}・削除{len(rep['deleted'])}・改名のみ{rep['renamed']})")
        for m in rep["merged"]:
            print(f"  統合: {m['name']}  {' + '.join(m['from'])} → {m['key']}")
        for d in rep["deleted"]:
            print(f"  削除(誤新規): {d['name']}  {d['old_id']}")
        for k in rep["kept_new"]:
            print(f"  維持(今朝の新規): {k['name']}  {k['id']}  [{k['reason']}]")
    for chain, b in report["baseline"].items():
        print(f"baseline {chain}: {b['before']} → {b['after']}")
    for chain, p in report["pending"].items():
        print(f"pending {chain}: 削除{p['dropped']} 改名{p['renamed']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--since", default=DEFAULT_SINCE)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    report = migrate(Path(a.data), a.since, a.dry_run)
    print_report(report)
    if a.dry_run:
        print("(dry-run: 何も書き換えていません)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
