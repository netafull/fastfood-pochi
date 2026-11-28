"""やよい軒(www.yayoiken.com)の合成ページ(実物の構造を再現した最小形)。

実物の要点(2026-11-25に確認):
 ・メニュー一覧 /menu_list/index/13(東京)は1ページに全タブ(定食/丼・麺・お子様/朝食/サイド/
   おうち定食)のパネルを持ち、各パネルは h2 のセクションに分かれる。カードは
   li.c-content-index-menu-list__item で、肩書き(.shoulder-top)・名前(.title)・
   価格(.price、税込)・詳細URL /menu_list/view/13/{商品ID} がテキストで読める。
   バッジは NEW(c-label__new)のみ。「おすすめメニュー(期間限定)」はサイドタブ内のセクション。
 ・ニュース一覧 /news は1ページ10件(?page=N で次ページ)。各行に data-datetime の日付、
   カテゴリ、見出しが入る。
 ・ニュース記事の ■商品概要 には「～肩書き～」の行+商品名の行+価格の行が列挙され、
   「・発売日」の次の行に 2026.11.17(火) の形で発売日が入る。[テイクアウト] の接頭辞は
   肩書きの前や独立行に付く。記事後半には既存商品の再掲(バリューメニュー紹介)がある。
 ・robots.txt は404(ファイルなし)=制限なし。
"""

from __future__ import annotations

BASE = "https://www.yayoiken.com"
ROBOTS_URL = BASE + "/robots.txt"
MENU_URL = BASE + "/menu_list/index/13"
INFO_URL = BASE + "/menu_list/info/13"
NEWS_URL = BASE + "/news"
NEWS2_URL = BASE + "/news?page=2"
ART_CHIGE = BASE + "/contents/news/261111_chige.html"
ART_KAKI = BASE + "/contents/news/261104_kaki.html"
ART_SHOP = BASE + "/contents/news/261110_shop.html"
ART_FUTURE = BASE + "/contents/news/261125_future.html"

TABS = ["定食", "丼・麺・お子様", "朝食", "サイド", "おうち定食"]
LIMITED_SECTION = "おすすめメニュー（期間限定）"

# (タブ, セクション, product_id, 名前, 価格, NEWか)
ITEMS = [
    ("定食", "おすすめメニュー", "101", "合成チゲ定食", 990, True),
    ("定食", "おすすめメニュー", "102", "合成定食", 800, False),
    ("定食", "定番メニュー（ベストセラー）", "103", "合成しょうが焼定食", 890, False),
    ("丼・麺・お子様", "丼メニュー", "201", "合成牛丼", 600, False),
    ("朝食", "※一部店舗の限定メニューです", "301", "合成朝定食", 500, False),
    ("サイド", LIMITED_SECTION, "401", "合成かきフライ（単品）", 450, False),
    ("サイド", LIMITED_SECTION, "402", "合成チヂミ（単品）", 240, True),
    ("サイド", "定食とご一緒にどうぞ", "403", "合成サラダ", 200, False),
    ("サイド", "アルコール", "404", "生ビール", 550, False),
    ("おうち定食", "おすすめメニュー", "501", "[テイクアウト]合成チゲ定食", 990, True),
    ("おうち定食", "おかずのみ", "502", "[テイクアウト]合成チゲおかずのみ", 890, False),
]
ALL_PIDS = [pid for _t, _s, pid, _n, _p, _new in ITEMS]
FUTURE_ITEM = ("定食", "おすすめメニュー", "601", "合成バタービーフ定食", 1250, True)


