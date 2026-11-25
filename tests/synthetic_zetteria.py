"""ゼッテリア用の合成フィクスチャ。

構造(クラス名・要素の並び)は実物の公式ページ(2026-11-25に確認)と同じだが、商品名・価格・日付・URL・説明文は創作したもの。
公式ページの実物はリポジトリに入れない方針(tests/fixtures/ は追跡しない)なので、テストはこれで書く。
プレスリリースPDFは、座標つき抽出(pdf_nutrition.extract_geometry)の戻り値の形で作る
(本文の行高11pt・ルビ5.5ptという実物の特徴も再現する)。
"""

from __future__ import annotations

BASE = "https://www.zetteria.jp"


def zet_item(pid, name, boxes) -> str:
    """一覧の商品ブロック。boxes は (ラベル, 価格文字列) の並び。"""
    box_html = "".join(
        f'<div class="box">\n<p class="item">{label}</p>\n<p class="price"><span class="yen">¥</span>{price}</p>\n</div>'
        for label, price in boxes)
    return (f'<div class="p-block">\n<a class="link c-box" href="/menu/{pid}.html">\n'
            f'<div class="p-img"><picture><img src="/images/menu/p_{pid}.png" alt=""></picture></div>\n'
            f'<div class="p-info">\n<h3 class="name">{name}</h3>\n'
            f'<div class="price-block">\n{box_html}\n</div>\n</div>\n</a>\n</div>')


def zet_menu_page(sections) -> str:
    """sections は (見出し, [商品ブロック]) の並び。"""
    out = ""
    for title, items in sections:
        out += (f'<div class="title-block">\n<p class="en">MENU</p>\n<h2 class="title">{title}</h2>\n</div>\n'
                f'<div class="p-menu">\n<div class="menu-block">\n{"".join(items)}\n</div>\n</div><!--/.p-menu-->\n')
    return (f'<html><body>\n<div class="notes-block-top"><p class="note">※一部店舗及びデリバリーでは価格が異なります。</p></div>\n'
            f'{out}</body></html>')


def zet_pages(fair_extra=(), drop=()) -> dict[str, str]:
    """全9ページぶんの一覧。drop は外す商品ID(ブロックごと取り除く)。"""
    A = zet_item
    pages = {
        "/menu/fair.html": zet_menu_page([
            ("フェア バーガー", [A("burger020901", "合成柚子<br>竜田バーガー", [("単品", "620")]),
                              A("burger020902", "合成タルタルバーガー", [("単品", "620")])]),
            ("フェア スイーツ・ドリンク", [A("sweets050901", "合成プリンシェーキ", [("単品", "490")])]),
            ("フェア サイド", [A("side24", "合成ごちそうポテト", [("単品", "390")])]),
        ]),
        "/menu/zeppin.html": zet_menu_page([
            ("絶品バーガー", [A("zeppin010901", "合成絶品バーガー", [("単品", "590~")])]),
        ]),
        "/menu/burger.html": zet_menu_page([
            ("レギュラーバーガー", [A("burger020902", "合成タルタルバーガー", [("単品", "620")]),
                                 A("burger020903", "合成レギュラーバーガー", [("単品", "440")]),
                                 A("burger020904", "合成チーズバーガー", [("単品", "460")])]),
        ]),
        "/menu/morning.html": zet_menu_page([
            ("モーニング　〜10:30AM", [A("morning010901", "合成モーニングバーガーセット", [("単品", "500")])]),
            ("バーガー", [A("burger020905", "合成朝バーガー", [("単品", "380")])]),
        ]),
        "/menu/lunch.html": zet_menu_page([
            ("ランチ", [A("lunch10", "合成ランチセット", [("単品", "700")])]),
        ]),
        "/menu/dinner.html": zet_menu_page([
            ("ディナーセット", [A("dinner07", "合成ディナーセット", [("単品", "900")])]),
            ("ディナー", [A("burger020906", "合成ディナーバーガー", [("単品", "550")])]),
        ]),
        "/menu/side.html": zet_menu_page([
            ("サイド", [A("side010901", "合成ポテト", [("S:", "250"), ("M:", "340")]),
                        A("side010902", "合成サラダ", [("単品", "300~")])]),
        ]),
        "/menu/sweets_drink.html": zet_menu_page([
            ("フェア", [A("sweets050901", "合成プリンシェーキ", [("単品", "490")])]),
            ("デザート シェーキ", [A("sweets050902", "合成シェーキ", [("S:", "250"), ("M:", "340")])]),
            ("スイーツ", [A("sweets050903", "合成チュロ", [("単品", "240~"), ("ハーフ", "150~")])]),
            ("スープ", [A("soup010901", "合成スープ", [("単品", "320")])]),
            ("ドリンク", [A("drink22", "合成コーラ", [("単品", "200")])]),
        ]),
        "/menu/wakuwaku.html": zet_menu_page([
            ("わくわくセット", [A("wakuwaku010901", "合成わくわくセット", [("単品", "530")])]),
        ]),
    }
    pages["/menu/fair.html"] = pages["/menu/fair.html"].replace(
        "</body></html>", "".join(fair_extra) + "</body></html>")
    for pid in drop:
        for path in pages:
            parts = pages[path].split('<div class="p-block">')
            pages[path] = parts[0] + "".join(
                '<div class="p-block">' + seg for seg in parts[1:]
                if f'href="/menu/{pid}.html"' not in seg)
    return {BASE + p: h for p, h in pages.items()}


