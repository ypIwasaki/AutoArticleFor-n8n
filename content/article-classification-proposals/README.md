# Article Classification Proposals

AIレビュー済みの記事種別・カテゴリ・関連度の提案を保存します。

1. `content/ai-article-classification-instructions/` の対象日ファイルを確認します。
2. 根拠を確認してMarkdownとJSONの提案を作成します。
3. JSONを `POST /webhook/article-classification/apply` に送信します。

未確認の本文や、タイトル・抜粋だけで確信できない分類はJSONに含めません。
