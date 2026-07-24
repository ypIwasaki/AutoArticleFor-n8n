# Article Feedback Instructions

このフォルダには、記事詳細で保存された可・不可評価を基に、収集・本文確認・要約・分類を行うAIへ渡す補助指示書を保存します。

- 作成者: Talent Index ダッシュボード、または python3 scripts/generate_article_feedback_instructions.py
- ファイル名: YYYY-MM-DD.md
- 入力: n8n Data Table article_feedback と articles
- 主な内容: 可・不可の件数、不可理由別の判断ルール、代表記事、媒体・URL単位の注意事項

記事評価を保存すると、当日分のファイルが最新の内容で更新されます。AIは対象作業前に、このフォルダの最新日付ファイルを確認します。

この指示書は補助情報です。理由が「ページ削除・取得不能」または「情報が古すぎる」の場合は該当URLだけを除外し、媒体全体や新しい記事へ不用意に拡大適用しません。