def zet_detail(name="合成柚子竜田バーガー", boxes=(("単品", "620"),),
               desc="合成の説明文です。2文目もあります。") -> str:
    box_html = "".join(
        f'<div class="box">\n<div class="txt">\n<p class="item">{label}</p>\n'
        f'<p class="price"><span class="yen">¥</span>{price}</p>\n</div>\n</div>'
        for label, price in boxes)
    return f'''<html><body>
<ul class="breadcrumbs"><li class="nav"><span class="title">{name}</span></li></ul>
<section class="contents">
<div class="main-menu">
<div class="main-block c-box">
<div class="main-info">
<h3 class="name">{name}</h3>
<div class="price-block">
{box_html}
</div>
<div class="desc">
<p class="txt">{desc}</p>
</div>
</div>
</div>
</div>
<div class="btn-block">
<div class="allergen-btn"><a class="btn" href="/allergen/">アレルゲンについて</a></div>
<div class="nutrition-btn"><a class="btn ext" href="https://images.zensho.co.jp/materials/zetteria/allergen/nutrition.pdf" target="_blank">栄養成分一覧表</a></div>
</div>
<div class="set-menu">
<div class="subtitle-block"><h2 class="title">セットメニュー</h2></div>
<h3 class="subtitle-price">+¥350</h3>
<div class="menu-block">
<div class="set-block"><div class="box c-box"><h3 class="name">ポテトM＋ドリンクM</h3></div></div>
</div>
</div>
</section>
</body></html>'''


def zet_news_item(date: str, category: str, title: str, url: str) -> str:
    y, m, d = date.split("-")
    return (f'<li class="news-item">\n<a class="link" href="{url}" rel="noopener noreferrer">\n'
            f'<div class="item">\n<p class="date">{y}.{int(m):02d}.{int(d):02d}</p>\n'
            f'<p class="cat"><span class="label c01">{category}</span></p>\n</div>\n'
            f'<p class="news-title">{title}</p>\n</a>\n</li>')


PRESS_URL = "/news/press/gosei_fair_202611.pdf"
PRESS_TITLE = "ゼッテリア「合成フェア 第1弾」開催"


def zet_news(extra=(), press_url=PRESS_URL, press_date="2026-11-11", press_title=PRESS_TITLE) -> str:
    items = [
        zet_news_item(press_date, "プレスリリース", press_title, press_url),
        zet_news_item("2026-11-11", "お知らせ", "合成の募金を寄託しました", "/news/20261111.html"),
        zet_news_item("2026-10-01", "プレスリリース", "ゼッテリア「合成旧フェア」開催", "/news/press/gosei_old_202610.pdf"),
    ] + list(extra)
    # 実物と同じく、タブごとに同じ記事が重複して出る
    return (f'<html><body>\n<div class="news-list c-box js-tab-cts is-active" data-tab="all">\n<ul class="list">\n'
            f'{"".join(items)}\n</ul>\n</div>\n'
            f'<div class="news-list c-box js-tab-cts" data-tab="press">\n<ul class="list">\n'
            f'{"".join(i for i in items if "プレスリリース" in i)}\n</ul>\n</div>\n</body></html>')


# --- プレスリリースPDF(座標つき抽出の形) ---

def _line(y, text, x=36.0, h=11.0):
    return [x, y, x + 9.0 * len(text), y + h, text]


def zet_press_pages(name="合成柚子竜田バーガー", price=620,
                    launch="11月18日（水）から期間限定で",
                    end="※12月上旬まで販売予定です。なくなり次第終了します。",
                    published="2026年11月11日") -> list[dict]:
    """実物のプレスリリース(和バーガーフェア)の構造を縮めた座標データ。ルビの行(高さ5.5)も入れる。"""
    page0 = [
        _line(790, published, x=461.5, h=9.9),
        _line(772, "株式会社バーガー・ワン", x=443.6, h=9.9),
        _line(735, "ゆ", x=104.3, h=5.5),
        _line(735, "ず", x=123.1, h=5.5),
        _line(717, "合成フェア", x=99.5, h=18.0),
        _line(679, "ゼッテリア「合成フェア 第1弾」開催", x=84.5, h=22.0),
        _line(624, f"ショップ「ゼッテリア」は、{launch}、「合成フェア 第1弾」を開催します。"),
        _line(111, end, x=36.0, h=9.0),
    ]
    page1 = [
        _line(787, "【商品概要】", h=14.0),
        _line(770, "New!!", x=43.0, h=12.0),
        _line(774, "ゆ", x=313.9, h=5.5),
        _line(763, "商 品 名：合成柚子", x=255.8),
        _line(763, "竜田バーガー", x=400.0),
        _line(748, "価", x=255.8),
        _line(748, f"格：{price} 円", x=288.6),
        _line(730, "商品概要：合成の商品概要です。", x=255.8),
        _line(460, "以上", x=526.0),
    ]
    return [{"texts": page0, "rules": [], "w": 595.3, "h": 841.9},
            {"texts": page1, "rules": [], "w": 595.3, "h": 841.9}]


def zet_press_dict(name="合成柚子竜田バーガー", price=620, launch_date="2026-11-18",
                   planned_end_text="12月上旬まで販売予定", published="2026-11-11") -> dict:
    """parse_zetteria_press の戻り値と同じ形(crawl のテストで fetch_zetteria_press を差し替えるときに使う)。"""
    return {"published_date": published, "launch_date": launch_date, "planned_end_text": planned_end_text,
            "items": [{"name": name, "price": price}],
            "text": f"{published}株式会社バーガー・ワン「{name}」価格:{price}円"}
