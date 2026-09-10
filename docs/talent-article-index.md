# Talent/article index

Shared source facts now come from
[the common review record](ai-rules/shared-article-review.md).
Use current `ready` entity facts/evidence and reconcile them with the current
talent registry. A `held` relation stays unresolved; all source articles still
remain registration candidates. Missing or incomplete facts require source
review (`--include-body` forces it) and a shared-record update.
This does not grant approval, enable search, or replace human feedback.

The daily workflow does not infer talents or update Data Tables. It reads the
existing `talents` table and adds only `approved` records whose
`search_enabled` value is true to the default search request, then writes an AI instruction file under
`content/ai-talent-index-instructions/`.

## AI review flow

1. Read the generated instruction and its `rulesPath`,
   [ai-rules/talent-index.md](ai-rules/talent-index.md).
2. Read the referenced structured records with the input reader below, passing
   the returned top-level `nextOffset` as `--offset` until `nextOffset` is `null`.
   Consult saved bodies, daily summaries, and existing
   talent rows as needed to verify evidence and preserve existing state.
3. Create a review Markdown file and a JSON proposal under
   `content/talent-index-proposals/`.
4. Verify every proposed relation against its article title, excerpt, or URL.
5. Send only the reviewed JSON to the active `Apply Talent Index Proposal`
   workflow at `POST /webhook/talent-index/apply`.

```bash
python3 scripts/read_ai_inputs.py --run-date YYYY-MM-DD --task talent-index --offset 0 --limit 20
```

Version 2 instructions contain paths and task metadata instead of embedding
article lists and fixed rules. The reusable rules retain the existing proposal
version 1 JSON contract (`articles`, `talents`, `articleTalents`) and the handling
of pending names and uncertain relationships. All articles remain registration
candidates, even when no talent relation can be established. The reader is
read-only; it does not register articles, approve talents, or apply proposals.

External AI chats need the referenced rules and input data attached with the
instruction. Existing proposal files and archived instructions remain usable.

The apply workflow performs schema and reference validation, then upserts the
proposal into the following n8n Data Tables. It contains no extraction logic.

## Tables

`talents` is the registry. Use `pending`, `approved`, or `rejected` in
`status`. A display name becomes a default daily search keyword only when its
status is `approved` and `search_enabled` is true. `aliases_json` stores a JSON
array of literal aliases.

`articles` stores one row per article URL/key. Its system `createdAt` is the
first insert time and `last_seen_at` is refreshed when a reviewed proposal
includes it.

`article_talents` is a many-to-many relation table. A separate row is written
for every article/talent pair, so one article can be linked to multiple
talents. Every relation must include evidence and confidence.

## Safety rules

- Do not automatically approve or search-enable a discovered name.
- Official-roster synchronization may approve a name backed by a configured
  official source, but it keeps individual-name search disabled by default.
- Do not delete rows or overwrite an approved record without explicit review.
- Do not infer official names, aliases, or organizations absent from the
  source evidence.
- Keep all relation evidence in `evidence_text`.

The initial backfill is recorded in
`content/talent-index-proposals/2026-07-16.{md,json}`. It contains 109 unique
articles, 21 pending candidates, and 22 reviewed relations. The table IDs in
the apply workflow are the IDs created in this local n8n instance; initialize
new n8n environments before importing that apply workflow.
