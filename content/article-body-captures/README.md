# Article Body Captures

`backfill-state.json` は、保存済み記事を本文確認済みの要約へ再作成するときの取得状態を保持します。

- Google News の中継 URL は、配信元の URL を復元してから確認します。
- 記事本文そのものは保存しません。確認日時、配信元 URL、本文テキスト量、本文要点、本文ハッシュだけを保存します。
- 本文を十分に取得できない場合は未確認として理由を保存し、タイトルや RSS 抜粋から要約を作りません。

実行コマンド:

```bash
python3 scripts/backfill_article_summaries.py --write
```

途中で停止しても `backfill-state.json` から再開できます。未確認の記事も再試行する場合は `--retry-unverified` を追加します。
