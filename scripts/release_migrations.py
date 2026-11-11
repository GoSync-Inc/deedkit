"""Migration planning and the migration release of the deploy tool (ANYM-444, ANYM-449, ANYM-586, production.md
"Migration releases"), moved verbatim from production.py (W5-1d). A tool file (TOOL_FILES): production.py loads it from
its own directory and sets `p` to a view of its names (ToolNames) before this code runs, so every name of production.py
is read through `p` at call time, and production.py imports every name defined here. `production.<name>` stays the one
name callers use and tests patch."""
from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

p = globals().get("p")  # production.py's names, set by its tool_module() before this module runs
if p is None:
    raise ImportError("release_migrations.py is loaded by production.py (tool_module), never imported on its own")


def migration_classes(mirror, base, candidate, sql):
    """`<file> <class> <tier>` for each messaging or calendar migration `base..candidate` adds (ANYM-444, ANYM-743):
    what the candidate's classifier of that database gives the candidate's bytes. It only names; whatever fails here
    is `manual` with no tier."""
    named = []
    for pattern, classifier in ((p.MIGRATION, p.CLASSIFIER), (CALENDAR_MIGRATION, CALENDAR_CLASSIFIER)):
        if any(pattern.fullmatch(path) for path in sql):
            named += database_classes(mirror, base, candidate, pattern, classifier)
    return named


def database_classes(mirror, base, candidate, pattern, classifier):
    try:
        added = [path for path in p.run(["git", "-C", str(mirror), "diff", "--name-only", "--no-renames", "--diff-filter=A",
                                       "-z", base, candidate]).split("\0") if pattern.fullmatch(path)]
    except p.Refused:
        return []
    objects = [f"{candidate}:{path}" for path in added + [classifier]]
    try:
        # Sizes first: nothing over the input limit is read, and then no process starts.
        if sum(int(p.run(["git", "-C", str(mirror), "cat-file", "-s", obj])) for obj in objects) > p.CLASSIFY_INPUT_LIMIT:
            raise ValueError("over the classifier input limit")
        blobs = [p.run(["git", "-C", str(mirror), "cat-file", "blob", obj], binary=True) for obj in objects]
        if sum(map(len, blobs)) > p.CLASSIFY_INPUT_LIMIT:
            raise ValueError("over the classifier input limit")
        *texts, source = [data.decode() for data in blobs]
        classes = p.classifier_child(p.CLASSIFY, {"source": source, "texts": texts})
    except Exception:  # Refused, OSError, a timeout, a limit, RecursionError of deep JSON: all name `manual`
        classes = None
    if not (isinstance(classes, list) and len(classes) == len(added) and all(isinstance(c, str) and p.NAMED_CLASS.fullmatch(c) for c in classes)):
        classes = ["manual"] * len(added)
    return [f"{pattern.fullmatch(path)[1]} {kind}" for path, kind in zip(added, classes)]


def pause_text(sql, pending, classes=()):
    reasons = []
    if sql:
        reasons.append("the SQL inventory changed (" + ", ".join(sql[:5]) + "); migrate by hand (production.md)"
                       + ("; added migrations: " + ", ".join(classes) if classes else ""))
    if pending:
        reasons.append("deploy tools changed (" + ", ".join(pending[:5]) + "); install them by hand (production.md)")
    return reasons


def pause_reasons(mirror, base, candidate, installed):
    """Why `base..candidate` needs a human: SQL inventory changes, each added migration's class, and deploy tools
    that are not installed yet."""
    sql, pending = p.range_changes(mirror, base, candidate, installed)
    return pause_text(sql, pending, migration_classes(mirror, base, candidate, sql))


# Unattended migrations (ANYM-449, production.md "Migration releases"). A messaging range whose every added migration
# the candidate's classifier names `expand` and whose every changed role file only appends grants (ANYM-596) runs in
# fixed phases: admission, preparation, the producer check, a verified copy (ANYM-588), a rehearsal on it, the final
# gate, one bounded `migrate-expand` on production, then plan A and plan B. Anything else pauses as before. A
# migration is never reversed, retried or followed by a restore; `<sha>/migration.json` records each step.
AUTO_DATABASE = "messaging"
PACKAGE = "services/messaging/messaging_service/"
ROLE_FILE = re.compile(re.escape(PACKAGE) + r"[A-Za-z0-9_]+_role\.sql")
MIGRATION_NAME = re.compile(r"([0-9]{3})_[A-Za-z0-9_]+\.sql")
BACKUP_SOURCE, STORE_SOURCE = PACKAGE + "backup.py", PACKAGE + "store.py"
BACKUP_SCHEMAS = re.compile(r"^SCHEMAS=range\(([0-9]+),([0-9]+)\)$", re.M)
# #352: the running Store prepares no statement server-side, so its cached `SELECT *` survives an ADD COLUMN.
UNPREPARED = re.compile(r"ConnectionPool\([^)]*'prepare_threshold': None")
HOST_PATH = re.compile(r"/[A-Za-z0-9_.@/-]{1,255}")
MIGRATION_WAITS = 3
MIGRATE_TIMEOUT = 30
REHEARSAL_LIMIT_MS = 1000
EXPAND_CODES = ("busy", "settings_refused", "plan_invalid", "ledger_mismatch", "not_unattended", "role_not_additive",
                "prefix_mismatch", "identity_mismatch", "lock_timeout", "statement_timeout", "transaction_timeout", "failed")
MIGRATE_FLAGS = ("--log-driver", "none", "--read-only", "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt",
                 "no-new-privileges", "--pids-limit", "64", "--memory", "512m")
# The candidate's classifiers in the same isolated child as CLASSIFY: each added file's `unattended_class` and each
# changed role file's `role_class` with the tables the range creates, as migrate-expand runs them.
UNATTENDED = p.CLASSIFY_PRELUDE + '''created, classes = {}, []
for text in data["texts"]:
    classes.append(module["unattended_class"](text))
    created = module["new_tables"](text, created)
print(json.dumps({"files": classes, "roles": [module["role_class"](old, new, created) for old, new in data["roles"]],
                  "tables": sorted(created)}))
'''
# M3's canonical catalog: one JSON line {"k": key, "v": value} per object, by name, never by OID; values hold
# definitions, owners and grants, never a row. Dependency catalogs (pg_depend, pg_shdepend) follow these objects.
CATALOG = r"""WITH ns AS (
  SELECT oid, nspname, nspowner, nspacl FROM pg_namespace
   WHERE nspname NOT IN ('pg_catalog', 'information_schema') AND nspname !~ '^pg_(toast|temp_|toast_temp_)'),
rel AS (
  SELECT c.oid, c.relkind, c.relowner, c.relacl, c.relrowsecurity, c.relforcerowsecurity, c.reloptions,
         c.relpersistence, c.relispartition, format('%I.%I', ns.nspname, c.relname) AS name
    FROM pg_class c JOIN ns ON ns.oid = c.relnamespace),
typ AS (
  SELECT t.oid, t.typtype, t.typowner, t.typacl, t.typbasetype, t.typtypmod, t.typdefault, t.typnotnull,
         format('%I.%I', ns.nspname, t.typname) AS name
    FROM pg_type t JOIN ns ON ns.oid = t.typnamespace
   WHERE t.typsubscript <> 'array_subscript_handler'::regproc
     AND NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.oid = t.typrelid AND c.relkind <> 'c')),
fn AS (
  SELECT p.oid, p.prokind, p.proowner, p.proacl,
         format('%I.%I(%s)', ns.nspname, p.proname, pg_get_function_identity_arguments(p.oid)) AS name
    FROM pg_proc p JOIN ns ON ns.oid = p.pronamespace),
acl(object, item) AS (
  SELECT json_build_array('schema', nspname)::text, aclexplode(coalesce(nspacl, acldefault('n', nspowner))) FROM ns
  UNION ALL SELECT json_build_array('relation', name)::text,
         aclexplode(coalesce(relacl, acldefault(CASE relkind WHEN 'S' THEN 's' ELSE 'r' END::"char", relowner))) FROM rel
  UNION ALL SELECT json_build_array('column', rel.name, a.attname)::text, aclexplode(a.attacl)
    FROM pg_attribute a JOIN rel ON rel.oid = a.attrelid WHERE a.attnum > 0 AND NOT a.attisdropped AND a.attacl IS NOT NULL
  UNION ALL SELECT json_build_array('function', name)::text, aclexplode(coalesce(proacl, acldefault('f', proowner))) FROM fn
  UNION ALL SELECT json_build_array('type', name)::text, aclexplode(coalesce(typacl, acldefault('T', typowner))) FROM typ
  UNION ALL SELECT json_build_array('database')::text, aclexplode(coalesce(datacl, acldefault('d', datdba)))
    FROM pg_database WHERE datname = current_database())
SELECT line FROM (
  SELECT json_build_object('k', json_build_array('schema', nspname), 'v', json_build_array(pg_get_userbyid(nspowner)))::text AS line
    FROM ns
  UNION ALL SELECT json_build_object('k', json_build_array('relation', name), 'v', json_build_array(relkind, relrowsecurity,
         relforcerowsecurity, reloptions, pg_get_userbyid(relowner), relpersistence, relispartition))::text FROM rel
  UNION ALL SELECT json_build_object('k', json_build_array('column', rel.name, a.attname), 'v', json_build_array(
         format_type(a.atttypid, a.atttypmod), a.attnotnull, pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated,
         (SELECT collname FROM pg_collation WHERE oid = a.attcollation), d.adbin IS NOT NULL
         AND NOT EXISTS (SELECT 1 FROM regexp_matches(d.adbin::text, '\{([A-Z_]+)', 'g') node
                          WHERE node[1] NOT IN ('CONST', 'FUNCEXPR', 'RELABELTYPE'))
         AND NOT EXISTS (SELECT 1 FROM regexp_matches(d.adbin::text, ':funcid ([0-9]+)', 'g') called
                          LEFT JOIN pg_proc f ON f.oid = called[1]::oid
                          WHERE f.oid IS NULL OR f.oid >= 16384 OR f.provolatile <> 'i')))::text
    FROM pg_attribute a JOIN rel ON rel.oid = a.attrelid LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
   WHERE a.attnum > 0 AND NOT a.attisdropped AND rel.relkind IN ('r', 'p', 'v', 'm', 'f', 'c')
  UNION ALL SELECT json_build_object('k', json_build_array('constraint', coalesce(rel.name, typ.name), co.conname), 'v',
         json_build_array(co.contype, pg_get_constraintdef(co.oid), co.convalidated, ARRAY(SELECT attname FROM pg_attribute
         WHERE attrelid = co.conrelid AND attnum = ANY (co.conkey) ORDER BY attnum)))::text
    FROM pg_constraint co JOIN ns ON ns.oid = co.connamespace LEFT JOIN rel ON rel.oid = co.conrelid
    LEFT JOIN typ ON typ.oid = co.contypid
  UNION ALL SELECT json_build_object('k', json_build_array('index', i.name), 'v', json_build_array(pg_get_indexdef(x.indexrelid),
         t.name, x.indisvalid))::text FROM pg_index x JOIN rel i ON i.oid = x.indexrelid JOIN rel t ON t.oid = x.indrelid
  UNION ALL SELECT json_build_object('k', json_build_array('trigger', rel.name, tg.tgname), 'v', json_build_array(
         pg_get_triggerdef(tg.oid), tg.tgenabled))::text FROM pg_trigger tg JOIN rel ON rel.oid = tg.tgrelid WHERE NOT tg.tgisinternal
  UNION ALL SELECT json_build_object('k', json_build_array('function', name), 'v', json_build_array(prokind, pg_get_userbyid(proowner),
         md5(CASE WHEN prokind IN ('f', 'p') THEN pg_get_functiondef(oid) ELSE '' END)))::text FROM fn
  UNION ALL SELECT json_build_object('k', json_build_array('type', name), 'v', json_build_array(typtype, pg_get_userbyid(typowner),
         format_type(typbasetype, typtypmod), typdefault, typnotnull,
         ARRAY(SELECT enumlabel FROM pg_enum WHERE enumtypid = typ.oid ORDER BY enumsortorder)))::text FROM typ
  UNION ALL SELECT json_build_object('k', json_build_array('rule', rel.name, r.rulename), 'v', json_build_array(
         md5(pg_get_ruledef(r.oid)), r.ev_enabled))::text FROM pg_rewrite r JOIN rel ON rel.oid = r.ev_class
  UNION ALL SELECT json_build_object('k', json_build_array('policy', rel.name, po.polname), 'v', json_build_array(po.polpermissive,
         po.polcmd, ARRAY(SELECT CASE WHEN g = 0 THEN 'public' ELSE pg_get_userbyid(g)::text END FROM unnest(po.polroles) g
         ORDER BY 1), pg_get_expr(po.polqual, po.polrelid), pg_get_expr(po.polwithcheck, po.polrelid)))::text
    FROM pg_policy po JOIN rel ON rel.oid = po.polrelid
  UNION ALL SELECT json_build_object('k', json_build_array('sequence', rel.name), 'v', json_build_array(format_type(s.seqtypid, NULL),
         s.seqstart, s.seqincrement, s.seqmax, s.seqmin, s.seqcache, s.seqcycle))::text
    FROM pg_sequence s JOIN rel ON rel.oid = s.seqrelid
  UNION ALL SELECT json_build_object('k', json_build_array('acl', object, pg_get_userbyid((item).grantor),
         CASE WHEN (item).grantee = 0 THEN 'public' ELSE pg_get_userbyid((item).grantee)::text END, (item).privilege_type),
         'v', (item).is_grantable)::text FROM acl
  UNION ALL SELECT json_build_object('k', json_build_array('default_acl', pg_get_userbyid(defaclrole),
         CASE WHEN defaclnamespace = 0 THEN '' ELSE defaclnamespace::regnamespace::text END, defaclobjtype, defaclacl::text),
         'v', true)::text FROM pg_default_acl
  UNION ALL SELECT json_build_object('k', json_build_array('role', rolname), 'v', json_build_array(rolsuper, rolinherit, rolcreaterole,
         rolcreatedb, rolcanlogin, rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil))::text
    FROM pg_roles WHERE rolname !~ '^pg_'
  UNION ALL SELECT json_build_object('k', json_build_array('membership', pg_get_userbyid(roleid), pg_get_userbyid(member),
         pg_get_userbyid(grantor)), 'v', json_build_array(admin_option, inherit_option, set_option))::text FROM pg_auth_members
  UNION ALL SELECT json_build_object('k', json_build_array('role_setting', coalesce(d.datname::text, ''),
         pg_get_userbyid(s.setrole)), 'v', s.setconfig)::text
    FROM pg_db_role_setting s LEFT JOIN pg_database d ON d.oid = s.setdatabase
  UNION ALL SELECT json_build_object('k', json_build_array('count', name), 'v', rows)::text FROM (VALUES
         ('pg_extension', (SELECT count(*) FROM pg_extension)), ('pg_publication', (SELECT count(*) FROM pg_publication)),
         ('pg_publication_rel', (SELECT count(*) FROM pg_publication_rel)),
         ('pg_publication_namespace', (SELECT count(*) FROM pg_publication_namespace)),
         ('pg_subscription', (SELECT count(*) FROM pg_subscription)),
         ('pg_event_trigger', (SELECT count(*) FROM pg_event_trigger)), ('pg_cast', (SELECT count(*) FROM pg_cast)),
         ('pg_operator', (SELECT count(*) FROM pg_operator)), ('pg_opclass', (SELECT count(*) FROM pg_opclass)),
         ('pg_opfamily', (SELECT count(*) FROM pg_opfamily)), ('pg_am', (SELECT count(*) FROM pg_am)),
         ('pg_language', (SELECT count(*) FROM pg_language)),
         ('pg_foreign_data_wrapper', (SELECT count(*) FROM pg_foreign_data_wrapper)),
         ('pg_foreign_server', (SELECT count(*) FROM pg_foreign_server)),
         ('pg_user_mappings', (SELECT count(*) FROM pg_user_mappings)),
         ('pg_statistic_ext', (SELECT count(*) FROM pg_statistic_ext)), ('pg_transform', (SELECT count(*) FROM pg_transform)),
         ('pg_conversion', (SELECT count(*) FROM pg_conversion)), ('pg_collation', (SELECT count(*) FROM pg_collation)),
         ('pg_ts_config', (SELECT count(*) FROM pg_ts_config)), ('pg_ts_dict', (SELECT count(*) FROM pg_ts_dict)),
         ('pg_ts_parser', (SELECT count(*) FROM pg_ts_parser)), ('pg_ts_template', (SELECT count(*) FROM pg_ts_template))
         ) AS counted(name, rows)) AS lines ORDER BY line;"""
