#!/usr/bin/env python3
"""ネタフル(WordPress)の公開REST APIから、マクドナルド・モスバーガーの記事を取得し、
ファストフードポチの商品(data/products/)とひも付けてキャッシュする。

認証不要のREST API (https://netaful.jp/wp-json/wp/v2/posts?tags=...) を使う。
タグID(config.json の netaful_tags): マクドナルド=735(208件)、モスバーガー=882(3件)。
モスバーガーのタグは記事が3件しか付いておらず、実際にはタグ無しの記事が多いので、
モスだけは記事の全文検索(search=モスバーガー)の結果も使い、タイトルに
config.json の netaful_title_filters の語を含む記事だけを採用する。

初回(または --backfill 指定時)は全ページを取得する。通常実行(毎日、crawl.pyの後)は
最新1ページ(per_page=50)だけ取得してキャッシュに追加し、全商品に対して再照合する
(レビューは発売後に書かれることが多く、過去の商品にも後から記事がつくため)。

ネタフル側の失敗はサイト更新全体を止めないよう、errorsに記録するだけで
処理を続ける。

保存先: data/netaful_reviews.json
  {
    "fetched_at": "...",
    "articles": {"mcdonalds": [{"id":.., "date":.., "link":.., "title":..,
                             "product_name":.., "thumb": {"url":.., "width":.., "height":..} | null}],
                 "mos": [...]},
    "matches": {"mcdonalds-6900": [209819, ...], ...},
    "errors": ["..."]
  }

アイキャッチ画像(thumb)は `_embed=wp:featuredmedia` でWP REST APIから取得する。
記事にはネタフル(運営者自身)の画像なので、直リンクで表示してよい。
"""

from __future__ import annotations

import datetime
import html as html_lib
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
PRODUCTS = ROOT / "data" / "products"
CACHE_PATH = ROOT / "data" / "netaful_reviews.json"
JST = datetime.timezone(datetime.timedelta(hours=9))

UA = CONFIG.get("user_agent", "FastfoodPochi/1.0 (+https://fastfood.netaful.jp/)")
API_BASE = "https://netaful.jp/wp-json/wp/v2/posts"
REQUEST_INTERVAL = 1.0  # ネタフルへのリクエスト間隔(秒)、1秒以上

TAGS = {k: int(v) for k, v in CONFIG["netaful_tags"].items()}
# タグの付け漏れが多いチェーンだけ、全文検索の結果も使う(タイトルに語を含むものだけ採用)
TITLE_FILTERS: dict[str, list[str]] = CONFIG.get("netaful_title_filters", {})
SEARCH_TERMS = {"mos": "モスバーガー"}

TITLE_NAME_RE = re.compile(r"「(.*?)」")

# カード表示に使うアイキャッチ画像サイズの優先順(幅300〜700px程度を狙う)
THUMB_SIZE_ORDER = ("medium_large", "medium", "large", "thumbnail")
THUMB_WIDTH_MIN = 300
THUMB_WIDTH_MAX = 700

# 正規化で除去する記号・空白類(全角/半角の中黒・括弧・句読点等)
_STRIP_RE = re.compile(
    r"[\s　・:：/／,、。.!！?？\-—―~〜()（）\[\]【】「」『』\"'’”×%!®™©]+"
)


