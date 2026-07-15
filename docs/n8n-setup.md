# n8n Setup

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
4. `Build Search RSS URLs` creates Google News RSS and Hatena Bookmark RSS search URLs for each keyword.
5. `Read RSS Search Results` reads matching RSS items.
6. `Normalize and Deduplicate Articles` filters recent items and removes duplicates.
7. `Build Daily Digest` creates the source-based digest and LLM prompt.
8. `Write Markdown Files` writes the digest and keyword candidates under `content/`.
9. `Summarize Saved Markdown Files` returns the saved file paths as the webhook response.

## Default keywords

The default keywords are configured in the `Default Daily Summary Request` Code node:

```js
keywords: [
  'Vtuber',
  'にじさんじ',
  'VOLTACTION',
  'うおむすめ',
  'エデン組',
  'りぷらい',
  'VTuberオーディション'
]
```

Edit that node in n8n to change the scheduled daily keywords.

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
  "maxArticles": 30,
  "locale": "ja-JP",
  "language": "ja",
  "country": "JP",
  "outputLanguage": "ja"
}
```

After activation, use the production webhook path:

```text
http://localhost:5678/webhook/daily-keyword-summary/request
```


## When n8n shows `No output step`

If an RSS source returns no matching items, n8n may show `No output step` around
the RSS node. The workflow now enables `alwaysOutputData` on `Read RSS Search
Results`, so later nodes should still run and return a digest with
`articleCount: 0` when no items are found.

If `articleCount` is still 0, try these changes in `Default Daily Summary Request`:

```js
lookbackHours: 72,
maxArticles: 50
```

The workflow searches both Google News and Hatena Bookmark for each keyword.

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

Each successful run writes four files:

```text
content/daily-digests/YYYY-MM-DD.md
content/keyword-candidates/YYYY-MM-DD.md
content/ai-extraction-instructions/YYYY-MM-DD.md
content/ai-summary-instructions/YYYY-MM-DD.md
```

The first file contains the captured article list, source URLs, digest metadata,
and LLM prompt. The second file extracts candidate follow-up keywords from the
saved digest with deterministic rules. The third file is a prompt/instruction
Markdown file for manual semantic keyword extraction. The fourth file is a
prompt/instruction Markdown file for manual article summarization. The workflow
does not call AI APIs automatically.

For npm/WSL usage, the workflow defaults to this project path:

```text
/home/raimu/N8N/AutoArticleFor-n8n
```

For Docker usage, `docker-compose.yml` mounts the project at `/project` and sets
`PROJECT_ROOT=/project`.

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