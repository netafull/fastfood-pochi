#!/usr/bin/env python3
"""HTTP取得の共通部分。正直なUser-Agentで、丁寧な間隔・タイムアウト・
リトライ(1回まで)を入れる。どこかがブロック(403やチャレンジページ)してきた
場合は、UA偽装などの回避策には切り替えず、ブロックされた事実を BlockedError として
呼び出し側に伝える。回避するかどうかは運営者が判断する。
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request


class BlockedError(Exception):
    """403等、明確にブロックされたと判断できる応答。"""


class NotFoundError(Exception):
    """404。リトライしても無駄で、商品ページの削除などを意味する。"""


TIMEOUT = 20


def _looks_like_challenge(body: bytes) -> bool:
    low = body[:4000].lower()
    return (
        b"incapsula" in low
        or b"imperva" in low
        or b"_incap_" in low
        or b"request unsuccessful" in low
        or b"just a moment" in low  # Cloudflareのチャレンジ
        or b"cf-chl" in low
        or b"access denied" in low
    )


def fetch(url: str, user_agent: str, retries: int = 1) -> str:
    """1回リクエストし、失敗したら retries 回までやり直す(合計 retries+1 回)。

    403 やチャレンジページはリトライしても無駄なので即座に BlockedError で
    知らせる(UA偽装などの回避策はここでは実装しない。判断は運営者)。
    """
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                body = res.read()
                if _looks_like_challenge(body):
                    raise BlockedError(f"challenge page detected: {url}")
                return body.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 429):
                raise BlockedError(f"HTTP {e.code}: {url}") from e
            if e.code == 404:
                raise NotFoundError(f"HTTP 404: {url}") from e
            last_err = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    raise last_err or RuntimeError(f"fetch failed: {url}")


def fetch_binary(url: str, user_agent: str, method: str = "GET", retries: int = 1) -> tuple[bytes, dict]:
    """PDFなどバイナリの取得(method="HEAD" なら本文なしで応答ヘッダーだけ)。-> (本文, 小文字キーのヘッダー辞書)

    403・429・チャレンジページ(PDFのはずがHTML)はリトライせず BlockedError。UA偽装などの回避はしない。
    """
    req = urllib.request.Request(url, headers={"User-Agent": user_agent}, method=method)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT * 3) as res:
                headers = {k.lower(): v for k, v in res.headers.items()}
                body = b"" if method == "HEAD" else res.read()
                if method != "HEAD" and not body.startswith(b"%PDF"):
                    if _looks_like_challenge(body) or body[:200].lstrip().lower().startswith((b"<!doctype", b"<html")):
                        raise BlockedError(f"PDFではない応答(チャレンジページの疑い): {url}")
                return body, headers
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 429):
                raise BlockedError(f"HTTP {e.code}: {url}") from e
            if e.code == 404:
                raise NotFoundError(f"HTTP 404: {url}") from e
            last_err = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    raise last_err or RuntimeError(f"fetch failed: {url}")
