"""丸亀製麺(jp.marugame.com。Next.js製。__NEXT_DATA__ のJSON)用の合成フィクスチャ。

構造(キー名・値の形)は実物の公式ページと同じだが、商品名・価格・栄養・アレルゲンは創作したもの。
公式ページの実物はリポジトリに入れない方針(tests/fixtures/ は追跡しない)なので、テストはこれで書く。
"""

from __future__ import annotations

import json

ROBOTS = "User-agent: *\nAllow: /\nSitemap: https://jp.marugame.com/sitemap.xml\n"

NAV = [("udon", "うどん"), ("tempura", "天ぷら"), ("gohanmono", "ご飯もの"), ("topping", "薬味・\nトッピング")]
NAV_TAKEOUT = [("udonbento", "うどん弁当")] + NAV

SIZE_UNITS = {"small": "小", "regular": "並", "large": "大", "xlarge": "得", "no_size": "", "piece": "1個"}


def next_page(props: dict) -> str:
    data = {"props": {"pageProps": props}, "page": "/menu/[category]", "query": {}, "buildId": "x"}
    return ('<!DOCTYPE html><html><head><title>x</title></head><body><div id="__next"></div>'
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data, ensure_ascii=False)}</script>'
            "</body></html>")


def entry(cat: str, slug: str, name: str, prices: dict, icons=(), attrs=None, description="合成の説明文です。二文目は載せません。",
          lead="合成のキャッチコピー", with_prices_list=True) -> dict:
    a = {"new_classic": False, "limited_time_item": False, "kids_12_and_under": False, "limited_kids": False,
         "mobile_order_disable": False, "takeout_available": False, "takeout_only": False, "shop_exclusive": False,
         "limited": False}
    a.update(attrs or {})
    e = {
        "id": slug, "createdAt": "2026-01-01T00:00:00.000Z", "updatedAt": "2026-01-02T00:00:00.000Z",
        "publishedAt": "2026-01-03T00:00:00.000Z", "revisedAt": "2026-01-04T00:00:00.000Z",
        "name": name, "lead": lead, "description": description,
        "attributes": a, "icon_select": list(icons),
        "price": dict({"fieldId": "price"}, **prices),
        "href": f"/menu/{cat}/{slug}/",
        "image": {"url": "https://example.invalid/x.jpg"},
    }
    if with_prices_list:
        e["prices"] = [{"key": k, "unit": SIZE_UNITS.get(k, k), "number": v, "default": k in ("regular", "no_size", "piece")}
                       for k, v in prices.items()]
    return e


def list_page(cat: str, shop=(), takeout=(), nav=NAV, nav_takeout=NAV_TAKEOUT) -> str:
    names = dict(NAV_TAKEOUT)
    return next_page({
        "currentCategory": {"id": cat, "name": names.get(cat, cat)},
        "shopMenuNavigation": [{"id": i, "name": n, "link": f"/menu/{i}/"} for i, n in nav],
        "takeoutMenuNavigation": [{"id": i, "name": n, "link": f"/menu/{i}/"} for i, n in nav_takeout],
        "menus": {"shop": list(shop), "takeout": list(takeout)},
    })


HEADERS = [("wheat", "小麦", "1"), ("buckwheat", "そば", "1"), ("egg", "卵", "1"), ("milk", "乳", "1"),
           ("shrimp", "えび", "1"), ("pork", "豚肉", "2"), ("sesame", "ごま", "2")]


def detail_page(cat: str, slug: str, name: str, nutrition_items=None, allergen_uses=("wheat",), odd_value=None,
                no_nutrition=False) -> str:
    items = nutrition_items if nutrition_items is not None else [
        {"temperature": "温", "size": "並", "energy": "400", "protein": "10", "fat": "5", "carbohydrate": "75",
         "sodium": "1200", "saltEquivalent": "3"},
        {"temperature": "温", "size": "大", "energy": "560", "protein": "14", "fat": "7", "carbohydrate": "105",
         "sodium": "1700", "saltEquivalent": "4.3"},
    ]
    alg = {"contentId": slug, "category": "x", "categoryEn": cat, "productsName": name}
    for hid, _n, _o in HEADERS:
        alg[hid] = "1" if hid in allergen_uses else "5"
    if odd_value is not None:
        alg["milk"] = odd_value
    props = {
        "detail": {"id": slug, "name": name},
        "allergenHeadersData": [{"id": hid, "name": n, "is_specified": s, "order": str(i + 1)}
                                for i, (hid, n, s) in enumerate(HEADERS)],
        "allergenItemData": alg,
    }
    if not no_nutrition:
        props["nutritionItemData"] = {"id": slug, "categoryId": cat, "productsName": name, "types": [], "items": items}
    return next_page(props)


# --- 丸亀製麺(合成) ---
def default_pages(extra_udon=(), drop=()) -> dict:
    """{パス: HTML}。期間限定1(合成限定うどん)・新定番1・定番(うどん2・天ぷら1・トッピング1・弁当1)。"""
    limited = entry("udon", "genteiudon", "合成限定うどん", {"regular": 800, "large": 990, "xlarge": 1180}, ["期間限定（limited_time_item）"])
    classic = entry("udon", "shinteiban", "合成新定番うどん", {"no_size": 900}, ["新定番（new_classic）"])
    kake = entry("udon", "kake", "合成かけうどん", {"small": 340, "regular": 440, "large": 630, "xlarge": 820})
    kama = entry("udon", "kama", "合成釜揚げ", {"regular": 390, "large": 580})
    udon = [e for e in [limited, classic, kake, kama, *extra_udon] if e["id"] not in drop]
    tempura = [entry("tempura", "ebi", "合成えび天", {"piece": 200})]
    topping = [entry("topping", "mentaiko", "合成トッピング明太子", {"piece": 90})]
    gohan = [entry("gohanmono", "mentaiko", "合成おにぎり明太子", {"piece": 170})]
    bento = [entry("udonbento", "teiban", "合成弁当", {"regular": 490, "large": 680}, attrs={"takeout_only": "お持ち帰り専用"})]
    pages = {
        "/menu/udon/": list_page("udon", udon, [e for e in udon if e["id"] in ("genteiudon", "kake")]),
        "/menu/tempura/": list_page("tempura", tempura, tempura),
        "/menu/gohanmono/": list_page("gohanmono", gohan, gohan),
        "/menu/topping/": list_page("topping", topping, topping),
        "/menu/udonbento/": list_page("udonbento", [], bento),
    }
    pages = {k: v for k, v in pages.items()}
    return pages


def detail_pages(*specs) -> dict:
    out = {}
    for cat, slug, name, kw in specs:
        out[f"/menu/{cat}/{slug}/"] = detail_page(cat, slug, name, **kw)
    return out
