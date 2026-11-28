# 日高屋: 日付形式IDの発売日・IDの付け替え対応 — 2026-11-28

ブランチ `mol/hidakaya-dated-id`（最新の origin/main から）。push・マージはしていない。

## 背景（確認済みの事実）
- 2026-11-27 に新しく出た4件の発売日が null になった。801 チゲ味噌ラーメンはお知らせに日付が無く（`news_without_date`）、もつ煮3件は商品名がお知らせの本文にしか無く照合しない設計。
- 日高屋は期間限定品に日付形式のIDを付ける。20261009（発売日2026-10-09）・20261127（もつ煮、2026-11-27）はIDの日付が発売日と一致している。
- 07:40〜13:55の巡回の間に、公式が 20261127-1/-2 を 20261127_1/_2 に付け替え、単品→おつまみへ移した。このため `20261127-1/-2`（終了・missed_runs 2、誤り）と `20261127_1/_2`（販売中）が二重に記録された。

## 変えたこと
- `scripts/parsers.py`: `hidakaya_is_dated_id` を `20\d{6}` に任意の `[-_]\d+` を許す形に拡張。`hidakaya_dated_id_date`（先頭8桁→ISO日付。暦に無い日付は None）を追加。
- `scripts/crawl.py`（日高屋のみ）:
  - `hidakaya_apply_dated_id`: 日付形式IDの商品に `dated_id: true`。発売日が null で定番でなければ、IDの日付が初回確認日の前120日〜後30日に収まるとき `launch_date_source="dated_id"` で設定。既存の発売日（manual・お知らせ）は上書きしない。新規作成時と、末尾の「発売日の確定」ループ（既存記録）の両方で呼ぶ。お知らせの照合のあとに行う。お知らせが空でも動く。
  - `hidakaya_resolve_reassigned_ids`（純粋関数。丸亀の `marugame_resolve_moves` を踏襲）と `hidakaya_apply_reassign`: 一覧が全部読めた回だけ、新ID（未記録・baseline外）を、一覧から消えた記録済み商品（販売中／終了から14日以内）に、名前・価格が同じで1対1のときだけ付け替える。曖昧なら何もしない。キーは変えず、`official_url`・`official_id`・カテゴリを更新。終了から14日以内なら販売中に戻す（`relaunches` には積まない）。記録済みの `official_id` は翌日以降も名前・価格に依らず追う。`burst_filter` の前に行うので付け替え分は新規候補に数えない。
  - コメントブロックの「付け替わっても…古い方が終了になるだけで済む」を、上記の挙動の説明に置き換え。
- `scripts/generate_site.py` `render_ramen_notes`: `dated_id` の注記「発売日は公式サイトの商品ページのID（日付の形式）によります。」と、`manual` の `launch_date_note`（吉野家と同じ「発売日：…」）を追加。発売日の出どころが dated_id / manual のとき、「発売日は公式のお知らせ（…）によります」とは書かない。
- データ（手作業）:
  - `20261127-1.json`・`20261127-2.json` を販売中に復元（`status` on_sale・`ended_observed_at` null・`missed_runs` 0・`last_miss_date` 削除・`last_seen_at` 2026-11-28）、`official_url` を _1/_2 のURLに、`official_id`・`dated_id: true` を追加。
  - `20261127_1.json`・`20261127_2.json` を削除。
  - `801.json`: `launch_date` 2026-11-27、`launch_date_source` manual、`launch_date_note` を指示どおり記入。
- `README.md` 日高屋の節を更新。
- テスト（`tests/test_ramen.py`）: 枝番付きID・ID→日付（正常/不正）、dated_id の発売日（お知らせ無し／枝番付き新規／範囲外／暦に無い日付／manual・お知らせを上書きしない／既存記録への反映／お知らせに日付が無い場合）、付け替え（クロール全体で1対1・終了から復元・一覧の一部取得失敗・価格違いは付け替えない、純粋関数で曖昧・基準線・終了が古い・official_id追跡）、注記の描画。既存の2テストは新仕様に合わせて更新（`test_renumbered_dated_id_does_not_spread` を付け替えの検証に、`test_dated_id_without_news_is_recorded_as_new_listing` の発売日期待値を2026-10-09に）。

## 検証
- 全スイート804件: 失敗は既存の `test_regular.py` の13件（失敗10・エラー3。変更前と同数・同一）のみ。
- 実データでの確認（ネットワークなし）: 現在の `data/products/hidakaya/` と「今日の一覧に _1/_2 と 20261127 がある」想定で純粋関数を実行 → `20261127_1→20261127-1`、`20261127_2→20261127-2` に対応。`hidakaya_apply_dated_id` で3件（-1・-2・20261127）が2026-11-27・dated_id になり、20261009 は news_headline のまま。
- `generate_site.py` はクラッシュなく完走。docs/ は再生成後に元へ戻した（コミットに docs の変更なし）。

## やらなかったこと・未確認
- 実サイトへの巡回は未実施（指示どおりネットワークなし）。次回の本番巡回で、付け替え・もつ煮3件の発売日・dated_id が想定どおり入るかは未確認。
- docs/ の再生成はしていない（次回の定期実行で更新される。`docs/items/hidakaya-20261127_*.html` は stale として消える想定）。
- お知らせの日付を発売日の代用にはしていない（オーナー決定）。
- 20261127-2（もつ煮定食）のカテゴリは、指示では「おつまみ」だが、削除した 20261127_2 の記録は「定食」だったため「定食」にした（次回巡回で一覧の値に同期される）。
- 推測: 枝番（-1/_1）は同日のサイズ・定食違いを分けるためのもの。公式が今後もIDを付け替えるかは不明。
- `data/pending.json`・`baseline_ids.json`・`ramen_state.json` に 20261127 系のIDの参照は無かった（grep済み。docs/ と tests/synthetic_hanamaru.py の無関係な一致のみ）。