def normalize_name(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = _STRIP_RE.sub("", s)
    return s.lower()


def http_get(url: str) -> tuple[bytes, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as res:
        return res.read(), dict(res.getheaders())


def fetch_tag_page(tag_id, page: int, per_page: int, search: str | None = None) -> tuple[list[dict], int]:
    """tag_id の記事、または search 指定時は全文検索の結果を返す。"""
    if search:
        query = f"search={urllib.parse.quote(search)}"
    else:
        query = f"tags={tag_id}"
    url = (
        f"{API_BASE}?{query}&per_page={per_page}&page={page}"
        "&_embed=wp:featuredmedia&_fields=id,date,link,title,_links,_embedded"
    )
    body, headers = http_get(url)
    total_pages = int(headers.get("X-WP-TotalPages") or headers.get("x-wp-totalpages") or 1)
    try:
        posts = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as e:
        # 2026-10-03から、GitHub Actions上でだけ「JSONではない応答」が返るようになった
        # (ローカルでは正常)。原因を特定できるよう、応答の先頭と種類をエラーに残す
        ctype = headers.get("Content-Type") or headers.get("content-type") or "?"
        head = body[:120].decode("utf-8", "replace").replace("\n", " ")
        raise json.JSONDecodeError(
            f"{e.msg} [{len(body)}バイト, {ctype}, 先頭: {head!r}]", e.doc, e.pos
        ) from e
    return posts, total_pages


RETRY_WAITS = (30, 90)  # 失敗時に待つ秒数。最大3回試す


def fetch_tag_page_with_retry(tag_id, page: int, per_page: int,
                              search: str | None = None) -> tuple[list[dict], int]:
    """毎朝7時台(日本時間)の実行でだけ、ネタフル側が空・非JSONの応答を返す日が
    あった(2026-10-03, 10-04。同日の昼に再実行すると成功)。サーバーが一時的に
    重いと見て、待って再試行する。"""
    for wait in RETRY_WAITS:
        try:
            return fetch_tag_page(tag_id, page, per_page, search)
        except (json.JSONDecodeError, urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"[retry] tag={tag_id} page={page}: {e} → {wait}秒後に再試行", file=sys.stderr)
            time.sleep(wait)
    return fetch_tag_page(tag_id, page, per_page, search)


def extract_thumb(post: dict) -> dict | None:
    """`_embed=wp:featuredmedia` で埋め込まれたアイキャッチ画像から、カード表示に
    適したサイズ(幅300〜700px程度)のURLを選ぶ。無ければNone。"""
    media_list = (post.get("_embedded") or {}).get("wp:featuredmedia") or []
    if not media_list:
        return None
    media = media_list[0]
    if not isinstance(media, dict) or media.get("code"):
        # 埋め込み失敗時はエラーオブジェクト({"code": "rest_forbidden", ...})が入る
        return None
    sizes = ((media.get("media_details") or {}).get("sizes")) or {}

    for key in THUMB_SIZE_ORDER:
        s = sizes.get(key)
        w = s.get("width") if s else None
        if s and s.get("source_url") and w and THUMB_WIDTH_MIN <= w <= THUMB_WIDTH_MAX:
            return {"url": s["source_url"], "width": w, "height": s.get("height")}

    # 好みの範囲に収まるサイズが無ければ、幅300px以上の中で一番小さいものを使う
    candidates = [s for s in sizes.values() if s.get("source_url") and (s.get("width") or 0) >= THUMB_WIDTH_MIN]
    if candidates:
        best = min(candidates, key=lambda s: s["width"])
        return {"url": best["source_url"], "width": best.get("width"), "height": best.get("height")}

    src = media.get("source_url")
    if src:
        details = media.get("media_details") or {}
        return {"url": src, "width": details.get("width"), "height": details.get("height")}
    return None


def extract_article(post: dict) -> dict:
    raw_title = html_lib.unescape(post.get("title", {}).get("rendered", ""))
    m = TITLE_NAME_RE.search(raw_title)
    product_name = m.group(1).strip() if m else None
    return {
        "id": post["id"],
        "date": post.get("date"),
        "link": post.get("link"),
        "title": raw_title,
        "product_name": product_name,
        "thumb": extract_thumb(post),
    }


def load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"fetched_at": None, "articles": {c: [] for c in TAGS}, "matches": {}, "errors": []}
    except json.JSONDecodeError:
        return {"fetched_at": None, "articles": {c: [] for c in TAGS}, "matches": {}, "errors": []}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )


def title_passes_filter(chain: str, title: str) -> bool:
    words = TITLE_FILTERS.get(chain)
    return True if not words else any(w in title for w in words)


def fetch_articles_for_chain(chain: str, backfill: bool, errors: list[str],
                             search: str | None = None) -> list[dict]:
    tag_id = TAGS[chain]
    per_page = 100 if backfill else 50
    articles: list[dict] = []
    page = 1
    total_pages = 1
    first = True
    while page <= total_pages:
        if not first:
            time.sleep(REQUEST_INTERVAL)
        first = False
        try:
            posts, total_pages = fetch_tag_page_with_retry(tag_id, page, per_page, search)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            errors.append(f"{chain} tag={tag_id} page={page}: {e}")
            break
        except json.JSONDecodeError as e:
            errors.append(f"{chain} tag={tag_id} page={page}: JSON解析失敗 {e}")
            break
        found = [extract_article(p) for p in posts]
        if search:
            found = [a for a in found if title_passes_filter(chain, a["title"])]
        articles.extend(found)
        if not backfill:
            break  # 通常実行は最新1ページのみ
        page += 1
    return articles


