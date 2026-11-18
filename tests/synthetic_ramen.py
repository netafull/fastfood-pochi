"""幸楽苑・日高屋用の合成フィクスチャ。

構造(クラス名・要素の並び)は実物の公式ページ(2026-11-18に確認)と同じだが、商品名・価格・日付・URL・説明文は創作したもの。
公式ページの実物はリポジトリに入れない方針(tests/fixtures/ は追跡しない)なので、テストはこれで書く。
"""

from __future__ import annotations

import json

KOU_STORES = "https://stores.kourakuen.co.jp"
KOU_WWW = "https://www.kourakuen.co.jp"
HID = "https://hidakaya.hiday.co.jp"

# 幸楽苑の stores 側の robots.txt は 404.html へ飛ばされ、HTMLが返る
KOU_ROBOTS_STORES_HTML = "<html><body><h1>404 Not Found</h1></body></html>"
KOU_ROBOTS_WWW = "User-agent: *\nDisallow:\n"
# 日高屋の robots.txt は、特定のボットだけを名指しで拒否する(* の記述が無い)
HID_ROBOTS = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: PerplexityBot\nDisallow: /\n"


# --- 幸楽苑 ---
def kou_item(pid, name, price, notes=()) -> str:
    tags = ""
    if notes:
        tags = '<ul class="menuListTag">' + "".join(f'<li class="menuListTagItem">{n}</li>' for n in notes) + "</ul>"
    return (f'<li class="c-menuListItem"><a class="c-menuListLink c-gridBox" href="../grandmenu/info.html?id={pid}">'
            f'<div class="c-gridImgBox"><img src="../grandmenu/opImage.php?id={pid}" alt="{name}" /></div><div>'
            f'<p class="c-menuListTitle">{name}</p><p class="c-menuListPriceWrap"><span class="c-menuListPrice">'
            f'<span class="c-menuListPriceNum">{price}</span><span>円</span></span><span>（税込）</span></p>{tags}</div></a></li>')


def kou_menu(sections: list[tuple[str, list[str]]]) -> str:
    out = ""
    for i, (title, items) in enumerate(sections):
        out += (f'<section class="c-menuListWrap{" -active" if i == 0 else ""}" data-category="menu{i}">'
                f'<h2 class="c-heading02 mb20">{title}</h2><ul class="c-menuList c-gridBox">\n{"".join(items)}</ul></section>')
    return f'<html><body><main><div class="c-menuListOuter c-gridBox">{out}</div></main></body></html>'


def kou_menu_default(extra=(), drop=()) -> str:
    A = kou_item
    secs = [
        ("おすすめ", [A("101", "合成トマトらーめん", 890), A("102", "合成担担麺", 860), A("103", "合成セット", 990),
                    A("111", "合成中華そば", 490)]),
        ("醤油らーめん", [A("111", "合成中華そば", 490), A("112", "合成ねぎらーめん", 630)]),
        ("味噌らーめん", [A("121", "合成味噌らーめん", 490)]),
        ("つけめん", [A("131", "合成つけめん", 1230)]),
        ("キッズ", [A("141", "合成お子様らーめん", 290), A("142", "合成お子様らーめんセット", 390)]),
        ("もう一品", [A("151", "合成餃子", 280), A("152", "合成味玉", 130), A("153", "合成にんにく", 20),
                    A("154", "合成チャーシュー盛り", 460)]),
        ("時間帯限定", [A("161", "合成野菜炒め", 590, ["15:00からの販売となります。"])]),
        ("アルコール", [A("171", "合成ビール", 480)]),
    ] + list(extra)
    secs = [(t, [i for i in items if not any(f"id={d}\"" in i for d in drop)]) for t, items in secs]
    return kou_menu(secs)


def kou_detail(name="合成中華そば", price=490, kcal="639Kcal", allergens=("卵", "小麦")) -> str:
    names = ["卵", "乳", "小麦", "えび", "かに", "そば", "落花生", "くるみ", "カシューナッツ"]
    al = "".join(f'<dl class="menuDataTable c-gridBox"><dt class="menuDataTableTitle">{n}</dt>'
                 f'<dd class="menuDataTableText">{"●" if n in allergens else ""}</dd></dl>' for n in names)
    nut = "".join(f'<dl class="menuDataTable c-gridBox"><dt class="menuDataTableTitle">{k}</dt>'
                  f'<dd class="menuDataTableText">{v}</dd></dl>'
                  for k, v in [("熱量", kcal), ("たんぱく質", "24.4g"), ("脂質", "16.6g"), ("炭水化物", "89.1g"), ("食塩相当量", "9.8g")])
    return f'''<html><body><main class="c-contents"><article><div class="menuContents"><div class="menuMain c-gridBox">
<h1 class="menuMainTitle mb10">{name}</h1>
<p class="menuMainPriceWrap"><span class="menuMainPrice"><span class="menuMainPriceNum">{price}</span><span>円</span></span><span>（税込）</span></p>
<p class="menuMainText">合成の説明文。保存しない。</p></div>
<div class="menuInfoWrap c-gridBox c-largeGap"><section><h3 class="c-heading03 mb15">アレルギー情報</h3>
<div class="allergyInfo menuDataTableWrap c-gridBox">{al}</div>
<div class="allergyInfoMoreButton"><form name="formAllergy"></form></div></section>
<section><h3 class="c-heading03 mb15">栄養成分表</h3><div class="nutritionFacts menuDataTableWrap c-gridBox">{nut}</div></section></div>
<p class="menuNote">※ 一部店舗では販売しておりません。</p></div></article></main></body></html>'''


