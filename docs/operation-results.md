# 短い運用結果の返し方

通常の起動・収集・反映・状態確認には、短いJSONと再開ガードを備えた
`scripts/autoarticle_ops.py` を使う。コマンドと工程記録は
[定型運用](autoarticle-operations.md) を参照。本書の直接curl例は個別調査用で、
新しい入口が停止した際に再送ガードを迂回するためには使わない。

## 共通ルール

通常の運用では [短い返答ルール](ai-rules/operation-result.md) だけを一度読む。
本書はコマンドの導入・使用方法・障害調査に必要な場合の詳細資料。
最終報告は3〜5行程度にまとめる。これは返答とツール出力の短縮であり、記事確認・成果物・
検証の内容を省略する変更ではない。ユーザーが詳細を求めた場合は必要な説明を行う。

- 返すもの：対象日、完了した範囲、主要件数、未完了/保留/失敗、確認ページまたは成果物へのリンク。
- 省くもの：全コマンドの履歴、全ファイル一覧、本文、候補一覧、長いAPI応答、同じ成功説明の繰り返し。
- 件数は既存の検証済み集計や実行結果から取得する。短い報告のために記事を全件読み直したり、手集計したりしない。
- 未確認は「未確認」、未計測は「未計測」。0件や成功に置き換えない。取得成功・意味的な本文確認・成果物完成・DB反映は別々の状態。
- コマンド終了コード0だけで全工程完了としない。成果物の保存と検証、依頼された反映、ページ起動など、その工程の完了条件を確認する。
- HTTP成功はn8nワークの完了を意味しない。起動確認と対象実行の完了を区別する。ファイルの存在だけでも今回生成された証明にはならない。
- 保留、部分成功、欠損日、採否未反映の暫定集計、DB未反映は隠さない。正常時より長くなっても必要な原因と次の対応を付ける。
- スクリプト/ルールの変更だけを頼まれた場合、収集・再実行・n8n同期・DB更新・プッシュを勝手に追加しない。
- ページはHTTPで確認した状態と実際にブラウザで開いた状態を区別する。未実施の操作を完了として報告しない。
- 作業中は工程の変化や問題を短く伝える。長いログの逐次転記はしない。長時間かかる場合も利用者への必要な経過連絡は省略しない。
- 外部AIなど保存手段がない環境では、成果物本文を返す既存の出力契約を優先する。短い報告だけを返して成果物を欠落させない。

正常終了の例（件数・URLは実際に確認した値に差し替える）：

```text
対象：YYYY-MM-DD。収集・記事作業・検証まで完了しました。
収集：N件／今回の要約完成：M件／保留：K件。
確認ページ：確認して開いたURL
```

部分終了の例：

```text
対象：YYYY-MM-DD。記事作業は完了、週次集計は採否未反映の暫定値です。
未完了：全件採否JSONの取得と、反映後の週次検証。
確認ページ：確認して開いたURL
```

失敗時は「失敗した工程・短い原因・既に完了した範囲・次の対応」を示す。
同じ操作を自動再試行しない。収集や提案反映の再試行前には、
最初の操作が実行済みか確認し、二重実行を防ぐ。

## コマンド結果を短く取得する

有限時間で終了する運用コマンドは、原則として次のラッパーを使う。
対象コマンド自体に既に十分短い出力がある場合や、本文・根拠を読む場合は不要。

```bash
python3 scripts/operation_result.py --step weekly-generate --run-date YYYY-MM-DD --profile weekly -- python3 scripts/generate_analysis_reports.py --through YYYY-MM-DD
python3 scripts/operation_result.py --step weekly-check --run-date YYYY-MM-DD --profile weekly -- python3 scripts/generate_analysis_reports.py --through YYYY-MM-DD --check
```

必要に応じ、内側のコマンドへ `--as-of`、`--require-feedback`、
`--sync-report`、`--check-report` を付ける。対象日は収集日で、
作業した今日の日付ではない。

収集開始が依頼され、対象ワークが未実行であることを確認した後の例：

```bash
python3 scripts/operation_result.py --step collect --run-date YYYY-MM-DD --profile collection --timeout 900 -- curl --fail-with-body --silent --show-error --max-time 850 -X POST -H 'Content-Type: application/json' --data '{}' http://127.0.0.1:5678/webhook/daily-keyword-summary/request
```

`curl` が `--fail-with-body` 非対応なら `--fail` を使う。
このコマンドは実際に収集を開始するので、単なる状態確認のためには使わない。
n8n自体のワークやWebhookの応答スキーマは変更していない。