# M3's data ledger on the copy: probe_metadata (values other than schema_version only as digests), archive_migrations
# and each table's write counters.
MIGRATION_DATA = r"""SELECT line FROM (
  SELECT json_build_object('k', json_build_array('metadata', name), 'v',
         CASE WHEN name = 'schema_version' THEN value ELSE md5(value) END)::text AS line FROM probe_metadata
  UNION ALL SELECT json_build_object('k', json_build_array('ledger', version::text), 'v', sha256)::text FROM archive_migrations
  UNION ALL SELECT json_build_object('k', json_build_array('writes', format('%I.%I', schemaname, relname)), 'v',
         json_build_array(n_tup_ins, n_tup_upd, n_tup_del))::text FROM pg_stat_user_tables
  UNION ALL SELECT json_build_object('k', json_build_array('rows', format('%I.%I', schemaname, relname)), 'v',
         (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', schemaname, relname), false,
                                              true, '')))[1]::text)::text FROM pg_stat_user_tables
  UNION ALL SELECT json_build_object('k', json_build_array('attnum', format('%I.%I', s.schemaname, s.relname), a.attname),
         'v', a.attnum)::text FROM pg_stat_user_tables s JOIN pg_attribute a ON a.attrelid = s.relid
   WHERE a.attnum > 0 AND NOT a.attisdropped) AS lines ORDER BY line;"""
LEDGER = r"""SELECT json_build_object('schema_version', (SELECT value FROM probe_metadata WHERE name = 'schema_version'),
  'applied', (SELECT coalesce(json_object_agg(version::text, sha256 ORDER BY version), '{}'::json) FROM archive_migrations))::text;"""
OTHER_SESSIONS = "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' AND pid <> pg_backend_pid();"


class MigrationWait(p.Refused):
    """A migration gate that waits: nothing changed, the next run retries, the third wait in a row pauses."""


class MigrationRefused(p.Refused):
    """A migration gate or phase that pauses with this reason; the reason holds names, versions and codes only."""


def rehearsal_limit(settings):
    section = settings.get("migration")
    limit = section.get("rehearsal_limit_ms", REHEARSAL_LIMIT_MS) if isinstance(section, dict) else REHEARSAL_LIMIT_MS
    if type(limit) is not int or not 1 <= limit <= 4000:
        raise p.Refused("migration.rehearsal_limit_ms must be 1 to 4000")
    return limit


def unattended_settings(settings):
    """`migration.unattended` {network, dsn_file, runner_config}: the database's Docker network, the owner DSN file
    the migration container mounts and the backup runner's runner.json; with `migration.rehearsal_limit_ms`."""
    section = settings.get("migration")
    found = section.get("unattended") if isinstance(section, dict) else None
    if (not isinstance(found, dict) or set(found) != {"network", "dsn_file", "runner_config"}
            or not all(isinstance(value, str) for value in found.values()) or not p.DOCKER_NAME.fullmatch(found["network"])
            or not all(HOST_PATH.fullmatch(found[key]) and ".." not in found[key].split("/")
                       for key in ("dsn_file", "runner_config"))):
        raise p.Refused("migration.unattended must name the database network, the owner DSN file and runner.json")
    return {**found, "rehearsal_limit_ms": rehearsal_limit(settings)}


# Calendar (ANYM-586 K3): the same phases for the calendar database. Its migrations are the service package's
# `schema.sql` (version 1) and `schema_vN.sql`, its grants the whole `roles.sql`; the candidate's calendar image runs
# `family-calendar --home /data service migrate-expand`, then `db-roles` and the read-only `db-roles --check`, with
# the owner's runtime.json mounted read-only. In a range touching both databases calendar goes first.
CALENDAR = "calendar"
CALENDAR_PACKAGE = "services/calendar/family_calendar/service/"
CALENDAR_FILE = re.compile(r"schema(?:_v([1-9][0-9]*))?\.sql")
CALENDAR_ROLES = CALENDAR_PACKAGE + "roles.sql"
CALENDAR_CLASSIFIER, CALENDAR_STORE = CALENDAR_PACKAGE + "migration_rollout.py", CALENDAR_PACKAGE + "store.py"
CALENDAR_MIGRATION = re.compile(re.escape(CALENDAR_PACKAGE) + r"(schema_v[0-9]+\.sql)")
CALENDAR_TARGET = "calendar"
CALENDAR_RUNTIME = "/data/service/runtime.json"
# K0: the calendar Store's pool prepares no statement server-side either.
CALENDAR_UNPREPARED = re.compile(r"ConnectionPool\([^)]*\"prepare_threshold\": None")
CALENDAR_CODES = EXPAND_CODES + ("database_unavailable", "usage", "db_roles_drift", "existing_runtime_configuration_required",
                                 "unsafe_private_path", "unsafe_private_permissions")
# The candidate's calendar classifier (`plan`, K1) in the isolated child; hashlib is imported before the audit hook.
CALENDAR_PLAN = "import hashlib\n" + p.CLASSIFY_PRELUDE + '''base, after = ({int(v): t for v, t in side.items()} for side in data["files"])
found, created = module["plan"](base, after, *data["roles"]), {}
for version in sorted(set(after) - set(base)):
    created = module["new_tables"](after[version], created)
print(json.dumps({"plan": found, "tables": sorted(created)}))
'''
CALENDAR_LEDGER = r"""SELECT json_build_object('schema_version', coalesce(max(version), 0),
  'applied', coalesce(json_object_agg(version::text, checksum ORDER BY version), '{}'::json))::text FROM fcs.migrations;"""
# The calendar data ledger on the copy: fcs.migrations and each table's write counters.
CALENDAR_DATA = r"""SELECT line FROM (
  SELECT json_build_object('k', json_build_array('ledger', version::text), 'v', checksum)::text AS line FROM fcs.migrations
  UNION ALL SELECT json_build_object('k', json_build_array('writes', format('%I.%I', schemaname, relname)), 'v',
         json_build_array(n_tup_ins, n_tup_upd, n_tup_del))::text FROM pg_stat_user_tables
  UNION ALL SELECT json_build_object('k', json_build_array('rows', format('%I.%I', schemaname, relname)), 'v',
         (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', schemaname, relname), false,
                                              true, '')))[1]::text)::text FROM pg_stat_user_tables
  UNION ALL SELECT json_build_object('k', json_build_array('attnum', format('%I.%I', s.schemaname, s.relname), a.attname),
         'v', a.attnum)::text FROM pg_stat_user_tables s JOIN pg_attribute a ON a.attrelid = s.relid
   WHERE a.attnum > 0 AND NOT a.attisdropped) AS lines ORDER BY line;"""
# fcs.migrations has no applied_at: the rehearsal's transaction time is the database's active and idle-in-transaction
# milliseconds its session added (counted at its exit), plus the few reads between (an overestimate).
BUSY = ("SELECT round(active_time + idle_in_transaction_time) FROM pg_stat_database "
        "WHERE datname = current_database();")


def calendar_settings(settings):
    """`migration.unattended_calendar` {network, runtime_file}: the calendar database's Docker network and the
    owner's runtime.json (`<CALENDAR_STATE_DIR>/service/runtime.json`, the image user's own 0600 file)."""
    section = settings.get("migration")
    found = section.get("unattended_calendar") if isinstance(section, dict) else None
    if (not isinstance(found, dict) or set(found) != {"network", "runtime_file"}
            or not all(isinstance(value, str) for value in found.values()) or not p.DOCKER_NAME.fullmatch(found["network"])
            or not HOST_PATH.fullmatch(found["runtime_file"]) or ".." in found["runtime_file"].split("/")):
        raise p.Refused("migration.unattended_calendar must name the calendar database network and its runtime.json")
    return dict(found)


