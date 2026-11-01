from datetime import datetime, timedelta, timezone

from family_calendar.errors import CalendarError
from .store import instant, ref


# A live link that `project` leaves alone, by its drift status (ANYM-436); moved and missing drives are planned.
DRIFT_SKIP = {
    "in_sync": "already_projected",
    "unknown": "already_projected",
    "projection_edited": "projection_edited",
    "source_cancelled": "use_unproject",
}


def carries(step):
    """A series step that checks the preview's read of the series, or one write of a carried exception (ANYM-614)."""
    series = step.get("series") or {}
    return "snapshot" in series or series.get("role") == "exception"


def carried_step(step):
    return (step.get("series") or {}).get("role") == "exception"


def shown_time(value, zone):
    """A series wall time as an aware ISO time in the series zone; a date stays a date."""
    from . import series as recurrence

    if zone is None:
        return value.isoformat()
    return recurrence.placed(value, zone).isoformat()


def uuid_token():
    # Google's client-generated IDs accept lowercase base32hex; hex is a valid subset.
    from uuid import uuid4

    return "fcs" + uuid4().hex


class ProjectionMixin:
    """FamilyService's projection (drives), carry and series-step methods; they use the service's own
    `authorize`, `rule_reader`, `rule_view` and `_can`."""

    def _fresh(self, calendar_ref, start, end, after=None):
        """Complete coverage of [start, end] read in the last 5 minutes (SERVICE.md), and after `after`."""
        cov = self.store.one("SELECT * FROM coverage WHERE calendar_ref=%s", (calendar_ref,))
        since = datetime.now(timezone.utc) - timedelta(minutes=5)
        return bool(
            cov
            and cov["status"] == "complete"
            and cov["start_at"] <= start
            and cov["end_at"] >= end
            and cov["checked_at"] > (max(since, after) if after else since)
        )

    def _details(self, p, calendar_ref):
        return self._can(p, calendar_ref, "agenda:read", details=True) or self._can(
            p, calendar_ref, "event:read", details=True
        )

    def _drift(self, p, link):
        """A link's status as p may know it: from fresh coverage only, and about the lesson only through p's own
        details read of its calendar (authorized apart from the drive); anything else is `unknown`."""
        target, source = link["target_calendar_ref"], link["source_calendar_ref"]
        if link["state"] != "active" or not link["target_resource_ref"] or not self._details(p, target):
            return "unknown"
        drive = self.store.one(
            "SELECT r.etag FROM occurrences o JOIN resources r ON r.ref=o.resource_ref "
            "WHERE o.calendar_ref=%s AND o.resource_ref=%s LIMIT 1",
            (target, link["target_resource_ref"]),
        )
        if not drive:
            lead = self.store.one("SELECT lead_minutes FROM guardian_rules WHERE id=%s", (link["rule_id"],))
            # Gone only in a complete read, after the link's last write, of the span the drive can occupy.
            start = link["source_start"] - timedelta(minutes=lead["lead_minutes"])
            return "projection_missing" if self._fresh(target, start, link["source_end"], link["updated_at"]) else "unknown"
        if drive["etag"] != link["etag"]:
            return "projection_edited"
        if not self._details(p, source) or not self._fresh(source, link["source_start"], link["source_end"]):
            return "unknown"
        occ = self.store.one(
            "SELECT start_at,end_at FROM occurrences WHERE ref=%s AND calendar_ref=%s",
            (link["source_event_ref"], source),
        )
        if not occ:
            # A cancelled occurrence is never published, so cancelled and deleted read alike.
            return "source_cancelled"
        if (occ["start_at"], occ["end_at"]) == (link["source_start"], link["source_end"]):
            return "in_sync"
        return "source_moved" if self._fresh(source, occ["start_at"], occ["end_at"]) else "unknown"

    def _source(self, key, calendar_ref):
        """The lesson's calendar as the principal reads it with details, and the scope that grants it."""
        for scope in ("event:read", "agenda:read"):
            try:
                _, cal = self.authorize(key, calendar_ref, scope)
            except CalendarError:
                continue
            if cal["visibility"] == "details":
                return cal, scope
        raise CalendarError("not_found_or_forbidden")

    def _project(self, key, p, action):
        """One linked create per enabled rule of the lesson's children whose Google target the principal can write."""
        if self.store.guardian_readiness():
            raise CalendarError("capability_unverified")
        if not self.rule_reader(p):
            raise CalendarError("not_found_or_forbidden")
        source, scope = self._source(key, action.get("calendar_ref"))
        occ = self.store.one(
            "SELECT o.*,r.target FROM occurrences o JOIN resources r ON r.ref=o.resource_ref "
            "WHERE o.ref=%s AND o.calendar_ref=%s",
            (action.get("event_ref"), source["ref"]),
        )
        if not occ:
            # A lesson gone since it was projected still names its links: they point to unproject.
            gone = self.store.query(
                self.LINKS + "l.source_calendar_ref=%s AND l.source_event_ref=%s AND r.activity=%s "
                "ORDER BY l.duty,l.id",
                (p["group_id"], source["ref"], action.get("event_ref"), action["activity"]),
            ) if isinstance(action.get("event_ref"), str) else []
            if not gone:
                raise CalendarError("not_found_or_forbidden")
            skips = [
                {
                    "rule_id": r["rule_id"],
                    "duty": r["duty"],
                    "guardian_id": r["guardian_id"],
                    "reason": "projection_pending"
                    if r["state"] == "pending"
                    else "outcome_unknown"
                    if r["state"] != "active"
                    else {"projection_edited": "projection_edited", "unknown": "already_projected"}.get(
                        self._drift(p, r), "use_unproject"
                    ),
                }
                for r in gone
            ]
            labels = {
                r["activity"]
                for r in self.store.query(
                    "SELECT activity FROM guardian_rules WHERE group_id=%s AND source_calendar_ref=%s AND enabled",
                    (p["group_id"], source["ref"]),
                )
            }
            return source, scope, [], skips, labels
        if occ["details"].get("all_day"):
            raise CalendarError("source_all_day")
        # Projecting a drive would chain links; any link naming this event as its target refuses it.
        if self.store.one(
            "SELECT 1 FROM projection_links WHERE target_calendar_ref=%s AND target_id=%s",
            (source["ref"], occ["target"]),
        ):
            raise CalendarError("projection_source_is_projection")
        start = instant(occ["start_at"])
        end = start if occ["details"].get("instant") else instant(occ["end_at"])
        steps, skips, labels = self._drives(key, p, source, action["activity"], occ["ref"], start, end, occ["details"])
        return source, scope, steps, skips, labels

    def _drives(self, key, p, source, activity, event_ref, start, end, details, source_step=None):
        """The linked creates for one lesson; a lesson created in the same plan has no event_ref yet and names
        its create step instead, whose verified receipt supplies the event_ref."""
        rules = self.store.query(
            "SELECT r.*,m.enabled AS guardian_enabled,cn.provider AS target_provider,"
            "(SELECT l.state FROM projection_links l WHERE l.source_event_ref=%s AND l.rule_id=r.id "
            "AND l.state IN ('pending','active','repairing')) AS claim "
            "FROM guardian_rules r JOIN bindings b ON b.group_id=r.group_id AND b.person_id=r.dependent_id "
            "AND b.calendar_ref=r.source_calendar_ref "
            "JOIN memberships m ON m.group_id=r.group_id AND m.person_id=r.guardian_id "
            "JOIN calendars c ON c.ref=r.target_calendar_ref JOIN connections cn ON cn.id=c.connection_id "
            "WHERE r.group_id=%s AND r.source_calendar_ref=%s ORDER BY r.dependent_id,r.duty,r.id",
            (event_ref, p["group_id"], source["ref"]),
        )
        steps, skips = [], []
        for r in rules:
            if r["activity"] != activity:
                continue
            # A live link is planned again only to fix drift: a moved lesson or a drive gone (ANYM-436).
            link = r["claim"] == "active" and self.store.one(
                self.LINKS + "l.rule_id=%s AND l.source_event_ref=%s AND l.state='active'",
                (p["group_id"], r["id"], event_ref),
            )
            drift = self._drift(p, link) if link else None
            kind = "update" if drift == "source_moved" else "create"
            # A live link's drive stays in the calendar it was placed in, even after the rule names another.
            tcal = link["target_calendar_ref"] if link else r["target_calendar_ref"]
            tprovider = (
                self.store.one("SELECT provider FROM connections cn JOIN calendars c ON c.connection_id=cn.id "
                               "WHERE c.ref=%s", (tcal,)) or {}
            ).get("provider") if link else r["target_provider"]
            reason = (
                "disabled"
                if not r["enabled"]
                else "guardian_disabled"
                if not r["guardian_enabled"]
                else "projection_pending"
                if r["claim"] == "pending"
                else "outcome_unknown"
                if r["claim"] == "repairing"
                else DRIFT_SKIP.get(drift or "", "already_projected")
                if r["claim"] and drift not in ("source_moved", "projection_missing")
                # _source already proved source details through event:read or agenda:read. A live link is fixed in
                # its own calendar, so the rule's current target is not consulted for it.
                else (None if link else {"source_not_visible": None}.get(
                    ready := self.rule_view(p, r)["not_ready_reason"], ready))
                or (
                    None
                    if self._can(p, tcal, "plan:create") and self._can(p, tcal, "event:" + kind)
                    else "target_not_writable"
                )
                or (None if tprovider == "google" else "provider_not_supported")
            )
            if reason:
                skips.append({"rule_id": r["id"], "duty": r["duty"], "guardian_id": r["guardian_id"], "reason": reason})
                continue
            _, target = self.authorize(key, tcal, "event:" + kind)
            lead = timedelta(minutes=r["lead_minutes"])
            # Both drives end at the lesson boundary they serve.
            a, b = (start - lead, start) if r["duty"] == "dropoff" else (end - lead, end)
            event = {"title": r["title"], "start": a.isoformat(), "end": b.isoformat(), "timezone": target["timezone"]}
            if r["copy_location"] and details.get("location"):
                event["location"] = details["location"]
            if kind == "update":
                # Reproject: the drive moves to the lesson's new span, conditional on the link ETag.
                drive = self.store.one(
                    "SELECT start_at,end_at,details FROM occurrences WHERE calendar_ref=%s AND resource_ref=%s",
                    (target["ref"], link["target_resource_ref"]),
                )
                del event["title"]
                step = {
                    "action": "update",
                    "calendar_ref": target["ref"],
                    "provider": target["provider"],
                    "event": event,
                    "span": {"start": a.isoformat(), "end": b.isoformat()},
                    "resource_ref": link["target_resource_ref"],
                    "target_id": link["target_id"],
                    "expected_etag": link["etag"],
                    "reproject": True,
                    "link": {
                        "rule_id": r["id"],
                        "rule_version": r["version"],
                        "duty": r["duty"],
                        "guardian_id": link["guardian_id"],
                        "source_event_ref": event_ref,
                    },
                    "source": {"start": start.isoformat(), "end": end.isoformat(), "title": details.get("title")},
                    "before": {
                        "start": drive["start_at"].isoformat(),
                        "end": drive["end_at"].isoformat(),
                        **{k: v for k, v in drive["details"].items() if k == "location"},
                    },
                }
                steps.append((step, target))
                continue
            step = {
                "action": "create",
                "calendar_ref": target["ref"],
                "provider": target["provider"],
                "event": event,
                "span": {"start": a.isoformat(), "end": b.isoformat()},
                "resource_ref": ref("res"),
                "target_id": uuid_token(),
                "link": {
                    "rule_id": r["id"],
                    "rule_version": r["version"],
                    "duty": r["duty"],
                    "guardian_id": r["guardian_id"],
                    "source_event_ref": event_ref,
                    **({} if source_step is None else {"source_step": source_step}),
                },
                "source": {"start": start.isoformat(), "end": end.isoformat(), "title": details.get("title")},
                # Worker-only: the opaque link ID is the one marker the provider event carries.
                "projection": {"link_id": ref("link"), "source_calendar_ref": source["ref"], "visibility": r["visibility"]},
            }
            if link:
                # Re-create a drive gone outside DeedKit, with a new client ID on the same link row; the worker
                # takes the link over from its old target at admission, or fails projection_pending.
                step["link"]["guardian_id"] = link["guardian_id"]
                step["reproject"] = True
                step["projection"].update(
                    link_id=link["id"],
                    repair_of={"target_id": link["target_id"], "resource_ref": link["target_resource_ref"]},
                )
            steps.append((step, target))
        return steps, skips, {r["activity"] for r in rules if r["enabled"]}

    def _unproject(self, key, p, action, skips):
        """One delete per active link of a lesson occurrence or of one of p's operations; a link whose
        outcome is unknown is skipped. Source authority rests on the link's stored source calendar."""
        if self.store.guardian_readiness():
            raise CalendarError("capability_unverified")
        if not self.rule_reader(p):
            raise CalendarError("not_found_or_forbidden")
        if "operation_id" in action:
            if set(action) != {"action", "operation_id"}:
                raise CalendarError("invalid_action")
            if not self.store.one(
                "SELECT 1 FROM operations WHERE id=%s AND principal_id=%s", (action["operation_id"], p["id"])
            ):
                raise CalendarError("not_found_or_forbidden")
            rows = self.store.query(
                self.LINKS + "l.operation_id=%s ORDER BY l.ordinal,l.id", (p["group_id"], action["operation_id"])
            )
        else:
            if set(action) != {"action", "calendar_ref", "event_ref"} or not isinstance(action["event_ref"], str):
                raise CalendarError("invalid_action")
            # Checked before the lookup, so a calendar the principal cannot read reveals no link.
            self._source(key, action["calendar_ref"])
            rows = self.store.query(
                self.LINKS + "l.source_calendar_ref=%s AND l.source_event_ref=%s ORDER BY l.duty,l.id",
                (p["group_id"], action["calendar_ref"], action["event_ref"]),
            )
        sources = {ref: self._source(key, ref) for ref in {r["source_calendar_ref"] for r in rows}}
        result = []
        for r in rows:
            skip = {"rule_id": r["rule_id"], "duty": r["duty"], "guardian_id": r["guardian_id"]}
            if r["state"] != "active":
                # A pending or repairing link may name a write whose outcome is unknown: reconcile first.
                skips.append(dict(skip, reason="outcome_unknown"))
                continue
            if not (self._can(p, r["target_calendar_ref"], "plan:create") and self._can(
                p, r["target_calendar_ref"], "event:delete"
            )):
                skips.append(dict(skip, reason="target_not_writable"))
                continue
            _, target = self.authorize(key, r["target_calendar_ref"], "event:delete")
            start, end = r["source_start"], r["source_end"]
            # An instant lesson is stored with the smallest span the CHECK allows; it ends at its start.
            end = start if end - start <= timedelta(microseconds=1) else end
            step = {
                "action": "delete",
                "calendar_ref": target["ref"],
                "provider": target["provider"],
                "resource_ref": r["target_resource_ref"],
                "target_id": r["target_id"],
                "expected_etag": r["etag"],
                "link": {
                    "rule_id": r["rule_id"],
                    "rule_version": r["rule_version"],
                    "duty": r["duty"],
                    "guardian_id": r["guardian_id"],
                    "source_event_ref": r["source_event_ref"],
                },
                # The stored span: the lesson may since be moved, cancelled or gone.
                "source": {"start": start.isoformat(), "end": end.isoformat(), "title": r["title"]},
            }
            result.append((step, target, *sources[r["source_calendar_ref"]]))
        return result

    def _carry(self, key, p, step, event_ref, source, skips, selected):
        """The linked change of each active drive of a lesson that step moves or cancels: an update to the new
        span by the link's rule, or a delete; both conditional on the link ETag. None when the lesson has no
        link the principal may see."""
        if self.store.guardian_readiness() or not self.rule_reader(p):
            return None
        try:
            source, scope = self._source(key, source["ref"])
        except CalendarError:
            return None
        rows = self.store.query(
            self.LINKS + "l.source_calendar_ref=%s AND l.source_event_ref=%s ORDER BY l.duty,l.id",
            (p["group_id"], source["ref"], event_ref),
        )
        if not rows:
            return None
        kind, result = step["action"], []
        for r in rows:
            skip = {"rule_id": r["rule_id"], "duty": r["duty"], "guardian_id": r["guardian_id"]}
            reason = (
                "projection_pending"
                if r["state"] == "pending"
                # A repairing link names a write whose outcome is unknown: reconcile first.
                else "outcome_unknown"
                if r["state"] != "active"
                else None
                if self._can(p, r["target_calendar_ref"], "plan:create")
                and self._can(p, r["target_calendar_ref"], "event:" + kind)
                else "target_not_writable"
            )
            if not reason and kind == "update" and step["event"].get("all_day"):
                reason = "source_all_day"
            if reason:
                skips.append(dict(skip, reason=reason))
                continue
            _, target = self.authorize(key, r["target_calendar_ref"], "event:" + kind)
            rule = self.store.one(
                "SELECT version,lead_minutes,copy_location FROM guardian_rules WHERE id=%s", (r["rule_id"],)
            )
            drive = self.store.one(
                "SELECT ref,start_at,end_at,details FROM occurrences WHERE calendar_ref=%s AND resource_ref=%s",
                (target["ref"], r["target_resource_ref"]),
            )
            if drive:
                # The drive's cached occurrence moves with the plan: it is no overlap of its own new span.
                selected.add(drive["ref"])
            if kind == "update":
                start, end = (instant(step["span"][k]) for k in ("start", "end"))
                lead = timedelta(minutes=rule["lead_minutes"])
            else:
                start, end = r["source_start"], r["source_end"]
                end = start if end - start <= timedelta(microseconds=1) else end
            linked = {
                "action": kind,
                "calendar_ref": target["ref"],
                "provider": target["provider"],
                "resource_ref": r["target_resource_ref"],
                "target_id": r["target_id"],
                "expected_etag": r["etag"],
                "link": {
                    "rule_id": r["rule_id"],
                    "rule_version": rule["version"] if kind == "update" else r["rule_version"],
                    "duty": r["duty"],
                    "guardian_id": r["guardian_id"],
                    "source_event_ref": r["source_event_ref"],
                    "source_step": step["ordinal"],
                },
                "source": {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "title": step.get("event", {}).get("title") or r["title"],
                },
            }
            if kind == "update":
                a, b = (start - lead, start) if r["duty"] == "dropoff" else (end - lead, end)
                event = {"start": a.isoformat(), "end": b.isoformat(), "timezone": target["timezone"]}
                if rule["copy_location"] and "location" in step["event"]:
                    event["location"] = step["event"]["location"]
                linked.update(event=event, span={"start": a.isoformat(), "end": b.isoformat()})
                if drive:
                    linked["before"] = {
                        "start": drive["start_at"].isoformat(),
                        "end": drive["end_at"].isoformat(),
                        **{k: v for k, v in drive["details"].items() if k == "location"},
                    }
            result.append((linked, target))
        return source, scope, result

    LINKS = (
        # The guardian the drive was made for, frozen in the projecting plan; the rule may since name another.
        "SELECT l.*,coalesce((SELECT p.payload->'steps'->l.ordinal->'link'->>'guardian_id' FROM operations op "
        "JOIN plans p ON p.id=op.plan_id WHERE op.id=l.operation_id),r.guardian_id) AS guardian_id,"
        "o.details->>'title' AS title FROM projection_links l "
        "JOIN guardian_rules r ON r.id=l.rule_id "
        "LEFT JOIN occurrences o ON o.ref=l.source_event_ref AND o.calendar_ref=l.source_calendar_ref "
        "WHERE r.group_id=%s AND l.state<>'removed' AND "
    )

    def replacement_of(self, p, handle):
        """The verified replacement series one of p's own following moves created, by its series_ref: the plan step
        that created it and the create's receipt. Durable, so an undo never waits for a read to publish it."""
        from .providers import series_ref

        rows = self.store.query(
            "SELECT p.payload,s.receipt FROM operations o JOIN plans p ON p.id=o.plan_id "
            "JOIN operation_steps s ON s.operation_id=o.id AND s.ordinal=0 "
            "WHERE o.principal_id=%s AND s.state='verified' "
            "AND p.payload->'steps'->0->'series'->>'role'='replacement' ORDER BY o.created_at",
            (p["id"],),
        )
        for row in rows:
            made = row["payload"]["steps"][0]
            if series_ref(made["calendar_ref"], made["target_id"]) == handle and (row["receipt"] or {}).get("etag"):
                return dict(made, receipt=row["receipt"])
        raise CalendarError("not_found_or_forbidden")

    def _undo_step(self, p, cal, handle, step):
        """Delete the replacement series of a partial following move, only as its create left it (its etag)."""
        made = self.replacement_of(p, handle)
        if made["calendar_ref"] != cal["ref"] or cal["provider"] != "google":
            raise CalendarError("not_found_or_forbidden")
        return dict(step, target_id=made["target_id"], resource_ref=made["resource_ref"],
                    expected_etag=made["receipt"]["etag"], before=dict(made["span"]),
                    series={"scope": "all", "role": "undo", "series_ref": handle,
                            "planned_at": datetime.now(timezone.utc).isoformat()})

    def _series_steps(self, key, cal, step, scope, ordinal, read=None):
        """The steps of a series edit (ANYM-556): `all` is one write to the master; `following` ends the master just
        before the occurrence, and a following move first creates the replacement series under a stable,
        client-generated ID (step 0), then truncates (step 1), so a failed truncation never loses an occurrence.
        A time move previewed with a read of the series (ANYM-614) carries its exceptions: after those steps, one
        write per exception to its instance in the moved series, and every series step first checks that the
        series and its exceptions in range are still as read."""
        if cal["provider"] != "google":
            raise CalendarError("capability_unsupported")
        planned = datetime.now(timezone.utc).isoformat()
        carry = self._series_carry(cal, step, scope, read) if read is not None and step["action"] == "update" else None
        extra = {} if carry is None else {"snapshot": carry["snapshot"]}
        if scope == "all" or step["action"] == "delete":
            steps = [dict(step, ordinal=ordinal, series={"scope": scope, "planned_at": planned, **extra})]
            return self._carried_steps(key, cal, steps, carry, planned)
        self.authorize(key, cal["ref"], "event:create")
        replacement = {
            "action": "create", "calendar_ref": step["calendar_ref"], "provider": step["provider"],
            "ordinal": ordinal, "event": step["event"], "span": step["span"], "resource_ref": ref("res"),
            "target_id": uuid_token(), "occurrence": step["occurrence"],
            "series": {"scope": "following", "role": "replacement", "instance_id": step["target_id"],
                       "instance_etag": step["expected_etag"], "planned_at": planned},
        }
        truncation = {k: v for k, v in step.items() if k not in ("event", "span")}
        truncation.update(ordinal=ordinal + 1, series={"scope": "following", "role": "truncation",
                                                       "replacement_step": ordinal, "planned_at": planned})
        replacement["series"].update(extra)
        truncation["series"].update(extra)
        return self._carried_steps(key, cal, [replacement, truncation], carry, planned)

    def _series_carry(self, cal, step, scope, read):
        """The exceptions a series time move carries, from the preview's read of the series (ANYM-614); None when
        the move's shape is one the carried writes do not express (the worker then refuses it before any write as
        it always did: series_exceptions, unsupported_recurrence, capability_unverified)."""
        from . import series as recurrence

        if (read.get("instance") or {}).get("id") != step["target_id"]:
            return None
        if read["instance"].get("etag") != step["expected_etag"]:
            # The cache and the read disagree on the occurrence: a refresh before the preview settles which is right.
            raise CalendarError("version_conflict")
        master, occurrence, event = read["master"], step["occurrence"], step["event"]
        try:
            zone = recurrence.zone_of(master)
            if scope == "all":
                recurrence.edit_all(master, occurrence, event, zone)
            else:
                recurrence.replacement(master, occurrence, event, zone, "preview")
            items = recurrence.carried(master, read["exceptions"], occurrence, event, scope, zone)
        except CalendarError:
            return None
        if len(items) > recurrence.MAX_CARRIED:
            parts, _ = recurrence.rule_of(master)
            delta, _ = recurrence.shift(occurrence, event, zone)
            later = recurrence.later_start(parts, recurrence.master_start(master, zone),
                                           [i["original"] for i in items], recurrence.MAX_CARRIED)
            raise CalendarError("series_exceptions_too_many", details={
                "count": len(items), "limit": recurrence.MAX_CARRIED,
                **({"later": {"original_start": shown_time(later, zone), "start": shown_time(later + delta, zone)}}
                   if later is not None else {})})
        snapshot = {"master_etag": master["etag"], "exceptions": {i["id"]: i["etag"] for i in items}}
        return {"zone": zone, "items": items, "master_id": master["id"], "snapshot": snapshot}

    def _carried_steps(self, key, cal, steps, carry, planned):
        """The series steps followed by one write per carried exception that needs one; the primary step (the
        master's write, or a following move's truncation) keeps the list the preview shows."""
        from . import series as recurrence
        from .providers import source_ref

        if carry is None:
            return steps
        zone, primary = carry["zone"], steps[-1]
        # The moved series: the replacement a following move creates, or the master an all move patches.
        series_id = steps[0]["target_id"] if steps[0]["action"] == "create" else carry["master_id"]
        shown, out = [], list(steps)
        for item in carry["items"]:
            # Every exception gets its own write and read-back, also one whose fields equal the master's: its
            # instance in the moved series is checked to be the one occurrence there, as previewed.
            target = recurrence.instance_id(series_id, item["start"], zone)
            shown.append({"fate": item["fate"], "original_start": shown_time(item["original"], zone),
                          "start": shown_time(item["start"], zone),
                          **({"own": item["own"]} if item["fate"] == "kept" else {})})
            action = "delete" if item["fate"] == "cancelled" else "update"
            self.authorize(key, cal["ref"], "event:" + action)
            out.append({"action": action, "calendar_ref": cal["ref"], "provider": cal["provider"],
                        "ordinal": steps[0]["ordinal"] + len(out), "target_id": target,
                        "resource_ref": source_ref("res", cal["ref"], target),
                        "series": {"scope": primary["series"]["scope"], "role": "exception", "planned_at": planned,
                                   "series_id": series_id, "desired": item["desired"],
                                   "exception": {"id": item["id"], "fate": item["fate"],
                                                 "original_start": shown[-1]["original_start"],
                                                 "start": shown[-1]["start"]}}})
        primary["series"]["carried"] = shown
        return out
