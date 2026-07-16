# Keyword Management

## Manual keywords

Edit `config/keywords.json` to manage keywords that should always be searched.
The workflow reads this file at the beginning of every run, so a saved change is
used by the next scheduled, manual, or keyword-unspecified webhook run. Running
`scripts/sync_workflow_to_n8n.py` is not required after editing the file.

```json
{
  "manualKeywords": ["Vtuber", "にじさんじ", "追加したい語句"],
  "excludedKeywords": ["自動追加しない語句"],
  "maxAutoKeywords": 30
}
```

Keep the file valid JSON: use double quotes and do not add a trailing comma.

## Automatic keywords

`Promote Extracted Keywords` stores automatically promoted terms in n8n workflow
static data. They are combined with `manualKeywords` on the next run.
`excludedKeywords` prevents automatic promotion, but never removes a term from
`manualKeywords`.

To remove an automatically promoted term, open the workflow execution/static
data in n8n and remove it from `autoKeywords`. Manual terms should be edited
only in `config/keywords.json`.

## Webhook override

A webhook request with `keywords` explicitly supplied uses only those terms for
that one request. Omitting `keywords` uses the combined manual and automatic
keyword lists.
