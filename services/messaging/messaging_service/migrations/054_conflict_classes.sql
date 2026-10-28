-- rollout: manual
-- Conflict classes (ANYM-557): each version conflict stores the class of what it contradicted and the version of
-- the rule that named it. Additive and nullable: N-1 code inserts without them and readers take NULL as
-- `unclassified`. Manual so the release runs migrate(), which classifies the existing rows after this transaction
-- commits, in short batches of their own (conflict_classes.backfill); the columns themselves take no table rewrite.
ALTER TABLE message_conflicts ADD COLUMN reason text;
ALTER TABLE message_conflicts ADD COLUMN reason_version smallint;