def _card(pid: str, name: str, price: int, is_new: bool) -> str:
    label = ('<li class="c-label c-label__new"><img src="/img/menu-list/icon-menu-new.jpg" alt="NEW"></li>'
             if is_new else "")
    return (f'<li class="c-content-index-menu-list__item">'
            f'<a href="{BASE}/menu_list/view/13/{pid}" class="c-content-index-menu-list__item-inr">'
            f'<picture><img src="/files/menu_img/{pid}s.jpg" alt="{name}" class="c-content-index-menu-list__pict"></picture>'
            f'<ul class="c-label__list">{label}</ul>'
            f'<div class="c-content-index-menu-list__title-box js-match-height-product">'
            f'<p class="c-content-index-menu-list__shoulder-top">合成の肩書き</p>'
            f'<p class="c-content-index-menu-list__title">{name}</p>'
            f'<p><span class="c-content-index-menu-list__price">{price:,}</span>円'
            f'<span class="c-content-index-menu-list__tax">（税抜：<strong>{price}</strong>円）</span></p>'
            f'</div></a></li>')


def yayoiken_menu_page(items=None, drop: tuple = (), prices: dict | None = None) -> str:
    """全タブのパネルを持つメニュー一覧(実物と同じく1ページに全商品)。"""
    items = ITEMS if items is None else items
    prices = prices or {}
    panels = []
    for tab in TABS:
        sections: dict[str, list[str]] = {}
        order: list[str] = []
        for t, sec, pid, name, price, is_new in items:
            if t != tab or pid in drop:
                continue
            if sec not in sections:
                sections[sec] = []
                order.append(sec)
            sections[sec].append(_card(pid, name, prices.get(pid, price), is_new))
        body = "".join(f"<h2>{sec}</h2><ul class=\"c-content-index-menu-list__list\">{''.join(sections[sec])}</ul>"
                       for sec in order)
        panels.append(f'<li class="c-tab__cont-item js-tab__cont">{body}</li>')
    return ('<html><body><ul id="box-category" class="c-tab__cont">'
            + "".join(panels) + "</ul></body></html>")


def _news_item(date: str, cat: str, title: str, url: str) -> str:
    return (f'<li><a href="{url}">'
            f'<div class="c-media-list__meta">'
            f'<div class="c-media-list__time" data-datetime="{date}"><span>{date.replace("-", ".")}</span></div>'
            f'<div class="c-category"><ul><li data-category="x"><span>{cat}</span></li></ul></div>'
            f'</div>'
            f'<div class="c-media-list__title">{title}</div>'
            f'</a></li>')


def yayoiken_news_page1() -> str:
    items = [
        _news_item("2026-11-25", "商品情報", "「やよい軒」合成バタービーフ定食発売のお知らせ", ART_FUTURE),
        _news_item("2026-11-11", "商品情報", "「やよい軒」合成チゲ定食発売のお知らせ", ART_CHIGE),
        _news_item("2026-11-10", "出店情報", "「やよい軒」合成店新規オープンのお知らせ", ART_SHOP),
    ]
    return "<html><body><ul>" + "".join(items) + "</ul></body></html>"


def yayoiken_news_page2() -> str:
    items = [
        _news_item("2026-11-04", "商品情報", "「やよい軒」合成かきフライ発売のお知らせ", ART_KAKI),
    ]
    return "<html><body><ul>" + "".join(items) + "</ul></body></html>"


def yayoiken_article_chige() -> str:
    """■商品概要(肩書き行つき)+・発売日 あり。後半の再掲(バリューメニュー紹介)は照合範囲外。"""
    return ("<html><body><div>"
            "<p>本文の説明です。</p>"
            "<h3>■商品概要</h3>"
            "<p>～辛旨仕立て～</p>"
            "<p>合成チゲ定食</p>"
            "<p>990円</p>"
            "<p>[テイクアウト]～辛旨仕立て～</p>"
            "<p>合成チゲ定食</p>"
            "<p>990円</p>"
            "<p>・発売日</p>"
            "<p>2026.11.17(火)</p>"
            "<p>・発売店舗</p>"
            "<p>全国の「やよい軒」店舗</p>"
            "<h3>■「やよい軒」のバリューメニュー</h3>"
            "<p>合成しょうが焼定食</p>"
            "<p>890円</p>"
            "</div></body></html>")