def load_all_products() -> list[dict]:
    items = []
    for chain_dir in PRODUCTS.iterdir() if PRODUCTS.is_dir() else []:
        if not chain_dir.is_dir():
            continue
        for f in chain_dir.glob("*.json"):
            try:
                items.append(json.loads(f.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
    return items


def product_effective_date(item: dict) -> str | None:
    return item.get("launch_date") or item.get("list_week_start") or (item.get("first_seen_at") or "")[:10] or None


def names_match(article_name_norm: str, product_name_norm: str) -> bool:
    if not article_name_norm or not product_name_norm:
        return False
    if article_name_norm == product_name_norm:
        return True
    shorter, longer = sorted([article_name_norm, product_name_norm], key=len)
    if len(shorter) >= 6 and shorter in longer:
        return True
    return False


def rematch(cache: dict) -> tuple[dict, list[tuple[str, dict, dict]], list[tuple[dict, dict, str]]]:
    """全商品×全記事を再照合する。

    戻り値: (matches辞書, ひも付いた例のリスト[(key, product, article)],
             惜しくも外れた例のリスト[(product, article)] ※デバッグ用)
    """
    products = load_all_products()
    by_chain: dict[str, list[dict]] = {}
    for p in products:
        by_chain.setdefault(p["chain"], []).append(p)

    matches: dict[str, list[int]] = {}
    matched_examples: list[tuple[str, dict, dict]] = []
    near_misses: list[tuple[dict, dict, str]] = []

    for chain, articles in cache["articles"].items():
        chain_products = by_chain.get(chain, [])
        prod_norms = [
            (p, normalize_name(p.get("name")), product_effective_date(p)) for p in chain_products
        ]
        for art in articles:
            art_name = art.get("product_name")
            if not art_name:
                continue
            art_norm = normalize_name(art_name)
            try:
                art_date = datetime.date.fromisoformat((art.get("date") or "")[:10])
            except ValueError:
                art_date = None

            best_near = None
            for p, p_norm, p_eff_date in prod_norms:
                if names_match(art_norm, p_norm):
                    # 日付フィルタ: 記事が商品発売日より14日以上前なら対象外
                    date_excluded = False
                    if art_date is not None and p_eff_date:
                        try:
                            eff = datetime.date.fromisoformat(p_eff_date)
                        except ValueError:
                            eff = None
                        if eff is not None and (eff - art_date).days > 14:
                            date_excluded = True
                    if date_excluded:
                        if len(near_misses) < 20:
                            near_misses.append((p, art, "日付フィルタで除外(14日超)"))
                        continue
                    key = f"{chain}-{p['product_id']}"
                    matches.setdefault(key, [])
                    if art["id"] not in matches[key]:
                        matches[key].append(art["id"])
                        if len(matched_examples) < 20:
                            matched_examples.append((key, p, art))
                elif art_norm and p_norm and not best_near:
                    # 惜しくも外れた例の簡易検出: 先頭4文字以上が共通するが不一致
                    common = len(_common_prefix(art_norm, p_norm))
                    if common >= 4:
                        best_near = (p, art, "名前不一致(先頭のみ類似)")
            if best_near and len(near_misses) < 20:
                near_misses.append(best_near)

    return matches, matched_examples, near_misses


def _common_prefix(a: str, b: str) -> str:
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return a[:n]


def main() -> int:
    backfill = "--backfill" in sys.argv[1:] or not CACHE_PATH.exists()
    cache = load_cache()
    errors: list[str] = []  # 今回の実行で起きた失敗だけ。前回分は引き継がない

    for chain in TAGS:
        new_articles = fetch_articles_for_chain(chain, backfill, errors)
        if chain in SEARCH_TERMS:
            time.sleep(REQUEST_INTERVAL)
            new_articles += fetch_articles_for_chain(chain, backfill, errors, SEARCH_TERMS[chain])
        if not new_articles:
            continue
        existing = {a["id"]: a for a in cache["articles"].get(chain, [])}
        for a in new_articles:
            existing[a["id"]] = a
        cache["articles"][chain] = sorted(existing.values(), key=lambda a: a.get("date") or "", reverse=True)
        time.sleep(REQUEST_INTERVAL)

    matches, matched_examples, near_misses = rematch(cache)
    cache["matches"] = matches
    cache["errors"] = errors[-50:]  # 直近分だけ保持
    cache["fetched_at"] = datetime.datetime.now(JST).isoformat(timespec="seconds")
    save_cache(cache)

    counts = {c: len(cache["articles"].get(c, [])) for c in TAGS}
    matched_products = len(matches)
    matched_by_chain: dict[str, int] = {}
    for key in matches:
        chain = key.split("-", 1)[0]
        matched_by_chain[chain] = matched_by_chain.get(chain, 0) + 1

    print(f"=== ネタフルレビュー取得結果 (backfill={backfill}) ===")
    print(f"記事キャッシュ件数: {counts}")
    print(f"ひも付いた商品数(社別): {matched_by_chain} / 合計{matched_products}件")
    if errors:
        print(f"エラー: {len(errors)}件（直近: {errors[-3:]}）", file=sys.stderr)

    print("\n--- ひも付いた例 ---")
    for key, p, art in matched_examples[:5]:
        print(f"  {key}: 商品「{p['name']}」 <-> 記事「{art['product_name']}」({art['link']})")

    print("\n--- 惜しくも外れた例 ---")
    for p, art, reason in near_misses[:5]:
        print(f"  商品「{p['name']}」({p['chain']}) <-> 記事「{art['product_name']}」({art['link']}) [{reason}]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
