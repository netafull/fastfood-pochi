# フレッシュネス: HTMLのお知らせをブロック扱いにする誤判定の直し (2026-11-28)

## 不具合 (モルが 2026-11-28 の手動 update で発見)
- 2026-11-28 12:00〜12:39 の手動 update で、フレッシュネスの巡回が「ブロック=あり」になり、栄養成分の取り込み (run_pdf_step) ごと飛ばされた。本番でフレッシュネスだけ栄養が 0/20 のまま。
- 引き金は brandnews のお知らせ 5214 (「HAPPY BAG 2027」事前予約受付終了のお知らせ)。200 の通常の HTML で、チャレンジページではない (モルが取得して確認済み)。
- 原因: `fetch.py` の `fetch_binary` は、PDFのはずの取得で本文が HTML だと BlockedError にする (他のチェーンでは HTML = チャレンジページの前提)。`fetch_freshness_press` は「HTML のお知らせなら None」のつもりで書かれていたが、その手前で例外が出て届かなかった。#20 (フレッシュネス追加) からある不具合で、PDFでない新しいお知らせが出たことで表に出た。
- 影響: 5214 には「照合済み」の印が付かないため、このままだと毎回の巡回でブロック扱いになり続け、フレッシュネスの栄養成分も入らない。
- 直しはオーナー判断でマルが担当 (2026-11-28)。

## 変えたこと
- `scripts/fetch.py`: `fetch_binary` に `allow_html=False` を追加。チャレンジの兆候 (`_looks_like_challenge`) がある応答は allow_html でも従来どおり BlockedError。通常の HTML (doctype/html で始まる) は、allow_html=True のときだけそのまま返す。デフォルトの挙動は変えていない。
- `scripts/crawl.py`: `Getter.pdf_get` に `allow_html` を追加して fetch_binary へ渡す。`fetch_freshness_press` は `allow_html=True` で取得するようにし、%PDF でないことを見て None を返す (呼び出し側の既存の処理で「照合済み」の印が付く)。docstring に経緯を書いた。
- テスト: `tests/test_fetch.py` に TestFetchBinaryHtml (4件) を追加。PDFは通る・通常HTMLはデフォルトで BlockedError・allow_html で返る・チャレンジHTMLは allow_html でも BlockedError。

## 検証
- 全テスト 827件 (このブランチは main 基点なのでバーガーキングの6件は含まない)。失敗は従来からの test_regular.py の13件のみで、それ以外の FAIL/ERROR は0件。
- 実物の 5214 で確認: デフォルトでは従来どおり BlockedError、allow_html=True では 25,669 バイトの HTML が返り、fetch_freshness_press が None を返す流れになることを確かめた。

## やらなかったこと・未確認のこと
- ゼッテリアの fetch_zetteria_press も同じ pdf_get を使うが、そちらは PDF への直リンクだけを対象にしていて今回の問題は出ていない。scope を広げないため触っていない (必要になれば同じ指定を足せる)。
- 本番での回復 (次の巡回でフレッシュネスの栄養が入ること) は未確認。マージ後の巡回で、ブロック扱いが消えて栄養が付くことを確認する必要がある。
