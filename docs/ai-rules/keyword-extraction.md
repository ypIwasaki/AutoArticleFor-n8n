# キーワード抽出の固定ルール

VTuber業界のニュースから、今後の検索精度を上げる候補を意味に基づいて選ぶ。日次指示書の対象日・入力先・出力先を使う。

## 入力と判断

1. このルールを読み、プロジェクトルートで `python3 scripts/read_ai_inputs.py --run-date YYYY-MM-DD --task keyword-extraction --offset 0 --limit 20` を実行する。返されたトップレベルの `nextOffset` を `--offset` に指定し、`nextOffset` が `null` になるまで全記事を確認する。実際の検索キーワード・期間・件数は構造化JSONLの `run` 行を参照する。
2. 記事タイトル・抜粋・URLを候補と根拠の確認に使う。このtaskは本文を返さない。必要時は当該記事の `runDate` で `--task article-summary --include-body --article-url URL` を指定して本文を読み、または指示書のDaily Digestや本文取得ファイルを参照する。本文未確認の情報を確認済みとして記載しない。
3. 指示書の `ruleBasedCandidates` は機械抽出の参考資料として読む。ノイズを含むため、そのまま採用しない。
4. 事務所・グループ・運営会社を特に優先する。本人名、ユニット、イベント、ライブ、コラボ、商品・グッズ、メディア企画、継続して追う価値のある話題も検討する。
5. 一般語・媒体都合の語、URL断片・ID・意味の薄い英数字、1日だけの挨拶タグ・汎用タグ、VTuberと無関係なゲーム攻略語、既存検索語とほぼ同じ語を除外する。
6. `Add` は毎日固定監視する価値が高い場合だけ `yes` とし、採否に理由と記事の根拠を付ける。候補を作成しただけで検索設定へ追加しない。

## 出力契約

指示書の `outputPath` に日本語のMarkdownを保存する。ファイル名は対象日のみ。編集機能がないAIでは保存するMarkdown本文を出力する。以下の見出し・候補表の列を維持する。

````markdown
# AI Keyword Candidates

## Summary

抽出結果を短く説明。

## Candidates

| Candidate | Category | Confidence | Add | Reason | Evidence |
| --- | --- | ---: | --- | --- | --- |

## Suggested Default Keywords

```js
keywords: [
  "追加推奨キーワード"
]
```

## Rejected Terms

- 除外した語: 除外理由
````

`Category` は `vtuber_agency / vtuber_group / company / talent / event / collaboration / product_or_goods / platform_or_media / topic / other`、`Confidence` は0.00–1.00、`Add` は `yes/no` とする。Evidenceには記事タイトル・抜粋・URLなど根拠を記載する。候補0件ならその旨を記載する。
