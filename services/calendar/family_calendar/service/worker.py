import asyncio
from datetime import datetime, timedelta, timezone
import psycopg
from psycopg.types.json import Jsonb
from family_calendar.errors import CalendarError
from .core import FamilyService
from .providers import created_event_ref
from .store import instant

# Step states whose intent was durably admitted to send: only their own failure may undo a link change.
ADMITTED = ("sending", "unknown", "needs_attention")
AMBIGUOUS = {"unknown_result", "write_unknown", "provider_unavailable"}


class Worker:
    def __init__(self, store, providers):
        self.store, self.providers = store, providers
        self.service = FamilyService(store)

    async def run_operation(self, operation_id, reconcile=False):
        # Transaction-scoped lock remains held across the network call. A second process cannot send this operation.
        with self.store.transaction() as lock:
            acquired = lock.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS ok",
                (self.store.lock_key("op", operation_id),),
            ).fetchone()["ok"]
            if not acquired:
                return
            op = self.store.one("SELECT * FROM operations WHERE id=%s", (operation_id,))
            if not op or op["state"] in ("verified", "failed", "conflict"):
                return
            if (
                op["state"] == "running"
                and op["lease_until"]
                and op["lease_until"] > datetime.now(timezone.utc)
            ):
                return
            if op["state"] in ("unknown", "needs_attention", "partial") and not reconcile:
                return
            plan = self.store.one("SELECT * FROM plans WHERE id=%s", (op["plan_id"],))
            self.store.query(
                "UPDATE operations SET state='running',lease_until=now()+interval '2 minutes' WHERE id=%s",
                (operation_id,),
            )
            completed = 0
            # Carried exceptions of a series move (ANYM-614) that did not end as previewed: each is reported, and the
            # other exceptions are still written, so the move never ends as a silent partial.
            mismatched = False
            for step in plan["payload"]["steps"]:
                ordinal = step["ordinal"]
                args = (operation_id, ordinal)
                carried = (step.get("series") or {}).get("role") == "exception"
                # A linked create reserves its link; a linked delete (unproject) closes an existing one.
                claim = step.get("link") and step["action"] == "create"
                # A re-create of a drive gone outside DeedKit takes over its existing link (ANYM-436).
                repair = claim and (step.get("projection") or {}).get("repair_of")
                row = self.store.one(
                    "SELECT * FROM operation_steps WHERE operation_id=%s AND ordinal=%s", args
                )
                if row["state"] == "verified":
                    completed += 1
                    continue
                if carried and row["state"] in ("conflict", "failed"):
                    mismatched = True
                    continue
                try:
                    self.service.validate_plan(plan, op["principal_id"])
                    _, cal = self.service.authorize(
                        None, step["calendar_ref"], "event:" + step["action"], principal_id=op["principal_id"]
                    )
                    # Serialize publishing read snapshots with every write to this calendar.
                    if not lock.execute(
                        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS ok",
                        (self.store.lock_key("calendar", cal["ref"]),),
                    ).fetchone()["ok"]:
                        raise CalendarError("resource_busy")
                    # A resource lock protects different operations targeting the same remote object.
                    acquired = lock.execute(
                        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS ok",
                        (self.store.lock_key("resource", cal["ref"] + step["target_id"]),),
                    ).fetchone()["ok"]
                    if not acquired:
                        raise CalendarError("resource_busy")
                    if row["state"] in ("sending", "unknown", "needs_attention"):
                        receipt = await self.providers.verify(cal, row["intent"])
                        if not receipt:
                            self.store.query(
                                "UPDATE operation_steps SET state='needs_attention',error='unknown_result' WHERE operation_id=%s AND ordinal=%s",
                                args,
                            )
                            self._finish(operation_id, "needs_attention", "unknown_result")
                            return
                    elif row["state"] == "prepared":
                        # A series plan that already wrote resumes past the preview TTL (ANYM-556): the TTL
                        # bounds when an apply may start, not how a started plan finishes.
                        if plan["expires_at"] < datetime.now(timezone.utc) and not (completed and step.get("series")):
                            raise CalendarError("plan_expired")
                        if claim and step["link"].get("source_step") is not None:
                            step = self._created_source(plan, step, operation_id)
                        if (step.get("series") or {}).get("replacement_step") is not None:
                            step = self._replaced_series(step, operation_id)
                        intent = await self.providers.prepare(cal, step)
                        if intent.get("absent"):
                            # The fresh read found our drive already gone: the link closes without a write.
                            receipt = {"deleted": True, "absent": True, "resource_ref": intent["resource_ref"]}
                        else:
                            # Revalidate after the read/prepare and immediately before durable admission to send.
                            self.service.validate_plan(plan, op["principal_id"])
                            with self.store.transaction() as c:
                                if repair:
                                    self._repair(c, step, intent)
                                elif claim:
                                    self._claim(c, step, cal, intent, args)
                                elif step["action"] != "create" and self._links_writable(c):
                                    self._hold(c, step, cal)
                                c.execute(
                                    "UPDATE operation_steps SET state='sending',intent=%s WHERE operation_id=%s AND ordinal=%s",
                                    (Jsonb(intent), *args),
                                )
                            self.store.query(
                                "UPDATE operations SET lease_until=now()+interval '2 minutes' WHERE id=%s",
                                (operation_id,),
                            )
                            # A timed-out write can still have changed availability.
                            self.store.query(
                                "UPDATE coverage SET status='stale' WHERE calendar_ref=%s", (cal["ref"],)
                            )
                            await self.providers.mutate(cal, intent)
                            receipt = await self.providers.verify(cal, intent)
                            if not receipt:
                                raise CalendarError("unknown_result")
                    else:
                        self._finish(operation_id, "partial" if completed else "failed", row["error"])
                        return
                    with self.store.transaction() as c:
                        c.execute(
                            "UPDATE operation_steps SET state='verified',receipt=%s,error=NULL WHERE operation_id=%s AND ordinal=%s",
                            (Jsonb(receipt), *args),
                        )
                        if repair and not c.execute(
                            # The drive is placed at the lesson's current span, which the link records with it.
                            self.REPAIRED + "state='active',etag=%s,source_start=%s,source_end=%s,rule_version=%s,"
                            "updated_at=now() WHERE id=%s AND state='repairing' AND target_id=%s RETURNING id",
                            (
                                receipt.get("etag"),
                                step["source"]["start"],
                                self._source_end(step),
                                step["link"]["rule_version"],
                                step["projection"]["link_id"],
                                step["target_id"],
                            ),
                        ).fetchone():
                            # A replacement no link tracks is never reported done.
                            raise CalendarError("unknown_result")
                        elif claim:
                            # Also the reconcile path: the claim admitted with the send becomes the live link.
                            c.execute(
                                "UPDATE projection_links SET state='active',etag=%s,updated_at=now() "
                                "WHERE operation_id=%s AND ordinal=%s AND state='pending'",
                                (receipt.get("etag"), *args),
                            )
                        elif not self._links_writable(c):
                            # Before migration 2 and its grants no link exists: ordinary writes never depend on it.
                            pass
                        elif step["action"] == "delete":
                            # Any verified DeedKit delete of a drive, unproject or ordinary, ends its link.
                            c.execute(
                                "UPDATE projection_links SET state='removed',updated_at=now() "
                                "WHERE target_calendar_ref=%s AND target_id=%s AND state IN ('active','repairing')",
                                (cal["ref"], step["target_id"]),
                            )
                        elif step.get("link"):
                            # A drive moved with its lesson: the link records the lesson's new span and the rule
                            # version that placed it.
                            c.execute(
                                "UPDATE projection_links SET state='active',etag=%s,source_start=%s,source_end=%s,"
                                "rule_version=%s,updated_at=now() "
                                "WHERE target_calendar_ref=%s AND target_id=%s AND state IN ('active','repairing')",
                                (
                                    receipt.get("etag"),
                                    step["source"]["start"],
                                    self._source_end(step),
                                    step["link"]["rule_version"],
                                    cal["ref"],
                                    step["target_id"],
                                ),
                            )
                        else:
                            # A verified DeedKit update of a drive keeps its link on the new version.
                            c.execute(
                                "UPDATE projection_links SET state='active',etag=%s,updated_at=now() "
                                "WHERE target_calendar_ref=%s AND target_id=%s AND state IN ('active','repairing')",
                                (receipt.get("etag"), cal["ref"], step["target_id"]),
                            )
                    self.store.query(
                        "UPDATE coverage SET status='stale' WHERE calendar_ref=%s", (cal["ref"],)
                    )
                    completed += 1
                except CalendarError as e:
                    current = self.store.one(
                        "SELECT state FROM operation_steps WHERE operation_id=%s AND ordinal=%s", args
                    )["state"]
                    # Read-back may fail after a successful send (including loss of authorization).
                    # Only a definite conditional conflict proves this request did not apply.
                    ambiguous = current in ADMITTED and e.code not in (
                        "version_conflict",
                        "write_rejected",
                    )
                    state = (
                        "unknown" if ambiguous else "conflict" if e.code == "version_conflict" else "failed"
                    )
                    with self.store.transaction() as c:
                        c.execute(
                            "UPDATE operation_steps SET state=%s,error=%s WHERE operation_id=%s AND ordinal=%s",
                            (state, e.code, *args),
                        )
                        if repair and not ambiguous and current in ADMITTED:
                            # A definite failure of the admitted repair gives the link its old target back (still
                            # missing there); a step never admitted (prepared, or already conflict/failed when a
                            # reconcile re-enters it) leaves the link to whoever holds it.
                            c.execute(
                                self.REPAIRED + "state='active',target_id=previous_target_id,"
                                "target_resource_ref=%s,previous_target_id=NULL "
                                "WHERE id=%s AND state='repairing' AND target_id=%s",
                                (repair["resource_ref"], step["projection"]["link_id"], step["target_id"]),
                            )
                        elif claim and not repair and not ambiguous:
                            # A definite failure releases the claim (no DELETE grant) so the lesson can be projected
                            # again; an ambiguous one keeps it until reconcile.
                            c.execute(
                                "UPDATE projection_links SET state='removed',updated_at=now() "
                                "WHERE operation_id=%s AND ordinal=%s AND state='pending'",
                                args,
                            )
                        elif (
                            step["action"] != "create"
                            and current in ADMITTED
                            and not ambiguous
                            and self._links_writable(c)
                        ):
                            # An admitted write to a drive that definitely did not apply gives its hold back.
                            c.execute(
                                "UPDATE projection_links SET state='active',updated_at=now() "
                                "WHERE target_calendar_ref=%s AND target_id=%s AND state='repairing'",
                                (step["calendar_ref"], step["target_id"]),
                            )
                    if carried and not ambiguous:
                        mismatched = True
                        continue
                    self._finish(operation_id, state if ambiguous or not completed else "partial", e.code)
                    return
                except Exception:
                    current = self.store.one(
                        "SELECT state FROM operation_steps WHERE operation_id=%s AND ordinal=%s", args
                    )["state"]
                    if current == "prepared":
                        # No intent was admitted to send, so nothing can have been mutated.
                        self.store.query(
                            "UPDATE operation_steps SET state='failed',error='service_unavailable' WHERE operation_id=%s AND ordinal=%s",
                            args,
                        )
                        self._finish(operation_id, "partial" if completed else "failed", "service_unavailable")
                        return
                    # Even an unexpected local fault may follow a successful remote mutation.
                    self.store.query(
                        "UPDATE operation_steps SET state='unknown',error='unknown_result' WHERE operation_id=%s AND ordinal=%s",
                        args,
                    )
                    self._finish(operation_id, "unknown", "unknown_result")
                    return
            if mismatched:
                self._finish(operation_id, "partial", "series_exception_mismatch")
                return
            self._finish(operation_id, "verified", None)

    @staticmethod
    def _links_writable(c):
        return c.execute(
            "SELECT coalesce(has_table_privilege(to_regclass('projection_links'),'SELECT') "
            "AND has_table_privilege(to_regclass('projection_links'),'UPDATE'),false) AS ok"
        ).fetchone()["ok"]

    @staticmethod
    def _hold(c, step, cal):
        """While a write to a drive is in flight its link is `repairing`: no second write or undo of that drive
        is admitted until the first one is verified, definitely failed or reconciled."""
        link = c.execute(
            "SELECT id,state FROM projection_links WHERE target_calendar_ref=%s AND target_id=%s "
            "AND state<>'removed' FOR UPDATE",
            (cal["ref"], step["target_id"]),
        ).fetchone()
        if link is None:
            if step.get("link"):
                # Closed since the plan, though the drive still reads at the planned version: not ours to delete.
                raise CalendarError("version_conflict")
            return
        if link["state"] != "active":
            raise CalendarError("outcome_unknown")
        c.execute("UPDATE projection_links SET state='repairing',updated_at=now() WHERE id=%s", (link["id"],))

    @staticmethod
    def _source_end(step):
        # An instant lesson keeps the smallest span CHECK(source_end>source_start) allows, as occurrences do.
        return max(instant(step["source"]["end"]), instant(step["source"]["start"]) + timedelta(microseconds=1))

    def _created_source(self, plan, step, operation_id):
        """A drive of a lesson created in the same plan names the lesson by the create's verified receipt."""
        source = plan["payload"]["steps"][step["link"]["source_step"]]
        row = self.store.one(
            "SELECT state,receipt FROM operation_steps WHERE operation_id=%s AND ordinal=%s",
            (operation_id, source["ordinal"]),
        )
        if row["state"] != "verified" or not (row["receipt"] or {}).get("resource_ref"):
            raise CalendarError("source_unverified")
        event_ref = created_event_ref(source["provider"], row["receipt"]["resource_ref"], source["target_id"])
        return dict(step, link=dict(step["link"], source_event_ref=event_ref))

    def _replaced_series(self, step, operation_id):
        """The truncation of a following edit ends the master the verified replacement was made from, by its etag."""
        row = self.store.one(
            "SELECT state,intent FROM operation_steps WHERE operation_id=%s AND ordinal=%s",
            (operation_id, step["series"]["replacement_step"]),
        )
        if row["state"] != "verified" or not (row["intent"] or {}).get("series_etag"):
            raise CalendarError("source_unverified")
        return dict(step, series=dict(step["series"], master_etag=row["intent"]["series_etag"]))

    REPAIRED = "UPDATE projection_links SET "

    @staticmethod
    def _repair(c, step, intent):
        """Take the link over from its gone drive in the admission transaction: only from `active` on its old
        target, so of two competing repairs the second fails before any send."""
        link = step["link"]
        rule = c.execute("SELECT enabled,version FROM guardian_rules WHERE id=%s", (link["rule_id"],)).fetchone()
        if not rule or not rule["enabled"] or rule["version"] != link["rule_version"]:
            raise CalendarError("rule_changed")
        old = step["projection"]["repair_of"]
        if not c.execute(
            "UPDATE projection_links SET state='repairing',previous_target_id=target_id,target_id=%s,"
            "target_resource_ref=%s,rule_version=%s "
            "WHERE id=%s AND state='active' AND target_id=%s RETURNING id",
            (step["target_id"], intent.get("resource_ref"), link["rule_version"], step["projection"]["link_id"],
             old["target_id"]),
        ).fetchone():
            raise CalendarError("projection_pending")

    @staticmethod
    def _claim(c, step, cal, intent, args):
        """Reserve the (lesson, rule) link in the admission transaction; checked only before a fresh send."""
        link = step["link"]
        rule = c.execute("SELECT enabled,version FROM guardian_rules WHERE id=%s", (link["rule_id"],)).fetchone()
        if not rule or not rule["enabled"] or rule["version"] != link["rule_version"]:
            raise CalendarError("rule_changed")
        try:
            c.execute(
                "INSERT INTO projection_links(id,rule_id,rule_version,duty,source_calendar_ref,source_event_ref,"
                "source_start,source_end,target_calendar_ref,target_id,target_resource_ref,operation_id,ordinal,state) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')",
                (
                    step["projection"]["link_id"],
                    link["rule_id"],
                    link["rule_version"],
                    link["duty"],
                    step["projection"]["source_calendar_ref"],
                    link["source_event_ref"],
                    step["source"]["start"],
                    Worker._source_end(step),
                    cal["ref"],
                    intent["target"],
                    intent.get("resource_ref"),
                    *args,
                ),
            )
        except psycopg.errors.UniqueViolation:
            # Another operation holds a pending or active link for this lesson and rule.
            raise CalendarError("already_projected") from None

    def _finish(self, operation_id, state, error):
        self.store.query(
            "UPDATE operations SET state=%s,error=%s,lease_until=NULL WHERE id=%s",
            (state, error, operation_id),
        )
        self.store.query(
            "INSERT INTO audit(principal_id,action,ref,outcome) SELECT principal_id,%s,id,%s FROM operations WHERE id=%s",
            ("operation", state, operation_id),
        )

    async def sync_one(self):
        with self.store.transaction() as conn:
            job = conn.execute(
                "SELECT * FROM sync_jobs WHERE state='prepared' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"
            ).fetchone()
            if not job:
                return False
            conn.execute("UPDATE sync_jobs SET state='sending' WHERE id=%s", (job["id"],))
        try:
            # Refresh requests cannot bypass the same calendar visibility boundary.
            for scope in ("agenda:read", "availability:read"):
                try:
                    _, cal = self.service.authorize(
                        None, job["calendar_ref"], scope, principal_id=job["principal_id"]
                    )
                    break
                except CalendarError as error:
                    if error.code != "not_found_or_forbidden":
                        raise
            else:
                raise CalendarError("not_found_or_forbidden")
            with self.store.transaction() as lock:
                if not lock.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS ok",
                    (self.store.lock_key("calendar", cal["ref"]),),
                ).fetchone()["ok"]:
                    self.store.query("UPDATE sync_jobs SET state='prepared' WHERE id=%s", (job["id"],))
                    return True
                resources = await self.providers.read_window(cal, job["start_at"], job["end_at"])
                self.store.publish_window(cal["ref"], job["start_at"], job["end_at"], resources, "complete")
            state = "verified"
        except CalendarError as e:
            self.store.query(
                "INSERT INTO coverage(calendar_ref,start_at,end_at,status,error) VALUES (%s,%s,%s,'blocked',%s) "
                "ON CONFLICT(calendar_ref) DO UPDATE SET status='blocked',error=excluded.error",
                (job["calendar_ref"], job["start_at"], job["end_at"], e.code),
            )
            state = "failed"
        except Exception:
            self.store.query(
                "INSERT INTO coverage(calendar_ref,start_at,end_at,status,error) "
                "VALUES (%s,%s,%s,'blocked','provider_unavailable') ON CONFLICT(calendar_ref) "
                "DO UPDATE SET status='blocked',error='provider_unavailable'",
                (job["calendar_ref"], job["start_at"], job["end_at"]),
            )
            state = "failed"
        self.store.query("UPDATE sync_jobs SET state=%s WHERE id=%s", (state, job["id"]))
        return True

    def _series_reads_ready(self):
        # roles.sql grants the worker UPDATE on the answer's columns only, never the whole row.
        return self.store.one(
            "SELECT coalesce(has_table_privilege(to_regclass('series_reads'),'SELECT') "
            "AND has_column_privilege(to_regclass('series_reads'),'state','UPDATE') "
            "AND has_column_privilege(to_regclass('series_reads'),'result','UPDATE') "
            "AND has_column_privilege(to_regclass('series_reads'),'error','UPDATE') "
            "AND has_column_privilege(to_regclass('series_reads'),'answered_at','UPDATE'),false) AS ok"
        )["ok"]

    async def read_series_one(self):
        """Answer one preview's request for a read of a series (ANYM-614): read-only, and only what the asking
        principal may edit in that calendar. False when there is none (or before schema 7 and its grants)."""
        if not self._series_reads_ready():
            return False
        with self.store.transaction() as conn:
            job = conn.execute(
                "SELECT * FROM series_reads WHERE state='prepared' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"
            ).fetchone()
            if not job:
                return False
            conn.execute("UPDATE series_reads SET state='sending' WHERE id=%s", (job["id"],))
        result, error = None, None
        try:
            _, cal = self.service.authorize(None, job["calendar_ref"], "event:update", principal_id=job["principal_id"])
            result = await self.providers.read_series(cal, job["target"])
        except CalendarError as e:
            error = e.code
        except Exception:
            error = "provider_unavailable"
        self.store.query(
            "UPDATE series_reads SET state=%s,result=%s,error=%s,answered_at=now() WHERE id=%s",
            ("failed" if error else "verified", Jsonb(result) if result is not None else None, error, job["id"]),
        )
        return True

    async def tick(self):
        await self.sync_one()
        try:
            await self.read_series_one()
        except Exception:
            # A series read is a preview's convenience: its failure never stops syncs or operations.
            pass
        op = self.store.one(
            "SELECT id FROM operations WHERE state='queued' OR (state='running' AND lease_until<now()) ORDER BY created_at LIMIT 1"
        )
        if op:
            await self.run_operation(op["id"])

    async def run(self):
        try:
            # One session-level lease per database/schema, independent of process
            # and checkout paths. Acquire before recovering any unfinished reads.
            with self.store._connect() as lease:
                lease.autocommit = True
                acquired = lease.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS ok",
                    (self.store.lock_key("worker", "singleton"),),
                ).fetchone()["ok"]
                if not acquired:
                    raise CalendarError("worker_already_running")
                # A read left 'sending' may itself have killed the process. Re-queueing it
                # first would repeat the crash and block every sync and operation; fail it
                # with an explicit coverage error so the agent can request a fresh refresh.
                self.store.query(
                    "WITH interrupted AS (UPDATE sync_jobs SET state='failed' WHERE state='sending' "
                    "RETURNING calendar_ref,start_at,end_at) "
                    "INSERT INTO coverage(calendar_ref,start_at,end_at,status,error) "
                    "SELECT DISTINCT ON (calendar_ref) calendar_ref,start_at,end_at,'blocked','sync_interrupted' "
                    "FROM interrupted ON CONFLICT(calendar_ref) DO UPDATE SET status='blocked',error=excluded.error"
                )
                if self._series_reads_ready():
                    # A series read left 'sending' failed with the process; its preview asks again.
                    self.store.query("UPDATE series_reads SET state='failed',error='read_interrupted',"
                                     "answered_at=now() WHERE state='sending'")
                while True:
                    # Loss of the dedicated lease connection stops the process.
                    lease.execute("SELECT 1")
                    await self.tick()
                    await asyncio.sleep(0.5)
        finally:
            await self.providers.close()
            self.store.close()
