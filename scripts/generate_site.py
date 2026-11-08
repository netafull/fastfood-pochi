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
CHAIN_SOURCE = {"mcdonalds": "マクドナルド公式サイト", "mos": "モスバーガー公式サイト", "matsuya": "松屋公式サイト",
                "matsunoya": "松のや公式サイト", "yoshinoya": "吉野家公式サイト", "sukiya": "すき家公式サイト",
                "katsuya": "かつや公式サイト", "karayama": "からやま公式サイト", "marugame": "丸亀製麺公式サイト",
                "burgerking": "バーガーキング公式サイト"}
# 全チェーン名の並び(ページの説明文用): 「マクドナルド・モスバーガー・松屋・松のや・吉野家・すき家」
CHAIN_LIST_TEXT = "・".join(c["name"] for c in CHAINS)
# 「終了日」の注意書き。公式発表の最終販売日ではなく、一覧から消えたことの観測日
ENDED_NOTE = "公式サイトの一覧から消えたことを確認した日"
# バーガーキングは「期間限定」で絞り込んだ一覧から消えた日(絞り込みから外れた場合も含む)
BK_ENDED_NOTE = "公式サイトの「期間限定」の一覧から消えたことを確認した日"

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


# 公式に「期間限定」の表示が無く、「おすすめ」掲載と新規掲載を記録するチェーン(松屋フーズ系)
MATSUYA_FAMILY = ("matsuya", "matsunoya")


# 公式サイトに栄養成分が載っていない、アークランド系(かつや・からやま。同じテンプレートの公式サイト)
ARCLAND_FAMILY = ("katsuya", "karayama")


def is_matsuya_family(chain: str | None) -> bool:
    return chain in MATSUYA_FAMILY


def is_regular(item: dict) -> bool:
    """定番メニューとして記録した商品か(crawl.py の mark_regular。限定・新メニューに変わると is_regular は外れる)。"""
    return item.get("is_regular") is True


def is_limited_listed(item: dict) -> bool:
    """説明文で「限定メニュー」と呼んでよい商品か。松屋・松のやは公式に限定の表示が無いので呼ばない。
    吉野家は公式が「期間限定」と表示したものだけ。定番は限定ではない。"""
    if is_regular(item):
        return False
    if is_matsuya_family(item.get("chain")):
        return False
    if item.get("chain") in ("yoshinoya", "sukiya", "marugame", "burgerking", *ARCLAND_FAMILY):
        return item.get("limited_kind") == "期間限定"
    return True


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


def season_observed_start(item: dict) -> str:
    """発売日が分からないときの「記録上の開始日」。再登場した商品は、再登場を確認した日(今季の起点)。"""
    rel = item.get("relaunches") or []
    if rel and rel[-1].get("reappeared_at"):
        return rel[-1]["reappeared_at"]
    return (item.get("first_seen_at") or "")[:10]


def effective_date(item: dict) -> str | None:
    """販売開始日。取れなかった商品は初回に見つけた日で代替する。"""
    return item.get("launch_date") or season_observed_start(item) or None


def is_ended(item: dict) -> bool:
    return item.get("status") == "ended"


# 注意: item_url はサイトルートからの相対パス(先頭に / が無い)。**カードのhrefに使うときは先頭に "/" を付ける**。
# 付け忘れると、/chains/xxx/ や /weeks/xxx/ のような階層のあるページで /chains/xxx/items/… を指して404になる
# (2026-10-15に、ファストフードポチのチェーンページとコンビニポチのチェーン・週別ページで発覚)。
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
    first = season_observed_start(item)
    if is_regular(item):
        # 定番には発売日が無い。記録を始めた日から表示する(発売日ではない)
        if is_ended(item):
            s = f"記録開始 {first}〜{item.get('ended_observed_at') or ''}"
            return s + (f"（終了日は{ENDED_NOTE}）" if detailed else "")
        return f"記録開始 {first}〜 販売中"
    head = start or f"{first}（記録開始日）"
    if is_ended(item):
        if item.get("official_end_date"):
            # バーガーキング: 公式イベントの終了日が商品と確実にひも付いたときは、その日付を終了日にする
            s = f"{head}〜{item['official_end_date']}"
            if detailed:
                s += "（終了日は公式イベントの終了日）"
            return s
        s = f"{head}〜{item.get('ended_observed_at') or ''}"
        if detailed:
            s += f"（終了日は{BK_ENDED_NOTE if item.get('chain') == 'burgerking' else ENDED_NOTE}）"
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
    return f"""<a class="item" href="/{esc(item_url(item))}" data-kcal="{kcal if kcal is not None else ''}"
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
  --mcdonalds: #da291c; --mos: #1b7f3b; --matsuya: #a34a00; --matsunoya: #7a1f5c; --yoshinoya: #1f5fad; --sukiya: #8a6a00; --katsuya: #455a64; --karayama: #c2185b; --marugame: #e65100; --burgerking: #512da8;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #14151a; --card: #1e2027; --text: #e8e8e6; --muted: #9a9a96;
    --line: #2c2e36; --accent: #26a69a;
    /* 暗い背景でも見出しの下線・バッジが沈まないよう、松屋・松のや・吉野家・すき家だけ少し明るくする */
    --matsuya: #b85a00; --matsunoya: #a83a82; --yoshinoya: #3a7bd0; --sukiya: #b8921a; --katsuya: #6b8794; --karayama: #d6477f; --marugame: #ff8a3d; --burgerking: #7e57c2; }
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text);
  font-family: "Hiragino Sans", "Noto Sans JP", sans-serif; line-height: 1.6; }
a { color: inherit; }
header { padding: 24px 16px 12px; max-width: 980px; margin: 0 auto; }
/* サイト名はトップだけ h1、他のページでは div.site-name(見た目は h1 と同じ) */
header .site-name { font-size: 2em; font-weight: bold; }
header h1 a, header .site-name a { text-decoration: none; font-size: 22px;
  display: inline-flex; align-items: center; gap: 8px; }
/* ロゴは文字とほぼ同じ高さに揃える(96px画像を縮小して表示。姉妹サイトと同じ) */
header h1 img, header .site-name img { width: 44px; height: 44px; }
header p { color: var(--muted); font-size: 13px; margin-top: 4px; }
nav.crumbs { font-size: 12px; color: var(--muted); margin-top: 8px; }
nav.crumbs a { text-decoration: none; color: var(--accent); }
.sites { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; align-items: baseline; }
.sites .lbl { font-size: 12px; color: var(--muted); }
.sites a { font-size: 12px; padding: 3px 10px; border-radius: 999px;
  border: 1px solid var(--line); background: var(--card); text-decoration: none; }
main { max-width: 980px; margin: 0 auto; padding: 8px 16px 48px; }
/* 一覧ページの見出し(h1.pt)は、見た目を h2 と同じにする */
h2, h1.pt { font-size: 18px; padding-left: 10px; border-left: 4px solid var(--accent); margin: 24px 0 12px; }
.sort-bar { display: flex; gap: 8px; margin: 8px 0 16px; flex-wrap: wrap; align-items: center; }
.sort-bar select, .sort-bar input { font-size: 13px; padding: 5px 8px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--card); color: var(--text); font-family: inherit; }
.chain-group h3 { font-size: 15px; margin: 18px 0 8px; padding-left: 8px; border-left: 4px solid var(--line); }
.chain-group[data-chain="mcdonalds"] h3 { border-color: var(--mcdonalds); }
.chain-group[data-chain="mos"] h3 { border-color: var(--mos); }
.chain-group[data-chain="matsuya"] h3 { border-color: var(--matsuya); }
.chain-group[data-chain="matsunoya"] h3 { border-color: var(--matsunoya); }
.chain-group[data-chain="yoshinoya"] h3 { border-color: var(--yoshinoya); }
.chain-group[data-chain="sukiya"] h3 { border-color: var(--sukiya); }
.chain-group[data-chain="katsuya"] h3 { border-color: var(--katsuya); }
.chain-group[data-chain="karayama"] h3 { border-color: var(--karayama); }
.chain-group[data-chain="marugame"] h3 { border-color: var(--marugame); }
.chain-group[data-chain="burgerking"] h3 { border-color: var(--burgerking); }
.regular-menu h3 { font-size: 15px; margin: 18px 0 8px; padding-left: 8px; border-left: 4px solid var(--line); }
.regular-menu .lead { font-size: 13px; color: var(--muted); margin-bottom: 4px; }
table.pricelog { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
table.pricelog th, table.pricelog td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }
table.pricelog td.num { white-space: nowrap; }
table.pricelog td.up { color: #c62828; }
table.pricelog td.down { color: #2e7d32; }
.tablewrap { overflow-x: auto; }
.grid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }
.item { display: block; background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; padding: 12px; text-decoration: none; }
.item:hover { border-color: var(--accent); }
.item .badge { display: inline-block; font-size: 10px; font-weight: 700; color: #fff;
  border-radius: 4px; padding: 1px 6px; margin-bottom: 6px; }
.item[data-chain="mcdonalds"] .badge { background: var(--mcdonalds); }
.item[data-chain="mos"] .badge { background: var(--mos); }
.item[data-chain="matsuya"] .badge { background: var(--matsuya); }
.item[data-chain="matsunoya"] .badge { background: var(--matsunoya); }
.item[data-chain="yoshinoya"] .badge { background: var(--yoshinoya); }
.item[data-chain="sukiya"] .badge { background: var(--sukiya); }
.item[data-chain="katsuya"] .badge { background: var(--katsuya); }
.item[data-chain="karayama"] .badge { background: var(--karayama); }
.item[data-chain="marugame"] .badge { background: var(--marugame); }
.item[data-chain="burgerking"] .badge { background: var(--burgerking); }
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
.detail .tablewrap { overflow-x: auto; }
.detail table.multi th, .detail table.multi td { width: auto; text-align: right; white-space: nowrap; }
.detail table.multi th:first-child { text-align: left; }
.detail table.pv { max-width: 320px; }
/* 商品ページの節見出しは h2。見た目は従来の h3 と同じにする */
.detail h2 { margin: 16px 0 0; padding: 0; border: 0; font-size: 14px; }
.detail h1 .variant { font-size: 14px; font-weight: 400; color: var(--muted); }
.detail ul.rel { margin-top: 8px; padding-left: 20px; font-size: 13px; }
.detail ul.rel li { margin: 4px 0; }
.detail ul.rel a { color: var(--accent); }
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
/* カード一覧(.grid)の中に差し込まれる枠は、カード1枚ぶんのマス(列幅)を使う */
.grid > .ad-slot { max-width: none; margin: 0; }
"""


