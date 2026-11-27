"""大戸屋の合成ページ (実物の構造を縮めた検証用データ)。

公式ページのコピーはリポジトリに入れていないので、構造だけを再現する。
メニューは基準店舗 (新宿センタービル店) の1ページで、タブごとに同じ見出しが
繰り返される実物の癖 (同じ商品が別のタブにも出る) も含める。
ニュース記事の「販売期間」の日付は、実物どおり康熙部首の ⽉・⽇ で書く。
"""

from __future__ import annotations

BASE = "https://www.ootoya.com"
STORE_ID = "27214"
ROBOTS_URL = BASE + "/robots.txt"
MENU_URL = f"{BASE}/menu_list/index/{STORE_ID}"
NEWS_URL = BASE + "/news/"
ARTICLE_URL = BASE + "/news/501.html"
CAMPAIGN_ARTICLE_URL = BASE + "/news/502.html"
OLD_ARTICLE_URL = BASE + "/news/503.html"
ALL_PIDS = ["1001", "1002", "2001", "2002", "3001"]


def detail_url(pid: str) -> str:
    return f"{BASE}/menu_list/view/{STORE_ID}/{pid}"


def _card(pid: str, name: str, price: str, single: str | None = None) -> str:
    single_html = ""
    if single is not None:
        single_html = (
            '<dl class="c-card-01__single">'
            '<dt class="c-card-01__single-lead">単品</dt>'
            f'<dd class="c-card-01__single-price">{single}'
            '<span class="c-card-01__single-unit">円</span>'
            '<span class="c-card-01__single-vat">（税込）</span></dd></dl>')
    return (
        '<div class="c-box-card-01__item">'
        f'<a class="c-card-01" href="{detail_url(pid)}">'
        '<div class="c-card-01__tag"></div>'
        f'<h3 class="c-card-01__title">{name}</h3>'
        f'<p class="c-card-01__price">{price}'
        '<span class="c-card-01__price-unit">円</span>'
        '<span class="c-card-01__price-vat">（税込）</span></p>'
        f"{single_html}</a></div>")


def menu_page() -> str:
    limited = (_card("1001", "合成かきフライ4個", "1,420", "1,330")
               + _card("1002", "合成かきフライ6個", "1,720", "1,630"))
    return (
        "<html><body>"
        '<h3 class="c-heading-no2">限定メニュー</h3>'
        f'<div class="c-box-card-01">{limited}</div>'
        '<h3 class="c-heading-no2">定食</h3>'
        '<div class="c-box-card-01">'
        + _card("2001", "合成しょうが焼き定食", "980", "890")
        + _card("2002", "合成さば定食", "1,050")
        + "</div>"
        '<h3 class="c-heading-no2">小鉢</h3>'
        '<div class="c-box-card-01">'
        + _card("3001", "合成ほうれん草の胡麻和え", "150")
        + "</div>"
        # 別のタブにも同じ限定メニューの見出しと商品が出る (実物の癖)。IDで重複を除く
        '<h3 class="c-heading-no2">限定メニュー</h3>'
        f'<div class="c-box-card-01">{limited}</div>'
        "</body></html>")


def news_list_page() -> str:
    def item(num: str, date: str, badge: str, title: str) -> str:
        return (
            f'<li><a href="./{num}.html">'
            '<div class="c-notice-01__post-info">'
            f'<time datetime="{date}">{date.replace("-", "/")}</time>'
            f'<span class="c-badge-tag-03">{badge}</span></div>'
            f'<p class="c-notice-01__post-text">{title}</p></a></li>')
    return (
        "<html><body><ul>"
        + item("501", "2026-10-02", "商品情報", "旨み広がる「合成かきフライ」10月2日 (金)～")
        + item("502", "2026-10-06", "お知らせ", "【終了】合成コラボキャンペーン")
        + item("503", "2023-05-01", "商品情報", "合成さば定食が新登場")
        + "</ul></body></html>")


def article_page() -> str:
    # 販売期間の日付は実物どおり康熙部首 (⽉ U+2F49・⽇ U+2F47) で書く
    period = "2026年10\u2f492\u2f47(金)～2027年2月28日(日)"
    return (
        "<html><body>"
        '<div class="c-heading-post__info">'
        '<time datetime="2026-10-02">2026/10/02</time>'
        '<span class="c-badge-tag-03">商品情報</span></div>'
        '<h1 class="c-heading-post__title">旨み広がる「合成かきフライ」10月2日 (金)～</h1>'
        "<p>2026年10月2日(金)より、全国の大戸屋ごはん処にて、広島県産「合成かきフライ」を"
        "販売いたします。毎年ご好評をいただいている、大戸屋冬の定番「合成かきフライ」が"
        "今年も登場します。</p>"
        "<h2>大戸屋冬の定番！広島県産「合成かきフライ」</h2>"
        "<p>広島県産の合成かきを、サクッと軽やかな衣で揚げました。店内仕込みの特製"
        "タルタルソースとの相性も抜群です。<br><br>"
        '<span>●</span> 定食価格 1,420円(税込) '
        '<span>●</span> 単品価格 1,330円(税込)<br><br>'
        "＜2個増量＞合成かきフライ (6個)<br>"
        '<span>●</span> 定食価格 1,720円(税込) '
        '<span>●</span> 単品価格 1,630円(税込)</p>'
        "<h3>販売期間について</h3>"
        f"<p>{period}の期間で数量限定販売を予定しております。</p>"
        "<h3>販売店舗について</h3>"
        "<p>大戸屋ごはん処全店で販売いたします。</p>"
        "</body></html>")


def campaign_article_page() -> str:
    return (
        "<html><body>"
        '<div class="c-heading-post__info">'
        '<time datetime="2026-10-06">2026/10/06</time>'
        '<span class="c-badge-tag-01">お知らせ</span></div>'
        '<h1 class="c-heading-post__title">【終了】合成コラボキャンペーン</h1>'
        "<p>キャンペーンは終了しました。</p>"
        "</body></html>")


def ootoya_pages(menu: str | None = None) -> dict:
    return {
        MENU_URL: menu_page() if menu is None else menu,
        NEWS_URL: news_list_page(),
        ARTICLE_URL: article_page(),
        CAMPAIGN_ARTICLE_URL: campaign_article_page(),
        # OLD_ARTICLE_URL は入れない (古い記事は取得せずに照合済みにするため、
        # 取得しようとしたら FakeOotoya が落ちて気付ける)
    }
