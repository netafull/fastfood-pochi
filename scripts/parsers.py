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
    r"(?:(本日)|(\d{1,2})月(\d{1,2})日)(?:[（(][^）)]{1,3}[）)])?(?:\s*\d{1,2}時(?:\d{1,2}分)?)?\s*(?:より|から)")
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
        return nd.isoformat()
    try:
        d = datetime.date(nd.year, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    if (nd - d).days > 200:
        d = d.replace(year=d.year + 1)
    elif (d - nd).days > 200:
        d = d.replace(year=d.year - 1)
    return d.isoformat()


def yoshinoya_title_matches(name: str, title: str) -> bool:
    """商品名が見出しの商品として載っているか。見出しに「」『』があれば、その中身と
    完全一致するときだけ(「さんま定食」が「牛さんま定食」の見出しに部分一致する誤爆を避ける)。
    括弧が無い見出しだけ、正規化した部分一致(4文字以上)。"""
    n = normalize_for_match(name)
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
