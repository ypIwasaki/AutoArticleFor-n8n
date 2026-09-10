"""Pure Markdown readers shared by the dashboard and saved-artifact validation."""
from __future__ import annotations
import re
from pathlib import Path

MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
SOURCE_NOTES_HEADING = "## Source-by-source Notes"
SOURCE_NOTE_ITEM_PATTERN = re.compile(r"^(?:-\s+|\d+\.\s+)(.+)$")
SOURCE_NOTE_SUMMARY_PATTERN = re.compile(
    r"(?:^|\s)-\s*要約\s*[:：]\s*(.*?)(?=\s+-\s*(?:関連キーワード|重要度|根拠)\s*[:：]|$)"
)
BODY_VERIFIED_PATTERN = re.compile(r"(?:^|\s)-\s*本文確認\s*[:：]\s*確認済み(?:\s|（|\(|$)")
WEEKLY_REPORT_FILENAME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
WEEKLY_REPORT_FRONT_MATTER_PATTERN = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def source_note_items(markdown: str) -> list[str]:
    """Return source-note items written as bullets or numbered Markdown lists."""
    if SOURCE_NOTES_HEADING not in markdown:
        return []

    section = markdown.split(SOURCE_NOTES_HEADING, 1)[1]
    section = re.split(r"^##\s+", section, maxsplit=1, flags=re.MULTILINE)[0]
    items: list[str] = []
    current: str | None = None
    for line in section.splitlines():
        match = SOURCE_NOTE_ITEM_PATTERN.match(line)
        if match:
            if current:
                items.append(current)
            current = match.group(1).strip()
        elif current and line.strip():
            current = f"{current} {line.strip()}"
    if current:
        items.append(current)
    return items


def parsed_summaries(markdown: str, summary_date: str) -> list[tuple[list[tuple[str, str]], dict]]:
    result = []
    for item in source_note_items(markdown):
        links = MARKDOWN_LINK_PATTERN.findall(item)
        without_links = MARKDOWN_LINK_PATTERN.sub("", item).strip()
        match = SOURCE_NOTE_SUMMARY_PATTERN.search(without_links)
        if links and BODY_VERIFIED_PATTERN.search(without_links) and match:
            result.append((links, {
                "text": re.sub(r"\s+", " ", match.group(1)).strip(),
                "summary_date": summary_date,
                "source_titles": [title for title, _ in links],
            }))
    return result


def weekly_report_metadata(markdown: str, path: Path) -> dict[str, str]:
    metadata = {
        "weekStart": path.stem,
        "weekEnd": "",
        "coveredThrough": "",
        "title": "週次ニュース調査レポート",
        "generatedAt": "",
    }
    match = WEEKLY_REPORT_FRONT_MATTER_PATTERN.match(markdown)
    if not match:
        return metadata
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        normalized_key = key.strip()
        if normalized_key not in metadata:
            continue
        metadata[normalized_key] = value.strip().strip('"').strip("'")
    return metadata
