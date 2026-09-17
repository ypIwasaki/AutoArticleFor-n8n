# Talent Index Dashboard

`talents`、`articles`、`article_talents`、`article_classifications` のn8n Data Tableを閲覧し、日次検索キーワードを管理するローカルアプリケーションです。ダッシュボードではタレント候補、収集記事、複数タレントと記事の関係、検出根拠を確認できます。キーワードページでは手動設定・n8n自動設定・タレント登録由来の検索語を確認できます。

## コードの責務

- `apps/talent-dashboard/server.py`: HTTPの受付、画面用データの組立て、キーワード管理。
- `scripts/talent_dashboard_data.py`: 保存先設定に従うデータ取得。専用DBの読取り失敗は旧ファイルへ暗黙に切り替えない。
- `scripts/article_feedback_service.py`: 記事評価の入力検証、Webhook送信、評価指示書とスナップショットの保存。
- 指示書生成と日次レビューのCLIは共通モジュールを直接利用し、Webサーバーをインポートしない。

共通処理にはプロジェクトルートを明示的に渡す。別のルートを指定した場合、そのルートのDB・出力先を使い、実プロジェクトのDBへ書き込まない。

## 起動

WSLのプロジェクトルートで次を実行します。

```bash
bash scripts/start_talent_dashboard.sh
```

ブラウザで `http://127.0.0.1:8765` を開きます。停止するには、起動した端末で `Ctrl+C` を押します。

ポートを変更する場合は、次のように実行します。

```bash
TALENT_DASHBOARD_PORT=8766 bash scripts/start_talent_dashboard.sh
```

## データの読み込み

現在の読取り先は専用プロジェクトDBです。旧経路を明示的に選択した場合のみ、既定で `~/.n8n/database.sqlite` のn8n Data Tableを読み取り専用で参照します。旧経路のn8n保存先は次の設定で指定できます。

```bash
N8N_DATABASE_PATH=/path/to/.n8n/database.sqlite bash scripts/start_talent_dashboard.sh
```

旧経路でn8nのDBまたは対象テーブルを読み込めない場合のみ、`content/talent-index-proposals/*.json` のレビュー済み提案ファイルを代替データとして表示します。画面上部のデータソース表示で、現在どちらを表示しているか確認できます。

## 操作範囲

このアプリケーションはタレント・記事・関係のData Tableを変更しません。キーワードページでは、`config/keywords.json` の手動設定とn8nの`autoKeywords`を編集できます。`タレント登録`はData Tableから動的に作られる読み取り専用の検索語です。タレント候補の承認、記事とタレントの登録は、既存のn8nワークフローと `docs/talent-article-index.md` の手順で行います。

## 記事要約

記事の詳細画面では、article-summaries内のSource-by-source Notesにある本文確認済みのAI要約だけを、MarkdownリンクのURLで取得記事に関連付けて表示します。本文確認: 確認済みがない旧要約や本文未確認項目は表示せず、要約未作成として扱います。

次回以降に作成するAI要約指示書は、取得記事を省略せず、各記事を同じURLへのMarkdownリンク付きで1項目ずつ要約するよう更新しました。このワークフロー定義をn8nへ同期した後の新規要約から、より広く記事単位で表示されます。

## 詳細画面

タレント詳細では、公式レジストリと一致した場合に公式プロフィール・公式名簿へのリンク、所属グループ、関連記事数、要約・分類の網羅状況、主カテゴリを表示します。関連記事では要約を行単位で展開できます。

記事詳細では、本文確認済み要約、記事分類、元記事URL、関連タレントとの紐づけ根拠を確認できます。同じタレントに紐づく直近記事も最大12件表示します。公式プロフィールは `content/official-talent-registry/` の最新スナップショットから読み込むため、Data Tableに追加のプロフィール列は必要ありません。

## 記事評価の保存

記事詳細の「記事評価」で可・不可を選び、不可の場合は理由を指定して「評価を保存」します。この操作はn8nの評価Webhookへ送信し、article_feedbackへ反映します。記事・人材の登録操作とは別です。レビュー専用アプリのMarkdown保存・一括反映とは混同しないでください。

統合した操作手順は [HTML利用マニュアル](user-manual.html) を参照してください。

## 開発時の確認

WSLのプロジェクトルートから、関連する隔離テストを実行できます。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts:tests python3 -m unittest test_talent_dashboard_services test_artifact_validation test_weekly_metrics -q
```

2026-09-17の責務分離では44件が成功。変更前後の画面JSON 6例と評価指示書6例も、保存・通信を伴わない比較で一致を確認しました。実サービスへの配備・再起動はこの変更に含みません。

### 保存先の隔離修正と復旧記録

分離前の既存テストは、指示書の出力ルートを一時フォルダーへ変えても、DBの保存処理には実プロジェクトの既定値を使っていました。今回の変更では、DB経路の確認と保存の両方へ明示したルート・DBパスを渡します。一時ルートにDBがない場合はそのルート内のファイル出力だけを行います。

修正前テストにより、2026-09-10の空の評価指示書が実DBに保存され、Markdownが出力されました。元のJSONとMarkdownのハッシュが要求に記録された変更前ハッシュに一致することを確認し、元の16件の評価内容を復旧しました。

- 誤生成要求: `db-feedback-document-f3602081d99cb3a07c99bf60633ff2911af24ed2980d3d7868b30dbbd0c47989`
- 復旧要求: `restore-feedback-test-20260917-f3602081`
- Markdownは変更前とバイト単位で一致。旧JSONファイルは変更していません。
- DBの指示書は元の内容に`recovery`メタデータを付けて再保存。元の履歴と誤生成要求も保持しています。
- 必要時出力の要求には誤生成分と後続の復旧分が残ります。既存の出力順序に従い、誤生成要求だけを選んで業務入力へ取り込まないでください。
