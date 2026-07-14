# AI Summary Instructions

The workflow does not call OpenAI or any other AI API automatically.

Instead, each workflow run writes a prompt/instruction Markdown file that can be
copied into an AI chat manually:

```text
content/ai-summary-instructions/YYYY-MM-DD.md
```

That file asks the AI to summarize the fetched articles using only the saved
Digest and captured source list. It includes:

- Current search keywords.
- The target time window and article count.
- Summarization policy and source-citation rules.
- A requested Markdown output format.
- Structured article data.
- The full saved daily digest as input context.

The source digest is saved separately:

```text
content/daily-digests/YYYY-MM-DD.md
```
