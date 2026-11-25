"""ケンタッキー(www.kfc.co.jp / japan.kfc.co.jp)の合成ページ(実物の構造を再現した最小形)。

実物の要点(2026-11-25に確認):
 ・メニューページは全カテゴリ分の <section id="pc-category-<カテゴリ>"> を含む1枚のページで、
   どのカテゴリのURLでも同じページが返る。先頭にはモバイル表示(おすすめ+選択中カテゴリの複製)がある。
 ・カテゴリのセクションは定番のみ。期間限定の商品は「おすすめ」のセクションにだけ載る
   (おすすめは定番の一部との混載)。
 ・カードは「商品画像(img.olo.kfc.co.jp/menu/<6桁コード>/…webp) → 商品名 <p> → 価格(¥と数値が別スパン)」。
 ・リリース一覧は li.news-table__list__item(日付は <time datatime="…">、種別、見出し)。
 ・商品リリースの詳細には【商品概要】があり、「■商品名」に「名前」…価格円 の列挙(セットには内訳〔…〕)、
   「■発売日」に完全な日付、終了は「※数量限定、なくなり次第販売終了」。
"""

from __future__ import annotations

import urllib.parse

BASE = "https://www.kfc.co.jp"
CORP = "https://japan.kfc.co.jp"
ROBOTS_URL = BASE + "/robots.txt"
CORP_ROBOTS_URL = CORP + "/robots.txt"
RELEASE_LIST = CORP + "/news_release"
REL_LIMITED = CORP + "/news_release/9001"
REL_NEWMENU = CORP + "/news_release/9002"
REL_CM = CORP + "/news_release/9003"

ROBOTS = "User-agent: *\nDisallow: /mypage\nDisallow: /api/\n"
CORP_ROBOTS = "User-agent: *\n"

# product_id: (名前, 価格)。カテゴリ別
ITEMS = {
    "チキン": [("900101", "合成チキン", 300), ("900102", "合成チキン2ピース", 580), ("900103", "合成パック", 1290)],
    "セット": [("900201", "合成セット", 890), ("900202", "合成よくばりセット", 1250)],
    "バーガー": [("900301", "合成バーガー", 490)],
    "サイド": [("900401", "合成ポテトS", 290), ("900402", "合成ポテトL", 420)],
    "ドリンク": [("900501", "合成ドリンクS", 270), ("900502", "合成新ドリンク", 320)],
    "キッズ": [("900601", "合成キッズセット", 590)],
}
# 「おすすめ」のセクションにだけ載る商品(期間限定。カテゴリのセクションには無い)
RECOMMENDED_ONLY = [("900302", "合成ポットパイ", 490), ("900303", "合成ポットパイセット", 950),
                    ("900999", "合成おすすめ限定", 700)]
ALL_PIDS = [pid for items in ITEMS.values() for pid, _n, _p in items] + [pid for pid, _n, _p in RECOMMENDED_ONLY]


def menu_url(category: str) -> str:
    return f"{BASE}/menu?category={urllib.parse.quote(category)}"


def _card(pid: str, name: str, price: int) -> str:
    return (f'<div class="card"><img src="https://img.olo.kfc.co.jp/menu/{pid}/x-800.webp" alt="{name}">'
            f'<p class="n">{name}</p><div class="p"><span>¥</span><span>{price:,}</span></div></div>')


def kfc_menu_page(drop: tuple = (), prices: dict | None = None) -> str:
    """全カテゴリ分のセクションを含むメニューページ(実物と同じく、どのカテゴリのURLでも同じ内容)。"""
    prices = prices or {}
    sections = []
    # モバイル表示(選択中カテゴリ=チキンの複製)と「おすすめ」(横断セレクション+おすすめ専用商品)
    chicken = "".join(_card(pid, n, prices.get(pid, p)) for pid, n, p in ITEMS["チキン"] if pid not in drop)
    sections.append(f'<div class="mobile">{chicken}</div>')
    rec = _card("900101", "合成チキン", prices.get("900101", 300)) if "900101" not in drop else ""   # 定番との混載(重複)
    rec += "".join(_card(pid, n, prices.get(pid, p)) for pid, n, p in RECOMMENDED_ONLY if pid not in drop)
    sections.append(f'<section id="pc-category-おすすめ">{rec}</section>')
    for cat, items in ITEMS.items():
        cards = "".join(_card(pid, n, prices.get(pid, p)) for pid, n, p in items if pid not in drop)
        sections.append(f'<section id="pc-category-{cat}">{cards}</section>')
    return "<html><body>" + "".join(sections) + "</body></html>"


def _release_item(date: str, cat: str, title: str, url: str) -> str:
    return (f'<li class="news-table__list__item" data-cat="notice"><a href="{url}" target="_self">'
            f'<div class="c-item-data"><div class="c-item-data__title">'
            f'<p class="c-item-data__title__date"><time datatime="{date}T04:00:00+09:00">{date.replace("-", "/")}</time></p>'
            f'<p class="c-item-data__title__cat c-item-data__title__cat--notice">{cat}</p></div>'
            f'<dl class="c-item-data__desc-wrap"><dd class="c-item-data__desc" data-js="truncate">{title}</dd></dl>'
            f'</div></a></li>')


def kfc_release_list() -> str:
    items = [
        _release_item("2026-11-22", "ブランド情報", "合成CMのお知らせ", REL_CM),
        _release_item("2026-11-20", "ブランド情報", "合成ポットパイを発売", REL_LIMITED),
        _release_item("2026-11-18", "ブランド情報", "合成新ドリンクを発売", REL_NEWMENU),
    ]
    return "<html><body><ul>" + "".join(items) + "</ul></body></html>"


def kfc_release_limited() -> str:
    return ("<html><body><div>"
            "<p>【商品概要】</p>"
            "<p>■商品名</p>"
            "<p>「合成ポットパイ」…490円</p>"
            "<p>「合成ポットパイセット」…950円<small>〔内訳〕「合成ポットパイ」・「合成ドリンクS」</small></p>"
            "<p>■発売日</p>"
            "<p>2026年11月25日(水)</p>"
            "<p>※数量限定、なくなり次第販売終了。</p>"
            "<p>■販売店舗</p>"
            "<p>全国のケンタッキー店舗</p>"
            "</div></body></html>")


def kfc_release_newmenu() -> str:
    return ("<html><body><div>"
            "<p>【商品概要】</p>"
            "<p>■商品名</p>"
            "<p>「合成新ドリンク」…320円</p>"
            "<p>■発売日</p>"
            "<p>2026年11月19日(木)</p>"
            "<p>■販売店舗</p>"
            "<p>全国のケンタッキー店舗</p>"
            "</div></body></html>")


def kfc_release_cm() -> str:
    return ("<html><body><div>"
            "<p>新しいCMを公開しました。出演は合成タレントです。</p>"
            "</div></body></html>")


def kfc_pages(menu: str | None = None) -> dict:
    """URL → ページ の対応(メニューは全カテゴリURLに同じページ)。"""
    page = kfc_menu_page() if menu is None else menu
    pages = {ROBOTS_URL: ROBOTS, CORP_ROBOTS_URL: CORP_ROBOTS,
             RELEASE_LIST: kfc_release_list(),
             REL_LIMITED: kfc_release_limited(), REL_NEWMENU: kfc_release_newmenu(), REL_CM: kfc_release_cm()}
    for cat in ITEMS:
        pages[menu_url(cat)] = page
    return pages
