# Article Types and Categories

## Purpose

`article_classifications` stores reviewed classifications for the saved
`articles` table. One article has one article type, one primary category, up to
three secondary categories, and one relevance level. This supports category
ratios and trends without forcing a single topic onto multi-topic articles.

The taxonomy is defined in `config/article-classification-taxonomy.json`.

## Data Table

Create an n8n Data Table named `article_classifications` with these columns.

| Column | Purpose |
| --- | --- |
| `article_key` | Unique key of the existing `articles` row. Use this as the upsert key. |
| `article_type` | One ID from `articleTypes`. |
| `primary_category` | One ID from `categories`. |
| `secondary_categories_json` | JSON array of zero to three category IDs. |
| `relevance` | `in_scope`, `low_relevance`, or `out_of_scope`. |
| `confidence` | Number from `0` to `1`. |
| `evidence_text` | Short factual evidence from the article. |
| `classification_method` | Normally `ai_review`. |
| `classified_at` | ISO-8601 timestamp. |

Set `N8N_ARTICLE_CLASSIFICATIONS_TABLE_ID` to the created table ID in `.env`,
then restart n8n with `scripts/start_n8n_with_file_access.sh` before activating
the apply workflow. The startup script reads only this ID from `.env`, so the
exported workflow JSON does not contain a local table ID.

## AI Review

The daily workflow writes an instruction file under
`content/ai-article-classification-instructions/`. The reviewer must verify the
article body when possible and save both files below.

- `content/article-classification-proposals/YYYY-MM-DD.md`
- `content/article-classification-proposals/YYYY-MM-DD.json`

The JSON payload is:

```json
{
  "proposalVersion": 1,
  "proposalDate": "2026-07-23",
  "classifications": [
    {
      "article_url": "https://example.com/article",
      "article_type": "news_article",
      "primary_category": "event",
      "secondary_categories_json": ["live_or_music", "product_or_goods"],
      "relevance": "in_scope",
      "confidence": 0.9,
      "evidence_text": "記事本文でイベント名、開催日、出演者を確認した。",
      "classification_method": "ai_review",
      "classified_at": "2026-07-23T00:00:00Z"
    }
  ]
}
```

Do not include a row when the article cannot be classified with adequate
evidence. The apply workflow resolves each article URL to its existing article key, then
validates taxonomy IDs, duplicate articles, secondary-category limits, and
confidence before it upserts a row.

## Apply Workflow

1. Create the Data Table and set `N8N_ARTICLE_CLASSIFICATIONS_TABLE_ID`.
2. Import `n8n/workflows/apply-article-classification-proposal.workflow.json`.
3. Sync it when updating an existing n8n workflow:

   ```bash
   python3 scripts/sync_workflow_to_n8n.py \
     --workflow-file n8n/workflows/apply-article-classification-proposal.workflow.json \
     --workflow-name "Apply Article Classification Proposal"
   ```

4. Send a reviewed proposal to `POST /webhook/article-classification/apply`.

The dashboard reads the table by name when it is present. Until then it uses
the proposal JSON files as a read-only fallback.
