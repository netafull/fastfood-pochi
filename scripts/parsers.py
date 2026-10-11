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


def matsuya_product_id(path: str, brand: str = "matsuya") -> str | None:
    """`/matsuya/menu/curry/kashmir_hp_261006.html` → `curry-kashmir_hp_261006`。
    末尾の6桁は掲載日(YYMMDD)のことが多く、同じ商品でも更新でslugが変わりうる。"""
    root = matsuya_menu_root(brand)
    if not path.startswith(root):
        return None
    rest = path[len(root):]
    rest = re.sub(r"\.html?$", "", rest)
    if not rest or rest.endswith("index"):
        return None
    pid = re.sub(r"[^0-9A-Za-z_\-]+", "-", rest.replace("/", "-")).strip("-")
    return pid or None


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
        pid = matsuya_product_id(path, brand)
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
