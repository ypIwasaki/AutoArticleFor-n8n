# Official Talent Registry

`config/organizations.json` defines the groups whose official public rosters
are monitored. The initial scope is the organizations explicitly represented
by the current manual keywords: にじさんじ / NIJISANJI EN, ホロライブ,
ホロスターズ, ぶいすぽっ！, あおぎり高校, ネオポルテ, すぺしゃりて,
りぷらい！, and うおむすめ.

Run the synchronization from the project root:

```bash
python3 scripts/sync_official_talent_registry.py
```

It fetches only the configured official roster pages and writes a dated source
snapshot, Markdown report, and n8n proposal under
`content/official-talent-registry/`. Review the report before applying it:

```bash
python3 scripts/sync_official_talent_registry.py --apply
```

`--apply` posts the generated roster-only proposal to the local `Apply Talent
Index Proposal` workflow. It upserts official roster entries but never deletes
existing talents or changes an unmatched record to inactive.

All official entries are written as `approved`; their `search_enabled` value is
`false` by default. This keeps the full roster available for talent pages and
future official-channel collection without multiplying the daily RSS requests.
Enable individual name search only for deliberate, narrowly scoped monitoring.

The source snapshot records the official list URL, an individual profile URL
when the source exposes one, aliases, and the synchronization timestamp. The
current n8n `talents` Data Table does not yet have columns for that provenance,
so the snapshot is the authoritative audit record until the table schema is
expanded.
