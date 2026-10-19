-- Per-subscription hint timing (ANYM-252): debounce, batch, quiet hours, silence timers and a
-- per-key hourly hint budget. Expand-only: schema-36 code never reads the new columns or the
-- held-state table and never writes kind 'silence'. Policy changes only when the matcher
-- emits a hint, never matches, ACK positions or passive reads.
-- A short lock wait fails the upgrade instead of stalling hint writers. NOT VALID skips the
-- history scan under the lock: the replaced, validated constraint allowed a subset of the kinds.
DO $$ BEGIN
 IF NOT (SELECT convalidated FROM pg_constraint
         WHERE conrelid='subscription_outbox'::regclass AND conname='subscription_outbox_kind_check') THEN
  RAISE EXCEPTION 'subscription_outbox_kind_check_not_validated';
 END IF;
END $$;
SET LOCAL lock_timeout='2s';
ALTER TABLE subscription_outbox DROP CONSTRAINT subscription_outbox_kind_check;
ALTER TABLE subscription_outbox ADD CONSTRAINT subscription_outbox_kind_check
 CHECK(kind IN ('events','test','silence')) NOT VALID;
-- One hint per (subscription, version, kind, window): retries and parallel workers cannot double-emit.
ALTER TABLE subscription_outbox ADD COLUMN window_id bigint CHECK(window_id>0);
CREATE UNIQUE INDEX subscription_hint_window ON subscription_outbox(subscription,subscription_version,kind,window_id)
 WHERE window_id IS NOT NULL;
CREATE INDEX subscription_hint_budget ON subscription_outbox(subscription,created_at);
-- Dispatcher admission: the first POST of a hint ID reserves one slot of the key's rolling hour.
ALTER TABLE subscription_outbox ADD COLUMN admitted_at timestamptz;
CREATE INDEX subscription_hint_admission ON subscription_outbox(subscription,admitted_at)
 WHERE admitted_at IS NOT NULL;
-- The policy is the whole delivery configuration; silence timers belong to policy_version only.
ALTER TABLE subscription_delivery ADD COLUMN policy jsonb
 CHECK(policy IS NULL OR (jsonb_typeof(policy)='object' AND octet_length(policy::text)<=4096));
ALTER TABLE subscription_delivery ADD COLUMN policy_version bigint CHECK(policy_version>0);
ALTER TABLE subscription_delivery ADD COLUMN silence_deadline timestamptz;
SET LOCAL lock_timeout=DEFAULT;

-- Durable held state, one row per subscription, owned by the matcher. A row for an older
-- subscription version is stale and is reset before use. next_window never decreases.
CREATE TABLE subscription_hint_windows (
 subscription uuid PRIMARY KEY REFERENCES subscriptions(id) ON DELETE CASCADE,
 subscription_version bigint NOT NULL CHECK(subscription_version>0),
 next_window bigint NOT NULL DEFAULT 1 CHECK(next_window>0),
 first_match_at timestamptz, last_match_at timestamptz,
 match_count integer NOT NULL DEFAULT 0 CHECK(match_count BETWEEN 0 AND 1000000),
 through_position bigint CHECK(through_position>=0),
 bypass boolean NOT NULL DEFAULT false,
 -- Matches above hinted_through with no held range are reopened, so a failed hint step retries.
 hinted_through bigint CHECK(hinted_through>=0),
 due_at timestamptz,
 held_reason text CHECK(held_reason IN ('debounce','batch','quiet_hours','budget')),
 silence_state text CHECK(silence_state IN ('replied','fired','hinted','cancelled')),
 silence_fired_at timestamptz, silence_due_at timestamptz,
 silence_held_reason text CHECK(silence_held_reason IN ('quiet_hours','budget')),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK((first_match_at IS NULL)=(through_position IS NULL))
);
CREATE INDEX subscription_hint_due ON subscription_hint_windows(due_at) WHERE due_at IS NOT NULL;
-- Every match of an existing subscription was hinted by schema-36 code in its matching transaction.
INSERT INTO subscription_hint_windows(subscription,subscription_version,hinted_through)
 SELECT id,version,matched_through FROM subscriptions;
