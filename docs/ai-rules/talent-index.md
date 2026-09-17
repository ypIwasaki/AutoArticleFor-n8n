# タレント索引の固定ルール

記事とタレントの関係を根拠付きでレビューし、日次指示書のMarkdown・JSON出力先へ提案を保存する。日次収集処理だけでは提案を適用しない。

## 共通確認記録

[共通の本文確認ルール](shared-article-review.md)の inputVersion 2 を使う。保存済み工程は本文・根拠・ルールが変わっても再確認しない。未完了の state: ready は工程別 facts / entities / evidence を利用し、needs_review は保存本文を確認する。excluded の saved / held / unavailable は生成対象に戻さない。保留は関連する判断材料の追加時だけ再開する。

## 入力と判断

1. このルールを読み、プロジェクトルートで `python3 scripts/read_ai_inputs.py --run-date YYYY-MM-DD --task talent-index --offset 0 --limit 20` を実行する。返されたトップレベルの `nextOffset` を `--offset` に指定し、`nextOffset` が `null` になるまで全記事を確認する。必要に応じて同日のDaily Digest・記事要約・本文、既存の `talents` テーブルを参照する。
2. タレント本人と判断できる名前だけを抽出する。URL・タイトル・本文抜粋にない人物名、根拠を確認できない所属・別名を推測で追加しない。
3. 全記事を `articles` の登録候補に含める。1記事に複数人の根拠がある場合は全員を個別に関連付ける。
4. 新規タレントは `status: pending`、`search_enabled: false` とし、AIだけで承認・検索有効に変更しない。既存の承認状態・`aliases_json`・検索設定は明確な更新根拠がない限り変更・削除しない。
5. 関係にはURLまたは記事タイトルを含む `evidence_text`、`matched_fields`、`confidence`、`detection_method` を付ける。低確信度ではタレント・関係を提案せず、Markdownで保留と理由を記録する。
6. 本文取得状態は保持し、部分取得・公開メタデータを本文確認済みとしない。長文の続きが必要なら `--article-ref REF --reference-kind body --content-offset N --max-content-chars N` で取得する。同一URLの入力行が複数ある場合は、そのURL内の `--offset` で対象行を選ぶ。

## 出力契約

レビューMarkdownには対象日、件数、候補の名前・所属・承認状態・検索設定・根拠、記事との関係、保留項目を記載する。既存の `New Talent Candidates`、`Article Relationships`、`Held Items` 見出しを利用できる。

JSONの外形は次のとおり。3項目は常に配列とし、削除操作を含めない。

```json
{
  "proposalVersion": 1,
  "proposalDate": "YYYY-MM-DD",
  "articles": [],
  "talents": [],
  "articleTalents": []
}
```

各配列の行は次のフィールドを持つ。

| 配列 | フィールド |
| --- | --- |
| `articles` | `article_key`, `url`, `title`, `excerpt`, `source`, `published_at`, `last_seen_at` |
| `talents` | `talent_id`, `display_name`, `organization`, `aliases_json`, `status`, `search_enabled`, `auto_discovered`, `last_seen_at` |
| `articleTalents` | `relation_key`, `article_key`, `talent_id`, `matched_aliases_json`, `matched_fields`, `evidence_text`, `confidence`, `detection_method`, `last_seen_at` |

- 入力URLを保持し、既存行と同じ対象には既存の安定したキー・IDを使う。
- `aliases_json` と `matched_aliases_json` はJSON配列を文字列化した値（例: `"[\"別名\"]"`）。`matched_fields` は `title, excerpt` のような文字列。
- `search_enabled` と `auto_discovered` は真偽値。新規のAIレビュー候補の `auto_discovered` は `false`。`confidence` は0–1、`detection_method` は `ai_review`、日時はISO-8601。
- 関係の `article_key` と `talent_id` は同じJSON内の行または既存テーブルの行を参照する。
- 記事0件なら空配列を保持する。生成スクリプトの出力は下書きとして根拠をレビューする。

JSONと参照関係の確認後、反映工程では専用の `Apply Talent Index Proposal` ワークフローへ渡す。テーブル仕様は [talent-article-index.md](../talent-article-index.md) を参照する。

## DB保存と再開（inputVersion 2）

入力・追加参照・保存の方法は共通ルールに従う。正式なURLや内部IDを推測しない。保存済み提案で後続反映を再開し、反映失敗でAI判断をやり直さない。保存中に対象数が減った場合は一覧offsetを0に戻し、保存前のnextOffsetを流用しない。
