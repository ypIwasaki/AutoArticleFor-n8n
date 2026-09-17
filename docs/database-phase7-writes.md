# フェーズ7: DB先行保存と互換反映

現状は **フェーズ7完了（代表パターン受入）**。通常収集184で525件のDB先行保存と互換反映を確認した。業務の最初の保存先はproject-db。フェーズ8は開始していない。

## 保存契約

- 処理IDと要求ハッシュを先に予約する。同じIDの異内容は拒否して監査する。
- 業務行・元行・変更前後の履歴・互換反映要求を専用DBの1トランザクションで保存する。
- DBコミット後、保存済み要求からJSON/JSONL/Markdownまたはn8n Data Table APIへ反映する。
- 互換反映が終わるまで処理を成功扱いにしない。DB保存済み・互換反映未完了を区別する。
- 保存済み本文欠落・旧判断・競合の上書き、記事の自動統合、保留解除は行わない。
- 要約・分類・評価は新しい版を追加する。変更前の可変行は `sync_row_history` に保存する。
- 本文取得ごとに巨大なキャッシュ全体をDBへ複製しない。互換キャッシュは不変の元行と固定したrowid上限から生成する。
- 規則検証は既存の共通本文確認処理を使う。本文や引用根拠をログへ出力しない。

マイグレーション007は `project_write_requests` と `compatibility_deliveries` を追加する。001～006は変更しない。

## 対象と入口

| 対象 | DB先行保存の入口 |
|---|---|
| 収集・記事出現 | 日次ワークフロー → `/v1/write/collection` |
| 人材・別名・記事関係 | 人材提案ワークフロー → `/v1/write/talent` |
| 分類・副カテゴリ | 分類提案ワークフロー → `/v1/write/classification` |
| 記事評価 | 評価ワークフロー → `/v1/write/feedback` |
| 本文・本文版・取得状態・再試行予定 | `capture_article_contents.py` |
| 共通レビュー・工程・事実・人物団体・引用 | `save_article_review_facts.py` |
| 確認済み要約 | `save_project_artifact.py --kind summary` |
| 提案資料・公式台帳・評価資料 | 既存生成コマンド → DB保存 → 互換出力 |

DBサービスは127.0.0.1限定、既存のBearer認証を使用する。日次処理のIDはn8nのworkflow IDとexecution IDに基づく。要求が不明な状態でワークフロー全体を再実行して別IDを作らない。

要約は互換出力先を直接編集せず、Dドライブの作業用領域で作成した内容を明示的に保存する。保存時に対象記事・現行レビュー・本文入力・要約工程readyを検証する。

```bash
python3 scripts/save_project_artifact.py --kind summary --run-date YYYY-MM-DD --input .operation-state/database/authoring/article-summaries/YYYY-MM-DD.md
```

本文取得の `--write` は、この作業用領域に草稿を作成する。草稿の自動採用はしない。旧 `backfill_article_summaries.py` はDB先行設定中は処理を拒否する。

## 再開と結果不明

```bash
python3 scripts/project_write_control.py status
python3 scripts/project_write_control.py status --operation-id OPERATION_ID
python3 scripts/project_write_control.py resume --operation-id OPERATION_ID
```

`resume` はDBコミット済みの互換反映を再開する。コミット前に失敗した場合は、n8n実行記録または元の著述入力に保存された **同じ要求とID** を再送する。同じIDへ異なる内容を送らない。

互換ファイルや旧Data Tableが要求保存時点から変更されていた場合は上書きせず停止する。反映後に応答が失われた場合は、保存予定の値が既に存在することを確認して反映済みとする。

## 切替・切戻し

事前にn8n・記事処理・DBサービスを停止し、実行中・待機中・結果不明の処理がないことを確認する。切替CLIもn8n/DBサービス停止と未反映0件を検査する。

```bash
python3 scripts/project_write_control.py switch --feature n8n-daily --target project-db
python3 scripts/project_write_control.py switch --feature ai-reader --target project-db
python3 scripts/project_write_control.py switch --feature dashboard --target project-db
```

切戻しは、専用DBだけにある新規データを先に `resume` で互換経路へ反映し、その後 `--target legacy` を指定する。旧バックアップを上書き復元して新規データを消す方法は使わない。専用DBの履歴と復旧地点を保持する。読み取りを戻す必要がある場合は、書き込みをlegacyに戻した後でフェーズ6の明示的な読み取り切替を使う。

