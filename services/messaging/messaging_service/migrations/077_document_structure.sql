-- rollout: expand
-- Risk tier: non-critical (two new tables and their indexes, grants only on them; no existing row, column or grant
-- changes; docs/operations/production.md "Migration risk tiers").
-- ANYM-851: a stored Markdown version of a PDF, DOCX or PPTX (Docling on the owner's mini, ANYM-850), next to the
-- file's extracted text. It is read only by get_file_text content='structured': no tsvector and no search index, so
-- search, excerpts and attachment_text stay as they are. A row goes with its attachment_text row (ON DELETE CASCADE):
-- a message purge or an attachment delete removes it, and a write after a purge fails its foreign key (ANYM-511).
-- `day_attempts` on `attempt_day` is the daily attempt count the worker's cap sums. structure_profiles is the
-- enricher-mini worker's publication that the structure work is configured for an account (the processing kinds'
-- CHECKs of 018 and 020 admit audio, image and document only), with a pause it set. A schema-76 runtime never names
-- either table. Grants: enrichment_role.sql (the worker) and gateway_role.sql (reads). No column default calls a
-- function other than now() (the expand grammar). IF NOT EXISTS keeps a rerun over a rewound ledger idempotent.
SET LOCAL lock_timeout='2s';
CREATE TABLE IF NOT EXISTS attachment_structure (
 attachment text PRIMARY KEY REFERENCES attachment_text(attachment) ON DELETE CASCADE,
 source_digest text NOT NULL CHECK(source_digest ~ '^[a-f0-9]{64}$'),
 state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','leased','ready','failed','unsupported')),
 markdown text NOT NULL DEFAULT '' CHECK(length(markdown)<=1000000),
 truncated boolean NOT NULL DEFAULT false,
 engine jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(engine)='object' AND octet_length(engine::text)<=4096),
 metadata jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(metadata)='object' AND octet_length(metadata::text)<=65536),
 error_code text CHECK(error_code ~ '^[a-z_]{1,64}$'),
 attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 2),
 lease_token uuid,
 lease_until timestamptz,
 next_due timestamptz NOT NULL DEFAULT now(),
 attempt_day date,
 day_attempts integer NOT NULL DEFAULT 0 CHECK(day_attempts BETWEEN 0 AND 100),
 indexed_at timestamptz,
 updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK(state<>'leased' OR (lease_token IS NOT NULL AND lease_until IS NOT NULL)),
 CHECK(state<>'ready' OR indexed_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS attachment_structure_due ON attachment_structure(state,next_due);
CREATE INDEX IF NOT EXISTS attachment_structure_day ON attachment_structure(attempt_day);
CREATE INDEX IF NOT EXISTS attachment_structure_digest ON attachment_structure(source_digest);
CREATE TABLE IF NOT EXISTS structure_profiles (
 account text PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
 provider text NOT NULL CHECK(provider ~ '^[a-z_]{1,32}$'),
 model text NOT NULL CHECK(model ~ '^[A-Za-z0-9_.:-]{1,128}$'),
 enabled boolean NOT NULL,
 paused_until timestamptz,
 observed_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL
);
