# Acceptance record 2026-10-23, calendar live run (ANYM-234, 432, 433, 434, 435)

The record behind the Tiden review branch `review/2026-10-23-acceptance-calendar`. Live calendar writes ran on production
(calendar stack at cd6011c) through the common gateway MCP as principal `claude-code`, only on test events created for this run in
the "Family Calendar Pilot" test calendar of each provider. Owner authority 2026-10-23: both providers, re-enable iCloud
`pilot-a` with its stored app password for the run, bind the child and egor to the Google test calendar and set a test guardian
rule. Every operation below ended `verified`, every test event was deleted, `pilot-a` was disconnected again and the test
rule disabled. A database backup was taken before the first change.

| Card | Result | Live evidence | What is left |
|---|---|---|---|
| ANYM-432 guardian rules | Done | `calendar_capabilities` reports `guardian_projections.available: true`; rule set and disabled from the owner CLI | none |
| ANYM-433 projection | Done | `project` created one drive 20 minutes before the lesson; agenda shows it `in_sync` | none |
| ANYM-434 take back | Done | `unproject` deleted only the drive; a second call is `nothing_to_unproject` | none |
| ANYM-435 drives follow | Done | lesson move updated its drive; `create` with `activity` made lesson and drive; lesson delete removed both | none |
| ANYM-234 occurrence, all-day | Review | all-day create and delete verified on Google and iCloud; repeat apply returns the same operation | occurrence move and cancel need a recurring series (DeedKit cannot create one; the owner declined); client view |

Left in production on purpose: the bindings child and egor to the Google test calendar, the `guardians` row and the
disabled rule (no unbind command; removing rows is an owner decision). The Google test calendar is empty.
