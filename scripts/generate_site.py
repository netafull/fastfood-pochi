#!/usr/bin/env python3
"""data/products/*/*.json から docs/ 一式を生成する静的サイトジェネレータ。

コンビニポチ(conbini-pochi)の骨格を踏襲しつつ、ファストフードポチは
「いつからいつまで売っていたか」が軸なので、週別ではなく
販売状況(販売中/終了)と、終了した限定メニューの開始月別アーカイブで構成する。
"""

from __future__ import annotations

import datetime
import html
import json
import re
import shutil
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA = ROOT / "data"
PRODUCTS = DATA / "products"
DOCS = ROOT / "docs"
JST = datetime.timezone(datetime.timedelta(hours=9))

CHAINS = CONFIG["chains"]
CHAIN_NAME = {c["slug"]: c["name"] for c in CHAINS}
# 説明文の引用元の表記(出典：○○公式サイト)
CHAIN_SOURCE = {"mcdonalds": "マクドナルド公式サイト", "mos": "モスバーガー公式サイト"}
# 「終了日」の注意書き。公式発表の最終販売日ではなく、一覧から消えたことの観測日
ENDED_NOTE = "公式サイトの一覧から消えたことを確認した日"

REVIEWS_PATH = DATA / "netaful_reviews.json"


def load_reviews() -> dict:
    try:
        return json.loads(REVIEWS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"articles": {}, "matches": {}}


REVIEWS = load_reviews()
ARTICLES_BY_ID: dict[int, dict] = {}
for _chain, _arts in REVIEWS.get("articles", {}).items():
    for _a in _arts:
        _a = dict(_a)
        _a["chain"] = _chain
        ARTICLES_BY_ID[_a["id"]] = _a
del _chain, _arts


def item_reviews(item: dict) -> list[dict]:
    """商品にひも付いたネタフルのレビュー記事(新しい順)。"""
    key = f"{item['chain']}-{item['product_id']}"
    ids = REVIEWS.get("matches", {}).get(key, [])
    arts = [ARTICLES_BY_ID[i] for i in ids if i in ARTICLES_BY_ID]
    return sorted(arts, key=lambda a: a.get("date") or "", reverse=True)


def latest_reviews(chain: str | None, limit: int) -> list[dict]:
    arts = list(ARTICLES_BY_ID.values())
    if chain:
        arts = [a for a in arts if a["chain"] == chain]
    return sorted(arts, key=lambda a: a.get("date") or "", reverse=True)[:limit]


def render_review_card(a: dict) -> str:
    thumb = a.get("thumb") or {}
    img_html = ""
    if thumb.get("url"):
        w, h = thumb.get("width"), thumb.get("height")
        wh_attr = f' width="{int(w)}" height="{int(h)}"' if w and h else ""
        img_html = (
            f'<img src="{esc(thumb["url"])}" alt="{esc(a.get("title") or "")}" '
            f'loading="lazy"{wh_attr}>'
        )
    return f"""<a class="review-card" href="{esc(a.get('link') or '')}" target="_blank" rel="noopener">
{img_html}
<div class="rc-body">
<div class="rc-t">{esc(a.get('title') or '')}</div>
<div class="rc-date">{esc((a.get('date') or '')[:10])}</div>
</div>
</a>"""


def render_review_cards(title: str, articles: list[dict]) -> str:
    """ネタフルのレビュー記事を画像付きカードのグリッドで表示する。"""
    if not articles:
        return ""
    cards = "\n".join(render_review_card(a) for a in articles)
    return f"""<h2>{esc(title)}</h2>
<div class="review-grid">
{cards}
</div>"""


def nfkc(s: str | None) -> str:
    return unicodedata.normalize("NFKC", s or "")


RELATED_KEYWORDS: list[dict] = CONFIG.get("related_keywords") or []


def match_keyword_for_name(name: str, keywords: list[dict] | None = None) -> dict | None:
    """商品名にマッチする関連キーワードのうち、一致した語が最も長い(具体的な)
    ものを返す。excludeに指定された語を含む場合、その語による一致は無視する。
    複数のキーワードが同じ長さで一致した場合はkeywordsに書かれた順を優先する。"""
    keywords = RELATED_KEYWORDS if keywords is None else keywords
    name_n = nfkc(name)
    best_kw = None
    best_len = -1
    for kw in keywords:
        excludes = kw.get("exclude") or []
        for w in kw.get("words") or []:
            if w not in name_n:
                continue
            if any(ex in name_n for ex in excludes):
                continue
            if len(w) > best_len:
                best_len = len(w)
                best_kw = kw
            break  # このキーワード内では最初にマッチした語で十分(長さはword単位)
    return best_kw


def articles_for_keyword(keyword: dict, articles: list[dict]) -> list[dict]:
    """記事タイトルにキーワードのいずれかの語を含む記事(除外語を含むものは除く)。"""
    words = keyword.get("words") or []
    excludes = keyword.get("exclude") or []
    result = []
    for a in articles:
        title_n = nfkc(a.get("title"))
        if any(ex in title_n for ex in excludes):
            continue
        if any(w in title_n for w in words):
            result.append(a)
    return result


def select_related_articles(
    item: dict, articles_by_id: dict[int, dict], keywords: list[dict] | None = None,
    limit: int = 4,
) -> tuple[str, list[dict]] | None:
    """商品名から関連キーワードを判定し、そのキーワードに該当する記事(直接ひも
    付いたレビュー記事を除く)を、同じチェーン優先・新しい順に最大limit件返す。
    該当キーワードが無い、または記事が1件も無ければNoneを返す。"""
    kw = match_keyword_for_name(item.get("name", ""), keywords)
    if not kw:
        return None
    candidates = articles_for_keyword(kw, list(articles_by_id.values()))
    linked_ids = {a["id"] for a in item_reviews(item)}
    candidates = [a for a in candidates if a["id"] not in linked_ids]
    if not candidates:
        return None
    # 日付の新しい順に並べた後、同じチェーンのものを優先する安定ソート
    candidates.sort(key=lambda a: a.get("date") or "", reverse=True)
    candidates.sort(key=lambda a: a.get("chain") != item.get("chain"))
    return kw["label"], candidates[:limit]