def calendar_plan(settings, mirror, base, candidate, sql):
    """K1 in deploy: the calendar plan of `base..candidate` (`sql` holds its calendar SQL paths), else None. The
    configuration has `migration.databases.calendar` and `migration.unattended_calendar`; every path is an added
    `schema_vN.sql` or the modified roles.sql; the candidate's `migration_rollout.plan` qualifies the range."""
    try:
        p.copy_source(settings, CALENDAR)
        calendar_settings(settings)
        rehearsal_limit(settings)
    except p.Refused:
        return None
    git = ["git", "-C", str(mirror)]
    try:
        def changed(kind):
            return set(p.run([*git, "diff", "--name-only", "--no-renames", "--diff-filter=" + kind, "-z", base,
                            candidate]).split("\0")) - {""}
        def content(sha, path):
            return p.run([*git, "cat-file", "blob", f"{sha}:{path}"], binary=True)
        def versions(sha):
            names = set(p.run([*git, "ls-tree", "--name-only", "-z", f"{sha}:{CALENDAR_PACKAGE}"]).split("\0")) - {""}
            found = {int(match[1] or 1): name for name in names if (match := CALENDAR_FILE.fullmatch(name))}
            if "schema_v1.sql" in names or "roles.sql" not in names:
                raise ValueError("schema_v1.sql or no roles.sql")
            return found
        added, modified = changed("A"), changed("M")
        before, after = versions(base), versions(candidate)
        pending = sorted(set(after) - set(before))
        files = {CALENDAR_PACKAGE + after[version] for version in pending}
        if (set(sql) - files - {CALENDAR_ROLES} or not files <= added & set(sql)
                or (CALENDAR_ROLES in sql and CALENDAR_ROLES not in modified)
                or any(after.get(version) != name for version, name in before.items())):
            return None
        texts = [{version: content(sha, CALENDAR_PACKAGE + names[version]) for version in names}
                 for sha, names in ((base, before), (candidate, after))]
        roles = [content(base, CALENDAR_ROLES), content(candidate, CALENDAR_ROLES)]
        source = content(candidate, CALENDAR_CLASSIFIER)
        if sum(map(len, [source, *roles, *texts[0].values(), *texts[1].values()])) > p.CLASSIFY_INPUT_LIMIT:
            return None
        found = p.classifier_child(CALENDAR_PLAN, {
            "source": source.decode(), "roles": [data.decode() for data in roles],
            "files": [{str(version): data.decode() for version, data in side.items()} for side in texts]})
        plan = found.get("plan") if isinstance(found, dict) else None
        if (not isinstance(plan, dict) or set(plan) != {"base", "target", "roles"}
                or (plan["base"], plan["target"]) != (max(before), max(after))
                or plan["roles"] != {"bytes": len(roles[0]), "sha256": hashlib.sha256(roles[0]).hexdigest()}
                or not isinstance(found.get("tables"), list)
                or not all(isinstance(table, str) and p.SQL_NAME.fullmatch(table) for table in found["tables"])):
            return None
    except Exception:  # Refused, a decode error, the child's bounds: the range pauses as before
        return None
    return {"database": CALENDAR, "base": plan["base"], "target": plan["target"],
            "files": [after[version] for version in pending],
            "roles_changed": ["roles.sql"] if CALENDAR_ROLES in sql else [],
            "pending": {str(version): hashlib.sha256(texts[1][version]).hexdigest() for version in pending},
            "ledger": {str(version): hashlib.sha256(data).hexdigest() for version, data in sorted(texts[0].items())},
            "tables": found["tables"], "roles": plan["roles"]}


def migration_plans(settings, mirror, base, candidate, sql):
    """M1 for every database the range touches (K3): [calendar's plan, messaging's plan], calendar first, each for its
    own SQL paths; None when either does not qualify (the range pauses as before)."""
    calendar = [path for path in sql if path.startswith(CALENDAR_PACKAGE)]
    messaging = [path for path in sql if path not in calendar]
    plans = ([calendar_plan(settings, mirror, base, candidate, calendar)] if calendar else []) + (
        [unattended_plan(settings, mirror, base, candidate, messaging)] if messaging else [])
    return plans if plans and all(plans) else None


def unattended_plan(settings, mirror, base, candidate, sql):
    """M1: the plan of `base..candidate` when `migrate-expand` may run it without a person, else None (the pause
    names the range as before). The plan A configuration has `migration.databases.messaging` and
    `migration.unattended`; every SQL inventory path is an added messaging migration or a modified messaging role
    file; the candidate's classifier names each added file `expand` and each role file `additive`; the added
    versions follow the base's highest without a gap. Whatever fails here is None."""
    try:
        p.copy_source(settings, AUTO_DATABASE)
        unattended_settings(settings)
    except p.Refused:
        return None
    git = ["git", "-C", str(mirror)]
    try:
        def changed(kind):
            return set(p.run([*git, "diff", "--name-only", "--no-renames", "--diff-filter=" + kind, "-z", base,
                            candidate]).split("\0")) - {""}
        def tree(sha, path):
            return set(p.run([*git, "ls-tree", "--name-only", "-z", f"{sha}:{path}"]).split("\0")) - {""}
        def content(sha, path):
            return p.run([*git, "cat-file", "blob", f"{sha}:{path}"], binary=True)
        def versions(sha):
            names = [name for name in tree(sha, PACKAGE + "migrations") if MIGRATION_NAME.fullmatch(name)]
            found = {int(name[:3]): name for name in names}
            if len(found) != len(names):
                raise ValueError("two migrations share a version")
            return found
        added, modified = changed("A"), changed("M")
        files = [path for path in sql if path in added and p.MIGRATION.fullmatch(path)]
        roles = sorted(path for path in sql if path in modified and ROLE_FILE.fullmatch(path))
        if len(files) + len(roles) != len(sql):
            return None
        before, after = versions(base), versions(candidate)
        start, target = max(before), max(after)
        pending = sorted(set(after) - set(before))
        if (any(after.get(version) != name for version, name in before.items())
                or pending != list(range(start + 1, target + 1))
                or {PACKAGE + "migrations/" + after[version] for version in pending} != set(files)):
            return None
        role_names = {name for name in tree(candidate, PACKAGE) if name.endswith("_role.sql")}
        if role_names != {name for name in tree(base, PACKAGE) if name.endswith("_role.sql")}:
            return None
        texts = {version: content(candidate, PACKAGE + "migrations/" + after[version]) for version in pending}
        prefixes = {name: content(base, PACKAGE + name) for name in sorted(role_names)}
        appended = [(prefixes[path[len(PACKAGE):]], content(candidate, path)) for path in roles]
        source = content(candidate, p.CLASSIFIER)
        if sum(map(len, [source, *texts.values(), *(new for _, new in appended)])) > p.CLASSIFY_INPUT_LIMIT:
            return None
        found = p.classifier_child(UNATTENDED, {"source": source.decode(),
                                              "texts": [texts[version].decode() for version in pending],
                                              "roles": [[old.decode(), new.decode()] for old, new in appended]})
        if (not isinstance(found, dict) or found.get("files") != ["expand"] * len(pending)
                or found.get("roles") != ["additive"] * len(roles) or not isinstance(found.get("tables"), list)
                or not all(isinstance(table, str) and p.SQL_NAME.fullmatch(table) for table in found["tables"])):
            return None
        ledger = {str(version): hashlib.sha256(content(base, PACKAGE + "migrations/" + name)).hexdigest()
                  for version, name in sorted(before.items()) if version > 1}
    except Exception:  # Refused, a decode error, the child's bounds: the range pauses as before
        return None
    return {"database": AUTO_DATABASE, "base": start, "target": target,
            "files": [after[version] for version in pending], "roles_changed": [path[len(PACKAGE):] for path in roles],
            "pending": {str(version): hashlib.sha256(texts[version]).hexdigest() for version in pending},
            "ledger": ledger, "tables": found["tables"],
            "roles": {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                      for name, data in prefixes.items()}}


def expand_plan(plan, identity):
    """The plan file migrate-expand reads (ANYM-596), with the identity of the database it must reach (ANYM-449)."""
    return {"base": plan["base"], "target": plan["target"], "roles": plan["roles"], "identity": identity}


def live_lines(source, step, text):
    """The lines `text` prints on the live database, through `docker exec` as its owner (no DSN leaves the
    container); stderr is discarded."""
    status, output = p.bounded(["docker", "exec", "-i", "-e", f"PGAPPNAME=deedkit-migration-{step}", source["container"],
                              "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", source["owner"], "-d",
                              source["database"]], timeout=60, text=text)
    if status != 0:
        raise MigrationRefused(f"the {source['database']} database could not be read ({step})")
    return [line for line in output.splitlines() if line]


def live_ledger(source):
    """(schema_version, {version: sha256}) of the live database."""
    try:
        found = json.loads(p.live_lines(source, "ledger", LEDGER)[0])
        return int(found["schema_version"]), dict(found["applied"])
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise MigrationRefused(f"the live {AUTO_DATABASE} ledger could not be read") from exc


def at_target(plan, version, applied):
    """A messaging range a person already migrated (production.md "Migration releases" item 4): the live ledger is
    exactly the target's (its version, the base's and the range's own checksums), so the release copies and migrates
    nothing. Never with a role file change, whose grants the ledger does not show, and never for calendar."""
    return (plan["database"] == AUTO_DATABASE and not plan["roles_changed"] and version == plan["target"]
            and applied == {**plan["ledger"], **plan["pending"]})


def migrated_by_hand(settings, plan):
    """`at_target` from a read of its own, for a run that finds an earlier run's refusal; a read that fails is False,
    so that refusal stands."""
    if plan["database"] != AUTO_DATABASE or plan["roles_changed"]:
        return False
    try:
        return at_target(plan, *live_ledger(p.copy_source(settings, AUTO_DATABASE)[0]))
    except Exception:
        return False


def calendar_ledger(source):
    """(max version, {version: checksum}) of the live calendar ledger, fcs.migrations."""
    try:
        found = json.loads(p.live_lines(source, "calendar ledger", CALENDAR_LEDGER)[0])
        return int(found["schema_version"]), dict(found["applied"])
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise MigrationRefused("the live calendar ledger could not be read") from exc


def catalog_map(lines):
    """{key: value} of CATALOG or MIGRATION_DATA lines; keys are tuples of names."""
    rows = {}
    for line in lines:
        row = json.loads(line)
        rows[tuple(row["k"])] = row["v"]
    return rows


def digested(rows):
    """{key: sha256 of its value}: what migration-catalog.json keeps and what recovery compares; a catalog value
    (a default, a policy expression, a role setting) never reaches a file (Astra r2 F10)."""
    return {key: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() for key, value in rows.items()}


def catalog_difference(before, after):
    """What changed between two catalog maps, canonically ordered: what the rehearsal and production compare."""
    order = json.dumps
    return {"removed": sorted((list(key) for key in before.keys() - after.keys()), key=order),
            "changed": sorted(([list(key), after[key]] for key in before.keys() & after.keys()
                               if before[key] != after[key]), key=order),
            "added": sorted(([list(key), after[key]] for key in after.keys() - before.keys()), key=order)}


def described(key):
    return " ".join(str(part) for part in key)


def destroys_data(before, after):
    """M3 (ANYM-743; was ANYM-449's `not_additive`, owner 11-04: backups and pauses only in critical cases): the first
    catalog difference of the rehearsal that loses stored data or breaks the running release's writes, or None. That
    is a removed table, partitioned table, materialized view or column (a rename reads as one), a column whose type or
    generation expression changed, and a new NOT NULL column without a default on an existing table. Everything else
    a non-critical file does (indexes, constraints, functions, views, policies, grants, defaults, roles) passes;
    `data_unchanged` refuses a moved write counter, row count or column number."""
    change = catalog_difference(before, after)
    for key in change["removed"]:
        if key[0] == "column" or key[0] == "relation" and (before[tuple(key)] or [None])[0] in ("r", "p", "m"):
            return "removed " + described(key)
    for key, value in change["changed"]:
        old = before[tuple(key)]
        if key[0] == "column" and (old[0] != value[0] or old[4] != value[4] or value[4] and old[2] != value[2]):
            return "changed the type of " + described(key)
    for key, value in change["added"]:
        if (key[0] == "column" and value[1] and value[2] is None and not value[3] and not value[4]
                and (before.get(("relation", key[1])) or [None])[0] in ("r", "p")):
            return "added a NOT NULL column without a default to " + described(key)
    return None


