# Structured Records

This directory stores the machine-readable archive used for analysis.

- Creator: the `Build Structured Records` branch of the n8n workflow, or the backfill script for historical files
- File name: `YYYY-MM-DD.jsonl`
- Format: one JSON object per line
- Contents: one `run` record followed by one `article` record for each article saved by the workflow

The workflow overwrites the file for a date when it is run again on the same date. Use these files as the input for weekly reports rather than parsing Markdown output.

To create records from existing digest Markdown files, run:

```bash
python3 scripts/backfill_structured_records.py
```