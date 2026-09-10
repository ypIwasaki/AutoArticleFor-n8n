# 定型運用コマンドと再開

入口は `python3 scripts/autoarticle_ops.py`。WSL/Linuxでプロジェクトルートから実行する。
日付の省略は**JSTの今日**。過去資料の作業・反映・再開は `--date YYYY-MM-DD` をサブコマンドの前に付ける。
記事を読み、根拠に基づいて成果物を作る工程はCodexが引き続き担当する。この入口はAIレビューを自動で済ませない。

## 通常使うコマンド

| 目的 | コマンド（共通の `python3 scripts/autoarticle_ops.py` に続ける） |
| --- | --- |
| 一括状態確認（読み取り専用） | `--date YYYY-MM-DD status` |
| 保存済み進捗と現在の状態を照合（読み取り専用） | `--date YYYY-MM-DD resume` |
| n8nを起動、起動済みなら再利用 | `start n8n` |
| 今日の収集を開始、検証できる既存実行は再利用 | `collect` |
| レビュー済み人材→分類の順でDB反映 | `--date YYYY-MM-DD apply` |
| 片方だけ反映 | `--date YYYY-MM-DD apply --kind talent` / `--kind classification` |
| 確認アプリを起動、起動済みなら再利用 | `start dashboard` |

起動・収集・DB反映は実際の操作なので、依頼された範囲だけ実行する。
`start n8n` はワークを有効化・同期・収集しない。起動後の `status` でactive・schedule・timezoneも確認する。
`start dashboard` はHTTP疎通まで。返されたURLをCodexのブラウザで開き、対象日と表示内容・データソースを確認する。
既定は `http://127.0.0.1:8765/`。画面を見ずにページ確認済みと記録しない。

### 状態JSON

`status` は n8n疎通、対象ワークID・有効状態・スケジュール・ローカル定義との一致、
対象日の実行状態別件数・直近3件、実行中件数、8種の生成物と5種の成果物群の有無を返す。
実行日はstartedAtまたはstoppedAtをJST換算して照合し、前日から継続中の実行も重複実行防止の対象にする。
本文・候補・全履歴は標準出力に出さない。

`artifactVerification: existence_only` は保存の有無であり、レビューや今回の収集成功の証明ではない。
APIキーなし・疎通失敗・履歴取得不完全は `unknown` と理由コードで返す。未実行や0件に置き換えない。
API履歴はページングし最大50ページで止める。上限や権限エラーで確認できない場合、収集は送信しない。
`status` / `resume` の終了コード0は「照会JSONを返せた」の意味。JSON内のunknownや未完了を読む。
操作の停止・検証失敗は短いJSONと終了コード2。サーバー応答本文やAPIキーをエラーに転載しない。

## レビュー工程を記録する

工程別トークン量も記録する場合は、作業前に `tokens begin STEP` を呼ぶ。
checkpoint成功時に一致する工程の計測を閉じ、作業日別Markdownを更新する。
初回ログ接続・未割当の差分補完・計測範囲は [トークン記録](token-usage.md) を参照。

各成果物を保存・検証した時点で、工程・検証根拠のファイル・確認内容を指定する。
根拠は既存のレビューMarkdown、検証結果ファイルなどを使い、定型文だけの架空の証拠を作らない。

```bash
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD checkpoint summary --evidence content/article-summaries/YYYY-MM-DD.md --note '本文根拠と出典を確認。未取得記事は保留として記載'
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD checkpoint talent-review --evidence content/talent-index-proposals/YYYY-MM-DD.md --note '人物・記事対応、根拠、既存IDと承認状態の維持を確認'
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD checkpoint classification-review --evidence content/article-classification-proposals/YYYY-MM-DD.md --note '本文根拠とtaxonomyを照合。保留記事を除外'
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD checkpoint keywords --evidence content/ai-keyword-candidates/YYYY-MM-DD.md --note '候補語の出典・既存語との重複を確認。採用操作なし'
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD checkpoint weekly --evidence content/weekly-reports/MONDAY.md --note '考察を確認。採否未反映のため暫定'
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD checkpoint page --evidence content/page-checks/YYYY-MM-DD.md --note '対象日の記事・要約・分類・週次表示とDBデータソースをブラウザで確認'
```

上記の日付・月曜日・確認文は実際の対象と結果に合わせる。`--evidence` は複数回指定できる。
`page` の根拠ファイルは実際の表示確認を記録したローカルファイルを指定する（このパスは例）。
短いログラッパーを使った検証なら、その実行のresult.jsonや必要なログも根拠にできる。

記録には必須出力の存在、根拠・入力・本文確認記録・ルール・設定・出力のSHA-256を保存する。
存在しない入力もnullで記録し、後から追加されたら再確認対象にする。
`verification: operator_attested` は**操作者が確認したという記録**であり、スクリプトが意味的な正しさを保証したという意味ではない。
保留・部分確認・暫定をnoteと成果物に残す。工程completedでも「全記事をreadyにできた」という意味ではない。
週次checkpointは既存集計スクリプトの `--check --check-report` も実行し、数値・参照日の一致を検査する。
暫定値でも検査は通り得るため、記録のmetricsStatusとnoteを確認する。

## 再開・二重実行防止

