import unittest

import helpers  # noqa: F401
import fetch_netaful_reviews as nr
import generate_site as g


class TestTags(unittest.TestCase):
    def test_tag_ids_from_config(self):
        self.assertEqual(nr.TAGS, {"mcdonalds": 735, "mos": 882})

    def test_mos_uses_title_filter(self):
        self.assertTrue(nr.title_passes_filter("mos", "【モスバーガー】月見フォカッチャ"))
        self.assertFalse(nr.title_passes_filter("mos", "ハンバーガーがおいしいチェーン店といえば"))
        self.assertTrue(nr.title_passes_filter("mcdonalds", "なんでも"))


class TestMatching(unittest.TestCase):
    def test_names_match_strictness(self):
        n = nr.normalize_name
        self.assertTrue(nr.names_match(n("月見フォカッチャ"), n("月見フォカッチャ ～黄金(おうごん)チーズソース～")))
        self.assertFalse(nr.names_match(n("月見"), n("月見バーガー")))  # 6文字未満の部分一致は不可
        self.assertFalse(nr.names_match(n("チキンタツタ"), n("月見バーガー")))
        self.assertTrue(nr.names_match(n("月見バーガー"), n("月見バーガー")))

    def test_registered_mark_ignored(self):
        n = nr.normalize_name
        self.assertEqual(n("マックフルーリー® オレオ®"), n("マックフルーリー オレオ"))

    def test_extract_name_from_title(self):
        a = nr.extract_article({"id": 1, "date": "2026-01-01T00:00:00", "link": "x",
                                "title": {"rendered": "【マクドナルド】「月見バーガー」を食べた"}})
        self.assertEqual(a["product_name"], "月見バーガー")

    def test_keyword_longest_match_wins(self):
        kw = g.match_keyword_for_name("月見フォカッチャ ～黄金チーズソース～")
        self.assertEqual(kw["label"], "フォカッチャ")  # 月見(2)よりフォカッチャ(5)が具体的
        self.assertEqual(g.match_keyword_for_name("月見バーガー")["label"], "月見")
        self.assertEqual(g.match_keyword_for_name("食べくらべポテナゲ大")["label"], "ナゲット")  # ポテトにはしない
        self.assertIsNone(g.match_keyword_for_name("zzz"))

    def test_related_articles_prefer_same_chain(self):
        arts = {
            1: {"id": 1, "chain": "mos", "title": "モスの月見", "date": "2026-02-01"},
            2: {"id": 2, "chain": "mcdonalds", "title": "マックの月見", "date": "2025-02-01"},
        }
        it = {"chain": "mcdonalds", "product_id": "9", "name": "月見バーガー"}
        label, got = g.select_related_articles(it, arts)
        self.assertEqual(label, "月見")
        self.assertEqual([a["id"] for a in got], [2, 1])


if __name__ == "__main__":
    unittest.main()
