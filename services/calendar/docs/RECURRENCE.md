# Recurring events

The original VCALENDAR is copied whole; the needed properties are changed. The master VEVENT, exceptions, VALARM, VTIMEZONE, unknown X-properties/parameters are preserved. Floating time can be written and read, but expansion and slot search without an explicit timezone are forbidden. All-day keeps DATE and the exclusive end date.

## Single and series

`move_single` creates/changes an exception with the original RECURRENCE-ID; `edit_single` is the same with title/location/description (the service's occurrence update; a second call updates the same exception, and a UTC identity read from storage is written in the master's own zone). `cancel_single` marks such an exception CANCELLED, keeps the time of an exception moved before, and never adds EXDATE. The service (MCP) uses `edit_single` and `cancel_single` for one occurrence of a Google/CalDAV series (see SERVICE.md); `shift_series` and the splits stay harness-only. `shift_series` shifts the master, RDATE/EXDATE and recurrence identities. Previously moved/cancelled exceptions keep their actual absolute time (`preserve_absolute`).

A nonexistent and an ambiguous local DST time are rejected. When the zone changes, the value gets its own TZID/UTC encoding, and opaque parameters are preserved. Native RANGE exceptions cannot be silently rewritten by an ordinary series shift. Moving a BYDAY series to another weekday is rejected until the rule transformation is implemented.

## Future split

The boundary is inclusive by the original identity, not by the actual time of a moved meeting. One simple WEEKLY rule is supported with INTERVAL, COUNT or UNTIL, WKST and, if present, a single BYDAY matching DTSTART. Other rules are explicitly unsupported.

1. Save a private split plan with the before/past/future payload, versions, new UID/href and exception remapping.
2. Create the new future series with a conditional PUT to a stable href.
3. Conditionally trim the past one; both operations go through the shared journal/read-back.
4. On a confirmed trim failure, delete the new part only if its version and content match our receipt.
5. On an unknown trim, keep both parts and reconcile the result. Compensation here could delete a future that was already moved successfully.

COUNT is divided by ordinary recurrence positions; cancellations do not increase COUNT. UNTIL is adjusted for the shift. An unbounded series is kept as an RRULE. EXDATE/RDATE and exceptions are distributed across the boundary; future exception identities are remapped, the actual time of individual moves is kept.

These are several external steps without common atomicity: two overlapping series are temporarily possible. The receipt reveals a partial result. Calendar sharing and invitations are not part of the transformation.

## Native probe and comparison

The harness sends `RECURRENCE-ID;RANGE=THISANDFUTURE` separately. The construct is defined by [RFC 5545](https://datatracker.ietf.org/doc/html/rfc5545#section-3.8.4.4); its presence in a response does not prove the behavior of iCloud/the Apple client. Until visual acceptance the native capability is unverified.

The semantic comparison allows reordering of top-level components/properties and changes to DTSTAMP, LAST-MODIFIED, CREATED, SEQUENCE. Other properties, parameters, the timezone definition and alarms are compared conservatively. An equivalent normalization by the provider can give `needs_attention`; this is a reason to investigate the specific diff, not to weaken the comparison silently.

Fixture timezone definitions are limited to 2020–2035. Support for arbitrary historical/non-standard VTIMEZONE and invitation series requires separate acceptance. The matrix proves specific examples, not all variants of iCalendar.
