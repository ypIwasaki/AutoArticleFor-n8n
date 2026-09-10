# 週次レポートの固定ルール

週次指示書の `weekStart`・`weekEnd`・`coveredThrough` を使い、日本語の週間調査レポートを更新する。週は月曜から日曜、JST。出力は週開始日だけをファイル名に持つ同じMarkdownとし、次の月曜から新しいファイルにする。

## 入力と作業

1. このルールを読み、日次の確認・分類・候補作成後に、プロジェクトルートで下記の生成・読み取りを行う。日付は指示書の `sourceRunDate`。既定の週次リーダーは全記事ではなく検証済みの集計概要を返す。保存済み集計がない・入力が変わった場合は再生成が必要。
2. 採否は `content/article-feedback-instructions/YYYY-MM-DD.json` の全件スナップショットを使う。Markdownは代表例であり、そこから全件を推定しない。必要なら既存の `python3 scripts/generate_article_feedback_instructions.py` でn8nから当日のMDとJSONを出力する。過去期間に後日の評価を適用する場合は、その日を明示的に `--as-of` に指定し、生成・読み取り・検証で統一する。指定しない場合は収集締切日までの評価だけを使う。
3. 数値の唯一の正本は `content/analysis/weekly-metrics/YYYY-Www.json`。URL重複、最新保存行の選択、不可評価のURL・媒体除外、欠損日、分類・媒体の件数と分母はスクリプトに任せ、手計算し直さない。採否JSONが欠落・不完全・指示書より古い場合は「暫定・採否未反映」と明記する。確定用途では生成時に `--require-feedback` を付け、未反映のまま進めない。欠損日を0件と同一視せず、部分レポートとして扱う。
4. 通常記事の週間考察には、保存済み日次要約に本文確認済みの記録がある記事の本文要約だけを根拠として使う。タイトル・RSS抜粋だけで通常記事の事実を推測しない。
5. YouTube・動画ページ・配信アーカイブは、ページで確認したタイトル・概要欄・公開日時から短く要約してよい。「動画メタデータ要約」と明記し、本文確認済み記事や動画本編の確認と混同しない。メタデータを取得できない場合は「未確認」とする。
6. 人物・団体等の件数は、共通確認記録の本文確認済み・要約readyのentitiesを既存別名辞書で正規化したURL数を使う。任意の話題の意味的な出現頻度は自動確定していない。JSONにない数値を推測・手集計で補わず「未集計」とする。回数だけで流行を断定しない。
7. 配信で使われたゲーム、イベント、グッズ、コラボ、オーディション、企業発表などの主題を整理する。事実と考察を区別し、根拠のない評価・人物情報を追加しない。
8. 既存レポートの考察を読み、今回までの全期間の集計を再生成して自動ブロックを同期する。考察・根拠選定は引き続きAIが行う。取得数を視聴数・注目度・社会的流行そのものとして扱わない。

## 実行と検証

```bash
python3 scripts/generate_analysis_reports.py --through YYYY-MM-DD
python3 scripts/read_ai_inputs.py --run-date YYYY-MM-DD --task weekly-report
# 考察を編集した後、指示書のoutputPathへ数値ブロックだけを反映
python3 scripts/generate_analysis_reports.py --through YYYY-MM-DD --sync-report content/weekly-reports/WEEK_START.md
python3 scripts/generate_analysis_reports.py --through YYYY-MM-DD --check --check-report content/weekly-reports/WEEK_START.md
```

- 全件台帳は集計JSONの `articleDecisions`。必要な記事だけ根拠を確認する。メタデータ一覧が必要な場合に限り `read_ai_inputs.py --run-date YYYY-MM-DD --task weekly-report --weekly-articles --offset 0 --limit 20` を使い、`nextOffset` で続ける。この一覧は除外前なので、台帳の `excludedReasons` があるURLを考察の根拠にしない。本文は当該記事のrunDateで `--task article-summary --include-body --article-url URL` を指定する。
- `bodyReviewReadyArticles` は有効な共通本文確認記録の数で、過去の要約Markdownの確認済み件数や要約完成数ではない。自動で過去要約を確認済みに変換しない。
- `videoMetadataSummaries: null` は「未計測」。取得状態から動画要約完成数を推定しない。
- 分類の比率は有効な分類提案ありの記事を分母にする。入力ハッシュのない旧提案は `unboundClassifications` として区別し、手法ラベルだけで本文確認済みとしない。
- 数値は `weekly-metrics:start/end` 内にまとめ、外側の古い集計値は削除またはJSONに整合させる。`--sync-report` は考察を保存するが、その数値・意味は検証しない。`--check-report` の対象も自動ブロックだけであり、本文の根拠と記述は別途確認する。

## 出力契約

指示書の `outputPath`（既存の `reportPath` と同じ対象）へ保存する。次の構成を維持する。

```markdown
# Weekly News Research Report - WEEK_START to WEEK_END

## Coverage

## Weekly Assessment

## Frequent Terms and Topic Signals

| Term or topic | Type | Articles | Why it mattered this week | Evidence |
| --- | --- | ---: | --- | --- |

## Major Themes

### テーマ名

## Video and Stream Archive Highlights

| Video or archive | Published | Summary basis | Summary | Related talents or groups | URL |
| --- | --- | --- | --- | --- | --- |

## Category and Source Overview

## Limits and Follow-ups

## Daily Input Files
```

- Coverage: 週とJST、今回までの範囲、評価参照日、欠損日、暫定/採否反映済みの区別。数値は自動ブロックを参照し、未計測と0件を区別する。
- Weekly Assessment: 今週の流れを根拠URLまたは記事タイトル付きで3–6項目。
- Frequent Terms: 種別は `talent / group / event / game / goods / collaboration / business / other`。JSONにある記事数だけを記載し、それ以外は未集計とする。確認できる内容、考察、根拠を記載。
- Major Themes: 本文確認済み要約または動画メタデータから分かる事実、関連する人物・団体、週内での広がり。
- Video and Stream Archive Highlights: 確認根拠を必ず「動画メタデータ要約」または「未確認」とし、公開日・人物/団体・URLを記載。
- Category and Source Overview: 確認できた分類件数と偏り、情報源の偏り、転載、本文未確認の比率。
- Limits and Follow-ups: 未取得日、本文未確認、評価で除外した記事、次週の事実確認事項。
- Daily Input Files: 実際に参照した構造化記事と関連する要約・分類ファイルを列挙。