その他の単発コマンドは `--profile command`（既定）で実行する。
ラッパーは指定コマンドをシェルを介さず一度だけ起動し、終了コードを保持する。
コマンド列は `--` の後に渡す。対話入力、パイプ、シェル展開はしない。

### 短い結果の意味

標準出力は1行のJSON。本文や候補名、任意のAPI応答、実行引数は転載しない。

| フィールド | 意味 |
| --- | --- |
| step / runDate | 操作者が指定した工程名 / 対象日（未指定はnull） |
| processStatus | exited_ok / failed / timed_out / interrupted / launch_failed |
| exitCode | 元コマンドの終了コード。タイムアウト124、中断130、起動失敗127。POSIXシグナル終了はログに負値、CLIは128+シグナル番号 |
| summary.resultStatus | commandはnot_checked。collectionはsaved/not_saved。weeklyはready/provisional。応答不一致はunrecognized |
| summary.counts | 対応する応答に明示された主要件数だけ。本文や長い配列は含めない |
| summary.checked | weeklyの元コマンドが検証モードだったか。生成だけならfalse |
| summary.warningCount | weeklyの警告件数。それ以外や形式不一致はnull（警告0件ではない） |
| attentionRequired | 失敗、標準エラー出力あり、暫定値、形式不一致など、追加確認が必要な状態 |
| verification | 常にcommand_result_only。全工程の検証済み・記事確認済みという意味ではない |
| logs | その実行専用のstdout.log、stderr.log、result.jsonの絶対パス |

`collection` は日次Webhookのsaved/date/articleCount/candidateCount/writtenFilesから
件数を抜き出すだけで、保存ファイルの実在や意味的な確認までは検証しない。
`weekly` は週次CLIの応答を短縮し、暫定状態と警告件数を保持する。
`--run-date` と応答の日付が違えば `unrecognized` とし、古い結果を今回の成功扱いにしない。

応答形式が不明な場合も元の終了コードは変更せず、
`unrecognized` / `not_checked` を返す。これらを成功の根拠にせず、
個別の成果物・検証結果を確認する。`attentionRequired: false` でも、
未知の標準出力内の警告やアプリケーション上の失敗がないと保証するものではない。

## 詳細を読む場面とログ管理

既定のログは `.operation-logs/工程名-一意ID/` に保存し、Git管理しない。
同じ対象日・工程の再実行でも別ディレクトリに保存し、過去の結果を上書きしない。
Linux/WSLでは実行ディレクトリを所有者だけがアクセスできる権限で作成する。
ログには元コマンドが出力した秘密情報や第三者の記事本文が含まれる可能性がある。
公開・コミット・無制限の保存を避け、不要になったログは対象を確認して整理する。

1. 通常は短いJSONだけを読む。
2. 失敗・警告・暫定値・形式不一致の場合だけ、該当ログまたは参照成果物の必要箇所を読む。
3. 自由テキストのログは、まず検索件数と1行の長さを制限する。例えば下記。
4. 1行の巨大JSONはそのまま表示せず、ローカルで必要なフィールドだけ抽出する。
5. 利用者への報告には短い原因を記し、秘密情報や詳細ログ全文を貼らない。
6. 入力リーダーが返す記事本文・根拠は作業用データなので、このラッパーで隠して確認を省略しない。

```bash
rg -n -m 5 --max-columns 240 --max-columns-preview 'warning|error|Error|Exception|Traceback' PATH_TO_LOG
```

既定タイムアウトは3600秒。`--timeout` で有限の秒数を指定する。
タイムアウト後に処理を再開・再試行する機能はない。外部のn8n実行は、
クライアントのタイムアウトや中断だけでは停止したと判断しない。

n8nや確認ページのサーバーをこのラッパーで直接起動しない。
常駐プロセスは既存起動手順でログを別ファイルへ流し、
別の有限なHTTP確認コマンドで疎通を確認する。HTTP確認の例：

```bash
python3 scripts/operation_result.py --step dashboard-health --timeout 15 -- curl --fail --silent --show-error --max-time 10 --output /dev/null http://127.0.0.1:8765/api/health
```

この疎通確認はページをブラウザで開く操作の代わりではない。
WSL/Linuxではタイムアウト・Ctrl+C時にラッパーが作成したプロセスグループを停止する。
Windowsネイティブでは直接の子プロセスだけを停止するため、
子孫プロセスを持つ処理はWSLで実行する。常駐化するコマンドには使わない。

新しく生成される5種類のAI指示書は `resultRulesPath` で短い返答ルールを参照する。
過去の指示書は書き換えない。運用開始時はREADMEから本書を確認する。
報告方法だけの変更で共通本文確認のpolicyHashを無効にしないよう、
記事の意味的な確認ルールとは別ファイルにしている。
