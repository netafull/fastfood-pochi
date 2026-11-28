import unittest
import unittest.mock

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


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.headers = {"content-type": "text/html" if body.startswith(b"<") else "application/pdf"}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestFetchBinaryHtml(unittest.TestCase):
    """fetch_binary の HTML の扱い (フレッシュネスのお知らせの誤判定の回帰防止)。

    PDFのURLのはずが通常のHTMLのお知らせだった場合、allow_html を付けた取得だけ
    そのまま返し、チャレンジの兆候があるHTMLはどちらでも BlockedError にする。
    """

    NOTICE = "<!DOCTYPE html><html><head><title>お知らせ</title></head><body>story</body></html>".encode("utf-8")
    CHALLENGE = b"<html><head><title>Just a moment...</title></head><body>checking</body></html>"
    PDF = b"%PDF-1.7 fake"

    def _run(self, body: bytes, **kw):
        with unittest.mock.patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            return fetch.fetch_binary("https://example.test/x", "TestUA/1.0", **kw)

    def test_pdf_passes(self):
        body, _ = self._run(self.PDF)
        self.assertEqual(body, self.PDF)

    def test_html_notice_blocked_by_default(self):
        with self.assertRaises(fetch.BlockedError):
            self._run(self.NOTICE)

    def test_html_notice_allowed_with_allow_html(self):
        body, _ = self._run(self.NOTICE, allow_html=True)
        self.assertEqual(body, self.NOTICE)

    def test_challenge_blocked_even_with_allow_html(self):
        with self.assertRaises(fetch.BlockedError):
            self._run(self.CHALLENGE, allow_html=True)
