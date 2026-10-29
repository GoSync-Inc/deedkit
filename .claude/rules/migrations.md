---
paths:
  - "services/messaging/messaging_service/migrations/**"
---
<!-- Sources: AGENTS.md, CONTRIBUTING.md, FILE_INDEX.md, MEDIA_STORAGE.md, messaging_service/store.py -->
# Messaging SQL migrations
- Applied migrations are immutable: `Store.migrate()` compares each file's sha256 with `archive_migrations` and raises `migration_checksum_mismatch`. Never edit, rename, reorder or reformat an existing file.
- Schema changes get a new next-numbered file (current head: `061_delivery_attempts.sql`, schema 61).
- In the same change: register it in the `migrate()` list, raise the `version>61` runtime ceiling, update backup `TABLES`/`V*_TABLES` and the restore range in `backup.py`, and update role grants (`*_role.sql`).
- Check roles/ACL and a repeated `migrate()` (CONTRIBUTING.md). Update the documented schema version (README, RELEASING).
- Keep source revisions, original provider IDs, archive IDs and acknowledgement semantics stable. Older runtimes deliberately reject a newer database.
- Expand, then contract: a migration works with both the deployed and the new code; drop, rename or narrow only in a later release, once no deployed code uses the old shape. Merging an expand migration is AUTO+review; destructive SQL is OWNER (`scripts/change_path.py`). The agent of a task (a ready card) applies its migration under docs/operations/production.md "Migration releases and the backup gate": an additive release needs no fresh backup, a contract or destructive one does (AGENTS.md section 2). Reserve the next number in the PR title while you hold the `schema` lane.
- Production `deploy` runs a messaging range only when it qualifies (added `expand` files without DROP, appended role grants, a verified copy and a rehearsal; docs/operations/production.md "Unattended migrations", ANYM-449); every other schema change pauses and needs its own procedure (a fresh backup only for a contract or destructive one).