# カード一覧の中に差し込む広告枠の間隔と、1ページ全体での上限(ヘッダー下の1枠は別)。
# SORT_JSがこの値を使うので、変えるとJSも変わる(コンビニポチと同じ仕組み)
ADS_EVERY = 12
ADS_MAX_PER_GRID = 3


def render_ad_slot() -> str:
    """手動設置のディスプレイ広告ユニット。

    adsense_client_id / adsense_ad_slot のどちらかが未設定なら何も出さない。
    サーバー側でHTMLに置くのはヘッダー下と商品ページの枠だけ。カード一覧(.grid)の
    中の枠は、並び替え・絞り込みのたびに位置が変わるのでSORT_JSが差し込む
    (render_ad_template参照)。push(読み込み)は枠ごとのインラインscriptではなく、
    ページ下部のAD_JS(syncAds)がまとめて行う。
    data-ad-formatはrectangle(autoだと縦長の広告が来る)
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
</div>"""


def render_ad_template() -> str:
    """SORT_JSがカード一覧に差し込む広告枠の雛形。広告未設定なら空(=JSは何も差し込まない)。

    <template>の中身はDOMに入らないので、これ自体はpushされない。
    JS無効・検索エンジンにはカードだけが全件見える"""
    ad = render_ad_slot()
    return f'<template id="ad-template">{ad}</template>' if ad else ""


# 広告のpush(AdSenseへ読み込みを依頼する)をまとめて行う。広告が設定されているページだけに入る。
# 一度pushした<ins>は再利用できないので、印(dataset.pushed)を付けて二重pushを防ぐ。
# 隠れている枠は幅0で失敗し、あとで表示しても二度と入らないので、adsbygoogleクラスを
# 外してAdSenseの対象から除外しておく(次のsyncAdsで表示中なら戻してpushする)
AD_JS = """
<script>
function syncAds() {
  document.querySelectorAll("ins[data-ad-slot]").forEach(function (ins) {
    if (ins.dataset.pushed) return;
    if (ins.offsetParent === null) {
      ins.classList.remove("adsbygoogle");
      return;
    }
    ins.classList.add("adsbygoogle");
    ins.dataset.pushed = "1";
    (window.adsbygoogle = window.adsbygoogle || []).push({});
  });
}
syncAds();
</script>
"""


def updated_label(now: datetime.datetime | None = None) -> str:
    """トップの「最終更新: 2026年10月14日 07:37」。電書・家電・林檎ポチと同じ書式(日本時間)。
    トップページだけに出す(全ページに入れると、毎日すべてのページのファイルが変わってしまう)。"""
    now = now or datetime.datetime.now(JST)
    return f"最終更新: {now.strftime('%Y年%m月%d日 %H:%M')}"


