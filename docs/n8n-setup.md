# n8n Setup

## Shared review facts

The three review tasks now follow
[shared verification rules](ai-rules/shared-article-review.md) and reuse
validated records under `content/article-review-facts/`.
The first reviewer records source-grounded facts, not the RSS capture node.
The input reader and `save_article_review_facts.py` do not start workflows or
write Data Tables. This extension uses the existing version 2 instruction
paths and updated local task rules; it requires no new n8n node. If the earlier
version 2 workflow change has not yet been synced, that sync is still required.

## Local runtime options

Use the npm install you already completed. To allow Markdown output under this project, start n8n with:

```bash
./scripts/start_n8n_with_file_access.sh
```

Or use Docker:

```bash
cp .env.example .env
docker compose up -d
```

n8n will be available at:

```text
http://localhost:5678
```

## Import the daily keyword summary workflow

In the n8n UI:

1. Open `Workflows`.
2. Select `Import from File`.
3. Choose `n8n/workflows/daily-keyword-news-summary.workflow.json`.
4. Save the workflow.

If you prefer the CLI:

```bash
n8n import:workflow --input=./n8n/workflows/daily-keyword-news-summary.workflow.json
```

## What the workflow does

The workflow is designed for daily keyword monitoring.

1. `Daily Schedule` runs every day at 08:00 Asia/Tokyo after the workflow is activated.
2. `Manual Trigger` lets you test the default keyword set from the n8n editor.
3. `Keyword Summary Webhook` lets another system pass keywords at runtime.
4. `Read Keyword Configuration` loads `config/keywords.json`, and `Parse Keyword Configuration` parses it.
5. `Load Talent Registry` reads registered talent names from the existing n8n `talents` Data Table.
6. `Build Keyword Summary Request` combines manual keywords, previously promoted terms, and names of approved registered talents whose individual-name search is enabled.
7. `Build Search RSS URLs` creates Google News and Hatena Bookmark RSS search URLs for each keyword, and adds one PANORA new-items RSS request filtered by the configured keywords.
8. `Read RSS Search Results` reads matching RSS items.
9. `Normalize and Deduplicate Articles` filters recent items and removes duplicates.
10. `Build Daily Digest` creates the source-based digest and LLM prompt.
11. `Build Markdown Files` creates the digest, keyword candidates, and compact version 2 instructions for keyword extraction, article summaries, talent-index updates, article classifications, and weekly reports.
12. `Write Markdown Files` writes these files under `content/`. The daily workflow does not automatically extract talent data or apply article classifications.
13. `Summarize Saved Markdown Files` returns the saved file paths as the webhook response.

## Default keywords

Edit `config/keywords.json` to change the default keywords. The next workflow
run reads the saved file directly; do not edit a Code node or resync the
workflow after changing only this configuration.

```json
{
  "manualKeywords": ["Vtuber", "にじさんじ", "追加したい語句"],
  "excludedKeywords": ["自動追加しない語句"],
  "maxAutoKeywords": 30
}
```

`manualKeywords` is always included in the default search. `excludedKeywords`
only blocks automatic promotion and removes matching previously promoted terms
from the default search; it does not remove a manual keyword. See
`docs/keyword-management.md` for full behavior.

## Test the webhook

In n8n, open the imported workflow and click `Execute workflow`.

Send a POST request to:

```text
http://localhost:5678/webhook-test/daily-keyword-summary/request
```

Example body:

```json
{
  "keywords": ["Vtuber", "にじさんじ"],
  "lookbackHours": 24,
  "locale": "ja-JP",
  "language": "ja",
  "country": "JP",
  "outputLanguage": "ja"
}
```

`maxArticles` を省略すると、期間内で重複除去後に取得できた全記事を保存し、要約指示書の対象にします。必要な場合だけ正の整数を指定して件数を制限できます。

After activation, use the production webhook path:

```text
http://localhost:5678/webhook/daily-keyword-summary/request
```


## When n8n shows `No output step`

If an RSS source returns no matching items, n8n may show `No output step` around
the RSS node. The workflow now enables `alwaysOutputData` on `Read RSS Search
Results`, so later nodes should still run and return a digest with
`articleCount: 0` when no items are found.

If `articleCount` is still 0, increase the lookback window in `Default Daily Summary Request`:

```js
lookbackHours: 72
```

