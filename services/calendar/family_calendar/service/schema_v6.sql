-- rollout: expand
-- Calendar members linked to people (ANYM-554). Additive only: version-5 code never reads this table.
-- The owner's link from a calendar member to a messaging person UUID; only the owner CLI writes it, and only the UUID
-- is kept (no name, handle or channel). No foreign key: the migration takes no lock on memberships (as 3-5).
CREATE TABLE member_people(group_id text NOT NULL, member_id text NOT NULL, person_uuid uuid NOT NULL,
 linked_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(group_id,member_id), UNIQUE(group_id,person_uuid));