def changed_data(key):
    """The reason a `writes`, `rows` or `attnum` line of the data check changed."""
    if key[0] == "writes": return f"rows of {key[1]} were written"
    if key[0] == "rows": return f"the row count of {key[1]} changed"
    return f"column {key[2]} of {key[1]} was dropped or replaced"


def data_unchanged(before, after, plan):
    """M3's data: the first reason the copy's data changed beyond the migration's own ledger rows, or None."""
    def metadata(rows):
        return {key: value for key, value in rows.items() if key[0] == "metadata" and key[1] != "schema_version"}
    def ledger(rows):
        return {key[1]: value for key, value in rows.items() if key[0] == "ledger"}
    if (metadata(before) != metadata(after) or before.get(("metadata", "schema_version")) != str(plan["base"])
            or after.get(("metadata", "schema_version")) != str(plan["target"])):
        return "probe_metadata changed beyond schema_version"
    if set(plan["pending"]) & set(ledger(before)) or ledger(after) != {**ledger(before), **plan["pending"]}:
        return "archive_migrations changed beyond the pending versions"
    for key, value in sorted(before.items(), key=json.dumps):
        # ANYM-743 (Astra r1 F1): a TRUNCATE moves no write counter, so the exact row count of every table counts too;
        # a column dropped and added again under its name has a new attnum and no values (Sol r2 F1).
        if key[0] in ("writes", "rows", "attnum") and key[1] not in ("public.probe_metadata", "public.archive_migrations") \
                and after.get(key) != value:
            return changed_data(key)
    return None


def calendar_data_unchanged(before, after, plan):
    """The calendar data check: the first reason the copy's data changed beyond the migration's own ledger rows."""
    def ledger(rows):
        return {key[1]: value for key, value in rows.items() if key[0] == "ledger"}
    if set(plan["pending"]) & set(ledger(before)) or ledger(after) != {**ledger(before), **plan["pending"]}:
        return "fcs.migrations changed beyond the pending versions"
    for key, value in sorted(before.items(), key=json.dumps):
        if key[0] in ("writes", "rows", "attnum") and key[1] != "fcs.migrations" and after.get(key) != value:
            return changed_data(key)
    return None


def scratch_lines(copy, copied, step, text):
    """The lines `text` prints on the copy's scratch database (names, digests and counts, never a row)."""
    try:
        return [line for line in p.helper_output(copy, p.copy_exec(
            copy, step, copied["scratch_container"], "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-h",
            "127.0.0.1", "-U", copied["scratch_user"], "-d", copied["database"]), timeout=60, failure="restore_failed",
            text=text).splitlines() if line]
    except p.CopyFailed as exc:
        raise MigrationRefused(f"the rehearsal could not read the copy ({step})") from exc


def expand_run(name, image, network, dsn_file, plan_file, *, label=None, status=False, timed=False):
    """One `migrate-expand` (or `--status`) container: the candidate's API image with MIGRATE_FLAGS, the DSN file
    and the plan mounted read-only, at most MIGRATE_TIMEOUT seconds; its last JSON line, or None when the answer is
    lost (a deadline kills the container). Its output holds a version or a fixed code, never PostgreSQL's message.
    `timed` (the rehearsal) adds `transaction_ms` to a version: the line before the answer, {"transaction_ms": N},
    the migrator's own monotonic time from its first pending statement to its commit's return; None when the image printed none."""
    mounts = [] if status else ["--mount", f"type=bind,src={plan_file},dst=/run/deedkit/plan.json,readonly"]
    before = []
    found = container_json(name, image, network, dsn_file, ["python", "-m", "messaging_service", "migrate-expand",
                                                            *(["--status"] if status else ["--plan", "/run/deedkit/plan.json"])],
                           label=label, mounts=mounts, before=before)
    if isinstance(found, dict) and set(found) == {"schema_version"} and type(found["schema_version"]) is int:
        if timed:
            try:
                line = json.loads(before[-1]) if len(before) == 1 else None
            except ValueError:
                line = None
            took = line.get("transaction_ms") if isinstance(line, dict) and set(line) == {"transaction_ms"} else None
            return {**found, "transaction_ms": took if type(took) is int and took >= 0 else None}
        return found
    if isinstance(found, dict) and set(found) == {"error"}:
        return {"error": found["error"] if found["error"] in EXPAND_CODES else "failed"}
    return None


def container_json(name, image, network, dsn_file, command, *, label=None, mounts=(), before=None):
    """The last stdout line, as JSON, of one candidate API container with MIGRATE_FLAGS, the DSN file mounted read-only
    and PGAPPNAME=<name> (its database session carries the name), at most MIGRATE_TIMEOUT seconds; None when the
    answer is lost (a deadline kills the container; `migration_settled` then confirms it gone). A `before` list
    gets the lines printed ahead of the last one."""
    argv = ["docker", "run", "--rm", "--name", name, *(["--label", label] if label else []), "--network", network,
            *MIGRATE_FLAGS, "-e", "DATABASE_URL_FILE=/run/secrets/database_url", "-e", "PGAPPNAME=" + name,
            "--mount", f"type=bind,src={dsn_file},dst=/run/secrets/database_url,readonly", *mounts, image, *command]
    code, output = p.bounded(argv, timeout=MIGRATE_TIMEOUT)
    if code is None:
        with suppress(Exception):
            p.run(["docker", "kill", name], timeout=30)
        return None
    lines = output.strip().splitlines() or ["null"]
    if before is not None:
        before.extend(lines[:-1])
    try:
        return json.loads(lines[-1])
    except ValueError:
        return None


def calendar_run(name, image, network, runtime_file, arguments, *, label=None, mounts=(), entrypoint=None):
    """(exit status, its JSON) of one candidate calendar container with MIGRATE_FLAGS, `runtime_file` mounted
    read-only as the image's runtime.json and PGAPPNAME=<name>, at most MIGRATE_TIMEOUT seconds; (None, None) when
    the answer is lost (a deadline kills the container). The JSON is the whole output (db-roles indents it) or else
    its last line."""
    argv = ["docker", "run", "--rm", "--name", name, *(["--label", label] if label else []), "--network", network,
            *MIGRATE_FLAGS, "-e", "PGAPPNAME=" + name, "--mount",
            f"type=bind,src={runtime_file},dst={CALENDAR_RUNTIME},readonly", *mounts,
            *(["--entrypoint", entrypoint] if entrypoint else []), image, *arguments]
    code, output = p.bounded(argv, timeout=MIGRATE_TIMEOUT)
    if code is None:
        with suppress(Exception):
            p.run(["docker", "kill", name], timeout=30)
        return None, None
    for text in (output, (output.strip().splitlines() or ["null"])[-1]):
        try:
            return code, json.loads(text)
        except ValueError:
            pass
    return code, None


def calendar_code(found):
    """A calendar answer's fixed code, never its details (a drift line names roles and tables)."""
    error = found.get("error") if isinstance(found, dict) else None
    return error if error in CALENDAR_CODES else "failed"


def calendar_expand(name, image, network, runtime_file, plan_file, *, label=None, status=False):
    """`service migrate-expand --plan` (or `--status`) in the calendar image: {"schema_version": N}, {"error": code}
    or None when the answer is lost, as expand_run."""
    mounts = [] if status else ["--mount", f"type=bind,src={plan_file},dst=/run/deedkit/plan.json,readonly"]
    _, found = p.calendar_run(name, image, network, runtime_file,
                            ["migrate-expand", *(["--status"] if status else ["--plan", "/run/deedkit/plan.json"])],
                            label=label, mounts=mounts)
    if isinstance(found, dict) and set(found) == {"schema_version"} and type(found["schema_version"]) is int:
        return found
    return {"error": calendar_code(found)} if isinstance(found, dict) and "error" in found else None


def calendar_identity(name, image, network, runtime_file):
    """[system identifier, database name, database OID] of the database the calendar runtime.json reaches, or None."""
    _, found = p.calendar_run(name, image, network, runtime_file, ["-c", CALENDAR_IDENTITY], entrypoint="python")
    return found if isinstance(found, list) and len(found) == 3 and all(isinstance(item, str) for item in found) else None


def calendar_roles(settings, candidate, image, target):
    """K3 after calendar's commit: `db-roles` (the whole roles.sql), then the read-only `db-roles --check` (ANYM-601);
    None when both exit 0 with their answer, else the first one's code."""
    found = calendar_settings(settings)
    for suffix, arguments in (("roles", ["db-roles"]), ("check", ["db-roles", "--check"])):
        code, answer = p.calendar_run(f"deedkit-calendar-{suffix}-{candidate[:12]}", image, found["network"],
                                    found["runtime_file"], arguments)
        if code is None:
            return "no answer"
        expected = (isinstance(answer, dict) and "db_roles" in answer if suffix == "roles"
                    else answer == {"db_roles_check": "ok", "schema_version": target})
        if code != 0 or not expected:
            return calendar_code(answer)
    return None


IDENTITY_SQL = ("SELECT json_build_array((SELECT system_identifier::text FROM pg_control_system()), current_database()::text,"
                " (SELECT oid::text FROM pg_database WHERE datname = current_database()))::text")
# The database the owner DSN file reaches, read through that DSN in the candidate image (psycopg; no psql there).
IDENTITY = ("import json, os, psycopg\n"
            "with psycopg.connect(open(os.environ['DATABASE_URL_FILE']).read().strip(), connect_timeout=5) as c:\n"
            f"    print(c.execute({IDENTITY_SQL!r}).fetchone()[0])\n")
# The calendar database the owner's runtime.json reaches, read in the candidate calendar image.
CALENDAR_IDENTITY = ("import json, psycopg\n"
                     f"dsn = json.load(open({CALENDAR_RUNTIME!r}))['dsn']\n"
                     "with psycopg.connect(dsn, connect_timeout=5) as c:\n"
                     f"    print(c.execute({IDENTITY_SQL!r}).fetchone()[0])\n")


def database_identity(name, image, network, dsn_file):
    """[system identifier, database name, database OID] of the database the DSN file reaches, or None."""
    found = container_json(name, image, network, dsn_file, ["python", "-c", IDENTITY])
    return found if isinstance(found, list) and len(found) == 3 and all(isinstance(item, str) for item in found) else None


def migration_settled(source, name):
    """True once the container `name` is gone and no live session carries its application name (read back for up to
    a minute), so a ledger read afterwards is final."""
    deadline = p.copy_clock() + 60
    while True:
        try:
            if not p.run(["docker", "ps", "-a", "-q", "--filter", f"name=^/{name}$"], timeout=30).split() and p.live_lines(
                    source, "settled", f"SELECT count(*) FROM pg_stat_activity WHERE application_name = '{name}';") == ["0"]:
                return True
        except Exception:
            pass
        if p.copy_clock() >= deadline:
            return False
        time.sleep(1)


def stable_data(copy, copied, query=MIGRATION_DATA):
    """`query` (MIGRATION_DATA or CALENDAR_DATA) once no other session is left on the copy and two reads half a second
    apart agree: an ended session's write counters are flushed as it exits."""
    deadline = p.copy_clock() + 10
    while scratch_lines(copy, copied, "sessions", OTHER_SESSIONS) != ["0"]:
        if p.copy_clock() >= deadline:
            raise MigrationRefused("the rehearsal's session did not end")
        time.sleep(0.2)
    found = scratch_lines(copy, copied, "data", query)
    for _ in range(10):
        time.sleep(0.5)
        again = scratch_lines(copy, copied, "data", query)
        if again == found:
            return catalog_map(found)
        found = again
    raise MigrationRefused("the copy's write counters did not settle")


