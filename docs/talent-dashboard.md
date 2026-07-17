# Talent Index Dashboard

`talents`、`articles`、`article_talents` のn8n Data Tableを閲覧するための、ローカル専用の読み取り専用アプリケーションです。タレント候補、収集記事、複数タレントと記事の関係、検出根拠をブラウザから確認できます。

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

このアプリケーションはデータを変更しません。タレント候補の承認、検索有効化、記事とタレントの登録は、既存のn8nワークフローと `docs/talent-article-index.md` の手順で行います。

## 記事要約

記事の詳細画面では、`content/article-summaries/YYYY-MM-DD.md` の `Source-by-source Notes` にあるAI要約を、MarkdownリンクのURLで取得記事に関連付けて表示します。要約がない記事は「要約未作成」として表示します。

次回以降に作成するAI要約指示書は、取得記事を省略せず、各記事を同じURLへのMarkdownリンク付きで1項目ずつ要約するよう更新しました。このワークフロー定義をn8nへ同期した後の新規要約から、より広く記事単位で表示されます。
