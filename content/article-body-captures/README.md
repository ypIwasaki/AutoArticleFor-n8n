# Article Body Captures

リンク先から取得した本文テキストと、動画・SNSで取得できた公開メタデータを保存します。

- 日付別の JSONL: YYYY-MM-DD.jsonl
- 再開用の取得状態: backfill-state.json
- n8n Data Table: article_contents

本文は正規化して最大 100,000 文字まで保存します。通常記事は本文、動画・SNSは公開されているタイトル・投稿者・概要だけを保存します。本文が短い場合も partial として保存し、取得不能は理由を残します。

実行コマンド:

python3 scripts/capture_article_contents.py --retry-unverified

Google News は最小 2.5 秒、配信元ページは最小 0.75 秒の間隔で取得します。429 などは待機後に再試行します。既存の本文も保存し直す場合は --refresh、日次要約も再作成する場合は --refresh --write を使います。

N8N_API_KEY が設定された .env があれば、結果は article_contents Data Table にも article_key をキーとして upsert されます。
