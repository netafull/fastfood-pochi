#!/usr/bin/env python3
"""マクドナルド(HTML)・モスバーガー(JSON)の公式データのパーサ。

ネットワークには触らない純粋関数だけを置く(tests/fixtures のスナップショットで
開発・テストするため)。公式のHTML/JSONの構造が変わったら、まずここが壊れる。
"""

from __future__ import annotations

import html as html_lib
import json
import re
import unicodedata

# ---------------------------------------------------------------------------
# 共通
# ---------------------------------------------------------------------------

DESCRIPTION_MAX = 100


def clean_text(s: str | None) -> str:
    """HTMLタグ(<br>は改行扱い)を除き、実体参照を戻し、空白を整える。"""
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_lib.unescape(s)
    s = s.replace(" ", " ")
    s = re.sub(r"[ \t　]+", " ", s)
    s = re.sub(r"\s*\n\s*", "\n", s)
    return s.strip()


def shorten_description(text: str | None, limit: int = DESCRIPTION_MAX) -> tuple[str | None, int]:
    """公式の説明文は全文を保存・表示しない(複製・転載の禁止に配慮した、コグレとの
    取り決め)。最初の1文(「。」まで)を最大 limit 文字で返す。

    先頭の【…】(販売期間の注記)や「※」始まりの行は説明文ではないので飛ばす。
    戻り値は (短縮した文, 元の全文の文字数)。説明文が無ければ (None, 0)。
    """
    full = clean_text(text)
    if not full:
        return None, 0
    body = full
    # 先頭の【…】ブロックは期間の注記なので取り除く
    body = re.sub(r"^(?:【[^】]*】\s*)+", "", body).strip()
    lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("※") and not re.match(r"^【[^】]*】", ln)]
    if not lines:
        return None, len(full)
    first_para = lines[0]
    m = re.search(r"。", first_para)
    sentence = first_para[: m.end()] if m else first_para
    if len(sentence) > limit:
        sentence = sentence[: limit - 1].rstrip() + "…"
    return sentence, len(full)


def to_number(text: str | None) -> float | None:
    """「401kcal」「21.0g」「5g」「0」→数値。「-」「－」や空は None(未分析)。"""
    if text is None:
        return None
    t = unicodedata.normalize("NFKC", str(text)).strip().replace(",", "")
    if t in ("", "-", "ー", "−"):
        return None
    m = re.match(r"^(-?\d+(?:\.\d+)?)", t)
    return float(m.group(1)) if m else None


def split_unit(text: str | None) -> str:
    """数量文字列の単位部分(g, kcal, mg, μg …)。"""
    t = unicodedata.normalize("NFKC", str(text or "")).strip()
    m = re.match(r"^-?\d+(?:\.\d+)?\s*(.*)$", t)
    return m.group(1).strip() if m else ""


# 栄養成分の主要項目(日本語ラベル→キー)。マクドナルドとモスで共通に使う
NUTRIENT_KEYS = {
    "エネルギー": "kcal",
    "熱量": "kcal",
    "たんぱく質": "protein_g",
    "脂質": "fat_g",
    "炭水化物": "carbs_g",
    "糖類": "sugar_g",
    "食物繊維": "fiber_g",
    "食塩相当量": "salt_g",
}


def build_nutrition(rows: list[tuple[str, str]]) -> dict | None:
    """(ラベル, 数量文字列) の並びから nutrition 辞書を作る。

    主要項目は数値のキー(kcal, protein_g, fat_g, carbs_g, sugar_g, fiber_g, salt_g)、
    それ以外は other に「ラベル→原文の数量」で入れる。「-」(未分析)は None。
    行が1つも無ければ None(セット商品など、栄養が載らない商品)。
    """
    if not rows:
        return None
    out: dict = {k: None for k in ("kcal", "protein_g", "fat_g", "carbs_g", "sugar_g", "fiber_g", "salt_g")}
    other: dict[str, str] = {}
    for label, qty in rows:
        label = unicodedata.normalize("NFKC", label).strip()
        key = NUTRIENT_KEYS.get(label)
        if key:
            out[key] = to_number(qty)
        else:
            other[label] = unicodedata.normalize("NFKC", str(qty)).strip()
    out["other"] = other
    return out


# ---------------------------------------------------------------------------
# マクドナルド
# ---------------------------------------------------------------------------

MCD_BASE = "https://www.mcdonalds.co.jp"
_CARD_SPLIT = "<div class='product-list-card "
_JST_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# セット商品はバーガー等の組み合わせで、限定バーガーの「セット」は本体と二重になる。
# 栄養も載らないことが多いので記録の対象外にする(名前で判定)
MCD_SET_RE = re.compile(r"セット|コンビ$|募金付き")


def is_mcd_set(name: str) -> bool:
    return bool(MCD_SET_RE.search(unicodedata.normalize("NFKC", name)))


