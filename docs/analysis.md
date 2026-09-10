# Analysis Workflow

週次の数値はスクリプトで確定し、AIはその数値の読み取りと、確認済み記事に基づく考察に集中する。
集計にn8n・AI APIへのアクセスは不要。保存済み入力だけを使う。

## 入力の準備

n8nは `content/structured-records/YYYY-MM-DD.jsonl` にrun行とarticle行を保存する。
古い日次Markdownしかない場合だけ、次を実行する。既存JSONLは変更しない。

```bash
python3 scripts/backfill_structured_records.py
```

日次の共通本文確認、分類提案、キーワード候補作成を終えてから集計する。
全件採否JSONがない場合は、n8n Data Tablesを読める環境で次を実行する。
これは当日の採否MDとJSONを保存するが、Data Tablesの評価そのものは変更しない。

```bash
python3 scripts/generate_article_feedback_instructions.py
```

`content/article-feedback-instructions/YYYY-MM-DD.md` は代表例の補助指示で、
全件の代用にはならない。JSONの仕様は同ディレクトリのREADMEを参照。
データ取得不能時に過去MDから全件を推測して埋めない。

## 生成 → 読み取り → レポート反映 → 検証

プロジェクトルートで実行する。日付は週次指示書の `sourceRunDate`、
レポートパスは `outputPath`（週の月曜日名）。

```bash
python3 scripts/generate_analysis_reports.py --through YYYY-MM-DD
python3 scripts/read_ai_inputs.py --run-date YYYY-MM-DD --task weekly-report
# AIが本文確認済みの根拠から考察を編集した後に実行
python3 scripts/generate_analysis_reports.py --through YYYY-MM-DD --sync-report content/weekly-reports/WEEK_START.md
python3 scripts/generate_analysis_reports.py --through YYYY-MM-DD --check --check-report content/weekly-reports/WEEK_START.md
```

- `--through`: 収集締切。該当週の月曜からこの日まで。省略時は最新保存日。
- `--week 2026-W36`: ISO週指定。単独指定は日曜まで（未取得日は欠損）。`--through` と併用するなら同じ週。
- `--as-of YYYY-MM-DD`: 評価参照日。省略時は収集締切と同じ。締切後に作成した採否・分類・共通確認を過去週に反映する場合に明示する。生成、リーダー、検証で同じ値を使う。収集締切自体は延長しない。
- `--require-feedback`: 全件採否JSONが欠落・不完全・対応MDより古い場合に保存せず失敗する。付けなければ `provisional`（暫定・採否未反映）。
- `--check`: 入力から再計算し、保存JSONと2種類のMarkdownが一致するか検証する。書き込みなし。
- `--sync-report PATH`: 週次考察レポートの `weekly-metrics:start/end` ブロックのみ挿入/更新し、それ以外の文章を保存する。
- `--check-report PATH`: 保存JSON・分析Markdown・考察レポートの自動ブロックを検証する。書き込みなし。`--sync-report` と同時使用不可。
- 従来の `--records-dir`、`--ai-candidate-dir`、`--classification-proposal-dir`、`--output-dir` も利用可能。

例：9月3日までの収集に、9月10日までの評価を反映する場合：

```bash
python3 scripts/generate_analysis_reports.py --through 2026-09-03 --as-of 2026-09-10 --require-feedback
python3 scripts/read_ai_inputs.py --run-date 2026-09-03 --as-of 2026-09-10 --task weekly-report
python3 scripts/generate_analysis_reports.py --through 2026-09-03 --as-of 2026-09-10 --check
```

## 出力と責任分担

```text
content/analysis/weekly-metrics/YYYY-Www.json                    # 数値の正本・URL別判断台帳・入力ハッシュ
content/analysis/weekly-reports/weekly-trends-YYYY-Www.md       # 同じ正本から生成
content/analysis/keyword-quality/keyword-quality-YYYY-Www.md   # 同じ正本から生成
content/weekly-reports/WEEK_START.md                          # AIの考察＋同期した数値ブロック
```

週ごとのJSONと分析Markdownは再生成で置き換わる。入力ファイル、分類提案、
日次要約は変更しない。1ファイルずつ一時ファイルから置換し、同じ週の
同時書き込みをロックする。3ファイル全体のトランザクションではないため、
中断後は再生成し検証する。ロックが残った場合は動作中の生成処理がないことを
確認してからその週の `.lock` ファイルだけを解除する。

同一入力・ルール・実装なら同じ `snapshotId` になる。集計JSONには入力パスと
SHA-256も保存する。入力やルールが変われば、数値が同じでも古いスナップショットは
検証に失敗する。集計中は入力を更新せず、日次作業の完了後に実行する。