def esc(s) -> str:
    return html.escape(s or "", quote=True)


def has_asset(name: str) -> bool:
    return (DOCS / "assets" / name).is_file()


def load_all_products() -> list[dict]:
    items = []
    for chain in CHAIN_NAME:
        d = PRODUCTS / chain
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                items.append(json.loads(f.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
    return items


def effective_date(item: dict) -> str | None:
    """販売開始日。取れなかった商品は初回に見つけた日で代替する。"""
    return item.get("launch_date") or (item.get("first_seen_at") or "")[:10] or None


def is_ended(item: dict) -> bool:
    return item.get("status") == "ended"


def item_url(item: dict) -> str:
    return f"items/{item['chain']}-{item['product_id']}.html"


def fmt_price(item: dict) -> str:
    p = item.get("price")
    if p is None:
        return ""
    v = int(p) if float(p) == int(p) else p
    return f"{v}円（税込）"


def period_text(item: dict, detailed: bool = False) -> str:
    """「いつからいつまで売っていたか」。終了前は「販売中」と終了予定の原文。"""
    start = item.get("launch_date")
    first = (item.get("first_seen_at") or "")[:10]
    head = start or f"{first}（記録開始日）"
    if is_ended(item):
        s = f"{head}〜{item.get('ended_observed_at') or ''}"
        if detailed:
            s += f"（終了日は{ENDED_NOTE}）"
        return s
    s = f"{head}〜 販売中"
    if detailed and item.get("planned_end_text"):
        s += f"（終了予定：{item['planned_end_text']}）"
    return s


def render_card(item: dict) -> str:
    chain_name = CHAIN_NAME.get(item["chain"], item["chain"])
    date = effective_date(item) or ""
    kcal = (item.get("nutrition") or {}).get("kcal")
    kcal_html = f'<span class="kcal">{kcal:g}kcal</span>' if kcal is not None else ""
    review_html = '<div class="review-badge">レビューあり</div>' if item_reviews(item) else ""
    status = "ended" if is_ended(item) else "on_sale"
    status_html = '<span class="st st-ended">終了</span>' if is_ended(item) else '<span class="st st-on">販売中</span>'
    kind = item.get("limited_kind") or ""
    return f"""<a class="item" href="{esc(item_url(item))}" data-kcal="{kcal if kcal is not None else ''}"
   data-date="{esc(date)}" data-chain="{esc(item['chain'])}" data-status="{status}">
  <div class="badge">{esc(chain_name)}</div>{status_html}
  <div class="t">{esc(item['name'])}</div>
  <div class="price">{esc(fmt_price(item))}{kcal_html}</div>
  <div class="meta">{esc(period_text(item))}{('・' + esc(kind)) if kind else ''}</div>
  {review_html}
</a>"""


CSS = """
:root {
  --bg: #fafaf7; --card: #ffffff; --text: #1a1a1a; --muted: #6b6b6b;
  --accent: #00897b; --line: #e5e2dc;
  --mcdonalds: #da291c; --mos: #1b7f3b;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #14151a; --card: #1e2027; --text: #e8e8e6; --muted: #9a9a96;
    --line: #2c2e36; --accent: #26a69a; }
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text);
  font-family: "Hiragino Sans", "Noto Sans JP", sans-serif; line-height: 1.6; }
a { color: inherit; }
header { padding: 24px 16px 12px; max-width: 980px; margin: 0 auto; }
header h1 a { text-decoration: none; font-size: 22px;
  display: inline-flex; align-items: center; gap: 8px; }
/* ロゴは文字とほぼ同じ高さに揃える(96px画像を縮小して表示。姉妹サイトと同じ) */
header h1 img { width: 32px; height: 32px; }
header p { color: var(--muted); font-size: 13px; margin-top: 4px; }
nav.crumbs { font-size: 12px; color: var(--muted); margin-top: 8px; }
nav.crumbs a { text-decoration: none; color: var(--accent); }
.sites { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; align-items: baseline; }
.sites .lbl { font-size: 12px; color: var(--muted); }
.sites a { font-size: 12px; padding: 3px 10px; border-radius: 999px;
  border: 1px solid var(--line); background: var(--card); text-decoration: none; }
main { max-width: 980px; margin: 0 auto; padding: 8px 16px 48px; }
h2 { font-size: 18px; padding-left: 10px; border-left: 4px solid var(--accent); margin: 24px 0 12px; }
.sort-bar { display: flex; gap: 8px; margin: 8px 0 16px; flex-wrap: wrap; align-items: center; }
.sort-bar select, .sort-bar input { font-size: 13px; padding: 5px 8px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--card); color: var(--text); font-family: inherit; }
.chain-group h3 { font-size: 15px; margin: 18px 0 8px; padding-left: 8px; border-left: 4px solid var(--line); }
.chain-group[data-chain="mcdonalds"] h3 { border-color: var(--mcdonalds); }
.chain-group[data-chain="mos"] h3 { border-color: var(--mos); }
.grid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }
.item { display: block; background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; padding: 12px; text-decoration: none; }
.item:hover { border-color: var(--accent); }
.item .badge { display: inline-block; font-size: 10px; font-weight: 700; color: #fff;
  border-radius: 4px; padding: 1px 6px; margin-bottom: 6px; }
.item[data-chain="mcdonalds"] .badge { background: var(--mcdonalds); }
.item[data-chain="mos"] .badge { background: var(--mos); }
.item .st { display: inline-block; font-size: 10px; border-radius: 4px; padding: 0 6px; margin-left: 6px;
  border: 1px solid var(--line); color: var(--muted); }
.item .st-on { color: var(--accent); border-color: var(--accent); }
.item[data-status="ended"] { opacity: .82; }
.item .t { font-size: 14px; font-weight: 600; display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; min-height: 2.6em; }
.item .price { margin-top: 6px; font-size: 13px; }
.item .price .kcal { margin-left: 8px; color: var(--muted); }
.item .meta { font-size: 11px; color: var(--muted); margin-top: 4px; }
.monthlist { display: flex; flex-direction: column; gap: 6px; margin-top: 12px; }
.monthlist a { text-decoration: none; padding: 10px 14px; background: var(--card);
  border: 1px solid var(--line); border-radius: 8px; font-size: 14px; }
.monthlist a:hover { border-color: var(--accent); }
.monthlist .n { color: var(--muted); font-size: 12px; margin-left: 8px; }
.chainlist { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 8px; }
.chainlist a { text-decoration: none; padding: 8px 16px; border-radius: 999px;
  border: 1px solid var(--line); background: var(--card); font-size: 13px; }
.detail { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 20px; margin-top: 12px; }
.detail h1 { font-size: 20px; margin-bottom: 8px; }
.detail .price { font-size: 18px; font-weight: 700; margin: 8px 0; }
.detail .price small { font-size: 12px; font-weight: 400; color: var(--muted); }
.detail table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13px; }
.detail table th, .detail table td { border-bottom: 1px solid var(--line); text-align: left;
  padding: 6px 4px; }
.detail table th { color: var(--muted); font-weight: 500; width: 40%; }
.detail h3 { margin-top: 16px; font-size: 14px; }
.detail details { margin-top: 10px; font-size: 13px; }
.detail details summary { cursor: pointer; color: var(--accent); }
.detail blockquote { border-left: 3px solid var(--accent); margin: 12px 0; padding: 8px 14px;
  color: var(--text); background: var(--bg); border-radius: 4px; font-size: 14px; }
.detail .source { font-size: 12px; color: var(--muted); margin-top: 4px; }
.detail .warn { font-size: 12px; color: var(--muted); margin-top: 2px; }
.detail .note { font-size: 12px; color: var(--muted); margin-top: 6px; }
.tags { display: flex; gap: 6px; flex-wrap: wrap; margin: 8px 0; }
.tags span { font-size: 11px; border: 1px solid var(--line); border-radius: 4px; padding: 1px 6px; }
.searchbox { margin: 16px 0; }
.searchbox input { width: 100%; font-size: 14px; padding: 10px 12px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--card); color: var(--text); font-family: inherit; }
footer { max-width: 980px; margin: 0 auto; padding: 16px; color: var(--muted); font-size: 12px;
  border-top: 1px solid var(--line); }
.about { max-width: 980px; margin: 40px auto 0; padding: 20px 16px 0; border-top: 1px solid var(--line);
  color: var(--muted); font-size: 13px; line-height: 1.9; }
.about h2 { font-size: 14px; border-left-width: 3px; margin-bottom: 8px; color: var(--text); }
.about p { margin-top: 8px; }
.about a { color: var(--accent); }
.empty { color: var(--muted); font-size: 14px; padding: 12px 0; }
.item .review-badge { display: inline-block; margin-top: 4px; font-size: 10px; color: var(--accent);
  border: 1px solid var(--accent); border-radius: 4px; padding: 0 5px; }
.review-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 10px; margin-top: 8px; }
.review-card { display: block; background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; overflow: hidden; text-decoration: none; color: inherit; }
.review-card:hover { border-color: var(--accent); }
.review-card img { width: 100%; height: 110px; object-fit: cover; display: block; background: var(--line); }
.review-card .rc-body { padding: 8px 10px; }
.review-card .rc-t { font-size: 12.5px; font-weight: 600; display: -webkit-box;
  -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; line-height: 1.4; }
.review-card .rc-date { color: var(--muted); font-size: 11px; margin-top: 4px; }
.detail .review-grid { margin-top: 4px; }
@media (max-width: 480px) {
  .review-grid { grid-template-columns: repeat(2, 1fr); }
}
/* 手動のAdSense広告枠(Auto adsは使わない)。設定が空の間は何も出力しない。
   width:100%指定必須: グリッドやflexの中で幅が0に潰れるとAdSenseが配信に失敗する。
   overflow:hiddenは付けない。未充填だけ畳む */
.ad-slot { width: 100%; max-width: 960px; margin: 16px auto; background: var(--card);
  border: 1px solid var(--line); border-radius: 10px; padding: 12px; text-align: center; }
.ad-slot:has(ins[data-ad-status="unfilled"]) { display: none; }
"""


def render_ad_slot() -> str:
    """手動設置のディスプレイ広告ユニット。

    adsense_client_id / adsense_ad_slot のどちらかが未設定なら何も出さない
    (ファストフードポチは当面広告なし。2026-10-07 コグレ方針)。
    置く場所は [data-sortable] のグリッドの外だけ(SORT_JSが並び替えで枠を壊すため)。
    """
    ad_slot = CONFIG.get("adsense_ad_slot", "")
    adsense_id = CONFIG.get("adsense_client_id", "")
    if not ad_slot or not adsense_id:
        return ""
    return f"""<div class="ad-slot">
<ins class="adsbygoogle"
     style="display:block"
     data-ad-client="{esc(adsense_id)}"
     data-ad-slot="{esc(ad_slot)}"
     data-ad-format="rectangle"
     data-full-width-responsive="true"></ins>
<script>(adsbygoogle = window.adsbygoogle || []).push({{}});</script>
</div>"""


def page_shell(title: str, description: str, body: str, canonical: str, extra_head: str = "",
               header_ad: bool = False) -> str:
    site_url = CONFIG.get("site_url", "")
    page_title = f'{esc(title)}｜{esc(CONFIG["site_title"])}' if title != CONFIG["site_title"] else esc(title)

    ga_id = CONFIG.get("ga_measurement_id")
    ga_tag = ""
    if ga_id:
        ga_tag = (
            f'<script async src="https://www.googletagmanager.com/gtag/js?id={esc(ga_id)}"></script>\n'
            "<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}"
            f"gtag('js',new Date());gtag('config','{esc(ga_id)}');</script>"
        )

    adsense_id = CONFIG.get("adsense_client_id", "")
    adsense_tag = (
        '<script async src="https://pagead2.googlesyndication.com/pagead/js/'
        f'adsbygoogle.js?client={esc(adsense_id)}" crossorigin="anonymous"></script>'
        if adsense_id else ""
    )
    header_ad_html = render_ad_slot() if header_ad else ""

    # 見出しの左に置くポチシリーズ共通のアイコン。画像が置かれていなければ何も出さない
    logo_img = (
        '<img src="/assets/logo.png" alt="" width="32" height="32">'
        if has_asset("logo.png") else ""
    )
    icon_tags = []
    if has_asset("favicon.png"):
        # Googleの検索結果に出るファビコンは48pxの倍数が必要(32pxだと地球儀になる。
        # コンビニポチで2026-10-07に判明)。favicon.png は96px。sizes を明示する
        icon_tags.append('<link rel="icon" type="image/png" sizes="96x96" href="/assets/favicon.png">')
    if has_asset("apple-touch-icon.png"):
        icon_tags.append('<link rel="apple-touch-icon" href="/assets/apple-touch-icon.png">')
    ogp_tags = []
    if has_asset("ogp.jpg"):
        ogp_tags = [
            f'<meta property="og:image" content="{esc(site_url)}assets/ogp.jpg">',
            '<meta property="og:image:width" content="1200">',
            '<meta property="og:image:height" content="630">',
            '<meta name="twitter:card" content="summary_large_image">',
        ]

    related = CONFIG.get("related_sites") or []
    links = "\n".join(
        f'<a href="{esc(s["url"])}">{esc(s["name"])}'
        + (f'<span class="lbl"> {esc(s["desc"])}</span>' if s.get("desc") else "")
        + "</a>"
        for s in related
    )
    related_html = f'<nav class="sites"><span class="lbl">関連サイト</span>\n{links}\n</nav>' if related else ""

    policy_url = CONFIG.get("policy_url", "")
    policy_link = (
        f'｜ <a href="{esc(policy_url)}">メディアポリシー</a>\n' if policy_url else ""
    )

    out = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{esc(canonical)}">
{ga_tag}
{adsense_tag}
{chr(10).join(icon_tags)}
<meta property="og:type" content="website">
<meta property="og:title" content="{page_title}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{esc(canonical)}">
<meta property="og:site_name" content="{esc(CONFIG['site_title'])}">
<meta property="og:locale" content="ja_JP">
{chr(10).join(ogp_tags)}
<link rel="alternate" type="application/rss+xml" title="RSS" href="/rss.xml">
<style>{CSS}</style>
{extra_head}
</head>
<body>
<header>
<h1><a href="/">{logo_img}{esc(CONFIG["site_title"])}</a></h1>
<p>{esc(CONFIG["site_description"])}</p>
{related_html}
</header>
{header_ad_html}
<main>
{body}
</main>
<footer>
価格・販売期間・販売状況は取得時点のものです。最新の状況は各社公式サイトでご確認ください。
当サイトはマクドナルド、モスバーガー各社とは関係のない非公式サイトです。
公式サイト： <a href="https://www.mcdonalds.co.jp/">マクドナルド</a> ／ <a href="https://www.mos.jp/">モスバーガー</a>
{policy_link}｜ <a href="/rss.xml">RSS</a> ｜ <a href="/archive/">終了した限定メニュー</a>
{related_html}
</footer>
</body>
</html>
"""
    if adsense_id:
        # AdSenseダッシュボードではvignette(全画面)広告をサブドメイン単位で無効化
        # できないため、リンクごとにdata-google-vignette="false"を付ける
        out = re.sub(r'<a (?![^>]*data-google-vignette)', '<a data-google-vignette="false" ', out)
    return out


SORT_JS = """
<script>
(function () {
  // チェーンごとのグループ(.chain-group)内でだけ並び替える。絞り込みはカード単位・グループ単位で隠す
  var grids = Array.prototype.slice.call(document.querySelectorAll('[data-sortable]'));
  if (!grids.length) return;
  var select = document.getElementById('sort-select');
  var chainSel = document.getElementById('chain-filter');
  var statusSel = document.getElementById('status-filter');
  function apply() {
    var mode = select ? select.value : 'date';
    var chain = chainSel ? chainSel.value : 'all';
    var status = statusSel ? statusSel.value : 'all';
    grids.forEach(function (grid) {
      var group = grid.closest('.chain-group');
      var items = Array.prototype.slice.call(grid.querySelectorAll('.item'));
      items.sort(function (a, b) {
        if (mode === 'kcal') {
          return parseFloat(b.dataset.kcal || '-1') - parseFloat(a.dataset.kcal || '-1');
        }
        return (b.dataset.date || '').localeCompare(a.dataset.date || '');
      });
      var visible = 0;
      items.forEach(function (el) {
        grid.appendChild(el);
        var show = status === 'all' || el.dataset.status === status;
        el.style.display = show ? '' : 'none';
        if (show) visible++;
      });
      if (group) {
        var chainOk = chain === 'all' || group.dataset.chain === chain;
        group.style.display = (chainOk && visible > 0) ? '' : 'none';
        var cnt = group.querySelector('.cnt');
        if (cnt) cnt.textContent = visible;
      }
    });
  }
  if (select) select.addEventListener('change', apply);
  if (chainSel) chainSel.addEventListener('change', apply);
  if (statusSel) statusSel.addEventListener('change', apply);
  apply();
})();
</script>
"""


def render_grouped(items: list[dict], sortable: bool = True, ads: bool = False) -> str:
    """商品をチェーンごと(config.jsonのchains順)にまとめ、各チェーンの中は販売開始日の新しい順に並べる。"""
    attr = " data-sortable" if sortable else ""
    ad_html = render_ad_slot() if ads else ""
    sections = []
    active = [c for c in CHAINS if any(it["chain"] == c["slug"] for it in items)]
    for c in active:
        group = [it for it in items if it["chain"] == c["slug"]]
        group.sort(key=lambda it: effective_date(it) or "", reverse=True)
        cards = "\n".join(render_card(it) for it in group)
        sections.append(
            f'<section class="chain-group" data-chain="{esc(c["slug"])}">\n'
            f'<h3>{esc(c["name"])} (<span class="cnt">{len(group)}</span>件)</h3>\n'
            f'<div class="grid"{attr}>\n{cards}\n</div>\n'
            # 広告はチェーングループの間(最後のグループの後ろには置かない)。
            # [data-sortable]のグリッドの外に置くので並び替えで位置が壊れない
            f'{ad_html if ads and c is not active[-1] else ""}\n</section>'
        )
    return "\n".join(sections)


SEARCH_JS = """
<script>
(function () {
  var box = document.getElementById('search-input');
  var results = document.getElementById('search-results');
  if (!box || !results) return;
  var index = null;
  box.addEventListener('input', function () {
    var q = box.value.trim();
    if (!q) { results.innerHTML = ''; return; }
    function render() {
      var lower = q.toLowerCase();
      var hits = index.filter(function (it) {
        return it.n.toLowerCase().indexOf(lower) !== -1;
      }).slice(0, 50);
      results.innerHTML = hits.map(function (it) {
        return '<a class="item" data-chain="' + it.c + '" href="' + it.u + '">' +
          '<div class="badge">' + it.cn + '</div><div class="t">' + it.n +
          '</div><div class="meta">' + (it.d || '') + (it.s === 'ended' ? '　終了' : '') + '</div></a>';
      }).join('');
    }
    if (index) { render(); return; }
    fetch('/search-index.json').then(function (r) { return r.json(); }).then(function (data) {
      index = data; render();
    });
  });
})();
</script>
"""


def build_search_index(items: list[dict]) -> list[dict]:
    return [
        {
            "n": it["name"],
            "u": "/" + item_url(it),
            "c": it["chain"],
            "cn": CHAIN_NAME.get(it["chain"], it["chain"]),
            "d": effective_date(it) or "",
            "s": "ended" if is_ended(it) else "on_sale",
        }
        for it in items
    ]


def filter_bar(status: bool = True, default_status: str = "on_sale") -> str:
    """並び替え・販売状況・チェーンの絞り込み。[data-sortable]の外に置く。"""
    status_sel = ""
    if status:
        opts = [("on_sale", "販売中"), ("ended", "終了"), ("all", "すべて")]
        options = "".join(
            f'<option value="{v}"{" selected" if v == default_status else ""}>{n}</option>' for v, n in opts)
        status_sel = f'\n<label>販売状況: <select id="status-filter">{options}</select></label>'
    chain_opts = "".join(f'<option value="{esc(c["slug"])}">{esc(c["name"])}</option>' for c in CHAINS)
    return f"""<div class="sort-bar">
<label>並び替え: <select id="sort-select"><option value="date">開始日順</option>
<option value="kcal">カロリー順</option></select></label>{status_sel}
<label>絞り込み: <select id="chain-filter"><option value="all">すべて</option>{chain_opts}</select></label>
</div>"""


def render_grid_page(title: str, description: str, items: list[dict], canonical: str,
                      show_filters: bool = True, review_chain: str | None = None,
                      status_filter: bool = False, default_status: str = "all",
                      empty_text: str = "商品がありません。") -> str:
    reviews_section = render_review_cards("ネタフルの最新レビュー", latest_reviews(review_chain, 5))
    if not items:
        body = f"<h2>{esc(title)}</h2><p class='empty'>{esc(empty_text)}</p>\n{reviews_section}"
        return page_shell(title, description, body, canonical)
    filters = filter_bar(status_filter, default_status) if show_filters else ""
    on = sum(1 for it in items if not is_ended(it))
    count_text = f"{len(items)}件" if not status_filter else f"{len(items)}件（販売中{on}件）"
    body = f"""<h2>{esc(title)} ({count_text})</h2>
{filters}
{render_grouped(items, ads=True)}
{SORT_JS if show_filters else ""}
{reviews_section}"""
    return page_shell(title, description, body, canonical, header_ad=True)


RECENT_ENDED_DAYS = 30


def recently_ended(items: list[dict], today: datetime.date) -> list[dict]:
    out = []
    for it in items:
        if not is_ended(it):
            continue
        try:
            d = datetime.date.fromisoformat(it.get("ended_observed_at") or "")
        except ValueError:
            continue
        if (today - d).days <= RECENT_ENDED_DAYS:
            out.append(it)
    return out


def render_about() -> str:
    about = CONFIG.get("about") or []
    if not about:
        return ""
    link = '<a href="https://netaful.jp/">ネタフル</a>'
    paras = "\n".join(f"<p>{esc(x).replace('{{ネタフル}}', link)}</p>" for x in about)
    return f'<section class="about"><h2>{esc(CONFIG["site_title"])}について</h2>\n{paras}\n</section>'


def render_top(items: list[dict], today: datetime.date | None = None) -> str:
    today = today or datetime.datetime.now(JST).date()
    on_sale = [it for it in items if not is_ended(it)]
    # 切り替えフィルタ(販売中/終了)が効くよう、最近終わったものも載せておく。
    # 既定は「販売中」なので、JSが動けば見えるのは販売中だけ
    shown = on_sale + recently_ended(items, today)
    ended_total = sum(1 for it in items if is_ended(it))
    site_url = CONFIG.get("site_url", "")

    search_html = """<div class="searchbox"><input id="search-input" type="search"
placeholder="メニュー名で検索（例: 月見、てりやき、フォカッチャ）"></div>
<div class="grid" id="search-results"></div>"""
    chain_links = "\n".join(
        f'<a href="/chains/{c["slug"]}/">{esc(c["name"])}</a>' for c in CHAINS
    )
    archive_link = (
        f'<p><a href="/archive/">終了した限定メニューのアーカイブ（{ended_total}件）を見る →</a></p>'
        if ended_total else ""
    )
    if shown:
        list_section = f"""<h2>販売中の限定メニュー ({len(on_sale)}件)</h2>
{filter_bar(True, "on_sale")}
{render_grouped(shown, ads=True)}
{SORT_JS}"""
    else:
        list_section = "<h2>販売中の限定メニュー</h2><p class='empty'>まだ記録がありません。</p>"

    body = f"""<div class="chainlist">
{chain_links}
</div>
<h2>メニュー名で検索</h2>
{search_html}
{SEARCH_JS}
{render_review_cards("ネタフルの最新レビュー", latest_reviews(None, 6))}
{list_section}
{archive_link}
{render_about()}"""
    # 検索結果のサイト名。サブドメイン(fastfood.netaful.jp)は何も指定しないと親ドメインの
    # 「ネタフル」と表示されるので、トップページに WebSite 構造化データでサイト名を伝える
    # (コンビニポチで2026-10-07に判明した対処を最初から入れている)
    site_name_ld = json.dumps({
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": CONFIG["site_title"],
        "alternateName": ["Fastfood Pochi"],
        "url": site_url,
    }, ensure_ascii=False)
    return page_shell(
        CONFIG["site_title"], CONFIG["site_description"], body, site_url,
        extra_head=f'<script type="application/ld+json">{site_name_ld}</script>',
        header_ad=True,
    )


def start_month(item: dict) -> str | None:
    d = effective_date(item)
    return d[:7] if d and re.match(r"^\d{4}-\d{2}", d) else None


def ended_items(items: list[dict]) -> list[dict]:
    return [it for it in items if is_ended(it)]


def render_archive_index(items: list[dict]) -> str:
    ended = ended_items(items)
    by_month: dict[str, int] = {}
    for it in ended:
        m = start_month(it)
        if m:
            by_month[m] = by_month.get(m, 0) + 1
    if not by_month:
        body = ("<h2>終了した限定メニュー</h2>"
                "<p class='empty'>まだ販売が終わったメニューはありません。"
                "公式サイトの一覧から消えたことを連続で確認すると、ここに並びます。</p>")
    else:
        rows = "\n".join(
            f'<a href="/archive/{m}/">{m[:4]}年{int(m[5:7])}月に始まった限定メニュー<span class="n">{n}件</span></a>'
            for m, n in sorted(by_month.items(), reverse=True)
        )
        body = f"""<h2>終了した限定メニュー ({len(ended)}件)</h2>
<p class="empty">販売開始月ごとにまとめています。終了日は公式サイトの一覧から消えたことを確認した日です。</p>
<div class="monthlist">
{rows}
</div>"""
    return page_shell("終了した限定メニュー", "販売が終わったマクドナルド・モスバーガーの限定メニューを、販売開始月ごとにまとめたアーカイブ",
                       body, CONFIG.get("site_url", "") + "archive/")


def render_chains_top() -> str:
    links = "\n".join(f'<a href="/chains/{c["slug"]}/">{esc(c["name"])}</a>' for c in CHAINS)
    body = f'<h2>チェーン一覧</h2><div class="chainlist">{links}</div>'
    return page_shell("チェーン一覧", "マクドナルド・モスバーガーの限定メニュー一覧", body,
                       CONFIG.get("site_url", "") + "chains/")


NUTRITION_LABELS = [
    ("kcal", "熱量", "kcal"), ("protein_g", "たんぱく質", "g"),
    ("fat_g", "脂質", "g"), ("carbs_g", "炭水化物", "g"),
    ("sugar_g", "　糖類", "g"), ("fiber_g", "　食物繊維", "g"),
    ("salt_g", "食塩相当量", "g"),
]
# 「ほか」の項目がこの数以下ならそのまま表に並べ、超えたら折りたたむ(モスは20項目前後)
OTHER_INLINE_MAX = 3


def render_nutrition(item: dict) -> str:
    nut = item.get("nutrition")
    if not nut:
        return ("<h3>栄養成分</h3><p class='note'>栄養成分は公式サイトに掲載がありませんでした"
                "（セット商品や一部の限定商品は載らないことがあります）。</p>")
    trs = []
    for key, label, unit in NUTRITION_LABELS:
        v = nut.get(key)
        if v is None:
            continue
        trs.append(f"<tr><th>{esc(label)}</th><td>{v:g}{unit}</td></tr>")
    other = nut.get("other") or {}
    inline_rows = ""
    details = ""
    if other:
        rows = "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in other.items())
        if len(other) <= OTHER_INLINE_MAX:
            inline_rows = rows
        else:
            details = (f"<details><summary>そのほかの栄養成分（{len(other)}項目）を見る</summary>"
                       f"<table>{rows}</table></details>")
    if not trs and not inline_rows and not details:
        return ""
    table = f"<table>{''.join(trs)}{inline_rows}</table>" if (trs or inline_rows) else ""
    return f"<h3>栄養成分</h3>{table}{details}"


def render_allergens(item: dict) -> str:
    a = item.get("allergens")
    if a is None:
        return ""
    contains = a.get("contains") or []
    text = "、".join(contains) if contains else "表示対象の品目は含まれていません"
    html_ = f"<p class='tags'><strong>アレルゲン（含む）：</strong>{esc(text)}</p>"
    partial = a.get("partial") or []
    if partial:
        ptxt = "、".join(f"{p['name']}{p['mark']}" for p in partial)
        html_ += (f"<p class='note'>条件つきの表示：{esc(ptxt)}"
                  "（記号の意味は公式サイトの凡例でご確認ください）</p>")
    return html_


def render_origin(item: dict) -> str:
    origin = item.get("origin")
    if not origin:
        return ""
    rows = "".join(f"<tr><th>{esc(o['name'])}</th><td>{esc(o['origin'])}</td></tr>" for o in origin)
    return f"<details><summary>原産地・製造所の情報（{len(origin)}件）を見る</summary><table>{rows}</table></details>"


def render_price_history(item: dict) -> str:
    hist = item.get("price_history") or []
    if len(hist) <= 1:
        return ""
    rows = "".join(f"<tr><th>{esc(h['date'])}〜</th><td>{h['price']:g}円</td></tr>" for h in hist)
    return f"<h3>価格の履歴</h3><table>{rows}</table><p class='note'>公式サイトの一覧に出ていた価格を、変わった日に記録しています。</p>"


def seo_title(item: dict) -> str:
    chain_name = CHAIN_NAME.get(item["chain"], item["chain"])
    nut = item.get("nutrition") or {}
    facts = "カロリー・価格・栄養成分" if nut.get("kcal") is not None else "価格・販売期間"
    return f"{chain_name}「{item['name']}」の{facts}"


def seo_description(item: dict) -> str:
    chain_name = CHAIN_NAME.get(item["chain"], item["chain"])
    nut = item.get("nutrition") or {}
    bits = [f"{chain_name}の限定メニュー「{item['name']}」"]
    if nut.get("kcal") is not None:
        bits.append(f"{nut['kcal']:g}kcal")
    if item.get("price") is not None:
        bits.append(f"{item['price']:g}円（税込）")
    bits.append(period_text(item))
    return ("、".join(bits) + "。" + (item.get("description") or ""))[:200]


def render_item_page(item: dict) -> str:
    chain_name = CHAIN_NAME.get(item["chain"], item["chain"])
    official_url = item.get("official_url") or ""

    quote_html = ""
    if item.get("description"):
        fetched = (item.get("first_seen_at") or "")[:10]
        quote_html = f"""<blockquote cite="{esc(official_url)}">{esc(item['description'])}</blockquote>
<p class="source">出典：{esc(CHAIN_SOURCE.get(item['chain'], chain_name + '公式サイト'))}（{esc(fetched)}取得時点） ・
<a href="{esc(official_url)}" target="_blank" rel="noopener">公式ページを見る</a></p>
<p class="warn">※現在は公式ページが削除されている場合があります。</p>"""
    elif official_url:
        quote_html = (f'<p class="source"><a href="{esc(official_url)}" target="_blank" rel="noopener">'
                      "公式ページを見る</a>（販売終了後は削除されている場合があります）</p>")

    reviews_html = ""
    reviews = item_reviews(item)
    if reviews:
        reviews_html = f"""<h3>ネタフルのレビュー</h3>
<div class="review-grid">
{chr(10).join(render_review_card(a) for a in reviews)}
</div>"""

    related_html = ""
    related = select_related_articles(item, ARTICLES_BY_ID)
    if related:
        label, related_articles_list = related
        related_html = f"""<h3>関連するネタフルの記事（{esc(label)}）</h3>
<div class="review-grid">
{chr(10).join(render_review_card(a) for a in related_articles_list)}
</div>"""

    # 直接のレビューもキーワードの関連記事も無い商品には、そのチェーンの最新レビューを出す
    # (どの商品ページからもネタフルへ行けるようにする。コンビニポチと同じ方針)
    if not reviews and not related:
        recent = latest_reviews(item["chain"], 4)
        if recent:
            related_html = f"""<h3>{esc(chain_name)}のネタフル最新レビュー</h3>
<div class="review-grid">
{chr(10).join(render_review_card(a) for a in recent)}
</div>"""

    nutrition_html = render_nutrition(item)
    allergen_html = render_allergens(item)
    origin_html = render_origin(item)
    history_html = render_price_history(item)
    has_extras = bool(item.get("nutrition") or item.get("allergens") or history_html)
    mid_ad = render_ad_slot() if has_extras else ""
    bottom_ad = "" if has_extras else render_ad_slot()

    price = fmt_price(item)
    price_note = ""
    if item.get("price") is not None and item.get("price_note"):
        price_note = f"<br><small>{esc(item['price_note'])}</small>"
    status_label = "販売終了（公式サイトの一覧から消えています）" if is_ended(item) else "販売中"
    kind = item.get("limited_kind")
    kind_html = f"<div class='tags'><span>{esc(kind)}</span><span>{esc(status_label.split('（')[0])}</span></div>" if kind else ""
    relaunch = ""
    for r in item.get("relaunches") or []:
        relaunch += (f"<p class='note'>一度販売終了を確認した後（{esc(r.get('ended_observed_at') or '')}）、"
                     f"{esc(r.get('reappeared_at') or '')}に公式サイトの一覧へ再び登場しました。</p>")
    mop_note = ""
    if item.get("chain") == "mcdonalds" and item.get("mop_ended_at") and not is_ended(item):
        mop_note = ("<p class='note'>マクドナルドは公式の終了予定日を載せていないため、"
                    "公式ページのモバイルオーダー受付期間の終了日を「終了予定」として表示しています。</p>")

    body = f"""<nav class="crumbs"><a href="/">トップ</a> &gt; <a href="/chains/{esc(item['chain'])}/">{esc(chain_name)}</a></nav>
<div class="detail">
<div class="badge" style="display:inline-block;font-size:11px;font-weight:700;color:#fff;
border-radius:4px;padding:2px 8px;background:var(--{esc(item['chain'])})">{esc(chain_name)}</div>
<h1>{esc(item['name'])}</h1>
{kind_html}
<p class="price">{esc(price)}{price_note}</p>
<p><strong>販売期間：</strong>{esc(period_text(item, detailed=True))}</p>
{mop_note}
{relaunch}
{('<p><strong>カテゴリ：</strong>' + esc(item['category']) + '</p>') if item.get('category') else ''}
{quote_html}
{nutrition_html}
{allergen_html}
{origin_html}
{history_html}
{mid_ad}
{reviews_html}
{related_html}
{bottom_ad}
</div>"""
    canonical = CONFIG.get("site_url", "") + item_url(item)
    return page_shell(seo_title(item), seo_description(item), body, canonical)


# AIの学習データ集め専用のクローラーは断る。商品説明は各社公式サイトからの引用なので、
# 引用元への配慮として学習用にまとめて持っていかれるのは避ける。
# 検索やAI検索の表示に使うクローラー(Googlebot, Bingbot, OAI-SearchBot, ChatGPT-User,
# PerplexityBot 等)は集客の入口なので止めない。
AI_TRAINING_BOTS = [
    "GPTBot", "CCBot", "Google-Extended", "Applebot-Extended", "anthropic-ai",
    "ClaudeBot", "Bytespider", "meta-externalagent", "cohere-training-data-crawler",
    "Diffbot", "Omgilibot",
]


def generate_robots() -> str:
    lines = [f"User-agent: {bot}" for bot in AI_TRAINING_BOTS]
    lines += ["Disallow: /", "", "User-agent: *", "Allow: /", "",
              f"Sitemap: {CONFIG.get('site_url', '')}sitemap.xml", ""]
    return "\n".join(lines)


def generate_rss(items: list[dict]) -> str:
    """新しく記録したメニューのRSS。"""
    site_url = CONFIG.get("site_url", "")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    recent = sorted(items, key=lambda it: it.get("first_seen_at") or "", reverse=True)[
        : CONFIG.get("rss_max_items", 100)]
    entries = []
    for it in recent:
        chain_name = CHAIN_NAME.get(it["chain"], it["chain"])
        title = f"【{chain_name}】{it['name']}"
        link = f"{site_url}{item_url(it)}"
        guid = f"{it['chain']}-{it['product_id']}"
        try:
            pub = datetime.datetime.fromisoformat(it.get("first_seen_at"))
            pub_html = "\n<pubDate>" + pub.strftime("%a, %d %b %Y %H:%M:%S %z") + "</pubDate>"
        except (TypeError, ValueError):
            pub_html = ""
        entries.append(
            f"""<item>
<title>{esc(title)}</title>
<link>{esc(link)}</link>
<guid isPermaLink="false">{esc(guid)}</guid>
<category>{esc(chain_name)}</category>{pub_html}
</item>"""
        )
    items_xml = "\n".join(entries)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>{esc(CONFIG["site_title"])}</title>
<link>{esc(site_url)}</link>
<description>{esc(CONFIG["site_description"])}</description>
<lastBuildDate>{now}</lastBuildDate>
{items_xml}
</channel>
</rss>
"""


def generate_sitemap(items: list[dict], months: list[str]) -> str:
    site_url = CONFIG.get("site_url", "")
    today = datetime.datetime.now(JST).date().isoformat()
    urls = [site_url, site_url + "archive/", site_url + "chains/"]
    for c in CHAINS:
        urls.append(f"{site_url}chains/{c['slug']}/")
    for m in months:
        urls.append(f"{site_url}archive/{m}/")
    for it in items:
        urls.append(site_url + item_url(it))
    body = "\n".join(
        f"<url><loc>{esc(u)}</loc><lastmod>{today}</lastmod></url>" for u in urls
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{body}
</urlset>
"""


def main() -> int:
    items = load_all_products()
    DOCS.mkdir(parents=True, exist_ok=True)

    (DOCS / "index.html").write_text(render_top(items), encoding="utf-8")

    # アーカイブ: 終了した限定メニューを開始月ごとに。0件の月のページは作らない
    ended = ended_items(items)
    months = sorted({m for it in ended if (m := start_month(it))})
    (DOCS / "archive").mkdir(parents=True, exist_ok=True)
    (DOCS / "archive" / "index.html").write_text(render_archive_index(items), encoding="utf-8")
    for m in months:
        month_items = [it for it in ended if start_month(it) == m]
        mdir = DOCS / "archive" / m
        mdir.mkdir(parents=True, exist_ok=True)
        label = f"{m[:4]}年{int(m[5:7])}月に始まった限定メニュー"
        page = render_grid_page(
            label, f"{m[:4]}年{int(m[5:7])}月に販売が始まり、すでに終了したマクドナルド・モスバーガーの限定メニュー一覧",
            month_items, CONFIG.get("site_url", "") + f"archive/{m}/", show_filters=True,
        )
        (mdir / "index.html").write_text(page, encoding="utf-8")
    stale_months = 0
    for mdir in (DOCS / "archive").iterdir():
        if mdir.is_dir() and mdir.name not in set(months):
            shutil.rmtree(mdir)
            stale_months += 1

    (DOCS / "chains").mkdir(parents=True, exist_ok=True)
    (DOCS / "chains" / "index.html").write_text(render_chains_top(), encoding="utf-8")
    for c in CHAINS:
        cdir = DOCS / "chains" / c["slug"]
        cdir.mkdir(parents=True, exist_ok=True)
        chain_items = sorted(
            [it for it in items if it["chain"] == c["slug"]],
            key=lambda it: effective_date(it) or "", reverse=True,
        )
        page = render_grid_page(
            f"{c['name']}の限定メニュー", f"{c['name']}の期間限定・新登場メニューの一覧。販売中と販売終了の記録", chain_items,
            CONFIG.get("site_url", "") + f"chains/{c['slug']}/",
            review_chain=c["slug"], status_filter=True, default_status="all",
            empty_text="まだ記録がありません。",
        )
        (cdir / "index.html").write_text(page, encoding="utf-8")

    (DOCS / "items").mkdir(parents=True, exist_ok=True)
    for it in items:
        (DOCS / item_url(it)).write_text(render_item_page(it), encoding="utf-8")

    # 商品削除などで不要になった旧itemページ(オーファン)を掃除する
    valid_item_files = {(DOCS / item_url(it)).name for it in items}
    stale_items = 0
    for f in (DOCS / "items").glob("*.html"):
        if f.name not in valid_item_files:
            f.unlink()
            stale_items += 1

    (DOCS / "search-index.json").write_text(
        json.dumps(build_search_index(items), ensure_ascii=False), encoding="utf-8"
    )
    (DOCS / "rss.xml").write_text(generate_rss(items), encoding="utf-8")
    (DOCS / "sitemap.xml").write_text(generate_sitemap(items, months), encoding="utf-8")
    (DOCS / "robots.txt").write_text(generate_robots(), encoding="utf-8")
    (DOCS / "CNAME").write_text(
        CONFIG.get("site_url", "").replace("https://", "").strip("/") + "\n", encoding="utf-8"
    )
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")

    print(
        f"generated: index.html, {len(months)} archive months, {len(CHAINS)} chain pages, "
        f"{len(items)} item pages, rss.xml, sitemap.xml, search-index.json"
        + (f" / removed {stale_items} stale item pages" if stale_items else "")
        + (f" / removed {stale_months} stale archive dirs" if stale_months else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
