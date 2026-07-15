# Analysis Reports

This directory stores Markdown reports generated from `../structured-records/`.

- `weekly-reports/`: weekly volume, topic, source, and follow-up keyword trends
- `keyword-quality/`: query coverage, noise signals, source quality, and AI candidate review

Generate the latest available week with:

```bash
python3 scripts/generate_analysis_reports.py
```