"""CoCo壱番屋(www.ichibanya.co.jp)の合成ページ。実物の構造を縮めて再現したもの。

公式ページのコピーはリポジトリに入れない。実サイトでの試行結果は作業レポートに残す。
一覧の商品ブロックは a.lst-product__item > h2.box-media__title + p.box-media__price
(「本体円（税込○円）」) の形、詳細ページの発売日は「○月○日より販売開始！」の形。
"""

from __future__ import annotations

BASE = "https://www.ichibanya.co.jp"
ROBOTS_URL = BASE + "/robots.txt"


def list_url(cid: int) -> str:
    return f"{BASE}/menu/list.html?cid={cid}"


def detail_url(pid: str) -> str:
    return f"{BASE}/menu/detail.html?id={pid}"


def _item(pid: str, name: str, price_excl: int, price_incl: int) -> str:
    return (
        f'<li class="grid-col--4 grid-col-sp--6 js-mh-menu">'
        f'<a href="/menu/detail.html?id={pid}" class="lst-product__item is-takeout">'
        f'<div class="box-media">'
        f'<p class="box-media__thumb"><img src="/sys/upload/save/x_370.png" alt="合成｜{name}"></p>'
        f'<div class="box-media__content">'
        f'<p class="box-media__tag txt-en">Synthetic</p>'
        f'<h2 class="box-media__title"><i class="fa fa-arrow-circle-right"></i>{name}</h2>'
        f'<p class="box-media__price"><span>{price_excl:,}</span>円（税込{price_incl:,}円）</p>'
        f"</div></div></a></li>"
    )


def list_page(category: str, items: list[str]) -> str:
    body = "".join(items)
    return (
        f'<!DOCTYPE html><html lang="ja"><head><title>{category} | ココイチのメニュー | '
        f"カレーハウスCoCo壱番屋</title></head><body>"
        f'<ul class="lst-product">{body}</ul>'
        f"</body></html>"
    )


# (cid, カテゴリー名, [(pid, 名前, 税抜, 税込)])
DEFAULT_LISTS = {
    1: ("肉類のカレー", [("101", "合成ポークカレー", 646, 710),
                          ("102", "合成ビーフカレー", 722, 794)]),
    2: ("期間/数量限定メニュー", [("201", "合成スープカレー", 1200, 1320),
                                    ("202", "【予告】合成冬野菜カレー", 1000, 1100)]),
    12: ("その他", [("301", "【予告】合成チーズカレーパン", 291, 320)]),
}
EMPTY_CATEGORIES = {3: "魚介類のカレー", 4: "野菜類のカレー", 5: "その他のカレーメニュー",
                    6: "地域限定メニュー", 7: "お子さまメニュー", 8: "特定原材料を使用していないカレー",
                    13: "デザート", 14: "ドリンク", 16: "サラダ", 22: "麺類",
                    23: "スモールカレー", 24: "ハヤシライス", 25: "夜定食メニュー（20店舗限定）"}
ALL_PIDS = ["101", "102", "201", "202", "301"]

DETAILS = {
    "202": "合成の説明です。12月5日より販売開始！ 数量限定です。",
    "301": "合成の説明です。12月1日より販売開始！ 揚げたてです。",
    "201": "合成の説明です。なくなり次第終了させていただきます。",
}


def detail_page(text: str) -> str:
    return ("<!DOCTYPE html><html lang=\"ja\"><head><title>合成商品 | ココイチのメニュー</title></head>"
            f"<body><p>{text}</p></body></html>")


def cocoichi_pages(lists=None, details=None) -> dict:
    lists = DEFAULT_LISTS if lists is None else lists
    details = DETAILS if details is None else details
    pages = {}
    for cid in [1, 2, 3, 4, 5, 6, 7, 8, 12, 13, 14, 16, 22, 23, 24, 25]:
        if cid in lists:
            cat, items = lists[cid]
            pages[list_url(cid)] = list_page(cat, [_item(*it) for it in items])
        else:
            pages[list_url(cid)] = list_page(EMPTY_CATEGORIES[cid], [])
    for pid, text in details.items():
        pages[detail_url(pid)] = detail_page(text)
    return pages
