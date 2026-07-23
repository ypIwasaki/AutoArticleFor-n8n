# Official Talent Registry

This directory stores dated snapshots of the active talent rosters fetched from
the official sites configured in `config/organizations.json`.

- `YYYY-MM-DD.json`: machine-readable roster snapshot with source and profile URLs.
- `YYYY-MM-DD.md`: human-readable synchronization report.
- `proposals/YYYY-MM-DD.json`: n8n Data Table proposal for the verified roster.

The synchronization never deletes an existing talent record. A talent absent
from a source is reported for review; it is not automatically marked inactive.
Newly synchronized official talents are approved registry entries, but their
individual-name search is disabled by default to avoid generating hundreds of
RSS requests per daily run.