def kou_news_item(date: str, title: str, url="https://www.kourakuen.co.jp/news/900") -> str:
    y, m, d = date.split("-")
    return (f'<li>\n<a\thref="{url}"\n>\n<div class="list-item"><div class="content-item">'
            f'<div class="date green-text">{y}.{m}.{d}</div><div class="title">{title}</div></div></div></a></li>')


def kou_news(extra=()) -> str:
    items = list(extra) + [
        kou_news_item("2026-11-10", "出店記念「合成トマトらーめん」が11月4日（水）より、登場！"),
        kou_news_item("2026-10-28", "「合成担担麺（味噌・塩）」11月4日（水）より、期間限定販売！", "https://www.kourakuen.co.jp/news/901"),
        kou_news_item("2026-11-16", "2026年11月16日（月） 幸楽苑 合成店 グランドオープン！", "https://www.kourakuen.co.jp/news/902"),
        kou_news_item("2026-06-23", "2026年6月30日（火）より「合成つけめん」期間限定で販売開始！", "https://www.kourakuen.co.jp/news/903"),
    ]
    return f'<html><body><div class="list-content"><ul class="list-unstyled">{"".join(items)}</ul></div></body></html>'


# --- 日高屋 ---
def hid_item(pid, name, price) -> str:
    price_html = f'<br/><span>{price}</span> <span class="yen">円</span><span class="tax">(税込)</span>' if price is not None else ""
    return (f'<li>\n<a href="/hits/ja/menu/1/detail/{pid}.html"><dl><dt><figure><img src="/hits/outimages/picture/x" alt="{name}" />'
            f'</figure></dt><dd><span>{name}</span>{price_html}</dd></dl></a>\n</li>')


def hid_list(title: str, items: list[str]) -> str:
    return (f'<html><body><div class="panel is-show"><article class="art01"><h1 class="tit03">{title}</h1>'
            f'<ul class="box01">{"".join(items)}</ul></article></div></body></html>')


HID_LIST_PATHS = {"rec": "/hits/ja/menu/1/list.html", 1: "/hits/ja/menu/1/list/1.html", 3: "/hits/ja/menu/1/list/3.html",
                  4: "/hits/ja/menu/1/list/4.html", 5: "/hits/ja/menu/1/list/5.html"}


def hid_pages(dated="20261009", extra=None, drop=()) -> dict[str, str]:
    A = hid_item
    ramen = [A("1", "合成中華そば", 420), A("2", "合成味玉中華そば", 540), A(dated, "合成濃厚豚骨ラーメン", 690)]
    teishoku = [A("18", "合成野菜炒め定食", 720)]
    single = [A("151", "合成半チャーハン", 310), A("197", "合成ライスセット", 250), A("139", "合成お子さまセット", 350)]
    otsumami = [A("30", "合成餃子（6個）", 300), A("808-1", "合成砂肝", 210), A("64", "合成冷凍唐揚げ1kg 税込1220円", None)]
    rec = [A("1", "合成中華そば", 420), A(dated, "合成濃厚豚骨ラーメン", 690), A("808-1", "合成砂肝", 210)]
    pages = {"rec": ("おすすめ", rec), 1: ("ラーメン", ramen), 3: ("定食", teishoku), 4: ("単品", single), 5: ("おつまみ", otsumami)}
    for k, items in (extra or {}).items():
        pages[k] = (pages[k][0], pages[k][1] + items)
    out = {}
    for k, (t, items) in pages.items():
        items = [i for i in items if not any(f"detail/{d}.html" in i for d in drop)]
        out[HID_LIST_PATHS[k]] = hid_list(t, items)
    return out


def hid_detail(name="合成中華そば", price=420, kcal="636", salt="6.4", note="（スープを含む)", allergens="大豆、卵、鶏肉、小麦粉") -> str:
    return f'''<html><body><div class="menu_body"><div class="menu_top"><div class="cont_r"><ul class="menu_name">
<li class="name"><h1>{name}</h1></li><li class="ex">合成の説明文。保存しない。</li>
<li class="price">
        <span>{price}</span> <span class="yen">円</span><span class="tax">(税込)</span>
</li></ul></div></div>
<div class="menu_spec"><table>
<tr><th>カロリー</th><td><span>{kcal}</span>kcal
{note}</td></tr>
<tr><th>塩分相当量</th><td><span>{salt}</span>g
{note}</td></tr>
<tr><th>アレルギー</th><td>{allergens}</td></tr><tr><th>その他</th><td></td></tr></table></div></div></body></html>'''


def hid_news(extra=()) -> str:
    base = [
        {"news_id": 345, "news_kbn": "2", "news_title": "【期間限定】合成濃厚豚骨ラーメン発売開始しました",
         "news_body": "10月９日(金)より「合成濃厚豚骨ラーメン」を期間限定で発売開始します。", "display_date_from": "2026/10/09",
         "link": "", "display_date_to": "2026/11/30"},
        {"news_id": 348, "news_kbn": "2", "news_title": "日高屋 合成店が10月28日（水）10時30分にオープンします",
         "news_body": "オープンします", "display_date_from": "2026/10/28", "link": "https://example.invalid/shop"},
        {"news_id": 322, "news_kbn": "1", "news_title": "テイクアウト商品一部販売休止のお知らせ", "news_body": "休止",
         "display_date_from": "2026/05/27", "link": ""},
    ] + list(extra)
    return json.dumps(base, ensure_ascii=False)
