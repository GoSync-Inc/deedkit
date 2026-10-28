-- rollout: manual
-- Conflict classes (ANYM-557): each version conflict stores the class of what it contradicted and the version of
-- the rule that named it. Additive and nullable: N-1 code inserts without them and readers take NULL as
-- `unclassified`. Manual only because migrate() then classifies the existing rows (conflict_classes.backfill).
ALTER TABLE message_conflicts ADD COLUMN reason text;
ALTER TABLE message_conflicts ADD COLUMN reason_version smallint;
