# 記事確認Markdownの運用

取得記事の本文を確認し、検索キーワードとの関連性と記事の採否を人が判断するためのローカルレビュー機能です。
生成物には配信元の記事本文が含まれ得るため、`content/article-review/` はGit管理対象外です。

## 設計

1記事につき、編集可能な正本Markdownを `articles/<article-key>.md` に1件だけ作成します。
同じ記事が複数のキーワードに一致した場合、各 `by-keyword/<keyword-id>/` には正本への参照Markdownを置きます。本文やレビュー欄を複製しないため、キーワード間で判定が食い違いません。

```text
content/article-review/YYYY-MM-DD/
  index.md
  articles/
    <article-key>.md                 # 本文と唯一のレビュー欄
  by-keyword/
    <keyword-id>/
      index.md
      <article-key>.md               # 正本への自動生成参照
  _multiple-keywords.md              # 2語以上に一致
  _ambiguous.md                      # 主キーワードの評価値が同点
  _unmatched.md                      # 一致なし
  partial.md                         # 本文を部分取得
  unavailable.md                     # 取得不能
```

## キーワード重複と同点の扱い

`config/keyword-aliases.json` が表記揺れの正本です。たとえば `Vtuber` と `VTuber`、`ぶいすぽ` と `ぶいすぽっ！` は同じキーワードIDへ統合され、同じフォルダに入ります。未登録語は正規化した文字列のハッシュから安定したIDを作ります。

記事は一致したすべてのキーワードフォルダに参照されます。主キーワードは次の根拠を加点して決定します。

- 記事タイトル
- 本文見出し
- 取得元の検索クエリ（n8nが保存した由来情報）
- 本文
- RSSタイトル・概要
- 記事概要

ナビゲーション、ヘッダー、サイドバー、フッター内の一致は記録しますが、関連性スコアには加えません。同点の場合は、キーワード種別（タレント、ユニット、組織、一般語）と設定上の優先度で解決し、それでも同じなら全候補を主キーワードとして `_ambiguous.md` に出します。どのケースでも判定欄は正本1か所だけです。

## 初回設定

リポジトリの変更をn8nへ同期します。

```bash
python3 scripts/sync_workflow_to_n8n.py \
  --workflow-file n8n/workflows/daily-keyword-news-summary.workflow.json \
  --workflow-name "Daily Keyword News Summary"
python3 scripts/sync_workflow_to_n8n.py \
  --workflow-file n8n/workflows/apply-article-feedback.workflow.json \
  --workflow-name "Apply Article Feedback" --create-if-missing
```

日次ワークフローを同期した後の新しい収集結果には、URLがどの検索語・RSSから得られたかが保存されます。古い `structured-records` には由来情報がないため、Markdown生成時にタイトル・概要・本文から推定します。

## 生成手順

対象日の本文を取得します。

```bash
python3 scripts/capture_article_contents.py --run-date YYYY-MM-DD --retry-unverified
```

本文の見出し、段落、箇条書き、引用、リンク、画像説明、表セルを可能な範囲でMarkdownに保持します。`article` または `main` 要素を優先し、JSON-LDの `articleBody` も補助情報として使用します。取得量は `full`、`substantial`、`partial`、`metadata_only`、`unavailable` のいずれかで記録されます。

続いてレビューMarkdownを生成します。

```bash
python3 scripts/generate_article_review_markdown.py --date YYYY-MM-DD --clean-generated
```

`--clean-generated` はキーワード別の自動生成参照だけを作り直します。`articles/` の正本は削除せず、既に記入したレビュー欄を維持します。

## 中断と再開

全期間を日付順に処理する場合は、履歴ランナーを使用します。

```bash
python3 -u scripts/run_article_review_history.py --max-retries 1
```

安全に中断する場合は別のターミナルから次を実行します。

```bash
python3 scripts/run_article_review_history.py --stop
```

再開時は `--resume` を付けます。

```bash
python3 -u scripts/run_article_review_history.py --resume --max-retries 1
```

再開時には次の2段階で処理済みデータを除外します。

- `article-review-backfill-status.json` の `completedDates` にある日付をスキップ
- `article-review-backfill-progress.json` にある処理済みURLを、中断した日付と後続日からスキップ

記事の取得結果が `unavailable` や `partial` でも、そのバックフィルで1度試行済みなら再開時には再試行しません。再取得が必要な記事はMarkdownで `needs_refetch` を指定し、通常の再取得キューを使用してください。

状態ファイルは処理のたびに一時ファイルから置換して保存するため、URL取得中に終了しても直前に完了した記事まで保持されます。中断した瞬間に通信中だった1記事だけは、次回再試行されます。

## レビュー欄

正本Markdown末尾の次のブロックだけを編集します。

```yaml
decision: pending
keyword_check: pending
content_check: pending
reason_code: ""
note: ""
reviewed_at: ""
```

`decision`:

- `pending`: 未判定。反映しない
- `approved`: 採用
- `rejected`: 不採用。`reason_code` が必須
- `needs_refetch`: 再取得キューへ出力

`keyword_check` と `content_check`:

- `pending`
- `correct`
- `incorrect`
- `uncertain`

`reason_code`:

- `irrelevant`: 対象テーマと無関係
- `suspicious_source`: 配信元を除外候補にする
- `unavailable`: 内容を確認できない
- `outdated`: 古すぎる、または再掲

## 一括反映

最初にドライランします。1件でも入力エラーがあれば、何も反映しません。

```bash
python3 scripts/apply_article_markdown_reviews.py --date YYYY-MM-DD
```

件数と対象を確認した後、n8nの `article_feedback` に反映します。

```bash
python3 scripts/apply_article_markdown_reviews.py --date YYYY-MM-DD --apply
```

`approved` と `rejected` だけがWebhookへ送信されます。`pending` は無視され、`needs_refetch` は `content/article-refetch-requests/YYYY-MM-DD.jsonl` に出力されます。

再取得は次のように実行します。

```bash
python3 scripts/capture_article_contents.py \
  --refetch-file content/article-refetch-requests/YYYY-MM-DD.jsonl
python3 scripts/generate_article_review_markdown.py --date YYYY-MM-DD --clean-generated
```

反映結果は `content/article-review-results/YYYY-MM-DD.json` に保存されます。

## 制約

- JavaScript描画後にだけ本文が現れるページ、ログイン必須ページ、bot対策ページは完全に取得できない場合があります。
- 動画・SNSは原則として公開メタデータのみです。
- 抽出本文は原文確認用であり、自動生成要約を事実の正本にはしません。
- 記事内容の転載・共有ではなく、ローカルでの確認用途を想定しています。