def page_shell(title: str, description: str, body: str, canonical: str, extra_head: str = "",
               header_ad: bool = False, header_note: str = "", is_top: bool = False) -> str:
    site_url = CONFIG.get("site_url", "")
    # トップページのtitleは、サイト名のあとに説明を添える(姉妹サイトと同じ「名前｜説明」の形)
    page_title = (f'{esc(title)}｜{esc(CONFIG["site_title"])}' if title != CONFIG["site_title"]
                  else esc(CONFIG.get("top_title") or title))

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
    ad_js = AD_JS if adsense_id and CONFIG.get("adsense_ad_slot") else ""

    # 見出しの左に置くポチシリーズ共通のアイコン。画像が置かれていなければ何も出さない
    logo_img = (
        '<img src="/assets/logo.png" alt="" width="44" height="44">'
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

    # サイト名の見出し: トップだけ h1。他のページはページ自身の見出しを h1 にするため div にする
    site_name_tag = "h1" if is_top else "div"
    site_name_cls = "" if site_name_tag == "h1" else ' class="site-name"'

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
<{site_name_tag}{site_name_cls}><a href="/">{logo_img}{esc(CONFIG["site_title"])}</a></{site_name_tag}>
<p>{esc(CONFIG["site_description"])}{(" ｜ " + esc(header_note)) if header_note else ""}</p>
</header>
{header_ad_html}
<main>
{body}
</main>
<footer>
価格・販売期間・販売状況は取得時点のものです。最新の状況は各社公式サイトでご確認ください。
当サイトはマクドナルド、モスバーガー、松屋、松のや、吉野家、すき家、かつや、からやま、丸亀製麺、バーガーキング各社とは関係のない非公式サイトです。
公式サイト： <a href="https://www.mcdonalds.co.jp/">マクドナルド</a> ／ <a href="https://www.mos.jp/">モスバーガー</a> ／ <a href="https://www.matsuyafoods.co.jp/matsuya/">松屋</a> ／ <a href="https://www.matsuyafoods.co.jp/matsunoya/">松のや</a> ／ <a href="https://www.yoshinoya.com/">吉野家</a> ／ <a href="https://www.sukiya.jp/">すき家</a> ／ <a href="https://www.arclandservice.co.jp/katsuya/">かつや</a> ／ <a href="https://www.arclandservice.co.jp/karayama/">からやま</a> ／ <a href="https://jp.marugame.com/">丸亀製麺</a> ／ <a href="https://www.burgerking.co.jp/">バーガーキング</a>
{policy_link}｜ <a href="/rss.xml">RSS</a> ｜ <a href="/archive/">終了した限定メニュー</a> ｜ <a href="/prices/">価格改定の記録</a>
{related_html}
</footer>
{ad_js}
</body>
</html>
"""
    if adsense_id:
        # AdSenseダッシュボードではvignette(全画面)広告をサブドメイン単位で無効化
        # できないため、リンクごとにdata-google-vignette="false"を付ける
        out = re.sub(r'<a (?![^>]*data-google-vignette)', '<a data-google-vignette="false" ', out)
    return out


# 広告の配置計算。DOMに触れない純粋関数(テストからnodeで単体実行する)。
# counts: グリッドごとの「見えているカード数」(見えていないグリッドは0)
# 返り値: グリッドごとの、広告を差し込む位置(その位置のカードの直前)の配列
AD_PLAN_JS = """
function computeAdPlan(counts, every, max) {
  var eligible = [];
  counts.forEach(function (n, i) { if (n > every) eligible.push(i); });
  var chosen = eligible;
  if (eligible.length > max) {
    chosen = [];
    for (var k = 0; k < max; k++) chosen.push(eligible[Math.floor(k * eligible.length / max)]);
  }
  var perGrid = Math.max(1, Math.floor(max / (chosen.length || 1)));
  var left = max;
  var plan = counts.map(function () { return []; });
  chosen.forEach(function (gi) {
    var placed = 0;
    for (var i = every; i < counts[gi] && left > 0 && placed < perGrid; i += every) {
      plan[gi].push(i);
      left--;
      placed++;
    }
  });
  return plan;
}
"""


SORT_JS = """
<script>
__AD_PLAN__(function () {
  // チェーンごとのグループ(.chain-group)内でだけ並び替える。絞り込みはカード単位・グループ単位で隠す
  var grids = Array.prototype.slice.call(document.querySelectorAll('[data-sortable]'));
  if (!grids.length) return;
  var select = document.getElementById('sort-select');
  var chainSel = document.getElementById('chain-filter');
  var statusSel = document.getElementById('status-filter');
  // 広告枠。サーバー側はグリッドに枠を置かず、並び替え・絞り込みのたびにここで差し込み直す。
  // 雛形(#ad-template)が無ければ(広告未設定・非対応ブラウザ)何もしない
  var AD_EVERY = __ADS_EVERY__;
  var AD_MAX = __ADS_MAX__;
  var adTpl = document.getElementById('ad-template');
  function placeAds() {
    var proto = adTpl && adTpl.content && adTpl.content.firstElementChild;
    if (!proto) return;
    // 広告枠(.ad-slot)は、表示済みの広告を読み込み直さないよう、DOMから外さない。
    // 並べ替えで動かすのはカード(.item)だけ。枠をDOMから外すのは、下の「余った枠を捨てる」1か所だけ。
    // 必要な枠数が今の枠数と同じなら枠はそのまま、足りない分だけ新規に作る(pushは新しい枠だけ)。
    // 見えていないグリッドの枠は触らず残しておき、再表示されたら再利用する
    var allItems = function (g) { return Array.prototype.slice.call(g.querySelectorAll('.item')); };
    // 販売状況の絞り込みで隠れたカードは数えない
    var visibleItems = function (g) {
      return allItems(g).filter(function (el) { return el.style.display !== 'none'; });
    };
    var shown = grids.map(function (g) { return g.offsetParent !== null; });
    var counts = grids.map(function (g, i) { return shown[i] ? visibleItems(g).length : 0; });
    var plan = computeAdPlan(counts, AD_EVERY, AD_MAX);
    grids.forEach(function (grid, gi) {
      if (!shown[gi]) return;
      var pos = plan[gi];
      var slots = Array.prototype.filter.call(grid.children, function (el) {
        return el.classList.contains('ad-slot');
      });
      while (slots.length > pos.length) {
        var extra = slots.pop();
        extra.parentNode.removeChild(extra);
      }
      while (slots.length < pos.length) {
        var fresh = proto.cloneNode(true);
        grid.appendChild(fresh);
        slots.push(fresh);
      }
      if (!slots.length) return;
      var put = function (el, ref) {
        if (el.classList.contains('ad-slot')) return;
        if (ref) grid.insertBefore(el, ref); else grid.appendChild(el);
      };
      var vis = visibleItems(grid);
      var hidden = allItems(grid).filter(function (el) { return vis.indexOf(el) < 0; });
      var from = 0;
      pos.forEach(function (p, j) {
        for (var k = from; k < p; k++) put(vis[k], slots[j]);
        from = p;
      });
      for (var k = from; k < vis.length; k++) put(vis[k], null);
      hidden.forEach(function (el) { put(el, null); });
    });
  }
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
    placeAds();
    // 初回はページ末尾のAD_JSがpushするので、まだ無ければ何もしない
    if (window.syncAds) window.syncAds();
  }
  if (select) select.addEventListener('change', apply);
  if (chainSel) chainSel.addEventListener('change', apply);
  if (statusSel) statusSel.addEventListener('change', apply);
  apply();
})();
</script>
""".replace("__AD_PLAN__", AD_PLAN_JS).replace("__ADS_EVERY__", str(ADS_EVERY)).replace("__ADS_MAX__", str(ADS_MAX_PER_GRID))


def render_grouped(items: list[dict], sortable: bool = True) -> str:
    """商品をチェーンごと(config.jsonのchains順)にまとめ、各チェーンの中は販売開始日の新しい順に並べる。"""
    attr = " data-sortable" if sortable else ""
    sections = []
    active = [c for c in CHAINS if any(it["chain"] == c["slug"] for it in items)]
    for c in active:
        group = [it for it in items if it["chain"] == c["slug"]]
        group.sort(key=lambda it: effective_date(it) or "", reverse=True)
        cards = "\n".join(render_card(it) for it in group)
        sections.append(
            f'<section class="chain-group" data-chain="{esc(c["slug"])}">\n'
            f'<h3>{esc(c["name"])} (<span class="cnt">{len(group)}</span>件)</h3>\n'
            # 広告枠はここでは置かない(SORT_JSが見えているカードの間に差し込む)
            f'<div class="grid"{attr}>\n{cards}\n</div>\n</section>'
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
                      empty_text: str = "商品がありません。", seo_title: str | None = None,
                      extra_head: str = "", extra_body: str = "") -> str:
    # seo_title: <title>に入れる文言。省略すると見出し(title)と同じ
    reviews_section = render_review_cards("ネタフルの最新レビュー", latest_reviews(review_chain, 5))
    if not items:
        body = f"<h1 class='pt'>{esc(title)}</h1><p class='empty'>{esc(empty_text)}</p>\n{extra_body}\n{reviews_section}"
        return page_shell(seo_title or title, description, body, canonical, extra_head=extra_head)
    filters = filter_bar(status_filter, default_status) if show_filters else ""
    on = sum(1 for it in items if not is_ended(it))
    count_text = f"{len(items)}件" if not status_filter else f"{len(items)}件（販売中{on}件）"
    body = f"""<h1 class="pt">{esc(title)} ({count_text})</h1>
{filters}
{render_grouped(items)}
{render_ad_template() if show_filters else ""}
{SORT_JS if show_filters else ""}
{extra_body}
{reviews_section}"""
    return page_shell(seo_title or title, description, body, canonical, extra_head=extra_head, header_ad=True)


def category_order(chain_slug: str) -> list[str]:
    """チェーンのカテゴリの並び(公式サイトの順。config のカテゴリ設定から)。無いチェーンは空。"""
    if chain_slug == "mcdonalds":
        return [p["label"] for p in CONFIG.get("mcdonalds_pages", []) if p.get("label")]
    return [c["label"] for c in CONFIG.get(f"{chain_slug}_categories", []) if isinstance(c, dict) and c.get("label")]


def render_regular_section(chain: dict, items: list[dict]) -> str:
    """チェーン別ページの「定番メニュー（N品）」。限定の一覧の後ろに、カテゴリごとに並べる。
    ここのカードは並び替え・絞り込みの対象にしない(data-sortable を付けない)。"""
    if not items:
        return ""
    order = category_order(chain["slug"])
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(it.get("category") or "その他", []).append(it)

    def key(name: str):
        return (name == "その他", order.index(name) if name in order else len(order), name)

    parts = []
    for name in sorted(groups, key=key):
        group = sorted(groups[name], key=lambda it: (nfkc(it.get("name")), str(it["product_id"])))
        parts.append(f'<h3>{esc(name)}（{len(group)}品）</h3>\n<div class="grid">\n'
                     + "\n".join(render_card(it) for it in group) + "\n</div>")
    return (f'<section class="regular-menu" id="regular">\n<h2>定番メニュー（{len(items)}品）</h2>\n'
            f'<p class="lead">定番メニューは記録を始めた日から、価格の変わった日を残しています。'
            f'<a href="/prices/{esc(chain["slug"])}/">{esc(chain["name"])}の価格改定の記録</a></p>\n'
            + "\n".join(parts) + "\n</section>")


def price_change_events(items: list[dict]) -> list[dict]:
    """価格が変わった日の記録(price_history の隣り合う2件、price_variants_history のサイズ別)を、新しい日付順に並べる。
    価格が取れなかった(None)記録・同じ価格の記録(再登場時の再掲など)は変更に数えない。"""
    chain_idx = {c["slug"]: i for i, c in enumerate(CHAINS)}
    events: list[dict] = []
    for it in items:
        base: set[tuple] = set()
        hist = it.get("price_history") or []
        for prev, cur in zip(hist, hist[1:]):
            a, b = prev.get("price"), cur.get("price")
            if a is None or b is None or a == b:
                continue
            events.append({"date": cur["date"], "item": it, "label": "", "old": a, "new": b})
            base.add((cur["date"], a, b))
        vh = it.get("price_variants_history") or []
        for prev, cur in zip(vh, vh[1:]):
            old = {(v.get("label") or "標準"): v["price"] for v in prev.get("variants") or []}
            for v in cur.get("variants") or []:
                lb = v.get("label") or "標準"
                a, b = old.get(lb), v["price"]
                if a is None or a == b or (cur["date"], a, b) in base:
                    continue  # 代表の価格と同じ変更は二重に載せない
                events.append({"date": cur["date"], "item": it, "label": lb, "old": a, "new": b})
    events.sort(key=lambda e: (e["date"], -chain_idx.get(e["item"]["chain"], 99), e["item"]["name"]), reverse=True)
    return events


def recording_start(items: list[dict]) -> str:
    return min(((it.get("first_seen_at") or "")[:10] for it in items if it.get("first_seen_at")), default="")


def ja_date(text: str) -> str:
    d = _date_or_none(text)
    return f"{d.year}年{d.month}月{d.day}日" if d else text


def render_prices(all_items: list[dict], chain: dict | None = None) -> str:
    """価格改定の記録。chain を渡すとそのチェーンだけ(JSなしで絞り込めるよう、チェーンごとに別ページにしている)。"""
    items = [it for it in all_items if chain is None or it["chain"] == chain["slug"]]
    events = price_change_events(items)
    scope = chain["name"] if chain else "全チェーン"
    site_url = CONFIG.get("site_url", "")
    nav = ('<div class="chainlist">\n'
           + f'<a href="/prices/">すべて</a>\n'
           + "\n".join(f'<a href="/prices/{c["slug"]}/">{esc(c["name"])}</a>' for c in CHAINS) + "\n</div>")
    start = recording_start(items)
    if events:
        rows = []
        for e in events:
            it = e["item"]
            diff = e["new"] - e["old"]
            name = it["name"] + (f"（{e['label']}）" if e["label"] else "")
            cls = "up" if diff > 0 else "down"
            sign = "+" if diff > 0 else "−"
            rows.append(
                f"<tr><td class='num'>{esc(e['date'])}</td><td>{esc(CHAIN_NAME.get(it['chain'], it['chain']))}</td>"
                f"<td><a href=\"/{esc(item_url(it))}\">{esc(name)}</a></td>"
                f"<td class='num'>{fmt_yen(e['old'])} → {fmt_yen(e['new'])}</td>"
                f"<td class='num {cls}'>{sign}{abs(diff):g}円</td></tr>")
        table = ("<div class='tablewrap'><table class='pricelog'>"
                 "<tr><th>日付</th><th>チェーン</th><th>商品</th><th>価格</th><th>差額</th></tr>\n"
                 + "\n".join(rows) + "</table></div>")
        lead = (f"<p class='empty'>限定メニューも定番メニューも、毎日の巡回で価格の変わった日を残しています。"
                f"記録は{esc(ja_date(start))}からです。価格は税込で、公式サイトの一覧・詳細に出ていた金額です。"
                "サイズ別の価格は商品名のあとに（）で示しています。</p>")
        body = f"<h1 class='pt'>価格改定の記録（{scope}・{len(events)}件）</h1>\n{lead}\n{nav}\n{table}"
    else:
        since = f"記録は{ja_date(start)}から" if start else "記録はこれから始まります"
        body = (f"<h1 class='pt'>価格改定の記録（{scope}）</h1>\n{nav}\n"
                f"<p class='empty'>まだ価格の変更はありません。{esc(since)}。</p>")
    path = f"prices/{chain['slug']}/" if chain else "prices/"
    desc = (f"{scope}のメニューの価格改定を、変更のあった日付の新しい順に記録。"
            "限定メニューも定番メニューも、毎日の巡回で価格の変わった日を残しています。")
    return page_shell(f"価格改定の記録（{scope}）" if chain else "価格改定の記録", desc, body, site_url + path)


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
{render_grouped(shown)}
{render_ad_template()}
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
        header_note=updated_label(), is_top=True,
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
        body = ("<h1 class='pt'>終了した限定メニュー</h1>"
                "<p class='empty'>まだ販売が終わったメニューはありません。"
                "公式サイトの一覧から消えたことを連続で確認すると、ここに並びます。</p>")
    else:
        rows = "\n".join(
            f'<a href="/archive/{m}/">{m[:4]}年{int(m[5:7])}月に始まった限定メニュー<span class="n">{n}件</span></a>'
            for m, n in sorted(by_month.items(), reverse=True)
        )
        body = f"""<h1 class="pt">終了した限定メニュー ({len(ended)}件)</h1>
