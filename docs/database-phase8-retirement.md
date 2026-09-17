# フェーズ8: 旧書き込み停止・必要時出力

業務の読み書きは専用DB。通常処理は旧Data Tablesと機械用JSON／JSONLへ自動反映しない。Markdownなどの人向け出力は専用DB保存後に生成する。既存の旧データ・中断証跡・バックアップは保持する。

## 保存・完了の契約

008 `legacy_delivery_policy` は、停止設定 `compatibility_policy` と出力ごとの不変な `compatibility_delivery_schedule` を追加する。001～007は変更しない。

- `automatic`: フェーズ7の互換反映。既存007の出力もこの扱い。
- `on-demand`: Data Table・JSON・JSONLの出力要求をDBへ保存するが、自動反映しない。Markdownは従来どおり自動出力する。
- `maintenance`: 新規業務保存を拒否する。明示的な切戻し時だけ使う。

業務行、元行、変更履歴、出力内容・参照、出力方針は同じトランザクションで保存する。成功結果の `compatibility: on-demand` は「DB保存と必要な人向け出力が完了、旧側出力は要求時のみ」を表す。未送信の旧側出力は `pending` のまま保持し、配信済みとは記録しない。

`pendingDeliveries` は自動反映の未完了数、`deferredDeliveries` は必要時出力の未送信数。同じIDの同じ要求と完了済みresumeは元の結果を返す。別内容のID再使用は拒否する。自動出力の失敗は従来どおり成功扱いにしない。

同じファイル・旧テーブルキーへの後続出力は、先行する不変の出力要求へ参照を持つ。切戻しではこの順序で照合して反映する。古いファイルのハッシュを全更新へ流用して、後続更新を上書きすることはない。本文キャッシュは元行の固定rowid上限から必要時に生成する。

## 日常の入口と読み取り

本文取得、共通レビュー、要約、提案反映、評価の入口は [フェーズ7の保存手順](database-phase7-writes.md) と同じ。旧ファイルを直接編集して取り込む運用へ戻さない。

本文取得は収集記事、本文キャッシュ、ホスト別待機状態を専用DBから読む。旧Data Tableの事前接続・直接upsertを行わず、保存要求だけをDBへ記録する。新しい取得でも部分取得・取得不能・再試行予定・同一性保留を自動格上げしない。

日次収集は保存後に専用DBから収集結果を返し、それをMarkdown生成へ渡す。JSONLバイナリの毎日生成を止めた。完了検証は保存済み実行・DB内の収集行・生成Markdownを照合する。人材・分類の反映後照合も専用DBを使用する。

機械資料の存在確認、checkpoint、変更検知は `source_records` の現行版を使う。提案JSONが旧フォルダーにないことを理由に、新しいDB内の提案を無視しない。既存の日次収集184は実行記録と不変の旧成果物を再検証してDB指紋へ移行した。新規収集や再送ではない。

運用計測JSON、操作状態、設定JSON、著述用の入力、週次の分析出力は「廃止する旧業務キャッシュ」と区別する。これらを自動削除しない。

## 状態確認・明示的な出力

プロジェクトルートで実行する。出力先はDドライブの新しいディレクトリにする。

```bash
python3 scripts/project_legacy_retirement.py status
python3 scripts/project_write_control.py status --operation-id OPERATION_ID
python3 scripts/project_legacy_retirement.py export --operation-id OPERATION_ID --directory .operation-state/database/exports/NEW_NAME
python3 scripts/project_legacy_retirement.py report --day YYYY-MM-DD --output .operation-state/database/exports/NEW_NAME/capture-report.md
```

`export` は保存済み出力を別フォルダーへ作り、SHA-256台帳を付ける。本文キャッシュ、出典、取得状態を保持する。旧側への配信済み状態は変更しない。既存出力先への上書きは拒否する。`report` はDB内の本文取得結果から人向けMarkdownを作る。本文抜粋は確認用で、レビューや要約を確定しない。

## 明示的な切戻し

