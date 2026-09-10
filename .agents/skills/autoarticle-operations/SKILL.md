---
name: autoarticle-operations
description: "AutoArticleFor-n8nの運用を進める。n8nの起動・自動実行への準備、収集状況の確認、実行時間後の収集と生成指示書に沿う記事作業、確認アプリの起動を依頼されたときに使う。一般的なn8n開発や、このプロジェクトのコード変更だけの依頼には使わない。"
---

# AutoArticle 日次運用

既存の固定ルールとスクリプトを使い、依頼範囲を検証して完了する。全手順の再設計や、ルール・本文・ログの一括読み込みをしない。

## 開始と範囲

- ユーザーの明示指示を優先する。「状態確認」は読み取りだけ、「起動して備える」は起動・定時実行の準備まで。「生成済み資料で作業」は収集し直さず記事作業から始める。全工程の依頼だけを最後まで進める。
- 対象のワークスペースからプロジェクトルートを特定し、`n8n/workflows/daily-keyword-news-summary.workflow.json`、`scripts/read_ai_inputs.py`、`docs/ai-rules/` を確認する。不明なら場所を尋ね、別プロジェクトを操作しない。
- 以下のパス・コマンドはすべてそのルート基準。現在の構成ではWSLで実行する。別PCではユーザー名・絶対パス・DB・ポートを決め打ちしない。日次指示書は `content/ai-*-instructions/YYYY-MM-DD.md`、週次だけは `content/ai-weekly-report-instructions/WEEK_START.md`（JSTの月曜日）。
- 最初に `docs/ai-rules/operation-result.md` を読む。各工程では以下で指定した資料だけを必要時に読む。既存の未コミット変更・成果物を保持する。
- 通常の入口は `python3 scripts/autoarticle_ops.py`。`--date YYYY-MM-DD` はサブコマンドの前に置き、省略時はJSTの今日。最初に `--date YYYY-MM-DD resume` で状態と進捗を一括確認する。単なる状況照会は `--date YYYY-MM-DD status` だけでよい。どちらも読み取り専用。unknownを未実行、ファイル存在を完了と扱わない。
- コマンドの設定・停止理由・根拠の書式が不明な場合だけ `docs/autoarticle-operations.md` を読む。本文やAPI応答の全量取得で手順を組み立て直さない。
- 実運用の開始時は、このCodexタスクのログを `tokens bind` で選ぶ（既存選択を使う場合もタスクID一致を確認）。環境から取得できないWSL等では `docs/token-usage.md` の初回接続だけを参照し、別タスクの「最新ログ」を推測して選ばない。計測不可でも運用を妨げず、未計測を明示する。単なる状態照会では計測用ファイルも作らない。

## 運用手順

1. **n8nの準備**：起動が依頼範囲なら `python3 scripts/autoarticle_ops.py start n8n`。既存起動を再利用し、必要時だけログ付きで常駐起動する。続けて `status` のactive・schedule・timezone・definitionMatchesを確認する。設定変更による再起動・同期は別途影響を確認する。「備える」だけなら収集せずここで終える。

2. **ワーク定義の確認**：初回設定・不整合・更新反映が必要な場合だけ `docs/n8n-setup.md` と `docs/n8n-api-sync.md` を読む。ローカルJSONの編集だけでは稼働側に反映されない。同期は対象・稼働側との差分・依頼の権限を確認してから `scripts/sync_workflow_to_n8n.py` を使う。無関係なワークやUI変更を上書きしない。同期の許可が必要なら、その工程の前で確認する。

3. **収集**：依頼範囲の場合だけ `python3 scripts/autoarticle_ops.py collect`。今日JSTの対象ワーク・実行履歴・生成物を照合し、検証可能な既存実行は再利用する。過去日再収集、実行中、失敗・複数実行、不明なPOST、定義不一致は停止する。停止を迂回してcurl再送や状態削除をしない。生成済み資料の作業だけならこのコマンドも実行しない。

4. **日次記事作業**：生成指示書の対象日 `sourceRunDate`（旧形式は収集記録と照合）を使う。記事要約、人材索引、分類、キーワード抽出の指示書を順次読み、`rulesPath` とその必須参照を読む。入力は `scripts/read_ai_inputs.py` の対応taskで分割取得し、記事・本文の継続を必要範囲まで確認する。本文未取得なら取得処理へ進み、コマンド確認は `python3 scripts/capture_article_contents.py --help`。ローカル取得だけなら `--no-sync-contents`、DB同期は反映範囲に含まれる場合だけ行う。
   要約工程で `docs/ai-rules/shared-article-review.md` に沿う共通記録を `scripts/save_article_review_facts.py` で検証・保存し、人材・分類で再利用する。current/readyは再利用、current/heldは保留継承、不足・失効時だけ本文へ戻る。自動下書きや取得成功を意味的なレビュー完了とせず、未確認の工程をreadyにしない。各指示書の出力契約どおり保存・検証する。

