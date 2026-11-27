#!/usr/bin/env python3
"""マクドナルド(HTML)・モスバーガー(JSON)の公式データのパーサ。

ネットワークには触らない純粋関数だけを置く(tests/fixtures のスナップショットで
開発・テストするため)。公式のHTML/JSONの構造が変わったら、まずここが壊れる。
"""

from __future__ import annotations

import datetime
import hashlib
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


# ---------------------------------------------------------------------------
# 松屋 (静的HTML + ニュースJSON)
# ---------------------------------------------------------------------------

MATSUYA_BASE = "https://www.matsuyafoods.co.jp"
MATSUYA_MENU_ROOT = "/matsuya/menu/"
MATSUYA_NEWS_JSON = "/matsuya/whatsnew/data/whatsnew.json"
# 「おすすめ」一覧。公式の「NEW」「期間限定」ラベルは無く、運営の編集枠
MATSUYA_LIMITED_CATEGORY = "limited"
# 新メニューとして記録しない(セットの組み合わせ・トッピング)カテゴリ。
# 終了判定のための一覧取得には含める
MATSUYA_NO_RECORD_CATEGORIES = ("topping",)

# 松屋フーズ系のチェーン。サイト構造(メニュー一覧・詳細・ニュースJSON・記事)は同じで、
# URLの接頭辞(/{slug}/menu/ ・ /{slug}/whatsnew/…)、「おすすめ」カテゴリのslug、
# 記録しないカテゴリ、ニュース見出しで省かれる接頭辞(「松屋の」「松のやの」)だけが違う。
# チェーンのslugはURLの接頭辞と一致させる。
MATSUYA_FAMILY = {
    "matsuya": {"ja": "松屋", "limited": "limited", "no_record": ("topping",)},
    "matsunoya": {"ja": "松のや", "limited": "osusume", "no_record": ()},
}


def matsuya_menu_root(brand: str = "matsuya") -> str:
    return f"/{brand}/menu/"


def matsuya_news_json_path(brand: str = "matsuya") -> str:
    return f"/{brand}/whatsnew/data/whatsnew.json"
# 「生玉子＜選べるサイド＞」のようなランチ・朝定食の選択肢は商品として記録しない
MATSUYA_OPTION_RE = re.compile(r"＜選べる")


def _strip_html_comments(page: str) -> str:
    return re.sub(r"<!--.*?-->", "", page, flags=re.S)


def matsuya_normalize_path(href: str, base_url: str | None = None, brand: str = "matsuya") -> str:
    """詳細リンクを「/matsuya/menu/…」のパスにする。実物は
    `https://www.matsuyafoods.co.jp/menu/../matsuya/menu/curry/x.html` のように `/menu/../`
    を含む(2026-10-11に確認)ので、`..` を解決する。"""
    import posixpath
    from urllib.parse import urljoin, urlparse
    base_url = base_url or MATSUYA_BASE + matsuya_menu_root(brand)
    path = urlparse(urljoin(base_url, href)).path
    return posixpath.normpath(path)


MATSUYA_DATE_SUFFIX_RE = re.compile(r"_\d{6}$")


def matsuya_slug_id(path: str, brand: str = "matsuya") -> str | None:
    """`/matsuya/menu/curry/kashmir_hp_261006.html` → `curry-kashmir_hp_261006`(日付つきの現在のslug)。
    末尾の6桁は掲載日(YYMMDD)で、公式が一斉に更新することがある(2026-10-13に松屋のほぼ全商品が
    `_261013` になった)。商品の識別には使わず、official_url・current_slug・発売日の手がかりにだけ使う。"""
    root = matsuya_menu_root(brand)
    if not path.startswith(root):
        return None
    rest = path[len(root):]
    rest = re.sub(r"\.html?$", "", rest)
    if not rest or rest.endswith("index"):
        return None
    pid = re.sub(r"[^0-9A-Za-z_\-]+", "-", rest.replace("/", "-")).strip("-")
    return pid or None


def matsuya_base_key(slug_id: str) -> str:
    """日付つきslugから末尾の `_YYMMDD`(6桁)だけを取り除いた基本キー。
    `curry-kashmir_gyuu_hp_261013` → `curry-kashmir_gyuu_hp`。日付が無ければそのまま。
    カテゴリが違えば別商品(カテゴリはキーの先頭に残る)。"""
    return MATSUYA_DATE_SUFFIX_RE.sub("", slug_id) or slug_id


def matsuya_product_id(path: str, brand: str = "matsuya") -> str | None:
    """商品ID(基本キー)。ID生成はここ1箇所。"""
    sid = matsuya_slug_id(path, brand)
    return matsuya_base_key(sid) if sid else None


def matsuya_slug_date(product_id: str) -> str | None:
    """slug末尾の `_YYMMDD` を日付にする(取れなければ None)。"""
    m = re.search(r"_(\d{2})(\d{2})(\d{2})$", product_id)
    if not m:
        return None
    yy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return None
    return f"{2000 + yy:04d}-{mm:02d}-{dd:02d}"


def parse_matsuya_list(page: str, brand: str = "matsuya") -> list[dict]:
    """カテゴリ一覧の `<li class="item">` を返す。価格が無い項目(選べるサイド等)は price=None。"""
    entries = []
    for block in re.findall(r'<li class="item">(.*?)<!-- //\.item -->', page, re.S):
        hm = re.search(r'href="([^"]+)"', block)
        if not hm:
            continue
        path = matsuya_normalize_path(hm.group(1), brand=brand)
        slug = matsuya_slug_id(path, brand)
        pid = matsuya_base_key(slug) if slug else None
        if not pid:
            continue
        nm = re.search(r'<span class="txt">(.*?)</span>', block, re.S)
        name = clean_text(nm.group(1)) if nm else ""
        if not name:
            am = re.search(r'alt="([^"]*)"', block)
            name = clean_text(am.group(1)) if am else ""
        pm = re.search(r'class="price">\s*([\d,]+)\s*円', block)
        no = re.search(r"<!--\s*(\d+)\s*-->", block)
        entries.append({
            "product_id": pid,
            "current_slug": slug,
            "name": name,
            "price": int(pm.group(1).replace(",", "")) if pm else None,
            "detail_path": path,
            "product_no": no.group(1) if no else None,
            "option": bool(MATSUYA_OPTION_RE.search(name)),
        })
    return entries


def _matsuya_variant_label(raw: str | None) -> str:
    return clean_text(raw).replace("\n", "") if raw else ""


def parse_matsuya_detail(page: str) -> dict | None:
    """商品詳細から名前・価格(複数)・栄養(複数)・アレルゲン・基準日・注記を取る。
    h1 が無ければ None。

    価格と栄養は「ラベルつきの複数」になる: セット(ラベル無し)と「単品」、または
    「小盛／並盛／大盛」。ラベルの無いものは空文字のラベルで返す。
    """
    page = _strip_html_comments(page)
    h1 = re.search(r'<h1 class="ttl">(.*?)</h1>', page, re.S)
    if not h1:
        return None
    name = clean_text(h1.group(1))

    prices = []
    pb = re.search(r'<ul class="ul-text">(.*?)</ul>', page, re.S)
    if pb:
        for li in re.findall(r"<li>(.*?)</li>", pb.group(1), re.S):
            tm = re.search(r'<p class="th">(.*?)</p>', li, re.S)
            vm = re.search(r'<span class="clr">\s*([\d,]+)\s*</span>', li)
            if vm:
                prices.append({
                    "label": _matsuya_variant_label(tm.group(1)) if tm else "",
                    "price": int(vm.group(1).replace(",", "")),
                })

    nutrition_variants = []
    nb = re.search(r'<div class="nourishment">(.*?)</ul>', page, re.S)
    if nb:
        for li in re.findall(r"<li>(.*?)</li>", nb.group(1), re.S):
            lm = re.search(r'<h3 class="txt">(.*?)</h3>', li, re.S)
            pm = re.search(r"<p>(.*?)</p>", li, re.S)
            if not pm:
                continue
            rows = []
            for part in re.split(r"<br\s*/?>", pm.group(1)):
                part = clean_text(part)
                if "／" not in part:
                    continue
                label, qty = part.split("／", 1)
                label = "熱量" if label.strip() == "カロリー" else label.strip()
                rows.append((label, qty.strip()))
            nut = build_nutrition(rows)
            if nut is not None:
                nutrition_variants.append({
                    "label": _matsuya_variant_label(lm.group(1)) if lm else "",
                    "nutrition": nut,
                })

    contains, partial = [], []
    seen_any = False
    ab = re.search(r'<ul class="ul-data[^"]*">(.*?)</ul>', page, re.S)
    if ab:
        for li in re.findall(r"<li>(.*?)</li>", ab.group(1), re.S):
            tm = re.search(r'<p class="th"><span>(.*?)</span></p>', li, re.S)
            dm = re.search(r'<p class="td">(.*?)</p>', li, re.S)
            if not tm or not dm:
                continue
            seen_any = True
            label = clean_text(tm.group(1)).replace("\n", "")
            mark = clean_text(dm.group(1))
            if mark == "○":
                contains.append(label)
            elif mark and mark not in ("－", "-", "ー"):
                partial.append({"name": label, "mark": mark})
    allergens = {"contains": contains, "partial": partial} if seen_any else None

    as_of = None
    am = re.search(r"栄養成分は\s*(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日現在", page)
    if am:
        as_of = f"{int(am.group(1)):04d}-{int(am.group(2)):02d}-{int(am.group(3)):02d}"
    note = None
    nm = re.search(r"<li>※?\s*(特定原材料・栄養成分の数値には[^<]*)</li>", page)
    if nm:
        note = clean_text(nm.group(1))

    return {
        "name": name,
        "prices": prices,
        "nutrition_variants": nutrition_variants,
        "allergens": allergens,
        "nutrition_as_of": as_of,
        "nutrition_note": note,
    }


def matsuya_pick_primary(variants: list[dict]) -> dict | None:
    """表示に使う代表のバリエーション: ラベル無し(セット等)→「並盛」→先頭。
    一覧ページの価格(ラベル無し、または並盛)と一致する。"""
    if not variants:
        return None
    for v in variants:
        if not v["label"]:
            return v
    for v in variants:
        if v["label"] == "並盛":
            return v
    return variants[0]


def matsuya_variant_labels(variants: list[dict], note: str | None) -> list[dict]:
    """ラベル無しのバリエーションに表示用のラベルを付ける。「単品」と並ぶときは「セット」
    (注記に「みそ汁・福神漬も含まれ」とあればその旨も)。単独のときは空のまま。"""
    out = []
    has_single = any(v["label"] == "単品" for v in variants)
    inc = None
    if note:
        m = re.search(r"数値には、?(.+?)も含まれ", note)
        inc = m.group(1) if m else None
    for v in variants:
        v = dict(v)
        if not v["label"]:
            if has_single:
                v["label"] = "セット" + (f"（{inc}を含む）" if inc else "")
            else:
                v["label"] = ""
        out.append(v)
    return out


def normalize_for_match(s: str | None) -> str:
    """ニュース見出しと商品名の照合用: NFKC・空白と記号(「」・"など)を除く。"""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"[\s「」『』\"“”'’・＜＞<>()（）【】]+", "", s).lower()


def parse_matsuya_news(text: str) -> list[dict]:
    """whatsnew.json から category=menu の記事だけを、新しい順で返す。"""
    data = json.loads(text)
    out = []
    for x in data.get("list_whatsnew") or []:
        if x.get("category") != "menu":
            continue
        try:
            date = f"{int(x['year']):04d}-{int(x['month']):02d}-{int(x['day']):02d}"
        except (KeyError, ValueError, TypeError):
            continue
        out.append({"date": date, "title": clean_text(x.get("title")), "url": x.get("permalink") or ""})
    out.sort(key=lambda a: a["date"], reverse=True)
    return out


def matsuya_name_variants(name: str, brand: str = "matsuya") -> list[str]:
    """見出しとの照合に使う名前の候補。「松屋の」「松のやの」始まりは見出しで省かれることがある。"""
    n = normalize_for_match(name)
    cands = [n]
    prefix = MATSUYA_FAMILY[brand]["ja"] + "の"
    if n.startswith(prefix) and len(n) > len(prefix) + 3:
        cands.append(n[len(prefix):])
    return [c for c in cands if len(c) >= 4]


# 見出しとの照合で、商品の掲載日(slug末尾の日付、無ければ今日)から見て、この範囲のニュースだけ採用する。
# 「新牛めし」のように短い名前が、何年も前の別商品の見出しに偶然含まれる誤爆を避けるため
MATSUYA_NEWS_WINDOW_BEFORE_DAYS = 120
MATSUYA_NEWS_WINDOW_AFTER_DAYS = 14


def match_matsuya_news_title(name: str, news: list[dict], ref: str | None = None,
                             brand: str = "matsuya") -> dict | None:
    """商品名(正規化)を見出しに部分一致で含む最新の記事。ref(ISO日付)を渡すと、その前後の
    範囲(MATSUYA_NEWS_WINDOW_*)の記事だけを対象にする。"""
    import datetime
    lo = hi = None
    if ref:
        try:
            c = datetime.date.fromisoformat(ref)
            lo = c - datetime.timedelta(days=MATSUYA_NEWS_WINDOW_BEFORE_DAYS)
            hi = c + datetime.timedelta(days=MATSUYA_NEWS_WINDOW_AFTER_DAYS)
        except ValueError:
            pass
    cands = matsuya_name_variants(name, brand)
    for a in news:
        if lo is not None:
            try:
                d = datetime.date.fromisoformat(a["date"])
            except ValueError:
                continue
            if not (lo <= d <= hi):
                continue
        t = normalize_for_match(a["title"])
        if any(c in t for c in cands):
            return a
    return None


def matsuya_news_launch_words(title: str) -> bool:
    return bool(re.search(r"発売|販売|登場|新", title or ""))


def parse_matsuya_article(page: str) -> dict:
    """ニュース記事ページから (本文の平文, 発売日, 説明文の元) を取る。

    発売日は「【発売日】2026年10月6日（火）…」。説明文の元は、記事冒頭の見出し(h4の太字、
    商品の特徴を1〜2行で書いたもの)。無ければ「このたび…」以外の最初の段落。
    """
    page = _strip_html_comments(page)
    a = page.find('class="article-main"')
    b = page.find("</article>", a)
    body = page[a:b] if a >= 0 else page
    text = clean_text(re.sub(r"<(?:script|style)[^>]*>.*?</(?:script|style)>", "", body, flags=re.S))

    launch = None
    m = re.search(r"(?:発売日|販売開始日|販売日)[】\]\s:：]*?(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", text)
    if m:
        launch = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    desc_src = None
    for h in re.findall(r"<h4[^>]*>\s*<strong[^>]*>(.*?)</strong>\s*</h4>", body, re.S):
        h = clean_text(h)
        if h and not h.startswith("【"):
            # 見出しは <br> で折り返されている。文の途中(読点で終わる行)なら次の行とつなぐ
            desc_src = "".join(ln.strip() for ln in h.split("\n") if ln.strip())
            break
    if desc_src is None:
        for p in re.findall(r"<p[^>]*>(.*?)</p>", body, re.S):
            p = clean_text(p)
            if p and not p.startswith("このたび") and not p.startswith("※"):
                desc_src = p
                break
    return {"text": text, "launch_date": launch, "description_src": desc_src}


# ---------------------------------------------------------------------------
# 吉野家 (www.yoshinoya.com)
# ---------------------------------------------------------------------------
# 静的HTML。限定一覧 /menu/gentei/ と、入口 /menu/ からたどる全カテゴリの一覧、詳細ページ、
# お知らせ一覧 /news/{年}/ を読む。栄養成分・アレルゲンは別PDFで、このコードはPDFを取得・解析しない
# (config.json の yoshinoya_pdf にあるURLへリンクを出すだけ)。
# 規約(/sitepolicy/)は複製・転用と営利目的の行為を禁じているため、記録するのは事実情報
# (価格・日付・販売状況)だけで、説明文・画像は保存しない。

YOSHINOYA_BASE = "https://www.yoshinoya.com"
YOSHINOYA_HOST = "www.yoshinoya.com"
YOSHINOYA_MENU_ROOT = "/menu/"
YOSHINOYA_LIMITED_CATEGORY = "gentei"

_YOSHI_UNIT_SPLIT = '<div class="menu__unit'


def yoshinoya_normalize_path(href: str, base_url: str = YOSHINOYA_BASE + "/") -> str | None:
    """リンクを、そのページ自身のホストで解決して /menu/... のパス(末尾スラッシュつき)にする。
    別ホスト(stores.yoshinoya.com 等)・メニュー以外は None。"""
    import urllib.parse
    u = urllib.parse.urlsplit(urllib.parse.urljoin(base_url, href.strip()))
    if u.netloc and u.netloc != YOSHINOYA_HOST:
        return None
    path = u.path
    if not path.endswith("/"):
        path += "/"
    return path if path.startswith(YOSHINOYA_MENU_ROOT) else None