def yayoiken_article_kaki() -> str:
    """■商品概要 はあるが ・発売日 が読み取れない記事(news だけ付く)。
    後半の再掲(バリューメニュー紹介)は ■商品概要 の範囲外として読まない。"""
    return ("<html><body><div>"
            "<p>本文の説明です。</p>"
            "<h3>■商品概要</h3>"
            "<p>～濃厚な海の恵み～</p>"
            "<p>合成かきフライ（単品）</p>"
            "<p>450円</p>"
            "<p>※価格はすべて税込です。</p>"
            "<h3>■「やよい軒」のバリューメニュー</h3>"
            "<p>合成しょうが焼定食</p>"
            "<p>890円</p>"
            "</div></body></html>")


def yayoiken_article_future() -> str:
    """発売前の商品の記事(メニュー一覧にはまだ載らない)。"""
    return ("<html><body><div>"
            "<p>本文の説明です。</p>"
            "<h3>■商品概要</h3>"
            "<p>～冬のごちそう～</p>"
            "<p>合成バタービーフ定食</p>"
            "<p>1,250円</p>"
            "<p>・発売日</p>"
            "<p>2026.12.2(水)</p>"
            "</div></body></html>")


def yayoiken_article_shop() -> str:
    return ("<html><body><div>"
            "<p>新しい店舗がオープンしました。</p>"
            "</div></body></html>")


_NUTRIENT_HEADS = ["熱量<br>（kcal）", "蛋白質<br>（g）", "脂質<br>（g）", "炭水化物<br>（g）",
                   "糖質<br>（g）", "食物繊維<br>（g）", "食塩相当量<br>（g）", "カリウム<br>（mg）",
                   "リン<br>（mg）"]


def _nut_row(cells: list) -> str:
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def yayoiken_info_page() -> str:
    """店舗情報ページの栄養成分表 (実物の構造: 形態の列がある表と無い表、アレルギー表は対象外)。"""
    heads = "".join(f"<th>{h}</th>" for h in _NUTRIENT_HEADS)
    form_table = (
        '<table><thead><tr><th colspan=2 class="cross">商品名</th>' + heads + "</tr></thead><tbody>"
        + _nut_row(["合成チゲ定食", "白米", 747, "33.4", "30.0", "88.2", "81.8", "6.4", "5.5", 894, 421])
        + _nut_row(["もち麦ごはん普通盛", 763, "33.9", "30.0", "92.3", "84.7", "7.6", "5.5", 921, 436])
        + _nut_row(["もち麦ごはん大盛", 901, "36.4", "30.3", "124.7", "115.0", "9.7", "5.5", 952, 464])
        + _nut_row(["合成定食", "白米", 650, "25.0", "20.0", "90.0", "83.0", "7.0", "3.0", 500, 300])
        + _nut_row(["合成みそ汁", 24, "1.8", "0.6", "3.2", "2.3", "0.9", "1.8", 100, 24])
        + "</tbody></table>")
    side_table = (
        '<table><thead><tr><th>商品名</th>' + heads + "</tr></thead><tbody>"
        + _nut_row(["合成サラダ", 84, "3.0", "5.0", "7.0", "5.0", "2.0", "0.5", 228, 102])
        + _nut_row(["合成かきフライ（単品）", 283, "5.9", "20.0", "20.8", "18.8", "2.0", "1.5", 175, 69])
        + "</tbody></table>")
    allergy_table = ('<table><thead><tr><th>商品名</th><th>卵</th><th>乳</th></tr></thead><tbody>'
                     + _nut_row(["合成チゲ定食", "●", "-"]) + "</tbody></table>")
    return f"<html><body>{form_table}{side_table}{allergy_table}</body></html>"


def yayoiken_pages(menu: str | None = None) -> dict:
    """URL → ページ の対応(robots.txt は含まない。実物どおり404として扱う)。"""
    return {MENU_URL: yayoiken_menu_page() if menu is None else menu,
            INFO_URL: yayoiken_info_page(),
            NEWS_URL: yayoiken_news_page1(), NEWS2_URL: yayoiken_news_page2(),
            ART_CHIGE: yayoiken_article_chige(), ART_KAKI: yayoiken_article_kaki(),
            ART_SHOP: yayoiken_article_shop(), ART_FUTURE: yayoiken_article_future()}
