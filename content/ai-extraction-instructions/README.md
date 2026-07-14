# AI Keyword Extraction Instructions

このフォルダには、日次ダイジェストから意味的に有用な検索キーワード候補をAIへ依頼するための指示書を保存します。

- 作成者: n8nワークフロー
- ファイル名: `YYYY-MM-DD.md`
- 入力: 同日付の `../daily-digests/YYYY-MM-DD.md` と `../keyword-candidates/YYYY-MM-DD.md`
- 主な内容: 抽出・除外の基準、出力形式、ルールベース候補、根拠として使う記事一覧

この指示書自体はAIを自動実行しません。指示書に従って作成した候補は、同日付の `../ai-keyword-candidates/YYYY-MM-DD.md` に保存します。
