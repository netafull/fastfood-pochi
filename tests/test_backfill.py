"""手で補った商品(record に "backfill" がある)の扱いのテスト。

公式サイト停止中(2026-10-29〜11-25)に売られた松のや「国産麦小町」を、公式ニュース記事から補った記録。
 (a) 商品ページに補完の注記が出る(通常の商品には出ない)
 (b) 巡回(終了判定・再確認)が、巡回で見たことのない終了済み・first_seen_at=null の記録で落ちたり書き換えたりしない
"""

from __future__ import annotations

import json
import unittest

import helpers  # noqa: F401
import crawl
import generate_site as g
from helpers import TempData

BACKFILL = {
    "chain": "matsunoya", "product_id": "backfill-mugikomachi-2026-01",
    "name": "“国産麦小町”ロースかつ定食", "category": "定食", "price": 990,
    "price_note": "税込。", "price_variants": None,
    "launch_date": "2026-11-11", "launch_date_source": "news_article_body", "planned_end_text": None,
    "news_url": "https://www.matsuyafoods.co.jp/matsunoya/whatsnew/menu/192728.html",
    "news_title": "国産豚総選挙 第1弾！", "news_date": "2026-11-18",
    "description": None, "description_full_len": 0, "nutrition": None, "allergens": None, "origin": None,
    "limited_kind": "新メニュー", "in_recommended": False, "official_url": None, "current_slug": None,
    "first_seen_at": None, "last_seen_at": None, "status": "ended", "ended_observed_at": "2026-11-25",
    "missed_runs": 0, "price_history": [{"date": "2026-11-11", "price": 990}],
    "source_list": ["news_article"], "image": None,
    "backfill": {"source": "news_article", "created": "2026-11-25", "reason": "r", "end_note": "e"},
}


class TestBackfillNote(unittest.TestCase):
    def test_note_shown_for_backfill(self):
        h = g.render_item_page(dict(BACKFILL))
        self.assertIn("巡回では確認できていません", h)
        self.assertIn("2026年10月29日〜11月25日", h)
        self.assertIn("192728.html", h)
        self.assertIn("2026-11-11〜2026-11-25", h)
        self.assertIn("公式サイトの一覧から消えたことを確認した日", h)

    def test_note_not_shown_for_normal(self):
        normal = dict(BACKFILL)
        normal.pop("backfill")
        h = g.render_item_page(normal)
        self.assertNotIn("巡回では確認できていません", h)


class TestCrawlerLeavesBackfillAlone(unittest.TestCase):
    def test_apply_missing_and_loaders_ignore_it(self):
        with TempData():
            crawl.save_product("matsunoya", dict(BACKFILL))
            stats = crawl.new_stats()
            crawl.apply_missing("matsunoya", set(), "2026-11-26", stats)  # 一覧に居なくても
            p = crawl.load_product("matsunoya", BACKFILL["product_id"])
            self.assertEqual(p, BACKFILL)  # 変わらない(終了済みは数え直さない)
            self.assertEqual(stats["ended"], 0)
            # 価格の取り直しの対象にならない
            self.assertEqual(crawl.due_for_recheck(crawl.load_chain_products("matsunoya"),
                                                   {BACKFILL["product_id"]: {}}, True, "2026-11-26"), [])


if __name__ == "__main__":
    unittest.main()
