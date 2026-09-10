# 記事分類の固定ルール

記事種別・カテゴリ・対象関連度を本文根拠付きでレビューし、日次指示書のMarkdown・JSON出力先へ提案を保存する。日次収集処理だけでは分類を適用しない。

## 共通確認記録

[共通の本文確認ルール](shared-article-review.md)を先に読む。入力の `sharedReview` がcurrent/readyなら本文に基づく確認済み事実を現在の分類体系へ当てはめる。current/heldは理由をMarkdownへ残し、JSONへ推測を追加しない。記録なし・失効・不正・needs_reviewなら本文を確認して共通記録を更新する。

分類体系の変更時は事実を再利用しつつ分類判断をやり直す。根拠不足・矛盾があれば `--include-body` で原文へ戻る。共通記録に分類IDや承認状態を事実として保存しない。

## 入力と判断

1. このルールと分類の正本 [article-classification-taxonomy.json](../../config/article-classification-taxonomy.json) を読む。ID・説明・制約は正本を参照し、固定ルール内に分類一覧を複製しない。
2. プロジェクトルートで `python3 scripts/read_ai_inputs.py --run-date YYYY-MM-DD --task article-classification --offset 0 --limit 20` を実行する。返されたトップレベルの `nextOffset` を `--offset` に指定し、`nextOffset` が `null` になるまで全記事を確認する。必要時は同日のDaily Digest・要約・タレント索引提案を参照する。
3. 各URLの有効な共通記録の本文根拠、または本文・公式ページを確認してから分類する。保存済み本文を利用できるが、タイトル・RSS抜粋だけで分類しない。部分取得・公開メタデータを本文確認済みとしない。
4. 本文の続きが必要なら `--article-url URL --content-offset N --max-content-chars N --limit 1` で取得する。同一URLの入力行が複数ある場合は、そのURL内の `--offset` で対象行を選ぶ。根拠を本文で確認できない記事、不確実な記事はJSONの提案から外し、推測で補完しない。
5. 記事種別1つ、主カテゴリ1つを選び、副カテゴリは必要な場合のみ最大3つ、主カテゴリと重複させない。
6. VTuber・所属・タレント・イベント・配信・関連ゲームなど調査対象と直接関係する記事を `in_scope`、単なる検索ノイズを `out_of_scope` とする。関連度の定義は分類正本に従う。
7. `evidence_text` には分類理由となる本文上の事実を短く書く。URLやタイトルをそのまま根拠文にしない。

## 出力契約

レビューMarkdownには対象日、分類した記事と分類・関連度・確信度・根拠、未確認・保留の理由を記録する。JSONには十分な根拠のある行だけを含め、全記事を確認したうえで保留を区別する。

```json
{
  "proposalVersion": 1,
  "proposalDate": "YYYY-MM-DD",
  "classifications": [
    {
      "article_url": "https://example.com/article",
      "article_type": "news_article",
      "primary_category": "event",
      "secondary_categories_json": ["live_or_music"],
      "relevance": "in_scope",
      "confidence": 0.9,
      "evidence_text": "本文でイベント名、開催日、出演者を確認した。",
      "classification_method": "ai_review",
      "classified_at": "ISO-8601"
    }
  ]
}
```

`article_url` は入力のURLをそのまま使う。`secondary_categories_json` は配列、`confidence` は0–1。根拠を満たす記事が0件なら `classifications: []` とする。生成スクリプトの `ai_review` 表記だけでレビュー済みとは判断しない。

反映工程では記事が先に登録されていることを確認する。専用ワークフローが入力URLを既存の記事キーへ解決し、ID・重複・副カテゴリ・確信度を検証する。詳細は [article-classifications.md](../article-classifications.md) を参照する。
