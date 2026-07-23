#!/usr/bin/env python3
"""Synchronize active talent rosters from configured official sources.

The script writes a provenance-preserving source snapshot and a roster-only
proposal for the existing Apply Talent Index Proposal n8n workflow. It never
deletes records and only posts to n8n when --apply is explicitly requested.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib import error, request
from urllib.parse import urljoin


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "organizations.json"
OUTPUT_DIR = PROJECT_ROOT / "content" / "official-talent-registry"
USER_AGENT = "AutoArticleFor-n8n official talent registry sync/1.0 (local monitoring)"
INACTIVE_MARKERS = ("卒業", "退職", "配信活動終了", "活動終了")
JST = timezone(timedelta(hours=9))


class RegistryError(RuntimeError):
    """Raised for invalid source data that must be reviewed before applying."""


class NextDataParser(HTMLParser):
    """Collect the JSON payload in a Next.js __NEXT_DATA__ script tag."""

    def __init__(self) -> None:
        super().__init__()
        self.capture = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.capture = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.capture = False

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.parts.append(data)


def now_jst_date() -> str:
    return datetime.now(JST).date().isoformat()


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(value: object) -> str:
    return " ".join(unescape(str(value or "")).replace("\xa0", " ").split())


def strip_html(value: str) -> str:
    return clean_text(re.sub(r"<[^>]+>", " ", value))


def compact_name(value: str) -> str:
    return re.sub(r"\s+", "", clean_text(value))


def normalized_name(value: object) -> str:
    return compact_name(str(value or "")).replace("！", "!").replace("・", "").replace("･", "").casefold()


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def attr_value(attrs: str, name: str) -> str:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*(['\"])(.*?)\1", attrs, re.I | re.S)
    return unescape(match.group(2)).strip() if match else ""


def anchor_blocks(html: str) -> Iterable[tuple[str, str]]:
    pattern = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
    for attrs, inner in pattern.findall(html):
        href = attr_value(attrs, "href")
        if href:
            yield href, inner


def image_alt(inner: str) -> str:
    match = re.search(r"<img\b([^>]*)>", inner, re.I | re.S)
    return attr_value(match.group(1), "alt") if match else ""


def aliases_for(display_name: str, extras: Iterable[str] = ()) -> list[str]:
    aliases: list[str] = []
    for value in [display_name, compact_name(display_name), *extras]:
        text = clean_text(value)
        if text and normalized_name(text) not in {normalized_name(item) for item in aliases}:
            aliases.append(text)
    return aliases


def roster_entry(
    group: dict[str, Any],
    display_name: str,
    profile_url: str,
    extras: Iterable[str] = (),
    organization: str | None = None,
) -> dict[str, Any]:
    name = clean_text(display_name)
    if not name:
        raise RegistryError(f"{group['id']}: empty talent name")
    return {
        "group_id": group["id"],
        "group_name": group["displayName"],
        "organization": organization or group.get("organization") or group["displayName"],
        "display_name": name,
        "aliases": aliases_for(name, extras),
        "profile_url": profile_url,
        "source_url": group["sourceUrl"],
    }


def fetch_source(url: str, timeout: int) -> str:
    source_request = request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with request.urlopen(source_request, timeout=timeout) as response:
            if response.status != 200:
                raise RegistryError(f"Unexpected HTTP status {response.status} for {url}")
            return response.read().decode("utf-8", errors="replace")
    except error.URLError as exc:
        raise RegistryError(f"Could not fetch {url}: {exc}") from exc


def parse_next_data(html: str) -> dict[str, Any]:
    parser = NextDataParser()
    parser.feed(html)
    if not parser.parts:
        raise RegistryError("__NEXT_DATA__ was not found")
    try:
        return json.loads("".join(parser.parts))
    except json.JSONDecodeError as exc:
        raise RegistryError(f"__NEXT_DATA__ is invalid JSON: {exc}") from exc


def walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def parse_nijisanji(group: dict[str, Any], html: str) -> list[dict[str, Any]]:
    allowed = set(group.get("allowedAffiliations") or [])
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in walk_json(parse_next_data(html)):
        profile = value.get("profile") if isinstance(value, dict) else None
        if not isinstance(profile, dict):
            continue
        name = clean_text(value.get("name"))
        identifier = clean_text(value.get("id"))
        affiliation = profile.get("affiliation")
        if not name or not identifier or not isinstance(affiliation, list) or not affiliation:
            continue
        organization = clean_text(affiliation[0])
        if organization not in allowed or identifier in seen:
            continue
        slug = clean_text(value.get("slug"))
        profile_url = urljoin(group["sourceUrl"], f"/talents/{slug}") if slug else group["sourceUrl"]
        result.append(roster_entry(group, name, profile_url, [clean_text(value.get("enName"))], organization))
        seen.add(identifier)
    return result


def parse_talent_h3(group: dict[str, Any], html: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for href, inner in anchor_blocks(html):
        heading = re.search(r"<h3\b[^>]*>(.*?)</h3>", inner, re.I | re.S)
        if not heading:
            continue
        raw = heading.group(1)
        visible = strip_html(raw)
        if not visible or any(marker in visible for marker in INACTIVE_MARKERS):
            continue
        japanese_part = re.split(r"<span\b", raw, maxsplit=1, flags=re.I)[0]
        display_name = strip_html(japanese_part)
        if not display_name:
            continue
        span = re.search(r"<span\b[^>]*>(.*?)</span>", raw, re.I | re.S)
        english_name = strip_html(span.group(1)) if span else ""
        profile_url = urljoin(group["sourceUrl"], href)
        identity = profile_url.rstrip("/")
        if identity in seen:
            continue
        seen.add(identity)
        result.append(roster_entry(group, display_name, profile_url, [english_name]))
    return result


def parse_vspo(group: dict[str, Any], html: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for attrs in re.findall(r"<li\b([^>]*)>", html, re.I | re.S):
        if "member__select__member-item" not in attrs:
            continue
        name = attr_value(attrs, "data-name")
        member_id = attr_value(attrs, "data-id")
        if not name or not member_id or member_id in seen:
            continue
        seen.add(member_id)
        result.append(roster_entry(group, compact_name(name), f"{group['sourceUrl']}#member-{member_id}", [name]))
    return result


def parse_image_link_roster(
    group: dict[str, Any], html: str, path_fragment: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for href, inner in anchor_blocks(html):
        profile_url = urljoin(group["sourceUrl"], href)
        if path_fragment not in profile_url:
            continue
        if group["id"] in {"aogiri-high-school", "specialite"} and profile_url.rstrip("/").endswith(path_fragment.rstrip("/")):
            continue
        name = image_alt(inner)
        if not name or name.casefold() == "coming soon":
            continue
        identity = profile_url.rstrip("/")
        if identity in seen:
            continue
        seen.add(identity)
        result.append(roster_entry(group, compact_name(name), profile_url, [name]))
    return result


def parse_replay(group: dict[str, Any], html: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for href, inner in anchor_blocks(html):
        profile_url = urljoin(group["sourceUrl"], href)
        if "/member_list/" not in profile_url:
            continue
        name = strip_html(inner)
        if not name:
            continue
        identity = profile_url.rstrip("/")
        if identity in seen:
            continue
        seen.add(identity)
        result.append(roster_entry(group, compact_name(name), profile_url, [name]))
    return result


def parse_group(group: dict[str, Any], html: str) -> list[dict[str, Any]]:
    parser = group.get("parser")
    if parser == "nijisanji_next_data":
        rows = parse_nijisanji(group, html)
    elif parser == "talent_h3":
        rows = parse_talent_h3(group, html)
    elif parser == "vspo_data_name":
        rows = parse_vspo(group, html)
    elif parser == "aogiri_member_links":
        rows = parse_image_link_roster(group, html, "/aogirihighschool/members/")
    elif parser == "neo_porte_member_links":
        rows = parse_image_link_roster(group, html, "/member/")
    elif parser == "specialite_talent_links":
        rows = parse_image_link_roster(group, html, "/talents/")
    elif parser == "replay_member_links":
        rows = parse_replay(group, html)
    elif parser == "uomusume_character_links":
        rows = parse_image_link_roster(group, html, "/character/")
    else:
        raise RegistryError(f"Unsupported parser {parser!r} for {group.get('id')}")
    if not rows:
        raise RegistryError(f"{group['id']}: no active talents were extracted")
    return rows


def load_existing_talents() -> list[dict[str, Any]]:
    database = Path(os.environ.get("N8N_DATABASE_PATH", "~/.n8n/database.sqlite")).expanduser()
    if not database.exists():
        return []
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute("SELECT id FROM data_table WHERE name = 'talents'").fetchone()
        if table is None:
            return []
        return [dict(row) for row in connection.execute(f'SELECT * FROM "data_table_user_{table["id"]}"')]
    finally:
        connection.close()


def bool_value(value: object) -> bool:
    return value is True or value == 1 or str(value).casefold() == "true"


def parse_aliases(value: object) -> list[str]:
    if isinstance(value, list):
        return [clean_text(item) for item in value if clean_text(item)]
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parse_aliases(parsed)


def index_existing(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        names = [row.get("display_name", ""), *parse_aliases(row.get("aliases_json"))]
        for name in names:
            key = normalized_name(name)
            if key and key not in index:
                index[key] = row
    return index


def merge_aliases(*alias_sets: Iterable[str]) -> list[str]:
    result: list[str] = []
    keys: set[str] = set()
    for aliases in alias_sets:
        for alias in aliases:
            value = clean_text(alias)
            key = normalized_name(value)
            if value and key not in keys:
                keys.add(key)
                result.append(value)
    return result


def proposal_talents(
    roster: list[dict[str, Any]], existing_rows: list[dict[str, Any]], generated_at: str
) -> tuple[list[dict[str, Any]], int, int]:
    existing = index_existing(existing_rows)
    proposals: list[dict[str, Any]] = []
    inserted = 0
    updated = 0
    for entry in roster:
        match = next((existing.get(normalized_name(alias)) for alias in entry["aliases"] if normalized_name(alias) in existing), None)
        if match:
            talent_id = clean_text(match.get("talent_id"))
            if not talent_id:
                raise RegistryError(f"Existing talent {entry['display_name']} has no talent_id")
            search_enabled = bool_value(match.get("search_enabled"))
            aliases = merge_aliases(parse_aliases(match.get("aliases_json")), entry["aliases"])
            updated += 1
        else:
            identity = entry["profile_url"] or f"{entry['group_id']}:{entry['display_name']}"
            talent_id = stable_id("talent", f"official:{entry['group_id']}:{identity}")
            search_enabled = bool(entry.get("search_enabled_by_default", False))
            aliases = entry["aliases"]
            inserted += 1
        proposals.append(
            {
                "talent_id": talent_id,
                "display_name": entry["display_name"],
                "organization": entry["organization"],
                "aliases_json": json.dumps(aliases, ensure_ascii=False),
                "status": "approved",
                "search_enabled": search_enabled,
                "auto_discovered": False,
                "last_seen_at": generated_at,
            }
        )
    ids = [row["talent_id"] for row in proposals]
    if len(ids) != len(set(ids)):
        raise RegistryError("The generated proposal contains duplicate talent_id values")
    return proposals, inserted, updated


def markdown_table(headers: list[str], rows: list[list[object]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(clean_text(value).replace("|", "\\|") for value in row) + " |")
    return lines


def build_report(
    report_date: str,
    generated_at: str,
    groups: list[dict[str, Any]],
    roster: list[dict[str, Any]],
    inserted: int,
    updated: int,
    unmatched_existing: list[dict[str, Any]],
) -> str:
    group_counts: dict[str, int] = {}
    for entry in roster:
        group_counts[entry["group_id"]] = group_counts.get(entry["group_id"], 0) + 1
    group_rows = [
        [group["displayName"], group["sourceUrl"], group_counts.get(group["id"], 0)]
        for group in groups
    ]
    lines = [
        f"# Official Talent Registry - {report_date}",
        "",
        "## Summary",
        "",
        f"- Fetched at: {generated_at}",
        f"- Active talent entries extracted from official sources: {len(roster)}",
        f"- New n8n talent records proposed: {inserted}",
        f"- Existing n8n talent records refreshed: {updated}",
        "- All roster entries are approved registry records. Individual-name search remains disabled unless it was already enabled deliberately.",
        "",
        "## Sources",
        "",
        *markdown_table(["Group", "Official roster", "Active entries"], group_rows),
        "",
        "## Review Items",
        "",
        "The synchronization does not delete or deactivate records absent from a source. The following existing records are not present in this source snapshot and require review:",
        "",
    ]
    review_rows = [[row.get("display_name", ""), row.get("organization", ""), row.get("status", "")] for row in unmatched_existing]
    lines.extend(markdown_table(["Talent", "Organization", "Current status"], review_rows or [["None", "-", "-"]]))
    lines.extend(["", "## Files", "", f"- Source snapshot: `content/official-talent-registry/{report_date}.json`", f"- n8n proposal: `content/official-talent-registry/proposals/{report_date}.json`", ""])
    return "\n".join(lines)


def wait_for_talent_upserts(talents: list[dict[str, Any]], timeout_seconds: int = 30) -> int:
    expected = {str(row["talent_id"]): row for row in talents}
    deadline = time.monotonic() + timeout_seconds
    while True:
        rows = {str(row.get("talent_id")): row for row in load_existing_talents()}
        applied = sum(
            1
            for talent_id, expected_row in expected.items()
            if talent_id in rows
            and rows[talent_id].get("status") == expected_row["status"]
            and bool_value(rows[talent_id].get("search_enabled")) == bool(expected_row["search_enabled"])
        )
        if applied == len(expected) or time.monotonic() >= deadline:
            return applied
        time.sleep(0.5)


def post_proposal(proposal: dict[str, Any], webhook_url: str) -> dict[str, Any]:
    body = json.dumps(proposal, ensure_ascii=False).encode("utf-8")
    post = request.Request(webhook_url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(post, timeout=90) as response:
            response_body = response.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RegistryError(f"n8n returned HTTP {exc.code}: {response_body}") from exc
    try:
        return json.loads(response_body) if response_body else {"status": response.status}
    except json.JSONDecodeError:
        return {"status": response.status, "body": response_body}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Post the generated roster proposal to local n8n")
    parser.add_argument("--webhook-url", default="http://127.0.0.1:5678/webhook/talent-index/apply")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    groups = config.get("groups")
    if not isinstance(groups, list) or not groups:
        raise RegistryError("config/organizations.json must contain a non-empty groups array")

    generated_at = now_utc()
    roster: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict) or not group.get("id") or not group.get("sourceUrl"):
            raise RegistryError("Each group requires id and sourceUrl")
        html = fetch_source(str(group["sourceUrl"]), args.timeout)
        rows = parse_group(group, html)
        for row in rows:
            row["search_enabled_by_default"] = bool(group.get("searchEnabledByDefault", False))
        roster.extend(rows)

    unique_roster: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in roster:
        identity = (entry["group_id"], entry["profile_url"].rstrip("/"))
        if identity in unique_roster:
            raise RegistryError(f"Duplicate official profile URL: {entry['profile_url']}")
        unique_roster[identity] = entry
    roster = sorted(unique_roster.values(), key=lambda row: (row["organization"], row["display_name"]))

    existing_rows = load_existing_talents()
    talents, inserted, updated = proposal_talents(roster, existing_rows, generated_at)
    source_name_keys = {normalized_name(alias) for row in roster for alias in row["aliases"]}
    target_organizations = {entry["organization"] for entry in roster}
    unmatched_existing = [
        row
        for row in existing_rows
        if clean_text(row.get("organization")) in target_organizations
        and normalized_name(row.get("display_name")) not in source_name_keys
    ]
    unmatched_existing.sort(key=lambda row: (clean_text(row.get("organization")), clean_text(row.get("display_name"))))

    report_date = now_jst_date()
    snapshot = {
        "schemaVersion": 1,
        "generatedAt": generated_at,
        "groups": groups,
        "talents": roster,
    }
    proposal = {
        "proposalVersion": 1,
        "proposalDate": report_date,
        "articles": [],
        "talents": talents,
        "articleTalents": [],
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    proposal_dir = OUTPUT_DIR / "proposals"
    proposal_dir.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / f"{report_date}.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (proposal_dir / f"{report_date}.json").write_text(json.dumps(proposal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUTPUT_DIR / f"{report_date}.md").write_text(
        build_report(report_date, generated_at, groups, roster, inserted, updated, unmatched_existing), encoding="utf-8"
    )

    summary = {
        "date": report_date,
        "officialTalentCount": len(roster),
        "newTalentCount": inserted,
        "refreshedTalentCount": updated,
        "unmatchedExistingCount": len(unmatched_existing),
        "proposalPath": str((proposal_dir / f"{report_date}.json").relative_to(PROJECT_ROOT)),
    }
    if args.apply:
        summary["n8nResponse"] = post_proposal(proposal, args.webhook_url)
        applied = wait_for_talent_upserts(talents)
        summary["verifiedN8nTalentCount"] = applied
        if applied != len(talents):
            raise RegistryError(f"n8n accepted the proposal but verified only {applied}/{len(talents)} talent upserts")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
