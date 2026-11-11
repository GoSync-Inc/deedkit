-- rollout: manual
-- Module access journal (ANYM-577): the calendar service says, in its change journal, when a calendar principal's
-- effective access to one calendar changes, so a consumer that reads this journal (the event router, then messaging)
-- stops offering that principal's events at once, without a remote call and without trusting a label.
-- Effective access = authorize(principal, calendar, "agenda:read") with visibility "details" (core.py): the principal
-- is enabled and unexpired, its membership enabled, the calendar bound to it (agent_calendars), the principal's and the
-- grant's scopes both name agenda:read, the connection is enabled and neither visibility layer is busy_only.
-- module_access_state holds the last journaled state of each (principal, calendar) pair; a change appends one row to
-- module_access_events in change_journal's seq (journal_head's row is taken and its head added to in the same
-- transaction, as every journal writer does) and updates the state. The row says `details` with the principal's expiry
-- as `until` (the consumer stops reading at it: expiry is time, not a write), or `none` with a reason.
-- Triggers on every table authorize reads call module_access_refresh for the affected pairs inside the writer's own
-- transaction, so the journal order is the commit order of the access changes and no write path can forget one (the
-- owner CLI, direct SQL, a later feature). The functions are SECURITY DEFINER: the writers hold no journal privilege.
-- The migration journals the state of every existing agent binding once (the baseline the consumer starts from).
-- Risk tier: non-critical writes only to the two new tables and journal_head's head; no row is read for rewriting or
-- deleted. Manual because it creates triggers and functions and journals rows; version-9 code never reads these tables
-- and its journal page ignores rows of other types (it selects change_journal and starting_reminders only).
SET LOCAL lock_timeout='2s';

CREATE TABLE module_access_state(principal_id text NOT NULL, calendar_ref text NOT NULL,
 access text NOT NULL CHECK(access IN ('details','none')), until_at timestamptz,
 PRIMARY KEY(principal_id, calendar_ref));
CREATE TABLE module_access_events(seq bigint PRIMARY KEY CHECK(seq > 0), calendar_ref text NOT NULL,
 principal_id text NOT NULL, access text NOT NULL CHECK(access IN ('details','none')),
 reason text NOT NULL CHECK(reason IN ('granted','permission_removed','principal_removed','member_removed',
  'connection_disabled','visibility_reduced','expired')),
 until_at timestamptz, at timestamptz NOT NULL DEFAULT clock_timestamp());

-- The pair's effective access now, and why it is none.
CREATE FUNCTION module_access_effective(p_principal text, p_calendar text, OUT access text, OUT reason text,
  OUT until_at timestamptz) LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path FROM CURRENT AS $$
DECLARE p record; g record; a record; v_connection text; v_enabled boolean;
BEGIN
 SELECT * INTO p FROM principals WHERE id=p_principal;
 IF NOT FOUND OR NOT p.enabled THEN access:='none'; reason:='principal_removed'; RETURN; END IF;
 IF p.expires_at<=clock_timestamp() THEN access:='none'; reason:='expired'; RETURN; END IF;
 PERFORM 1 FROM memberships WHERE group_id=p.group_id AND person_id=p.person_id AND enabled;
 IF NOT FOUND THEN access:='none'; reason:='member_removed'; RETURN; END IF;
 SELECT * INTO a FROM agent_calendars WHERE principal_id=p_principal AND calendar_ref=p_calendar;
 IF NOT FOUND THEN access:='none'; reason:='permission_removed'; RETURN; END IF;
 SELECT * INTO g FROM calendar_grants WHERE group_id=p.group_id AND person_id=p.person_id AND calendar_ref=p_calendar;
 IF NOT FOUND OR NOT (p.scopes ? 'agenda:read') OR NOT (g.scopes ? 'agenda:read') THEN
  access:='none'; reason:='permission_removed'; RETURN;
 END IF;
 SELECT connection_id INTO v_connection FROM calendars WHERE ref=p_calendar;
 SELECT enabled INTO v_enabled FROM connections WHERE id=v_connection;
 IF NOT COALESCE(v_enabled,false) THEN access:='none'; reason:='connection_disabled'; RETURN; END IF;
 IF 'busy_only' IN (a.visibility, g.visibility) THEN access:='none'; reason:='visibility_reduced'; RETURN; END IF;
 access:='details'; reason:='granted'; until_at:=p.expires_at;
END $$;

