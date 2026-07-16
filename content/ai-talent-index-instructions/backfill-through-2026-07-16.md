# AI Talent Index Backfill Instructions - through 2026-07-16

## Sources

- `content/structured-records/2026-07-14.jsonl`
- `content/structured-records/2026-07-15.jsonl`
- `content/structured-records/2026-07-16.jsonl`

## Task

Register every captured article in the `articles` table. Identify a talent only when the title or excerpt explicitly names the person with strong context, then create a `pending` talent and an `article_talents` relation with evidence.

Do not set `approved` or `search_enabled` to true. Do not infer an official name, organization, or alias that is absent from the sources. Do not delete or modify existing approved records.

## Required outputs

- `content/talent-index-proposals/2026-07-16.md`
- `content/talent-index-proposals/2026-07-16.json`

The JSON must be applied only through the `Apply Talent Index Proposal` workflow after checking article URL, title, and evidence for every relation.