def yoshinoya_product_id(path: str) -> str | None:
    """/menu/{カテゴリ}/{slug}/ → 'カテゴリ-slug'。2階層でないものは商品ではない。"""
    m = re.fullmatch(r"/menu/([A-Za-z0-9_]+)/([A-Za-z0-9_\-]+)/", path)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _yoshi_int(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(s.replace(",", ""))
    except ValueError:
        return None


def parse_yoshinoya_index(page: str) -> list[dict]:
    """入口 /menu/ のカテゴリ一覧([{slug, label}]、出現順)。
    リンクは末尾スラッシュ無しの絶対URL(https://www.yoshinoya.com/menu/gyudon)で書かれている。"""
    out, seen = [], set()
    for chunk in page.split(_YOSHI_UNIT_SPLIT)[1:]:
        m = re.search(r'href="([^"]+)"', chunk)
        if not m:
            continue
        path = yoshinoya_normalize_path(m.group(1))
        mm = re.fullmatch(r"/menu/([A-Za-z0-9_]+)/", path or "")
        if not mm or mm.group(1) in seen:
            continue
        t = re.search(r'menu__title">(.*?)<', chunk, re.S)
        seen.add(mm.group(1))
        out.append({"slug": mm.group(1), "label": clean_text(t.group(1)) if t else None})
    return out


def parse_yoshinoya_list(page: str) -> list[dict]:
    """カテゴリ一覧ページの商品。price(税込・店内)が None のものは料理ではない案内(「温め直し方」等)。

    同じ商品が複数のカテゴリ一覧に載る(定食が /set/ と /karaage/ の両方に等)が、リンク先URLは
    どこでも同じなので、商品IDはURLから決まる。"""
    out = []
    for chunk in page.split(_YOSHI_UNIT_SPLIT)[1:]:
        m = re.search(r'href="([^"]+)"', chunk)
        if not m:
            continue
        path = yoshinoya_normalize_path(m.group(1))
        pid = yoshinoya_product_id(path) if path else None
        if not pid:
            continue
        t = re.search(r'menu__title">(.*?)<span class="icon__arrow"', chunk, re.S)
        name = clean_text(t.group(1)) if t else ""
        price_block = re.search(r'<div class="menu__price">(.*?)</div>', chunk, re.S)
        ex = incl = None
        label = ""
        if price_block:
            pb = price_block.group(1)
            pm = re.search(r'priceNum">([\d,]+)</span>円', pb)
            im = re.search(r'priceCap">[^<]*<span>([\d,]+)</span>', pb)
            ex = _yoshi_int(pm.group(1)) if pm else None
            incl = _yoshi_int(im.group(1)) if im else None
            lm = re.match(r"\s*([^<\s][^<]*?)\s*<span", pb)
            label = clean_text(lm.group(1)) if lm else ""
        badges = re.findall(r'<span class="budge(?:__new)?"><span>([^<]*)</span>', chunk)
        out.append({
            "product_id": pid,
            "category": pid.split("-", 1)[0],
            "name": name,
            "detail_path": path,
            "price": incl,
            "price_ex_tax": ex,
            "price_label": label,
            "is_new": "NEW" in badges,
            "is_limited": "期間限定" in badges,
            "takeout_icon": "menu__icon__takeout" in chunk,
        })
    return out


def _yoshi_price_rows(block: str) -> list[dict]:
    rows = []
    for li in re.findall(r"<li>(.*?)</li>", block, re.S):
        tm = re.search(r'menu__price__taxin">([\d,]+)</span>', li)
        xm = re.search(r'menu__price__taxout">([\d,]+)</span>', li)
        if not tm:
            continue
        lm = re.search(r'menu__label">(.*?)</div>', li, re.S)
        rows.append({"label": clean_text(lm.group(1)) if lm else "",
                     "price": _yoshi_int(tm.group(1)),
                     "price_ex_tax": _yoshi_int(xm.group(1)) if xm else None})
    return rows


def parse_yoshinoya_detail(page: str) -> dict | None:
    """詳細ページから 名前・店内の税込価格(サイズ別)・テイクアウト価格・バッジ・テイクアウト可否。
    説明文・画像・栄養は読まない(栄養はPDFで、ページには載っていない)。"""
    h1 = re.search(r"<h1>(.*?)</h1>", page, re.S)
    eat = re.search(r'tab__contents__eatin[^"]*"[^>]*>(.*?)(?=tab__contents__takeout|menu__caption|menu__btn__pdf|$)',
                    page, re.S)
    take = re.search(r'tab__contents__takeout[^"]*"[^>]*>(.*?)(?=menu__caption|menu__btn__pdf|$)', page, re.S)
    eat_rows = _yoshi_price_rows(eat.group(1)) if eat else []
    if not h1 or not eat_rows:
        return None
    take_rows = _yoshi_price_rows(take.group(1)) if take else []
    cap = re.search(r'<div class="menu__caption">(.*?)</div>', page, re.S)
    caption = clean_text(cap.group(1)) if cap else ""
    # 本文のバッジは .menu__unit の中(おすすめ欄の他商品のバッジを拾わないよう、h1より後の最初の商品欄だけを見る)
    unit = re.search(r'<div class="menu__unit">(.*?)<div class="menu__photo__cap"', page, re.S)
    badges = re.findall(r'<span class="budge(?:__new)?"><span>([^<]*)</span>', unit.group(1)) if unit else []
    if take_rows or "tab__menu__takeout" in page or 'id="tab__menu__takeout"' in page:
        takeout = True
    elif "テイクアウトできません" in caption:
        takeout = False
    else:
        takeout = None
    return {
        "name": clean_text(h1.group(1)),
        "eatin": eat_rows,
        "takeout_prices": take_rows,
        "takeout": takeout,
        "is_new": "NEW" in badges,
        "is_limited": "期間限定" in badges,
        "caption": caption,
    }


def yoshinoya_price_variants(detail: dict) -> list[dict]:
    """店内の税込価格(サイズ別)に、店内と税込額が違うテイクアウト価格(消費税率の違いで差が出る)を足す。"""
    out = [{"label": r["label"], "price": r["price"]} for r in detail["eatin"]]
    eat_by_label = {r["label"]: r["price"] for r in detail["eatin"]}
    for r in detail["takeout_prices"]:
        if eat_by_label.get(r["label"]) != r["price"]:
            out.append({"label": f"{r['label']}・テイクアウト" if r["label"] else "テイクアウト", "price": r["price"]})
    return out


def yoshinoya_pick_primary(detail: dict) -> dict | None:
    """代表の価格: 店内の「並盛」→ 先頭(一覧に出る価格と一致する)。"""
    rows = detail["eatin"]
    for r in rows:
        if r["label"] == "並盛":
            return r
    return rows[0] if rows else None


# --- お知らせ(見出しだけを読む。リンク先のPDFは取得しない) ---

def parse_yoshinoya_news(page: str) -> list[dict]:
    """お知らせ一覧 /news/{年}/ を、新しい順(ページの並びのまま)の
    [{date(ISO), title, url, category}] にする。日付は「2026.10.7」の形(ゼロ埋めなし)。"""
    out = []
    for u in page.split('<div class="news__unit">')[1:]:
        d = re.search(r'news__date">\s*(\d{4})\.(\d{1,2})\.(\d{1,2})', u)
        a = re.search(r'<p class="news__title">\s*<a href="([^"]*)"[^>]*>(.*?)</a>', u, re.S)
        if not d or not a:
            continue
        inner = re.sub(r'<span class="budge__new"><span>NEW</span></span>', "", a.group(2))
        inner = re.sub(r'<span class="icon__pdf"></span>', "", inner)
        c = re.search(r'news__cat"><a[^>]*>\s*([^<]*)', u)
        out.append({
            "date": f"{int(d.group(1)):04d}-{int(d.group(2)):02d}-{int(d.group(3)):02d}",
            "title": clean_text(inner).replace("\n", " "),
            "url": a.group(1),
            "category": clean_text(c.group(1)) if c else "",
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


_YOSHI_START_RE = re.compile(
    r"(?:(本日|明日)|(\d{1,2})月(\d{1,2})日)(?:[（(][^）)]{1,3}[）)])?(?:\s*\d{1,2}時(?:\d{1,2}分)?)?\s*(?:より|から)")
# 「登場」「開始」だけでは TVCM の放映開始などに当たってしまうため、販売・発売の語を要求する
_YOSHI_LAUNCH_WORDS = re.compile(r"販売|発売")
# 公式通販ショップ(冷凍商品・ギフト)のお知らせは店舗のメニューではないので照合から外す
_YOSHI_ECOMMERCE_RE = re.compile(r"通販|ショップ|ギフト")


def yoshinoya_start_date(title: str, news_date: str) -> str | None:
    """見出しの「10月8日より…販売」「本日より販売開始」から発売日(ISO)を取る。
    年は書かれていないのでお知らせの日付から補う(12月のお知らせの「1月5日より」は翌年)。"""
    import datetime
    if not _YOSHI_LAUNCH_WORDS.search(title or ""):
        return None
    try:
        nd = datetime.date.fromisoformat(news_date)
    except ValueError:
        return None
    m = _YOSHI_START_RE.search(title)
    if not m:
        return None
    if m.group(1):
        return (nd + datetime.timedelta(days=1 if m.group(1) == "明日" else 0)).isoformat()
    try:
        d = datetime.date(nd.year, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    if (nd - d).days > 200:
        d = d.replace(year=d.year + 1)
    elif (d - nd).days > 200:
        d = d.replace(year=d.year - 1)
    return d.isoformat()


def strip_yoshinoya_notice(name: str | None) -> str | None:
    """公式メニューが発売前に付ける先頭の【予告】を外す(例: 「【予告】牛スンドゥブチゲ鍋膳」)。"""
    if not name:
        return name
    return re.sub(r"^\s*【予告】\s*", "", name)


def yoshinoya_title_matches(name: str, title: str) -> bool:
    """商品名が見出しの商品として載っているか。見出しに「」『』があれば、その中身と
    完全一致するときだけ(「さんま定食」が「牛さんま定食」の見出しに部分一致する誤爆を避ける)。
    括弧が無い見出しだけ、正規化した部分一致(4文字以上)。"""
    n = normalize_for_match(strip_yoshinoya_notice(name))
    if len(n) < 3:
        return False
    quoted = [normalize_for_match(q) for q in re.findall(r"[「『](.*?)[」』]", title or "")]
    if quoted:
        return n in quoted
    return len(n) >= 4 and n in normalize_for_match(title)


YOSHINOYA_NEWS_WINDOW_BEFORE_DAYS = 120
YOSHINOYA_NEWS_WINDOW_AFTER_DAYS = 14
# 商品に関係しないお知らせの種類(新規開店)。見出しの照合から外す
YOSHINOYA_NEWS_SKIP_CATEGORIES = ("店舗情報",)


def match_yoshinoya_launch(name: str, news: list[dict], ref: str,
                           before: int = YOSHINOYA_NEWS_WINDOW_BEFORE_DAYS,
                           after: int = YOSHINOYA_NEWS_WINDOW_AFTER_DAYS) -> dict | None:
    """商品名を含み、発売日(「○月○日より販売」)が読める最新のお知らせ。
    ref(その季の初観測日)の前 before 日〜後 after 日のお知らせだけを対象にする。
    戻り値は {"news": {...}, "launch_date": ISO}。"""
    import datetime
    try:
        c = datetime.date.fromisoformat(ref)
    except ValueError:
        return None
    lo, hi = c - datetime.timedelta(days=before), c + datetime.timedelta(days=after)
    for a in news:
        if a.get("category") in YOSHINOYA_NEWS_SKIP_CATEGORIES:
            continue
        try:
            d = datetime.date.fromisoformat(a["date"])
        except ValueError:
            continue
        if not (lo <= d <= hi) or not yoshinoya_title_matches(name, a["title"]):
            continue
        if re.search(r"終了", a["title"]) or _YOSHI_ECOMMERCE_RE.search(a["title"]):
            continue
        ld = yoshinoya_start_date(a["title"], a["date"])
        if ld:
            return {"news": a, "launch_date": ld}
    return None


def match_yoshinoya_end_notice(name: str, news: list[dict], since: str, until: str) -> dict | None:
    """「早期販売終了のお知らせ」など、商品名つきの終了告知の見出し(since〜until の日付のもの)。"""
    for a in news:
        if a.get("category") in YOSHINOYA_NEWS_SKIP_CATEGORIES:
            continue
        if not (since <= a["date"] <= until):
            continue
        if (re.search(r"終了", a["title"]) and not _YOSHI_ECOMMERCE_RE.search(a["title"])
                and yoshinoya_title_matches(name, a["title"])):
            return a
    return None


# ---------------------------------------------------------------------------
# すき家 (www.sukiya.jp)
# ---------------------------------------------------------------------------
# 静的HTML。入口 /menu/ → 通常メニューのカテゴリ一覧 /menu/in/{カテゴリ}/ → 詳細
# /menu/in/{カテゴリ}/{数字}/index.html、お知らせ一覧 /news/ を読む。
# 限定だけの一覧HTMLは無い(「LIMITED MENU」バナーは画像とリンクだけ)。カテゴリ一覧の各商品に
# 公式の「New」「期間限定」「店舗限定」「おすすめ」アイコンが付くので、それを読む。一覧に価格は無く詳細にある。
# 栄養成分・アレルゲンは別PDFで、このコードはPDFを取得・解析しない(config.json の sukiya_pdf のURLへリンクを出すだけ)。
# 規約(/aboutsite.html)は「文書による許諾なく…使用および転載…禁止」と他社より強いため、
# 記録するのは事実情報(価格・日付・販売状況)だけで、説明文・画像は保存も表示もしない。
# ページのリンクは、そのページ自身のホスト(www.sukiya.jp)で解決する(news.sukiya.jp 等を推測しない)。

SUKIYA_BASE = "https://www.sukiya.jp"
SUKIYA_HOST = "www.sukiya.jp"
SUKIYA_MENU_ROOT = "/menu/"
SUKIYA_NEWS_PATH = "/news/"
SUKIYA_NEWS_WINDOW_BEFORE_DAYS = 90
SUKIYA_NEWS_WINDOW_AFTER_DAYS = 14


def _sukiya_strip_comments(page: str) -> str:
    return re.sub(r"<!--.*?-->", "", page or "", flags=re.S)


def sukiya_normalize_path(href: str, base_url: str = SUKIYA_BASE + "/") -> str | None:
    """リンクを、そのページ自身のホストで解決したパスにする。別ホスト(news.sukiya.jp 等)は None。"""
    import urllib.parse
    u = urllib.parse.urlsplit(urllib.parse.urljoin(base_url, (href or "").strip()))
    if u.netloc and u.netloc != SUKIYA_HOST:
        return None
    return u.path or None


def sukiya_product_id(path: str) -> str | None:
    """/menu/in/{カテゴリ}/{数字}/index.html → 'カテゴリ-数字'。"""
    m = re.fullmatch(r"/menu/in/([A-Za-z0-9_]+)/(\d+)/index\.html", path or "")
    return f"{m.group(1)}-{m.group(2)}" if m else None


def parse_sukiya_index(page: str) -> list[dict]:
    """入口 /menu/ の通常メニューのカテゴリ([{slug, label}]、出現順)。
    HTMLコメントの中の旧カテゴリ(鍋・うどん等)は読まない。ラベルは画像の alt。"""
    out, seen = [], set()
    for chunk in _sukiya_strip_comments(page).split('<li class="menu_list_li')[1:]:
        m = re.search(r'href="([^"]+)"', chunk)
        if not m:
            continue
        path = sukiya_normalize_path(m.group(1))
        mm = re.fullmatch(r"/menu/in/([A-Za-z0-9_]+)/", path or "")
        if not mm or mm.group(1) in seen:
            continue
        a = re.search(r'<h3[^>]*>\s*<img[^>]*alt="([^"]*)"', chunk)
        seen.add(mm.group(1))
        out.append({"slug": mm.group(1), "label": clean_text(a.group(1)) if a else None})
    return out


_SUKIYA_ICON_FLAGS = {"new": "is_new", "limited": "is_limited", "store_limited": "is_store_limited",
                      "recommend": "is_recommend"}


def parse_sukiya_list(page: str) -> list[dict]:
    """カテゴリ一覧の商品(価格は載っていない)。公式のアイコン(New/期間限定/店舗限定/おすすめ)を読む。
    同じ数字のIDが別カテゴリに出ることはなく(2026-10-11に全13カテゴリ174件で確認)、同名の別商品
    (サラダ・季節の小鉢等が朝食とおかずの両方)は別IDなので、商品IDは『カテゴリ-数字』。"""
    out = []
    for chunk in _sukiya_strip_comments(page).split('<li class="page_menu_list_li">')[1:]:
        m = re.search(r'href="([^"]+)"', chunk)
        if not m:
            continue
        path = sukiya_normalize_path(m.group(1))
        pid = sukiya_product_id(path) if path else None
        if not pid:
            continue
        t = re.search(r'<div class="pro_name">(.*?)</div>', chunk, re.S)
        name = clean_text((t.group(1) if t else "").replace("<br>", "")).replace("\n", "")
        icons = re.search(r'<ul class="icn_list">(.*?)</ul>', chunk, re.S)
        classes = re.findall(r'<li class="icn_list_li (\w+)"', icons.group(1)) if icons else []
        e = {"product_id": pid, "category": pid.split("-", 1)[0], "name": name, "detail_path": path,
             "price": None, "is_new": False, "is_limited": False, "is_store_limited": False,
             "is_recommend": False}
        for c in classes:
            if c in _SUKIYA_ICON_FLAGS:
                e[_SUKIYA_ICON_FLAGS[c]] = True
        out.append(e)
    return out


def parse_sukiya_detail(page: str) -> dict | None:
    """詳細ページから 名前・店内の税込価格(サイズ/組み合わせ別)。説明文・画像・栄養は読まない
    (栄養はPDFで、ページには載っていない)。お持ち帰りの価格は別の読み込みで、ここには無い。"""
    body = _sukiya_strip_comments(page)
    h = re.search(r'<h2 class="pro_page_title media_pc">(.*?)</h2>', body, re.S)
    if not h:
        h = re.search(r"<title>(.*?)\s*\|", body, re.S)
    end = body.find("about_calory_wrap")
    main = body[:end] if end > 0 else body
    rows = []
    for li in re.findall(r'<li class="pro_page_size_li[^"]*">(.*?)</li>', main, re.S):
        pm = re.search(r"<span>([\d,]+)</span>\s*円", li)
        if not pm:
            continue
        sm = re.search(r'<div class="size">(.*?)</div>', li, re.S)
        rows.append({"label": clean_text(sm.group(1)).replace("\n", "") if sm else "",
                     "price": int(pm.group(1).replace(",", ""))})
    if not h or not rows:
        return None
    return {"name": clean_text((h.group(1)).replace("<br>", "")).replace("\n", ""), "prices": rows}


def sukiya_price_variants(detail: dict) -> list[dict]:
    return [{"label": r["label"], "price": r["price"]} for r in detail["prices"]]


def sukiya_pick_primary(detail_or_rows) -> dict | None:
    """代表の価格: ラベルに「並盛」を含む行 → 先頭(多くは単品の最小サイズ)。"""
    rows = detail_or_rows["prices"] if isinstance(detail_or_rows, dict) else detail_or_rows
    for r in rows:
        if "並盛" in (r.get("label") or ""):
            return r
    return rows[0] if rows else None


# --- お知らせ(見出しだけを読む。リンク先のPDFは取得しない) ---

def parse_sukiya_news(page: str) -> list[dict]:
    """お知らせ一覧 /news/ を、新しい順の [{date(ISO), title, url(絶対), category, kind}] にする。
    同じ内容が「新着」と「全件」の2か所に出るので、(日付, URL)で重複を除く。日付は「2026年10月08日」の形。"""
    out, seen = [], set()
    for m in re.finditer(r'<div class="news_list (\w+)">\s*<time>(.*?)</time>\s*<a href="([^"]*)"[^>]*>(.*?)</a>',
                         _sukiya_strip_comments(page), re.S):
        kind, tm, href, inner = m.groups()
        d = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", tm)
        if not d:
            continue
        date = f"{int(d.group(1)):04d}-{int(d.group(2)):02d}-{int(d.group(3)):02d}"
        if (date, href) in seen:
            continue
        seen.add((date, href))
        c = re.search(r'<span class="cate">(.*?)</span>', inner, re.S)
        title = clean_text(re.sub(r'<span class="cate">.*?</span>', "", inner, flags=re.S)).replace("\n", " ")
        import urllib.parse
        url = urllib.parse.urljoin(SUKIYA_BASE + SUKIYA_NEWS_PATH, href.strip())
        out.append({"date": date, "title": title, "url": url,
                    "category": clean_text(c.group(1)) if c else "", "kind": kind})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


_SUKIYA_LAUNCH_WORDS = re.compile(r"販売|発売|登場|復活")
_SUKIYA_START_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")


def sukiya_is_menu_press(a: dict) -> bool:
    """新メニューの発表(プレスリリースのPDF /news/Press_sukiya_*.pdf)か。"""
    return "/news/Press_sukiya_" in (a.get("url") or "")


def sukiya_start_date(title: str, news_date: str) -> str | None:
    """見出し「10月14日（水）朝9:00より『…』販売開始！」の発売日(ISO)。日付は商品名(『』)より前に書かれる。
    年は書かれていないのでお知らせの日付から補う。発売日が読めなければ None。"""
    import datetime
    if not _SUKIYA_LAUNCH_WORDS.search(title or ""):
        return None
    try:
        nd = datetime.date.fromisoformat(news_date)
    except ValueError:
        return None
    head = re.split(r"[「『]", title, maxsplit=1)[0]
    m = _SUKIYA_START_RE.search(head)
    if not m:
        return None
    try:
        d = datetime.date(nd.year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
    if (nd - d).days > 200:
        d = d.replace(year=d.year + 1)
    elif (d - nd).days > 200:
        d = d.replace(year=d.year - 1)
    return d.isoformat()


def match_sukiya_launch(name: str, news: list[dict], ref: str,
                        before: int = SUKIYA_NEWS_WINDOW_BEFORE_DAYS,
                        after: int = SUKIYA_NEWS_WINDOW_AFTER_DAYS) -> dict | None:
    """商品名を『』の中身と完全一致で含む(括弧が無い見出しだけ4文字以上の部分一致)、発売・登場を告げる
    プレスリリースのうち、ref の前 before 日〜後 after 日にあるもので最新のもの。
    戻り値は {"news": {...}, "launch_date": ISO または None}。"""
    import datetime
    try:
        c = datetime.date.fromisoformat(ref)
    except ValueError:
        return None
    lo, hi = c - datetime.timedelta(days=before), c + datetime.timedelta(days=after)
    for a in news:
        if not sukiya_is_menu_press(a):
            continue
        try:
            d = datetime.date.fromisoformat(a["date"])
        except ValueError:
            continue
        if not (lo <= d <= hi):
            continue
        t = a["title"]
        if not _SUKIYA_LAUNCH_WORDS.search(t) or re.search(r"終了|改定", t):
            continue
        if yoshinoya_title_matches(name, t):
            return {"news": a, "launch_date": sukiya_start_date(t, a["date"])}
    return None


# ---------------------------------------------------------------------------
# かつや・からやま (www.arclandservice.co.jp。アークランドサービスホールディングスの共通テンプレート)
# ---------------------------------------------------------------------------
# 調査(2026-10-11):
#  ・メニュー一覧 /{brand}/menu/ は静的HTMLの1ページに全商品(カテゴリ別ページは無い)。商品名・税抜/税込価格・
#    公式アイコン(「期間限定」「NEW」「おすすめ」)・詳細URL(…/menu/{番号}/?identifier=)が一覧だけで揃うので、
#    商品の詳細ページは取らない。アレルゲン一覧PDFへのリンクも一覧ページの上部にある(PDF自体は取得しない)。
#  ・お知らせ /{brand}/information/ も1ページに全件。各見出しは prtimes.jp などへの外部リンク(リンク先は取得しない)。
#  ・栄養成分は公式サイトのHTMLにもPDFにも載っていない。
#  ・ページのリンクは、そのページ自身のホスト(www.arclandservice.co.jp)で解決する。別のホストを推測しない。
#  ・著作権表記(KATSUYA CO.,LTD. / EVER ACTION CO.,LTD.)があるので、記録は事実情報(価格・日付・販売状況)と
#    見出しの出典表示、公式ページへのリンクだけ。説明文・画像は保存も表示もしない。
ARCLAND_BASE = "https://www.arclandservice.co.jp"
ARCLAND_HOST = "www.arclandservice.co.jp"
ARCLAND_BRANDS = ("katsuya", "karayama")
ARCLAND_NEWS_WINDOW_BEFORE_DAYS = 120
ARCLAND_NEWS_WINDOW_AFTER_DAYS = 14
ARCLAND_NEWS_RECORD_DAYS = 90  # 一覧に載った商品を「お知らせに出た」として記録する対象の期間(直近何日)
ARCLAND_LIMITED_SECTION = "期間限定"


def arcland_menu_path(brand: str) -> str:
    return f"/{brand}/menu/"


def arcland_news_path(brand: str) -> str:
    return f"/{brand}/information/"


def _arcland_yen(s: str | None) -> int | None:
    m = re.search(r"(\d[\d,]*)", s or "")
    return int(m.group(1).replace(",", "")) if m else None


def parse_arcland_menu(page: str, brand: str = "katsuya") -> dict:
    """メニュー一覧を {"items": [...], "allergen_pdf_url": str|None} にする。
    items: product_id(URLの番号)・name・price(税込)・price_ex_tax・category(セクション名。「期間限定」セクションは None)・
    badges(公式アイコンの文字列)・is_limited・is_new・official_url(?identifier= を除いた絶対URL)。
    同じ商品が複数のセクションに出たらアイコンをまとめる。構造が変わって商品が取れなければ items は空。"""
    import urllib.parse
    page = _strip_html_comments(page)
    base = f"{ARCLAND_BASE}{arcland_menu_path(brand)}"
    allergen = None
    for m in re.finditer(r'<a href="([^"]*)"[^>]*class="menu-list-nav__search"[^>]*>(.*?)</a>', page, re.S):
        if "アレルゲン" in clean_text(m.group(2)):
            allergen = urllib.parse.urljoin(base, m.group(1).strip())
            break
    items: dict[str, dict] = {}
    for sec in page.split('<section class="menu-list__section')[1:]:
        t = re.search(r'menu-list__ttl">\s*([^<]*)', sec)
        title = clean_text(t.group(1)) if t else ""
        category = None if title == ARCLAND_LIMITED_SECTION else (title or None)
        for li in sec.split('<li class="menu-list__item">')[1:]:
            a = re.search(r'<a href="([^"]*/menu/(\d+)/[^"]*)"', li)
            n = re.search(r'menu-list__name">(.*?)</h3>', li, re.S)
            if not a or not n:
                continue
            pid = a.group(2)
            badges = [clean_text(x) for x in re.findall(r'menu-icon__txt">(.*?)</span>', li, re.S)]
            pn = re.search(r'menu-price__number[^"]*">([^<]*)', li)
            pt = re.search(r'menu-price__tax">([^<]*)', li)
            entry = {
                "product_id": pid,
                "name": clean_text(n.group(1)),
                "price": _arcland_yen(pt.group(1)) if pt else None,
                "price_ex_tax": _arcland_yen(pn.group(1)) if pn else None,
                "category": category,
                "badges": badges,
                "official_url": urllib.parse.urljoin(base, a.group(1).split("?")[0]),
            }
            if pid in items:
                prev = items[pid]
                prev["badges"] = prev["badges"] + [b for b in badges if b not in prev["badges"]]
                prev["category"] = prev["category"] or category
                continue
            items[pid] = entry
    out = list(items.values())
    for e in out:
        e["is_limited"] = "期間限定" in e["badges"]
        e["is_new"] = "NEW" in e["badges"]
    return {"items": out, "allergen_pdf_url": allergen}


def parse_arcland_news(page: str, brand: str = "katsuya") -> list[dict]:
    """お知らせ一覧を、新しい順の [{date(ISO), title, url(絶対), category}] にする(categoryは常に空)。"""
    import urllib.parse
    base = f"{ARCLAND_BASE}{arcland_news_path(brand)}"
    out, seen = [], set()
    for li in _strip_html_comments(page).split('<li class="information__item">')[1:]:
        a = re.search(r'<a href="([^"]*)"', li)  # 古いお知らせにはリンクが無いものがある(url は空)
        d = re.search(r'information__date"[^>]*>\s*(\d{4})\.(\d{1,2})\.(\d{1,2})', li)
        t = re.search(r'information__txt[^"]*">(.*?)</span>', li, re.S)
        if not d or not t:
            continue
        date = f"{int(d.group(1)):04d}-{int(d.group(2)):02d}-{int(d.group(3)):02d}"
        url = urllib.parse.urljoin(base, a.group(1).strip()) if a else ""
        title = clean_text(t.group(1)).replace("\n", " ")
        if (date, url, title) in seen:
            continue
        seen.add((date, url, title))
        out.append({"date": date, "title": title, "url": url, "category": ""})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


_ARCLAND_DATE_RE = re.compile(r"(\d{1,2})月(\d{1,2})日|(?<![\d/])(\d{1,2})/(\d{1,2})(?=[（(])")
_ARCLAND_LAUNCH_WORDS = re.compile(r"販売|発売")
_ARCLAND_NOT_LAUNCH = re.compile(r"終了|改定|値上げ|引き|無料|クーポン")


def arcland_start_date(title: str, news_date: str) -> str | None:
    """見出し「10月9日(金)～期間限定で「…」販売開始！」「新メニューを7月27日(月)販売開始！」の発売日(ISO)。
    最初の「販売/発売」より前にある最初の日付を取る。年は書かれていないのでお知らせの日付から補う
    (12月のお知らせの「1月8日」は翌年)。読めなければ None。"""
    import datetime
    t = title or ""
    w = _ARCLAND_LAUNCH_WORDS.search(t)
    if not w:
        return None
    try:
        nd = datetime.date.fromisoformat(news_date)
    except ValueError:
        return None
    m = _ARCLAND_DATE_RE.search(t[:w.start()])
    if not m:
        return None
    mo, dy = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    try:
        d = datetime.date(nd.year, int(mo), int(dy))
    except ValueError:
        return None
    if (nd - d).days > 200:
        d = d.replace(year=d.year + 1)
    elif (d - nd).days > 200:
        d = d.replace(year=d.year - 1)
    return d.isoformat()


def arcland_title_matches(name: str, title: str) -> bool:
    """商品名が見出しの「」『』の中身と完全一致するか(正規化後)。「A・B」のように中黒で並べた商品名は、
    全体と各項目のどちらとも照合する。括弧が無い見出しは、正規化した部分一致(4文字以上)。"""
    n = normalize_for_match(name)
    if len(n) < 2:
        return False  # 「豚丼」のような2文字の商品名もあるので、完全一致なら2文字から認める
    quotes = re.findall(r"[「『](.*?)[」』]", title or "")
    if not quotes:
        return len(n) >= 4 and n in normalize_for_match(title)
    cands = set()
    for q in quotes:
        cands.add(normalize_for_match(q))
        cands.update(normalize_for_match(p) for p in q.split("・"))
    return n in cands


def match_arcland_launch(name: str, news: list[dict], ref: str,
                         before: int = ARCLAND_NEWS_WINDOW_BEFORE_DAYS,
                         after: int = ARCLAND_NEWS_WINDOW_AFTER_DAYS) -> dict | None:
    """商品名を見出しの「」と完全一致で含み、販売・発売を告げる最新のお知らせ(ref の前 before 日〜後 after 日)。
    戻り値は {"news": {...}, "launch_date": ISO または None}。"""
    import datetime
    try:
        c = datetime.date.fromisoformat(ref)
    except ValueError:
        return None
    lo, hi = c - datetime.timedelta(days=before), c + datetime.timedelta(days=after)
    for a in news:
        try:
            d = datetime.date.fromisoformat(a["date"])
        except ValueError:
            continue
        if not (lo <= d <= hi):
            continue
        t = a["title"]
        if not _ARCLAND_LAUNCH_WORDS.search(t) or _ARCLAND_NOT_LAUNCH.search(t):
            continue
        if arcland_title_matches(name, t):
            return {"news": a, "launch_date": arcland_start_date(t, a["date"])}
    return None


# ---------------------------------------------------------------------------
# 丸亀製麺 (jp.marugame.com。Next.js製。全ページの <script id="__NEXT_DATA__"> のJSONから取る)
# ---------------------------------------------------------------------------
# 調査・方針(2026-10-17。実物で確認した事実):
#  ・公式は jp.marugame.com。www.marugame.co.jp は別会社なので使わない。robots.txt は Allow: / と Sitemap: だけ。
#  ・メニュー一覧は /menu/{カテゴリ}/ の props.pageProps.menus = {"shop": [...], "takeout": [...]}。
#    カテゴリは udon・tempura・gohanmono・topping・udonbento の5つ(各一覧の shopMenuNavigation / takeoutMenuNavigation にある)。
#    店内(shop)とお持ち帰り(takeout)で同じ商品が重複して出る。udonbento は shop が空で takeout だけ。
#  ・商品の slug(id)はカテゴリをまたいで重複する(mentaiko がご飯ものとトッピングの両方にある)ので、
#    商品IDは「カテゴリ-slug」(href の /menu/{カテゴリ}/{slug}/ から)にする。
#  ・限定の印: icon_select の文字列(「期間限定（limited_time_item）」「新定番（new_classic）」「小学６年生まで限定（kids_12_and_under）」)
#    と attributes。attributes.limited_time_item は false のまま icon_select だけが立つことがあり(肉辛まぜうどん)、
#    逆に icon_select が空で attributes.limited=「期間限定」だけの商品もある(すだち)ので、どれか1つでも期間限定なら期間限定とする。
#    「新定番」は期間限定から定番に加わった印で、新登場ではない(記録の対象にしない)。
#  ・publishedAt / revisedAt / createdAt / updatedAt はCMSの更新日時で、発売日ではない。使わない。
#  ・発売日・販売期間の専用フィールドは無い。→ launch_date は null のまま、初めて見つけた日を「記録開始日」として表示する。
#  ・アレルゲン: allergenItemData の値は "1"=使用(CSV一覧の●)、"5"=不使用(CSVの‐)。公式のアレルギー情報CSV
#    (allergen_261006.csv)と、メニュー一覧のJSON(64商品×27品目=1,728セル)を突き合わせて、完全に一致することを確認した。
#    この2値以外が出たら対応が不明なので、アレルゲンは出さない(None)。
#  ・栄養: nutritionItemData.items はサイズ別(並・大・得・小)・温冷別。値は文字列(energy=kcal、sodium=mg、他g)。
#  ・存在しないパスは HTTP 403 のソフト404(本文「お探しのページは見つかりませんでした。」)。
MARUGAME_BASE = "https://jp.marugame.com"
MARUGAME_HOST = "jp.marugame.com"
MARUGAME_ENTRY_CATEGORY = "udon"
MARUGAME_MENU_RE = re.compile(r"^/menu/([a-z0-9_]+)/([A-Za-z0-9_-]+)/?$")
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_MARUGAME_SIZE_LABELS = {"small": "小", "regular": "並", "large": "大", "xlarge": "得", "no_size": "",
                         "piece": "1個", "bowl": "1杯", "1_serving": "うどん１玉", "2_serving": "うどん２玉"}


def marugame_menu_path(category: str) -> str:
    return f"/menu/{category}/"


def marugame_page_props(page: str) -> dict | None:
    """ページの __NEXT_DATA__ から props.pageProps を取り出す。無い・壊れている・形が違うときは None。"""
    m = _NEXT_DATA_RE.search(page or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        props = data["props"]["pageProps"]
    except (ValueError, KeyError, TypeError):
        return None
    return props if isinstance(props, dict) else None


def marugame_prices(m: dict) -> list[dict]:
    """[{label, price, default}]。公式の prices(単位つき)を使い、無ければ price の辞書から作る。"""
    out = []
    for p in m.get("prices") or []:
        if isinstance(p, dict) and isinstance(p.get("number"), (int, float)):
            out.append({"label": p.get("unit") or "", "price": int(p["number"]), "default": bool(p.get("default"))})
    if out:
        return out
    price = m.get("price") or {}
    for key, label in _MARUGAME_SIZE_LABELS.items():
        v = price.get(key) if isinstance(price, dict) else None
        if isinstance(v, (int, float)):
            out.append({"label": label, "price": int(v), "default": key == "regular"})
    return out


def marugame_pick_primary_price(prices: list[dict]) -> dict | None:
    """代表の価格: 公式が既定にしているもの→「並」→先頭。"""
    if not prices:
        return None
    for p in prices:
        if p.get("default"):
            return p
    for p in prices:
        if p["label"] == "並":
            return p
    return prices[0]


def _marugame_icon_label(s: str) -> str:
    return re.sub(r"[（(][^）)]*[）)]\s*$", "", clean_text(s)).strip()


def marugame_flags(m: dict) -> dict:
    """icon_select と attributes から {is_limited, is_new_classic, badges}。期間限定はどれか1つでも立っていれば真。"""
    icons = [s for s in (m.get("icon_select") or []) if isinstance(s, str)]
    attrs = m.get("attributes") or {}
    badges = [_marugame_icon_label(s) for s in icons]
    limited = (any("limited_time_item" in s for s in icons) or bool(attrs.get("limited_time_item"))
               or bool(attrs.get("limited")))
    if limited and "期間限定" not in badges:
        badges.insert(0, "期間限定")
    new_classic = any("new_classic" in s for s in icons) or bool(attrs.get("new_classic"))
    if new_classic and "新定番" not in badges:
        badges.append("新定番")
    return {"is_limited": limited, "is_new_classic": new_classic,
            "badges": [b for b in dict.fromkeys(badges) if b]}


def parse_marugame_list(page: str) -> dict:
    """カテゴリ一覧ページを {"category": {id,name}|None, "nav": [{id,name}], "items": [...]} にする。
    items: product_id(カテゴリ-slug)・slug・category_id・name・price(代表)・prices・description(全文。保存時に冒頭1文にする)・
    lead・badges・is_limited・is_new_classic・channels(shop/takeoutのうち載っていたもの)・takeout_only・href・official_url。
    構造が変わって取れなければ items は空(nav も空のことがある)。"""
    props = marugame_page_props(page)
    out = {"category": None, "nav": [], "items": []}
    if not props:
        return out
    cur = props.get("currentCategory")
    if isinstance(cur, dict) and cur.get("id"):
        out["category"] = {"id": cur["id"], "name": clean_text(cur.get("name") or "").replace("\n", "")}
    nav, seen = [], set()
    for key in ("shopMenuNavigation", "takeoutMenuNavigation"):
        for c in props.get(key) or []:
            if isinstance(c, dict) and c.get("id") and c["id"] not in seen and re.fullmatch(r"[a-z0-9_]+", str(c["id"])):
                seen.add(c["id"])
                nav.append({"id": c["id"], "name": clean_text(c.get("name") or "").replace("\n", "")})
    out["nav"] = nav
    menus = props.get("menus")
    if not isinstance(menus, dict):
        return out
    items: dict[str, dict] = {}
    for channel, label in (("shop", "店内"), ("takeout", "お持ち帰り")):
        for m in menus.get(channel) or []:
            if not isinstance(m, dict) or not m.get("id") or not m.get("name"):
                continue
            href = m.get("href") or f"{marugame_menu_path((out['category'] or {}).get('id') or '')}{m['id']}/"
            hm = MARUGAME_MENU_RE.match(href)
            if not hm:
                continue
            cat, slug = hm.group(1), hm.group(2)
            pid = f"{cat}-{slug}"
            prices = marugame_prices(m)
            primary = marugame_pick_primary_price(prices)
            flags = marugame_flags(m)
            attrs = m.get("attributes") or {}
            if pid in items:
                cur_e = items[pid]
                cur_e["channels"] = [c for c in ("店内", "お持ち帰り") if c in cur_e["channels"] or c == label]
                cur_e["is_limited"] = cur_e["is_limited"] or flags["is_limited"]
                cur_e["is_new_classic"] = cur_e["is_new_classic"] or flags["is_new_classic"]
                cur_e["badges"] += [b for b in flags["badges"] if b not in cur_e["badges"]]
                if cur_e["price"] is None and primary:
                    cur_e["price"], cur_e["prices"] = primary["price"], prices
                continue
            items[pid] = {
                "product_id": pid, "slug": slug, "category_id": cat,
                "name": clean_text(m["name"]).replace("\n", " "),
                "price": primary["price"] if primary else None,
                "prices": prices,
                "description": m.get("description") if isinstance(m.get("description"), str) else None,
                "lead": m.get("lead") if isinstance(m.get("lead"), str) else None,
                "badges": flags["badges"], "is_limited": flags["is_limited"], "is_new_classic": flags["is_new_classic"],
                "channels": [label], "takeout_only": bool(attrs.get("takeout_only")),
                "href": f"/menu/{cat}/{slug}/", "official_url": f"{MARUGAME_BASE}/menu/{cat}/{slug}/",
            }
    out["items"] = list(items.values())
    return out


def marugame_nutrition_label(item: dict) -> str:
    return "・".join(x for x in (clean_text(item.get("temperature")), clean_text(item.get("size"))) if x)


def marugame_nutrition_variants(data) -> list[dict]:
    """nutritionItemData.items を [{label, nutrition}] にする(サイズ別・温冷別。数値は文字列→float)。
    食塩相当量は公式の saltEquivalent、ナトリウム(mg)は other に入れる。主要な数値が1つも読めない行は捨てる。"""
    out = []
    items = data.get("items") if isinstance(data, dict) else None
    for it in items or []:
        if not isinstance(it, dict):
            continue
        nut = build_nutrition([("エネルギー", str(it.get("energy") or "")), ("たんぱく質", str(it.get("protein") or "")),
                               ("脂質", str(it.get("fat") or "")), ("炭水化物", str(it.get("carbohydrate") or "")),
                               ("食塩相当量", str(it.get("saltEquivalent") or ""))])
        if nut is None or all(nut[k] is None for k in ("kcal", "protein_g", "fat_g", "carbs_g")):
            continue
        sodium = to_number(it.get("sodium"))
        if sodium is not None:
            nut["other"]["ナトリウム"] = f"{sodium:g}mg"
        out.append({"label": marugame_nutrition_label(it), "nutrition": nut})
    return out


def marugame_pick_primary_nutrition(variants: list[dict]) -> dict | None:
    """代表の栄養: ラベルが「並」で終わるもの(温・並など。先頭優先)→先頭。"""
    if not variants:
        return None
    for v in variants:
        if v["label"] in ("", "並") or v["label"].endswith("並"):
            return v
    return variants[0]


def marugame_allergens(item_data, headers) -> dict | None:
    """allergenItemData と allergenHeadersData から {"contains": [品目名], "partial": []}。
    値が "1"(使用)と "5"(不使用)以外のものが1つでもあれば、意味が確定していないので None。"""
    if not isinstance(item_data, dict) or not isinstance(headers, list) or not headers:
        return None
    heads = sorted((h for h in headers if isinstance(h, dict) and h.get("id") and h.get("name")),
                   key=lambda h: int(h["order"]) if str(h.get("order", "")).isdigit() else 999)
    if not heads:
        return None
    contains = []
    for h in heads:
        v = item_data.get(h["id"])
        if v == "1":
            contains.append(clean_text(h["name"]))
        elif v != "5":
            return None
    return {"contains": contains, "partial": []}


def parse_marugame_detail(page: str) -> dict | None:
    """商品詳細ページ。{name, nutrition_variants, allergens, allergens_ok} を返す。__NEXT_DATA__ が無い・商品が無いときは None。"""
    props = marugame_page_props(page)
    if not props or not isinstance(props.get("detail"), dict):
        return None
    d = props["detail"]
    nut = props.get("nutritionItemData")
    return {
        "id": d.get("id"),
        "name": clean_text(d.get("name") or ""),
        "nutrition_variants": marugame_nutrition_variants(nut),
        "allergens": marugame_allergens(props.get("allergenItemData"), props.get("allergenHeadersData")),
    }


# ---------------------------------------------------------------------------
# バーガーキング (www.burgerking.co.jp。SPA。画面が使う内部API(POST)のJSONから取る)
# ---------------------------------------------------------------------------
#  ・POST https://www.burgerking.co.jp/burgerking/{電文コード}.json。本文は form-urlencoded の message=<JSON>
#    ({"header":{"trcode":電文コード,"cdCallChnn":"02"},"body":{...}})。認証・Cookie不要。応答は header.result が true で成功。
#  ・電文: BKJ0508=メニュー一覧(menuKeywordList に「期間限定」を渡すと、その印の付いた商品だけ)、
#    BKJ0510=商品詳細(セット価格など)、BKJ0506=ニュース、BKJ0501=イベント(開催中 C / 終了 E。終了済みは dtEnd に実際の終了日)。
#  ・説明文・画像・栄養は扱わない(規約が複製・転載を禁じている。商品名・価格・日付などの事実だけを記録する)。
#  ・「NEW」の印(menuFlagList)は全商品で空なので使わない。期間限定の判定はキーワード絞り込みだけ。
BK_BASE = "https://www.burgerking.co.jp"
BK_HOST = "www.burgerking.co.jp"
BK_LIMITED_KEYWORD = "期間限定"
BK_SET_RE = re.compile(r"セット")  # セット商品は記録しない(バーガー本体と二重になる)
BK_OPEN_END = "99991231"  # 開催中のイベント・掲載中のお知らせの dtEnd
BK_NEWS_RECORD_DAYS = 90
BK_LAUNCH_WORDS = re.compile(r"新発売|新登場|発売|登場|復活|販売")
BK_NOT_LAUNCH = re.compile(r"終了|改定|値上げ|引き|無料|クーポン|オフ|プレゼント|閉店|オープン")


def bk_url(trcode: str) -> str:
    return f"{BK_BASE}/burgerking/{trcode}.json"


def bk_request_form(trcode: str, body: dict) -> dict:
    """POSTするフォームの中身({"message": JSON文字列})。"""
    return {"message": json.dumps({"header": {"trcode": trcode, "cdCallChnn": "02"}, "body": body},
                                  ensure_ascii=False, separators=(",", ":"))}


def bk_menu_url(category_cd: str | None, menu_cd: str) -> str:
    return f"{BK_BASE}/menu/{category_cd}/{menu_cd}" if category_cd else f"{BK_BASE}/menu"


def bk_body(text: str) -> dict:
    """応答のJSONから body を返す。JSONでない・header.result が true でない・body が無いときは ValueError。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSONではない応答: {e}") from e
    if not isinstance(data, dict) or not (data.get("header") or {}).get("result") or not isinstance(data.get("body"), dict):
        raise ValueError("応答が成功ではない(header.result≠true、または body なし)")
    return data["body"]


def _bk_price(s) -> int | None:
    try:
        return int(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _bk_date(s: str | None) -> str | None:
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def parse_bk_menu(text: str) -> list[dict]:
    """メニュー一覧(BKJ0508、期間限定で絞り込み済み)を、商品IDで1件にまとめて返す(セット商品は除く)。
    1件: product_id・name・price(税込。単品)・has_sets(価格幅あり=セットの価格が別にある)・category・category_cd・official_url。
    同じ商品が複数のカテゴリに出るので、「おすすめ」以外のカテゴリを優先して表示用に使う。商品が取れなければ空リスト
    (呼び出し側が「構造変更の疑い」として扱う)。応答が不正なら ValueError。"""
    body = bk_body(text)
    items: dict[str, dict] = {}
    for cat in body.get("allMenuList") or []:
        cname, ccd = cat.get("menuCategoryNm") or "", cat.get("menuCategoryCd")
        for m in cat.get("menuInfo") or []:
            pid, name = str(m.get("menuCd") or ""), clean_text(m.get("menuNm"))
            if not pid or not name or BK_SET_RE.search(name):
                continue
            e = items.get(pid)
            if e is None:
                e = items[pid] = {"product_id": pid, "name": name, "price": _bk_price(m.get("dineInPrc")),
                                  "has_sets": m.get("prcRangeYn") == "Y", "category": None, "category_cd": None,
                                  "_fallback": (cname, ccd)}
            if cname and cname != "おすすめ" and e["category"] is None:
                e["category"], e["category_cd"] = cname, ccd
    out = []
    for e in items.values():
        fb = e.pop("_fallback")
        if e["category"] is None:  # 「おすすめ」にしか出ない商品は、カテゴリ名を出さない(リンクだけ使う)
            e["category_cd"] = fb[1]
        e["official_url"] = bk_menu_url(e["category_cd"], e["product_id"])
        out.append(e)
    return out


def parse_bk_detail(text: str, menu_cd: str) -> dict:
    """商品詳細(BKJ0510)から、単品とセットの価格の内訳 price_variants([{label, price}]。2件以上のときだけ)と
    「期間限定」の印があるか(limited)を返す。"""
    body = bk_body(text)
    name = clean_text(body.get("menuNm"))
    variants = []
    for m in body.get("menuInfoList") or []:
        price = _bk_price(m.get("dineInPrc"))
        n = clean_text(m.get("menuNm"))
        if price is None or not n:
            continue
        if str(m.get("menuCd")) == str(menu_cd) or (name and n == name):
            label = "単品"
        elif name and n.startswith(name) and n != name:
            label = n[len(name):].strip()
        else:
            label = n
        variants.append({"label": label, "price": price})
    variants.sort(key=lambda v: (v["label"] != "単品", v["price"]))
    kws = [k.get("menuKeywordPk") or k.get("menuKeywordNm") for k in body.get("menuKeywordList") or []]
    return {"price_variants": variants if len(variants) >= 2 else None,
            "limited": BK_LIMITED_KEYWORD in kws}


def parse_bk_news(text: str) -> list[dict]:
    """ニュース(BKJ0506)を新しい順の [{id, date(ISO), title, url, category}] にする。応答が不正なら ValueError。"""
    out = []
    for d in bk_body(text).get("data") or []:
        date, title = _bk_date(d.get("dtStart")), clean_text(d.get("dsTitle"))
        nid = str(d.get("idNotice") or "")
        if not date or not title or not nid:
            continue
        out.append({"id": nid, "date": date, "title": title, "url": f"{BK_BASE}/notice/{nid}",
                    "category": d.get("categoryName") or ""})
    out.sort(key=lambda x: (x["date"], x["id"]), reverse=True)
    return out


def parse_bk_events(text: str) -> list[dict]:
    """イベント(BKJ0501)を [{id, title, start, end(ISO。開催中は None), status(C/E), url}] にする。画像URLは読まない。"""
    out = []
    for d in bk_body(text).get("eventList") or []:
        eid, title = str(d.get("eventId") or ""), clean_text(d.get("eventTitle"))
        if not eid or not title:
            continue
        end = None if d.get("dtEnd") == BK_OPEN_END else _bk_date(d.get("dtEnd"))
        out.append({"id": eid, "title": title, "start": _bk_date(d.get("dtStart")), "end": end,
                    "status": d.get("tpStatusEvent") or "", "url": f"{BK_BASE}/event/detail/{eid}"})
    return out


BK_LAUNCH_AFTER_CHARS = 12  # 括弧の直後(この文字数以内)に「新発売」等があれば、その括弧が新商品


def bk_title_matches(name: str, title: str, strict: bool = True) -> bool:
    """商品名が見出しの「」『』の中身と(正規化後に)完全一致するか。括弧のない見出しは照合しない(曖昧になるため)。
    strict=True は、括弧の**直後**(BK_LAUNCH_AFTER_CHARS文字以内)に「新発売」「新登場」「復活」等が続く括弧だけを見る。
    見出しは「大人気「マッシュルームワッパー」に新作！…「オータムスペシャル マッシュルームワッパー」新発売」のように
    元になった商品や使っているソースの名前も括弧で書くので、「新発売」が付いた括弧だけを新商品とみなす。
    strict=False は全部の括弧を見る(「開催中のイベントに名前が出ているか」を広めに確かめるのに使う)。"""
    n = normalize_for_match(name)
    if len(n) < 2:
        return False
    for m in re.finditer(r"[「『](.*?)[」』]", title or ""):
        if strict and not BK_LAUNCH_WORDS.search(title[m.end():m.end() + BK_LAUNCH_AFTER_CHARS]):
            continue
        q = m.group(1)
        if n == normalize_for_match(q) or n in {normalize_for_match(x) for x in q.split("・")}:
            return True
    return False


def _bk_is_launch_title(title: str) -> bool:
    return bool(BK_LAUNCH_WORDS.search(title)) and not BK_NOT_LAUNCH.search(title)


def match_bk_news(name: str, news: list[dict], ref: str, before: int = ARCLAND_NEWS_WINDOW_BEFORE_DAYS,
                  after: int = ARCLAND_NEWS_WINDOW_AFTER_DAYS) -> dict | None:
    """商品名を見出しの「」『』と完全一致で含み、発売・登場・復活を告げる最新のニュース(ref の前 before 日〜後 after 日)。
    発売日そのものは見出しに無い(PDFの画像の中)ので、返すのは告知日(ニュースの公開日)。"""
    import datetime
    try:
        c = datetime.date.fromisoformat(ref)
    except ValueError:
        return None
    lo, hi = c - datetime.timedelta(days=before), c + datetime.timedelta(days=after)
    for a in news:  # 新しい順
        try:
            d = datetime.date.fromisoformat(a["date"])
        except ValueError:
            continue
        if lo <= d <= hi and _bk_is_launch_title(a["title"]) and bk_title_matches(name, a["title"]):
            return a
    return None


def has_open_bk_event(name: str, events: list[dict]) -> bool:
    """開催中(C)のイベントの見出しの「」『』のどれかに商品名が完全一致しているか(広めの照合)。"""
    return any(e["status"] == "C" and bk_title_matches(name, e["title"], strict=False) for e in events)


def find_bk_event_end(name: str, events: list[dict], ref: str, before: int = ARCLAND_NEWS_WINDOW_BEFORE_DAYS) -> dict | None:
    """商品と確実にひも付くイベントの終了日。次をすべて満たすときだけ返す(曖昧なら None=終了は一覧からの消失で判定)。
    ・イベント名の「」『』に商品名が完全一致し、発売・登場・復活を告げる見出し(クーポン・値引き等は除く)
    ・開催中(C)の同名イベントが無い(あれば販売中)
    ・終了済み(E)で、開始日が ref の前 before 日以降のものがちょうど1種類の終了日に定まる(去年の販売の名残を拾わない)"""
    import datetime
    try:
        lo = datetime.date.fromisoformat(ref) - datetime.timedelta(days=before)
    except ValueError:
        return None
    if has_open_bk_event(name, events):
        return None  # 開催中のイベントに名前が出ている=販売中の可能性がある
    hits = [e for e in events if _bk_is_launch_title(e["title"]) and bk_title_matches(name, e["title"])]
    ended = []
    for e in hits:
        if e["status"] != "E" or not e["end"] or not e["start"]:
            continue
        if datetime.date.fromisoformat(e["start"]) >= lo:
            ended.append(e)
    if len({e["end"] for e in ended}) != 1:
        return None
    return max(ended, key=lambda e: e["id"])


# ---------------------------------------------------------------------------
# 幸楽苑・日高屋(ラーメンチェーン。2026-11-18追加、11・12チェーン目)
# ---------------------------------------------------------------------------
# 調査・方針(2026-11-18。実物で確認した事実):
#  共通:
#  ・公式サイトに「期間限定」の印は無い。限定・新メニューは、お知らせの見出し(日高屋は本文の日付も)に「期間限定」「発売」「販売開始」
#    「登場」があり、商品名が見出しと一致するものだけを記録する(かつや・からやまの arcland と同じ考え方)。
#  ・説明文・画像は保存も表示もしない。栄養・アレルゲンは詳細ページの値(事実)。
#  ・一覧にあっても記録しない: セット(名前に「セット」)・トッピング・アルコール・お持ち帰りの冷凍品など(下の各関数)。
#  幸楽苑(stores.kourakuen.co.jp / www.kourakuen.co.jp):
#  ・メニューは /grandmenu/ の静的HTML1ページ(タブ=カテゴリ。同じ商品が「おすすめ」と各カテゴリに重複して出る)。
#    商品IDは詳細リンクの数値(info.html?id=15145。日付は入らない)。価格は c-menuListPriceNum(1,230 のようにカンマあり)。
#  ・詳細 info.html?id= に栄養(熱量・たんぱく質・脂質・炭水化物・食塩相当量)とアレルゲン(●の列。主要9品目)がある。
#  ・お知らせは www.kourakuen.co.jp/news の1ページに全件(掲載日 2026.11.16 形式)。見出しの日付が発売日。
#  ・stores 側の robots.txt は /404.html へ301で飛ぶ(=制限なし)。www 側は Disallow なし。
#  日高屋(hidakaya.hiday.co.jp。hidaka.co.jp は別会社なので使わない):
#  ・メニューは /hits/ja/menu/1/list/{1..8}.html(カテゴリ別の静的HTML)と、おすすめ /hits/ja/menu/1/list.html。
#    期間限定の「濃厚豚骨醤油ラーメン」(detail/20261009)は「おすすめ」にしか出ない。ほかの menu/2・6・12・98 は来来軒・焼鳥日高・台南・
#    その他店舗の別ブランドのメニューで、menu/99 は全店舗の合算。日高屋(menu/1)だけを対象にする。
#  ・商品IDは詳細のファイル名(1・9_196・808-1・20261009)。8桁の日付形式は限定品ごとの別ページ(2026-11-18時点で1件)。
#  ・お知らせは /json/news.json(掲載期間があり、期間が過ぎると消える。店舗オープン・休業の告知が混ざる)。
KOURAKUEN_STORES_BASE = "https://stores.kourakuen.co.jp"
KOURAKUEN_WWW_BASE = "https://www.kourakuen.co.jp"
KOURAKUEN_MENU_PATH = "/grandmenu/"
KOURAKUEN_NEWS_PATH = "/news"
HIDAKAYA_BASE = "https://hidakaya.hiday.co.jp"
HIDAKAYA_NEWS_PATH = "/json/news.json"
# 日高屋の一覧: (パス, カテゴリ名)。おすすめ・ラーメン・定食・単品・おつまみだけ。セット(2)・ドリンク(6。ほぼお酒)・
# テイクアウト(7。冷凍品など)・トッピング(8)は記録しない
HIDAKAYA_LISTS = [("/hits/ja/menu/1/list.html", None), ("/hits/ja/menu/1/list/1.html", "ラーメン"),
                  ("/hits/ja/menu/1/list/3.html", "定食"), ("/hits/ja/menu/1/list/4.html", "単品"),
                  ("/hits/ja/menu/1/list/5.html", "おつまみ")]
RAMEN_NEWS_RECORD_DAYS = 90      # 一覧に居る商品を「お知らせに出た」として記録する対象の期間(直近何日)
RAMEN_NEWS_WINDOW_BEFORE_DAYS = 120
RAMEN_NEWS_WINDOW_AFTER_DAYS = 14

_RAMEN_LAUNCH_WORDS = re.compile(r"販売|発売|登場")
_RAMEN_NOT_LAUNCH = re.compile(r"終了|改定|値上げ|引き|無料|クーポン|オープン|閉店|休業|休止|店舗|募金|寄付|リニューアル")


def _yen_int(s: str | None) -> int | None:
    m = re.search(r"(\d[\d,]*)", s or "")
    return int(m.group(1).replace(",", "")) if m else None


def looks_like_html(text: str | None) -> bool:
    return bool(re.search(r"<\s*(html|!doctype|body)", (text or "")[:500], re.I))


# 幸楽苑 --------------------------------------------------------------------

KOURAKUEN_EXCLUDED_CATEGORIES = ("アルコール", "お酒のセット")  # セット・お酒
KOURAKUEN_TOPPING_MAX_PRICE = 130   # 「もう一品」のうちこの価格以下は味玉・ネギ・コーンなどのトッピング(薬味も含む)


def kourakuen_excluded(entry: dict) -> str | None:
    """記録しない商品なら理由を返す(セット・アルコール・トッピング)。"""
    name = entry.get("name") or ""
    if "セット" in name:
        return "set"
    if entry.get("category") in KOURAKUEN_EXCLUDED_CATEGORIES:
        return "category"
    if entry.get("category") == "もう一品" and entry.get("price") is not None and entry["price"] <= KOURAKUEN_TOPPING_MAX_PRICE:
        return "topping"
    return None


def parse_kourakuen_menu(page: str) -> list[dict]:
    """/grandmenu/ の全商品(記録の対象外も含む)を、IDの重複なしで返す。
    product_id・name・price(税込)・category(「おすすめ」以外で最初に出たタブ名。「おすすめ」だけなら None)・
    in_recommended・notes(「15:00からの販売」などの注記)・official_url。商品が取れなければ空。"""
    page = _strip_html_comments(page)
    items: dict[str, dict] = {}
    for sec in re.split(r'<section class="c-menuListWrap[^"]*" data-category="', page)[1:]:
        t = re.search(r"<h2[^>]*>(.*?)</h2>", sec, re.S)
        tab = clean_text(t.group(1)) if t else ""
        for li in sec.split('<li class="c-menuListItem">')[1:]:
            a = re.search(r'info\.html\?id=(\d+)"', li)
            n = re.search(r'c-menuListTitle">(.*?)</p>', li, re.S)
            if not a or not n:
                continue
            pid = a.group(1)
            p = re.search(r'c-menuListPriceNum">([^<]*)', li)
            notes = [clean_text(x) for x in re.findall(r'menuListTagItem">(.*?)</li>', li, re.S)]
            cat = None if tab == "おすすめ" else tab
            cur = items.get(pid)
            if cur is None:
                items[pid] = {"product_id": pid, "name": clean_text(n.group(1)), "price": _yen_int(p.group(1)) if p else None,
                              "category": cat, "in_recommended": tab == "おすすめ", "notes": [x for x in notes if x],
                              "official_url": f"{KOURAKUEN_STORES_BASE}/grandmenu/info.html?id={pid}"}
            else:
                cur["category"] = cur["category"] or cat
                cur["in_recommended"] = cur["in_recommended"] or tab == "おすすめ"
    return list(items.values())


def parse_kourakuen_detail(page: str) -> dict | None:
    """info.html の栄養とアレルゲン。栄養の表もアレルゲンの表も無ければ None(構造変更)。
    戻り値: {"name", "price", "nutrition": {kcal, protein_g, fat_g, carbs_g, salt_g, ...}|None, "allergens": {contains, partial}|None}"""
    page = _strip_html_comments(page)
    title = re.search(r'menuMainTitle[^>]*>(.*?)</h1>', page, re.S)
    price = re.search(r'menuMainPriceNum">([^<]*)', page)

    def table(cls: str) -> list[tuple[str, str]]:
        m = re.search(r'class="%s [^"]*">(.*?)</div>\s*(?:<div class="allergyInfoMoreButton"|</section>)' % cls, page, re.S)
        blk = m.group(1) if m else ""
        return [(clean_text(k), clean_text(v)) for k, v in
                re.findall(r'menuDataTableTitle">(.*?)</dt>\s*<dd class="menuDataTableText">(.*?)</dd>', blk, re.S)]

    nut_rows = dict(table("nutritionFacts"))
    al_rows = table("allergyInfo")
    nutrition = None
    if nut_rows:
        def num(key):
            m = re.search(r"(\d[\d,]*(?:\.\d+)?)", nut_rows.get(key, ""))  # 「1,120Kcal」のカンマ
            return float(m.group(1).replace(",", "")) if m else None
        nutrition = {"kcal": num("熱量"), "protein_g": num("たんぱく質"), "fat_g": num("脂質"), "carbs_g": num("炭水化物"),
                     "sugar_g": None, "fiber_g": None, "salt_g": num("食塩相当量"), "other": {}}
        if nutrition["kcal"] is None:
            nutrition = None
    allergens = {"contains": [k for k, v in al_rows if v.strip()], "partial": []} if al_rows else None
    if nutrition is None and allergens is None:
        return None
    return {"name": clean_text(title.group(1)) if title else None, "price": _yen_int(price.group(1)) if price else None,
            "nutrition": nutrition, "allergens": allergens}


def parse_kourakuen_news(page: str) -> list[dict]:
    """www.kourakuen.co.jp/news を、新しい順の [{date(ISO), title, url(絶対), category}] にする。"""
    import urllib.parse
    out, seen = [], set()
    for li in _strip_html_comments(page).split("<li>")[1:]:
        a = re.search(r'<a\s+href="([^"]*)"', li)
        d = re.search(r'class="date[^"]*">\s*(\d{4})\.(\d{1,2})\.(\d{1,2})', li)
        t = re.search(r'class="title">(.*?)</div>', li, re.S)
        if not d or not t:
            continue
        date = f"{int(d.group(1)):04d}-{int(d.group(2)):02d}-{int(d.group(3)):02d}"
        url = urllib.parse.urljoin(KOURAKUEN_WWW_BASE + "/news", a.group(1).strip()) if a else ""
        title = clean_text(t.group(1)).replace("\n", " ")
        if (date, url, title) in seen:
            continue
        seen.add((date, url, title))
        out.append({"date": date, "title": title, "url": url, "category": ""})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


# 日高屋 --------------------------------------------------------------------

def hidakaya_excluded(entry: dict) -> str | None:
    name = entry.get("name") or ""
    if "セット" in name:
        return "set"
    if entry.get("price") is None:
        return "no_price"      # 価格欄が無い(商品名に価格が入っているお持ち帰りの冷凍品など)
    return None


def hidakaya_is_dated_id(pid: str) -> bool:
    """detail/20261009 のような8桁の日付形式のID(期間限定の商品ごとに別ページ。2026-11-18時点で1件だけ)。"""
    return bool(re.fullmatch(r"20\d{6}", pid or ""))


_HIDAKAYA_TAG_RE = re.compile(r"[【\[［]\s*(期間限定|店舗限定)\s*[】\]］]")


def hidakaya_split_name(raw: str) -> tuple[str, bool, bool]:
    """一覧の商品名の【期間限定】【店舗限定】(前後の空白・全角半角の揺れを許す)を外す。
    戻り値は (名前, 期間限定の印, 店舗限定の印)。【期間限定】は公式の限定の印(お知らせに出ない限定品がある。2025年のチゲ味噌ラーメン)。"""
    t = clean_text(raw)
    tags = {m.group(1) for m in _HIDAKAYA_TAG_RE.finditer(unicodedata.normalize("NFKC", t))}
    name = _HIDAKAYA_TAG_RE.sub("", t)  # 【】は全角のまま。角括弧([]・［］)の揺れも同じ印として外す
    name = re.sub(r"[\s　]+", " ", name).strip()
    return name, "期間限定" in tags, "店舗限定" in tags


def parse_hidakaya_list(page: str, category: str | None = None) -> list[dict]:
    """メニュー一覧(おすすめ・カテゴリ別)の商品。product_id(詳細のファイル名)・name・price(税込。無ければ None)・category・official_url。"""
    page = _strip_html_comments(page)
    out: dict[str, dict] = {}
    for li in page.split("<li>")[1:]:
        m = re.search(r'href="/hits/ja/menu/1/detail/([^"/]+)\.html"', li)
        nm = re.search(r"<dd>\s*<span>(.*?)</span>", li, re.S)
        if not m or not nm:
            continue
        pr = re.search(r'<span>([\d,]+)</span>\s*<span class="yen">', li)
        pid = m.group(1)
        name, period, store = hidakaya_split_name(nm.group(1))
        cur = out.get(pid)
        if cur is not None:
            cur["period_limited"] = cur["period_limited"] or period
            cur["store_limited"] = cur["store_limited"] or store
            continue
        out[pid] = {"product_id": pid, "name": name, "price": _yen_int(pr.group(1)) if pr else None,
                    "category": category, "period_limited": period, "store_limited": store,
                    "official_url": f"{HIDAKAYA_BASE}/hits/ja/menu/1/detail/{pid}.html"}
    return list(out.values())


def parse_hidakaya_detail(page: str) -> dict | None:
    """詳細ページ。栄養(カロリー・塩分相当量。「スープを含む」等の注記つき)とアレルゲン(テキストの列挙)。表が無ければ None。"""
    page = _strip_html_comments(page)
    title = re.search(r"<h1>(.*?)</h1>", page, re.S)
    spec = re.search(r'<div class="menu_spec">(.*?)</div>', page, re.S)
    if not spec:
        return None
    rows = {clean_text(k): v for k, v in re.findall(r"<th>(.*?)</th>\s*<td>(.*?)</td>", spec.group(1), re.S)}
    price = re.search(r'<li class="price">.*?<span>([\d,]+)</span>', page, re.S)

    def num_note(label: str):
        v = rows.get(label)
        if v is None:
            return None, None
        m = re.search(r"<span>(\d[\d,]*(?:\.\d+)?)</span>", v)
        note = clean_text(re.sub(r"<span>[\d.,]+</span>\s*(?:kcal|g)", "", v)).strip("（）() 　")
        return (float(m.group(1).replace(",", "")) if m else None), (note or None)

    kcal, note = num_note("カロリー")
    salt, note2 = num_note("塩分相当量")
    nutrition = None
    if kcal is not None:
        nutrition = {"kcal": kcal, "protein_g": None, "fat_g": None, "carbs_g": None, "sugar_g": None, "fiber_g": None,
                     "salt_g": salt, "other": {}}
    allergens = None
    if "アレルギー" in rows:
        txt = clean_text(rows["アレルギー"])
        allergens = {"contains": [x.strip() for x in re.split(r"[、,，]", txt) if x.strip()], "partial": []}
    if nutrition is None and allergens is None:
        return None
    return {"name": clean_text(title.group(1)) if title else None, "price": _yen_int(price.group(1)) if price else None,
            "nutrition": nutrition, "nutrition_note": note or note2, "allergens": allergens}


def parse_hidakaya_news(text: str) -> list[dict]:
    """news.json を、新しい順の [{date(ISO), title, url, category, body}] にする。"""
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("news.json is not a list")
    out = []
    for x in data:
        m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", str(x.get("display_date_from") or ""))
        if not m:
            continue
        out.append({"date": f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}",
                    "title": clean_text(x.get("news_title")).replace("\n", " "),
                    "url": (x.get("link") or "").strip(), "category": str(x.get("news_kbn") or ""),
                    "body": clean_text(x.get("news_body")), "news_id": x.get("news_id")})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


# お知らせ見出しの照合(両チェーン共通) -------------------------------------------

_RAMEN_DATE_RE = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日|(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")


def ramen_start_date(text: str, news_date: str) -> str | None:
    """見出し・本文の最初の日付(「11月4日（水）より」「10月9日(金)より」「6/2より」。全角数字はNFKCで半角に)を発売日(ISO)にする。
    年が無いときはお知らせの日付から補う(12月のお知らせの「1月8日」は翌年)。読めなければ None。"""
    import datetime
    t = unicodedata.normalize("NFKC", text or "")
    m = _RAMEN_DATE_RE.search(t)
    if not m:
        return None
    try:
        nd = datetime.date.fromisoformat(news_date)
    except ValueError:
        return None
    year, mo, dy = (m.group(1), m.group(2), m.group(3)) if m.group(2) else (None, m.group(4), m.group(5))
    try:
        d = datetime.date(int(year) if year else nd.year, int(mo), int(dy))
    except ValueError:
        return None
    if not year:
        if (nd - d).days > 200:
            d = d.replace(year=d.year + 1)
        elif (d - nd).days > 200:
            d = d.replace(year=d.year - 1)
    return d.isoformat()


def ramen_name_in_title(name: str, title: str) -> bool:
    """商品名が見出しの「」の中身と一致するか(括弧書き「（味噌・塩）」を外した形・中黒で並べた各項目とも照合)。
    「」が無い見出しは、正規化した部分一致(4文字以上)。"""
    n = normalize_for_match(name)
    if len(n) < 2:
        return False
    quotes = re.findall(r"[「『](.*?)[」』]", title or "")
    if not quotes:
        return len(n) >= 4 and n in normalize_for_match(title)
    cands = set()
    for q in quotes:
        for base in (q, re.sub(r"[（(][^）)]*[）)]", "", q)):
            cands.add(normalize_for_match(base))
            cands.update(normalize_for_match(p) for p in base.split("・"))
    return n in cands


def match_ramen_launch(name: str, news: list[dict], ref: str, before: int = RAMEN_NEWS_WINDOW_BEFORE_DAYS,
                       after: int = RAMEN_NEWS_WINDOW_AFTER_DAYS) -> dict | None:
    """商品名を見出しに含み、販売・発売・登場を告げる最新のお知らせ(ref の前 before 日〜後 after 日)。
    戻り値は {"news": {...}, "launch_date": ISO または None, "limited": 見出しに「期間限定」があるか}。
    発売日は見出しから、読めなければ本文(日高屋)から取る。見出しに商品名が無い本文だけの一致は認めない。"""
    import datetime
    try:
        c = datetime.date.fromisoformat(ref)
    except ValueError:
        return None
    lo, hi = c - datetime.timedelta(days=before), c + datetime.timedelta(days=after)
    for a in news:
        try:
            d = datetime.date.fromisoformat(a["date"])
        except ValueError:
            continue
        if not (lo <= d <= hi):
            continue
        t = a["title"]
        if not (_RAMEN_LAUNCH_WORDS.search(t) or "期間限定" in t) or _RAMEN_NOT_LAUNCH.search(t):
            continue
        if ramen_name_in_title(name, t):
            launch = ramen_start_date(t, a["date"]) or ramen_start_date(a.get("body") or "", a["date"])
            return {"news": a, "launch_date": launch, "limited": "期間限定" in t}
    return None


# ---------------------------------------------------------------------------
# ゼッテリア (www.zetteria.jp。2026-11-25追加、13チェーン目)
# ---------------------------------------------------------------------------
# 調査・方針(2026-11-25。実物で確認した事実):
#  ・運営は株式会社バーガー・ワン(ゼンショーグループ)。メニューは /menu/<カテゴリ>.html の静的HTMLで、
#    商品ブロックは div.p-block > a.link(href=/menu/<商品コード>.html)。商品コードはカテゴリ接頭辞+数字
#    (burger020097・zeppin010001・sweets050010・drink22・lunch10 など)。side24 だけは接頭辞が無く、
#    fair.html の「フェア サイド」に載り、リンク先(/menu/side24.html)が詳細ページそのもの。
#  ・一覧の価格は div.price-block の div.box(p.item=単品・S:・M: など、p.price=¥620・¥590~)。
#    「~」は一部店舗で価格が異なる印。詳細ページは div.main-block の中に h3.name・価格・説明文(div.desc)が
#    ある(発売日・販売期間の記載は無い)。
#  ・カテゴリはページ内の見出し(h2.title)。fair.html は「フェア バーガー」など、sweets_drink.html は
#    「フェア/デザート シェーキ/スイーツ/スープ/ドリンク」のように、1ページに複数の見出しがある。
#    morning.html の見出しには「モーニング　〜10:30AM」のような時間の注記が付く。
#  ・お知らせは /news/ の1ページに全件(li.news-item。日付は 2026.11.25 形式、種別は span.label)。
#    タブごとに同じ記事が重複して出る。商品発売のプレスリリースは PDF への直接リンク(/news/press/*.pdf)で、
#    発売日(「11月18日（水）から期間限定で」)・終了予定(「12月上旬まで販売予定です」)・【商品概要】
#    (商品名・価格)は PDF 本文にしか無い。見出しには商品名ではなくフェア名しか出ないことが多い。
#  ・プレスリリースPDFの商品名にはルビ(ふりがな)が付く。座標つき抽出(pdf_nutrition.extract_geometry)では
#    ルビは行の高さが約5.5pt(本文は9pt以上)なので、行の高さで落とせる。
#  ・robots.txt は 404(制限なしとして扱う。他チェーンと同じ作法)。利用規約に自動取得・スクレイピングを
#    禁じる条項は無かった(2026-11-25に確認)。
ZETTERIA_BASE = "https://www.zetteria.jp"
ZETTERIA_NEWS_PATH = "/news/"
# 巡回の起点にする一覧ページ(現行ナビにあるものだけ。旧ロッテリア時代の残存ページは辿らない)。(パス, 見出しが無いときのカテゴリ)
ZETTERIA_MENU_PAGES = [("/menu/fair.html", "フェア"), ("/menu/zeppin.html", "絶品バーガー"),
                       ("/menu/burger.html", "レギュラーバーガー"), ("/menu/morning.html", "モーニング"),
                       ("/menu/lunch.html", "ランチ"), ("/menu/dinner.html", "ディナー"),
                       ("/menu/side.html", "サイド"), ("/menu/sweets_drink.html", "スイーツ・ドリンク"),
                       ("/menu/wakuwaku.html", "わくわくセット")]
ZETTERIA_ALLERGEN_URL = "https://www.zetteria.jp/allergen/"
ZETTERIA_NUTRITION_PDF_URL = "https://images.zensho.co.jp/materials/zetteria/allergen/nutrition.pdf"


def _zetteria_clean_name(raw: str) -> str:
    """商品名の <br> は除去してつなげる(「柚子香る<br>舞茸竜田チキンバーガー」→「柚子香る舞茸竜田チキンバーガー」)。"""
    name = clean_text(raw).replace("\n", "")
    return re.sub(r"\s+", " ", name).strip()


def _zetteria_section_category(raw: str) -> str | None:
    """一覧ページの見出しをカテゴリ名にする。「モーニング　〜10:30AM」のような時間の注記は落とす。"""
    t = clean_text(raw)
    t = re.sub(r"\s*〜.*$", "", t).strip()
    return t or None


def _zetteria_price_boxes(block: str) -> list[dict]:
    """div.price-block 内の価格を、並び順の [{label, price, tilde}] で返す(label は「単品」「S:」など)。"""
    out = []
    for box in re.split(r'<div class="box">', block)[1:]:
        it = re.search(r'<p class="item">(.*?)</p>', box, re.S)
        pr = re.search(r'<p class="price">(.*?)</p>', box, re.S)
        if not pr:
            continue
        txt = clean_text(pr.group(1))
        price = _yen_int(txt)
        if price is None:
            continue
        out.append({"label": clean_text(it.group(1)) if it else "", "price": price,
                    "tilde": "~" in txt or "～" in txt})
    return out


def _zetteria_pick_price(boxes: list[dict]) -> tuple[int | None, list[dict] | None]:
    """代表価格(「単品」の箱。無ければ先頭)と、サイズ別の内訳(2件以上のときだけ)を返す。"""
    if not boxes:
        return None, None
    primary = next((b for b in boxes if b["label"] == "単品"), boxes[0])
    variants = [{"label": b["label"], "price": b["price"]} for b in boxes] if len(boxes) >= 2 else None
    return primary["price"], variants


def parse_zetteria_menu(page: str, category: str | None = None) -> list[dict]:
    """カテゴリ別一覧ページの商品。product_id・name・price(単品。無ければ先頭)・price_variants・price_tilde・
    category(ページ内の見出し。見出しより前のブロックは引数のカテゴリ)・official_url。
    同じ商品が1ページに複数回出ても1件にする。商品が取れなければ空。"""
    page = _strip_html_comments(page)
    out: dict[str, dict] = {}
    cur_cat = category
    for part in re.split(r'(<h2 class="title">.*?</h2>)', page, flags=re.S):
        m = re.fullmatch(r'<h2 class="title">(.*?)</h2>', part, flags=re.S)
        if m:
            cur_cat = _zetteria_section_category(m.group(1)) or cur_cat
            continue
        for block in part.split('<div class="p-block">')[1:]:
            a = re.search(r'href="/menu/([a-z0-9_]+)\.html"', block)
            n = re.search(r'<h3 class="name">(.*?)</h3>', block, re.S)
            if not a or not n:
                continue
            pid = a.group(1)
            if pid in out:
                continue
            boxes = _zetteria_price_boxes(block)
            price, variants = _zetteria_pick_price(boxes)
            out[pid] = {"product_id": pid, "name": _zetteria_clean_name(n.group(1)), "price": price,
                        "price_variants": variants, "price_tilde": any(b["tilde"] for b in boxes),
                        "category": cur_cat, "official_url": f"{ZETTERIA_BASE}/menu/{pid}.html"}
    return list(out.values())


def parse_zetteria_detail(page: str) -> dict | None:
    """詳細ページ(div.main-block の中)。name・price・price_variants・price_tilde・description(全文)。
    名前か価格が取れなければ None(構造変更)。セットメニューの欄(別商品の名前・価格)は読まない。"""
    page = _strip_html_comments(page)
    m = re.search(r'<div class="main-block[^"]*">(.*?)<div class="btn-block">', page, re.S)
    if not m:
        m = re.search(r'<div class="main-block[^"]*">(.*?)<div class="set-menu">', page, re.S)
    if not m:
        return None
    blk = m.group(1)
    n = re.search(r'<h3 class="name">(.*?)</h3>', blk, re.S)
    boxes = _zetteria_price_boxes(blk)
    if not n or not boxes:
        return None
    price, variants = _zetteria_pick_price(boxes)
    d = re.search(r'<div class="desc">\s*<p class="txt">(.*?)</p>', blk, re.S)
    return {"name": _zetteria_clean_name(n.group(1)), "price": price, "price_variants": variants,
            "price_tilde": any(b["tilde"] for b in boxes),
            "description": clean_text(d.group(1)) if d else None}


def parse_zetteria_news(page: str) -> list[dict]:
    """/news/ を、新しい順の [{date(ISO), title, url(絶対), category}] にする( parse_kourakuen_news と同形式)。
    タブごとに同じ記事が重複して出るので、重複は除く。"""
    import urllib.parse
    out, seen = [], set()
    for li in _strip_html_comments(page).split('<li class="news-item">')[1:]:
        a = re.search(r'<a[^>]+href="([^"]+)"', li)
        d = re.search(r'class="date">\s*(\d{4})\.(\d{1,2})\.(\d{1,2})', li)
        c = re.search(r'class="label[^"]*">(.*?)</span>', li, re.S)
        t = re.search(r'class="news-title">(.*?)</p>', li, re.S)
        if not d or not t:
            continue
        date = f"{int(d.group(1)):04d}-{int(d.group(2)):02d}-{int(d.group(3)):02d}"
        url = urllib.parse.urljoin(ZETTERIA_BASE + ZETTERIA_NEWS_PATH, a.group(1).strip()) if a else ""
        title = clean_text(t.group(1)).replace("\n", " ")
        if (date, url, title) in seen:
            continue
        seen.add((date, url, title))
        out.append({"date": date, "title": title, "url": url, "category": clean_text(c.group(1)) if c else ""})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def zetteria_excluded(entry: dict) -> str | None:
    """記録しない商品なら理由を返す。セット商品は記録する(オーナー決定。モーニング・ランチ・ディナー・
    わくわくセットはセットが中心のカテゴリで、外すと丸ごと空になる)。価格が無いものだけは記録しない。"""
    if not (entry.get("name") or "").strip():
        return "no_name"
    if entry.get("price") is None:
        return "no_price"
    return None


# プレスリリースPDF(座標つき抽出 → 発売日・終了予定・【商品概要】) -----------------

ZETTERIA_RUBY_MAX_HEIGHT = 7.0   # 行の高さ(pt)がこれ以下ならルビ(ふりがな)とみなして落とす。本文は9pt以上


def zetteria_press_text(pages: list[dict]) -> str:
    """座標つき抽出(pdf_nutrition.extract_geometry の戻り値)を、ルビを落として1本の文字列にする。
    行は y の近いものをまとめ、x の順につなげる。空白は全部除く(日本語は分かち書きしないので、
    行の途中で割れた商品名も元に戻る)。"""
    lines: list[str] = []
    for page in pages or []:
        rows: dict[int, list] = {}
        for x0, y0, x1, y1, t in page.get("texts") or []:
            if y1 - y0 <= ZETTERIA_RUBY_MAX_HEIGHT:
                continue  # ルビ
            rows.setdefault(round(y0 / 3), []).append((x0, t))
        for k in sorted(rows, reverse=True):
            lines.append("".join(t for _, t in sorted(rows[k])))
    return re.sub(r"\s+", "", "".join(lines))


_PRESS_PUBLISHED_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_PRESS_LAUNCH_RES = [re.compile(r"(\d{1,2})月(\d{1,2})日（[月火水木金土日]）から"),
                     re.compile(r"(\d{1,2})月(\d{1,2})日（[月火水木金土日]）より"),
                     re.compile(r"発売日[:：]?(\d{1,2})月(\d{1,2})日")]
_PRESS_END_RES = [re.compile(r"(\d{1,2})月(上旬|中旬|下旬)まで販売予定"),
                  re.compile(r"(\d{1,2})月(\d{1,2})日まで販売予定")]


def parse_zetteria_press(pages: list[dict], news_date: str | None = None) -> dict:
    """プレスリリースPDF(座標つき抽出)から、発表日・発売日・終了予定・【商品概要】の商品名と価格を読む。
    戻り値: {"published_date", "launch_date", "planned_end_text", "items": [{"name", "price"}], "text"}。
    発売日の年は、PDFの冒頭の発表日(無ければ news_date)から補う。"""
    text = zetteria_press_text(pages)
    published = None
    m = _PRESS_PUBLISHED_RE.search(text)
    if m:
        published = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    ref = published or news_date
    launch = None
    for rx in _PRESS_LAUNCH_RES:
        mm = rx.search(text)
        if mm and ref:
            launch = ramen_start_date(f"{mm.group(1)}月{mm.group(2)}日", ref)
            if launch:
                break
    end_text = None
    for rx in _PRESS_END_RES:
        mm = rx.search(text)
        if mm:
            end_text = mm.group(0)
            break
    items = []
    idx = text.find("【商品概要】")
    tail = text[idx:] if idx >= 0 else ""
    for mm in re.finditer(r"商品名[:：](.+?)価格[:：]([\d,]+)円", tail):
        name = mm.group(1).strip("：:・")
        if name:
            items.append({"name": name, "price": int(mm.group(2).replace(",", ""))})
    return {"published_date": published, "launch_date": launch, "planned_end_text": end_text,
            "items": items, "text": text}


def _zetteria_norm(s: str | None) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or ""))


def match_zetteria_press(name: str, price: int | None, press: dict | None) -> bool:
    """商品がこのプレスリリースの対象か。【商品概要】の商品名(空白を除いて正規化し、完全一致か包含)で照合し、
    外れたら「本文に商品名があり、価格も【商品概要】のどれかと一致する」複合条件でも照合する
    (PDFのテキストはルビや改行で商品名が割れることがあるため)。"""
    if not press:
        return False
    n = _zetteria_norm(name)
    if len(n) < 2:
        return False
    for it in press.get("items") or []:
        inm = _zetteria_norm(it.get("name"))
        if inm == n or (len(n) >= 4 and (n in inm or inm in n)):
            return True
    if price is not None and n in _zetteria_norm(press.get("text")):
        return any(it.get("price") == price for it in press.get("items") or [])
    return False


# ---------------------------------------------------------------------------
# ケンタッキー (www.kfc.co.jp / japan.kfc.co.jp。2026-11-25追加、14チェーン目)
# ---------------------------------------------------------------------------
# 調査・方針(2026-11-25。実物で確認した事実):
#  ・消費者向け www.kfc.co.jp/menu は Next.js 製だが、サーバーサイドHTMLに全カテゴリ分の
#    <section id="pc-category-<カテゴリ名>"> が入り、商品名・価格がテキストで読める。カードは
#    「商品画像(img.olo.kfc.co.jp/menu/<6桁コード>/…webp) → 商品名 <p> → 価格(¥と数値が別スパン)」の並び。
#    クラス名は Tailwind で変わりやすいので、パーサはクラス名ではなくこの並び順で掴む。
#    6カテゴリのセクションは定番のみ(チキン11・セット20・バーガー9・サイド9・ドリンク6・キッズ2 の計57件。
#    2026-11-25実測)。**期間限定の商品はカテゴリのセクションには載らず、「おすすめ」のセクションにだけ載る**
#    (実測: おすすめ17件のうち定番の重複が5件、残り12件が期間限定。カテゴリ別URLで取得し比べても
#    セクションの中身は同じで、変わるのは先頭のモバイル表示だけ)。そのため「おすすめ」も取得元にし、
#    カテゴリのセクションに無い商品は category="おすすめ" で記録する。商品詳細ページは無い。
#  ・www の robots.txt は /api/ などが Disallow。内部APIやメニューPDFのエンドポイントは使わず、
#    表示用HTMLだけを解析する。
#  ・発売日は法人サイト japan.kfc.co.jp のニュースリリースが正本。一覧 /news_release は最新10件
#    (li.news-table__list__item。日付・種別・見出し)。商品リリースの詳細には【商品概要】が定型で入り、
#    「■商品名」に商品名と価格(「名前」…490円 の列挙。セットは別商品として並び、内訳〔…〕が付く)、
#    「■発売日」に完全な日付(2026年11月25日(水))、終了は「※数量限定、なくなり次第販売終了」が原則。
#    【商品概要】の無いリリース(CM・新店など)は商品リリースではない。
#  ・法人の robots.txt は全許可。利用規約に自動取得の禁止条項は無かった(2026-11-25に確認)。
KFC_BASE = "https://www.kfc.co.jp"
KFC_CORP_BASE = "https://japan.kfc.co.jp"
KFC_RELEASE_LIST_PATH = "/news_release"
KFC_CATEGORIES = ["チキン", "セット", "バーガー", "サイド", "ドリンク", "キッズ"]
KFC_RECOMMENDED = "おすすめ"   # 期間限定の商品が載るセクション(カテゴリのセクションには載らない)


def kfc_menu_url(category: str) -> str:
    import urllib.parse
    return f"{KFC_BASE}/menu?category={urllib.parse.quote(category)}"


_KFC_CARD_IMG_RE = re.compile(r"olo\.kfc\.co\.jp/menu/(\d{6})/")


def parse_kfc_menu(page: str, category: str | None = None) -> list[dict]:
    """カテゴリ別メニューページの商品。product_id(画像URLの6桁コード)・name・price(税込)・
    category・official_url(カテゴリ別メニューURL)。
    ページ内の <section id="pc-category-<カテゴリ>"> だけを読む(モバイル表示と「おすすめ」の
    セクションは読まない)。セクションが無ければ空(構造変更)。商品名は公式表記のまま。"""
    page = _strip_html_comments(page)
    page = re.sub(r"<script\b.*?</script>", "", page, flags=re.S)   # RSCペイロード内の複製を読まない
    scope = page
    if category:
        m = re.search(r'<section id="pc-category-' + re.escape(category) + r'"[^>]*>(.*?)</section>', page, re.S)
        if not m:
            return []
        scope = m.group(1)
    out: dict[str, dict] = {}
    imgs = list(_KFC_CARD_IMG_RE.finditer(scope))
    for i, m in enumerate(imgs):
        pid = m.group(1)
        if pid in out:
            continue
        end = imgs[i + 1].start() if i + 1 < len(imgs) else len(scope)
        seg = scope[m.end():end]
        nm = re.search(r"<p[^>]*>(.*?)</p>", seg, re.S)
        pm = re.search(r"¥\s*</span>\s*<span[^>]*>([\d,]+)</span>", seg)
        if not nm or not pm:
            continue
        name = re.sub(r"\s+", " ", clean_text(nm.group(1))).strip()
        if not name:
            continue
        out[pid] = {"product_id": pid, "name": name, "price": int(pm.group(1).replace(",", "")),
                    "category": category,
                    "official_url": kfc_menu_url(category) if category else KFC_BASE + "/menu"}
    return list(out.values())


def parse_kfc_release_list(page: str) -> list[dict]:
    """法人サイトのリリース一覧(最新10件)を、新しい順の [{date(ISO), title, url(絶対), category}] にする
    (parse_zetteria_news と同形式)。重複は除く。"""
    out, seen = [], set()
    for li in _strip_html_comments(page).split('<li class="news-table__list__item')[1:]:
        a = re.search(r'href="(https://japan\.kfc\.co\.jp/news_release/\d+)"', li)
        d = re.search(r'datatime="(\d{4})-(\d{1,2})-(\d{1,2})', li)
        if not d:
            d = re.search(r'c-item-data__title__date[^>]*>\s*(\d{4})/(\d{1,2})/(\d{1,2})', li)
        c = re.search(r'c-item-data__title__cat[^>]*>\s*(.*?)\s*</p>', li, re.S)
        t = re.search(r'<dd[^>]*data-js="truncate"[^>]*>(.*?)</dd>', li, re.S)
        if not t:
            t = re.search(r"<dd[^>]*>(.*?)</dd>", li, re.S)
        if not a or not d or not t:
            continue
        url = a.group(1)
        if url in seen:
            continue
        seen.add(url)
        date = f"{int(d.group(1)):04d}-{int(d.group(2)):02d}-{int(d.group(3)):02d}"
        title = re.sub(r"\s+", " ", clean_text(t.group(1))).strip()
        out.append({"date": date, "title": title, "url": url,
                    "category": clean_text(c.group(1)) if c else ""})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def _kfc_detail_text(page: str) -> str:
    """リリース詳細の本文テキスト(<br>は改行。空行は詰める)。"""
    page = _strip_html_comments(page)
    page = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", "", page, flags=re.S)
    txt = clean_text(page)
    return "\n".join(ln.strip() for ln in txt.split("\n") if ln.strip())


_KFC_RELEASE_ITEM_RE = re.compile(r"「([^」]+)」\s*[…‥]+\s*([\d,]+)\s*円")
_KFC_RELEASE_DATE_RE = re.compile(r"■発売日\s*\n?\s*(\d{4})年(\d{1,2})月(\d{1,2})日")
_KFC_RELEASE_END_RE = re.compile(r"※[^\n。]*なくなり次第販売終了")


def parse_kfc_release_detail(page: str) -> dict | None:
    """商品リリースの詳細。【商品概要】が無ければ None(商品リリースではない)。
    戻り値: {"launch_date"(■発売日。ISO), "planned_end_text", "limited"(数量限定・期間限定の記載があるか),
             "items": [{"name", "price"}], "text"}。
    商品名と価格は【商品概要】から■発売日までの範囲で読む(セット内訳〔…〕は商品として読まない)。"""
    text = _kfc_detail_text(page)
    idx = text.find("【商品概要】")
    if idx < 0:
        return None
    tail = text[idx:]
    end_idx = tail.find("■発売日")
    scope = tail[:end_idx] if end_idx >= 0 else tail
    items, seen = [], set()
    for m in _KFC_RELEASE_ITEM_RE.finditer(scope):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            items.append({"name": name, "price": int(m.group(2).replace(",", ""))})
    if not items:
        return None
    launch = None
    dm = _KFC_RELEASE_DATE_RE.search(text)
    if dm:
        launch = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
    em = _KFC_RELEASE_END_RE.search(text)
    return {"launch_date": launch, "planned_end_text": em.group(0) if em else None,
            "limited": ("数量限定" in text or "期間限定" in text), "items": items, "text": text}


def kfc_excluded(entry: dict) -> str | None:
    """記録しない商品なら理由を返す。名前が無い・価格が無いものだけ(セット商品もそのまま記録する)。"""
    if not (entry.get("name") or "").strip():
        return "no_name"
    if entry.get("price") is None:
        return "no_price"
    return None


def _kfc_norm(s: str | None) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or ""))


def match_kfc_release(name: str, price: int | None, release: dict | None) -> bool:
    """商品がこのリリースの【商品概要】の対象か(match_zetteria_press と同じ考え方)。
    【商品概要】の商品名(空白を除いて正規化し、完全一致か包含)で照合し、外れたら
    「本文に商品名があり、価格も【商品概要】のどれかと一致する」複合条件でも照合する。"""
    if not release:
        return False
    n = _kfc_norm(name)
    if len(n) < 2:
        return False
    for it in release.get("items") or []:
        inm = _kfc_norm(it.get("name"))
        if inm == n or (len(n) >= 4 and (n in inm or inm in n)):
            return True
    text = release.get("text")
    if price is not None and text and n in _kfc_norm(text):
        return any(it.get("price") == price for it in release.get("items") or [])
    return False


# ---------------------------------------------------------------------------
# やよい軒 (www.yayoiken.com。2026-11-25追加、15チェーン目)
# ---------------------------------------------------------------------------
# 調査・方針(2026-11-25。実物で確認した事実):
#  ・商品の正本は都道府県別メニュー一覧 /menu_list/index/{県ID}(東京=13)。1ページに全タブ
#    (定食/丼・麺・お子様/朝食/サイド/おうち定食)の全商品がサーバーサイドHTMLで載る(東京142件)。
#    カードは li.c-content-index-menu-list__item で、名前(.c-content-index-menu-list__title。
#    肩書きは .c-content-index-menu-list__shoulder-top に分離)・価格(.c-content-index-menu-list__price。
#    税込)・詳細URL /menu_list/view/{県ID}/{商品ID} がテキストで読める。バッジは NEW(c-label__new)のみ。
#    都道府県差は実質なし(東京142件・北海道142件で共通137件。差分はアルコールの県別並行IDのみで、
#    共通商品の価格差は0件)なので、代表は東京にする。
#  ・「おすすめメニュー(期間限定)」はサイドタブ内の h2 セクション(2026-11-25実測2件)。h2 のセクション名は
#    カテゴリにはせず、所属タブをカテゴリにする。期間限定の判定はこのセクションへの掲載だけに使う。
#  ・発売日は自社サイトのニュース記事 /contents/news/{YYMMDD}_{slug}.html が正本。■商品概要 に
#    商品名と価格が列挙され(肩書き「～…～」は別行で前置。[テイクアウト] の接頭辞が肩書きの前や
#    独立行に付くこともある)、「・発売日」の次の行に 2026.11.17(火) の形で発売日が明記される。
#    発表日と発売日は約1週間ずれるので、発表日での代用はしない。発売前の商品はメニュー一覧に
#    載らないため、告知だけでは記録せず、一覧に現れたら state に残した記事情報で照合する。
#    ■商品概要 の無い記事(出店情報など)は商品記事ではない。記事後半の既存商品の再掲
#    (バリューメニュー紹介など)と照合しないよう、照合範囲は ■商品概要 から ・発売日 までに限る。
#  ・robots.txt は404(ファイルなし)=制限なし。サイトポリシーに自動取得の禁止条文は無い
#    (2026-11-25確認)。記録は事実情報のみで、画像は保存・表示しない。
YAYOIKEN_BASE = "https://www.yayoiken.com"
YAYOIKEN_MENU_PATH = "/menu_list/index/13"   # 東京を代表にする(都道府県差は実質なし)
YAYOIKEN_NEWS_PATH = "/news"
YAYOIKEN_TABS = ["定食", "丼・麺・お子様", "朝食", "サイド", "おうち定食"]
YAYOIKEN_LIMITED_SECTION = "おすすめメニュー（期間限定）"


def yayoiken_menu_url() -> str:
    return YAYOIKEN_BASE + YAYOIKEN_MENU_PATH


def yayoiken_news_url(page: int = 1) -> str:
    return YAYOIKEN_BASE + YAYOIKEN_NEWS_PATH if page <= 1 else f"{YAYOIKEN_BASE}{YAYOIKEN_NEWS_PATH}?page={page}"


_YAYOIKEN_ITEM_RE = re.compile(r'<li class="c-content-index-menu-list__item">')


def parse_yayoiken_menu(page: str) -> list[dict]:
    """東京のメニュー一覧(1ページ・全タブ)の商品。product_id(詳細URL末尾の数値ID)・name・
    price(税込。取れなければ None)・category(所属タブ)・section(h2の見出し)・is_new(NEWバッジ)・
    in_limited_section(「おすすめメニュー(期間限定)」セクションに載っているか)・official_url。
    商品名は公式表記のまま(肩書きは含めない)。タブのパネルが見つからなければ空(構造変更)。"""
    page = _strip_html_comments(page)
    page = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", "", page, flags=re.S)
    box = page.find('id="box-category"')
    if box < 0:
        return []
    panels = re.split(r'<li class="c-tab__cont-item[^"]*">', page[box:])[1:]
    out: dict[str, dict] = {}
    for tab, panel in zip(YAYOIKEN_TABS, panels):
        sections = re.split(r"<h2[^>]*>(.*?)</h2>", panel, flags=re.S)
        # sections は [前置き, 見出し, 本文, 見出し, 本文, ...]
        for i in range(1, len(sections), 2):
            section = re.sub(r"\s+", " ", clean_text(sections[i])).strip()
            body = sections[i + 1] if i + 1 < len(sections) else ""
            for chunk in _YAYOIKEN_ITEM_RE.split(body)[1:]:
                a = re.search(r'href="https://www\.yayoiken\.com/menu_list/view/\d+/(\d+)"', chunk)
                t = re.search(r'c-content-index-menu-list__title">(.*?)</p>', chunk, re.S)
                if not a or not t:
                    continue
                pid = a.group(1)
                if pid in out:
                    continue
                name = re.sub(r"\s+", " ", clean_text(t.group(1))).strip()
                if not name:
                    continue
                pm = re.search(r'c-content-index-menu-list__price">([\d,]+)</span>', chunk)
                out[pid] = {"product_id": pid, "name": name,
                            "price": int(pm.group(1).replace(",", "")) if pm else None,
                            "category": tab, "section": section,
                            "is_new": "c-label__new" in chunk,
                            "in_limited_section": section == YAYOIKEN_LIMITED_SECTION,
                            "official_url": f"{YAYOIKEN_BASE}/menu_list/view/13/{pid}"}
    return list(out.values())


_YAYOIKEN_NEWS_A_RE = re.compile(
    r'<a href="(https://www\.yayoiken\.com/contents/news/[^"]+\.html)"[^>]*>(.*?)</a>', re.S)


def parse_yayoiken_news_list(page: str) -> list[dict]:
    """ニュース一覧を、新しい順の [{date(ISO), title, url(絶対), category}] にする。重複は除く。"""
    out, seen = [], set()
    for m in _YAYOIKEN_NEWS_A_RE.finditer(_strip_html_comments(page)):
        url, body = m.group(1), m.group(2)
        if url in seen:
            continue
        d = re.search(r'data-datetime="(\d{4}-\d{1,2}-\d{1,2})"', body)
        t = re.search(r'c-media-list__title">(.*?)</div>', body, re.S)
        if not d or not t:
            continue
        seen.add(url)
        c = re.search(r'<li data-category="[^"]*">\s*<span>(.*?)</span>', body, re.S)
        date = "-".join(f"{int(x):0{w}d}" for x, w in zip(d.group(1).split("-"), (4, 2, 2)))
        out.append({"date": date, "title": re.sub(r"\s+", " ", clean_text(t.group(1))).strip(),
                    "url": url, "category": clean_text(c.group(1)) if c else ""})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def _yayoiken_detail_text(page: str) -> str:
    """ニュース記事の本文テキスト(<br>とブロック要素の終わりは改行。空行は詰める)。"""
    page = _strip_html_comments(page)
    page = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", "", page, flags=re.S)
    page = re.sub(r"</(p|li|tr|div|h[1-6])>", "\n", page, flags=re.I)
    txt = clean_text(page)
    return "\n".join(ln.strip() for ln in txt.split("\n") if ln.strip())


_YAYOIKEN_PRICE_LINE_RE = re.compile(r"^([\d,]+)円$")
_YAYOIKEN_LAUNCH_RE = re.compile(r"・発売日\s*\n\s*(\d{4})\.(\d{1,2})\.(\d{1,2})")
_YAYOIKEN_LAUNCH_JP_RE = re.compile(r"・発売日\s*\n?\s*(\d{4})年(\d{1,2})月(\d{1,2})日")


def _yayoiken_article_item_name(line: str) -> tuple[str, bool]:
    """■商品概要 の1行から、肩書き「～…～」を落とした商品名と、[テイクアウト] の接頭辞が
    付いていたかを返す。肩書きだけの行・接頭辞だけの行は名前が空になる。
    商品名は原文の表記のまま返す(照合側でNFKC正規化する)。"""
    s = line.strip()
    takeout = False
    for prefix in ("[テイクアウト]", "［テイクアウト］"):
        if s.startswith(prefix):
            takeout = True
            s = s[len(prefix):].strip()
            break
    s = re.sub(r"[～~][^～~]*[～~]", "", s).strip()
    return s, takeout


def parse_yayoiken_news_article(page: str) -> dict | None:
    """ニュース記事の詳細。■商品概要 が無ければ None(商品記事ではない)。
    戻り値: {"launch_date"(・発売日。ISO。読めなければ None), "items": [{"name", "price"}]}。
    商品名と価格は ■商品概要 から ・発売日 までの範囲で読む(記事後半の既存商品の再掲は読まない)。
    肩書き「～…～」は商品名から落とす。[テイクアウト] の接頭辞は、独立行・肩書きの前置の
    どちらの形でも次の商品名に付ける。"""
    text = _yayoiken_detail_text(page)
    idx = text.find("■商品概要")
    if idx < 0:
        return None
    tail = text[idx + len("■商品概要"):]
    end = tail.find("・発売日")
    scope = tail[:end] if end >= 0 else tail
    items, seen = [], set()
    pending_prefix = ""
    pending_name: str | None = None
    for line in scope.split("\n"):
        if line.startswith("■"):
            break   # 次の見出し(バリューメニュー紹介など)は ■商品概要 の範囲外
        pm = _YAYOIKEN_PRICE_LINE_RE.match(line.strip())
        if pm:
            if pending_name:
                key = (pending_name, int(pm.group(1).replace(",", "")))
                if key not in seen:
                    seen.add(key)
                    items.append({"name": pending_name, "price": key[1]})
            pending_name = None
            continue
        name, takeout = _yayoiken_article_item_name(line)
        if takeout:
            pending_prefix = "[テイクアウト]"
        if not name:
            continue   # 肩書きだけの行・接頭辞だけの行
        pending_name = pending_prefix + name
        pending_prefix = ""
    if not items:
        return None
    launch = None
    dm = _YAYOIKEN_LAUNCH_RE.search(text) or _YAYOIKEN_LAUNCH_JP_RE.search(text)
    if dm:
        launch = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
    return {"launch_date": launch, "items": items}


def yayoiken_excluded(entry: dict) -> str | None:
    """記録しない商品なら理由を返す。名前が無い・価格が無いものだけ(テイクアウト・セット・アルコールも記録する)。"""
    if not (entry.get("name") or "").strip():
        return "no_name"
    if entry.get("price") is None:
        return "no_price"
    return None


def _yayoiken_norm(s: str | None) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or ""))


def match_yayoiken_article(name: str, price: int | None, article: dict | None) -> bool:
    """商品がこの記事の■商品概要の対象か。商品名(空白を除いてNFKC正規化し、完全一致か包含)と
    価格の両方が一致したときだけ True(価格が読めない商品は名前だけでは照合しない)。"""
    if not article or price is None:
        return False
    n = _yayoiken_norm(name)
    if len(n) < 2:
        return False
    for it in article.get("items") or []:
        if it.get("price") != price:
            continue
        inm = _yayoiken_norm(it.get("name"))
        if inm == n or (len(n) >= 4 and (n in inm or inm in n)):
            return True
    return False


# ---------------------------------------------------------------------------
# CoCo壱番屋 (www.ichibanya.co.jp。2026-11-27追加、16チェーン目)
# ---------------------------------------------------------------------------
# 調査・方針(2026-11-27。実物で確認した事実):
#  ・商品の正本は公式メニューのカテゴリー別一覧 /menu/list.html?cid={ID}(16カテゴリー)。
#    サーバーサイドHTMLで、商品ブロック a.lst-product__item の中に名前
#    (h2.box-media__title)・価格 (p.box-media__price の「本体円（税込○円）」)・
#    詳細URL /menu/detail.html?id={数値ID} がテキストで読める。全カテゴリー合計120件・
#    カテゴリー間の重複なし(2026-11-27実測。ページ内の導線リンクに出るだけの商品は
#    数えない)。カテゴリー名は各一覧の <title> 先頭
#    (「肉類のカレー | …」)から取る。
#  ・期間限定は cid=2「期間/数量限定メニュー」の一覧に載った商品(実測11件)。
#  ・発売日は【予告】商品の詳細ページに「12月1日より販売開始！」の形で明記される
#    (販売開始後は詳細ページから日付の記載が消える)。年は書かれていないので、
#    巡回日を基準に決める (parse_cocoichi_detail_launch)。ニュース記事は使わない
#    ので、市販のコラボ菓子・監修商品は混入しない(一覧は店舗メニューだけ)。
#  ・商品名の【予告】は外して記録し、予告だったことは is_teaser で持つ。
#  ・トッピング (topping.html) と店舗限定メニュー (shop_original.html) は一覧に
#    含まれない別ページなので対象外。cid=6「地域限定メニュー」と cid=25
#    「夜定食メニュー（20店舗限定）」は通常のカテゴリー一覧に含まれるため記録する。
#  ・robots.txt は User-agent: * のみで制限なし。サイトポリシーに自動取得の
#    禁止条文は無い (2026-11-27確認)。記録は事実情報のみで、画像は保存・表示しない。
COCOICHI_BASE = "https://www.ichibanya.co.jp"
COCOICHI_LIMITED_CID = 2
COCOICHI_CATEGORY_CIDS = [1, 2, 3, 4, 5, 6, 7, 8, 12, 13, 14, 16, 22, 23, 24, 25]
COCOICHI_TEASER_PREFIX = "【予告】"


def cocoichi_list_url(cid: int) -> str:
    return f"{COCOICHI_BASE}/menu/list.html?cid={cid}"


def cocoichi_detail_url(product_id: str) -> str:
    return f"{COCOICHI_BASE}/menu/detail.html?id={product_id}"


_COCOICHI_ITEM_RE = re.compile(r'<a href="/menu/detail\.html\?id=(\d+)" class="lst-product__item')
_COCOICHI_TITLE_RE = re.compile(r'box-media__title">(.*?)</h2>', re.S)
_COCOICHI_PRICE_RE = re.compile(r'box-media__price"><span>[\d,]+</span>円（税込([\d,]+)円）')
_COCOICHI_CATEGORY_RE = re.compile(r"<title>([^<|]+)\|")


def parse_cocoichi_list(page: str) -> dict:
    """カテゴリー別一覧1ページの解析。{"category": <title>先頭のカテゴリー名(取れなければ
    None), "items": [{product_id, name, price(税込。取れなければ None), is_teaser,
    official_url}]}。商品名は【予告】を外した公式表記。ブロックが見つからなければ
    items は空(構造変更)。"""
    page = _strip_html_comments(page)
    page = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", "", page, flags=re.S)
    cm = _COCOICHI_CATEGORY_RE.search(page)
    category = re.sub(r"\s+", " ", clean_text(cm.group(1))).strip() if cm else None
    items: list[dict] = []
    seen: set[str] = set()
    chunks = _COCOICHI_ITEM_RE.split(page)
    # chunks は [前置き, id, 本文, id, 本文, ...]
    for i in range(1, len(chunks) - 1, 2):
        pid, body = chunks[i], chunks[i + 1]
        if pid in seen:
            continue
        tm = _COCOICHI_TITLE_RE.search(body)
        if not tm:
            continue
        name = re.sub(r"\s+", " ", clean_text(tm.group(1))).strip()
        if not name:
            continue
        seen.add(pid)
        is_teaser = name.startswith(COCOICHI_TEASER_PREFIX)
        if is_teaser:
            name = name[len(COCOICHI_TEASER_PREFIX):].strip()
        pm = _COCOICHI_PRICE_RE.search(body)
        items.append({"product_id": pid, "name": name,
                      "price": int(pm.group(1).replace(",", "")) if pm else None,
                      "is_teaser": is_teaser,
                      "official_url": cocoichi_detail_url(pid)})
    return {"category": category, "items": items}


_COCOICHI_LAUNCH_RE = re.compile(r"(\d{1,2})月(\d{1,2})日より販売開始")


def parse_cocoichi_detail_launch(page: str, today) -> str | None:
    """詳細ページの「○月○日より販売開始」から発売日(ISO)を返す。無ければ None。
    呼ばれるのは【予告】商品だけなので、発売日は基本的に未来。年は書かれていない
    ため巡回日 today (datetime.date) を基準に決める: 今年の日付が today より
    3日以上前なら、来年の日付とする(年末に翌年分が予告される場合を捌く。
    3日の猶予は巡回のタイミングのずれ用)。"""
    page = _strip_html_comments(page)
    page = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", "", page, flags=re.S)
    m = _COCOICHI_LAUNCH_RE.search(clean_text(page))
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    try:
        d = datetime.date(today.year, month, day)
    except ValueError:
        return None
    if (today - d).days > 3:
        try:
            d = datetime.date(today.year + 1, month, day)
        except ValueError:
            return None
    return d.isoformat()


def cocoichi_excluded(entry: dict) -> str | None:
    """記録しない商品なら理由を返す。名前が無い・価格が無いものだけ。"""
    if not (entry.get("name") or "").strip():
        return "no_name"
    if entry.get("price") is None:
        return "no_price"
    return None




# ---------------------------------------------------------------------------
# はなまるうどん (www.hanamaruudon.com。2026-11-27追加、17チェーン目)
# ---------------------------------------------------------------------------
# 調査・方針(2026-11-27。実物で確認した事実):
#  ・商品の正本は公式メニュー1ページ /menu/ (セクション7つ・全43件)。名前・価格とも
#    HTMLテキストで読める。セクションは id 属性 (limited / kakedashi / tsukedashi /
#    okonomi / gohan / other / takeout) で区切られ、カテゴリー名は見出し
#    (limited は h2、他は menu-sectitle の <em>) が正本。HTMLコメントには販売終了した
#    旧商品が残っているので、解析前にコメントを除く (セイコーマートと同じ注意)。
#  ・価格はサイズ別 (小・中・大)。一覧ではうどん系は「小」の価格だけ (「円～」表記)、
#    サイド系は単品価格 (「1個」「各種」などの単位注記つき) が出る。期間限定セクション
#    だけは一覧に全サイズの価格が載る。全サイズの価格と kcal (サイズ別) は商品詳細
#    ページ (NN_N.html) にあるので、初回記録のときだけ詳細を取る (丸亀方式)。
#  ・商品IDは詳細ページのファイル名 (02_2 など)。期間限定の商品には詳細ページが
#    無い (キャンペーンページが共通の飛び先)。メニュー画像のファイル名は枠ごとの
#    使い回し (次の季節の商品も img_campaign_menu を使う。コメント内の旧商品も
#    同じ) なので ID には使わず、商品名から作る (campaign_ + 正規化した名前の
#    sha1 先頭10文字。モルのレビュー指摘 2026-11-27)。同じ商品が次の季節に
#    戻ってきたら同じ ID になり、再発売として同じ記録に付く。
#  ・発売日は自社ニュース記事が正本 (やよい軒方式)。商品記事には「商品価格」
#    セクション (商品名+サイズ別価格) と「販売期間」があり、販売期間の開始日を
#    発売日とする。定期券のような販促記事には「商品価格」が無いので商品記事と
#    みなさない。メニューページの期間限定セクションにも開始日が出るが、発売日の
#    出どころはニュース記事に一本化する (オーナー決定 2026-11-27)。
#  ・robots.txt は /test/ のみ Disallow。サイトに自動取得の禁止条文は無い
#    (2026-11-27確認)。記録は事実情報のみで、画像は保存・表示しない。
HANAMARU_BASE = "https://www.hanamaruudon.com"
HANAMARU_MENU_PATH = "/menu/"
HANAMARU_NEWS_PATH = "/news/"
HANAMARU_CAMPAIGN_PATH = "/menu/campaign.html"
HANAMARU_LIMITED_KEY = "limited"
# (セクションの id, カテゴリー名の予備)。カテゴリー名はページの見出しから取る
HANAMARU_SECTIONS = [
    ("limited", "期間限定メニュー"),
    ("kakedashi", "かけうどん系"),
    ("tsukedashi", "ぶっかけ系"),
    ("okonomi", "お好みのうどん"),
    ("gohan", "ごはんもの"),
    ("other", "天ぷら・おでん・その他"),
    ("takeout", "テイクアウト専用"),
]


def hanamaru_campaign_id(name: str) -> str:
    """期間限定の商品ID。詳細ページが無い商品のために商品名から作る (画像ファイル名は
    枠ごとの使い回しで季節が変わると別商品と衝突するため使わない)。"""
    norm = _yayoiken_norm(name)
    return "campaign_" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def hanamaru_menu_url() -> str:
    return HANAMARU_BASE + HANAMARU_MENU_PATH


def hanamaru_news_url() -> str:
    return HANAMARU_BASE + HANAMARU_NEWS_PATH


def hanamaru_detail_url(product_id: str) -> str:
    return f"{HANAMARU_BASE}/menu/{product_id}.html"


_HANAMARU_ITEM_SPLIT_RE = re.compile(r'<li class="menu-allstar__list__item">')
_HANAMARU_NAME_RE = re.compile(r'menu-allstar__list__name">(.*?)</h3>', re.S)
_HANAMARU_LIST_PRICE_RE = re.compile(r'menu-allstar__list__price">(.*?)</div>', re.S)
_HANAMARU_PRICE_LABEL_RE = re.compile(r"<i>([^<]*)</i>")
_HANAMARU_PRICE_NUM_RE = re.compile(r"<span>([\d,]+)</span>")
_HANAMARU_LIMITED_PRICE_UL_RE = re.compile(r'menu-limited__price[^"]*">(.*?)</ul>', re.S)
_HANAMARU_PRICE_LI_RE = re.compile(r"<li><i>([^<]+)</i><span>([\d,]+)")
_HANAMARU_DETAIL_HREF_RE = re.compile(r'href="(?:/menu/)?(\d{2}_\d+)\.html"')
_HANAMARU_MAIN_TITLE_RE = re.compile(r'menu-limited__udon__title__text">(.*?)</span>', re.S)
_HANAMARU_SECTION_TITLE_RE = re.compile(r'menu-sectitle__text">.*?<em>([^<]+)</em>', re.S)
_HANAMARU_LIMITED_TITLE_RE = re.compile(r'h2-title2__jp">(.*?)<i', re.S)


def _hanamaru_prices_from_ul(ul_html: str) -> list[dict]:
    return [{"label": m.group(1).strip(), "price": int(m.group(2).replace(",", ""))}
            for m in _HANAMARU_PRICE_LI_RE.finditer(ul_html)]


def _hanamaru_primary_price(prices: list[dict]) -> int | None:
    """代表価格: 「小」→先頭 (一覧の表示価格が「小」基準のため)。"""
    for p in prices:
        if p["label"] == "小":
            return p["price"]
    return prices[0]["price"] if prices else None


def _hanamaru_allstar_item(chunk: str, section_key: str, category: str | None) -> dict | None:
    nm = _HANAMARU_NAME_RE.search(chunk)
    if not nm:
        return None
    name = re.sub(r"\s+", " ", clean_text(nm.group(1))).strip()
    if not name:
        return None
    prices: list[dict] = []
    ul = _HANAMARU_LIMITED_PRICE_UL_RE.search(chunk)
    if ul:
        prices = _hanamaru_prices_from_ul(ul.group(1))
    else:
        pd = _HANAMARU_LIST_PRICE_RE.search(chunk)
        if pd:
            lm = _HANAMARU_PRICE_LABEL_RE.search(pd.group(1))
            pm = _HANAMARU_PRICE_NUM_RE.search(pd.group(1))
            if pm:
                prices = [{"label": lm.group(1).strip() if lm else "",
                           "price": int(pm.group(1).replace(",", ""))}]
    if not prices:
        return None
    href = _HANAMARU_DETAIL_HREF_RE.search(chunk)
    if href:
        pid = href.group(1)
        official_url = hanamaru_detail_url(pid)
        detail_path = f"/menu/{pid}.html"
    elif section_key == HANAMARU_LIMITED_KEY:
        pid = hanamaru_campaign_id(name)   # 期間限定は詳細ページが無いので名前から作る
        official_url = HANAMARU_BASE + HANAMARU_CAMPAIGN_PATH
        detail_path = None
    else:
        return None
    return {"product_id": pid, "name": name, "category": category,
            "section": section_key, "price": _hanamaru_primary_price(prices),
            "prices": prices, "is_limited": section_key == HANAMARU_LIMITED_KEY,
            "is_new": False, "official_url": official_url, "detail_path": detail_path}


def parse_hanamaru_menu(page: str) -> dict:
    """メニュー1ページの解析。{"items": [{product_id, name, category, section,
    price(代表=小), prices([{label, price}]), is_limited, is_new, official_url,
    detail_path}]}。期間限定の特集商品 (menu-limited__udon) も含む。セクションの
    見出しが取れないカテゴリーは予備の名前を使う。構造が変わって取れなければ
    items は空。"""
    page = _strip_html_comments(page)
    page = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", "", page, flags=re.S)
    # セクションの開始位置 (id 属性) でページを切る
    marks: list[tuple[int, str, str]] = []
    for key, fallback in HANAMARU_SECTIONS:
        m = re.search(rf'id="{key}"', page)
        if m:
            marks.append((m.start(), key, fallback))
    marks.sort()
    items: list[dict] = []
    seen: set[str] = set()
    for idx, (pos, key, fallback) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(page)
        chunk_all = page[pos:end]
        if key == HANAMARU_LIMITED_KEY:
            tm = _HANAMARU_LIMITED_TITLE_RE.search(chunk_all)
            category = re.sub(r"\s+", " ", clean_text(tm.group(1))).strip() if tm else ""
        else:
            tm = _HANAMARU_SECTION_TITLE_RE.search(chunk_all)
            category = tm.group(1).strip() if tm else ""
        category = category or fallback
        if key == HANAMARU_LIMITED_KEY:
            # 特集商品 (詳細ページなし・全サイズ価格と開始日がこのセクションに出る)
            mm = _HANAMARU_MAIN_TITLE_RE.search(chunk_all)
            if mm:
                name = re.sub(r"\s+", " ", clean_text(mm.group(1))).strip()
                ul = _HANAMARU_LIMITED_PRICE_UL_RE.search(chunk_all)
                prices = _hanamaru_prices_from_ul(ul.group(1)) if ul else []
                if name and prices:
                    pid = hanamaru_campaign_id(name)
                    if pid not in seen:
                        seen.add(pid)
                        items.append({
                            "product_id": pid, "name": name, "category": category,
                            "section": key, "price": _hanamaru_primary_price(prices),
                            "prices": prices, "is_limited": True,
                            "is_new": "menu-limited__udon__title__new" in chunk_all,
                            "official_url": HANAMARU_BASE + HANAMARU_CAMPAIGN_PATH,
                            "detail_path": None})
        for chunk in _HANAMARU_ITEM_SPLIT_RE.split(chunk_all)[1:]:
            item = _hanamaru_allstar_item(chunk, key, category)
            if item and item["product_id"] not in seen:
                seen.add(item["product_id"])
                items.append(item)
    return {"items": items}


_HANAMARU_DETAIL_NAME_RE = re.compile(r'menusingle-top__title">.*?<span>(.*?)</span>', re.S)
_HANAMARU_SIZE_BLOCK_RE = re.compile(r'menusingle-top__info__size">(.*?)</div>', re.S)
_HANAMARU_SIZE_LABEL_RE = re.compile(r'size-icon">([^<]*)</i>')
_HANAMARU_SIZE_PRICE_RE = re.compile(r'size-text">(?:<small>[^<]*</small>)?\s*([\d,]+)\s*<small>円（税込）')
_HANAMARU_SIZE_KCAL_RE = re.compile(r'/\s*([\d,.]+)\s*kcal')


def parse_hanamaru_detail(page: str) -> dict:
    """商品詳細ページ。{name, sizes: [{label, price, kcal}]}。サイズが無い商品は
    ラベル空の1件。サイズブロックが読めなければ sizes は空。"""
    page = _strip_html_comments(page)
    page = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", "", page, flags=re.S)
    nm = _HANAMARU_DETAIL_NAME_RE.search(page)
    name = re.sub(r"\s+", " ", clean_text(nm.group(1))).strip() if nm else None
    sizes: list[dict] = []
    for m in _HANAMARU_SIZE_BLOCK_RE.finditer(page):
        block = m.group(1)
        pm = _HANAMARU_SIZE_PRICE_RE.search(block)
        if not pm:
            continue
        lm = _HANAMARU_SIZE_LABEL_RE.search(block)
        km = _HANAMARU_SIZE_KCAL_RE.search(block)
        kcal = None
        if km:
            try:
                kcal = float(km.group(1).replace(",", ""))
            except ValueError:
                kcal = None
        sizes.append({"label": lm.group(1).strip() if lm else "",
                      "price": int(pm.group(1).replace(",", "")), "kcal": kcal})
    return {"name": name, "sizes": sizes}


_HANAMARU_NEWS_DATE_RE = re.compile(r'news-index__date">(\d{2})\.(\d{1,2})\.(\d{1,2})<')
_HANAMARU_NEWS_CAT_RE = re.compile(r'news-index__category[^>]*>([^<]+)<')
_HANAMARU_NEWS_A_RE = re.compile(
    r'<a href="(https://www\.hanamaruudon\.com/news/\d{4}/[^"]+\.html)">(.*?)</a>', re.S)


def parse_hanamaru_news_list(page: str) -> list[dict]:
    """ニュース一覧を、新しい順の [{date(ISO), title, url, category}] にする。
    PDFへのリンクなど記事以外のものは含めない。重複は除く。"""
    page = _strip_html_comments(page)
    out, seen = [], set()
    for chunk in page.split('news-index__item"')[1:]:
        dm = _HANAMARU_NEWS_DATE_RE.search(chunk)
        am = _HANAMARU_NEWS_A_RE.search(chunk)
        if not dm or not am:
            continue
        url = am.group(1)
        if url in seen:
            continue
        seen.add(url)
        date = f"20{int(dm.group(1)):02d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
        title = re.sub(r"<br\s*/?>", " ", am.group(2))
        title = re.sub(r"\s+", " ", clean_text(title)).strip()
        cm = _HANAMARU_NEWS_CAT_RE.search(chunk)
        out.append({"date": date, "title": title, "url": url,
                    "category": clean_text(cm.group(1)).strip() if cm else ""})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


_HANAMARU_SIZE_PRICE_LINE_RE = re.compile(r"（小）([\d,]+)円\s*（中）([\d,]+)円\s*（大）([\d,]+)円")
_HANAMARU_PERIOD_RE = re.compile(r"販売期間\s*(\d{4})年(\d{1,2})月(\d{1,2})日[^\n]*?～\s*(.+)")
_HANAMARU_LAUNCH_FALLBACK_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日（[^）]*）より")


def _hanamaru_article_item_name(line: str) -> str:
    """商品価格セクションの商品名行。末尾の（温）（つけだし：…）などの注記は落とす。"""
    return re.sub(r"（[^）]*）$", "", line.strip()).strip()


def parse_hanamaru_news_article(page: str) -> dict | None:
    """ニュース記事の詳細。「商品価格」セクションが無ければ None (商品記事ではない。
    定期券のような販促記事はここで落ちる)。戻り値: {"launch_date"(販売期間の開始日。
    ISO。読めなければ None), "planned_end_text"(販売期間の終了側の表記。無ければ
    None), "items": [{"name", "price"(小), "prices": [{label, price}]}]}。"""
    text = _yayoiken_detail_text(page)
    idx = text.find("商品価格")
    if idx < 0:
        return None
    # 実物の商品概要は見出しと中身が同じ行にくっつく (「商品価格鴨ねぎうどん（温）」の形)
    # ので、「商品価格」より後ろのテキストを行に分けて読む
    lines = text[idx + len("商品価格"):].split("\n")
    items, seen = [], set()
    pending_name: str | None = None
    for line in lines:
        s = line.strip()
        if s.startswith(("販売店舗", "販売期間")) or s.startswith("■"):
            break
        sm = _HANAMARU_SIZE_PRICE_LINE_RE.search(s)
        if sm:
            if pending_name:
                prices = [{"label": lab, "price": int(sm.group(i).replace(",", ""))}
                          for i, lab in enumerate(("小", "中", "大"), start=1)]
                key = (pending_name, prices[0]["price"])
                if key not in seen:
                    seen.add(key)
                    items.append({"name": pending_name, "price": prices[0]["price"],
                                  "prices": prices})
            pending_name = None
            continue
        pm = re.fullmatch(r"([\d,]+)円", s)
        if pm:
            if pending_name:
                price = int(pm.group(1).replace(",", ""))
                key = (pending_name, price)
                if key not in seen:
                    seen.add(key)
                    items.append({"name": pending_name, "price": price,
                                  "prices": [{"label": "", "price": price}]})
            pending_name = None
            continue
        if not s or s.startswith("＊"):
            continue
        pending_name = _hanamaru_article_item_name(s)
    if not items:
        return None
    launch = None
    planned_end = None
    pm = _HANAMARU_PERIOD_RE.search(text)
    if pm:
        launch = f"{int(pm.group(1)):04d}-{int(pm.group(2)):02d}-{int(pm.group(3)):02d}"
        planned_end = pm.group(4).strip() or None
    else:
        fm = _HANAMARU_LAUNCH_FALLBACK_RE.search(text)
        if fm:
            launch = f"{int(fm.group(1)):04d}-{int(fm.group(2)):02d}-{int(fm.group(3)):02d}"
    return {"launch_date": launch, "planned_end_text": planned_end, "items": items}


def hanamaru_excluded(entry: dict) -> str | None:
    """記録しない商品なら理由を返す。名前が無い・価格が無いものだけ。"""
    if not (entry.get("name") or "").strip():
        return "no_name"
    if entry.get("price") is None:
        return "no_price"
    return None


def match_hanamaru_article(name: str, price: int | None, article: dict | None) -> bool:
    """商品がこの記事の「商品価格」の対象か。商品名 (空白を除いてNFKC正規化し、完全一致か
    包含) と「小」の価格の両方が一致したときだけ True (やよい軒と同じ照合)。"""
    if not article or price is None:
        return False
    n = _yayoiken_norm(name)
    if len(n) < 2:
        return False
    for it in article.get("items") or []:
        if it.get("price") != price:
            continue
        inm = _yayoiken_norm(it.get("name"))
        if inm == n or (len(n) >= 4 and (n in inm or inm in n)):
            return True
    return False


# ---------------------------------------------------------------------------
# 大戸屋 (www.ootoya.com。2026-11-27追加、18チェーン目)
# ---------------------------------------------------------------------------
# 実物の構造 (2026-11-27に実物で確認):
#  ・消費者向け公式サイトは www.ootoya.com (旧 www.ootoya.co.jp は接続不可、ootoya.jp は
#    コーポレートサイト)。robots.txt は空 (200・0バイト)。自動取得の禁止条文は無い。
#  ・メニューは店舗ごとのページ (/menu_list/index/{店舗ID}) で、店舗により商品・価格が違う。
#    基準店舗は新宿センタービル店 (27214、限定メニューあり・191件)。記録はこの店舗の
#    メニューとして残す (やよい軒が東京の一覧を正本にしたのと同じ考え方)。
#  ・商品カードは <a class="c-card-01" href=".../menu_list/view/{店舗ID}/{商品ID}">。
#    商品IDは末尾の数字。名前は h3.c-card-01__title、価格は p.c-card-01__price (定食価格)、
#    単品価格がある商品は dl.c-card-01__single (見出しは「単品」のみ) に入る。
#    カテゴリーは直前の h3.c-heading-no2。タブごとに同じ見出しが繰り返されるため、
#    商品IDで重複を除き、最初に出たカテゴリーを採用する。
#  ・発売日は公式ニュース (新着情報) の「商品情報」記事が正本。リードに
#    「YYYY年M月D日(曜)より、…「商品名」を販売いたします」、本文に商品名と定食価格・
#    単品価格、「販売期間について」に開始日〜終了日が書いてある。記事本文は CMS の
#    blob で、価格行は ● 付きで br 区切り。日付の「月」「日」は康熙部首 (⽉・⽇) で
#    書かれることがあるため、記事の解析前に NFKC 正規化する。
#  ・記事の商品名とメニューの商品名は表記がずれる (記事「大粒牡蠣フライ」→メニュー
#    「大粒牡蠣フライ4個」、記事「＜2個増量＞大粒牡蠣フライ (6個)」→メニュー
#    「大粒牡蠣フライ6個」)。照合は、正規化した名前 (先頭の＜＞〈〉注記と括弧記号を
#    落とす) の一致・包含 + 定食価格の一致で行う。
OOTOYA_BASE = "https://www.ootoya.com"
OOTOYA_STORE_ID = "27214"   # 新宿センタービル店 (基準店舗)
OOTOYA_LIMITED_CATEGORY = "限定メニュー"
OOTOYA_PRODUCT_CATEGORY = "商品情報"


def ootoya_menu_url() -> str:
    return f"{OOTOYA_BASE}/menu_list/index/{OOTOYA_STORE_ID}"


def ootoya_news_url() -> str:
    return OOTOYA_BASE + "/news/"


def ootoya_detail_url(product_id: str) -> str:
    return f"{OOTOYA_BASE}/menu_list/view/{OOTOYA_STORE_ID}/{product_id}"


_OOTOYA_HEADING_RE = re.compile(r'<h3 class="c-heading-no2">([^<]+)</h3>')
_OOTOYA_CARD_RE = re.compile(
    r'<a class="c-card-01" href="https://www\.ootoya\.com/menu_list/view/\d+/(\d+)">(.*?)</a>', re.S)
_OOTOYA_CARD_NAME_RE = re.compile(r'<h3 class="c-card-01__title">([^<]+)</h3>')
_OOTOYA_CARD_PRICE_RE = re.compile(r'<p class="c-card-01__price">([\d,]+)')
_OOTOYA_CARD_SINGLE_RE = re.compile(
    r'c-card-01__single-lead">([^<]+)</dt>\s*<dd class="c-card-01__single-price">([\d,]+)')
_OOTOYA_NEWS_ITEM_RE = re.compile(
    r'<a href="\./(\d+)\.html"[^>]*>\s*<div class="c-notice-01__post-info">\s*'
    r'<time datetime="(\d{4}-\d{2}-\d{2})"[^>]*>[^<]*</time>'
    r'<span class="[^"]*">([^<]+)</span>\s*</div>\s*'
    r'<p class="c-notice-01__post-text">([^<]+)</p>', re.S)
_OOTOYA_BADGE_RE = re.compile(r'c-badge-tag-\d+">([^<]+)</span>')
_OOTOYA_TEISHOKU_PRICE_RE = re.compile(r"定食価格\s*([\d,]+)\s*円")
_OOTOYA_PERIOD_RE = re.compile(
    r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*[(（][月火水木金土日][)）]\s*[～〜~]\s*"
    r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*[(（]([月火水木金土日])[)）]")
_OOTOYA_LEAD_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*[(（][月火水木金土日][)）]\s*より")


def _ootoya_price(s: str) -> int | None:
    try:
        return int(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_ootoya_menu(page: str) -> dict:
    """基準店舗のメニュー1ページ → {"items": [...]}。"""
    events: list[tuple[int, str, object]] = []
    for m in _OOTOYA_HEADING_RE.finditer(page):
        events.append((m.start(), "h", m.group(1).strip()))
    for m in _OOTOYA_CARD_RE.finditer(page):
        events.append((m.start(), "c", m))
    events.sort(key=lambda x: x[0])
    items: list[dict] = []
    seen: set[str] = set()
    category: str | None = None
    for _, kind, payload in events:
        if kind == "h":
            category = payload  # type: ignore[assignment]
            continue
        m = payload  # type: ignore[assignment]
        pid = m.group(1)
        if pid in seen:
            continue
        body = m.group(2)
        nm = _OOTOYA_CARD_NAME_RE.search(body)
        pm = _OOTOYA_CARD_PRICE_RE.search(body)
        if not nm or not pm:
            continue
        name = clean_text(nm.group(1))
        price = _ootoya_price(pm.group(1))
        if not name or price is None:
            continue
        seen.add(pid)
        prices = [{"label": "", "price": price, "default": True}]
        sm = _OOTOYA_CARD_SINGLE_RE.search(body)
        if sm and sm.group(1).strip() == "単品":
            single = _ootoya_price(sm.group(2))
            if single is not None:
                prices = [{"label": "定食", "price": price, "default": True},
                          {"label": "単品", "price": single}]
        items.append({
            "product_id": pid, "name": name, "category": category, "price": price,
            "prices": prices, "is_limited": category == OOTOYA_LIMITED_CATEGORY,
            "is_new": False, "official_url": ootoya_detail_url(pid),
        })
    return {"items": items}


def parse_ootoya_news_list(page: str) -> list[dict]:
    """新着情報の一覧 → [{date, category, title, url}] (ページの並び順=新しい順)。"""
    out: list[dict] = []
    for m in _OOTOYA_NEWS_ITEM_RE.finditer(page):
        out.append({"date": m.group(2), "category": m.group(3).strip(),
                    "title": clean_text(m.group(4)),
                    "url": f"{OOTOYA_BASE}/news/{m.group(1)}.html"})
    return out


def _ootoya_article_item_name(lines: list[str], price_idx: int) -> str | None:
    """定食価格の行からさかのぼって、その商品の名前らしい行を探す。
    見出し行の「」内か、文になっていない独立した名前行を拾う。"""
    for j in range(price_idx - 1, max(-1, price_idx - 9), -1):
        s = lines[j].strip()
        if not s or s.startswith("●") or "価格" in s:
            continue
        quoted = re.findall(r"「([^」]+)」", s)
        if quoted:
            return quoted[-1]
        if len(s) <= 40 and not s.endswith("。"):
            return s
    return None


def parse_ootoya_news_article(page: str) -> dict | None:
    """「商品情報」記事 → {"launch_date", "planned_end_text", "items": [{name, price}]}。
    商品記事でない (バッジが商品情報でない・発売日が無い・定食価格の商品が無い) ものは None。"""
    badge = _OOTOYA_BADGE_RE.search(page)
    if badge and badge.group(1).strip() != OOTOYA_PRODUCT_CATEGORY:
        return None
    text = unicodedata.normalize("NFKC", _yayoiken_detail_text(page))
    lines = text.split("\n")
    launch_date: str | None = None
    planned_end_text: str | None = None
    pm = _OOTOYA_PERIOD_RE.search(text)
    if pm:
        launch_date = f"{pm.group(1)}-{int(pm.group(2)):02d}-{int(pm.group(3)):02d}"
        planned_end_text = (f"{pm.group(4)}年{int(pm.group(5))}月{int(pm.group(6))}日"
                            f"({pm.group(7)})").replace("(", "（").replace(")", "）")
    if not launch_date:
        lm = _OOTOYA_LEAD_DATE_RE.search(text)
        if lm:
            launch_date = f"{lm.group(1)}-{int(lm.group(2)):02d}-{int(lm.group(3)):02d}"
    if not launch_date:
        return None
    items: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for i, line in enumerate(lines):
        m = _OOTOYA_TEISHOKU_PRICE_RE.search(line)
        if not m:
            continue
        price = _ootoya_price(m.group(1))
        name = _ootoya_article_item_name(lines, i)
        if not name or price is None or (name, price) in seen:
            continue
        seen.add((name, price))
        items.append({"name": name, "price": price})
    if not items:
        return None
    return {"launch_date": launch_date, "planned_end_text": planned_end_text, "items": items}


def _ootoya_norm_name(name: str | None) -> str:
    """照合用の名前の正規化。NFKC + 先頭の＜＞〈〉注記を落とし、括弧記号と空白を除く。"""
    n = unicodedata.normalize("NFKC", name or "")
    n = re.sub(r"^[<〈《][^>〉》]*[>〉》]", "", n.strip())
    n = n.replace("(", "").replace(")", "").replace("（", "").replace("）", "")
    return re.sub(r"\s+", "", n)


def match_ootoya_article(name: str, price: int | None, info: dict) -> bool:
    """メニューの商品名・定食価格が、ニュース記事の商品 (名前・定食価格) と一致するか。"""
    if price is None:
        return False
    target = _ootoya_norm_name(name)
    if not target:
        return False
    for it in info.get("items") or []:
        if it.get("price") != price:
            continue
        cand = _ootoya_norm_name(it.get("name"))
        if cand and (cand == target or cand in target or target in cand):
            return True
    return False


def ootoya_excluded(entry: dict) -> str | None:
    name = (entry.get("name") or "").strip()
    price = entry.get("price")
    if not name:
        return "no_name"
    if price is None:
        return "no_price"
    if price < 30 or price > 100_000:
        return "price_out_of_range"
    return None
