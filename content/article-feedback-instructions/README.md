# Article Feedback Instructions

このフォルダには、記事詳細で保存された可・不可評価を基に、収集・本文確認・要約・分類を行うAIへ渡す補助指示書を保存します。

- 作成者: Talent Index ダッシュボード、または python3 scripts/generate_article_feedback_instructions.py
- ファイル名: YYYY-MM-DD.md（補助指示）と YYYY-MM-DD.json（全件採否スナップショット）
- 入力: n8n Data Table article_feedback と articles
- 主な内容: 可・不可の件数、不可理由別の判断ルール、代表記事、媒体・URL単位の注意事項

記事評価を保存すると、当日分のファイルが最新の内容で更新されます。AIは対象作業前に、このフォルダの最新日付ファイルを確認します。

Markdownの代表例は全件一覧ではありません。週次集計はJSONの全件データを利用します。
JSONは `schemaVersion`、`snapshotDate`、`generatedAt`、`complete`、
`feedback`、`issues`、対応するMarkdownの `instructionHash` を保持します。
各評価はURL、可否、理由、媒体ドメイン/ラベル、評価日時を含みます。
テーブル未取得・URL未解決・不正な評価等があれば `complete: false` とし、
週次集計では採否未反映の暫定値になります。既存MDから全件を推定して補完しません。

媒体不信だけはドメインまたは媒体ラベルの一致へ除外を広げ、それ以外の不可理由は
該当URLだけに適用します。サブドメインへは自動拡張しません。
週次では評価参照日以前の最新スナップショットを使用するため、後日出力した採否を
過去の収集期間へ反映する場合は集計コマンドの `--as-of` を明示してください。

この指示書は補助情報です。理由が「ページ削除・取得不能」または「情報が古すぎる」の場合は該当URLだけを除外し、媒体全体や新しい記事へ不用意に拡大適用しません。
