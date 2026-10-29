-- Operator-only installation after schema 57 (ANYM-562). The module router (ANYM-575) publishes
-- module events and reports module reachability through two definer functions and nothing else:
-- no table privilege, so it can neither write a message event, change a subscription source or
-- link, nor read message content.
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_module_router') THEN
        CREATE ROLE messaging_module_router NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA public TO messaging_module_router;
-- A restore runs without ACLs and reopens functions to PUBLIC: only this role keeps EXECUTE.
REVOKE ALL ON FUNCTION public.publish_module_event(text,text,text,text,text,timestamptz,jsonb,text,timestamptz),
    public.report_module_reachability(text,boolean),public.module_event_valid(text,text,text,text,jsonb,text,timestamptz)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.publish_module_event(text,text,text,text,text,timestamptz,jsonb,text,timestamptz)
    TO messaging_module_router;
GRANT EXECUTE ON FUNCTION public.report_module_reachability(text,boolean) TO messaging_module_router;
