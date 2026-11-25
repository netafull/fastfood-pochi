# やよい軒の追加（15チェーン目）作業レポート（2026-11-25）

KFC型（リリース照合型）の同系として追加した。**メニュー一覧が商品の正本**で、**発売日は
自社サイト（www.yayoiken.com）のニュース記事が正本**。KFCと違って法人サイトは無く、
1ドメインで完結する。商品詳細ページは第1段階では取らないので、巡回は一覧＋ニュースだけ。

判断ポイント5点（テイクアウトも記録する／`launch_date_source` に新値 `news_article`／
期間限定は「おすすめメニュー（期間限定）」セクション掲載だけ／アルコールも定番として記録する／
説明文・栄養・アレルゲンは第1段階では扱わない）は、すべて推奨どおりでオーナー承認済み。

## 変えたもの

- `config.json`: chains に `{"slug": "yayoiken", "name": "やよい軒", "base_url": "https://www.yayoiken.com", "color": "#1B2F63"}` を追加。`request_interval_seconds.yayoiken = 3`、`max_requests_per_run.yayoiken = 20`。site_tagline / site_description / about のチェーン列挙を15チェーンに更新。`netaful_tags` はやよい軒専用タグの存在が未確認のため追加していない。
- `scripts/parsers.py`: やよい軒セクションを追加。`parse_yayoiken_menu(page)`（タブのパネル→h2セクション→カードの順に読み、product_id・name・price・category（所属タブ）・section・is_new・in_limited_section を返す）、`parse_yayoiken_news_list(page)`、`parse_yayoiken_news_article(page)`（■商品概要 の商品列挙＋・発売日。肩書き「～…～」は落とし、[テイクアウト] の接頭辞は付ける。範囲は ■商品概要 から ・発売日 まで、かつ次の ■ 見出しまで）、`match_yayoiken_article(name, price, article)`（名前＋価格の一致が条件）、`yayoiken_excluded(entry)`。
- `scripts/crawl.py`: `crawl_yayoiken()` と `build_yayoiken_product()` を追加し CRAWLERS に登録。専用 state は `data/yayoiken_state.json`。歯止め（前回の半分未満は不更新）・RegularBudget・終了判定・burst_filter は共通部品をそのまま使う。冒頭の docstring のチェーン列挙も更新。
- `scripts/generate_site.py`: CHAIN_SOURCE に `"yayoiken": "やよい軒公式サイト"`、CSS の `--yayoiken`（明 #1B2F63 / 暗 #93A5D8）と chain-group / badge ルール、CATEGORY_ICONS（定食・丼・麺・お子様→donburi、朝食・おうち定食→bento、サイド→sozai）、`render_yayoiken_notes()`（発売日の出どころ注記。`launch_date_source="news_article"` に対応）、フッターの列挙にやよい軒を追加、限定一覧の対象チェーン（`is_limited_listed`）に yayoiken を追加。
- `scripts/notify.py`: CHAIN_LABEL に `"yayoiken": "やよい軒"`。
- `data/yayoiken_state.json`（新規。空の初期形）、`data/pending.json` に `"yayoiken": {}`。
- `tests/synthetic_yayoiken.py`・`tests/test_yayoiken.py`（新規21件）。チェーン数を固定している既存テスト7ファイル（test_burgerking / test_marugame / test_ramen / test_sukiya / test_yoshinoya / test_zetteria / test_kfc）の期待値を15チェーンに追随。`tests/fixtures_manifest.txt` に yayoiken のフィクスチャ名を追記。
- `README.md`: 冒頭の列挙と、やよい軒の個別セクションを追加。
- `.github/workflows/update.yml`: コメントのチェーン数と上限合計（14チェーン547→15チェーン567）のみ更新。

## 記録方針

- **チェーン色は #1B2F63（深い紺）**。やよい軒の看板・ロゴの基調色で、吉野家 #1F5FAD より明確に暗く、既存14色と重ならない。暗テーマは #93A5D8。
- product_id は詳細URL末尾の数値ID。name は一覧のタイトル表記のまま（肩書きは含めない。[テイクアウト] 等の接頭辞は公式表記のまま）、price は一覧の税込価格、official_url は詳細URL。カテゴリは所属タブで、h2 のセクション名はカテゴリにしない。
- **発売日の出どころは新値 `launch_date_source="news_article"`** を新設した（KFCの `press_release` は法人サイトのニュースリリース専用の含意のため流用しない）。発表日・見出し・URLは news_date / news_title / news_url に入る。発表日と発売日は約1週間ずれるので、発表日での代用はしない。
- 種別: 「おすすめメニュー（期間限定）」セクションに載った商品は 期間限定（badges にも）。NEWバッジ、またはニュース記事と照合して発売日が付いた商品は新メニュー。baseline に無い新規は新規掲載、それ以外は定番。定番が期間限定セクションに載ったら期間限定へ格上げする。フェア名（NABE FAIR等）は保存しない。
- 照合は ■商品概要 の範囲内に限り、商品名＋価格の一致を条件にする。記事の商品名は肩書き「～…～」を落としてから照合し、括弧・テイクアウト表記の半角/全角違いは NFKC 正規化で吸収する。照合の窓は商品の初観測日の前120日〜後14日（KFCと同じ）。
- テイクアウト（おうち定食タブ35件＋[テイクアウト] 表記）とアルコール5件も記録する。説明文・栄養成分・アレルゲンは第1段階では対象外（すべて null）。
- robots.txt は404（ファイルなし）＝制限なし。サイトポリシーに自動取得の禁止条文は無い（2026-11-25確認）。記録は事実情報のみで、画像は保存・表示しない。

## 実サイトでの確認（やよい軒のみ、1回）

一時コピー側で `python3 scripts/crawl.py yayoiken` を1回実行した（試行データはコミットしていない）。

- リクエスト計10件（上限20以内。robots＋メニュー一覧＝2、ニュース一覧2ページ＋記事詳細6件＝8）、一覧取得=全部成功、ブロックなし、失敗0件、所要28秒。
- 対象は142件（実測どおり）。初回に35件を作成（限定・新メニュー15件＋定番20件＝RegularBudget の上限）、定番107件は持ち越し（翌日以降に順次作成される）。
- **発売日が付けられたのは8件**: チゲ定食系5件（2026-11-17。うちニラチヂミ（単品）は期間限定セクション掲載で種別は期間限定）、すき焼き定食系3件（2026-11-05）。いずれも `launch_date_source="news_article"`。
- **かきフライ（単品）は発売日なし**（期間限定セクション掲載で種別は期間限定）。ニュース一覧2ページの範囲内に発売記事が無く、照合できなかった（下記「未確認・懸念」）。
- 大分とり天定食系（4件）は初回は発売日なし。発売記事（2026-11-11発表）はニュース一覧の8番目にあり、記事詳細の取得上限（6件/回）の外だったため初回は未取得。state に残る一覧の続きから翌回に取得され、発売日が付く見込み（上限の仕様どおりの段階的な照合で、KFCと同じ性質）。
- チゲ・すき焼きの照合は、元の発売記事ではなく「recommend」のまとめ記事（2026-11-24 / 2026-11-12）に当たった。まとめ記事にも同じ ■商品概要 と発売日が載るため発売日は正しいが、news_url / news_date の帰属はまとめ記事になる。照合は新しい記事を優先する KFC と同じ作法で、季節の再発売時に古い発売日を拾わないためのものでもある。
- 試行データで `generate_site.py` も実行し、15チェーン分のペ
...[truncated 1095 chars]