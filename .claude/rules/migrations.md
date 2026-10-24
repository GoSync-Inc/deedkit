---
paths:
  - "services/messaging/messaging_service/migrations/**"
---
<!-- Sources: AGENTS.md, CONTRIBUTING.md, FILE_INDEX.md, MEDIA_STORAGE.md, messaging_service/store.py -->
# Messaging SQL migrations
- Applied migrations are immutable: `Store.migrate()` compares each file's sha256 with `archive_migrations` and raises `migration_checksum_mismatch`. Never edit, rename, reorder or reformat an existing file.
- Schema changes get a new next-numbered file (current head: `044_key_operation_id.sql`, schema 44).
- In the same change: register it in the `migrate()` list, raise the `version>44` runtime ceiling, update backup `TABLES`/`V*_TABLES` and the restore range in `backup.py`, and update role grants (`*_role.sql`).
- Check roles/ACL and a repeated `migrate()` (CONTRIBUTING.md). Update the documented schema version (README, RELEASING).
- Keep source revisions, original provider IDs, archive IDs and acknowledgement semantics stable. Older runtimes deliberately reject a newer database.
- Expand, then contract: a migration works with both the deployed and the new code; drop, rename or narrow only in a later release, once no deployed code uses the old shape. Merging an expand migration is AUTO+review; destructive SQL is OWNER (`scripts/change_path.py`). The agent of an accepted task applies its migration after the backup gate of docs/operations/production.md (AGENTS.md section 2). Reserve the next number in the PR title while you hold the `schema` lane.
- Production deploys do not run migrations. A schema change needs its own procedure and a compatible backup (docs/operations/production.md).
