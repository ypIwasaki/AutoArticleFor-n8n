# Keyword Configuration

`keywords.json` は、日次検索に常に含める手動キーワードの正本です。

- `manualKeywords`: 次回以降の定期実行とキーワード未指定のWebhook実行に含める語句
- `excludedKeywords`: 自動抽出した語句を追加しないための除外語。`manualKeywords` は除外されません
- `maxAutoKeywords`: n8nが自動追加できるキーワードの最大数

手動キーワードを追加または削除したら `manualKeywords` を編集してください。JSONを保存した次回の実行から反映され、ワークフローJSONの再同期は不要です。

Webhook本文で `keywords` を明示した実行は、この設定と自動追加キーワードをその実行に限り上書きします。

## Markdown仕分け用の別名

`keyword-aliases.json` は記事レビューMarkdownのフォルダIDと表記揺れを定義します。
同じ対象を表す検索語は、同じ `id` の `aliases` に追加してください。
`kind` と `priority` は複数キーワードが同点になった場合の主キーワード決定に使います。
この設定は記事の参照先を整理するためのもので、`keywords.json` の検索語そのものは変更しません。