<p class="empty">販売開始月ごとにまとめています。終了日は公式サイトの一覧から消えたことを確認した日です。</p>
<div class="monthlist">
{rows}
</div>"""
    return page_shell("終了した限定メニュー", f"販売が終わった{CHAIN_LIST_TEXT}の限定メニューを、販売開始月ごとにまとめたアーカイブ",
                       body, CONFIG.get("site_url", "") + "archive/")


def render_chains_top() -> str:
    links = "\n".join(f'<a href="/chains/{c["slug"]}/">{esc(c["name"])}</a>' for c in CHAINS)
    body = f'<h1 class="pt">チェーン一覧</h1><div class="chainlist">{links}</div>'
    return page_shell("チェーン一覧", f"{CHAIN_LIST_TEXT}の限定メニュー一覧", body,
                       CONFIG.get("site_url", "") + "chains/")


NUTRITION_LABELS = [
    ("kcal", "熱量", "kcal"), ("protein_g", "たんぱく質", "g"),
    ("fat_g", "脂質", "g"), ("carbs_g", "炭水化物", "g"),
    ("sugar_g", "　糖類", "g"), ("fiber_g", "　食物繊維", "g"),
    ("salt_g", "食塩相当量", "g"),
]
# 「ほか」の項目がこの数以下ならそのまま表に並べ、超えたら折りたたむ(モスは20項目前後)
OTHER_INLINE_MAX = 3


def _nutrition_rows(nut: dict) -> tuple[list[tuple[str, str]], dict]:
    rows = []
    for key, label, unit in NUTRITION_LABELS:
        v = nut.get(key)
        if v is not None:
            rows.append((label, f"{v:g}{unit}"))
    return rows, (nut.get("other") or {})


def pdf_source_html(src: dict | None) -> str:
    """公式PDFから読み取った値の出典(PDFへのリンクと、PDFの版)。"""
    if not src or not src.get("url"):
        return ""
    label = src.get("label") or "公式PDF"
    ver = f"（{esc(src['version_text'])}）" if src.get("version_text") else ""
    return (f"出典：公式PDF <a href=\"{esc(src['url'])}\" target=\"_blank\" rel=\"noopener\">「{esc(label)}」</a>{ver}")


def nutrition_footnote(item: dict) -> str:
    """松屋: 栄養成分の基準日と、公式の注記(みそ汁・福神漬を含む等)。吉野家・すき家: 公式PDFの版と注記。"""
    bits = []
    if item.get("nutrition_as_of"):
        try:
            d = datetime.date.fromisoformat(item["nutrition_as_of"])
            bits.append(f"公式サイトの表示は{d.year}年{d.month}月{d.day}日現在")
        except ValueError:
            pass
    if item.get("nutrition_note"):
        bits.append(f"公式の注記：{item['nutrition_note'].lstrip('※').rstrip('。')}")
    out = f"<p class='note'>{esc('。'.join(bits))}。</p>" if bits else ""
    src = pdf_source_html(item.get("nutrition_source"))
    if src:
        out += f"<p class='note'>{src}。</p>"
    return out


def render_nutrition(item: dict) -> str:
    nut = item.get("nutrition")
    if not nut and item.get("chain") in ARCLAND_FAMILY:
        # かつや・からやま: 栄養成分は公式サイトのHTMLにもPDFにも載っていない(アレルゲン一覧PDFだけある)
        out = "<h2>栄養成分</h2><p class='note'>栄養成分は公式サイトに掲載がありません。</p>"
        if item.get("allergen_pdf_url"):
            al = item.get("allergen_pdf_label") or "アレルゲン一覧"
            out += (f"<p class='note'>アレルゲン情報は公式PDFをご覧ください：<a href=\"{esc(item['allergen_pdf_url'])}\" "
                    f"target=\"_blank\" rel=\"noopener\">{esc(al)}（公式PDF）</a></p>")
        else:
            out += "<p class='note'>アレルゲンは公式サイトをご覧ください。</p>"
        return out
    if not nut and item.get("allergen_pdf_url") and item.get("allergens") is not None:
        # すき家: アレルゲンだけがPDFから取れ、栄養成分は取れなかった(PDFに載っていない商品)
        nl = item.get("nutrition_pdf_label") or "栄養成分一覧"
        return ("<h2>栄養成分</h2><p class='note'>栄養成分は公式PDF"
                f"（{esc(nl)}）をご覧ください。この商品は当サイトでは取り込めていません。</p>"
                f"<p><a href=\"{esc(item['nutrition_pdf_url'])}\" target=\"_blank\" rel=\"noopener\">{esc(nl)}（公式PDF）</a></p>")
    if not nut and item.get("allergen_pdf_url"):
        # すき家: 栄養成分とアレルゲンは別々の公式PDF。取り込めなかった商品はリンクで案内する
        nl = item.get("nutrition_pdf_label") or "栄養成分一覧"
        al = item.get("allergen_pdf_label") or "アレルゲン一覧"
        return ("<h2>栄養成分・アレルギー情報</h2><p class='note'>栄養成分・アレルギー情報は公式PDF"
                f"（{esc(nl)}／{esc(al)}）をご覧ください。当サイトでは取り込んでいません。</p>"
                f"<p><a href=\"{esc(item['nutrition_pdf_url'])}\" target=\"_blank\" rel=\"noopener\">{esc(nl)}（公式PDF）</a>"
                f" ／ <a href=\"{esc(item['allergen_pdf_url'])}\" target=\"_blank\" rel=\"noopener\">{esc(al)}（公式PDF）</a></p>")
    if not nut and item.get("nutrition_pdf_url"):
        # 吉野家: 栄養成分・アレルゲンは商品ページに無く、公式の「吉野家メニュー情報」PDFにある。取り込まずリンクで案内する
        label = item.get("nutrition_pdf_label") or "メニュー情報"
        origin = (f" ／ <a href=\"{esc(item['origin_pdf_url'])}\" target=\"_blank\" rel=\"noopener\">食材の原産地情報（PDF）</a>"
                  if item.get("origin_pdf_url") else "")
        return ("<h2>栄養成分・アレルギー情報</h2><p class='note'>栄養成分・アレルギー情報は公式の"
                f"「{esc(label)}」PDFをご覧ください。当サイトでは取り込んでいません。</p>"
                f"<p><a href=\"{esc(item['nutrition_pdf_url'])}\" target=\"_blank\" rel=\"noopener\">"
                f"{esc(label)}（公式PDF）</a>{origin}</p>")
    if not nut and item.get("chain") == "burgerking":
        # バーガーキング: 栄養成分・アレルギー情報はAPIにほぼ無く(アレルゲンは全商品まとめのPDF1本)、当サイトでは扱わない
        link = (f" <a href=\"{esc(item['official_url'])}\" target=\"_blank\" rel=\"noopener\">公式の商品ページ</a>"
                if item.get("official_url") else "")
        return ("<h2>栄養成分・アレルギー情報</h2><p class='note'>栄養成分・アレルギー情報は当サイトでは掲載していません。"
                f"公式サイトでご確認ください。{link}</p>")
    if not nut:
        return ("<h2>栄養成分</h2><p class='note'>栄養成分は公式サイトに掲載がありませんでした"
                "（セット商品や一部の限定商品は載らないことがあります）。</p>")
    variants = item.get("nutrition_variants") or []
    if len(variants) > 1:
        # 松屋: セットと単品、または小盛/並盛/大盛の値を列で並べる
        parsed = [(v.get("label") or "", _nutrition_rows(v["nutrition"])[0]) for v in variants]
        row_labels = []
        for _, rows in parsed:
            for lb, _v in rows:
                if lb not in row_labels:
                    row_labels.append(lb)
        head = "<tr><th></th>" + "".join(f"<th>{esc(lb or '標準')}</th>" for lb, _ in parsed) + "</tr>"
        body_rows = ""
        for rl in row_labels:
            cells = ""
            for _, rows in parsed:
                d = dict(rows)
                cells += f"<td>{esc(d.get(rl, '－'))}</td>"
            body_rows += f"<tr><th>{esc(rl)}</th>{cells}</tr>"
        return (f"<h2>栄養成分</h2><div class='tablewrap'><table class='multi'>{head}{body_rows}</table></div>"
                f"{nutrition_footnote(item)}")
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
    return f"<h2>栄養成分</h2>{table}{details}{nutrition_footnote(item)}"


def render_allergens(item: dict) -> str:
    a = item.get("allergens")
    if a is None:
        if item.get("nutrition") and item.get("allergen_pdf_url"):
            # すき家: 栄養は取れたがアレルゲンの表に見つからなかった商品。PDFへ案内する
            al = item.get("allergen_pdf_label") or "アレルゲン一覧"
            return (f"<p class='note'>アレルギー情報は公式PDF（<a href=\"{esc(item['allergen_pdf_url'])}\" "
                    f"target=\"_blank\" rel=\"noopener\">{esc(al)}</a>）をご覧ください。この商品は当サイトでは取り込めていません。</p>")
        return ""
    contains = a.get("contains") or []
    text = "、".join(contains) if contains else "表示対象の品目は含まれていません"
    html_ = f"<p class='tags'><strong>アレルゲン（含む）：</strong>{esc(text)}</p>"
    partial = a.get("partial") or []
    if partial:
        ptxt = "、".join(f"{p['name']}{p['mark']}" for p in partial)
        meaning = (item.get("allergens_conditional") or {}).get("meaning")
        mark = (item.get("allergens_conditional") or {}).get("mark") or partial[0]["mark"]
        legend = (f"（{esc(mark)}：{esc(meaning.rstrip('。'))}）" if meaning
                  else "（記号の意味は公式サイトの凡例でご確認ください）")
        html_ += f"<p class='note'>条件つきの表示：{esc(ptxt)}{legend}</p>"
    src = pdf_source_html(item.get("allergen_source"))
    if src:
        html_ += f"<p class='note'>{src}。</p>"
    return html_


def render_origin(item: dict) -> str:
    origin = item.get("origin")
    if not origin:
        return ""
    rows = "".join(f"<tr><th>{esc(o['name'])}</th><td>{esc(o['origin'])}</td></tr>" for o in origin)
    return f"<details><summary>原産地・製造所の情報（{len(origin)}件）を見る</summary><table>{rows}</table></details>"


def render_price_variants(item: dict) -> str:
    """松屋: セットと単品、小盛/並盛/大盛など、複数の価格があるとき内訳を表にする。"""
    pv = item.get("price_variants") or []
    if len(pv) < 2:
        return ""
    rows = "".join(f"<tr><th>{esc(v.get('label') or '標準')}</th><td>{v['price']:g}円（税込）</td></tr>" for v in pv)
    return f"<table class='pv'>{rows}</table>"


def fmt_yen(v) -> str:
    return f"{v:g}円"


def render_variants_history(item: dict) -> str:
    """サイズ別の価格の履歴(price_variants_history)。2件以上(=変更があった)ときだけ、日付×サイズの表にする。"""
    hist = item.get("price_variants_history") or []
    if len(hist) < 2:
        return ""
    labels: list[str] = []
    for h in hist:
        for v in h.get("variants") or []:
            lb = v.get("label") or "標準"
            if lb not in labels:
                labels.append(lb)
    head = "<tr><th>確認した日</th>" + "".join(f"<th>{esc(lb)}</th>" for lb in labels) + "</tr>"
    rows = ""
    for h in hist:
        by = {(v.get("label") or "標準"): v["price"] for v in h.get("variants") or []}
        rows += (f"<tr><th>{esc(h['date'])}〜</th>"
                 + "".join(f"<td>{fmt_yen(by[lb]) if lb in by else '−'}</td>" for lb in labels) + "</tr>")
    return (f"<h3>サイズ別の価格の履歴</h3><div class='tablewrap'><table class='multi'>{head}{rows}</table></div>")


REGULAR_PRICE_NOTE = "記録開始より前の価格は分かりません。"


def render_price_history(item: dict) -> str:
    hist = item.get("price_history") or []
    regular = is_regular(item)
    variants_html = render_variants_history(item)
    if not hist or (len(hist) <= 1 and not regular and not variants_html):
        return ""
    if len(hist) == 1:
        # 定番は価格の変遷が目的なので、履歴が1件でも出す(「いつの時点の価格か」が分かるように)
        h = hist[0]
        body = f"<p>{esc(h['date'])} 時点 {fmt_yen(h['price'])}（税込）</p>" if hist[0].get("price") is not None else ""
        note = (f"<p class='note'>{REGULAR_PRICE_NOTE}価格が変わると、変わった日をここに追記します。</p>"
                if regular else "")
        return f"<h2>価格の履歴</h2>{body}{note}{variants_html}"
    rows = "".join(f"<tr><th>{esc(h['date'])}〜</th><td>{h['price']:g}円{'（再登場時）' if h.get('relaunch') else ''}</td></tr>"
                   for h in hist)
    note = "公式サイトの一覧に出ていた価格を、変わった日に記録しています。"
    if regular:
        note += REGULAR_PRICE_NOTE
    return f"<h2>価格の履歴</h2><table>{rows}</table><p class='note'>{note}</p>{variants_html}"


# ---- 同名商品の書き分け・関連リンク・パンくず・lastmod(SEO) ----

def same_name_key(item: dict) -> tuple[str, str]:
    """同じチェーンで同名とみなす鍵。NFKCにして空白を除いた商品名。"""
    return item["chain"], re.sub(r"\s+", "", nfkc(item.get("name")))


def group_same_name(items: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """同名の商品が2件以上あるものだけ、鍵ごとにまとめる(並びは開始日、商品番号の順)。"""
    groups: dict[tuple[str, str], list[dict]] = {}
    for it in items:
        groups.setdefault(same_name_key(it), []).append(it)
    return {k: sorted(v, key=lambda it: (effective_date(it) or "", str(it["product_id"])))
            for k, v in groups.items() if len(v) > 1}


def _ym(date_text: str | None) -> tuple[int, int] | None:
    m = re.match(r"^(\d{4})-(\d{2})", date_text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def period_label(item: dict) -> str | None:
    """販売期間を月単位にした補足(「2026年9〜10月販売」「2026年10月〜販売中」)。
    ページに出している開始日(販売開始日、無ければ記録開始日)と終了日(終了を確認した日)と同じ値から作る。"""
    start = _ym(item.get("launch_date") or season_observed_start(item))
    if not start:
        return None
    y, m = start
    if not item.get("launch_date"):
        # 販売開始日が分からない商品は、記録を始めた月だけを言う(販売開始とは言わない)
        return f"{y}年{m}月に記録開始"
    if not is_ended(item):
        return f"{y}年{m}月〜販売中"
    end = _ym(item.get("ended_observed_at"))
    if not end or end == start:
        return f"{y}年{m}月販売"
    if end[0] == y:
        return f"{y}年{m}〜{end[1]}月販売"
    return f"{y}年{m}月〜{end[0]}年{end[1]}月販売"


def is_takeout(item: dict) -> bool:
    return item.get("category") == "テイクアウト" or bool(item.get("takeout_only"))


def variant_labels(group: list[dict]) -> dict[str, str]:
    """同名商品の組に、それぞれを区別する短い補足を付ける(商品番号 -> 補足)。
    販売期間 → 価格(テイクアウト用は頭に付ける) → カロリー の順に、全員が区別できる最初のものを使う。
    どれでも区別できない組は空の辞書(補足を付けない)。"""
    def price_label(it):
        if it.get("price") is None:
            return None
        return ("テイクアウト用・" if is_takeout(it) else "") + f"{it['price']:g}円"

    def kcal_label(it):
        kcal = (it.get("nutrition") or {}).get("kcal")
        return f"{kcal:g}kcal" if kcal is not None else None

    for fn in (period_label, price_label, kcal_label):
        labels = [fn(it) for it in group]
        if all(labels) and len(set(labels)) == len(labels):
            return {str(it["product_id"]): lb for it, lb in zip(group, labels)}
    return {}


def _date_or_none(text: str | None) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat((text or "")[:10])
    except ValueError:
        return None


RELATED_MAX = 6


def related_items(item: dict, items: list[dict], limit: int = RELATED_MAX) -> list[dict]:
    """同じチェーンの他の商品。同じカテゴリを優先し、販売開始日が近い順(前後を区別しない)に選ぶ。
    自分自身と同名の商品は除く。現在時刻や乱数に依存しない(同じデータなら同じ結果)。"""
    me = same_name_key(item)
    pool = [o for o in items if o["chain"] == item["chain"] and same_name_key(o) != me]
    base = _date_or_none(effective_date(item))

    def order(o):
        d = _date_or_none(effective_date(o))
        dist = abs((d - base).days) if d and base else 10 ** 6
        return (dist, effective_date(o) or "", str(o["product_id"]))

    pool.sort(key=order)
    cat = item.get("category")
    same_cat = [o for o in pool if cat and o.get("category") == cat]
    rest = [o for o in pool if not (cat and o.get("category") == cat)]
    return (same_cat + rest)[:limit]


def render_item_list(entries: list[tuple[dict, str]]) -> str:
    """商品へのリンクの小さなリスト。entries は (商品, リンクに添える補足)。"""
    lis = []
    for o, label in entries:
        name = o["name"] + (f"（{label}）" if label else "")
        kcal = (o.get("nutrition") or {}).get("kcal")
        bits = [fmt_price(o)] if o.get("price") is not None else []
        if kcal is not None:
            bits.append(f"{kcal:g}kcal")
        bits.append("販売終了" if is_ended(o) else "販売中")
        lis.append(f'<li><a href="/{esc(item_url(o))}">{esc(name)}</a>　{esc("・".join(bits))}</li>')
    return '<ul class="rel">\n' + "\n".join(lis) + "\n</ul>"


def render_same_name(item: dict, group: list[dict], labels: dict[str, str]) -> str:
    """同じ名前の商品ブロック(自分以外の同名ページへのリンク)。"""
    others = [(o, labels.get(str(o["product_id"]), "")) for o in group
              if str(o["product_id"]) != str(item["product_id"])]
    if not others:
        return ""
    return f"<h2>同じ名前の商品</h2>\n{render_item_list(others)}"


def render_related(item: dict, items: list[dict]) -> str:
    rel = related_items(item, items)
    if not rel:
        return ""
    chain_name = CHAIN_NAME.get(item["chain"], item["chain"])
    return f"<h2>同じ時期の{esc(chain_name)}の商品</h2>\n{render_item_list([(o, '') for o in rel])}"


def breadcrumb_ld(trail: list[tuple[str, str]]) -> str:
    """BreadcrumbList の JSON-LD(<script>タグ)。trail は (名前, 絶対URL) の並び。"""
    data = json.dumps({
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": name, "item": url}
            for i, (name, url) in enumerate(trail, 1)
        ],
    }, ensure_ascii=False).replace("<", "\\u003c")
    return f'<script type="application/ld+json">{data}</script>'


def item_lastmod(item: dict) -> str:
    """商品ページのデータが実際に変わった日。初検出日、終了を確認した日、価格が変わった日、
    再登場した日のうち最も新しいもの。last_seen_at・price_checked_at は毎日更新されるので使わない。"""
    dates = [(item.get("first_seen_at") or "")[:10], item.get("ended_observed_at") or ""]
    dates += [h.get("date") or "" for h in item.get("price_history") or []]
    dates += [h.get("date") or "" for h in item.get("price_variants_history") or []]
    for r in item.get("relaunches") or []:
        dates += [r.get("ended_observed_at") or "", r.get("reappeared_at") or ""]
    return max((d for d in dates if _date_or_none(d)), default="")


def chain_seo_title(chain: dict) -> str:
    """チェーン別ページの<title>(サイト名の前の部分)。松屋・松のやは公式に期間限定の表示が無い。"""
    if is_matsuya_family(chain["slug"]):
        return f"{chain['name']}の新メニュー・おすすめ一覧（新しい順）"
    return f"{chain['name']}の期間限定・新メニュー一覧（新しい順）"


def chain_seo_description(chain: dict, chain_items: list[dict]) -> str:
    """チェーン別ページの説明文。件数と、新しい順の先頭の商品名、掲載している項目だけを書く。"""
    n = len(chain_items)
    on = sum(1 for it in chain_items if not is_ended(it))
    if is_matsuya_family(chain["slug"]):
        what, recent = "の「おすすめ」に載ったメニューと新メニュー", "最近の記録は"
    elif chain["slug"] in ("sukiya", "marugame", *ARCLAND_FAMILY):
        what, recent = "の期間限定・New表示・新メニュー", "最近の新登場は"
    else:
        what, recent = "の期間限定・新登場メニュー", "最近の新登場は"
    with_kcal = sum(1 for it in chain_items if (it.get("nutrition") or {}).get("kcal") is not None)
    facts = "カロリー・価格・栄養成分を掲載。" if n and with_kcal * 2 >= n else "価格・販売期間を掲載。"
    names: list[str] = []
    for it in sorted(chain_items, key=lambda it: (effective_date(it) or "", str(it["product_id"])), reverse=True):
        if it["name"] not in names:
            names.append(it["name"])
        if len(names) == 3:
            break
    head = f"{chain['name']}{what}{n}件（販売中{on}件・終了{n - on}件）を新しい順に記録。"
    while names:
        text = head + recent + "".join(f"「{x}」" for x in names) + "など。" + facts
        if len(text) <= 125:
            return text
        names.pop()
    return head + facts


def seo_title(item: dict, variant: str = "") -> str:
    chain_name = CHAIN_NAME.get(item["chain"], item["chain"])
    nut = item.get("nutrition") or {}
    facts = "カロリー・価格・栄養成分" if nut.get("kcal") is not None else "価格・販売期間"
    if is_regular(item):
        facts = "カロリー・価格の推移" if nut.get("kcal") is not None else "価格の推移"
    sup = f"（{variant}）" if variant else ""
    return f"{chain_name}「{item['name']}」{sup}の{facts}"


def seo_description(item: dict, variant: str = "") -> str:
    chain_name = CHAIN_NAME.get(item["chain"], item["chain"])
    nut = item.get("nutrition") or {}
    sup = f"（{variant}）" if variant else ""
    bits = [f"{chain_name}の{'限定メニュー' if is_limited_listed(item) else 'メニュー'}「{item['name']}」{sup}"]
    if nut.get("kcal") is not None:
        bits.append(f"{nut['kcal']:g}kcal")
    if item.get("price") is not None:
        bits.append(f"{item['price']:g}円（税込）")
    bits.append(period_text(item))
    return ("、".join(bits) + "。" + (item.get("description") or ""))[:200]


def render_yoshinoya_notes(item: dict) -> str:
    """吉野家: 発売日の出どころ、早期販売終了のお知らせ、昨季の販売期間。"""
    notes = []
    kind = item.get("limited_kind")
    if kind == "期間限定":
        notes.append("公式サイトの「期間限定」表示のあるメニューです。販売期間は公式サイトに載っていないため、終了日は一覧から消えたのを確認した日です。")
    else:
        notes.append(f"公式サイトの「{kind or '新規掲載'}」として記録したメニューです。販売期間は公式サイトに載っていません。")
    if item.get("launch_date_source") == "news_headline" and item.get("news_title"):
        link = item.get("news_url") or ""
        title = esc(item["news_title"])
        title_html = (f'<a href="{esc(link)}" target="_blank" rel="noopener">{title}</a>' if link else title)
        notes.append(f"発売日は公式のお知らせ（{esc(item.get('news_date') or '')}）の見出しによります：{title_html}")
    e = item.get("early_end_notice")
    if e:
        link = e.get("url") or ""
        title = esc(e.get("title") or "")
        title_html = (f'<a href="{esc(link)}" target="_blank" rel="noopener">{title}</a>' if link else title)
        notes.append(f"販売終了に関する公式のお知らせが出ています（{esc(e.get('date') or '')}）：{title_html}")
    for s_ in item.get("seasons") or []:
        start = s_.get("launch_date") or "開始日不明"
        notes.append(f"昨季の販売：{esc(start)}〜{esc(s_.get('ended_observed_at') or '')}（終了日は{ENDED_NOTE}）")
    if item.get("takeout") is False:
        notes.append("テイクアウトはできません（公式ページの注記）。")
    return "".join(f"<p class='note'>{n}</p>" for n in notes)


def render_sukiya_notes(item: dict) -> str:
    """すき家: 記録の理由、発売日・お知らせ日の出どころ、昨季の販売期間。"""
    notes = []
    kind = item.get("limited_kind")
    if kind == "期間限定":
        notes.append("公式サイトの「期間限定」表示のあるメニューです。")
    elif kind == "新メニュー":
        notes.append("公式サイトの「New」表示、または新メニューのお知らせに載ったメニューです。")
    else:
        notes.append("記録を始めたあとにメニューへ加わったメニューです。")
    notes.append("販売期間は公式サイトに載っていないため、終了日は一覧から消えたのを確認した日です。")
    title = item.get("news_title")
    if title:
        link = item.get("news_url") or ""
        title_html = (f'<a href="{esc(link)}" target="_blank" rel="noopener">{esc(title)}</a>' if link else esc(title))
        if item.get("launch_date"):
            notes.append(f"発売日は公式のお知らせ（{esc(item.get('announced_at') or item.get('news_date') or '')}）の見出しによります：{title_html}")
        else:
            notes.append(f"お知らせ日：{esc(item.get('announced_at') or item.get('news_date') or '')}"
                         f"（発売日は公式のお知らせPDFに記載）：{title_html}")
    for s_ in item.get("seasons") or []:
        start = s_.get("launch_date") or "開始日不明"
        notes.append(f"昨季の販売：{esc(start)}〜{esc(s_.get('ended_observed_at') or '')}（終了日は{ENDED_NOTE}）")
    if len(item.get("price_variants") or []) > 1:
        notes.append("価格はサイズや組み合わせにより異なります。")
    return "".join(f"<p class='note'>{n}</p>" for n in notes)


def render_arcland_notes(item: dict) -> str:
    """かつや・からやま: 記録の理由、発売日・お知らせ日の出どころ、昨季の販売期間。"""
    notes = []
    kind = item.get("limited_kind")
    if kind == "期間限定":
        notes.append("公式サイトの「期間限定」表示のあるメニューです。")
    elif kind == "新メニュー":
        notes.append("公式サイトの「NEW」表示、または新メニューのお知らせに載ったメニューです。")
    else:
        notes.append("記録を始めたあとにメニューへ加わったメニューです。")
    notes.append("販売期間は公式サイトに載っていないため、終了日は一覧から消えたのを確認した日です。")
    title = item.get("news_title")
    if title:
        link = item.get("news_url") or ""
        when = esc(item.get("announced_at") or item.get("news_date") or "")
        label = "公式お知らせ（PR TIMES）" if "prtimes.jp" in link else "公式お知らせ"
        link_html = f'（<a href="{esc(link)}" target="_blank" rel="noopener">{label}</a>）' if link else ""
        if item.get("launch_date"):
            notes.append(f"発売日は公式のお知らせ（{when}）の見出しによります：{esc(title)}{link_html}")
        else:
            notes.append(f"お知らせ日：{when}（見出しに発売日の記載がありません）：{esc(title)}{link_html}")
    for s_ in item.get("seasons") or []:
        start = s_.get("launch_date") or "開始日不明"
        notes.append(f"昨季の販売：{esc(start)}〜{esc(s_.get('ended_observed_at') or '')}（終了日は{ENDED_NOTE}）")
    return "".join(f"<p class='note'>{n}</p>" for n in notes)


def render_burgerking_notes(item: dict) -> str:
    """バーガーキング: 記録の理由、告知日・終了日の出どころ(発売日は公式のデータに無い)、昨季の販売期間。"""
    notes = ["公式サイトの「期間限定」の印がついたメニューです（画像・説明文・栄養成分は掲載していません）。",
             "公式のデータに発売日は載っていないため、一覧で初めて見つけた日（記録開始日）から表示しています。発売日ではありません。"]
    title = item.get("news_title")
    if title:
        link = item.get("news_url") or ""
        title_html = f'<a href="{esc(link)}" target="_blank" rel="noopener">{esc(title)}</a>' if link else esc(title)
        notes.append(f"告知日：{esc(item.get('announced_at') or item.get('news_date') or '')}（公式ニュース）：{title_html}")
    if item.get("official_end_date"):
        link = item.get("official_end_url") or ""
        et = esc(item.get("official_end_title") or "公式イベント")
        et_html = f'<a href="{esc(link)}" target="_blank" rel="noopener">{et}</a>' if link else et
        notes.append(f"終了日は、公式イベントの終了日（{esc(item['official_end_date'])}）です：{et_html}")
        if item.get("ended_observed_at"):
            notes.append(f"「期間限定」の一覧から消えたのを確認した日：{esc(item['ended_observed_at'])}")
    else:
        notes.append("公式が終了日を載せていないため、終了日は「期間限定」の一覧から消えたのを確認した日です。")
    if len(item.get("price_variants") or []) > 1:
        notes.append("価格は単品とセットで異なります。")
    for s_ in item.get("seasons") or []:
        start = s_.get("launch_date") or (f"{s_['observed_start']}（記録開始日）" if s_.get("observed_start") else "開始日不明")
        end = s_.get("official_end_date") or s_.get("ended_observed_at") or ""
        notes.append(f"昨季の販売：{esc(start)}〜{esc(end)}")
    return "".join(f"<p class='note'>{n}</p>" for n in notes)


def render_marugame_notes(item: dict) -> str:
    """丸亀製麺: 記録の理由、発売日が公式に無いこと(記録開始日で表示)、販売チャネル、アレルゲンの注意、昨季の販売期間。"""
    notes = []
    kind = item.get("limited_kind")
    if kind == "期間限定":
        notes.append("公式サイトで「期間限定」の表示があるメニューです。")
    else:
        notes.append("記録を始めたあとにメニューへ加わったメニューです。")
    if "新定番" in (item.get("badges") or []):
        notes.append("公式サイトで「新定番」の表示があります（期間限定から定番メニューに加わった印）。")
    notes.append("公式サイトに発売日・販売期間が載っていないため、一覧で初めて見つけた日（記録開始日）から表示しています。"
                 "発売日ではありません。終了日も、一覧から消えたのを確認した日です。")
    if item.get("takeout_only"):
        notes.append("お持ち帰り専用のメニューです。")
    elif item.get("channels") == ["店内"]:
        notes.append("公式サイトの店内メニューにだけ載っています（お持ち帰りメニューには載っていません）。")
    if item.get("allergens") is not None:
        notes.append("アレルゲンの対象は、表示義務の8品目と推奨の19品目です。使っていない原材料でも、店舗の厨房や製造工場で"
                     "器具が共有されていることがあります。天ぷら・フライ類は同じフライヤーで調理されます。詳しくは公式のアレルギー情報でご確認ください。")
    for s_ in item.get("seasons") or []:
        start = s_.get("launch_date") or (f"{s_['observed_start']}（記録開始日）" if s_.get("observed_start") else "開始日不明")
        notes.append(f"昨季の販売：{esc(start)}〜{esc(s_.get('ended_observed_at') or '')}（終了日は{ENDED_NOTE}）")
    return "".join(f"<p class='note'>{n}</p>" for n in notes)


def render_regular_notes(item: dict) -> str:
    """定番メニュー: 記録の趣旨と、記録開始日が発売日ではないこと。"""
    notes = [f"定番メニューとして記録しています。記録を始めた日（{esc(season_observed_start(item))}）から、毎日の巡回で価格などを確認しています。"
             "発売日ではありません。",
             "販売終了は、公式サイトの一覧から消えたことを連続で確認した日です。"]
    if item.get("takeout") is False:
        notes.append("テイクアウトはできません（公式ページの注記）。")
    if len(item.get("price_variants") or []) > 1:
        notes.append("価格はサイズや組み合わせにより異なります（内訳は上の表）。")
    return "".join(f"<p class='note'>{n}</p>" for n in notes)


def render_item_page(item: dict, all_items: list[dict] | None = None) -> str:
    chain_name = CHAIN_NAME.get(item["chain"], item["chain"])
    official_url = item.get("official_url") or ""
    all_items = all_items or []

    # 同名商品(別の商品番号で並ぶ再販・店内/テイクアウトなど)の書き分けと相互リンク
    group = group_same_name(all_items).get(same_name_key(item), [])
    labels = variant_labels(group) if group else {}
    variant = labels.get(str(item["product_id"]), "")
    same_name_html = render_same_name(item, group, labels) if group else ""
    related_links_html = render_related(item, all_items) if all_items else ""

    quote_html = ""
    if item.get("description"):
        fetched = (item.get("first_seen_at") or "")[:10]
        # 松屋の説明文は商品ページではなくニュース記事(発売のお知らせ)から引用している
        cite_url = item.get("news_url") or official_url
        source_label = CHAIN_SOURCE.get(item['chain'], chain_name + '公式サイト')
        if item.get("news_url"):
            source_label += "のニュース"
        link_label = "公式ニュースを見る" if item.get("news_url") else "公式ページを見る"
        extra_link = (f' ・ <a href="{esc(official_url)}" target="_blank" rel="noopener">商品ページ</a>'
                      if item.get("news_url") and official_url else "")
        quote_html = f"""<blockquote cite="{esc(cite_url)}">{esc(item['description'])}</blockquote>