CREATE INDEX subscription_silence_due ON subscription_hint_windows(silence_due_at) WHERE silence_due_at IS NOT NULL;

-- PostgreSQL moves a skipped local time forward by the gap and maps an ambiguous local time
-- to its later occurrence. Quiet windows use the first occurrence instead.
CREATE FUNCTION public.subscription_local_instant(local timestamp, zone text) RETURNS timestamptz
LANGUAGE plpgsql STABLE SET search_path=pg_catalog AS $$
DECLARE later timestamptz := local AT TIME ZONE zone; probe timestamptz; earlier timestamptz;
BEGIN
 probe := later - interval '3 hours';
 earlier := (local - ((probe AT TIME ZONE zone) - (probe AT TIME ZONE 'UTC'))) AT TIME ZONE 'UTC';
 IF earlier < later AND (earlier AT TIME ZONE zone) = local THEN
  RETURN earlier;
 END IF;
 RETURN later;
END $$;

-- quiet = {timezone, windows: [{days: [ISO weekday 1-7 the window starts], start, end}]}.
-- Returns the end of the quiet time containing at (chained through adjacent windows), or NULL.
CREATE FUNCTION public.subscription_quiet_until(quiet jsonb, at timestamptz) RETURNS timestamptz
LANGUAGE plpgsql STABLE SET search_path=pg_catalog AS $$
DECLARE zone text := quiet->>'timezone'; t timestamptz := at; result timestamptz;
 item jsonb; day date; opens timestamptz; closes timestamptz; moved boolean; rounds integer := 0;
BEGIN
 IF quiet IS NULL OR zone IS NULL OR at IS NULL THEN
  RETURN NULL;
 END IF;
 LOOP
  moved := false;
  FOR item IN SELECT value FROM jsonb_array_elements(quiet->'windows') LOOP
   FOR day IN SELECT (t AT TIME ZONE zone)::date + k FROM generate_series(-1, 0) AS k LOOP
    IF item->'days' @> to_jsonb(extract(isodow FROM day)::integer) THEN
     opens := public.subscription_local_instant(day + (item->>'start')::time, zone);
     closes := public.subscription_local_instant((day + CASE WHEN (item->>'end')::time <= (item->>'start')::time
       THEN 1 ELSE 0 END) + (item->>'end')::time, zone);
     IF opens <= t AND t < closes THEN
      t := closes; result := closes; moved := true;
     END IF;
    END IF;
   END LOOP;
  END LOOP;
  rounds := rounds + 1;
  EXIT WHEN NOT moved OR rounds >= 16;
 END LOOP;
 RETURN result;
END $$;

-- Existing restricted logins get the held state with the schema, so no deployed state runs the
-- schema-37 code without it (the 034 precedent). The role files repeat these for new installs.
DO $$ BEGIN
 IF EXISTS(SELECT FROM pg_roles WHERE rolname='messaging_subscriptions') THEN
  GRANT SELECT,INSERT,UPDATE ON subscription_hint_windows TO messaging_subscriptions;
 END IF;
 IF EXISTS(SELECT FROM pg_roles WHERE rolname='messaging_gateway') THEN
  GRANT SELECT ON subscription_hint_windows TO messaging_gateway;
  GRANT UPDATE(subscription_version,due_at,silence_state,silence_fired_at,silence_due_at,
   silence_held_reason,updated_at) ON subscription_hint_windows TO messaging_gateway;
 END IF;
 IF EXISTS(SELECT FROM pg_roles WHERE rolname='messaging_event_delivery') THEN
  GRANT UPDATE(admitted_at) ON subscription_outbox TO messaging_event_delivery;
 END IF;
END $$;