def parse_mcd_list(page: str) -> list[dict]:
    """/menu/ やカテゴリページの商品カードを返す。

    data-product-code は詳細ページ /products/{code}/ の code と一致する(2026-10-07に
    実物で確認。deeplink 内の code=NNNN は別の値で、画像パス用なので使わない)。

    期間限定の判定: バッジ画像(limit_badge.svg)に started-at 属性が付いている、または
    バッジが hidden クラスで隠されていないもの。普通の商品もバッジのimg自体は持っていて
    hidden で隠れているため、data-time-limited-offer='true' の有無だけでは判定できない。
    """
    entries = []
    for card in page.split(_CARD_SPLIT)[1:]:
        m = re.search(r"data-product-code='([^']+)'", card)
        if not m:
            continue
        code = m.group(1)
        name_m = re.search(r"product-list-card-name[^>]*>([^<]*)<", card)
        name = clean_text(name_m.group(1)) if name_m else ""
        badge = re.search(r"<img src='/assets/images/limit_badge\.svg'([^>]*)>", card)
        battrs = badge.group(1) if badge else ""
        started = re.search(r"data-time-limited-offer-started-at='([^']*)'", battrs)
        started_date = None
        if started:
            dm = _JST_DATE_RE.search(started.group(1))
            started_date = dm.group(1) if dm else None
        limited = bool(started) or bool(badge and " hidden" not in battrs)
        price = None
        pm = re.search(r"product-list-card-price-number[^>]*>\s*([\d,]+)", card)
        if pm:
            price = int(pm.group(1).replace(",", ""))
        else:
            dm2 = re.search(r"mcdonaldsjp://product\?[^']*?price=(\d+)", card)
            if dm2:
                price = int(dm2.group(1))
        entries.append({
            "product_id": code,
            "name": name,
            "price": price,
            "limited": limited,
            "list_started_at": started_date,
            "detail_url": f"/products/{code}/",
        })
    return entries


def parse_mcd_detail(page: str) -> dict | None:
    """/products/{code}/ から説明文・価格・栄養・アレルゲン・原産地・受付期間を取る。
    h1 が無ければ(エラーページ等) None。"""
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    if not h1:
        return None
    name = clean_text(h1.group(1))
    desc = None
    dm = re.search(r"</h1>\s*<p[^>]*>(.*?)</p>", page, re.S)
    if dm:
        desc = clean_text(dm.group(1)) or None
    price = None
    price_label = None
    pm = re.search(
        r"pdp__product-info-quantity'>([^<]*)</span>.*?product-section-price-primary-val'>([^<]*)<",
        page, re.S)
    if not pm:
        pm2 = re.search(r"product-section-price-primary-val'>([^<]*)<", page)
        raw = pm2.group(1) if pm2 else None
    else:
        price_label = clean_text(pm.group(1)) or None
        raw = pm.group(2)
    if raw:
        price = to_number(raw)
        price = int(price) if price is not None else None
    mop_start = mop_end = None
    mm = re.search(
        r"data-mop-availability-started-at='([^']*)'\s+data-mop-availability-ended-at='([^']*)'", page)
    if mm:
        s = _JST_DATE_RE.search(mm.group(1))
        e = _JST_DATE_RE.search(mm.group(2))
        mop_start = s.group(1) if s else None
        mop_end = e.group(1) if e else None

    # 栄養: id=product-nutrients-block の ul の中。親(飽和脂肪酸など)子の関係は
    # class で表されるが、ここでは項目名と値の並びだけを使う
    nutrition = None
    nb = _block(page, "product-nutrients-block")
    if nb:
        rows = re.findall(
            r"<li class='[^']*'>.*?font-semibold'>([^<]*)</span>.*?<span class='p-4:md-1px block'>([^<]*)</span>",
            nb, re.S)
        nutrition = build_nutrition([(clean_text(k), clean_text(v)) for k, v in rows])

    # アレルゲン: ●(solid-circle)=含む、×(x-type)=含まない
    contains: list[str] = []
    seen_any = False
    for block_id in ("product-allergensSpecifiedByLaw-block", "product-allergensSpecifiedRecommended-block"):
        ab = _block(page, block_id)
        if not ab:
            continue
        for label, mark in re.findall(r"font-semibold'>([^<]*)</span>.*?(pdp-mark_[a-z-]+)", ab, re.S):
            seen_any = True
            if mark == "pdp-mark_solid-circle":
                contains.append(clean_text(label))
    allergens = {"contains": contains, "partial": []} if seen_any else None

    origin = []
    ob = _block(page, "product-countryoforigin-block")
    if ob:
        for item in re.findall(r"<li class='pdp__material-item[^>]*>(.*?)</li>", ob, re.S):
            ps = re.findall(r"<p[^>]*>(.*?)</p>", item, re.S)
            if len(ps) >= 2:
                text = clean_text(ps[1]).replace("\n", "　")
                origin.append({"name": clean_text(ps[0]), "origin": text})

    return {
        "name": name,
        "description": desc,
        "price": price,
        "price_label": price_label,
        "mop_start": mop_start,
        "mop_end": mop_end,
        "nutrition": nutrition,
        "allergens": allergens,
        "origin": origin or None,
    }


