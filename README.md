# ファストフードポチ

マクドナルドとモスバーガーの**期間限定・新登場メニュー**を1日1回巡回して記録し、
販売が終わって公式サイトから消えても記録が残るアーカイブにするサイト
（公開予定 https://fastfood.netaful.jp/ ）。栄養成分つき。KFCは公式サイトが
curlを拒否するため対象外。

姉妹サイト [電書ポチ](https://book.netaful.jp/)・[家電ポチ](https://kaden.netaful.jp/)・
[林檎ポチ](https://apple.netaful.jp/)・[漫画ポチ](https://manga.netaful.jp/)・
[コンビニポチ](https://conbini.netaful.jp/)と同じく、pip依存なし
(Python標準ライブラリのみ、HTML解析は正規表現)。構造は `conbini-pochi` のほぼ流用。
サイトの色は ティール `#00897B`、広告(AdSense)・GA4は当面なし(config の値が空の間は何も出力しない)。

## 使い方

```bash
python3 scripts/crawl.py                    # 2社を巡回して data/products/ に蓄積
python3 scripts/fetch_netaful_reviews.py    # ネタフルのレビュー記事を取得・商品とひも付け
python3 scripts/generate_site.py            # docs/ 一式を生成
python3 -m unittest discover -s tests       # tests/fixtures/ の公式データのスナップショットでテスト
```

定期実行は `.github/workflows/update.yml`（workflow_dispatchのみ。scheduleもpushトリガーも無い）を
Cloud Schedulerから起動する前提。**開発中は本番サイトを叩かず、tests/fixtures/ で開発する。**
`NTFY_TOPIC` は GitHub Secrets（コードに書かない）。

## 何を記録するか

- **限定メニュー**（期間限定・季節限定・地域限定・数量限定）と、**初回の巡回より後に初めて
  現れた新メニュー**。定番の既存メニューは詳細を取らない。初回に見えた全商品IDは
  `data/baseline_ids.json` に保存し、以後これに無いIDが出たら新メニューとして記録する。
- セット商品（名前に「セット」「コンビ」「募金付き」）は記録しない（バーガー本体と二重になり、
  栄養も載らないことが多いため）。
- 商品1件1ファイル `data/products/{mcdonalds|mos}/{id}.json`。既存ファイルは丸ごと上書きしない
  （初回取得が正。更新するのは last_seen_at / status / price_history 等の追跡項目だけ）。
- 画像は保存も表示もしない（`image: null`）。説明文は最初の1文（最大100文字）だけ保存し、
  引用ブロックで出典つきで表示する。価格・栄養成分・アレルゲン・販売期間・原産地は事実情報として全部出す。

### 販売状況の追跡（サイトの核）

- 巡回で一覧に居たら `last_seen_at`（日付）を更新し `status=on_sale`。
- **そのチェーンの一覧取得が全部成功した回だけ**、一覧から消えた商品を数える。
  一覧のどれか1ページでも取得失敗・0件（構造変更の疑い）・robots.txtでDisallowなら、その回は数えない。
- **別の日に連続2回**見つからなかったら `status=ended`、`ended_observed_at` にその日付。
  同じ日に何度実行しても1回と数える（`last_miss_date`）。
- 終了後に再登場したら `on_sale` に戻し、`relaunches` に記録。
- 一覧の価格が前回と違えば `price_history` に追記。
- 終了日は公式発表の最終販売日ではなく「公式の一覧から消えたことを確認した日」。サイト上にもそう書いている。

## 取得元

### マクドナルド (www.mcdonalds.co.jp)

- 巡回ページ（`config.json` の `mcdonalds_pages`）: `/menu/`（おすすめ）、`/menu/burger/`、`dessert/`、`side/`、
  `drink/`、`mccafe/`、`morning/`、`dinner/`。**`/menu/` のおすすめだけでは足りない**
  （月見バーガー単品や明治アーモンドチョコのフルーリー等は `/menu/` に出ない）。
  `/menu/set/` と `/menu/happyset/` はセット商品ばかりなので巡回しない。robots.txt は毎回取得し、
  Disallow のパス（`/mop/` `/order/` `/coupon-*` 等）には行かない。
- 限定判定: バッジ画像 `limit_badge.svg` に `data-time-limited-offer-started-at` が付いている、または
  バッジが `hidden` で隠されていない。**普通の商品もバッジのimg自体は持っていて `hidden` なので、
  `data-time-limited-offer='true'` の有無だけでは判定できない。**
- **商品ID** は一覧の `data-product-code`（`/products/{code}/` の code と一致する。deeplink の
  `code=NNNN` は別の値で画像パス用なので使わない）。
- 販売開始日: 一覧の `started-at` は告知用の日付で、実際の発売日より数日〜2週間早いことがある
  （月見バーガーは一覧8/18、実際の発売9/2）。詳細ページの `data-mop-availability-started-at`
  （モバイルオーダー受付開始）が実際の発売日と一致するのでこちらを `launch_date` に使い、一覧の値は `list_started_at` に残す。
- 終了予定: マクドナルドは公式の終了予定日を載せていない。詳細ページの
  `data-mop-availability-ended-at`（モバイルオーダー受付期間の終了）を「終了予定」として、
  その旨を明記して表示している（`mop_ended_at`）。公式の販売終了日と一致する保証は無い。
- 価格は税込（一覧ページに明記）。「460~」の「~」は最低価格、店舗・デリバリーで異なる場合がある。
- 栄養・アレルゲン・原産地は詳細ページのHTML。栄養が載らない商品（食べくらべポテナゲ等）は `nutrition: null`。
- 規約 `/policy/` は複製・転載・公衆への再提供を禁止している。→ 説明文は冒頭1文だけ・画像なし・
  出典つきの引用にとどめる運用（コグレ判断）。

### モスバーガー (www.mos.jp)

- `mos.jp` は `www.mos.jp` へ301でリダイレクトされる。最初から `www.mos.jp` を使うこと。
- 一覧はJSON `/data/menu/menu_category/{c_id}.json`。限定メニューは `c_id=26`。**新メニュー検出と
  終了判定のため全カテゴリ（`/data/menu/mst_navi.json` の12個）を毎回取る**（1日13リクエスト）。
  限定アイコン（`/data/menu/mst_icon.json`: 1=期間限定, 2=季節限定, 4=地域限定, 5=店舗限定, 14=数量限定）が
  付いた商品は他カテゴリのものも限定として記録する（朝モスの店舗限定トースト等）。
- **アレルゲンの対応表がある**: `/data/menu/mst_allergy.json`（`/menu/allergy/` のJS `allergy.js` が読んでいる）。
  `level=5`（●）が「含む」、`1`（－）が含まない、`2,3,4,6`（△▽◇等）は凡例が必要な条件つきの表示で、
  サイトでは記号つきで「条件つきの表示」として出す。
- 発売日・終了予定は `explain` の「【2026年9月9日（水）〜2026年11月上旬頃までの販売予定】」から正規表現で取る
  （無ければ null。時間限定・曜日限定のメニューには無い）。
- 詳細ページのHTMLは取らない（JSONに全部入っている）。公式URLは
  `https://www.mos.jp/menu/detail/?menu_id={id}&c_id={c_id}`。
- 規約ページは見つけられていない（`/terms/` `/sitepolicy/` は404、トップのフッターはJS描画）。
  robots.txtは無い（404）。サイトのフッターに公式サイトへのリンクを置く程度にしている。

## クロールのマナー

- User-Agent は正直なもの `FastfoodPochi/1.0 (+https://fastfood.netaful.jp/)`。ブロック（403/429・チャレンジ）されたら
  **ブラウザ偽装に切り替えず**、そのチェーンの処理を止めて `stats.blocked` に記録する（他チェーンは続行）。
- リクエスト間隔は3秒以上、タイムアウト20秒、リトライは1回。1回の実行のリクエスト上限は
  `max_requests_per_run`（マクドナルド60・モス30）。通常の1日は、マクドナルド9（robots.txt＋一覧8）＋新メニューの詳細、
  モス13（カテゴリ一覧＋12）＋（新メニューがあれば）アレルゲン表1。
- 取得済みの商品の詳細ページは再取得しない。取得失敗はチェーン単位で `data/pending.json` に記録し、次回再試行する。
- `data/run_summary.json`（今回の新規・終了の件数。gitには入れない）を notify.py が読む。
  日付で数えるとUTC/JSTのずれで誤通知になる（コンビニポチで実際に起きた）。

## サイトの構成（docs/）

- トップ: 販売中の限定メニューをチェーンごとにグルーピング（開始日の新しい順）。並び替え（開始日順/カロリー順）、
  販売状況の絞り込み（販売中/終了/すべて）、チェーンの絞り込み、メニュー名検索（`search-index.json`）、
  ネタフルの最新レビュー。最近30日以内に終わったメニューも載せておき、既定の「販売中」では隠れる。
  トップに WebSite 構造化データ（name=ファストフードポチ。無いと検索結果のサイト名が親ドメインの「ネタフル」になる）。
- `/archive/` と `/archive/YYYY-MM/`: 終了した限定メニューを**開始月ごと**に。各カードに「いつからいつまで」。
- `/chains/{mcdonalds|mos}/`、`/items/{chain}-{id}.html`（販売期間・価格と価格履歴・栄養成分・アレルゲン・原産地・出典つき引用）。
- ページの title/description にチェーン名・カロリー・価格（例「マクドナルド「○○」のカロリー・価格・栄養成分｜ファストフードポチ」。
  栄養が無い商品は「価格・販売期間」）。favicon.png は96px（48の倍数が必要）。
- RSS（新しく記録した商品）、sitemap.xml、robots.txt（AI学習専用クローラーを拒否）、OGP、CNAME、.nojekyll。
  生成時に古い `items/` と `archive/` のページを掃除する。商品が0件のページに空の見出しは出さない。

## ネタフルのレビュー連携

`fetch_netaful_reviews.py` が WordPress REST API（認証不要）から記事を取り、商品名と「」内の名前を照合する
（コンビニポチと同じ厳しさ: 6文字以上の部分一致、記事が発売日の14日超前なら除外）。
タグID: マクドナルド=735（208件）、モスバーガー=882（**3件しか付いていない**）。
モスはタグ付けが少ないため、記事検索（search=モスバーガー）の結果のうちタイトルに「モスバーガー」「モス」を含むものも採用している。
関連記事のキーワード辞書は `config.json` の `related_keywords`（商品名とネタフル記事タイトルの実データで選んだ）。

## ブランド

アイコン類は `~/Desktop/Claude/pochi-branding/` で生成（`sites.py` に fastfood の行を追加済み。`make_branding.py` の
`main()` は他サイトのリポジトリを上書きするので実行しない。`build_standard_site` を import して使う）。
