# AI Summary Instructions

## Shared verification before repeated body reading

Follow [the shared review rules](ai-rules/shared-article-review.md) as well as
the summary rules. The first reviewer (usually this task) saves grounded facts,
entities, evidence ranges and per-task readiness with
`scripts/save_article_review_facts.py`. Review the full relevant source, not
only the short summary. Unfinished talent/classification checks remain
`needs_review`; saving a summary does not complete those checks automatically.

For `sharedReview.status: current`, use `ready` facts for the output or preserve
the `held` reason. The reader omits the body for those states. Missing, stale,
invalid or incomplete reviews return the body. Use `--include-body` to inspect
or reconsider any cached review. Saved outputs keep their existing format.

The workflow does not call OpenAI or any other AI API automatically.

Instead, each workflow run writes a compact task instruction:

```text
content/ai-summary-instructions/YYYY-MM-DD.md
```

Version 2 separates the task from reusable rules and article data:

- The instruction records `instructionVersion: 2`, the target date, `rulesPath`,
  and input/output paths.
- [ai-rules/article-summary.md](ai-rules/article-summary.md) defines body
  verification, citations, and the required Markdown structure.
- `content/structured-records/YYYY-MM-DD.jsonl` contains the run metadata
  (search keywords, period, article count) and article records.
- `content/article-body-captures/YYYY-MM-DD.jsonl` contains saved bodies and
  capture status. The existing `article_contents` Data Table may also be consulted.
- The Daily Digest remains a separate discovery aid, not a substitute for
  verified article text. It is not embedded in the instruction.

Read the instruction and fixed rules first. From the project root, obtain a
bounded batch of article inputs:

```bash
python3 scripts/read_ai_inputs.py --run-date YYYY-MM-DD --task article-summary --offset 0 --limit 20
```

Pass the returned top-level `nextOffset` as `--offset` until `nextOffset` is
`null`, so that all articles have been considered. The first batch supplies
shared run metadata; later batches omit repeated metadata.
Capture status, missing inputs, total count, and continuation information are
reported explicitly. `totalArticles`, `returnedArticles`, and `nextOffset` describe
article pagination; an initial `context` includes run metadata and source
references. To continue one long body, keep the date and task and add:

```bash
--article-url 'ARTICLE_URL' --content-offset N --max-content-chars N --limit 1
```

Use `content.nextOffset`, not an estimated position. `content.complete` describes
the reader's saved-text slice, not the success of original body capture. Truncated
output is not proof that the original body is missing. Read the continuation or
referenced source before making a decision that needs it. The reader is read-only;
body capture and review remain separate operations.
If a URL has multiple input rows, `--offset` selects a row within that URL's
matches; it is separate from the body character offset.

The instruction points to the existing Markdown output location:

```text
content/article-summaries/YYYY-MM-DD.md
```

The source digest is saved separately:

```text
content/daily-digests/YYYY-MM-DD.md
```

## Body verification

The AI must mark every source note as body-verified or body-unavailable. A body-unavailable item must state the reason and must not present a title or RSS excerpt paraphrase as an article summary. Executive and topic-level summaries may cite only body-verified items.

`partial` and `metadata_only` captures must retain those distinctions; neither
means an ordinary article's full body was verified. A summary includes a status
for every article, including unavailable articles and a zero-article run.
`not_captured` means no capture row exists; `unavailable` means capture was
attempted and failed. The reader does not perform missing captures itself.

## External AI chats and older files

Codex can follow repository-relative paths in version 2 instructions. For an
external AI chat, attach the referenced fixed rules and relevant input data as
well as the instruction. A compact instruction alone no longer contains the
source material. If the AI cannot edit files, ask it to return the Markdown to
save at the specified path. Archived instructions and existing JSONL/output
formats are not rewritten by this change.
