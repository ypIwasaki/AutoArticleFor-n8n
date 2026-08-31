# Article Review Viewer

`content/article-review/` のMarkdownをブラウザで確認し、記事ごとのレビュー欄を編集するローカルアプリです。
外部サーバーへ記事本文を送信せず、`127.0.0.1` だけで動作します。

## 起動

プロジェクトルートで次を実行します。

```bash
python3 scripts/article_review_viewer.py
```

ブラウザで [http://127.0.0.1:8765/](http://127.0.0.1:8765/) を開きます。
別のポートを使う場合は `--port` を指定します。

```bash
python3 scripts/article_review_viewer.py --port 8877
```

停止するには起動したターミナルで `Ctrl+C` を押します。

## 画面

- 日付、判定、キーワード、取得状態、文字列で記事を絞り込み
- 抽出Markdownを見出し、リスト、リンク、表として安全に表示
- 元記事を中央ペイン内に埋め込み表示し、抽出Markdownと切り替え
- YouTube記事は埋め込み動画と保存済みのタイトル・チャンネル・概要を分けて表示
- 記事の採否、キーワード取得、記事内容、不可理由、メモを編集
- 「保存して次へ」で連続レビュー
- 日付ごとの評価進捗を表示

初期状態では未評価の記事だけを表示します。「判定」を「すべて」に変えると評価済みの記事も確認できます。

「元記事」タブが初期表示です。配信元がiframe表示を禁止している場合は画面内に表示できないため、
埋め込み欄の「外部で開く」を使用してください。

元記事または解決済みURLがYouTubeの場合は、プライバシー強化モードの動画プレイヤーを16:9で表示します。
タイトル、チャンネル、配信元、概要は動画の下に別表示します。このテキストにはレビュー用Markdownへ
保存済みの内容を使用し、動画情報の表示だけを目的としたYouTube APIへの追加アクセスは行いません。

## 保存内容

保存すると、対象の `content/article-review/YYYY-MM-DD/articles/<article-key>.md` にある
`article-review:start` から `article-review:end` までのレビュー欄だけを更新します。
本文、URL、キーワード判定根拠などは変更しません。

不可を選んだ場合は、次のいずれかの理由が必須です。

- キーワードと無関係
- 配信元を除外候補にする
- 内容を確認できない
- 古い・再掲記事

保存は一時ファイルからの置換で行います。画面を開いた後に同じMarkdownが別の場所で変更された場合は、上書きせず再読み込みを求めます。

## キーボード操作

- `J` / `K`: 次 / 前の記事
- `A`: 可を選択
- `R`: 不可を選択
- `Ctrl+Enter` または `Command+Enter`: 保存して次へ
- `/`: 検索欄へ移動

## n8nへの反映

ビューアの保存だけではn8nへ反映されません。レビュー後にまずドライランします。

```bash
python3 scripts/apply_article_markdown_reviews.py --date YYYY-MM-DD
```

内容を確認した後、反映します。

```bash
python3 scripts/apply_article_markdown_reviews.py --date YYYY-MM-DD --apply
```
