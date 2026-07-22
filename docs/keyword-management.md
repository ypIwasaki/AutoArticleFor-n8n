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

## Talent-derived keywords

The Daily Keyword News Summary workflow reads the n8n `talents` Data Table at
run time. Every registered `display_name` except a row whose `status` is
`rejected` is added to the default search keywords. This list is not stored in
`autoKeywords`, is not subject to `maxAutoKeywords`, and is refreshed whenever
a talent proposal is applied. New talent records therefore become searchable on
the next scheduled or keyword-unspecified run.

## Webhook override

A webhook request with `keywords` explicitly supplied uses only those terms for
that one request. Omitting `keywords` uses the combined manual, automatic, and talent-derived
keyword lists.


## Dashboard candidates

The local Talent Index dashboard exposes the newest `content/ai-keyword-candidates/YYYY-MM-DD.md` file at `http://127.0.0.1:8765/`.
Candidates marked `Add: yes` can be added from the page. The dashboard accepts only terms from that latest file, then sends the selected term to the active Daily Keyword News Summary workflow. n8n stores the term in `autoKeywords`, so it is used by the next scheduled or keyword-unspecified run. Candidates marked `Add: no` remain review-only.


## Dashboard keyword management

The Talent Index dashboard can display and manage the two editable keyword stores. Use `手動設定` for durable entries in `config/keywords.json`; they are included on the next run. Use `n8n自動設定` for the workflow's `autoKeywords` static data. The page supports adding, editing, and deleting either type. `タレント登録` lists names derived from the n8n `talents` Data Table; it is read-only and automatically refreshed when the registry changes. Removing a keyword affects only its selected editable store, so a duplicate in the other store remains available until it is also removed.
