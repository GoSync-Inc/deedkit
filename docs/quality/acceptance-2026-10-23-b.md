# Acceptance record 2026-10-23, part B (deploy, calendar, calls, CI)

The record behind the Tiden review branch `review/2026-10-23-acceptance-b`: 15 cards in Review were checked against merged
pull requests, Tiden trace verdicts, CI runs and read-only observations of production at c00caa6 (calendar, calls, gateway and
runtime images all at that revision). No live action was taken: no event written, no call placed, no stack upgraded.
Tests were not re-run; the Tiden trace verdict (PASS on all 15) and the merged pull requests stand for them.

| Card | Result | What is left |
|---|---|---|
| ANYM-439 image cleanup after a release | Done | none |
| ANYM-442 plan A waits for an active call | Done | none required (active-call waiting is tested synthetically) |
| ANYM-443 autodeploy installs its own tool | Done | none |
| ANYM-406 no GitHub REST quota use | Review | quota reading before and after the next release |
| ANYM-411 Tiden error only warns | Review | revert pull request (the three-run health streak completed 2026-10-23) |
| ANYM-423 calendar key extension | Review | agenda call after 2026-11-12T08:00Z |
| ANYM-299 GHCR images by digest | Review | rollback drill on the installation (owner-kept) |
| ANYM-234 one occurrence, all-day events | Review | live run on a Google test calendar; iCloud half owner decision |
| ANYM-432 guardian rules, migration 2 | Review | live capability read; no rules set yet |
| ANYM-433 projection into guardians' calendars | Review | live projection of a test occurrence |
| ANYM-434 take back a projection | Review | live undo, after the 433 run |
| ANYM-435 lesson changes carry drives | Review | live move, after the 433 run |
| ANYM-239 authorized live voice calls | Review | WhatsApp sender, owner test call, listening checks |
| ANYM-428 in-band DTMF | Review | live check on a real menu did not pass; owner decision |
| ANYM-451 out-of-band key resend | Review | live check never run |

The exact missing calls for each Review card are in that card's evidence note in Tiden.

ANYM-427 (part A, Medium): the owner confirmed the Mandiri row on 2026-10-23; the live check was recorded the same day, so the card is Done.