def _block(page: str, element_id: str) -> str | None:
    i = page.find(f"id='{element_id}'")
    if i < 0:
        return None
    j = page.find("</ul>", i)
    return page[i: j if j > 0 else len(page)]


# ---------------------------------------------------------------------------
# モスバーガー
# ---------------------------------------------------------------------------

MOS_BASE = "https://www.mos.jp"
# mst_icon.json のアイコンID → 限定の種類
MOS_LIMITED_ICONS = {"1": "期間限定", "2": "季節限定", "4": "地域限定", "5": "店舗限定", "14": "数量限定"}
LIMITED_CATEGORY_ID = "26"

_MOS_PERIOD_RE = re.compile(r"【\s*(\d{4})年(\d{1,2})月(\d{1,2})日[^〜~】]*[〜~]\s*([^】]*?)】")


def parse_mos_period(explain: str | None) -> tuple[str | None, str | None]:
    """explain の「【2026年9月9日（水）〜2026年11月上旬頃までの販売予定】」から
    (発売日ISO, 終了予定の原文)を取る。見つからなければ (None, None)。
    終了予定は「の販売予定」を除いた形(例「2026年11月上旬頃まで」)。"""
    if not explain:
        return None, None
    text = unicodedata.normalize("NFKC", clean_text(explain))
    m = _MOS_PERIOD_RE.search(text)
    if not m:
        return None, None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    launch = f"{y:04d}-{mo:02d}-{d:02d}"
    end_text = m.group(4).strip()
    end_text = re.sub(r"の?販売予定$", "", end_text).strip() or None
    return launch, end_text


def parse_mos_allergy_map(text: str) -> dict[str, str]:
    """mst_allergy.json → {allergy id: 品目名}。"""
    data = json.loads(text)
    return {str(a["id"]): a["name"] for a in data}


def parse_mos_list(text: str, category_id: str) -> list[dict]:
    """/data/menu/menu_category/{id}.json の1件ごとを、記録に使う形へ整える。

    この関数は一覧に載っている情報だけを使う(詳細ページは取らない)。
    """
    data = json.loads(text)
    entries = []
    for x in data:
        pid = str(x.get("id") or "").strip()
        if not pid:
            continue
        tags = [t.strip() for t in str(x.get("tags") or "").split(",") if t.strip()]
        kinds = [MOS_LIMITED_ICONS[t] for t in tags if t in MOS_LIMITED_ICONS]
        entries.append({
            "product_id": pid,
            "name": clean_text(x.get("name")),
            "price": x.get("price") if isinstance(x.get("price"), int) else None,
            "category_id": category_id,
            "limited_kinds": kinds,
            "explain": x.get("explain"),
            "nutrition": x.get("nutrition") or [],
            "allergy": x.get("allergy") or [],
            "produce": x.get("produce") or [],
            "kcal": x.get("kcal"),
        })
    return entries


def mos_nutrition(entry: dict) -> dict | None:
    rows = [(n.get("name") or "", n.get("quantity") or "") for n in entry.get("nutrition") or []]
    nut = build_nutrition(rows)
    if nut is None:
        return None
    if nut.get("kcal") is None:
        nut["kcal"] = to_number(entry.get("kcal"))
    return nut


def mos_allergens(entry: dict, allergy_map: dict[str, str]) -> dict | None:
    """level 5(●)は「含む」。それ以外の記号(△▽◇)は公式の凡例が必要な
    「条件つき」の表示なので partial に記号つきで残す。level 1(－)は含まない。
    allergy が空(栄養が載らない商品)なら None。"""
    rows = entry.get("allergy") or []
    if not rows:
        return None
    contains, partial = [], []
    for a in rows:
        name = allergy_map.get(str(a.get("id")), f"品目{a.get('id')}")
        level = a.get("level")
        mark = a.get("name") or ""
        if level == 5:
            contains.append(name)
        elif level != 1 and mark and mark != "－":
            partial.append({"name": name, "mark": mark})
    return {"contains": contains, "partial": partial}


def mos_origin(entry: dict) -> list[dict] | None:
    out = []
    for p in entry.get("produce") or []:
        group = clean_text(p.get("group"))
        name = clean_text(p.get("name"))
        label = f"{group}：{name}" if group and name else (group or name)
        out.append({"name": label, "origin": clean_text(p.get("origin"))})
    return out or None
