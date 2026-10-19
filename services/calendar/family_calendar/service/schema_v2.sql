CREATE TABLE guardian_rules(id text PRIMARY KEY, group_id text NOT NULL, dependent_id text NOT NULL,
 activity text NOT NULL CHECK(char_length(activity) BETWEEN 1 AND 64),
 duty text NOT NULL CHECK(duty IN ('dropoff','pickup')), guardian_id text NOT NULL,
 source_calendar_ref text NOT NULL REFERENCES calendars, target_calendar_ref text NOT NULL REFERENCES calendars,
 lead_minutes int NOT NULL CHECK(lead_minutes BETWEEN 1 AND 180),
 title text NOT NULL CHECK(char_length(title) BETWEEN 1 AND 128),
 visibility text NOT NULL CHECK(visibility IN ('private','details')), copy_location boolean NOT NULL DEFAULT false,
 enabled boolean NOT NULL DEFAULT true, version bigint NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(group_id,dependent_id,activity,duty),
 FOREIGN KEY(group_id,guardian_id,dependent_id) REFERENCES guardians(group_id,adult_id,dependent_id));
CREATE TABLE projection_links(id text PRIMARY KEY, rule_id text NOT NULL REFERENCES guardian_rules,
 rule_version bigint NOT NULL, duty text NOT NULL CHECK(duty IN ('dropoff','pickup')),
 source_calendar_ref text NOT NULL REFERENCES calendars, source_event_ref text NOT NULL,
 source_start timestamptz NOT NULL, source_end timestamptz NOT NULL,
 target_calendar_ref text NOT NULL REFERENCES calendars, target_id text NOT NULL, target_resource_ref text,
 operation_id text REFERENCES operations, ordinal int, previous_target_id text, etag text,
 state text NOT NULL CHECK(state IN ('pending','active','repairing','removed')),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK(source_end>source_start), UNIQUE(target_calendar_ref,target_id));
-- One row owns each (occurrence, rule) claim while it is reserved, live or under repair.
CREATE UNIQUE INDEX projection_links_claim ON projection_links(source_event_ref,rule_id)
 WHERE state IN ('pending','active','repairing');
