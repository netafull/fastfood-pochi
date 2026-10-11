#!/usr/bin/env python3
"""吉野家・すき家の栄養成分・アレルゲンを公式PDFから商品データに反映する(取得と更新検知の側)。

PDFの読み取りそのもの(座標つきの表の解析・照合)は pdf_nutrition.py の純粋関数が行う。
ここは「いつ取得するか」「読み取った結果を、誤った値を出さないよう検証してから商品に入れる」役割。

方針:
  - PDFは毎回は取らない。HEAD(ETag・Last-Modified・Content-Length)を前回の記録と比べ、変わったときだけ
    取得して解析する。栄養が空で、まだこの版で試していない商品があるときも、1回だけ取得して試す
    (同じ版で何度も再試行しない: 商品の pdf_tried に、試した版を覚える)。
  - 1回の実行で取得(GET)するPDFは、すき家2本・吉野家1本まで。HEADは別に各1回。
  - 公式PDFの表そのものは保存しない。サイトで記録している商品に名前が完全一致した行の数値だけを、
    商品データに入れる(規約で複製・転載が禁じられているため、PDFの内容の全体を残さない)。
  - 検証ゲート: (1)エネルギー検算に外れる行は採用しない (2)見出しが見つからない・読み取れた行が極端に少ない・
    検算の通過率が低い、または照合した商品の半分以上が検算で落ちるときは、その回は何も更新せず
    (既存の値を変えない)、エラーを pending.json(と標準エラー)に残して続行する
    (3)同じ版(更新日・号数)の間は、一度入れた値を上書きしない。
  - 403・429・チャレンジ(BlockedError)になったら、回避せずその回のPDF取得を打ち切る。
  - pdfminer.six が無ければ警告を出してPDFの読み取りだけを飛ばす(クロール全体は止めない)。
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pdf_nutrition as pn
from fetch import BlockedError

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".cache" / "pdf"  # gitignore済み。開発中に同じPDFを何度も取らないための置き場(CIでは空)

SUKIYA_NOTE = ("表示した数値には、お好みでお使いいただく調味料（紅生姜・醤油・ドレッシング・七味・カルビ追いダレなど）"
               "は含まれていません。")
# ETag・Last-Modified・Content-Length のどれも返さないPDF(動的に配信される吉野家など)は、版が変わったかをHEADで
# 見分けられない。その場合は、前回の取得からこの日数が過ぎたときに取り直して、号数・日付で確かめる
NO_SIGNATURE_RECHECK_DAYS = 7
YOSHINOYA_NOTE = "栄養成分の数値は配合に基づく推定値で、お好みでお使いいただく調味料類は含まれていません。"


def have_pdfminer() -> bool:
    return importlib.util.find_spec("pdfminer") is not None


def head_signature(headers: dict | None) -> str | None:
    """HEADの応答から、PDFが変わったかを見る指紋。何も取れなければ None。"""
    if not headers:
        return None
    parts = [headers.get(k, "") for k in ("etag", "last-modified", "content-length")]
    return "|".join(parts) if any(parts) else None


def _cache_paths(cache_dir: Path, chain: str, key: str) -> tuple[Path, Path]:
    return cache_dir / f"{chain}_{key}.pdf", cache_dir / f"{chain}_{key}.sig"


def load_pdf(fetcher, chain: str, key: str, url: str, sig: str | None, cache_dir: Path) -> tuple[bytes, bool]:
    """-> (PDFのバイト列, 実際に取得したか)。キャッシュの指紋が今のHEADと同じなら再取得しない。"""
    pdf_path, sig_path = _cache_paths(cache_dir, chain, key)
    try:
        if sig and pdf_path.exists() and sig_path.read_text(encoding="utf-8") == sig:
            return pdf_path.read_bytes(), False
    except OSError:
        pass
    data = fetcher.pdf_get(chain, url)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(data)
        sig_path.write_text(sig or "", encoding="utf-8")
    except OSError:
        pass
    return data, True


def _warn(msg: str) -> None:
    print(f"[pdf] {msg}", file=sys.stderr)


def _record_error(pending: dict, chain: str, key: str, url: str, reason: str, now: str) -> None:
    pending.setdefault(chain, {})[f"pdf:{key}"] = {"url": url, "reason": reason, "checked_at": now}
    _warn(f"{chain}/{key}: {reason}")


def _clear_error(pending: dict, chain: str, key: str) -> None:
    pending.get(chain, {}).pop(f"pdf:{key}", None)


def _source_entry(state: dict, chain: str, key: str) -> dict:
    return state.setdefault(chain, {}).setdefault(key, {})


def _version_token(chain: str, state: dict) -> str:
    """商品の pdf_tried と比べる、いま記録している版の目印。"""
    s = state.get(chain, {})
    return "|".join(str(s.get(k, {}).get("version") or "") for k in sorted(s))


def _needs_attempt(product: dict, token: str) -> bool:
    """栄養またはアレルゲンが空で、まだこの版で試していない商品。"""
    incomplete = product.get("nutrition") is None or product.get("allergens") is None
    return incomplete and product.get("pdf_tried") != token


def _fetch_and_parse(spec: dict, ctx: dict, sig: str | None, report: dict):
    """1本のPDFを取得(またはキャッシュ)して解析・検証する。失敗なら None(エラーはpendingとstateに残す)。"""
    chain, key, url = ctx["chain"], spec["key"], spec["url"]
    entry = _source_entry(ctx["state"], chain, key)
    entry["last_get"] = ctx["today"]  # 取得を試みた日(成否によらず)。指紋の無いPDFの取り直しの間隔に使う
    try:
        data, fetched = load_pdf(ctx["fetcher"], chain, key, url, sig, ctx["cache_dir"])
        if fetched:
            report["get_requests"] += 1
    except BlockedError as e:
        report["blocked"] = True
        _record_error(ctx["pending"], chain, key, url, f"blocked: {e}", ctx["now"])
        return None
    except Exception as e:  # noqa: BLE001
        report["get_requests"] += 1
        _record_error(ctx["pending"], chain, key, url, f"fetch_error: {e}", ctx["now"])
        return None
    try:
        pages = ctx["extract"](data, with_chars=spec["with_chars"])
        parsed = spec["parse"](pages)
    except Exception as e:  # noqa: BLE001
        entry["failed_signature"] = sig
        _record_error(ctx["pending"], chain, key, url, f"parse_error: {type(e).__name__}: {e}", ctx["now"])
        return None
    err = pn.validate_source(spec["kind"], parsed, min_rows=ctx["min_rows"])
    if err:
        entry["failed_signature"] = sig
        _record_error(ctx["pending"], chain, key, url, f"validation: {err}", ctx["now"])
        return None
    _clear_error(ctx["pending"], chain, key)
    entry.pop("failed_signature", None)
    return parsed


def _source_meta(spec: dict, version: str, version_text: str, now: str) -> dict:
    return {"url": spec["url"], "label": spec["label"], "version": version, "version_text": version_text,
            "fetched_at": now}


def _apply_nutrition(products: list[dict], rows: list[dict], meta: dict, note: str, changed: dict,
                     report: dict, allergen_spec: dict | None) -> None:
    """名前が完全一致した商品だけに栄養(と、同じPDFにアレルゲンがあれば併せて)を入れる。ゲートつき。"""
    matched = []  # (product, rows)
    for p in products:
        rs = pn.rows_for_name(rows, p["name"])
        if rs is None:
            continue
        matched.append((p, rs))
    bad = 0
    built = []
    for p, rs in matched:
        nut, variants, dropped = pn.build_nutrition(rs)
        if nut is None or dropped:
            bad += 1
            for r in rs:
                if not r["ok"]:
                    _warn(f"{p['name']}（{r['size']}）: エネルギー検算に合わないため採用しません")
        built.append((p, rs, nut, variants))
    report["matched"] = len(matched)
    # 照合できた商品の半分以上が検算で落ちるなら、読み取り全体を疑ってこの回は何も更新しない
    if matched and bad * 2 >= len(matched):
        report["aborted"] = f"照合した{len(matched)}件のうち{bad}件が検算で落ちたため、更新しません"
        return
    for p, rs, nut, variants in built:
        if nut is None:
            continue
        src = p.get("nutrition_source") or {}
        if p.get("nutrition") is not None and src.get("version") == meta["version"]:
            continue  # 同じ版の間は上書きしない
        p["nutrition"] = nut
        p["nutrition_variants"] = variants
        p["nutrition_source"] = dict(meta)
        p["nutrition_note"] = note
        if allergen_spec is not None:
            good = [r for r in rs if r["ok"]]
            p["allergens"] = pn.allergens_from_marks(allergen_spec["names"], [r["marks"] for r in good], "○", "▲")
            p["allergens_conditional"] = {"mark": "▲", "meaning": allergen_spec["legend"].get("▲")}
        changed[p["product_id"]] = p


def _apply_sukiya_allergen(products: list[dict], parsed: dict, meta: dict, changed: dict) -> int:
    n = 0
    for p in products:
        src = p.get("allergen_source") or {}
        if p.get("allergens") is not None and src.get("version") == meta["version"]:
            continue
        al = pn.match_sukiya_allergen(parsed, p["name"])
        if al is None:
            continue
        p["allergens"] = al
        p["allergens_conditional"] = {"mark": "△", "meaning": (parsed.get("legend") or {}).get("△")}
        p["allergen_source"] = dict(meta)
        changed[p["product_id"]] = p
        n += 1
    return n


def _specs(chain: str, urls: dict, labels: dict) -> list[dict]:
    if chain == "sukiya":
        return [
            {"key": "nutrition", "kind": "sukiya_nutrition", "url": urls["nutrition"], "label": labels["nutrition"],
             "parse": pn.parse_sukiya_nutrition, "with_chars": False},
            {"key": "allergen", "kind": "sukiya_allergen", "url": urls["allergen"], "label": labels["allergen"],
             "parse": pn.parse_sukiya_allergen, "with_chars": False},
        ]
    if chain == "yoshinoya":
        return [{"key": "menu", "kind": "yoshinoya", "url": urls["nutrition"], "label": labels["nutrition"],
                 "parse": pn.parse_yoshinoya, "with_chars": True}]
    raise ValueError(chain)


def update_chain(chain: str, products: list[dict], fetcher, state: dict, pending: dict, urls: dict, labels: dict,
                 today: str, now: str, extract=None, cache_dir: Path | None = None, min_rows: int = 80) -> dict:
    """PDFの栄養・アレルゲンを商品に反映する。変わった商品は products の中身が書き換わる。

    fetcher: pdf_head(chain, url) -> ヘッダー辞書 と pdf_get(chain, url) -> bytes を持つ(crawl.Getter。テストでは偽物)。
    -> {"changed": [product_id...], "head_requests", "get_requests", "matched", "unmatched": [name...], "errors": [...],
        "blocked", "aborted", "skipped"}
    """
    report = {"changed": [], "head_requests": 0, "get_requests": 0, "matched": 0, "unmatched": [],
              "blocked": False, "aborted": None, "skipped": None, "changed_products": []}
    if extract is None:
        if not have_pdfminer():
            report["skipped"] = "pdfminer.six が未インストール"
            _warn(f"{chain}: pdfminer.six が見つからないため、PDFの読み取りを飛ばします(pip install -r requirements-pdf.txt)")
            return report
        extract = pn.extract_geometry
    ctx = {"chain": chain, "state": state, "pending": pending, "fetcher": fetcher, "now": now,
           "today": today, "extract": extract, "cache_dir": cache_dir or CACHE_DIR, "min_rows": min_rows}
    specs = _specs(chain, urls, labels)
    token = _version_token(chain, state)
    cands = [p for p in products if _needs_attempt(p, token)]

    # 1) HEADで変更を検知する
    need: list[tuple[dict, str | None]] = []
    for spec in specs:
        entry = _source_entry(state, chain, spec["key"])
        sig = None
        try:
            sig = head_signature(fetcher.pdf_head(chain, spec["url"]))
            report["head_requests"] += 1
        except BlockedError as e:
            report["blocked"] = True
            _record_error(pending, chain, spec["key"], spec["url"], f"blocked: {e}", now)
            return report
        except Exception as e:  # noqa: BLE001
            report["head_requests"] += 1
            _warn(f"{chain}/{spec['key']}: HEADに失敗(変更は検知できません): {e}")
        if sig is None:
            last = entry.get("last_get")
            due = last is None or (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(last)).days \
                >= NO_SIGNATURE_RECHECK_DAYS
            if due:
                need.append((spec, sig))
            continue
        known_bad = sig == entry.get("failed_signature")
        changed = sig != entry.get("signature")
        if known_bad:
            continue  # 前回、構造変更などで読み取れなかったのと同じファイルは取り直さない
        if changed or cands:
            need.append((spec, sig))
    if not need:
        return report

    # 2) 取得して解析・検証する
    parsed_by: dict = {}
    for spec, sig in need:
        parsed = _fetch_and_parse(spec, ctx, sig, report)
        if report["blocked"]:
            return report
        if parsed is not None:
            parsed_by[spec["key"]] = (spec, sig, parsed)

    # 3) 商品に反映する
    changed: dict = {}
    if chain == "sukiya":
        if "nutrition" in parsed_by:
            spec, sig, parsed = parsed_by["nutrition"]
            ver = parsed["updated"]
            y, m, d = ver.split("-")
            meta = _source_meta(spec, ver, f"{y}年{int(m)}月{int(d)}日更新", now)
            _apply_nutrition(products, parsed["rows"], meta, SUKIYA_NOTE, changed, report, None)
            if report["aborted"]:
                _record_error(pending, chain, spec["key"], spec["url"], f"validation: {report['aborted']}", now)
                _source_entry(state, chain, spec["key"])["failed_signature"] = sig
                parsed_by.pop("nutrition")
            else:
                _source_entry(state, chain, spec["key"]).update(signature=sig, version=ver, fetched_at=now)
        if "allergen" in parsed_by:
            spec, sig, parsed = parsed_by["allergen"]
            ver = parsed["updated"] or ""
            if ver:
                y, m, d = ver.split("-")
                text = f"{y}年{int(m)}月{int(d)}日更新"
            else:
                text = "更新日不明"
            meta = _source_meta(spec, ver, text, now)
            _apply_sukiya_allergen(products, parsed, meta, changed)
            _source_entry(state, chain, spec["key"]).update(signature=sig, version=ver, fetched_at=now)
    else:
        if "menu" in parsed_by:
            spec, sig, parsed = parsed_by["menu"]
            ver = f"{parsed['issue']}|{parsed['date']}"
            y, m, d = parsed["date"].split("-")
            meta = _source_meta(spec, ver, f"{parsed['issue']}号・{y}年{int(m)}月{int(d)}日現在", now)
            meta["issue"] = parsed["issue"]
            _apply_nutrition(products, parsed["rows"], meta, YOSHINOYA_NOTE, changed, report,
                             {"names": parsed["allergen_names"], "legend": parsed["legend"]})
            if report["aborted"]:
                _record_error(pending, chain, spec["key"], spec["url"], f"validation: {report['aborted']}", now)
                _source_entry(state, chain, spec["key"])["failed_signature"] = sig
                parsed_by.pop("menu")
            else:
                _source_entry(state, chain, spec["key"]).update(signature=sig, version=ver, fetched_at=now)

    # 4) この版で試したことを商品に覚えさせる(同じ版での再試行を避ける)。更新に成功した回だけ
    if parsed_by:
        new_token = _version_token(chain, state)
        for p in products:
            if p.get("nutrition") is None or p.get("allergens") is None:
                if p.get("pdf_tried") != new_token:
                    p["pdf_tried"] = new_token
                    changed[p["product_id"]] = p
                    if p.get("nutrition") is None:
                        report["unmatched"].append(p["name"])
    report["changed"] = sorted(changed)
    report["changed_products"] = list(changed.values())
    return report
