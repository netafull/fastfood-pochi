"""フレッシュネスバーガーの合成ページ (実物の構造を縮めた検証用データ)。

公式ページのコピーはリポジトリに入れていないので、構造だけを再現する。
メニューは3カテゴリページで、サイズ別価格 (price_detail)・詳細リンクの無い
カード (ソフトドリンク)・「店舗限定」セクションなど、実物の癖を含める。
プレスリリースは座標つき抽出 (pdf_nutrition.extract_geometry の戻り値) の形で作る。
"""

from __future__ import annotations

BASE = "https://www.freshnessburger.co.jp"
ROBOTS_URL = BASE + "/robots.txt"
HAMBURGER_URL = BASE + "/menu/hamburger"
SIDEMENU_URL = BASE + "/menu/sidemenu"
BEVERAGE_URL = BASE + "/menu/beverage"
NEWS_URL = BASE + "/brandnews"
WAGYU_RELEASE_URL = BASE + "/brandnews/9001/"
NOTICE_URL = BASE + "/brandnews/9002/"
OLD_RELEASE_URL = BASE + "/brandnews/9003/"
CORN_RELEASE_URL = BASE + "/brandnews/9004/"
ALL_PIDS = ["classic_burger", "wagyu_burger", "fried_potato", "corn_potage",
            "synthetic_cola", "synthetic_coffee"]
# 実物は robots.txt が無く、トップページのHTMLが返る
ROBOTS_HTML = "<html><head><title>FRESHNESS BURGER</title></head><body>トップページ</body></html>"


def _card_a(slug: str, cat: str, name_html: str, price_html: str) -> str:
    return (f"<a class='menulist_list' href='/menu/{cat}/{slug}'>"
            f"<img class='menulist_list_img' src='/images/menu/thumb/{slug}.jpg'>"
            "<div class='menulist_list_text'>"
            f"<h4 class='menulist_list_text_mame'>{name_html}</h4>"
            f"<div class='menulist_list_text_price'>{price_html}</div>"
            "</div></a>")


def _single(price: int) -> str:
    return f'<span class="price_num">{price}</span>'


def _sizes(*pairs) -> str:
    return "".join(f'<span class="price_detail">{lab}<span class="price_num">{p}</span></span>'
                   for lab, p in pairs)


def _section(key: str, title: str, cards: str) -> str:
    return (f'<div class="menulist_body menulist_body-{key}">'
            f'<h3 class="menulist_body_ttl">{title}</h3>'
            f'<div class="menulist_lists">{cards}</div></div>')


def hamburger_page() -> str:
    return ("<html><body>"
            + _section("premium_burger", "Premium Burger",
                       _card_a("classic_burger", "hamburger", "合成クラシック<br>バーガー", _single(690)))
            + _section("wagyu_beef_burger", "Wagyu Beef Burger",
                       _card_a("wagyu_burger", "hamburger", "合成和牛バーガー", _single(1080)))
            + _section("店舗限定 badge_tenpo", "店舗限定",
                       _card_a("stadium_burger", "hamburger", "合成球場バーガー", _single(900)))
            + "</body></html>")


def sidemenu_page() -> str:
    return ("<html><body>"
            + _section("fries", "Fries",
                       _card_a("fried_potato", "sidemenu", "合成フライドポテト",
                               _sizes(("S", 300), ("R", 350), ("L", 450))))
            + _section("others", "Others",
                       _card_a("corn_potage", "sidemenu", "合成コーンポタージュ", _single(380)))
            + "</body></html>")


def beverage_page() -> str:
    cola = ("<div class='menulist_list'>"
            "<img class='menulist_list_img' src='/images/menu/thumb/synthetic_cola.jpg'>"
            "<div class='menulist_list_text'>"
            "<h4 class='menulist_list_text_mame'>合成コーラ</h4>"
            "<div class='menulist_list_text_price'>"
            + _sizes(("S", 260), ("T", 330), ("G", 400))
            + "</div></div></div>")
    return ("<html><body>"
            + _section("coffee", "Coffee &amp; Espresso &#047; Tea &amp; Milk Foam",
                       _card_a("synthetic_coffee", "beverage", "合成ドリップコーヒー",
                               _sizes(("S", 330), ("T", 400), ("G", 470))))
            + _section("soft_drink", "Soft Drink", cola)
            + "</body></html>")


def _news_item(aid: str, cat_class: str, cat: str, title: str, date: str) -> str:
    return (f'<a class="post_list_item" href="brandnews/{aid}/">'
            '<div class="post_list_item_inner"><div class="post_list_item_text">'
            f'<div class="post_list_item_cats"><span class="post_list_item_cat {cat_class}">{cat}</span></div>'
            f'<h4 class="post_list_item_ttl">{title}</h4>'
            f'<div class="post_list_item_date">{date}</div>'
            "</div></div></a>")


def news_list_page(without_wagyu: bool = False) -> str:
    items = ""
    if not without_wagyu:
        items += _news_item("9001", "news", "おしらせ", "合成和牛バーガーが登場", "2026.11.26")
    items += _news_item("9002", "news", "おしらせ", "モバイルオーダーのお知らせ", "2026.11.15")
    items += _news_item("9004", "newmenu", "新メニュー", "合成コーンポタージュが登場", "2026.11.20")
    items += _news_item("9003", "newmenu", "新メニュー", "合成さばバーガー発売", "2023.05.01")
    return "<html><body>" + items + "</body></html>"


def _press_pages(lines: list[str]) -> list[dict]:
    texts = []
    y = 800.0
    for ln in lines:
        texts.append([10.0, y, 300.0, y + 10.0, ln])
        y -= 20.0
    return [{"texts": texts, "rules": []}]


def wagyu_press_pages() -> list[dict]:
    """販売期間のあるプレスリリース (実物の構造を縮めた形)。"""
    return _press_pages([
        "報道関係各位", "2026年11月26日",
        "「合成和牛バーガー」が登場",
        "フレッシュネスバーガーにて、2026年12月2日（水）より、冬季限定「合成和牛バーガー」を期間限定で発売いたします。",
        "合成和牛バーガー",
        "価格：1,080円（税込）",
        "合成和牛のパティを使ったバーガーです。",
        "【販売期間】2026年12月2日（水）～2027年1月12日（火）",
        "【販売店舗】全国のフレッシュネスバーガー店舗",
    ])


def wagyu_press_dict() -> dict:
    return {"published_date": "2026-11-26", "launch_date": "2026-12-02",
            "planned_end_text": "2027年1月12日（火）",
            "items": [{"name": "合成和牛バーガー", "price": 1080}],
            "text": "「合成和牛バーガー」を期間限定で発売価格：1,080円（税込）"}


def corn_press_dict() -> dict:
    return {"published_date": "2026-11-20", "launch_date": "2026-11-25",
            "planned_end_text": None,
            "items": [{"name": "合成コーンポタージュ", "price": 380}],
            "text": "「合成コーンポタージュ」が登場価格：380円（税込）"}


def freshness_pages(news: str | None = None) -> dict:
    return {
        HAMBURGER_URL: hamburger_page(),
        SIDEMENU_URL: sidemenu_page(),
        BEVERAGE_URL: beverage_page(),
        NEWS_URL: news_list_page() if news is None else news,
        # NOTICE_URL と OLD_RELEASE_URL は入れない (取得は fetch_freshness_press の
        # 差し替えで受ける。古いお知らせは取得せずに照合済みにする)
    }
