# Talent/article index

The daily workflow does not infer talents or update Data Tables. It reads the
existing `talents` table only to add names that have already been approved and
search-enabled, then writes an AI instruction file under
`content/ai-talent-index-instructions/`.

## AI review flow

1. Read the generated instruction and its referenced structured records.
2. Create a review Markdown file and a JSON proposal under
   `content/talent-index-proposals/`.
3. Verify every proposed relation against its article title, excerpt, or URL.
4. Send only the reviewed JSON to the active `Apply Talent Index Proposal`
   workflow at `POST /webhook/talent-index/apply`.

The apply workflow performs schema and reference validation, then upserts the
proposal into the following n8n Data Tables. It contains no extraction logic.

## Tables

`talents` is the registry. Use `pending`, `approved`, or `rejected` in
`status`; only an `approved` row with `search_enabled=true` becomes a default
daily search keyword. `aliases_json` stores a JSON array of literal aliases.

`articles` stores one row per article URL/key. Its system `createdAt` is the
first insert time and `last_seen_at` is refreshed when a reviewed proposal
includes it.

`article_talents` is a many-to-many relation table. A separate row is written
for every article/talent pair, so one article can be linked to multiple
talents. Every relation must include evidence and confidence.

## Safety rules

- Do not automatically approve or search-enable a discovered name.
- Do not delete rows or overwrite an approved record without explicit review.
- Do not infer official names, aliases, or organizations absent from the
  source evidence.
- Keep all relation evidence in `evidence_text`.

The initial backfill is recorded in
`content/talent-index-proposals/2026-07-16.{md,json}`. It contains 109 unique
articles, 21 pending candidates, and 22 reviewed relations. The table IDs in
the apply workflow are the IDs created in this local n8n instance; initialize
new n8n environments before importing that apply workflow.