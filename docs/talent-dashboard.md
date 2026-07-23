# Talent Index Dashboard

`talents`、`articles`、`article_talents`、`article_classifications` のn8n Data Tableを閲覧し、日次検索キーワードを管理するローカルアプリケーションです。ダッシュボードではタレント候補、収集記事、複数タレントと記事の関係、検出根拠を確認できます。キーワードページでは手動設定・n8n自動設定・タレント登録由来の検索語を確認できます。

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

既定では `~/.n8n/database.sqlite` のn8n Data TableをSQLiteの読み取り専用モードで読み込みます。n8nを別のユーザーフォルダで起動している場合は、起動時に同じDBを指定してください。

```bash
N8N_DATABASE_PATH=/path/to/.n8n/database.sqlite bash scripts/start_talent_dashboard.sh
```

n8nのDBまたは対象テーブルを読み込めない場合、`content/talent-index-proposals/*.json` のレビュー済み提案ファイルを代替データとして表示します。画面上部のデータソース表示で、現在どちらを表示しているか確認できます。

## 操作範囲

このアプリケーションはタレント・記事・関係のData Tableを変更しません。キーワードページでは、`config/keywords.json` の手動設定とn8nの`autoKeywords`を編集できます。`タレント登録`はData Tableから動的に作られる読み取り専用の検索語です。タレント候補の承認、記事とタレントの登録は、既存のn8nワークフローと `docs/talent-article-index.md` の手順で行います。

## 記事要約

記事の詳細画面では、article-summaries内のSource-by-source Notesにある本文確認済みのAI要約だけを、MarkdownリンクのURLで取得記事に関連付けて表示します。本文確認: 確認済みがない旧要約や本文未確認項目は表示せず、要約未作成として扱います。

次回以降に作成するAI要約指示書は、取得記事を省略せず、各記事を同じURLへのMarkdownリンク付きで1項目ずつ要約するよう更新しました。このワークフロー定義をn8nへ同期した後の新規要約から、より広く記事単位で表示されます。

## 詳細画面

タレント詳細では、公式レジストリと一致した場合に公式プロフィール・公式名簿へのリンク、所属グループ、関連記事数、要約・分類の網羅状況、主カテゴリを表示します。関連記事では要約を行単位で展開できます。

記事詳細では、本文確認済み要約、記事分類、元記事URL、関連タレントとの紐づけ根拠を確認できます。同じタレントに紐づく直近記事も最大12件表示します。公式プロフィールは `content/official-talent-registry/` の最新スナップショットから読み込むため、Data Tableに追加のプロフィール列は必要ありません。
