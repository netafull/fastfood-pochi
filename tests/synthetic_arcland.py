"""かつや・からやま(アークランドの共通テンプレート)用の合成フィクスチャ。

構造(クラス名・要素の並び)は実物の公式ページと同じだが、商品名・価格・日付・URL は創作したもの。
公式ページの実物はリポジトリに入れない方針(tests/fixtures/ は追跡しない)なので、テストはこれで書く。
"""

from __future__ import annotations

ROBOTS = "User-agent: *\nDisallow: /cms/wp-admin/\n"


def menu_item(brand: str, pid: str, name: str, ex_tax: int, tax_in: str, icons=()) -> str:
    icon_html = ""
    if icons:
        icon_html = ('<ul class="menu-icon__list--abs">'
                     + "".join(f'<li class="menu-icon__item">\n<span class="menu-icon__txt">{i}</span>\n</li>' for i in icons)
                     + "</ul>")
    return f'''<li class="menu-list__item">
<a href="https://www.arclandservice.co.jp/{brand}/menu/{pid}/?identifier=" class="menu-list__link">
<div class="menu-list__thumb menu-icon" style="background-image: url(https://example.invalid/x.jpg);">
{icon_html}
</div>
<h3 class="menu-list__name">{name}</h3>
<div class="menu-price">
<div class="menu-price__inner">
<span class="menu-price__small--{brand}">￥</span><span class="menu-price__number--{brand}">{ex_tax}</span><p class="menu-price__tax">税込￥{tax_in}</p>
</div>
</div>
</a>
</li>'''


def menu_page(brand: str, sections: list[tuple[str, list[str]]], allergy="2026/09/20260904_allergy.pdf",
              menu_pdf="2026/10/menu-202610.pdf") -> str:
    """sections: [(セクション名, [menu_item(...)の文字列])]。"""
    nav = ""
    if allergy:
        nav += (f'<a href="/{brand}/wp-content/uploads/sites/2/{menu_pdf}" class="menu-list-nav__search" target="_blank">'
                '<span class="menu-list-nav__search-txt">メニュー表</span></a>')
        nav += (f'<a href="/{brand}/wp-content/uploads/sites/2/{allergy}" class="menu-list-nav__search" target="_blank">'
                '<span class="menu-list-nav__search-txt">アレルゲン一覧</span></a>')
    secs = "".join(
        f'<section class="menu-list__section js-tab-content js-active">\n<h2 class="menu-list__ttl">{t}</h2>\n'
        f'<ul class="menu-list__list">\n{"".join(items)}\n</ul>\n</section>'
        for t, items in sections)
    return (f'<html><body><div class="menu-list__wapper"><div class="menu-list-nav">{nav}'
            f'<ul class="menu-list-nav__tab"><li class="x">すべてのメニュー</li></ul></div>{secs}</div></body></html>')


def news_item(brand: str, date: str, title: str, url: str | None = "https://prtimes.jp/main/html/rd/p/000000000.000000001.html") -> str:
    y, m, d = date.split("-")
    inner = (f'<time class="information__date" datetime="{date}">{y}.{m}.{d}</time>'
             f'<span class="information__txt--{brand}">{title}</span>')
    if url is None:
        return f'<li class="information__item"><span class="information__link--txt">{inner}</span></li>'
    return (f'<li class="information__item"><a href="{url}" class="information__link" target="_blank" '
            f'rel="noopener noreferrer">{inner}</a></li>')


def news_page(items: list[str]) -> str:
    return f'<html><body><section class="information"><ul class="information__list">{"".join(items)}</ul></section></body></html>'


# --- かつや(合成) ---
def katsuya_menu(extra: list[str] | None = None, drop: set[str] = frozenset(), allergy="2026/09/20260904_allergy.pdf") -> str:
    k = "katsuya"
    limited = [menu_item(k, "90001", "合成ロース丼", 890, "979", ["期間限定"]),
               menu_item(k, "90002", "合成ロース定食", 990, "1,089", ["期間限定"])]
    don = [menu_item(k, "2510001", "合成カツ丼", 700, "770", ["おすすめ"]),
           menu_item(k, "2510002", "合成親子丼", 650, "715"),
           menu_item(k, "2607001", "合成ライト丼", 600, "660", ["NEW"]),
           menu_item(k, "2607002", "合成豚丼", 620, "682", ["NEW"])]
    side = [menu_item(k, "1341", "合成ソース小袋", 20, "22"), menu_item(k, "1343", "合成ご飯", 150, "165")]
    takeout = [menu_item(k, "90003", "合成ロース丼弁当", 890, "961", ["期間限定"]),
               menu_item(k, "1443", "合成ご飯", 150, "162")] + (extra or [])
    secs = [("期間限定", limited), ("丼", don), ("サイド", side), ("テイクアウト", takeout)]

    def keep(item: str) -> bool:
        return not any(f"/menu/{p}/" in item for p in drop)
    secs = [(t, [i for i in items if keep(i)]) for t, items in secs]
    return menu_page(k, secs, allergy=allergy)


def katsuya_news(extra: list[str] | None = None) -> str:
    k = "katsuya"
    items = (extra or []) + [
        news_item(k, "2026-10-06", "【期間限定】10月9日(金)～期間限定で「合成ロース丼」販売開始！"),
        news_item(k, "2026-09-25", "【10/2(金)オープン】かつや 合成店", url="https://shop.example.invalid/x"),
        news_item(k, "2026-07-23", "【新商品】「合成ライト丼」「合成豚丼」など新メニューを7月27日(月)販売開始！",
                  url="https://prtimes.jp/main/html/rd/p/000000000.000000002.html"),
        news_item(k, "2023-05-26", "「かつや」価格改定のお知らせ", url=None),
    ]
    return news_page(items)


# --- からやま(合成) ---
def karayama_menu(drop: set[str] = frozenset()) -> str:
    k = "karayama"
    limited = [menu_item(k, "19096", "合成とり天定食", 990, "1,089", ["期間限定"])]
    teishoku = [menu_item(k, "1042", "合成南蛮定食", 880, "968", []),
                menu_item(k, "1043", "合成ねぎ塩定食", 860, "946", [])]
    takeout = [menu_item(k, "19100", "合成とり天弁当", 990, "1,069", ["期間限定"]),
               menu_item(k, "2042", "合成南蛮弁当", 880, "950", [])]
    secs = [("期間限定", limited), ("定食", teishoku), ("テイクアウト", takeout)]
    secs = [(t, [i for i in items if not any(f"/menu/{p}/" in i for p in drop)]) for t, items in secs]
    return menu_page(k, secs, allergy="2026/08/allergy-260828.pdf")


def karayama_news() -> str:
    k = "karayama"
    return news_page([
        news_item(k, "2026-09-24", "【期間限定】9月25日(金)～期間限定で「合成とり天定食」販売開始！"),
        news_item(k, "2026-03-02", "【期間限定】3月6日(金)～期間限定で「合成南蛮定食」販売開始！"),
    ])
