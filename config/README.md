# Keyword Configuration

`keywords.json` は、日次検索に常に含める手動キーワードの正本です。

- `manualKeywords`: 次回以降の定期実行とキーワード未指定のWebhook実行に含める語句
- `excludedKeywords`: 自動抽出した語句を追加しないための除外語。`manualKeywords` は除外されません
- `maxAutoKeywords`: n8nが自動追加できるキーワードの最大数

手動キーワードを追加または削除したら `manualKeywords` を編集してください。JSONを保存した次回の実行から反映され、ワークフローJSONの再同期は不要です。

Webhook本文で `keywords` を明示した実行は、この設定と自動追加キーワードをその実行に限り上書きします。
