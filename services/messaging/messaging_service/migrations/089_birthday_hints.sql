-- rollout: manual
-- Risk tier: critical by the computed rule only (docs/operations/production.md "Migration risk tiers": the new
-- function's body bumps the feed counter with UPDATE, as 055's journal_source_health does). It deletes or rewrites no
-- stored row: it creates two functions and replaces 055's subscription_hint_references with the same body plus one
-- reference field. Released by the hand route ("Migration releases" item 4) with a fresh verified backup.
-- ANYM-919: the contact.birthday hint. The matcher (subscription_role.sql: EXECUTE only) calls
-- journal_contact_birthdays with the owner's IANA zone (birthdays.py). The function itself derives the date from the
-- database clock: before 09:00 local time in that zone it journals nothing; from 09:00 it journals one message-less
-- row per active contact whose known birthday falls on tomorrow's local date (29 February on 28 February of a common
-- year), carrying the account, the private chat ID and the date only. The idempotency key is
-- contact.birthday:<account>:<user>:<year>, so a re-run the same day, or a second matcher, journals nothing more.
-- A schema-86 matcher looks for journal_contact_birthdays(date), which never exists, and keeps skipping the call.
SET LOCAL lock_timeout='2s';

-- The rows whose birthday falls on p_date: no grant to any role, called only by the definer function below.
CREATE OR REPLACE FUNCTION public.contact_birthdays_due(p_date date)
 RETURNS TABLE(account text, provider_user text, chat text)
 LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT b.account,b.provider_user,min(c.id)
 FROM public.contact_birthdays b JOIN public.chats c ON c.account=b.account AND c.provider_chat=b.provider_user
  AND c.chat_type='private' AND c.purged_at IS NULL
 WHERE b.state='known' AND (b.month=extract(month FROM p_date) AND b.day=extract(day FROM p_date)
  -- A common year has no 29 February: its last day of February is the 28th.
  OR b.month=2 AND b.day=29 AND extract(month FROM p_date)=2 AND extract(day FROM p_date)=28
   AND extract(day FROM make_date(extract(year FROM p_date)::integer,3,1)-1)=28)
 GROUP BY b.account,b.provider_user ORDER BY b.account,b.provider_user $$;
REVOKE ALL ON FUNCTION public.contact_birthdays_due(date) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.journal_contact_birthdays(p_zone text) RETURNS integer
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r record; seq bigint; journaled integer := 0; here timestamp; due date;
BEGIN
 -- A named zone of the server's time zone database only: no POSIX offset string, so no offset beyond a real zone's.
 IF p_zone IS NULL OR NOT EXISTS(SELECT FROM pg_catalog.pg_timezone_names WHERE name=p_zone) THEN
  RAISE EXCEPTION 'invalid_owner_zone';
 END IF;
 here := now() AT TIME ZONE p_zone;
 IF extract(hour FROM here) < 9 THEN
  RETURN 0;
 END IF;
 due := here::date+1;
 -- One caller at a time; a concurrent matcher skips this call.
 IF NOT pg_try_advisory_xact_lock(hashtextextended('contact-birthday-journal',0)) THEN
  RETURN 0;
 END IF;
 FOR r IN SELECT d.account,d.provider_user,d.chat FROM public.contact_birthdays_due(due) d
  WHERE NOT EXISTS(SELECT FROM public.events e WHERE e.event_key='contact.birthday:'||d.account||':'||d.provider_user
   ||':'||extract(year FROM due)::integer) LOOP
  UPDATE public.feed_counter SET value=value+1 WHERE id=1 RETURNING value INTO seq;
  INSERT INTO public.events(id,account,message,kind,revision,origin,snapshot,event_key)
   VALUES(seq,r.account,NULL,'contact.birthday',1,'live',
    jsonb_build_object('account',r.account,'chat',r.chat,'date',to_char(due,'YYYY-MM-DD')),
    'contact.birthday:'||r.account||':'||r.provider_user||':'||extract(year FROM due)::integer);
  journaled := journaled+1;
 END LOOP;
 RETURN journaled;
END $$;
REVOKE ALL ON FUNCTION public.journal_contact_birthdays(text) FROM PUBLIC;

-- 055's reference function, plus the birthday's date of contact.birthday (no name, no year of birth).
-- CREATE OR REPLACE keeps the owner and the delivery role's EXECUTE grant.
CREATE OR REPLACE FUNCTION subscription_hint_references(p_subscription uuid, p_after bigint, p_through bigint,
  p_accounts text[], p_chats text[])
 RETURNS TABLE(event bigint, kind text, origin text, account text, created_at timestamptz, evidence jsonb, total bigint)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT e.id,e.kind,e.origin,e.account,e.created_at,
  jsonb_strip_nulls(jsonb_build_object('chat',e.snapshot->'chat','sender',e.snapshot->'sender',
   'message',e.snapshot->'message','direction',e.snapshot->'direction',
   'metadata',jsonb_strip_nulls(jsonb_build_object('mentions_owner',e.snapshot->'metadata'->'mentions_owner',
    'reply_to_owner',e.snapshot->'metadata'->'reply_to_owner',
    'reply_to_provider_id',e.snapshot->'metadata'->'reply_to_provider_id')),
   'reply_evidence',jsonb_strip_nulls(jsonb_build_object('basis',e.snapshot->'reply_evidence'->'basis')),
   'send_id',CASE WHEN e.kind='send.failed' THEN e.snapshot->'send_id' END,
   'status',CASE WHEN e.kind IN ('send.failed','source.health') THEN e.snapshot->'status' END,
   'date',CASE WHEN e.kind='contact.birthday' THEN e.snapshot->'date' END)),
  count(*) OVER ()
 FROM public.subscription_matches m JOIN public.events e ON e.id=m.event
 WHERE m.subscription=p_subscription AND m.event>p_after AND m.event<=p_through AND e.account=ANY(p_accounts)
  AND (p_chats IS NULL OR e.snapshot->>'chat'=ANY(p_chats))
 ORDER BY m.event LIMIT 20 $$;
REVOKE ALL ON FUNCTION subscription_hint_references(uuid,bigint,bigint,text[],text[]) FROM PUBLIC;
