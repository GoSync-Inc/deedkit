-- rollout: manual
-- ANYM-997: Android's own metadata-only outbox. No transport subscription, existing cursor,
-- message/module kind, existing-table grant or applied migration is changed. Default PAUSED;
-- only root's separate binding/routine/installed verification can activate a binding.
CREATE TABLE android_wake_bindings (
 principal text NOT NULL REFERENCES principals(id),
 key_id uuid NOT NULL REFERENCES api_keys(id),
 device_id text NOT NULL CHECK(length(device_id) BETWEEN 1 AND 128),
 owner_id text NOT NULL CHECK(length(owner_id) BETWEEN 1 AND 128),
 source_id text NOT NULL CHECK(length(source_id) BETWEEN 1 AND 128),
 auth_epoch bigint NOT NULL CHECK(auth_epoch>0),
 endpoint uuid NOT NULL REFERENCES delivery_endpoints(id),
 generation bigint NOT NULL DEFAULT 1 CHECK(generation>0),
 state text NOT NULL DEFAULT 'paused' CHECK(state IN ('paused','active','revoked')),
 routine_verified boolean NOT NULL DEFAULT false,
 installed_verified boolean NOT NULL DEFAULT false,
 valid_until timestamptz NOT NULL,
 journal_id text CHECK(journal_id IS NULL OR length(journal_id) BETWEEN 1 AND 128),
 after_seq bigint NOT NULL DEFAULT 0 CHECK(after_seq>=0),
 PRIMARY KEY(principal,key_id,device_id)
);
CREATE TABLE android_wake_intents (
 principal text NOT NULL REFERENCES principals(id),
 key_id uuid NOT NULL REFERENCES api_keys(id),
 wake_id uuid NOT NULL,
 device_id text NOT NULL,
 endpoint uuid NOT NULL REFERENCES delivery_endpoints(id),
 binding_generation bigint NOT NULL,
 identity_hash text NOT NULL CHECK(identity_hash ~ '^[0-9a-f]{64}$'),
 origin_hash text CHECK(origin_hash ~ '^[0-9a-f]{64}$'),
 identity jsonb NOT NULL CHECK(jsonb_typeof(identity)='object' AND octet_length(identity::text)<=4096),
 journal_id text NOT NULL,
 source_seq bigint NOT NULL CHECK(source_seq>0),
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sending','accepted','unknown','refused','expired','cancelled')),
 hold_reason text,
 attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
 lease_until timestamptz,
 admitted_at timestamptz,
 next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(principal,key_id,wake_id)
);
CREATE INDEX android_wake_due ON android_wake_intents(state,next_attempt_at);
CREATE INDEX android_wake_admitted ON android_wake_intents(key_id,admitted_at);
CREATE UNIQUE INDEX android_wake_one_sending ON android_wake_intents(principal,key_id) WHERE state='sending';
CREATE UNIQUE INDEX android_wake_original_native_event ON android_wake_intents(principal,key_id,origin_hash);

-- Read-lock only: the dispatcher cannot mutate activation, routing or source cursors.
CREATE FUNCTION public.lock_android_wake_binding(p_principal pg_catalog.text,p_key pg_catalog.uuid,
    p_device pg_catalog.text) RETURNS SETOF public.android_wake_bindings
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog AS $$
    SELECT b.* FROM public.android_wake_bindings AS b
    WHERE b.principal=p_principal AND b.key_id=p_key AND b.device_id=p_device FOR SHARE OF b
$$;
REVOKE ALL ON FUNCTION public.lock_android_wake_binding(pg_catalog.text,pg_catalog.uuid,pg_catalog.text) FROM PUBLIC;