5. **週次作業**：日次作業後、週の月曜日名の指示書を読み、`docs/ai-rules/weekly-report.md` に従う。指示書の「集計→入力読み取り」、考察保存後の「数値反映→検証」を順に実行する。旧指示書にコマンドがない場合や参照日を変える場合だけ `docs/analysis.md` を読む。全件採否JSON、収集締切、評価参照日を揃え、数値を手集計しない。採否欠落は暫定とし、確定が必要なら `--require-feedback`。本文確認・要約完成・動画メタデータを混同しない。

6. **提案のDB反映（依頼範囲の場合だけ）**：人材・分類レビューのcheckpointを保存後、`python3 scripts/autoarticle_ops.py --date YYYY-MM-DD apply`。記事・人材→分類の順に反映し、応答と実DBを照合する。片方だけなら `--kind talent` または `--kind classification`。契約や保留の扱いを確認する場合だけ `docs/talent-article-index.md` / `docs/article-classifications.md` を読む。提案保存とDB反映を区別し、人物承認・検索有効化・候補語採用・Git操作を付随させない。

7. **確認アプリ**：`python3 scripts/autoarticle_ops.py start dashboard`。返されたURL（既定 `http://127.0.0.1:8765/`）をブラウザで開き、対象日の記事・要約・分類・週次成果物と表示データソースを確認する。DB表示か提案JSONの代替表示か不明なら `docs/talent-dashboard.md` を読む。HTTP疎通だけを表示確認完了とせず、ブラウザ操作ができなければURLと未確認範囲を返す。

## 工程記録と再開

- 各工程の判断・本文・ルール読み取りを始める前に `python3 scripts/autoarticle_ops.py --date YYYY-MM-DD tokens begin STEP`。STEPは上記工程の `prepare`、`n8n`、`collect`、`capture`、`shared-review`、`summary`、`talent-review`、`classification-review`、`keywords`、`weekly`、`apply`、`dashboard`、`page`、`report` 等。次のbeginは前工程の計測を閉じる。省略した工程を実行済みとしない。
- 起動・収集・反映はコマンドが `.operation-state/YYYY-MM-DD.json` に自動記録する。記事作業・表示の保存と検証が済んだら `python3 scripts/autoarticle_ops.py --date YYYY-MM-DD checkpoint STEP --evidence 根拠ファイル --note '実際の確認内容と保留事項'`。STEPは `summary`、`talent-review`、`classification-review`、`keywords`、`weekly`、`page`。根拠を捏造せず、既存のレビュー・検証結果を参照する。
- checkpoint成功時は一致する工程の計測も終了し、作業日JSTごとの `content/operation-usage/YYYY-MM-DD.md` とJSONを更新する。計測だけを閉じるなら `tokens end STEP`。依頼範囲の終了・中断時は `tokens finish`。次回に `tokens report` で遅延通知を再集計する（運用resumeとは別）。finish後の最終返答は計測範囲外であり、全使用量を捕捉したと主張しない。
- 未割当の量も前後の累積値の差から求める。工程が一意なら割当、配分不明は数量・計算根拠を付けて未割当とする。均等割や時間按分、キャッシュ・推論の二重加算をしない。詳細・未計測理由は `docs/token-usage.md` を必要時だけ参照する。
- checkpointはファイル一致と操作者の確認記録であり、本文の意味を自動検証するものではない。週次は数値検証も実行する。未確認を記録せず、共通本文確認を先に整えてから最終checkpointを保存する。
- 再開時は `resume` で現在の入力・成果物・DB・実行履歴と照合する。`stale` / `unverified` は再確認、`submission_unknown` は実行有無の調査、`needs_display_recheck` は画面再確認。記録なしは未確認。再開コマンド自体は操作を再実行しない。保留・暫定を含む工程完了と全記事レビュー完了を混同しない。

## 記事作業と反映を途中で保留しないための進め方

- 全工程の依頼に「記事作業」「レビュー済み提案のDB反映」が含まれていれば、その範囲は承認済み。提案の保存・レビュー・検証後に反映まで進め、同じ許可を再確認する理由で止めない。
- 本文未取得・部分取得・HTTP 429は記事単位の保留として記録する。取得可能な他ホストの処理、全記事の登録候補化、根拠のある人物関係・分類、キーワード・週次作業を継続する。未確認記事をreadyへ変えることで解消しない。
- 収集や反映の応答が不明でも、再送せず既存の実行・保存ファイル・実DBを照合する。既知の完了を確認できたら次工程へ進む。日時表記等の実装不具合は根拠付きで修正・検証する。真の失敗・不一致・権限外の変更は保留理由を明記する。
- 終了前にresumeで依頼範囲を照合し、工程の記録がない場合は作業へ戻る。記事単位の保留と、記事作業・DB反映工程そのものの未実施を区別する。

## 完了・例外

- 依頼された工程ごとの保存・検証・反映・表示を根拠付きで確認する。途中の失敗、保留、欠損、暫定、未反映を残し、全工程完了と誤報しない。
- エラーは該当工程のログ・資料だけを追加確認する。大量出力の単発コマンドは `scripts/operation_result.py`（使い方は `docs/operation-results.md`）で短く返す。記事本文や根拠の入力リーダーは隠さない。
- 最終報告は対象日・完了範囲・主要件数・未完了事項・確認リンクを短く返す。詳細ログは貼らない。検証だけの依頼で実行・反映まで広げない。