`.operation-state/YYYY-MM-DD.json` に最新工程状態と履歴を保存する。ファイルを一時保存→fsync→置換し、
全日付共通のOSロックでこの入口同士の同時操作を防ぐ。ロックはプロセス終了で解放される。
ログは `.operation-logs/`。いずれもGit管理しない。別PCには完了状態が自動移行しない。
必要な監査記録を移す場合も、別PCの `resume` で再照合する。

再開時は `resume` の `progress` を読む。

- `not_recorded`：記録なし。既存ファイルがあっても自動で完了にしない。
- `completed`：保存時の根拠が現在も一致。レビュー工程は操作者確認に依存する。
- `stale`：対象環境・入力・成果物などが変化。該当工程を再確認する。
- `unverified`：DB・実行履歴・週次数値等の再検証ができない。完了扱いを引き継がない。
- `submission_unknown`：POST前の意図記録が残っている。通信失敗・中断でも自動再送しない。
- `starting`：起動を要求済み。ログ・プロセス・疎通を調べる。
- `needs_display_recheck`：過去にページ確認済みでも、今回の画面は未確認。

`resume` 自体は状態ファイルを書き換えず、再収集・反映・プロセス起動もしない。
不明なPOSTも現在の実行結果・DB内容で解決できれば、照合結果ではcompletedを返す。
過去のsubmission_unknown記録は履歴として保持する。保留事項はevidenceFileのnoteを読み、
週次の暫定状態はweeklyMetricsStatusで確認する。
収集の完了は実行ID、成功状態、生成ノードの日付・件数、アーカイブの生成時刻、全生成物と照合する。
Markdownの実行バイナリが保存されていれば内容ハッシュ、外部保存ならノードの出力パスと書込時刻を検査する。
過去実行の保持期限切れ、古い形式、複数実行、後日の同週指示書上書き等は保守的に止まることがある。
既存実行が1件の成功で証拠が一致すれば `collect` は再POSTせず記録する。
複数実行や失敗・不明時はn8n側の履歴と生成物を個別確認する。

反映時はPOST前に `submission_unknown` を保存する。対象提案のレビュー記録が現在のファイルと一致することを要求する。
正常応答の日付・件数に加え、対象DBのキーと各保存フィールドを読み取り専用で比較する。
同じ提案の再呼び出しはDB一致を確認して記録するだけで、POSTしない。不一致なら停止する。
`apply` は人材→分類の順。途中停止した場合は先行工程の記録を保持し、全反映済みと報告しない。
空の分類提案は送れない。保留で0件なら `--kind talent` のみを使い、分類未反映の理由を残す。

失敗を回避するため状態ファイルを削除したり、直接curlで再送したりしない。
再実行が必要なら、最初のPOSTが未実行または部分反映である根拠、対象範囲、再実行の影響を確認し、
その範囲の操作について指示を得て既存の個別手順で対応する。本コマンドには無条件force/retryはない。
外部のスケジュール・UI操作はロック対象外。n8n側に同時実行の排他や冪等キーは追加していないため、
定時実行と手動操作の競合を完全に防ぐ「exactly once」の保証ではない。

## 設定・対応範囲

接続設定は既存の `.env` / 環境変数を再利用する（環境変数優先）。

- `N8N_API_BASE_URL`（または `N8N_BASE_URL`）、`N8N_API_KEY`。
- `N8N_WORKFLOW_ID`：収集ワーク。未指定ならリポジトリJSONのnameと完全一致するワークを1件に解決。
- 任意の `AUTOARTICLE_TALENT_WORKFLOW_ID`、`AUTOARTICLE_CLASSIFICATION_WORKFLOW_ID`：反映ワークのID。
- `N8N_DATABASE_PATH`、`N8N_USER_FOLDER`：既存確認アプリと同じSQLiteパス設定。
- `N8N_ARTICLE_CLASSIFICATIONS_TABLE_ID`：既存分類テーブルID。実際のn8n起動環境にも設定が必要。
- `TALENT_DASHBOARD_HOST`、`TALENT_DASHBOARD_PORT`：確認アプリ。既定127.0.0.1:8765。

起動はローカルHTTP、反映のDB照合はローカルn8n/SQLite向け。リモートn8n/PostgreSQLでは状態照会だけを使い、
起動やDB照合を迂回しない。SQLiteはmode=roのトランザクションで読み、ワークID・名前・テーブルIDも確認する。
起動には既存シェルを使い、バックグラウンド・ログ保存・最大約40秒の疎通確認を入口で行う。
異常な既存ポート、保存済みPIDが生存、起動途中・起動失敗の場合は再起動を重ねない。
`.env` の暗号化キー等を新しく起動環境全体へ注入せず、元の起動シェルの設定方法を維持する。
非標準のDBやn8nユーザーフォルダは、n8n自身の起動設定と照合用設定を一致させる。

収集・反映はローカルJSONと稼働定義（ノード・接続・ローカルに明記した設定）の差分があると停止する。
同期や有効化を自動で行わない。必要な場合だけ [n8n API同期](n8n-api-sync.md) を読み、差分と権限を確認する。
反映提案は既存契約の各保存フィールドと時刻を明示する（サーバー側の現在時刻等の暗黙既定値を使わない）。
人物承認・検索有効化の変更は禁止し、既存状態を維持する。候補語採用、Git操作も追加しない。

固定ルール・入力リーダー・共通本文確認・週次集計の内容は従来どおり。
詳細ログだけが必要なら [短い結果ラッパー](operation-results.md) を使う。入力本文を隠す目的では使わない。