def rehearse(copied, plan, image, limit):
    """M3: `migrate-expand` on the copy, its effect compared before and after; the rehearsal record, or
    MigrationRefused. The copy's scratch server and the migration container share a fresh internal network, each
    claimed in the copy's owner.json before it is created, so a killed run leaves nothing the next run's
    reconciliation (ANYM-588 C4) cannot remove; `release_copy` removes and confirms them all."""
    copy, scratch = copied["copy"], copied["scratch_container"]
    network = "deedkit-copy-" + copy["id"]
    p.claim(copy, "network", network)
    p.run(["docker", "network", "create", "--internal", "--label", "deedkit-copy=" + copy["id"], network], timeout=60)
    p.run(["docker", "network", "disconnect", "none", scratch], timeout=60)
    p.run(["docker", "network", "connect", network, scratch], timeout=60)
    calendar = plan.get("database") == CALENDAR
    before = catalog_map(scratch_lines(copy, copied, "before", CATALOG))
    identity = json.loads(scratch_lines(copy, copied, "identity", IDENTITY_SQL + ";")[0])
    data = stable_data(copy, copied, CALENDAR_DATA if calendar else MIGRATION_DATA)
    directory = Path(copy["directory"])
    plan_file = directory / "rehearsal-plan.json"
    plan_file.write_text(json.dumps(expand_plan(plan, identity)) + "\n")
    plan_file.chmod(0o644)
    # Trust on the internal network: the DSN names no password. Image user 10001 reads the files.
    spent = 0
    dsn = f"postgresql://{copy['source']['owner']}@{scratch}:5432/{copied['database']}"
    if calendar:
        # The calendar CLI reads only an owner-only runtime.json of its own user (private_files.read_private).
        secret = directory / "rehearsal-runtime.json"
        secret.write_text(json.dumps({"dsn": dsn, "port": 1}) + "\n")
        secret.chmod(0o600)
        os.chown(secret, 10001, 10001)
        spent = int(float(scratch_lines(copy, copied, "busy", p.BUSY)[0]))
    else:
        secret = directory / "rehearsal.dsn"
        secret.write_text(dsn + "\n")
        secret.chmod(0o644)
    container = network + "-migrate"
    p.claim(copy, "container", container)
    label = "deedkit-copy=" + copy["id"]
    answer = p.calendar_expand(container, image, network, str(secret), plan_file, label=label) if calendar else \
        p.expand_run(container, image, network, str(secret), plan_file, label=label, timed=True)
    # The migrator's own time, from its first pending statement to its commit's return (every lock released), never a later
    # read: that counted the container's exit, its removal and a new docker exec, which on the loaded host read
    # 068's 40 ms index as 1100 to 4681 ms (fix-migration-rehearsal-11-09).
    reported = answer.pop("transaction_ms", None) if isinstance(answer, dict) and not calendar else None
    if answer != {"schema_version": plan["target"]}:
        raise MigrationRefused("the rehearsal of migrate-expand failed: "
                               + (answer or {}).get("error", "no answer" if answer is None else "wrong version"))
    took = None
    if plan["pending"] and not calendar:
        if reported is None:
            raise MigrationRefused("the rehearsal's migrate-expand reported no transaction time")
        took = reported
    after_data = stable_data(copy, copied, CALENDAR_DATA if calendar else MIGRATION_DATA)
    if plan["pending"] and calendar:
        took = int(float(scratch_lines(copy, copied, "busy", p.BUSY)[0])) - spent
    if took is not None and not 0 <= took <= limit:
        raise MigrationRefused(f"too slow for an unattended lock: the rehearsal took {took} ms, over {limit} ms")
    after = catalog_map(scratch_lines(copy, copied, "after", CATALOG))
    if reason := destroys_data(before, after) or (calendar_data_unchanged if calendar else data_unchanged)(
            data, after_data, plan):
        raise MigrationRefused("not unattended: " + reason)
    return {"took_ms": took, "difference": catalog_difference(digested(before), digested(after))}


def producer_reads(settings, mirror, target):
    """The backup producer's pinned image (runner.json `image`) is a revision whose backup.py SCHEMAS holds
    `target`, else MigrationWait."""
    waiting = MigrationWait(f"the backup producer does not read schema {target} yet")
    try:
        image = p.read(unattended_settings(settings)["runner_config"])["image"]
        revision = p.run(["docker", "image", "inspect", "--format",
                        '{{index .Config.Labels "org.opencontainers.image.revision"}}', image], timeout=60).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise waiting
        found = BACKUP_SCHEMAS.search(p.run(["git", "-C", str(mirror), "cat-file", "blob", f"{revision}:{BACKUP_SOURCE}"]))
    except MigrationWait:
        raise
    except Exception as exc:
        raise waiting from exc
    if not found or not int(found[1]) <= target < int(found[2]):
        raise waiting


# ANYM-589 B1: the units that run a backup with the producer pin (messaging's and the calendar's runner).
BACKUP_UNITS = ("personal-os-backup.service", "deedkit-calendar-backup.service",
                "deedkit-calendar-backup-catchup.service")

# ANYM-694: the hourly freshness check (deploy/calendar-backup-freshness.py) writes this; `status` prints one line from it.
CALENDAR_FRESHNESS = Path("/opt/deedkit/calendar-backup/backups/freshness.json")
CALENDAR_FRESHNESS_MAX_AGE = 3 * 3600


