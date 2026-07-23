# Analysis Workflow

The n8n workflow writes a daily JSONL archive to `content/structured-records/YYYY-MM-DD.jsonl` in parallel with its Markdown outputs. The file contains one run record and one article record per captured article, which makes historical analysis independent of the presentation-oriented Markdown files.

## First-time setup

Create JSONL archives for existing daily digests:

```bash
python3 scripts/backfill_structured_records.py
```

Existing JSONL files are left unchanged. Use `--force` only when you intentionally want to rebuild them from the Markdown digests.

## Generate reports

Generate reports for the latest week with saved records:

```bash
python3 scripts/generate_analysis_reports.py
```

Generate a specific ISO week:

```bash
python3 scripts/generate_analysis_reports.py --week 2026-W29
```

The script writes:

```text
content/analysis/weekly-reports/weekly-trends-YYYY-Www.md
content/analysis/keyword-quality/keyword-quality-YYYY-Www.md
```

## Included analyses

- Daily article volume and weekly deduplicated volume
- AIレビュー済みの提案JSONを用いた記事種別・主カテゴリ・関連度の件数と比率
- ルールベースの補助話題シグナル（商品、イベント、オーディション、配信、制作技術、ゲーム攻略）
- Source distribution and noise candidates
- Literal coverage of the active search keywords in saved titles and excerpts
- Aggregated manual AI keyword-candidate decisions

## Limitations

The archive stores RSS titles, excerpts, URLs, and timestamps. It does not collect full article text, audience metrics, or social engagement. Reviewed category metrics are available only for articles present in `content/article-classification-proposals/`; unclassified articles are excluded from those ratios. The reports therefore support monitoring and query-quality decisions, not sentiment, popularity, or detailed factual analysis.