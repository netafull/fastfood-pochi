"""テスト共通: フィクスチャを返す偽の取得器と、一時データディレクトリの差し替え。"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import crawl  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def fx(path: str) -> str:
    return (FIX / path).read_text(encoding="utf-8")


MCD_PAGES = {
    "/robots.txt": "mcdonalds/robots.txt",
    "/menu/": "mcdonalds/menu.html",
    "/menu/burger/": "mcdonalds/cat_burger.html",
    "/menu/dessert/": "mcdonalds/cat_dessert.html",
    "/menu/side/": "mcdonalds/cat_side.html",
    "/menu/drink/": "mcdonalds/cat_drink.html",
    "/menu/mccafe/": "mcdonalds/cat_mccafe.html",
    "/menu/morning/": "mcdonalds/cat_morning.html",
    "/menu/dinner/": "mcdonalds/cat_dinner.html",
    "/products/6900/": "mcdonalds/p_6900.html",
    "/products/801705/": "mcdonalds/p_801705.html",
    "/products/2520/": "mcdonalds/p_2520.html",
}


MATSUYA_MENU_CATS = ["limited", "gyumeshi", "curry", "don", "teishoku", "morning", "lunch",
                     "currylunch", "okosama", "sidemenu", "topping", "drink"]
MATSUYA_PAGES = {
    "/robots.txt": "matsuya/robots.txt",
    "/matsuya/menu/": "matsuya/menu.html",
    "/matsuya/menu/curry/kashmir_hp_261006.html": "matsuya/p_curry-kashmir_hp_261006.html",
    "/matsuya/menu/don/don_gyuyaki_hp_260811.html": "matsuya/p_don-don_gyuyaki_hp_260811.html",
    "/matsuya/whatsnew/data/whatsnew.json": "matsuya/whatsnew_full.json",
    "/matsuya/whatsnew/menu/178231.html": "matsuya/n_178231.html",
    "/matsuya/whatsnew/menu/177082.html": "matsuya/n_177082.html",
    "/matsuya/whatsnew/menu/167268.html": "matsuya/n_167268.html",
}
for _c in MATSUYA_MENU_CATS:
    MATSUYA_PAGES[f"/matsuya/menu/{_c}/index.html"] = f"matsuya/cat_{_c}.html"


MATSUNOYA_MENU_CATS = ["osusume", "teishoku", "moriawase", "donburi", "okosama", "morning", "souzai", "side_menu"]
MATSUNOYA_PAGES = {
    "/robots.txt": "matsunoya/robots.txt",
    "/matsunoya/menu/": "matsunoya/menu.html",
    "/matsunoya/menu/teishoku/tei_kaki5_hp_260916.html": "matsunoya/p_teishoku-tei_kaki5_hp_260916.html",
    "/matsunoya/menu/donburi/don_kaki_hp_260916.html": "matsunoya/p_donburi-don_kaki_hp_260916.html",
    "/matsunoya/whatsnew/data/whatsnew.json": "matsunoya/whatsnew_full.json",
    "/matsunoya/whatsnew/menu/175938.html": "matsunoya/n_175938.html",
}
BLOCK_RE = re.compile(r'<li class="item">.*?<!-- //\.item -->', re.S)


def matsunoya_category_page(cat: str) -> str:
    """取得済みのosusume・teishokuは実物。ほかのカテゴリは、osusumeの実物のブロックのうち
    そのカテゴリのものを抜き出して作る(無いカテゴリは1件を複製して作った合成ページ)。"""
    if cat in ("osusume", "teishoku"):
        return fx(f"matsunoya/cat_{cat}.html")
    page = fx("matsunoya/cat_osusume.html")
    blocks = BLOCK_RE.findall(page)
    mine = [b for b in blocks if f"/matsunoya/menu/{cat}/" in b]
    if not mine:
        mine = [blocks[0].replace("/souzai/", f"/{cat}/").replace("souzai_set_nebiki_rosu1_kaki2_hp_261007",
                                                                   f"{cat}_synthetic_hp_261001")]
    head = page[:page.index(blocks[0])]
    return head + "\n".join(mine) + "\n</ul></body></html>"


class FakeGetter:
    """URLからフィクスチャを返す。呼ばれたURLを記録する。

    fallback_detail=True のとき、フィクスチャが無い /products/ は 6900 のページで代用する
    (サイト生成の目視確認用。テストの検証には使わない)。
    """

    def __init__(self, fallback_detail: bool = False, fail: set[str] | None = None,
                 overrides: dict[str, str] | None = None):
        self.calls: list[str] = []
        self.fallback_detail = fallback_detail
        self.fail = fail or set()
        self.overrides = overrides or {}

    def __call__(self, chain: str, url: str) -> str:
        self.calls.append(url)
        if chain == "mcdonalds":
            path = url.replace(crawl.MCD_BASE, "")
            if path in self.fail:
                raise OSError("simulated failure")
            if path in self.overrides:
                return self.overrides[path]
            if path in MCD_PAGES:
                return fx(MCD_PAGES[path])
            if path.startswith("/products/") and self.fallback_detail:
                return fx("mcdonalds/p_6900.html")
            raise OSError(f"no fixture: {path}")
        if chain == "matsuya":
            path = url.replace(crawl.MATSUYA_BASE, "")
            if path in self.fail:
                raise OSError("simulated failure")
            if path in self.overrides:
                return self.overrides[path]
            if path in MATSUYA_PAGES:
                return fx(MATSUYA_PAGES[path])
            if path.startswith("/matsuya/menu/") and self.fallback_detail:
                return fx("matsuya/p_curry-kashmir_hp_261006.html")
            raise OSError(f"no fixture: {path}")
        if chain == "matsunoya":
            path = url.replace(crawl.MATSUYA_BASE, "")
            if path in self.fail:
                raise OSError("simulated failure")
            if path in self.overrides:
                return self.overrides[path]
            if path in MATSUNOYA_PAGES:
                return fx(MATSUNOYA_PAGES[path])
            m = re.fullmatch(r"/matsunoya/menu/([a-z_]+)/index\.html", path)
            if m and m.group(1) in MATSUNOYA_MENU_CATS:
                return matsunoya_category_page(m.group(1))
            if path.startswith("/matsunoya/menu/") and self.fallback_detail:
                return fx("matsunoya/p_teishoku-tei_kaki5_hp_260916.html")
            raise OSError(f"no fixture: {path}")
        path = url.replace(crawl.MOS_BASE, "")
        if path in self.fail:
            raise OSError("simulated failure")
        if path in self.overrides:
            return self.overrides[path]
        if path == "/data/menu/mst_navi.json":
            return fx("mos/data_menu_mst_navi.json")
        if path == "/data/menu/mst_allergy.json":
            return fx("mos/mst_allergy.json")
        if path.startswith("/data/menu/menu_category/"):
            cid = path.rsplit("/", 1)[1].replace(".json", "")
            if cid == "26":
                return fx("mos/data_menu_menu_category_26.json")
            return "[]"  # 限定以外のカテゴリはフィクスチャ未取得。空の一覧として扱う
        raise OSError(f"no fixture: {path}")


class TempData:
    """crawl.py の保存先を一時ディレクトリへ差し替える(with で使う)。"""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.saved = (crawl.DATA, crawl.PRODUCTS, crawl.PENDING_PATH, crawl.BASELINE_PATH, crawl.RUN_SUMMARY)
        crawl.DATA = base
        crawl.PRODUCTS = base / "products"
        crawl.PENDING_PATH = base / "pending.json"
        crawl.BASELINE_PATH = base / "baseline_ids.json"
        crawl.RUN_SUMMARY = base / "run_summary.json"
        return base

    def __exit__(self, *exc):
        (crawl.DATA, crawl.PRODUCTS, crawl.PENDING_PATH, crawl.BASELINE_PATH, crawl.RUN_SUMMARY) = self.saved
        self.tmp.cleanup()