def calendar_backup_line(path=None, at=None):
    """The `status` line for the calendar backup's freshness; read-only, and never an error for `status`."""
    prefix = "calendar backup: "
    try:
        try:
            with open(path or p.CALENDAR_FRESHNESS, "rb") as handle:
                data = json.loads(handle.read(65536))
        except FileNotFoundError:
            return prefix + "no recent freshness check"
        if not isinstance(data, dict):
            return prefix + "freshness file unreadable (not an object)"
        checked = data.get("checked_at")
        try:
            moment = datetime.strptime(checked, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return prefix + "freshness file unreadable (bad checked_at)"
        word = {"fresh": "fresh", "stale": "STALE", "unknown": "UNKNOWN"}.get(data.get("status"))
        if word is None:
            return prefix + "freshness file unreadable (bad status)"
        elapsed = ((at or datetime.now(timezone.utc)) - moment).total_seconds()
        if elapsed > CALENDAR_FRESHNESS_MAX_AGE or elapsed < -300:
            return prefix + "no recent freshness check"
        age = data.get("age_hours")
        shown = f"{age:.1f}h" if isinstance(age, (int, float)) and not isinstance(age, bool) else "?"
        return f"{prefix}{word} age={shown} (checked {checked})"
    except Exception as exc:
        reason = "not JSON" if isinstance(exc, ValueError) else type(exc).__name__
        return f"{prefix}freshness file unreadable ({reason})"


INFERENCE_WORKER_RECEIPT = Path("/var/lib/deedkit/inference-worker.json")


# ANYM-807: the hourly backup checker (deploy/deedkit-backup-health.service) writes this; `status` prints one line a profile.
BACKUP_HEALTH_RESULT = Path("/var/lib/deedkit-backup-health/result.json")
BACKUP_HEALTH_PROFILES = ("messaging", "calendar", "recordings", "calls")
BACKUP_HEALTH_WORDS = ("delivered", "FAILED", "STALE", "UNKNOWN")


def backup_health_lines(path=None, at=None):
    """One `status` line per backup profile from the checker's result; read-only, and never an error for `status`."""
    def unknown(reason):
        return [f"backup {name}: UNKNOWN ({reason})" for name in BACKUP_HEALTH_PROFILES]
    hint = "backup: verified state is on backup-1 (backup_health.py --print)"
    try:
        try:
            with open(path or p.BACKUP_HEALTH_RESULT, "rb") as handle:
                data = json.loads(handle.read(65536))
        except FileNotFoundError:
            return [*unknown("no recent check"), hint]
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
            return [*unknown("result unreadable"), hint]
        checked = data.get("checked_at")
        try:
            moment = datetime.strptime(checked, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return [*unknown("result unreadable"), hint]
        elapsed = ((at or datetime.now(timezone.utc)) - moment).total_seconds()
        if elapsed > CALENDAR_FRESHNESS_MAX_AGE or elapsed < -300:
            return [*unknown("no recent check"), hint]
        lines = []
        for name in BACKUP_HEALTH_PROFILES:
            entry = data["profiles"].get(name)
            if entry is None:
                lines.append(f"backup {name}: not configured")
                continue
            word = entry.get("word") if isinstance(entry, dict) else None
            if word not in BACKUP_HEALTH_WORDS:
                lines.append(f"backup {name}: UNKNOWN (result unreadable)")
                continue
            last, age = entry.get("last_snapshot_at"), entry.get("age_hours")
            last = last if isinstance(last, str) and re.fullmatch(r"[0-9T:Z-]{20}", last) else "never"
            shown = f"{age:.1f}h" if isinstance(age, (int, float)) and not isinstance(age, bool) else "?"
            lines.append(f"backup {name}: {word} last capture {last} age={shown} (checked {checked})")
        return [*lines, hint]
    except Exception:
        return [*unknown("result unreadable"), hint]


def inference_worker_line(path=None):
    """The `status` line for deedkit-inference-worker.service's receipt (ANYM-794); read-only, never an error for `status`."""
    prefix = "inference worker: "
    try:
        try:
            with open(path or p.INFERENCE_WORKER_RECEIPT, "rb") as handle:
                data = json.loads(handle.read(65536))
        except FileNotFoundError:
            return prefix + "no receipt"
        if not isinstance(data, dict):
            return prefix + "receipt unreadable (not an object)"
        checked, outcome, reason = data.get("checked_at"), data.get("outcome"), data.get("reason")
        try:
            datetime.strptime(checked, "%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            return prefix + "receipt unreadable (bad checked_at)"
        if not isinstance(outcome, str) or outcome not in ("started", "already_running", "refused"):
            return prefix + "receipt unreadable (bad outcome)"
        if not (reason is None or isinstance(reason, str) and re.fullmatch(r"[a-z0-9_-]{1,64}", reason)):
            return prefix + "receipt unreadable (bad reason)"
        return f"{prefix}{outcome}{' ' + reason if reason else ''} (checked {checked})"
    except Exception as exc:
        return f"{prefix}receipt unreadable ({'not JSON' if isinstance(exc, ValueError) else type(exc).__name__})"


def negotiation_worker_line(settings, timeout=15):
    """The `status` line for the negotiation worker (ANYM-908): its heartbeat row and owner-zone label, which its
    own `--status` reads inside the container; None while no project selects it. Read-only, never an error."""
    project = next((item for item in settings.get("projects") or [] if "negotiations" in (item.get("services") or {})), None)
    if project is None:
        return None
    prefix = "negotiation worker: "
    try:
        data = json.loads(p.run(p.compose(project) + ["exec", "-T", "negotiations", "python", "-m",
                                                   "messaging_service.negotiation_worker", "--status"], timeout=timeout))
        zone, beat = data.get("owner_zone"), data.get("heartbeat")
        if not isinstance(zone, str) or not re.fullmatch(r"[A-Za-z0-9_+/-]{1,64}", zone):
            return prefix + "unreadable (bad owner_zone)"
        if beat is None:
            return f"{prefix}no heartbeat, owner zone {zone}"
        version, at = beat.get("version"), beat.get("beat_at")
        if not (isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", version)
                and isinstance(at, str) and re.fullmatch(r"[0-9T:.+-]{19,40}", at)):
            return prefix + "unreadable (bad heartbeat)"
        return f"{prefix}heartbeat {version} at {at}, owner zone {zone}"
    except Exception as exc:
        return f"{prefix}unreadable ({type(exc).__name__})"


def leaf_identity(found):
    """What a change to runner.json changes: its inode, content times, size, owner and mode."""
    return (found.st_dev, found.st_ino, found.st_size, found.st_mtime_ns, found.st_ctime_ns, found.st_mode,
            found.st_uid, found.st_gid)


def leaf_read(directory, name):
    """The bytes and stat of `name` in the open `directory`, never through a link."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(fd, "rb") as handle:
        found = os.fstat(handle.fileno())
        if not stat.S_ISREG(found.st_mode):
            raise OSError(f"{name} is not a regular file")
        return handle.read(), found


def leaf_write(directory, name, data, like):
    """A new file `name` in the open `directory` with `data` and the owner and mode of `like`, fsynced."""
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    with os.fdopen(fd, "wb") as handle:
        os.fchown(handle.fileno(), like.st_uid, like.st_gid)
        os.fchmod(handle.fileno(), stat.S_IMODE(like.st_mode))
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def stage_leaf(directory, path, old, before, data, suffix, what):
    """The first half of replacing `path` (read as `old` with stat `before`) below the open `directory`:
    `<name>.before-<suffix>` keeps `old` (only an earlier run's copy of the same bytes may stay it) and
    `.<name>.<suffix>.tmp` holds `data` with the owner and mode of `before`. Refused when `path` changed meanwhile; the
    caller renames the temporary over `path`. Returns the temporary's name and whether this call wrote the copy."""
    name, undo, temporary = path.name, f"{path.name}.before-{suffix}", f".{path.name}.{suffix}.tmp"
    try:
        kept = leaf_read(directory, undo)[0]
    except FileNotFoundError:
        kept = None
        p.leaf_write(directory, undo, old, before)
        os.fsync(directory)
    except OSError:
        kept = b""
    if kept is not None and kept != old:
        raise MigrationRefused(f"{path.parent / undo} exists and is not {path}; inspect both before {what} moves")
    with suppress(FileNotFoundError):
        os.unlink(temporary, dir_fd=directory)
    p.leaf_write(directory, temporary, data, before)
    try:
        again, current = leaf_read(directory, name)
        same = again == old and leaf_identity(current) == leaf_identity(before) == leaf_identity(
            os.stat(name, dir_fd=directory, follow_symlinks=False))
    except OSError:
        same = False
    if not same:
        os.unlink(temporary, dir_fd=directory)
        raise MigrationRefused(f"{path} changed while deploy moved {what}; inspect it")
    return temporary, kept is None


def settle_pin(path, receipt):
    """A run that stopped after the rename and before its receipt said so: the receipt follows the file."""
    with suppress(FileNotFoundError, ValueError, OSError, KeyError, TypeError):
        record = p.read(receipt)
        if record["status"] == "moving":
            moved = p.read(path).get("image") == record["after"]
            p.write_record(receipt, {**record, "status": "moved" if moved else "not-moved", "settled_at": p.now()})


def follow_producer(settings, mirror, plan, candidate, release):
    """Preparation phase, under the backup runner's lock (deploy holds it): when the pinned producer image does not
    read the target, runner.json `image` moves to the candidate's verified backup image, whose backup.py reads the
    base and the target. `runner.json.before-<candidate>` is written first and the new file replaces the old in one
    rename with the same owner and mode; no other key changes. A rollback keeps the new pin. Every path is opened
    below one descriptor of runner.json's directory; hand edits of runner.json take the same lock."""
    path = Path(unattended_settings(settings)["runner_config"])
    receipt = release / "producer-pin.json"
    try:
        producer_reads(settings, mirror, plan["target"])
        return settle_pin(path, receipt)
    except MigrationWait as exc:
        waiting = exc
    try:
        # The backup target is no Compose service, so preparation did not resolve it: pulled by its attested digest.
        image = p.build_image(release / "source", candidate, "backup")
        found = p.run(["docker", "image", "inspect", "--format",
                     '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}', image], timeout=60).split()
        schemas = BACKUP_SCHEMAS.search(p.run(["git", "-C", str(mirror), "cat-file", "blob", f"{candidate}:{BACKUP_SOURCE}"]))
    except Exception as exc:
        raise waiting from exc
    if not p.IMAGE_DIGEST.fullmatch(image) or found != [image, candidate] or not schemas or not all(
            int(schemas[1]) <= version < int(schemas[2]) for version in (plan["base"], plan["target"])):
        raise waiting
    # The installed runner is the candidate's (ANYM-488), so it knows every key the new worker reads.
    p.check_backup_runner(release / "source", candidate)
    for unit in BACKUP_UNITS:
        try:
            active = p.run(["systemctl", "show", "-P", "ActiveState", unit], timeout=60).strip()
        except Exception as exc:
            raise MigrationWait(f"the state of {unit} could not be read; the backup producer pin waits") from exc
        if active not in ("inactive", "failed"):
            raise MigrationWait(f"{unit} is {active}; the backup producer pin moves when it is idle")
    name, undo = path.name, f"{path.name}.before-{candidate}"
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise waiting from exc
    try:
        try:
            old, before = leaf_read(directory, name)
            pinned = json.loads(old)
        except (OSError, ValueError) as exc:
            raise waiting from exc
        if not isinstance(pinned, dict) or not isinstance(pinned.get("image"), str):
            raise waiting
        temporary = stage_leaf(directory, path, old, before,
                               (json.dumps({**pinned, "image": image}, indent=2) + "\n").encode(), candidate,
                               "the backup producer pin")[0]
        record = {"runner_config": str(path), "before": pinned["image"], "after": image, "undo": str(path.parent / undo),
                  "status": "moving", "at": p.now()}
        p.write_record(receipt, record)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)
    p.write_record(receipt, {**record, "status": "moved", "moved_at": p.now()})
    producer_reads(settings, mirror, plan["target"])


# Owner 11-04 («давай все делать не ждать ничего»): `deploy` installs the backup runner itself. The backup profiles
# (BACKUP.md) run the one host runner and pin messaging's backup image; recordings and calls exist once installed.
RUNNER_CONFIGS = (Path("/opt/personal-os-probe/backup-config/runner.json"),
                  Path("/opt/deedkit/calendar-backup/runner.json"), Path("/opt/deedkit/recordings-backup/runner.json"),
                  Path("/opt/deedkit/calls-backup/runner.json"))
RUNNER_UNITS = (*BACKUP_UNITS, "deedkit-recordings-backup.service", "deedkit-recordings-backup-catchup.service",
                "deedkit-calls-backup.service", "deedkit-calls-backup-catchup.service")
RUNNER_PYTHON = "/usr/bin/python3"  # the interpreter of the backup units' ExecStart


def install_backup_runner(settings, candidate, release):
    """Plan A after preparation, under the runner lock `deploy` holds (production.md, Backup runner): when the host
    runner is not the candidate's, install it with the candidate's verified backup image pinned in every runner.json,
    instead of pausing. Every check runs before the first rename: the backup units are idle and the image pulls (else a
    wait), the image is the candidate's, the new runner's read-only `--check` exits 0 on every runner.json as it will
    be (else a pause; exit 3 is an unfinished journal), and no call is active (else a call wait). Then each runner.json and the runner are replaced by one rename each, after a
    `.before-<candidate>` copy, keeping owner and mode; the runner is read back by its blob. The receipt is
    `<candidate>/backup-runner.json`. A manual `apply` keeps refusing a mismatch (check_backup_runner)."""
    source, receipt = release / "source", release / "backup-runner.json"
    found = p.runner_blobs(source, candidate)
    profiles = p.profiles_blobs(source, candidate) if found else None
    if found is None or (found[0] == found[1] and (profiles is None or profiles[0] == profiles[1])):
        # A run that stopped after the last rename (the runner's) and before its receipt: the receipt follows.
        with suppress(FileNotFoundError, ValueError, OSError, KeyError, TypeError):
            if found and p.read(receipt)["status"] == "installing":
                p.write_record(receipt, {**p.read(receipt), "status": "installed", "settled_at": p.now()})
        return
    installed, expected = found
    for unit in RUNNER_UNITS:
        try:
            active = p.run(["systemctl", "show", "-P", "ActiveState", unit], timeout=60).strip()
        except Exception as exc:
            raise MigrationWait(f"the state of {unit} could not be read; the backup runner install waits") from exc
        if active not in ("inactive", "failed"):
            raise MigrationWait(f"{unit} is {active}; the backup runner is installed when it is idle")
    runner = p.run(["git", "-C", str(source), "cat-file", "blob", f"{candidate}:{p.BACKUP_RUNNER_SOURCE}"], binary=True)
    if p.git_blob(runner) != expected:
        raise p.Refused(f"the backup runner of {candidate[:12]} does not read as its blob {expected[:12]}")
    # Its profiles module goes beside it when it is not the candidate's yet (BACKUP_PROFILES_SOURCE).
    module = p.BACKUP_RUNNER.with_name(Path(p.BACKUP_PROFILES_SOURCE).name) if profiles and profiles[0] != profiles[1] else None
    if module:
        sibling = p.run(["git", "-C", str(source), "cat-file", "blob", f"{candidate}:{p.BACKUP_PROFILES_SOURCE}"], binary=True)
        if p.git_blob(sibling) != profiles[1]:
            raise p.Refused(f"the backup runner's profiles of {candidate[:12]} do not read as their blob {profiles[1][:12]}")
    # The backup target is no Compose service, so preparation did not resolve it: pulled by its attested digest. A
    # failed pull (a registry or network error, a timeout) waits like the producer pin's (follow_producer).
    try:
        image = p.build_image(source, candidate, "backup")
    except Exception as exc:
        detail = str(exc) if isinstance(exc, p.Refused) else type(exc).__name__
        raise MigrationWait(f"the backup image of {candidate[:12]} could not be pulled: {detail}; the backup runner "
                            "install waits") from exc
    found = p.run(["docker", "image", "inspect", "--format",
                 '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}', image], timeout=60).split()
    if not p.IMAGE_DIGEST.fullmatch(image) or found != [image, candidate]:
        raise p.Refused(f"the backup image of {candidate[:12]} is not its verified image; the backup runner stays")
    try:
        messaging = Path(unattended_settings(settings)["runner_config"])
    except p.Refused:
        messaging = p.RUNNER_CONFIGS[0]
    configs = [path for path in dict.fromkeys((messaging, *p.RUNNER_CONFIGS[1:])) if os.path.lexists(path)]
    # staged: (path, temporary, before, after); copies: the `.before-<candidate>` files this run wrote.
    directories, staged, copies = {}, [], []
    try:
        for path in (p.BACKUP_RUNNER, *([module] if module else []), *configs):
            directories[path] = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            old, before = leaf_read(directories[p.BACKUP_RUNNER], p.BACKUP_RUNNER.name)
        except OSError as exc:
            raise p.Refused(f"cannot read the backup runner {p.BACKUP_RUNNER}: {type(exc).__name__}") from exc
        if p.git_blob(old) != installed:
            raise MigrationRefused(f"{p.BACKUP_RUNNER} changed while deploy read it; inspect it")
        work, wrote = stage_leaf(directories[p.BACKUP_RUNNER], p.BACKUP_RUNNER, old, before, runner, candidate,
                                 "the backup runner")
        copies += [p.BACKUP_RUNNER] if wrote else []
        staged.append((p.BACKUP_RUNNER, work, installed, expected))
        if module:
            try:
                current, kept = leaf_read(directories[module], module.name)
            except FileNotFoundError:
                current = None
            except OSError as exc:
                raise p.Refused(f"cannot read the backup runner's {module}: {type(exc).__name__}") from exc
            if current is None:
                # The first install of the module: a new file with the runner's owner and mode, no copy (undo: remove it).
                temporary = f".{module.name}.{candidate}.tmp"
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directories[module])
                p.leaf_write(directories[module], temporary, sibling, before)
            else:
                if p.git_blob(current) != profiles[0]:
                    raise MigrationRefused(f"{module} changed while deploy read it; inspect it")
                temporary, wrote = stage_leaf(directories[module], module, current, kept, sibling, candidate,
                                              "the backup runner's profiles")
                copies += [module] if wrote else []
            staged.append((module, temporary, profiles[0], profiles[1]))
        for path in configs:
            try:
                old, before = leaf_read(directories[path], path.name)
                pinned = json.loads(old)
            except (OSError, ValueError) as exc:
                raise MigrationRefused(f"cannot read {path}: {type(exc).__name__}; install the backup runner by hand "
                                       "(production.md, Backup runner)") from exc
            if not isinstance(pinned, dict) or not isinstance(pinned.get("image"), str):
                raise MigrationRefused(f"{path} names no backup image; install the backup runner by hand")
            live = path
            if pinned["image"] != image:
                data = (json.dumps({**pinned, "image": image}, indent=2) + "\n").encode()
                temporary, wrote = stage_leaf(directories[path], path, old, before, data, candidate,
                                              "the backup image pin")
                live, copies = path.parent / temporary, copies + ([path] if wrote else [])
                staged.append((path, live.name, pinned["image"], image))
            try:
                p.run([p.RUNNER_PYTHON, "-I", str(p.BACKUP_RUNNER.parent / work), "--config", str(live), "--check"],
                    timeout=60)
            except p.Refused as exc:
                raise MigrationRefused(f"the new backup runner's --check of {path} with image {image[:19]} failed: "
                                       f"{exc} (exit 3: an unfinished journal, production.md Backup runner)") from exc
        record = {"sha": candidate, "status": "installing", "at": p.now(), "image": image,
                  "files": [{"path": str(path), "before": old, "after": new,
                             "undo": f"{path}.before-{candidate}" if old else None} for path, _, old, new in staged]}
        # No production change during an active call (AGENTS.md section 1): checked last before the first rename; a
        # call (or an unreadable call state) ends the run with the staged files removed, so nothing changed.
        p.wait_for_calls(settings, time.monotonic() + p.CALL_POLL)
        copies.clear()  # from here on the copies are the undo
        p.write_record(receipt, record)
        try:
            # runner.json first: the new runner may write a worker.json key an older worker refuses (BACKUP.md). The
            # profiles module before both: no runner that loads it runs before it is in place (the older one never does).
            for path, temporary, _, _ in [*staged[1:], staged[0]]:
                os.replace(temporary, path.name, src_dir_fd=directories[path], dst_dir_fd=directories[path])
                os.fsync(directories[path])
            if p.runner_blobs(source, candidate) != (expected, expected):
                raise p.Refused(f"{p.BACKUP_RUNNER} does not read back as {expected[:12]}")
            if module and p.profiles_blobs(source, candidate) != (profiles[1], profiles[1]):
                raise p.Refused(f"{module} does not read back as {profiles[1][:12]}")
        except Exception as exc:
            detail = str(exc) if isinstance(exc, p.Refused) else type(exc).__name__
            raise MigrationRefused(f"the backup runner install of {candidate} stopped after it began to rename: "
                                   f"{detail}; inspect {receipt}") from exc
        staged.clear()
    finally:
        # A check failed before any rename: the files this run wrote go, so a pause leaves the host as it was.
        for path, temporary, _, _ in staged:
            with suppress(OSError):
                os.unlink(temporary, dir_fd=directories[path])
        for path in copies:
            with suppress(OSError):
                os.unlink(f"{path.name}.before-{candidate}", dir_fd=directories[path])
        for directory in directories.values():
            os.close(directory)
    p.write_record(receipt, {**record, "status": "installed", "installed_at": p.now()})
    print(f"backup runner: installed {expected[:12]}, image {image[:19]}", file=sys.stderr, flush=True)


# Incident 11-11: the hourly backup checker (deploy/deedkit-backup-health.service, ANYM-807) was a hand copy that kept
# naming release 634227af, so its Tiden issues landed on an old release. A healthy `apply` now installs the release's
# checker and names the release in its config's `release_sha`. A host without both files keeps none: the first install
# is production.md's (the config names the ingest DSN file).
BACKUP_HEALTH_PROGRAM = Path("/opt/deedkit/backup-health/backup_health.py")
BACKUP_HEALTH_CONFIG = Path("/etc/deedkit-backup-health/config.json")
BACKUP_HEALTH_SOURCE = "services/messaging/messaging_service/backup_health.py"


def install_backup_health(sha, release):
    """After a healthy apply: the release's backup checker and `release_sha`, recorded in `<sha>/backup-health.json`.
    Never raises: the checker reads backups and never gates one, so a refusal is a stderr line and the receipt, and the
    release stands with the previous checker. Returns the receipt, or None on a host without the checker."""
    try:
        result = backup_health_files(sha, release / "source")
    except Exception as exc:
        result = {"sha": sha, "status": "refused", "at": p.now(),
                  "reason": str(exc) if isinstance(exc, (p.Refused, MigrationRefused)) else type(exc).__name__}
    if result is None:
        return None
    with suppress(Exception):
        p.write_record(release / "backup-health.json", result)
    print(f"backup health: {result['status']}" + (f" ({result['reason']})" if "reason" in result else ""),
          file=sys.stderr, flush=True)
    return result


def backup_health_files(sha, source):
    """Replace the checker and its config by one rename each after a `.before-<sha>` copy (owner and mode kept), once
    the staged pair's read-only `--print` exits 0; any failure before the first rename leaves both files as they were.
    The program goes first: until the config follows, the release's checker names the previous release."""
    paths = (p.BACKUP_HEALTH_PROGRAM, p.BACKUP_HEALTH_CONFIG)
    if not all(os.path.lexists(path) for path in paths):
        return None
    program = p.run(["git", "-C", str(source), "cat-file", "blob", f"{sha}:{p.BACKUP_HEALTH_SOURCE}"], binary=True)
    directories, staged, temporary, copies = {}, [], {}, []
    try:
        for path in paths:
            directories[path] = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        found = {path: leaf_read(directories[path], path.name) for path in paths}
        config = json.loads(found[paths[1]][0])
        if not isinstance(config, dict) or not isinstance(config.get("release_sha"), str):
            raise p.Refused(f"{paths[1]} names no release_sha; install the backup checker by hand (production.md)")
        wanted = {paths[0]: program, paths[1]: (json.dumps({**config, "release_sha": sha}, indent=2) + "\n").encode()}
        files = [{"path": str(path), "before": p.git_blob(found[path][0]), "after": p.git_blob(wanted[path]),
                  "undo": f"{path}.before-{sha}"} for path in paths if found[path][0] != wanted[path]]
        if not files:
            return {"sha": sha, "status": "current", "at": p.now()}
        for path in paths:
            if found[path][0] != wanted[path]:
                temporary[path], wrote = stage_leaf(directories[path], path, *found[path], wanted[path], sha,
                                                    "the backup checker")
                staged.append(path)
                copies += [path] if wrote else []
        live = {path: path.parent / temporary.get(path, path.name) for path in paths}
        try:
            p.run([p.RUNNER_PYTHON, "-I", str(live[paths[0]]), "--config", str(live[paths[1]]), "--print"], timeout=60)
        except p.Refused as exc:
            raise p.Refused(f"the release's backup checker refused its configuration: {exc}") from exc
        copies.clear()  # from here on the copies are the undo
        for path in [*staged]:
            os.replace(temporary[path], path.name, src_dir_fd=directories[path], dst_dir_fd=directories[path])
            os.fsync(directories[path])
            staged.remove(path)
    finally:
        # A refusal before the first rename: the files this run wrote go, so the host is as it was.
        for path in staged:
            with suppress(OSError):
                os.unlink(temporary[path], dir_fd=directories[path])
        for path in copies:
            with suppress(OSError):
                os.unlink(f"{path.name}.before-{sha}", dir_fd=directories[path])
        for directory in directories.values():
            os.close(directory)
    return {"sha": sha, "status": "installed", "at": p.now(), "files": files}


def migration_gate(settings, http, plan, mirror, base, serving, *, admission=False):
    """M2's read-only gates, at admission and again as the final gate: no active or unknown call, nothing
    unfinished, the base release's Store without server-side prepared statements (#352) and the live ledger exactly
    the base's. The backup receiver's schema (`receiver_schema_max`) holds no release (owner 11-04): a receiver that
    does not read the target yet is a backup follow-up. At admission of a messaging-only range a ledger exactly at the
    target marks the plan `by_hand` (`at_target`): the base already runs on that schema and `migrate` skips the
    database, unless a release's migration ended uncertain or diverged and nobody settled it (Astra r1 F1)."""
    p.wait_for_calls(settings, time.monotonic() + p.CALL_POLL)
    if reason := p.unresolved(Path(settings["state_directory"]), Path(http["state_directory"]), lambda: mirror, serving):
        raise MigrationRefused(reason)
    if plan["database"] == CALENDAR:
        return calendar_gate(settings, plan, mirror, base)
    version, applied = live_ledger(p.copy_source(settings, AUTO_DATABASE)[0])
    if admission and at_target(plan, version, applied):
        if held := unsettled_outcomes(Path(settings["state_directory"])):
            raise MigrationRefused(f"the migration of {held[0]} ended uncertain or diverged; inspect its migration.json "
                                   "before any release (production.md)")
        plan["by_hand"] = True
        return
    try:
        store = p.run(["git", "-C", str(mirror), "cat-file", "blob", f"{base}:{STORE_SOURCE}"])
    except p.Refused:
        store = ""
    if not UNPREPARED.search(store):
        raise MigrationRefused("the running release would fail on the new columns: deploy #352 first")
    if version != plan["base"] or applied != plan["ledger"]:
        raise MigrationRefused(f"the live {AUTO_DATABASE} ledger is not the base's schema {plan['base']}; "
                               "migrate by hand (production.md)")


def calendar_gate(settings, plan, mirror, base):
    """The calendar part of M2 (K3): the base release's calendar Store prepares no statement server-side (K0) and the
    live calendar ledger is exactly the base's. Calendar has no backup receiver or producer pin to wait for (its
    backups take any fcs.migrations version)."""
    try:
        store = p.run(["git", "-C", str(mirror), "cat-file", "blob", f"{base}:{CALENDAR_STORE}"])
    except p.Refused:
        store = ""
    if not CALENDAR_UNPREPARED.search(store):
        raise MigrationRefused("the running calendar release would fail on the new columns: deploy ANYM-586 K0 first")
    version, applied = calendar_ledger(p.copy_source(settings, CALENDAR)[0])
    if version != plan["base"] or applied != plan["ledger"]:
        raise MigrationRefused(f"the live calendar ledger is not the base's schema {plan['base']}; "
                               "migrate by hand (production.md)")


def migration_receipt(release, plan, status, **fields):
    """`<sha>/migration.json`: the overall status with the summary of `plan` (the range's last database) at the top,
    as ANYM-449 wrote it, and each database's own phase under `phases` (K3)."""
    record = {"database": plan["database"], "base": plan["base"], "target": plan["target"], "files": plan["files"],
              "roles": plan["roles_changed"], "tables": plan["tables"], "status": status, "at": p.now(), **fields}
    p.write_record(release / "migration.json", record)
    return record


def migration_image(release, candidate, name=AUTO_DATABASE):
    """The candidate's verified API image (plan B's target), or its calendar image, pulled or reused under its
    production tag."""
    return p.build_image(release / "source", candidate, CALENDAR_TARGET if name == CALENDAR else p.PROOF_TARGET)


NO_CHANGE = {"removed": [], "changed": [], "added": []}


def step_name(name, step):
    """A live read's step: messaging's as ANYM-449 named them, calendar's prefixed."""
    return step if name == AUTO_DATABASE else f"{name} {step}"


def container_name(name, kind, candidate):
    return f"deedkit-{kind}-{candidate[:12]}" if name == AUTO_DATABASE else f"deedkit-{name}-{kind}-{candidate[:12]}"


def evidence_file(release, name, kind):
    """migrate-plan.json and migration-catalog.json for messaging, calendar-… for calendar."""
    return release / (kind if name == AUTO_DATABASE else f"{name}-{kind}")


def expand_answer(settings, plan, candidate, image, plan_file=None):
    """One production `migrate-expand` (or `--status` without `plan_file`) on `plan`'s database."""
    status, kind = plan_file is None, "status" if plan_file is None else "migrate"
    name = p.container_name(plan["database"], kind, candidate)
    if plan["database"] == CALENDAR:
        found = calendar_settings(settings)
        return p.calendar_expand(name, image, found["network"], found["runtime_file"], plan_file, status=status)
    found = unattended_settings(settings)
    return p.expand_run(name, image, found["network"], found["dsn_file"], plan_file, status=status)


def migration_outcome(settings, plan, candidate, image, expected):
    """Once the migration container and its session are confirmed gone: `migrated` when --status shows the target, the
    live ledger the candidate's checksums and the live catalog changed exactly as the rehearsal did; `not-applied`
    when ledger and catalog are still the copy's; `diverged` when the ledger moved but the catalog differs; else
    `uncertain`. `expected` is migration-catalog.json: the digests of the live catalog at the copy and the
    rehearsal's change between digests."""
    name = plan.get("database", AUTO_DATABASE)
    source = p.copy_source(settings, name)[0]
    if not p.migration_settled(source, p.container_name(name, "migrate", candidate)):
        return "uncertain"
    answer = expand_answer(settings, plan, candidate, image)
    try:
        version, applied = (calendar_ledger if name == CALENDAR else live_ledger)(source)
        effect = catalog_difference({tuple(key): value for key, value in expected["before"]},
                                    digested(catalog_map(p.live_lines(source, step_name(name, "effect"), CATALOG))))
    except (p.Refused, ValueError, KeyError, TypeError):
        return "uncertain"
    at_target = (answer == {"schema_version": plan["target"]} == {"schema_version": version}
                 and applied == {**plan["ledger"], **plan["pending"]})
    if at_target and effect == expected["difference"]:
        return "migrated"
    if (answer == {"schema_version": plan["base"]} == {"schema_version": version} and applied == plan["ledger"]
            and effect == NO_CHANGE and (plan["pending"] or expected["difference"] != NO_CHANGE)):
        return "not-applied"
    return "diverged" if at_target else "uncertain"


def outcome_text(candidate, outcome, code=None, name=AUTO_DATABASE):
    if outcome == "not-applied":
        return (f"migration of {candidate} did not apply" if name == AUTO_DATABASE
                else f"the {name} migration of {candidate} did not apply") + (f": {code}" if code else "")
    if outcome == "diverged":
        return (f"the migration of {candidate} changed {name} differently from its rehearsal; inspect it "
                "before any recovery")
    return (f"the migration of {candidate} is uncertain" if name == AUTO_DATABASE
            else f"the {name} migration of {candidate} is uncertain") + "; inspect the ledger before any recovery"


class Progress:
    """The phases of one range's databases (K3) and the receipt that records them: `rehearsed`, `planned` (its one
    migrate-expand may have run), then `migrated` (calendar adds `roles`: `running`, `ok` or a code) or how it ended.
    The pause of a database after an earlier one committed names that commit first."""

    def __init__(self, release, plans, phases=None, **fields):
        self.release, self.plans, self.phases, self.fields = release, plans, phases if phases is not None else {}, fields

    def record(self, status, **fields):
        return p.migration_receipt(self.release, self.plans[-1], status, phases=self.phases, **self.fields, **fields)

    def update(self, name, **fields):
        self.phases[name] = {**self.phases.get(name, {}), **fields}

    def committed(self, before):
        """"calendar migrated to 6; " for each database migrated before `before`."""
        return "".join(f"{plan['database']} migrated to {plan['target']}; " for plan in self.plans
                       if plan["database"] != before and self.phases.get(plan["database"], {}).get("status") == "migrated")

    def refused(self, name, status, text, **fields):
        prefix = self.committed(name)
        if prefix and status == "not-applied":
            text = f"{name} not applied: {fields.get('code') or 'not-applied'}"
        self.record(status, **fields)
        return MigrationRefused(prefix + text)

    def roles(self, settings, plan, candidate, image):
        """K3: db-roles and db-roles --check after calendar's commit; a non-zero exit pauses before messaging."""
        self.update(CALENDAR, roles="running")
        self.record("planned")
        code = calendar_roles(settings, candidate, image, plan["target"])
        self.update(CALENDAR, roles=code or "ok")
        if code:
            self.record("roles-failed", code=code)
            raise MigrationRefused(f"calendar migrated to {plan['target']}; roles: {code}")
        self.record("planned")


def resumed_migration(settings, plans, candidate, release):
    """A run that finds `migration.json`: True when it records `migrated` (or --status shows it), else
    MigrationRefused; nothing is retried. False when there is none. A database whose migrate-expand may have run is
    decided from its evidence; calendar's db-roles then runs when it had not started; a database that never
    started after an earlier one committed pauses (K3)."""
    if not (release / "migration.json").exists():
        return False
    record = p.read(release / "migration.json")
    status = record.get("status")
    if status == "migrated":
        return True
    if status in ("refused", "not-applied") and plans and all(migrated_by_hand(settings, plan) for plan in plans):
        # That run wrote nothing to production, and a person migrated the range since: admission reads it again.
        return False
    if status != "planned" or plans is None:
        raise MigrationRefused(f"the unattended migration of {candidate} ended {status}; inspect "
                               f"{release / 'migration.json'} and migrate by hand (production.md)")
    phases = record.get("phases")
    if not isinstance(phases, dict):  # an ANYM-449 receipt: messaging only
        phases = {plans[-1]["database"]: {"status": "planned"}}
    progress = Progress(release, plans, phases, resumed=True)
    for plan in plans:
        name = plan["database"]
        phase = found if isinstance(found := phases.get(name), dict) else {}
        phases[name] = phase
        if phase.get("status") == "planned":
            try:
                expected = p.read(evidence_file(release, name, "migration-catalog.json"))
            except (OSError, ValueError):
                expected = None
            outcome = migration_outcome(settings, plan, candidate, migration_image(release, candidate, name),
                                        expected) if isinstance(expected, dict) else "uncertain"
            progress.update(name, status=outcome)
            if outcome != "migrated":
                raise progress.refused(name, outcome, outcome_text(candidate, outcome, name=name))
            if name == CALENDAR:
                # Recovery runs before admission: db-roles is a production write, so it waits for calls like any
                # (Astra r1 F1). A wait records nothing; the next run decides the same evidence again.
                p.wait_for_calls(settings, time.monotonic() + p.CALL_POLL)
                progress.roles(settings, plan, candidate, migration_image(release, candidate, name))
        elif phase.get("status") != "migrated":
            progress.update(name, status="not-applied", code="interrupted")
            raise progress.refused(name, "not-applied", outcome_text(candidate, "not-applied", "interrupted", name),
                                   code="interrupted")
        elif name == CALENDAR and phase.get("roles") != "ok":
            progress.record("uncertain")
            raise MigrationRefused(f"calendar migrated to {plan['target']}; roles: interrupted")
    progress.record("migrated")
    return True


def unsettled_outcomes(state):
    """Releases whose unattended migration ended `uncertain` or `diverged` and whose plan A never ran: the ledger may be
    at the target while the catalog is not what its rehearsal proved. An unreadable receipt counts."""
    found = []
    for path in sorted(Path(state).glob("*/migration.json")):
        try:
            status = p.read(path).get("status")
        except Exception:
            status = None
        if status not in ("migrated", "planned", "refused", "not-applied") and not (path.parent / "attempted.json").exists():
            found.append(path.parent.name)
    return found


def unsettled_migrations(state):
    """Releases whose unattended migration may have changed the database before plan A ran: `planned`, or
    `migrated` with no plan A attempt. The next run finishes that release, never a newer one (Astra r1 F4)."""
    found = []
    for path in sorted(Path(state).glob("*/migration.json")):
        status = p.read(path).get("status")
        if status == "planned" or (status == "migrated" and not (path.parent / "attempted.json").exists()):
            found.append(path.parent.name)
    return found


def migrate_rehearse(settings, plans, candidate, images, limit, progress, refused):
    """M3 for each database of the range: its verified copy and the rehearsal of the migration on it;
    returns the copies and the rehearsals by database name."""
    copies, rehearsals = {}, {}
    for plan in plans:
        name = plan["database"]
        try:
            with p.database_copy(settings, name, candidate, keep=True, catalog=CATALOG) as copied:
                rehearsals[name] = p.rehearse(copied, plan, images[name], limit)
        except MigrationRefused as exc:
            raise refused(name, str(exc), "rehearsal") from exc
        except p.CopyFailed as exc:
            raise refused(name, f"the pre-migration copy of {name} failed: {exc.code}", "copy") from exc
        except Exception as exc:
            raise refused(name, f"the pre-migration copy or rehearsal of {name} stopped: "
                          + (str(exc) if isinstance(exc, p.Refused) else type(exc).__name__), "copy") from exc
        copies[name] = copied
        progress.update(name, status="rehearsed", copy=copied["id"], rehearsal_ms=rehearsals[name]["took_ms"])
    return copies, rehearsals


def migrate_gate(settings, http, plans, mirror, base, serving, candidate, release, messaging, images, copies,
                 rehearsals, refused):
    """The final gates before any production write; returns each database's plan file and expected catalog change."""
    # The final gate: everything admission read, again, and each live catalog still its copy's.
    for plan in plans:
        migration_gate(settings, http, plan, mirror, base, serving)
    if messaging:
        producer_reads(settings, mirror, messaging[0]["target"])
    evidence = {}
    for plan in plans:
        name, copied = plan["database"], copies[plan["database"]]
        source = p.copy_source(settings, name)[0]
        if hashlib.sha256("\n".join(p.live_lines(source, step_name(name, "shape"), CATALOG)).encode()).hexdigest() \
                != copied["catalog_sha256"]:
            raise refused(name, "the database changed shape since its copy" if name == AUTO_DATABASE
                          else f"the {name} database changed shape since its copy", "final gate")
        # The DSN the migration uses must reach the database that was copied and gated, not another one (Astra r1 F3);
        # its one connection checks the same identity again before anything runs (Astra r2 F3).
        identity = json.loads(p.live_lines(source, step_name(name, "identity"), IDENTITY_SQL + ";")[0])
        if name == CALENDAR:
            found = calendar_settings(settings)
            reached = calendar_identity(p.container_name(name, "identity", candidate), images[name], found["network"],
                                        found["runtime_file"])
        else:
            found = unattended_settings(settings)
            reached = p.database_identity(p.container_name(name, "identity", candidate), images[name], found["network"],
                                        found["dsn_file"])
        if reached != identity:
            raise refused(name, "the owner DSN file does not reach the copied database" if name == AUTO_DATABASE
                          else f"the {name} runtime.json does not reach the copied {name} database", "final gate")
        plan_file = evidence_file(release, name, "migrate-plan.json")
        plan_file.write_text(json.dumps(expand_plan(plan, identity)) + "\n")
        plan_file.chmod(0o644)
        # The recovery evidence first: digests of the live catalog the gate read and the change the rehearsal made.
        expected = {"before": sorted(([list(key), value] for key, value in digested(catalog_map(copied["catalog"])).items()),
                                     key=json.dumps), "difference": rehearsals[name]["difference"]}
        p.write_record(evidence_file(release, name, "migration-catalog.json"), expected)
        evidence[name] = plan_file, expected
    return evidence


def migrate_expand(settings, plans, candidate, images, copies, rehearsals, evidence, progress):
    """M4: one production `migrate-expand` per database (calendar also db-roles); each outcome is recorded."""
    last = plans[-1]["database"]
    summary = {"copy": copies[last]["id"], "rehearsal_ms": rehearsals[last]["took_ms"]}
    for plan in plans:
        name = plan["database"]
        plan_file, expected = evidence[name]
        progress.update(name, status="planned")
        progress.record("planned", **summary)
        try:
            answer = expand_answer(settings, plan, candidate, images[name], plan_file)
            outcome = migration_outcome(settings, plan, candidate, images[name], expected)
        except Exception:  # whatever happened, migration.json stays `planned`: the next run reads --status
            raise MigrationRefused(progress.committed(name) + outcome_text(candidate, "uncertain", name=name))
        code = (answer or {}).get("error")
        progress.update(name, status=outcome, **({"code": code} if code else {}))
        if outcome != "migrated":
            raise progress.refused(name, outcome, outcome_text(candidate, outcome, code, name), **summary,
                                   **({"code": code} if code else {}))
        if name == CALENDAR:
            progress.roles(settings, plan, candidate, images[name])
    progress.record("migrated", **summary)