The workflow searches Google News and Hatena Bookmark for each keyword. It also fetches PANORA once per run and stores only PANORA entries whose title or excerpt matches a configured keyword.

## File access setting

n8n restricts local file access for file nodes. This workflow writes generated
Markdown under `content/`, so n8n must be started with:

```bash
N8N_RESTRICT_FILE_ACCESS_TO=/home/raimu/N8N/AutoArticleFor-n8n/content
```

The easiest npm/WSL start command is:

```bash
./scripts/start_n8n_with_file_access.sh
```

For Docker, `docker-compose.yml` sets `N8N_RESTRICT_FILE_ACCESS_TO=/project/content`.

## Markdown output

Each successful run writes these Markdown files. The weekly instruction is
named by the week's Monday in JST and updated through the current run date.

```text
content/daily-digests/YYYY-MM-DD.md
content/keyword-candidates/YYYY-MM-DD.md
content/ai-extraction-instructions/YYYY-MM-DD.md
content/ai-summary-instructions/YYYY-MM-DD.md
content/ai-talent-index-instructions/YYYY-MM-DD.md
content/ai-article-classification-instructions/YYYY-MM-DD.md
content/ai-weekly-report-instructions/WEEK_START.md
```

The first file contains the captured article list, source URLs, digest metadata,
and LLM prompt. The second file extracts candidate follow-up keywords from the
saved digest with deterministic rules. The remaining files contain task metadata
and references to fixed rules, source files, and output paths. They do not embed
article lists, the full digest, or the search keyword list. Structured article
JSONL is written separately as described below. The workflow does not call AI
APIs automatically.

For npm/WSL usage, the workflow defaults to this project path:

```text
/home/raimu/N8N/AutoArticleFor-n8n
```

For Docker usage, `docker-compose.yml` mounts the project at `/project` and sets
`PROJECT_ROOT=/project`.

## Version 2 AI instruction files

Read the generated instruction's `instructionVersion: 2` metadata and `rulesPath`
before reading the article data. Repository-relative paths resolve from the
project root. Fixed policies are stored in:

| Task | Rules |
| --- | --- |
| `article-summary` | `docs/ai-rules/article-summary.md` |
| `keyword-extraction` | `docs/ai-rules/keyword-extraction.md` |
| `talent-index` | `docs/ai-rules/talent-index.md` |
| `article-classification` | `docs/ai-rules/article-classification.md` |
| `weekly-report` | `docs/ai-rules/weekly-report.md` |

Use the read-only reader to obtain relevant article fields and saved body
status without loading every embedded copy of the input:

```bash
python3 scripts/read_ai_inputs.py --run-date YYYY-MM-DD --task article-summary --offset 0 --limit 20
```

Pass the returned top-level `nextOffset` as `--offset` until `nextOffset` is
`null`, completing the article list. Shared run metadata and
input references are included on the first batch; long body slices have their
own `content.nextOffset`. Keyword and weekly views omit body text; switch to
the summary task with the article's run date and URL when it is needed.
See `docs/ai-summary-instructions.md` for continuation and capture-state handling.

For weekly reporting, first run `generate_analysis_reports.py --through SOURCE_RUN_DATE`.
The default `weekly-report` reader returns validated compact metrics rather than
article pages; request `--weekly-articles` only when raw metadata is needed.
Weekly instructions reference `content/analysis/weekly-metrics/{isoWeek}.json`.
Full feedback is exported as dated JSON alongside the example-based Markdown
by the dashboard or `generate_article_feedback_instructions.py`. Missing or
incomplete feedback keeps weekly totals provisional. See `docs/analysis.md`
for evaluation cutoffs, report synchronization, and read-only validation.

Keep the rules, reader, and workflow definition together when moving this
project to another PC. External AI chats need the referenced fixed rules and
relevant input data attached; a compact instruction alone is insufficient.
Version 2 preserves existing instruction output paths, article JSONL, digest,
body capture, and proposal formats. Past generated files are not rewritten.

All five instructions also reference `resultRulesPath`:
`docs/ai-rules/operation-result.md`. This shared, short policy limits routine
completion replies while preserving failures, holds and provisional results.
For bulky command output, use `scripts/operation_result.py` as documented in
`docs/operation-results.md`; the n8n webhook response itself is unchanged.

### Integrated instruction metadata and commands

The current generator keeps `instructionVersion: 2` and adds reference fields;
existing output paths and older instruction files remain compatible.