<p class="source">出典：{esc(source_label)}（{esc(fetched)}取得時点） ・
<a href="{esc(cite_url)}" target="_blank" rel="noopener">{link_label}</a>{extra_link}</p>
<p class="warn">※現在は公式ページが削除されている場合があります。</p>"""
    elif official_url:
        quote_html = (f'<p class="source"><a href="{esc(official_url)}" target="_blank" rel="noopener">'
                      "公式ページを見る</a>（販売終了後は削除されている場合があります）</p>")

    reviews_html = ""
    reviews = item_reviews(item)
    if reviews:
        reviews_html = f"""<h2>ネタフルのレビュー</h2>
<div class="review-grid">
{chr(10).join(render_review_card(a) for a in reviews)}
</div>"""

    related_html = ""
    related = select_related_articles(item, ARTICLES_BY_ID)
    if related:
        label, related_articles_list = related
        related_html = f"""<h2>関連するネタフルの記事（{esc(label)}）</h2>
<div class="review-grid">
{chr(10).join(render_review_card(a) for a in related_articles_list)}
</div>"""

    # 直接のレビューもキーワードの関連記事も無い商品には、そのチェーンの最新レビューを出す
    # (どの商品ページからもネタフルへ行けるようにする。コンビニポチと同じ方針)
    if not reviews and not related:
        recent = latest_reviews(item["chain"], 4)
        if recent:
            related_html = f"""<h2>{esc(chain_name)}のネタフル最新レビュー</h2>
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

    matsuya_note = ""
    if is_matsuya_family(item.get("chain")):
        matsuya_note = (f"<p class='note'>{esc(chain_name)}は公式サイトに「期間限定」の表示も販売期間も載せていません。"
                        "「おすすめ」に載ったメニューと、記録を始めてから新しく加わったメニューを記録しています。"
                        + ("発売日は公式ニュースの記載です。" if item.get("launch_date_source", "").startswith("news_article") else "")
                        + "</p>")
    yoshinoya_note = render_yoshinoya_notes(item) if item.get("chain") == "yoshinoya" else ""
    if item.get("chain") == "sukiya":
        yoshinoya_note = render_sukiya_notes(item)
    if item.get("chain") in ARCLAND_FAMILY:
        yoshinoya_note = render_arcland_notes(item)
    if item.get("chain") == "marugame":
        yoshinoya_note = render_marugame_notes(item)
    if item.get("chain") == "burgerking":
        yoshinoya_note = render_burgerking_notes(item)
    if is_regular(item):
        # 定番は限定・新メニューの注記(発売日の出どころ・昨季の販売など)を使わない
        matsuya_note = mop_note = ""
        yoshinoya_note = render_regular_notes(item)
    variant_span = f'<span class="variant">（{esc(variant)}）</span>' if variant else ""
    body = f"""<nav class="crumbs"><a href="/">トップ</a> &gt; <a href="/chains/{esc(item['chain'])}/">{esc(chain_name)}</a></nav>
<div class="detail">
<div class="badge" style="display:inline-block;font-size:11px;font-weight:700;color:#fff;
border-radius:4px;padding:2px 8px;background:var(--{esc(item['chain'])})">{esc(chain_name)}</div>
<h1>{esc(item['name'])}{variant_span}</h1>
{kind_html}
<p class="price">{esc(price)}{price_note}</p>
{render_price_variants(item)}
<p><strong>{'記録：' if is_regular(item) else '販売期間：'}</strong>{esc(period_text(item, detailed=True))}</p>
{mop_note}
{matsuya_note}
{yoshinoya_note}
{relaunch}
{('<p><strong>カテゴリ：</strong>' + esc(item['category']) + '</p>') if item.get('category') else ''}
{same_name_html}
{quote_html}
{nutrition_html}
{allergen_html}
{origin_html}
{history_html}
{mid_ad}
{reviews_html}
{related_html}
{related_links_html}
{bottom_ad}
</div>"""
    site_url = CONFIG.get("site_url", "")
    canonical = site_url + item_url(item)
    crumbs_ld = breadcrumb_ld([("トップ", site_url), (chain_name, f"{site_url}chains/{item['chain']}/"),
                               (item["name"], canonical)])
    return page_shell(seo_title(item, variant), seo_description(item, variant), body, canonical,
                      extra_head=crumbs_ld)


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


