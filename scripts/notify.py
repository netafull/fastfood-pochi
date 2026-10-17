#!/usr/bin/env python3
"""ntfy (https://ntfy.sh/) で新メニューの要約・失敗を知らせる。

1日1回の実行なので、通知の設計は単純にしてある:
  ・失敗したら毎回鳴らす
  ・成功して新しく記録したメニュー/販売終了を観測したメニューがあれば要約を送る

「今回の新規」は日付(UTC/JSTのずれで誤通知する)ではなく、crawl.py が実行ごとに
書く data/run_summary.json(gitには入れない)から数える。

トピック名は GitHub Secrets (NTFY_TOPIC) で管理し、リポジトリには書かない
(公開リポジトリなので、トピック名が漏れると誰でも通知を送りつけられる)。

使い方:
  python3 scripts/notify.py --summary   新規・終了の要約を送る(どちらも0件なら送らない)
  python3 scripts/notify.py --failure   更新が失敗したことを知らせる

必要な環境変数:
  NTFY_TOPIC : ntfyのトピック名 (未設定なら何もしない)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

SITE_NAME = "ファストフードポチ"
ROOT = Path(__file__).resolve().parent.parent
RUN_SUMMARY = ROOT / "data" / "run_summary.json"
CHAIN_LABEL = {"mcdonalds": "マクドナルド", "mos": "モスバーガー", "matsuya": "松屋", "matsunoya": "松のや",
               "yoshinoya": "吉野家", "sukiya": "すき家", "katsuya": "かつや", "karayama": "からやま", "marugame": "丸亀製麺"}


def send(topic: str, title: str, message: str, click: str = "") -> None:
    payload: dict[str, str] = {"topic": topic, "title": title, "message": message}
    if click:
        payload["click"] = click
    req = urllib.request.Request(
        "https://ntfy.sh/",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10):
        pass


def _run_click() -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else ""


def read_run_summary(path: Path | None = None) -> dict[str, dict]:
    """直前の crawl.py 実行の {"new": {chain: n}, "ended": {chain: n}, "suspected": {chain: {...}}} を返す。
    suspected は「新規候補が極端に多く、公式の構造変更の疑いで記録を保留した」チェーン。"""
    try:
        data = json.loads((path or RUN_SUMMARY).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"new": {}, "ended": {}, "suspected": {}}
    return {
        "new": {k: int(v) for k, v in (data.get("new") or {}).items()},
        "ended": {k: int(v) for k, v in (data.get("ended") or {}).items()},
        "suspected": {k: dict(v) for k, v in (data.get("suspected") or {}).items() if isinstance(v, dict)},
    }


ESCALATE_DAYS = 3


def build_warnings(summary: dict) -> list[tuple[str, str]]:
    """歯止めが働いたチェーンごとの警告通知 [(タイトル, 本文)]。要約とは別に送る。"""
    out = []
    for chain, info in (summary.get("suspected") or {}).items():
        name = CHAIN_LABEL.get(chain, chain)
        n = int(info.get("count", 0))
        days = int(info.get("consecutive_days", 1))
        body = (f"{name}で新規候補が一度に{n}件(閾値{info.get('threshold', '?')}件)。"
                "公式のサイト構造変更の疑い。記録を保留(終了判定もしていません)。")
        title = f"{SITE_NAME}: 【警告】{name}の記録を保留"
        if days >= ESCALATE_DAYS:
            title = f"{SITE_NAME}: 【要判断】{name}の保留が{days}日連続"
            body += f" {days}日連続で保留しています。自動では再開しないので、運営者の判断が必要です。"
        out.append((title, body))
    return out


def build_message(summary: dict) -> tuple[str, str] | None:
    """(タイトル, 本文)。新規も終了も0件で、保留も無ければ None。"""
    n_new = sum(summary["new"].values())
    n_end = sum(summary["ended"].values())
    held = summary.get("suspected") or {}
    if n_new == 0 and n_end == 0 and not held:
        return None
    parts = []
    if n_new:
        parts.append("新規: " + " / ".join(
            f"{CHAIN_LABEL.get(k, k)} {v}件" for k, v in summary["new"].items() if v))
    if n_end:
        parts.append("販売終了を観測: " + " / ".join(
            f"{CHAIN_LABEL.get(k, k)} {v}件" for k, v in summary["ended"].items() if v))
    head = []
    if n_new:
        head.append(f"新メニュー {n_new}件")
    if n_end:
        head.append(f"終了 {n_end}件")
    if held:
        names = "・".join(CHAIN_LABEL.get(k, k) for k in held)
        parts.append(f"保留あり: {names}は新規候補が多すぎるため、今回は新規0件として記録していません(別途警告を送っています)")
        if n_new == 0:
            head.insert(0, "新規0件")
        head.append("保留あり")
    return f"{SITE_NAME}: " + "・".join(head), "今回の巡回の結果。" + "　".join(parts)


def main() -> int:
    args = sys.argv[1:]
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        print("NTFY_TOPIC未設定のため通知はスキップします")
        return 0

    if "--summary" in args:
        summary = read_run_summary()
        for title, body in build_warnings(summary):
            try:
                send(topic, title, body, _run_click())
                print(f"警告を通知しました: {title}")
            except (urllib.error.URLError, OSError) as e:
                print(f"[warn] ntfy警告の通知に失敗しました: {e}", file=sys.stderr)
        msg = build_message(summary)
        if msg is None:
            print("新規・終了とも0件のため通知はスキップします")
            return 0
        try:
            send(topic, msg[0], msg[1], _run_click())
            print(f"要約を通知しました: {msg[0]}")
        except (urllib.error.URLError, OSError) as e:
            print(f"[warn] ntfy通知に失敗しました: {e}", file=sys.stderr)
        return 0

    if "--failure" in args:
        try:
            send(topic, f"{SITE_NAME}: 更新に失敗しました",
                 "本日の巡回が失敗しました。サイトは前回の内容のままです。実行ログを確認してください。",
                 _run_click())
            print("失敗を通知しました")
        except (urllib.error.URLError, OSError) as e:
            print(f"[warn] ntfy通知に失敗しました: {e}", file=sys.stderr)
        return 0

    print("usage: notify.py --summary|--failure", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
