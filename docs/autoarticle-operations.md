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

## 大きい収集結果・複数出力枝の検証

収集Webhookは最後に完了した枝の出力を返すことがあるため、`saved/date` の返答だけを必須条件にしない。対象日の成功実行が一意で、実行ID・記事件数・生成時刻・全生成ファイルの内容または書込時刻が一致した場合に限り完了とする。`webhookResponseVerified` には返答自体の確認結果を残す。未知・失敗・複数実行・証拠不一致では停止し、POSTを再送しない。

実行詳細の読み取り専用GETは256MiBまで、それ以外の応答は従来どおり32MiBまでとする。上限超過を成功扱いにせず、履歴・状態の削除や無制限読み取りで回避しない。

本文取得でHTTP 429を受けたホストには、その取得プロセス中の追加リクエストを送らない。他ホストや解決済みURLの取得は継続する。後続URLの未試行を理由に明記し、本文取得・意味的レビュー・記事登録・分類反映を区別する。

DB照合では日時列だけをUTC・ミリ秒精度に正規化し、n8nがSQLiteへ保存する日時表記と比較する。時差・ミリ秒単位の差や本文・ID等の不一致は引き続き停止する。送信済みで実DBが一致すれば再送せず照合で完了する。

## 全工程を依頼された場合の継続

記事作業・レビュー済み提案のDB反映を含む依頼では、追加の同意待ちにせず、成果物の保存・検証・checkpoint・apply・実DB照合まで進める。本文未取得は記事単位で理由を残し、確認できた関係・分類の反映を妨げない。未確認をreadyに変更しない。終了前のresumeで工程の未実施を検出したら、許可範囲内の残作業を継続する。

## 通常運用と障害調査の分離

### 通常経路

対象工程の直前に読み取り専用チェックを行う。collectは接続・稼働定義・実行履歴・検索設定、talent/classificationはさらにレビュー証跡・提案の保存項目・DB参照先・既存送信の照合を確認する。提案作成前に反映チェックを合格させる必要はない。

```bash
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD tokens begin preflight
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD preflight --kind collect
# 記事・人物提案のレビュー後は --kind talent、分類レビュー後は --kind classification
# 起動済み確認アプリの疎通は --kind dashboard
```

preflightはPOST・起動・同期・証跡の書換えを行わない。readyは軽い事前条件の成立を示すだけで、本文レビュー完了や全提案スキーマ検証・実行の成功保証ではない。not_readyは終了コード2。通常コマンドにも既存の直前検証を残す。過去の証跡が設定変更でstaleになった場合も、安易に消さず差分を確認する。

### 異常時だけの調査経路

start/collect/apply/checkpointが失敗すると、元の理由コードに加えて`.operation-logs/diagnostics/`の診断ファイルを返す。自動保存はローカルの工程状態・保存済み実行ID・変化したファイル最大10件・起動ログの場所のみ。本文、認証情報、API応答全体や過去ログは転載しない。診断保存が失敗しても元の失敗を保持する。

```bash
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD tokens begin investigation
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD diagnose --step apply-talent
```

`--step`には失敗応答の工程名を使う。診断は関連する接続・ワーク・最近の実行ID（最大3件）・DB不一致の項目名（最大10行）を読み取り、別ファイルに保存する。実DBの本文や値は保存しない。通常の失敗応答では追加のネットワーク調査を自動実行せず、診断コマンドで必要時に取得する。summary等のレビュー工程はローカル証跡のみ、n8n起動の詳細は記録された起動ログを確認する。

### 修正後の再開と計測

```bash
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD resume
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD tokens begin apply
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD apply --kind talent
```

resumeは`issues`に照合失敗理由、`nextSteps`に再確認・未実施工程を返す。依頼範囲内だけを再開する。unknownは未実行扱いにせず、既存実行・ファイル・実DBを照合する。送信済みapplyは既存の照合経路のみで完了し、不一致なら再送せず停止する。調査中に証跡を削除・成功扱いに書換えない。

次のtokens beginで前工程の計測が閉じる。調査資料を読み始める前にinvestigationへ切り替え、通常工程へ戻る前に対応する工程をbeginする。計測区分の分離であり、別タスクの自動作成や会話履歴の自動切り離しは行わない。

## 成果物を保存した直後の検証

要約・人材提案・分類提案・週次レポートを保存または修正した直後、次工程へ進む前に対象の検証を実行する。

```bash
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD validate summary
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD validate talent-review
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD validate classification-review
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD validate weekly
```

これは保存後に呼び出す読み取り専用コマンドであり、ファイル監視の常駐処理ではない。日次運用の保存手順として実行し、`checkpoint` も同じ検証を再実行する。不合格なら新しい完了記録を保存せず、対象ファイルを修正して再検証する。以前の記録は保持され、変更済み成果物はハッシュ不一致で失効する。

