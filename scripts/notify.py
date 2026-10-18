#!/usr/bin/env python3
"""ntfy (https://ntfy.sh/) で新メニューの要約・失敗を知らせる。

1日1回の実行なので、通知の設計は単純にしてある:
  ・失敗したら毎回鳴らす
  ・成功して新しく記録したメニュー/販売終了を観測したメニューがあれば要約を送る
  ・あるチェーンがブロック・一覧取得失敗・例外で取れていない日は、新規0件でも知らせる
    (毎日取れていない状態を、新メニューのない静かな日と区別するため)

「今回の新規」は日付(UTC/JSTのずれで誤通知する)ではなく、crawl.py が実行ごとに
書く data/run_summary.json(gitには入れない)から数える。

トピック名は GitHub Secrets (NTFY_TOPIC) で管理し、リポジトリには書かない
(公開リポジトリなので、トピック名が漏れると誰でも通知を送りつけられる)。

使い方:
  python3 scripts/notify.py --summary   新規・終了・巡回の問題の要約を送る(すべて0件なら送らない)
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


def send(topic: str, title: str, message: str, click: str = "", priority: int = 0) -> None:
    payload: dict = {"topic": topic, "title": title, "message": message}
    if click:
        payload["click"] = click
    if priority:
        payload["priority"] = priority
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
    suspected は「新規候補が極端に多く、公式の構造変更の疑いで記録を保留した」チェーン。
    blocked / list_failed / errors は、その実行で取れていなかったチェーンのリスト、streaks は
    その連続日数、detail_failed は詳細ページ単発の失敗件数(通知対象ではない)。古い形式(これらが
    無い run_summary.json)でも読める。"""
    try:
        data = json.loads((path or RUN_SUMMARY).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"new": {}, "ended": {}, "suspected": {}, "blocked": [], "list_failed": [], "errors": [],
                "streaks": {}, "detail_failed": {}}
    if not isinstance(data, dict):
        data = {}
    return {
        "blocked": list(data.get("blocked") or []),
        "list_failed": list(data.get("list_failed") or []),
        "errors": list(data.get("errors") or []),
        "streaks": {k: int(v) for k, v in (data.get("streaks") or {}).items()},
        "detail_failed": {k: int(v) for k, v in (data.get("detail_failed") or {}).items()},
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


PROBLEM_LABELS = (("blocked", "ブロックされた"), ("list_failed", "一覧の取得に失敗"),
                  ("errors", "例外で停止"))


def problem_lines(summary: dict) -> tuple[list[str], list[str]]:
    """問題のあったチェーンの(表示名リスト, 本文の行リスト)。同じチェーンの複数種別は1行にまとめる。"""
    kinds: dict[str, list[str]] = {}
    for key, label in PROBLEM_LABELS:
        for chain in summary.get(key) or []:
            kinds.setdefault(chain, []).append(label)
    shown, lines = [], []
    for chain, labels in kinds.items():
        name = CHAIN_LABEL.get(chain, chain)
        days = (summary.get("streaks") or {}).get(chain, 0)
        shown.append(name)
        lines.append(f"{name}: {'・'.join(labels)}" + (f"({days}日連続)" if days >= 2 else ""))
    return shown, lines


def build_message(summary: dict) -> tuple[str, str] | None:
    """(タイトル, 本文)。新規も終了も0件で、保留も問題も無ければ None。"""
    n_new = sum(summary["new"].values())
    n_end = sum(summary["ended"].values())
    held = summary.get("suspected") or {}
    shown, lines = problem_lines(summary)
    if n_new == 0 and n_end == 0 and not held and not shown:
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
    if shown:
        parts.append("巡回の問題: " + " / ".join(lines)
                     + "。取れていない日は、公式の掲載が入れ替わると記録が失われることがあります。実行ログを確認してください。")
        df = sum((summary.get("detail_failed") or {}).values())
        if df:
            parts.append(f"(詳細ページの取得失敗 {df}件は翌日に自動で再試行します)")
        if head:
            head.append(f"問題あり: {'・'.join(shown)}")
        else:
            head.append(f"巡回に問題({'・'.join(shown)})")
    return f"{SITE_NAME}: " + "・".join(head), "今回の巡回の結果。" + "　".join(parts)


def has_problem(summary: dict) -> bool:
    return bool(problem_lines(summary)[0])


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
            print("新規・終了・問題とも0件のため通知はスキップします")
            return 0
        try:
            send(topic, msg[0], msg[1], _run_click(), 4 if has_problem(summary) else 0)
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
