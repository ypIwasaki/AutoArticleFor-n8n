# AI Article Classification Instructions

日次ワークフローが `content/ai-article-classification-instructions/YYYY-MM-DD.md` を作成します。
この指示書は、保存済み記事について記事種別・主カテゴリ・副カテゴリ・関連度をAIが本文根拠に基づいてレビューするためのものです。

AIは、同日の `content/structured-records/`、必要に応じて記事本文と要約を確認し、次の2ファイルを作成します。

- レビュー用Markdown: `content/article-classification-proposals/YYYY-MM-DD.md`
- 適用用JSON: `content/article-classification-proposals/YYYY-MM-DD.json`

JSONは `Apply Article Classification Proposal` ワークフローへ送信します。日次ワークフロー自体は分類結果を自動適用しません。