- 要約：確認アプリと共通のMarkdown解析処理で件数を照合する。URL、本文確認表示、要約ラベル、重複、現在の共通レビューのready件数との一致を確認する。全件保留でreadyが0件なら要約0件を許容する。
- 人材・分類提案：日付、必須項目、キー重複、日時、配列・真偽値・確信度、記事URLと参照先を確認する。人物関係・分類の対象は現在のレビューが対応工程でreadyである必要がある。分類は設定の分類IDも照合する。同じJSONに含まれない参照は、必要時だけ接続先ワークフローとローカルDBを読み取り専用で確認する。
- 週次：アプリが読み取る週の開始・終了・収集締切と、既存の集計・数値ブロック検証を併用する。集計が古い場合は週次作業の手順で再生成し、考察への影響も確認する。

通常出力は件数と最大10件のエラーコード・行番号に限る。行番号はJSON配列または解析した要約の1始まりの番号。エラー総数は省略しない。本文・URL・入力ハッシュをログへ再出力しない。JSON構文エラー等は例外名だけ返す。

合格は形式とレビュー記録の整合性の確認であり、意味的レビュー・DB反映・ブラウザでの表示確認の代わりにはならない。空の分類提案は保存可能だが、DB反映コマンドの空配列に関する既存条件は変わらない。キーワード考察と表示確認はこの形式検証の対象外で、既存の工程確認を続ける。

## 変更が影響する工程だけを再検証する

新しいcheckpointは `dependencyVersion: 1` と対象工程を記録し、工程ごとに入力ファイルを保存する。明示した `--evidence` と成果物自体は必ず照合する。反映証跡にもレビューの依存情報を引き継ぐ。

| 変更対象 | 再確認の対象 |
| --- | --- |
| 検索語設定 | キーワード、週次、表示。既存の要約・人材・分類レビューには影響させない |
| 分類設定 | 分類、週次、表示 |
| 工程固有の生成指示書 | 該当工程、週次、表示 |
| 構造化記事・本文・共通レビュー記録 | 日次記事作業、週次、表示 |
| 保存した要約 | 要約、人材、分類、週次、表示 |
| 人材提案 | 人材、分類、週次、表示 |
| 明示的に追加した根拠 | その根拠を保存した工程 |

共通レビュー記録のpolicyHashは要約・人材・分類のルールをまとめて参照するため、これらのルール変更は引き続き3工程に影響する。日次のファイル単位で照合し、同一ファイル内の記事単位の変更判定は行わない。実DB、実行履歴、週次数値、表示確認の既存検証も継続する。収集済み結果を検索語変更だけで再送することはない。

`resume` は `dependencyChanges` に失効理由と変更ファイルを最大10件、総数を省略せず返す。診断の `changedFiles` も同じ判定を使う。設定・本文を戻して表示上の問題を隠したり、unknownを未送信として扱ったりしない。

旧証跡は `legacy_exact` と表示し、従来どおり保存した全ファイルを照合する。新しい依存関係による自動的な再承認や履歴の書き換えはしない。次の通常レビュー・checkpointで `scoped_v1` に移行する。既存の反映証跡が失効している場合も、再送せず既存の実行とDBを調査する。新しい依存先が後から追加された場合は `dependency_baseline_missing` として再確認する。

## 履歴に依存しない入口

通常運用は `brief --scope SCOPE` から開始する。同じチャットでも開始手順を揃える。範囲の選択、参考スナップショット、証跡と調査ログの読む順序は [operation-start.md](operation-start.md) を参照。briefは状態照合と情報表示であり、運用を実行しない。

## 本文取得の負荷調整とChrome直接確認

本文取得はホスト別に間隔を空け、既定でGoogle Newsは5秒、出版社は2秒。同時に複数の取得プロセスを起動しない。保存済み本文を再利用し、同一プロセスの同じリクエストは直近16件まで再利用する。明示的なrefresh以外で確認済み本文を取り直さない。

429ではRetry-After（秒数・HTTP日時）を尊重し、間隔を倍増して同じURLを再試行する。既定は最大4試行、1回60秒まで待機。長い待機・試行上限ではURLごとのretry_afterとホストの待機期限・調整間隔を保存し、他の記事へ進む。再試行予定のURLは完了チェックポイントに入れない。最後にdeferredByRateLimitとnextRetryAtを返す。期限後に同じコマンドを再実行すると、期限が来たURLを通常のキャッシュ設定のまま再開する。429に対する再開では--refreshや--reset-progressは不要。実行時間内は他工程後に再開し、制限が続く場合は予定を次回へ引き継ぐ。永続状態はcontent/article-body-captures/rate-limit-state.json。429の発生ゼロや取得成功を保証するものではない。

Windowsの表示確認はPowerShellから以下を第一手段として実行する。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify_dashboard_chrome.ps1
```

-Urlsで対象のローカル記事・週次URL、-OutputDirectoryでプロジェクト内の証跡保存先、-ChromePathでChrome実行ファイルを指定できる。独立した一時プロファイルで描画後DOMとPNGを保存する。通常のブラウザプロファイルには接続しない。出力ファイルの存在は表示内容の検証ではない。DOM・スクリーンショットで日付、データソース、要約・分類・週次表示を確認してからpageのcheckpointを記録する。一時プロファイルと起動ログは出力のtemporaryLogsに残る。
