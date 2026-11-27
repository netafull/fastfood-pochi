"""はなまるうどん(www.hanamaruudon.com)の合成ページ。実物の構造を縮めて再現したもの。

公式ページのコピーはリポジトリに入れない。実サイトでの試行結果は作業レポートに残す。
メニューは1ページにセクション7つ (id 属性で区切り)、商品は menu-allstar__list__item
(名前・「小」の価格・詳細リンク)、期間限定の特集商品は menu-limited__udon (全サイズ
価格つき)。詳細ページのサイズは menusingle-top__info__size。ニュース記事の商品記事は
「商品価格」+「販売期間」を持つ。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

BASE = "https://www.hanamaruudon.com"
ROBOTS_URL = BASE + "/robots.txt"
MENU_URL = BASE + "/menu/"
NEWS_URL = BASE + "/news/"
CAMPAIGN_URL = BASE + "/menu/campaign.html"
ARTICLE_URL = BASE + "/news/2026/1021-11569.html"        # 商品記事 (発売日あり)
CAMPAIGN_ARTICLE_URL = BASE + "/news/2026/1113-11683.html"  # 販促記事 (商品価格なし)


def detail_url(pid: str) -> str:
    return f"{BASE}/menu/{pid}.html"


def _allstar(name: str, price_html: str, href: str | None, img: str) -> str:
    btn = (f'<div class="text-center menu-allstar__list__btn">'
           f'<a class="btn-style4 btn-style4--hoverorange" href="{href}">'
           f'<span class="btn-style4__text">詳しくみる</span></a></div>') if href else ""
    return (f'<li class="menu-allstar__list__item">'
            f'<div class="menu-allstar__list__img icon-takeout">'
            f'<img src="/assets/img/menu/{img}" alt="{name}" loading="lazy"/></div>'
            f'<h3 class="menu-allstar__list__name">{name}\n                </h3>'
            f'<div class="menu-allstar__list__type"></div>'
            f'{price_html}{btn}</li>')


def _udon_price(ko: int) -> str:
    return f'<div class="menu-allstar__list__price"><i>小</i><span>{ko:,}</span><small> 円～</small>\n                </div>'


def _single_price(note: str, price: int, tilde: bool = False) -> str:
    return (f'<div class="menu-allstar__list__price"><small>{note}</small>'
            f'<span>{price:,}</span><small> 円{"～" if tilde else ""}</small>\n                </div>')


def _limited_price(sizes: list[tuple[str, int]]) -> str:
    lis = "".join(f'<li><i>{lab}</i><span>{p:,}</span></li>' for lab, p in sizes)
    # 実物は <span>890<small>円</small></span> の形。パーサは数値だけ読む
    lis = "".join(f'<li><i>{lab}</i><span>{p:,}<small>円</small></span></li>' for lab, p in sizes)
    return f'<ul class="menu-limited__price">{lis}</ul>'


def _section(key: str, title: str, inner: str, limited: bool = False) -> str:
    if limited:
        head = (f'<h2 class="h2-title2 menu-title"><span class="h2-title2__jp">{title}'
                f'<i class="menu-title__ballon"></i></span></h2>')
    else:
        head = (f'<h3 class="menu-sectitle"><span class="menu-sectitle__inner">'
                f'<span class="menu-sectitle__text"><small>合成の説明</small><br/><em>{title}</em></span>'
                f'</span></h3>')
    return f'<section id="{key}">{head}{inner}</section>'


def menu_page() -> str:
    limited_main = (
        '<div class="menu-limited__item icon-takeout">'
        '<img src="/assets/img/menu/img_campaign_menu.png??20261127144256" alt=""/></div>'
        '<div class="menu-limited__udon">'
        '<h3 class="menu-limited__udon__title"><span class="menu-limited__udon__title__inner">'
        '<i class="menu-limited__udon__title__new">新</i>'
        '<span class="menu-limited__udon__title__text"><span class="inline-block">合成かもねぎうどん</span></span>'
        '<span class="menu-limited__udon__title__date">10/22<small>(木)</small></span>'
        '</span></h3>'
        + _limited_price([("小", 890), ("中", 1080), ("大", 1300)])
        + '</div>'
        '<div class="menu-limited_other"><div class="menu-allstar"><ul class="menu-allstar__list">'
        + _allstar("合成かもせいろ", _limited_price([("小", 790), ("中", 980), ("大", 1200)]),
                   None, "img_campaign_menu_02.png??20261127144256")
        + '</ul></div></div>'
        '<div class="text-center"><a class="btn-style3" href="./campaign.html">キャンペーン詳細をみる</a></div>'
    )
    kakedashi = (
        '<div class="menu-allstar"><ul class="menu-allstar__list">'
        + _allstar("合成きつね", _udon_price(580), "02_2.html", "single/kv-item-02_2.png")
        + _allstar("合成かけ", _udon_price(340), "02_5.html", "single/kv-item-02_5.png")
        + '</ul></div>'
        # 販売終了した旧商品はコメントの中に残っている (実物どおり)。拾ってはいけない
        '<!--<li class="menu-allstar__list__item">'
        '<h3 class="menu-allstar__list__name">合成終了うどん</h3>'
        '<div class="menu-allstar__list__price"><i>小</i><span>999</span><small> 円～</small></div>'
        '<a href="02_9.html">詳しくみる</a></li>-->'
    )
    other = (
        '<div class="menu-allstar"><ul class="menu-allstar__list">'
        + _allstar("合成ちくわ天", _single_price("1個", 130), "05_5.html", "single/kv-item-05_5.png")
        + '</ul></div>'
    )
    takeout = (
        '<div class="menu-allstar"><ul class="menu-allstar__list">'
        + _allstar("【一部店舗限定】合成うどん弁当", _single_price("", 500), "06_1.html",
                   "single/kv-item-06_1.png")
        + '</ul></div>'
    )
    return ("<!DOCTYPE html><html lang=\"ja\"><head><title>おしながき | はなまるうどん</title></head>"
            "<body>"
            + _section("limited", "期間限定メニュー", limited_main, limited=True)
            + _section("kakedashi", "かけうどん系", kakedashi)
            + _section("tsukedashi", "ぶっかけ系", '<div class="menu-allstar"><ul class="menu-allstar__list"></ul></div>')
            + _section("okonomi", "お好みのうどん", '<div class="menu-allstar"><ul class="menu-allstar__list"></ul></div>')
            + _section("gohan", "ごはんもの", '<div class="menu-allstar"><ul class="menu-allstar__list"></ul></div>')
            + _section("other", "天ぷら・おでん・その他", other)
            + _section("takeout", "テイクアウト専用", takeout)
            + "</body></html>")


def detail_page(name: str, sizes: list[tuple[str, int, float | None]]) -> str:
    blocks = ""
    for label, price, kcal in sizes:
        icon = (f'<i class="menusingle-top__info__size-icon">{label}</i>' if label else "")
        kcal_text = f" / {kcal:g}kcal" if kcal is not None else ""
        blocks += (f'<div class="menusingle-top__info__size">{icon}'
                   f'<span class="menusingle-top__info__size-text">{price:,}'
                   f'<small>円（税込）{kcal_text}</small></span></div>')
    return ("<!DOCTYPE html><html lang=\"ja\"><body><main>"
            f'<h1 class="menusingle-top__title"><small>合成のキャッチ</small><br/><span>{name}</span></h1>'
            f'<div class="menusingle-top__info__left">{blocks}</div>'
            "</main></body></html>")


def news_list_page() -> str:
    def item(date: str, cat: str, url: str, title: str) -> str:
        return (f'<div class="news-index__item"><div class="news-index__info">'
                f'<span class="news-index__date">{date}</span>'
                f'<span class="news-index__category news__category--orange">{cat}</span></div>'
                f'<p class="news-index__text"><a href="{url}">{title}</a></p></div>')
    return ("<!DOCTYPE html><html lang=\"ja\"><body>"
            + item("26.11.13", "期間限定メニュー", CAMPAIGN_ARTICLE_URL,
                   "【合成フェス】ちくわ天を無料プレゼント！")
            + item("26.10.21", "期間限定メニュー", ARTICLE_URL,
                   "「合成かもねぎうどん」「合成かもせいろ」2種を<br>10月22日（木）より期間限定販売")
            + item("26.09.30", "お知らせ", BASE + "/assets/pdf/news20260930.pdf", "合成のお知らせPDF")
            + "</body></html>")


def article_page() -> str:
    return ("<!DOCTYPE html><html lang=\"ja\"><body>"
            "<h1>「合成かもねぎうどん」「合成かもせいろ」2種を10月22日（木）より期間限定販売</h1>"
            "<p>2026年10月22日（木）より、全国のはなまるうどんにて、「合成かもねぎうどん」"
            "「合成かもせいろ」を期間限定で新発売いたします。</p>"
            "<p>合成かもねぎうどん</p><p>（小）890円（中）1,080円（大）1,300円</p>"
            "<p>合成かもせいろ</p><p>（小）790円（中）980円（大）1,200円</p>"
            "<h2>商品概要</h2>"
            # 実物の商品概要は dl で、見出しと中身がテキスト化すると同じ行にくっつく
            "<dl><dt>商品価格</dt><dd>"
            "<p>合成かもねぎうどん（温）</p><p>（小）890円　（中）1,080円　（大）1,300円</p>"
            "<p>合成かもせいろ（つけだし：温、麺：冷）</p><p>（小）790円　（中）980円　（大）1,200円</p>"
            "<p>＊税込価格</p></dd>"
            "<dt>販売店舗</dt><dd>全国の「はなまるうどん」</dd>"
            "<dt>販売期間</dt><dd>2026年10月22日（木）～12月初旬（予定）</dd></dl>"
            "</body></html>")


def campaign_article_page() -> str:
    """販促記事。「商品価格」が無いので商品記事とみなされない。"""
    return ("<!DOCTYPE html><html lang=\"ja\"><body>"
            "<h1>【合成フェス】ちくわ天を無料プレゼント！</h1>"
            "<p>2026年11月20日（金）より4日間限定で、うどん1杯ご注文ごとに「合成ちくわ天」を"
            "無料プレゼントいたします。</p>"
            "<p>実施期間</p><p>2026年11月20日（金）～11月23日（月・祝）</p>"
            "</body></html>")


def _campaign_id(name: str) -> str:
    """parsers.hanamaru_campaign_id と同じ作り (期間限定の商品IDは商品名から作る)。"""
    norm = re.sub(r"\s+", "", unicodedata.normalize("NFKC", name))
    return "campaign_" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


PID_MAIN = _campaign_id("合成かもねぎうどん")
PID_SUB = _campaign_id("合成かもせいろ")
ALL_PIDS = [PID_MAIN, PID_SUB, "02_2", "02_5", "05_5", "06_1"]


def hanamaru_pages(menu: str | None = None) -> dict:
    return {
        MENU_URL: menu_page() if menu is None else menu,
        NEWS_URL: news_list_page(),
        ARTICLE_URL: article_page(),
        CAMPAIGN_ARTICLE_URL: campaign_article_page(),
        detail_url("02_2"): detail_page("合成きつね", [("小", 580, 404), ("中", 770, 686), ("大", 990, 968)]),
        detail_url("02_5"): detail_page("合成かけ", [("小", 340, 269), ("中", 530, 551), ("大", 750, 834)]),
        detail_url("05_5"): detail_page("合成ちくわ天", [("", 130, 156)]),
        detail_url("06_1"): detail_page("【一部店舗限定】合成うどん弁当", [("", 500, 650)]),
    }