-- Journal the pair's change, if any: nothing for a pair that was never `details` and still is not.
CREATE FUNCTION module_access_refresh(p_principal text, p_calendar text) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $$
DECLARE e record; s record; h bigint; known boolean;
BEGIN
 -- journal_head's row first (every journal writer's order): the state is read after any concurrent change committed.
 -- A journal that was never started (no row) stays not ready: the trigger on journal_head journals the baseline when
 -- the worker creates the row, so nothing here creates it.
 PERFORM 1 FROM journal_head WHERE id=1 FOR UPDATE;
 IF NOT FOUND THEN RETURN; END IF;
 SELECT * INTO e FROM module_access_effective(p_principal, p_calendar);
 SELECT * INTO s FROM module_access_state WHERE principal_id=p_principal AND calendar_ref=p_calendar;
 known := FOUND;
 IF NOT known AND e.access='none' THEN RETURN; END IF;
 -- Nothing changed for the consumer: the same access, and for details the same expiry (a loss needs no `until`).
 IF known AND s.access=e.access AND (e.access='none' OR s.until_at IS NOT DISTINCT FROM e.until_at) THEN
  RETURN;
 END IF;
 UPDATE journal_head SET head=head+1 WHERE id=1 RETURNING head INTO h;
 INSERT INTO module_access_events(seq,calendar_ref,principal_id,access,reason,until_at)
  VALUES (h,p_calendar,p_principal,e.access,e.reason,e.until_at);
 INSERT INTO module_access_state(principal_id,calendar_ref,access,until_at) VALUES (p_principal,p_calendar,e.access,e.until_at)
  ON CONFLICT(principal_id,calendar_ref) DO UPDATE SET (access, until_at) = (excluded.access, excluded.until_at);
END $$;

-- The affected pairs of one row change: a binding names its own pair (also when it is deleted); a principal, membership,
-- grant or connection names the bound pairs it decides.
CREATE FUNCTION module_access_trigger() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $$
DECLARE j jsonb; pair record;
BEGIN
 FOR j IN SELECT x FROM unnest(ARRAY[CASE WHEN TG_OP<>'INSERT' THEN to_jsonb(OLD) END,
   CASE WHEN TG_OP<>'DELETE' THEN to_jsonb(NEW) END]) AS x WHERE x IS NOT NULL LOOP
  IF TG_TABLE_NAME='agent_calendars' THEN
   PERFORM module_access_refresh(j->>'principal_id', j->>'calendar_ref');
   CONTINUE;
  END IF;
  FOR pair IN
   SELECT DISTINCT a.principal_id, a.calendar_ref FROM agent_calendars a WHERE
    CASE TG_TABLE_NAME
     WHEN 'principals' THEN a.principal_id=j->>'id'
     WHEN 'memberships' THEN a.principal_id IN (SELECT id FROM principals WHERE group_id=j->>'group_id'
       AND person_id=j->>'person_id')
     WHEN 'calendar_grants' THEN a.calendar_ref=j->>'calendar_ref' AND a.principal_id IN (SELECT id FROM principals
       WHERE group_id=j->>'group_id' AND person_id=j->>'person_id')
     WHEN 'connections' THEN a.calendar_ref IN (SELECT ref FROM calendars WHERE connection_id=j->>'id')
     ELSE false
    END
  LOOP
   PERFORM module_access_refresh(pair.principal_id, pair.calendar_ref);
  END LOOP;
 END LOOP;
 RETURN NULL;
END $$;

-- The worker creates journal_head's row with the journal's first write: that row journals the baseline of every binding,
-- in the same transaction and before the worker's own first entries.
CREATE FUNCTION module_access_baseline() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $$
DECLARE r record;
BEGIN
 FOR r IN SELECT principal_id, calendar_ref FROM agent_calendars ORDER BY 1,2 LOOP
  PERFORM module_access_refresh(r.principal_id, r.calendar_ref);
 END LOOP;
 RETURN NULL;
END $$;

REVOKE ALL ON FUNCTION module_access_effective(text,text), module_access_refresh(text,text), module_access_trigger(),
 module_access_baseline() FROM PUBLIC;
CREATE TRIGGER module_access_journal_started AFTER INSERT ON journal_head
 FOR EACH ROW EXECUTE FUNCTION module_access_baseline();
CREATE TRIGGER module_access_agent_calendars AFTER INSERT OR UPDATE OR DELETE ON agent_calendars
 FOR EACH ROW EXECUTE FUNCTION module_access_trigger();
CREATE TRIGGER module_access_principals AFTER INSERT OR UPDATE ON principals
 FOR EACH ROW EXECUTE FUNCTION module_access_trigger();
CREATE TRIGGER module_access_memberships AFTER UPDATE OR DELETE ON memberships
 FOR EACH ROW EXECUTE FUNCTION module_access_trigger();
CREATE TRIGGER module_access_calendar_grants AFTER INSERT OR UPDATE OR DELETE ON calendar_grants
 FOR EACH ROW EXECUTE FUNCTION module_access_trigger();
CREATE TRIGGER module_access_connections AFTER UPDATE ON connections
 FOR EACH ROW EXECUTE FUNCTION module_access_trigger();
SET LOCAL lock_timeout=DEFAULT;

-- The baseline: the state of every existing binding, so a consumer learns what was readable before this migration
-- (a no-op while the journal was never started: journal_head's row then journals it when it is created).
DO $$ DECLARE r record; BEGIN
 FOR r IN SELECT principal_id, calendar_ref FROM agent_calendars ORDER BY 1,2 LOOP
  PERFORM module_access_refresh(r.principal_id, r.calendar_ref);
 END LOOP;
END $$;
