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
CHAIN_LABEL = {"mcdonalds": "マクドナルド", "mos": "モスバーガー"}


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


def read_run_summary(path: Path | None = None) -> dict[str, dict[str, int]]:
    """直前の crawl.py 実行の {"new": {chain: n}, "ended": {chain: n}} を返す。"""
    try:
        data = json.loads((path or RUN_SUMMARY).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"new": {}, "ended": {}}
    return {
        "new": {k: int(v) for k, v in (data.get("new") or {}).items()},
        "ended": {k: int(v) for k, v in (data.get("ended") or {}).items()},
    }


def build_message(summary: dict[str, dict[str, int]]) -> tuple[str, str] | None:
    """(タイトル, 本文)。新規も終了も0件なら None。"""
    n_new = sum(summary["new"].values())
    n_end = sum(summary["ended"].values())
    if n_new == 0 and n_end == 0:
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
    return f"{SITE_NAME}: " + "・".join(head), "今回の巡回の結果。" + "　".join(parts)


def main() -> int:
    args = sys.argv[1:]
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        print("NTFY_TOPIC未設定のため通知はスキップします")
        return 0

    if "--summary" in args:
        msg = build_message(read_run_summary())
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
