#!/usr/bin/env python3
"""Semantic HTML-to-Markdown extraction for article review files."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Iterable

from article_review_common import clean_text, markdown_text, markdown_url


BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
    "figcaption", "pre", "th", "td",
}
HARD_SKIP_TAGS = {"style", "noscript", "svg", "canvas", "form", "template"}
NON_CONTENT_TAGS = {"nav", "footer", "header", "aside"}
NOISE_PHRASES = (
    "cookie", "プライバシー", "個人情報保護", "この記事をシェア",
    "会員登録", "ログイン", "メニューを開く", "関連記事", "おすすめ記事",
)


@dataclass
class HtmlBlock:
    tag: str
    text: str
    in_article: bool
    in_main: bool
    links: list[tuple[str, str]] = field(default_factory=list)


class SemanticHTMLParser(HTMLParser):
    """Extract semantic blocks and separately retain site-chrome text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[HtmlBlock] = []
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self.non_content_parts: list[str] = []
        self.json_ld_documents: list[str] = []
        self.current_json_ld: list[str] = []
        self.article_depth = 0
        self.main_depth = 0
        self.non_content_depth = 0
        self.hard_skip_depth = 0
        self.non_content_stack: list[bool] = []
        self.title_depth = 0
        self.json_ld_depth = 0
        self.current_tag = ""
        self.current_parts: list[str] = []
        self.current_links: list[tuple[str, str]] = []
        self.active_href = ""
        self.active_link_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if tag == "script" and "ld+json" in attributes.get("type", "").casefold():
            self.json_ld_depth += 1
            self.current_json_ld = []
            return
        if tag == "script" or tag in HARD_SKIP_TAGS:
            self.hard_skip_depth += 1
            return
        if self.hard_skip_depth:
            return
        if tag == "article":
            self.article_depth += 1
        if tag == "main" or attributes.get("role", "").casefold() == "main":
            self.main_depth += 1
        if tag in NON_CONTENT_TAGS:
            is_site_chrome = tag != "header" or not self.article_depth
            self.non_content_stack.append(is_site_chrome)
            if is_site_chrome:
                self.non_content_depth += 1
        if tag == "title":
            self.title_depth += 1
        if tag == "meta":
            key = (attributes.get("name") or attributes.get("property") or "").casefold()
            if key in {
                "description", "og:description", "twitter:description", "og:title",
                "article:published_time", "article:modified_time", "author",
            }:
                self.meta[key] = clean_text(attributes.get("content"))
            return
        if tag == "a":
            self.active_href = attributes.get("href", "").strip()
            self.active_link_parts = []
        if tag == "img" and not self.non_content_depth:
            alt = clean_text(attributes.get("alt"))
            src = attributes.get("src") or attributes.get("data-src") or ""
            if alt or src:
                text = alt or "画像"
                links = [(text, src)] if src else []
                self.blocks.append(
                    HtmlBlock("img", text, bool(self.article_depth), bool(self.main_depth), links)
                )
        if tag in BLOCK_TAGS:
            self._flush()
            self.current_tag = tag

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "script" and self.json_ld_depth:
            self.json_ld_depth = max(0, self.json_ld_depth - 1)
            document = "".join(self.current_json_ld).strip()
            if document:
                self.json_ld_documents.append(document)
            self.current_json_ld = []
            return
        if tag == "script" or tag in HARD_SKIP_TAGS:
            self.hard_skip_depth = max(0, self.hard_skip_depth - 1)
            return
        if self.hard_skip_depth:
            return
        if tag == "a":
            label = clean_text(" ".join(self.active_link_parts))
            if self.active_href:
                self.current_links.append((label or self.active_href, self.active_href))
            self.active_href = ""
            self.active_link_parts = []
        if tag in BLOCK_TAGS:
            self._flush()
        if tag == "title":
            self.title_depth = max(0, self.title_depth - 1)
        if tag in NON_CONTENT_TAGS:
            is_site_chrome = self.non_content_stack.pop() if self.non_content_stack else True
            if is_site_chrome:
                self.non_content_depth = max(0, self.non_content_depth - 1)
        if tag == "article":
            self.article_depth = max(0, self.article_depth - 1)
        if tag == "main":
            self.main_depth = max(0, self.main_depth - 1)

    def handle_data(self, data: str) -> None:
        if self.json_ld_depth:
            self.current_json_ld.append(data)
            return
        if self.hard_skip_depth:
            return
        if self.title_depth:
            self.title_parts.append(data)
        if self.non_content_depth:
            self.non_content_parts.append(data)
            return
        self.current_parts.append(data)
        if self.active_href:
            self.active_link_parts.append(data)

    def close(self) -> None:
        super().close()
        self._flush()

    def _flush(self) -> None:
        value = clean_text(" ".join(self.current_parts))
        tag = self.current_tag or "p"
        self.current_parts = []
        self.current_tag = ""
        if value and not any(phrase.casefold() in value.casefold() for phrase in NOISE_PHRASES):
            self.blocks.append(
                HtmlBlock(
                    tag,
                    value,
                    bool(self.article_depth),
                    bool(self.main_depth),
                    list(self.current_links),
                )
            )
        self.current_links = []


