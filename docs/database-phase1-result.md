# Phase 1 verified result (2026-09-14)

Status: complete. Phase 2 may begin; legacy readers/writers remain unchanged.

- Private evidence: `.operation-state/database-consolidation/20260914T040750.644763Z/phase1-result.json`
- SQLite backup API snapshot and isolated restoration both passed integrity checks.
- All 6 tables matched by schema, count, complete row-content hash, key duplicates, reference mismatches and states.
- Counts: articles 18009; article_contents 14822; talents 604; article_talents 1675; article_classifications 830; article_feedback 16.
- 397 input/supporting files were copied and rehashed. Source inventory remained unchanged.
- Existing URL duplicates: 114 article groups, 37 content groups. Duplicate article/talent pairs: 27 groups. Content keys absent from articles: 13624 rows. These are preserved, not corrected or approved for merging.
- Dashboard, weekly and all 5 AI input task outputs were saved privately. Dashboard sourceError was empty. Reader parameters are recorded with the outputs; AI behavior was not modified.
- n8n and article writers were absent in WSL before/after; no new/running/waiting executions were present. Windows process inspection found only shells opened in the parent N8N directory, not n8n writers.
- Backup directory mode: 0700; database mode: 0600; Git ignore verified.
- No legacy data/schema changes, no source switching, no deletions or automatic merges. No rollback is necessary for this phase.

See [backup and restoration procedure](database-phase1-backup-restore.md). Detailed data and full internal backups remain outside Git.