| Instruction | Input mode and handoff |
| --- | --- |
| Summary / talent index / classification | `inputMode: shared-review-first`; `sharedReviewRulesPath`, `sourceSharedReviewsPattern`, `sharedReviewOutputPath`, and `sharedReviewWriter` identify the shared review contract |
| Keyword extraction | `inputMode: article-metadata`; uses its existing input reader and candidate references, without claiming to consume shared review facts |
| Weekly report | `inputMode: weekly-metrics`; `sourceMetricsPath` identifies the actual ISO-week JSON, in addition to the existing pattern |

Daily instructions explicitly carry `sourceRunDate`. The shared review pattern
can include earlier dates when the reader validates reuse; it is not an instruction
to read all historical review files. The usual order is summary/shared review,
talent and classification reuse, keyword work, then weekly reporting. A standalone
task can create missing shared facts under its own rules; absent facts do not
authorize skipping review or labelling other tasks ready.

Weekly instructions include `sourceClassificationsJsonPattern`,
`sourceFeedbackSnapshotPattern`, and `reviewAsOf` (default: `coveredThrough`).
They provide two separate command blocks:

1. Generate metrics through the short-result wrapper, then read the compact metrics directly.
2. After the AI saves its interpretation, synchronize the numeric block and verify it through the wrapper.

All four commands share the same collection/evaluation cutoffs. For later
evaluations, update `reviewAsOf` and every `--as-of` together. Optional raw
`--weekly-articles` reads are separate and do not accept `--as-of`.
Do not wrap the input reader: its output is the evidence the AI needs to read.
Final-use commands require `--require-feedback`; otherwise missing feedback is
explicitly provisional. Approval, search activation and DB application remain
separate authorized operations.

After validating repository changes, use the existing API sync procedure to
update the deployed n8n workflow. Editing the JSON on disk alone does not change
an already imported workflow. The next run after sync generates version 2
instructions; a new collection run is not needed to test the generator against
saved data.

## Adding AI summarization

The workflow does not call any AI API by default. To add a polished article
summary later, connect the `llmPrompt` output from `Build Daily Digest` to one
of these nodes:

- OpenAI node
- Anthropic node
- Google Gemini node
- HTTP Request node calling your preferred LLM API

Recommended prompt rule: summarize only the provided source list, and cite the
source title or URL for each important claim.

## Delivery options

After the summary is generated, add one of these output nodes:

- Gmail or Send Email for a daily email digest.
- Slack, Discord, or Teams for team notification.
- Notion, Google Docs, or Google Sheets for archival.
- WordPress or CMS API for publication after review.

## Notes and limitations

Google News RSS and Hatena Bookmark RSS are useful for a no-credential starter workflow, but they are not a
complete web search API. For more control, replace the RSS branch with a paid or
self-hosted search provider such as NewsAPI, SerpAPI, Brave Search API, Tavily,
Exa, or SearXNG.

For production, add a history store if the same article should not appear in
multiple daily summaries. A simple option is Google Sheets or a database table
keyed by article URL.

## Development workflow

Keep exported workflow JSON files under `n8n/workflows/`.

Recommended process:

1. Edit and test workflows in the n8n UI.
2. Export the workflow as JSON.
3. Replace the matching file under `n8n/workflows/`.
4. Review the diff before committing.

Do not commit n8n credentials, SQLite databases, execution logs, or `.env` files.

## Sync repository changes to n8n

To update an existing n8n workflow through the n8n API instead of importing from
the UI, use:

```bash
python3 scripts/sync_workflow_to_n8n.py
```

See `docs/n8n-api-sync.md` for API key and workflow ID setup.

## Analysis archive

The workflow also writes `content/structured-records/YYYY-MM-DD.jsonl` through the `Build Structured Records` and `Write Structured Records` nodes. This JSONL file contains one run record and the captured article records for that day.

Use `python3 scripts/generate_analysis_reports.py` to create weekly reports from the archive. See `docs/analysis.md` for the backfill command, output locations, and limitations.
## Automatic keyword promotion

After the Markdown files are written, `Promote Extracted Keywords` adds high-confidence agency and unit names to n8n workflow static data. The new terms are included in the next scheduled run and in keyword-unspecified production webhook requests.

See `docs/automatic-keyword-promotion.md` for the promotion policy and persistence behavior.