def generate_sitemap(items: list[dict], months: list[str], regular_items: list[dict] | None = None) -> str:
    """items は限定・新メニュー(トップ・アーカイブ・チェーン別の限定の一覧に載るもの)、regular_items は定番。"""
    site_url = CONFIG.get("site_url", "")
    regular_items = regular_items or []
    # lastmod は「そのページの内容が実際に変わった日」。商品ページは item_lastmod、
    # 一覧ページは載っている商品の lastmod の最大値(毎日の生成日にはしない)
    def newest(group: list[dict]) -> str:
        return max((item_lastmod(it) for it in group), default="")

    ended = ended_items(items)
    urls: list[tuple[str, str]] = [(site_url, newest(items)), (site_url + "archive/", newest(ended)),
                                   (site_url + "chains/", newest(items))]
    for c in CHAINS:
        urls.append((f"{site_url}chains/{c['slug']}/",
                     newest([it for it in items + regular_items if it["chain"] == c["slug"]])))
    for m in months:
        urls.append((f"{site_url}archive/{m}/", newest([it for it in ended if start_month(it) == m])))
    # 価格改定の記録。lastmod は最後に価格が変わった日
    all_items = items + regular_items
    events = price_change_events(all_items)
    urls.append((site_url + "prices/", events[0]["date"] if events else ""))
    for c in CHAINS:
        ev = price_change_events([it for it in all_items if it["chain"] == c["slug"]])
        urls.append((f"{site_url}prices/{c['slug']}/", ev[0]["date"] if ev else ""))
    for it in all_items:
        urls.append((site_url + item_url(it), item_lastmod(it)))
    body = "\n".join(
        f"<url><loc>{esc(u)}</loc>" + (f"<lastmod>{lm}</lastmod>" if lm else "") + "</url>" for u, lm in urls
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{body}
</urlset>
"""


def main() -> int:
    # 定番(is_regular)は、トップ・新着/アーカイブ・RSS・チェーン別の限定の一覧には混ぜない。
    # チェーン別ページの「定番メニュー」の節・商品ページ・価格改定の記録・検索・サイトマップにだけ出す
    all_items = load_all_products()
    items = [it for it in all_items if not is_regular(it)]
    regular_items = [it for it in all_items if is_regular(it)]
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
            label, f"{m[:4]}年{int(m[5:7])}月に販売が始まり、すでに終了した{CHAIN_LIST_TEXT}の限定メニュー一覧",
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
        chain_url = CONFIG.get("site_url", "") + f"chains/{c['slug']}/"
        page = render_grid_page(
            f"{c['name']}の限定メニュー", chain_seo_description(c, chain_items), chain_items,
            chain_url,
            review_chain=c["slug"], status_filter=True, default_status="all",
            empty_text="まだ記録がありません。",
            seo_title=chain_seo_title(c),
            extra_head=breadcrumb_ld([("トップ", CONFIG.get("site_url", "")), (c["name"], chain_url)]),
            extra_body=render_regular_section(c, [it for it in regular_items if it["chain"] == c["slug"]]),
        )
        (cdir / "index.html").write_text(page, encoding="utf-8")

    (DOCS / "items").mkdir(parents=True, exist_ok=True)
    for it in all_items:
        # 関連リンク・同名商品は、限定は限定どうし、定番は定番どうしで作る(限定の商品ページの見え方を変えない)
        pool = regular_items if is_regular(it) else items
        (DOCS / item_url(it)).write_text(render_item_page(it, pool), encoding="utf-8")

    # 価格改定の記録(全体とチェーンごと。JSなしで絞り込めるよう別ページにしている)
    (DOCS / "prices").mkdir(parents=True, exist_ok=True)
    (DOCS / "prices" / "index.html").write_text(render_prices(all_items), encoding="utf-8")
    for c in CHAINS:
        (DOCS / "prices" / c["slug"]).mkdir(parents=True, exist_ok=True)
        (DOCS / "prices" / c["slug"] / "index.html").write_text(render_prices(all_items, c), encoding="utf-8")

    # 商品削除などで不要になった旧itemページ(オーファン)を掃除する
    valid_item_files = {(DOCS / item_url(it)).name for it in all_items}
    stale_items = 0
    for f in (DOCS / "items").glob("*.html"):
        if f.name not in valid_item_files:
            f.unlink()
            stale_items += 1

    (DOCS / "search-index.json").write_text(
        json.dumps(build_search_index(all_items), ensure_ascii=False), encoding="utf-8"
    )
    (DOCS / "rss.xml").write_text(generate_rss(items), encoding="utf-8")
    (DOCS / "sitemap.xml").write_text(generate_sitemap(items, months, regular_items), encoding="utf-8")
    (DOCS / "robots.txt").write_text(generate_robots(), encoding="utf-8")
    (DOCS / "CNAME").write_text(
        CONFIG.get("site_url", "").replace("https://", "").strip("/") + "\n", encoding="utf-8"
    )
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")

    print(
        f"generated: index.html, {len(months)} archive months, {len(CHAINS)} chain pages, "
        f"{len(all_items)} item pages ({len(regular_items)} regular), prices pages, rss.xml, sitemap.xml, search-index.json"
        + (f" / removed {stale_items} stale item pages" if stale_items else "")
        + (f" / removed {stale_months} stale archive dirs" if stale_months else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
