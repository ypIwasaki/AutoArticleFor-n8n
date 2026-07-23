# AI Summary Instructions

The workflow does not call OpenAI or any other AI API automatically.

Instead, each workflow run writes a prompt/instruction Markdown file that can be
copied into an AI chat manually:

```text
content/ai-summary-instructions/YYYY-MM-DD.md
```

That file requires the AI to open each captured URL, follow redirects to the
original article, and summarize the verified article body. The saved Digest and
captured source list are discovery aids, not substitutes for article text. It includes:

- Current search keywords.
- The target time window and article count.
- Summarization policy and source-citation rules.
- A requested Markdown output format.
- Structured article data.
- The full saved daily digest as input context.

The instruction tells the AI to save the created summary as a Markdown file in
the project when file access is available:

```text
content/article-summaries/YYYY-MM-DD.md
```

The source digest is saved separately:

```text
content/daily-digests/YYYY-MM-DD.md
```


## Body verification

The AI must mark every source note as body-verified or body-unavailable. A body-unavailable item must state the reason and must not present a title or RSS excerpt paraphrase as an article summary. Executive and topic-level summaries may cite only body-verified items.
