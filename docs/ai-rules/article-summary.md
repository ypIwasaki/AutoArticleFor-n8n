# 記事要約の固定ルール

日次指示書の対象日・入力先・出力先を使い、日本語の要約を作成する。ファイル名は対象日のみとし、編集機能がないAIでは保存するMarkdown本文を出力する。

## 共通確認記録

[共通の本文確認ルール](shared-article-review.md)の inputVersion 2 を使う。保存済み工程は本文・根拠・ルールが変わっても再確認しない。未完了の state: ready は工程別 facts / entities / evidence を利用し、needs_review は保存本文を確認する。excluded の saved / held / unavailable は生成対象に戻さない。保留は関連する判断材料の追加時だけ再開する。

## 入力の読み方

1. このルールを読み、指示書の `sourceStructuredRecords` と `sourceBodyCaptures` を参照する。実行期間・検索キーワード・件数は構造化JSONLの `run` 行を正本とする。
2. プロジェクトルートで `python3 scripts/read_ai_inputs.py --run-date YYYY-MM-DD --task article-summary --offset 0 --limit 20` を実行する。返されたトップレベルの `nextOffset` を `--offset` に指定し、`nextOffset` が `null` になるまで読み進め、全記事の処理状態を確認する。記事0件でもその旨を記録する。
3. 保存済み本文を先に利用する。必要時だけrefから専用DB内の該当本文・根拠を追加参照する。Digest・RSS抜粋は記事発見用の補助情報。
4. 長文の省略は本文未取得と区別する。`--article-ref REF --reference-kind body --content-offset N --max-content-chars N` で続きを読み、必要な根拠が不足したまま要約しない。同一URLの入力行が複数ある場合は、そのURL内の `--offset` で対象行を選ぶ。

## 本文確認と要約

- 各URLの本文、掲載元、公開日、主要な事実を確認する。有効な共通記録の本文確認結果を利用できる。not_captured の初回取得だけを既存取得処理へ回す。unavailable / metadata_only は自動再取得しない。共有する新しい根拠は先に本文キャプチャへ保存し、その入力ハッシュで記録する。
- `verified` は保存済み本文、`partial` は部分取得、`metadata_only` は公開メタデータとして扱う。後二者を本文確認済みとしない。
- `not_captured` は本文キャプチャ行がない状態、`unavailable` は取得を試みたが取得できなかった状態として区別する。
- 確認済みの記事は、誰が・何を・いつ・どのように発表または実施したかを本文に基づき2–4文で記載する。タイトルの言い換えやRSS抜粋だけの通常要約は禁止する。
- ログイン、ペイウォール、削除、robots制限、動画のみなどで本文を確認できない記事は「本文未確認（理由）」とし、推測で補わない。
- Executive SummaryとImportant Topicsには本文確認済み記事だけを使う。公開動画メタデータは取得根拠を明記し、通常記事の本文要約と混同しない。
- 重要なニュース、発表、イベント、コラボ、商品、配信、企業動向を優先する。同じ話題は統合し、関係の薄いゲーム攻略記事などはノイズ候補へ分ける。不確かな内容は断定しない。
- 重要な主張には記事タイトルまたはURLを添える。YouTube・SNS由来であることを明示し、本文で確認できない情報を追加しない。

## 出力契約

指示書の `outputPath` に、次の見出しを持つMarkdownを保存する。

```markdown
# Daily Article Summary

## Executive Summary

重要ポイントを3–5行で記載。

## Important Topics

| Topic | Summary | Sources |
| --- | --- | --- |

## Source-by-source Notes

## Noise or Low-relevance Items

## Items to Monitor Next
```

Source-by-source Notesは全記事を1記事1項目で記録する。タイトルを入力と同じURLへのMarkdownリンクにし、本文確認状態、確認済みの場合の2–4文の要約、関連キーワード、重要度、根拠URLを含める。確認済みなら「本文確認: 確認済み（確認元: ドメイン名）」、未確認なら「本文確認: 未確認（理由）」を必ず記載する。未確認記事は理由を記載し、タイトル・抜粋の言い換えを要約にしない。話題を統合しても記事単位の確認状態を省略しない。

Noise or Low-relevance Itemsには記事と理由、Items to Monitor Nextには今後追う話題・イベント・企業・ユニットなどを記載する。

## DB保存と再開（inputVersion 2）

入力・追加参照・保存の方法は共通ルールに従う。正式なURLや内部IDを推測しない。保存済み提案で後続反映を再開し、反映失敗でAI判断をやり直さない。保存中に対象数が減った場合は一覧offsetを0に戻し、保存前のnextOffsetを流用しない。