1. n8n、記事処理、DBサービス、書込み可能な確認アプリを止め、実行中・結果不明の要求を確認する。未完了業務は同じIDと保存要求で確認・再開する。現行DBを新しい復旧地点へ保存する。
2. `python3 scripts/project_legacy_retirement.py policy maintenance --writers-stopped`。CLIは停止状態も検査する。
3. 4ワークフローの現行公開版を記録して一時的に非公開にする。その後、旧Data Table APIだけに必要なローカルn8nを起動する。DBサービスと記事処理は停止したままにする。定時・Webhook業務を走らせない。
4. `python3 scripts/project_legacy_retirement.py catch-up`。保存済みの順序で旧ファイル・旧テーブルへ反映し、変更前値を照合する。外部編集があれば停止する。応答が失われた場合も、同じ保存要求の結果を確認してから再開する。
5. `status` で `pendingTotal=0` を確認し、代表的な新規データを旧経路から読み戻す。失敗時はmaintenanceを維持する。
6. n8nを再停止する。フェーズ7へ戻すだけなら `policy automatic --writers-stopped` と次表の旧公開版を復旧し、業務保存先はproject-dbのまま再開する。
7. 業務保存先までlegacyへ戻す場合は、未反映0件の後に [フェーズ7の切戻し手順](database-phase7-writes.md) の3書込み設定・4公開版を戻す。読み取り切戻しはフェーズ6の手順で明示的に実施する。

新しい専用DBを古いバックアップで上書きして、停止後のデータを失う方法は使わない。明示的なcatch-upは例外的な切戻し操作であり、専用DBから旧側への自動書き戻しではない。判断履歴、元行、本文欠落13件と未解決競合を保持する。

| ワークフロー | フェーズ7へ戻す公開版 | フェーズ8公開版 |
|---|---|---|
| UHJ4LW3Oswhpj4zV | 9546ded7-7ab1-49c8-af00-ea843972f61d | d47524a4-208b-4f8f-b2a7-caf499d2f76e |
| wdoTTKnHJ2r4ODlx | abe7f4f3-e1a1-4c39-9f1c-8f4924497135 | 83052478-db8b-48aa-9705-5bfbeb549717 |
| oaE1yyeE7iHlSjXg | 56e76742-a626-48de-a124-5abe29128814 | cddbb5ac-2c8f-47d9-8fe4-e9ecba55104c |
| joeIHOKXPPqBkSS9 | 11eb885d-9947-4ea3-a677-8ac7a5ae495d | 070b7f82-2f0a-41a3-a7a4-e40db7a4e436 |

CLIは既存起動スクリプトと同じホームディレクトリから実行する。プロジェクト直下でn8n CLIを起動すると開発用 `.env` を自動ロードするため、その方法を使わない。インポート時はn8nが実際に発行した版IDと内容を確認してから公開する。認証値は変更・表示しない。

## 復旧地点・保存期間

2026-09-17の利用者指示により、以下に記載したD側の復旧コピー・作業証跡は削除済み。登録済み判断根拠のみプロジェクト内へ移し、D側への参照を解除した。現在の状態は[保存先整理記録](database-storage-cleanup.md)を参照。以下の復旧地点は作業当時の記録であり、現在は利用できない。

停止前の完全復旧地点: `/mnt/d/N8N/backups/phase8-before-20260917T023801Z`。両DB、業務ファイル、設定・認証設定、確定済みフェーズ7実装を保持する。停止後のDB復旧コピーと代表復元結果は検証JSONを参照する。

新規の作業証跡: `/mnt/d/N8N/database/phase8-retirement-20260917`。過去の証跡は変更・削除していない。切戻し・復元の境界動作は小規模DBの代表試験を再利用し、本番を実際にlegacyへ書き戻す試験は行わない。

完了後の成功した日次運用30回は既存のn8n実行、`sync_runs`、`.operation-state/YYYY-MM-DD.json` で経過を確認する。今回の再送・試験・実行184の再検証を加算しない。旧JSON／JSONLは30回保持する。完全バックアップの90日保持は今回の利用者指示で取りやめ、D側のコピーを削除した。30回待つことはフェーズ8の完了条件ではなく、削除は別作業である。

## DB読み取り契約

`project_readers.Reader` の収集・記事・本文・レビュー・要約・提案・分類・人材の参照、`read_ai_inputs.py`、確認アプリが専用DBを読む。元行、出典、本文版、判断履歴、引用根拠はDBへ保持する。必要な互換出力だけを上記コマンドで作る。このフェーズではAIへ渡す情報の削減を実施しない。

検証結果と通常業務の保留事項: [フェーズ8検証JSON](database-phase8-verification.json)、[進捗資料](database-consolidation-progress.md)。
