import unittest

import helpers  # noqa: F401
import fetch


class TestFetch(unittest.TestCase):
    def test_challenge_detection(self):
        self.assertTrue(fetch._looks_like_challenge(b"<html>Request unsuccessful. Incapsula incident ID"))
        self.assertTrue(fetch._looks_like_challenge(b"<title>Just a moment...</title>"))

    def test_fixtures_are_not_challenges(self):
        for rel in ("mcdonalds/menu.html", "mcdonalds/p_6900.html", "mos/data_menu_menu_category_26.json"):
            self.assertFalse(fetch._looks_like_challenge(helpers.fx(rel).encode("utf-8")), rel)


if __name__ == "__main__":
    unittest.main()