n8nの書込みを切り戻す場合は、未反映0件を確認してn8n・DBサービスを停止した後、4ワークフローも次の旧公開版へ戻す。書込みフラグだけを戻すと新ワークフローのDB保存要求が拒否されるため、公開版復旧も必要。停止中に公式CLI `n8n publish:workflow --id=ID --versionId=VERSION` を使い、実際の `activeVersionId` を照合してから再起動する。既存の実行記録、専用DB、新しい版は削除しない。

| ワークフローID | 切戻し先の公開版 |
|---|---|
| UHJ4LW3Oswhpj4zV | 7ad8303e-909e-4bdc-a602-85675f71a6db |
| wdoTTKnHJ2r4ODlx | a9659bd8-08c7-4883-8e53-cdcd3daee994 |
| oaE1yyeE7iHlSjXg | b06303e4-97a3-443a-8d4a-5b33835d59c6 |
| joeIHOKXPPqBkSS9 | d937f354-ef6a-4133-bea3-a113a6b1b720 |

現在の復旧地点: `/mnt/d/N8N/backups/phase7-before-20260917T004938Z`。両DB・業務ファイルのアーカイブ・フェーズ6確定実装・ハッシュ台帳・代表ファイルの復元記録を保持する。途中コピーも削除していない。

承認待ちの間は4ワークフローの公開版を切替前へ戻した。利用者の明示承認後に準備済みの版を公開し、収集183を実行したが、RSS取得中のタイムアウトでDB保存前に停止した。未検証の経路が自動実行されないよう、4公開版と3書込み設定を再び切替前へ戻した。準備版と失敗実行は保持している。その後、利用者の再開指示に従ってRSS対策版を配備し、DB先行設定を再有効化した。現在の日次公開版は `9546ded7-7ab1-49c8-af00-ea843972f61d`。配備前の定義と配備・公開版復旧結果: `/mnt/d/N8N/database/phase7-write-cutover-20260917`。

記事評価の本番は従来どおり確認アプリからのみ受け付ける。Gitの `65eb1f2` にあったMarkdownレビュー送信元の追加は本番へ未配備だったため、今回のワークフロー切替には含めていない。

## 完了判定

13種類の小規模パターンと、RSS・限定再開7件、既存運用の回帰を確認済み。失敗実行183を保持したまま正式に再開した184が成功し、525件のDB保存・互換反映、実保存先と代表値、未反映0件を確認した。新規記事の同一性保留、本文欠落13件の保留を維持し、自動統合や保留解除はしていない。

通常業務の成功実績は1回として扱い、同じ業務の再試行を別件に数えない。検証の詳細は [フェーズ7検証JSON](database-phase7-verification.json) に集約する。フェーズ7の残件はない。旧データ削除・旧経路廃止・AI入力最小化は実施しない。


## RSS取得のタイムアウト対策と限定再開

RSSを1件ずつループし、`Fetch One RSS Feed` だけを5秒間隔で最大3回試す。既存rss-parserの1回60秒の制限は有限のまま保持する。成功済みのRSSを同じノード内の再試行で取り直さず、検索元・検索語は明示的に各結果へ添付する。空RSSも次へ進めるが、最終的に取得に失敗したRSSを黙って欠落させない。外部サイトの継続障害は正常収集とは記録しない。

既存実行183のスタックを正規に再開できるよう `Read RSS Search Results` のIDと名前を維持し、このノードを1件ずつのループ入口へ変更した。再開は `loadWorkflow=true` を指定したn8n公式APIを使い、実行履歴の `retryOf` で親子を結び付ける。通常業務としては同じ1件であり、再試行回数を成功実績に加算しない。

利用者が指定失敗の再開を依頼した場合に限り、次を使う。

```bash
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD preflight --kind collect --retry-failed-execution EXECUTION_ID
python3 scripts/autoarticle_ops.py --date YYYY-MM-DD collect --retry-failed-execution EXECUTION_ID
```

対象日・ワークフロー・エラー種別・実行済みノードを検査し、RSS段階での保存前失敗だけを受け付ける。DB保存要求、当日の保存行、互換未反映、既存成果物、無関係な実行がある場合は拒否する。POST前に再試行意図を保存し、応答が不明で子実行も確認できない場合は再送しない。既に成功した子実行があれば照合だけを行う。旧実行と操作履歴を消してガードを回避しない。