AIは数値を手で再集計せず、根拠の選定、話題の説明、考察、限界の説明を行う。
自動ブロック外の古い数値はAIが削除または正本へ整合させる。
検証コマンドは自由記述中の数値や意味的な主張の正しさまでは検証しない。

## 集計定義

| 指標 | 定義 |
| --- | --- |
| 報告件数 / 保存行数 | runのarticleCount合計 / 実際のarticle行数。混同しない |
| URL数 / 重複行数 | URL文字列の完全一致で重複除去 / 保存行数からURL数を引いた値 |
| 代表記事 | 締切内の最新収集日、その日の最後の保存行。同日の本文取得だけを結合 |
| 除外 / 対象記事 | 完全な採否JSONを適用したURL数 / URL数から除外を引いた値 |
| 媒体不信の除外 | 該当URLに加え、ドメインまたは媒体ラベルが一致する記事。サブドメインに拡張しない |
| その他の不可理由 | 関係なし・取得不能・古すぎる、いずれも該当URLだけ |
| 分類網羅率 | 有効な分類提案ありの記事数 ÷ 対象記事数 |
| 主カテゴリ / 記事種別 / 関連度 | 各件数 ÷ 有効な分類提案ありの記事数。未分類を分母に入れない |
| 媒体比率 | 媒体別件数 ÷ 対象記事数 |
| 本文確認ready | 最新入力・ルールに有効な共通確認記録で、basis=bodyかつ要約工程ready |
| 人物・団体等 | 上記本文確認readyのentitiesだけを既存別名辞書とNFKCで正規化し、異なるURL数を数える |
| キーワード一致 | 対象記事のタイトル・RSS抜粋に対する文字列一致。意味的関連度ではない |
| 候補判断 | 対象収集期間の候補Markdown行に記録された採否回数。記事頻度ではない |

期間はJST。評価ファイルの日付と評価時刻は `reviewAsOf` の日末まで。
タイムゾーンのない旧評価時刻はUTCとして扱う。評価は保存されたスナップショットを
参照するもので、過去日時のDB状態を復元する機能ではない。
完全採否JSONは最新の全件状態を使い、同じURLの評価は時刻が新しいものを採用する。
同時刻で矛盾する評価や不正な入力は失敗させる。
媒体不信が有効なら、同媒体の別URLに可評価があっても媒体除外を優先する。
1記事に複数の除外理由が付くため、理由別件数の合計は除外URL数とは限らない。

分類提案は評価参照日までの最新ファイルをURL別に採用する。形式、分類体系、
信頼度、時刻、入力ハッシュ（ある場合）を検証する。
入力ハッシュのない旧提案は `unboundClassifications` として明示し、
`ai_review` ラベルだけを本文確認の証拠にはしない。

## AIへの入力と限界

n8nのv2週次指示書は固定ルール、収集範囲、集計JSONパターン、出力先を参照する。
新規生成分にはISO週を解決した `sourceMetricsPath`、分類・全件採否JSONの参照、
`reviewAsOf` も含まれる。指示書内の生成・反映・検証は短い結果ラッパーを使い、
集計の入力リーダーは直接実行する。考察保存前と保存後のコマンドブロックを区別する。
[週次固定ルール](ai-rules/weekly-report.md)を読み、生成してから
`read_ai_inputs.py --task weekly-report` を使う。既定は短い集計概要であり、
記事台帳全件をAIに返さない。入力変更後は再生成を要求する。

根拠記事の一覧が必要な場合だけ次を使う：

```bash
python3 scripts/read_ai_inputs.py --run-date YYYY-MM-DD --task weekly-report --weekly-articles --offset 0 --limit 20
```

これは従来同様、除外前の生記事メタデータ。全ページを読む場合は
`nextOffset` がnullになるまで続ける。JSONの `articleDecisions.excludedReasons` を
参照し、除外記事を考察の根拠にしない。本文は対象記事のrunDateと
`--task article-summary --include-body --article-url URL` で必要時だけ読む。

- 欠損日と0件取得日を区別する。採否反映済みでも全日取得済みとは限らない。
- 取得成功数、共通本文確認ready数、要約完成数は別。旧要約Markdownを自動的に共通確認済みへ変換しない。
- `videoMetadataSummaries` は現状null（未計測）。動画メタデータ取得数から要約完成数を推測しない。
- 意味的な話題の網羅的抽出・未知の別名統合は自動化していない。entitiesのない記事は頻出名集計に入らない。
- 人物等の `representativeDates` は選択された代表収集日であり、公開日や全再取得日ではない。
- 通常記事の考察は本文確認済みの日次要約・共通確認の根拠を使う。動画メタデータは別扱いで明記する。
- 取得数は視聴数、注目度、社会的流行を意味しない。
- 外部AIチャットには指示書だけでなく、必要なルール・集計概要・根拠を添付する。