def _visit_json_ld(value: Any, result: dict[str, Any]) -> None:
    if isinstance(value, list):
        for item in value:
            _visit_json_ld(item, result)
        return
    if not isinstance(value, dict):
        return
    if value.get("@graph"):
        _visit_json_ld(value["@graph"], result)
    raw_kind = value.get("@type", "")
    kinds = set(raw_kind if isinstance(raw_kind, list) else [raw_kind])
    if not kinds.intersection({"Article", "NewsArticle", "BlogPosting", "Report", "WebPage"}):
        return
    for key in ("headline", "description", "datePublished", "dateModified", "articleBody"):
        if value.get(key) and not result.get(key):
            result[key] = value[key]
    author = value.get("author")
    if author and not result.get("author"):
        if isinstance(author, list):
            result["author"] = ", ".join(
                clean_text(item.get("name") if isinstance(item, dict) else item)
                for item in author
            )
        elif isinstance(author, dict):
            result["author"] = clean_text(author.get("name"))
        else:
            result["author"] = clean_text(author)


def json_ld_metadata(documents: Iterable[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for document in documents:
        try:
            payload = json.loads(document)
        except json.JSONDecodeError:
            continue
        _visit_json_ld(payload, result)
    return result


def render_block(block: HtmlBlock) -> str:
    text = block.text
    if block.links:
        suffixes: list[str] = []
        seen: set[str] = set()
        for label, url in block.links:
            if not url or url in seen:
                continue
            seen.add(url)
            suffixes.append(f"[{markdown_text(label)}]({markdown_url(url)})")
        if suffixes:
            text += " " + " ".join(suffixes)
    if block.tag.startswith("h") and block.tag[1:].isdigit():
        level = min(6, max(2, int(block.tag[1:]) + 1))
        return "#" * level + " " + text
    if block.tag == "li":
        return "- " + text
    if block.tag == "blockquote":
        return "> " + text
    if block.tag == "pre":
        return "~~~text\n" + text + "\n~~~"
    if block.tag == "figcaption":
        return "*" + text + "*"
    if block.tag == "img":
        url = block.links[0][1] if block.links else ""
        return (
            f"![{markdown_text(text)}]({markdown_url(url)})"
            if url
            else f"*画像: {markdown_text(text)}*"
        )
    if block.tag in {"th", "td"}:
        return "- " + text
    return text


def semantic_html_to_markdown(page_html: str, fallback_title: str = "") -> dict[str, Any]:
    parser = SemanticHTMLParser()
    parser.feed(page_html)
    parser.close()
    article_blocks = [block for block in parser.blocks if block.in_article]
    main_blocks = [block for block in parser.blocks if block.in_main]

    def block_length(blocks: Iterable[HtmlBlock]) -> int:
        return sum(len(block.text) for block in blocks)

    if block_length(article_blocks) >= 120:
        selected = article_blocks
        scope = "article"
    elif block_length(main_blocks) >= 120:
        selected = main_blocks
        scope = "main"
    else:
        selected = parser.blocks
        scope = "page-fallback"

    metadata: dict[str, Any] = dict(parser.meta)
    metadata["html_title"] = clean_text(" ".join(parser.title_parts)) or clean_text(fallback_title)
    metadata.update(
        {key: value for key, value in json_ld_metadata(parser.json_ld_documents).items() if value}
    )
    json_body = clean_text(metadata.get("articleBody"))
    if json_body and block_length(selected) < max(120, len(json_body) // 2):
        selected = [
            HtmlBlock("p", paragraph, True, True)
            for paragraph in json_body.splitlines()
            if clean_text(paragraph)
        ]
        if len(selected) == 1:
            selected = [HtmlBlock("p", json_body, True, True)]
        scope = "json-ld-articleBody"

    rendered = [render_block(block) for block in selected if clean_text(block.text)]
    plain_text = "\n".join(block.text for block in selected if clean_text(block.text))
    content_markdown = "\n\n".join(value for value in rendered if value).strip()
    non_content_text = clean_text(" ".join(parser.non_content_parts))
    block_count = len(selected)
    if len(plain_text) >= 3000 and block_count >= 8:
        completeness = "full"
    elif len(plain_text) >= 500 and block_count >= 3:
        completeness = "substantial"
    elif len(plain_text) >= 80:
        completeness = "partial"
    elif any(clean_text(metadata.get(key)) for key in ("description", "og:description", "articleBody")):
        completeness = "metadata_only"
    else:
        completeness = "unavailable"
    return {
        "content_markdown": content_markdown,
        "plain_text": plain_text,
        "non_content_text": non_content_text,
        "metadata": metadata,
        "blocks": selected,
        "extraction_scope": scope,
        "completeness": completeness,
    }
