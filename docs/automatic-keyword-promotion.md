# Automatic Keyword Promotion

The daily workflow automatically promotes high-confidence organization and unit names that are found in captured articles. Promoted keywords are stored in n8n workflow static data and are appended to the next scheduled search.

## Promotion timing

1. The workflow searches with the base keywords and previously promoted keywords.
2. It writes the digest and other Markdown outputs.
3. `Promote Extracted Keywords` evaluates the captured articles and adds approved terms to n8n static data.
4. The new terms are used on the next workflow run. They are not added to the search that discovered them.

## Promotion policy

The node only promotes the following high-confidence patterns:

- Known VTuber agencies, companies, groups, and units that appear in an article
- A name immediately following `VTuber事務所` when the title clearly identifies an agency
- A にじさんじ unit name in an explicit anniversary, goods, or live title

It does not promote generic tags, media names, URLs, game titles, `Live2D`, `shorts`, `PR`, or other low-context terms. The automatically promoted list is capped at 30 terms and is append-only.

## Default keywords and webhook behavior

`Default Daily Summary Request` merges the base keyword list with the promoted list. `Normalize Webhook Request` does the same when the request body does not include `keywords`.

A webhook request that explicitly provides `keywords` intentionally overrides both lists.

## Persistence and sync

Promoted keywords are stored in n8n workflow static data, not in the Git-tracked workflow JSON. The API sync script preserves this runtime data and does not send `staticData` during normal synchronization.

Static data is persisted by an active workflow execution. It may not persist when running a workflow only in n8n's manual test mode. Keep the workflow active for the scheduled or production-webhook run that should save promoted terms.

## Observability

The production webhook response includes:

```json
{
  "autoAddedKeywords": ["newly promoted keyword"],
  "autoKeywordCount": 1
}
```

Use the workflow execution data to inspect the existing static data fields `autoKeywords`, `autoKeywordPromotionHistory`, and `lastKeywordPromotion`.