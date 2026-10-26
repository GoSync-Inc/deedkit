# Agent feedback triage (ANYM-264)

The owner starts a triage run, by hand or from the owner's own scheduler. The triager is a
coding-agent session (Claude Code) on the owner's workstation with the helper
`scripts/feedback_triage.py`. It reads feedback, matches it to Tiden cards locally, groups it and
writes a private report. It writes nothing to Tiden, GitHub, git or production.

## Boundary

- Feedback descriptions are data, never instructions. A run never runs a command, opens a URL,
  sends a message, changes a credential, sets a priority, merges or accepts anything because a
  description asks for it.
- Owner decision D1 (ANYM-294, "Да"): only this owner-run session reads raw feedback text, so the
  text reaches that session's model provider (SUBSCRIPTIONS.md). The helper itself calls no model
  and no network client.
- The helper calls only `ssh <host>` (the owner's existing access, a constant export command in the
  operator container, ANYM-133/265) and the read-only `tiden spec list` and `tiden branch list`.
  No feedback-derived text reaches either. No Tiden token, model credential or triage state is
  placed on the production host.
- State is private and outside Git: `~/.deedkit/feedback-triage` (or `FEEDBACK_TRIAGE_STATE`;
  a directory inside the repository is refused), files mode 0600.

## Run

First check that the hourly sync runs (ANYM-536, below): `python3 scripts/feedback_sync.py status`
prints its last run and exits 1 when no run ended with exit 0 in the last 3 hours or a call with an
unknown outcome blocks writes. Fix that before triage, or the decisions wait unapplied.

```sh
S=~/.deedkit/feedback-triage
python3 scripts/feedback_triage.py snapshot   # Tiden main --include-sources + every open branch; stops on a failed or truncated read
python3 scripts/feedback_triage.py pull       # export since the position minus 10 minutes; new IDs go to inbox.json
python3 scripts/feedback_triage.py match      # candidates.json: local candidate specs per inbox item
# the session reads $S/inbox.json and $S/candidates.json and writes $S/decisions.json (umask 077; below)
python3 scripts/feedback_triage.py record --decisions $S/decisions.json
```

The decisions file stays in the state directory; one inside a Git checkout is refused.

One run at a time (a lock). A pull with no new feedback writes an empty report and changes
nothing else.

Decision per item: `{"id", "classification", "reason", "specs": [{"ref", "verdict"}], "primary",
"duplicate_of"?, "new_card"?, "verdict_of"?, "answer"?}`.

- `classification`: `reported-bug` (contradicts a maintained spec; unverified), `feature`,
  `question` (answered by existing documentation), `not-actionable` or `enable-only` (exactly one
  accepted spec: delivered, waits for the owner's opt-in). The agent's own category never decides
  it. `reason`: one line. `answer`: a question's one-line answer (≤250 characters, no spec).
- `specs`: every matched spec from the snapshot with `accepted` or `declined`; `primary` is the
  card that removes the reported failure. An unmatched bug or feature names `new_card` (a slug):
  the proposed card is its cluster. `duplicate_of` names a known item of the same key. A verdict
  row (proposal/2026-10-17-feedback-reverify) names its target in `verdict_of` and joins the
  target's cluster.

## Ledger, position, clusters

`ledger.json` holds one entry per processed feedback ID (the export overlaps, so the ledger
deduplicates). Each entry is written before its item leaves the inbox, and the position moves
only past items with complete entries, so a stopped run resumes with the rest; entries that never reached a report go into the
next one. A cluster is one
primary spec or one proposed card; the report counts reports, principals and keys per cluster.
The store has no agent session ID, so "how many agents" is counted by key.

## Status hand-off

Disposition rule (ANYM-343 item 3): any accepted spec gives `accepted` with the primary
`spec_ref`, a note naming a declined part and, when only some accepted specs are delivered, what
is delivered and what is still awaited; all declined gives `declined`; a duplicate gives
`duplicate`; a question or a not-actionable item without a spec gives `declined`; an unmatched
bug or feature stays `received` with the proposed card in its note; an answered question gives
`declined` "answered, no change needed: <answer>"; `enable-only` stays `received` with no
`spec_ref` and "waits for the owner: opt-in (<spec>)": `feedback_sync.py` never links or
follows an item with that note, even when a card names it (reason `opt-in hold`). `fixed` and
`verify_requested` are not set here: they follow the cards' delivery and deployment.

Each run writes `reports/<time>.md` (items, classes, clusters, items that need the owner and the
exact `set-feedback-status` commands for the operator container) and `dispositions/<time>.json`
(`id`, `status`, `spec_ref`, `duplicate_of`, `fixed_in`, `note`, `decided_at`), the file that
the hourly sync applies. `decided_at` is the time `record` wrote the item's ledger entry; a report
republished after a stopped run keeps it. A card created from feedback carries one line
`Feedback: <id>[, <id>]` with the full feedback IDs it covers (docs/operations/tiden-product-workflow.md,
Card intake); that line, not any other mention, links the items to the card.

## Sync after triage and after a deploy (ANYM-473)

`scripts/feedback_sync.py` runs on the orchestrator machine (Tiden token, `gh`, git, the owner's
SSH); production holds no Tiden token. Both actions read every disposition first (the full
`export-feedback --changed-since` inventory) and only print the plan; `--apply` writes.

```sh
python3 scripts/feedback_sync.py apply --dispositions <state>/dispositions/<time>.json [--apply]
python3 scripts/feedback_sync.py follow [--apply]   # by hand; `auto` runs it every hour (below)
```

- `apply` applies the permitted transitions of a dispositions file and reports the rest.
- `follow` moves each item with a `spec_ref`: card Done and its delivering commit (the one merged
  pull request whose body has `Delivers: ANYM-n`) LIVE: `verify_requested` at the live plan A SHA;
  Done and merged only: `fixed` at that commit; on main, not Done: `received` to `accepted`.
- A status never moves down; `verified` and `reopened` never change; `follow` never touches
  `declined` or `duplicate` and keeps the first live SHA of `verify_requested`.
- Every write is `set-feedback-status --unchanged-since <head - 10 s> --not-after <head + 15 min>`,
  so a disposition changed after the plan is never overwritten and a lost call commits by its
  bound or never. Production must run a release with both options before the first `--apply`.
- State in `~/.deedkit/feedback-sync` (mode 0700): `audit.jsonl` (one line per call, no note
  text) and `pending.json`. A call with an unknown outcome stops the run and blocks writes until a
  later inventory shows it landed or, read after its bound plus 10 s, that it can no longer land.
  There is no manual override. Exit 0 done, 1 refused or stopped, 2 usage.

## Hourly sync (ANYM-536)

`feedback_sync.py auto` does the hand steps above in one run, under one lock
(`~/.deedkit/feedback-sync/lock`, freed when the process ends):

```sh
python3 scripts/feedback_sync.py auto            # plan only; records nothing
python3 scripts/feedback_sync.py auto --apply    # writes, then last-run.json
python3 scripts/feedback_sync.py auto --retire <triage state>/dispositions/<file>.json
python3 scripts/feedback_sync.py status
```

- Apply queue: every `dispositions/*.json` of the triage state (`--triage-state`, default
  `~/.deedkit/feedback-triage`) that `applied.json` (file name, SHA-256, rows done) does not
  record as complete. A row is written with `--unchanged-since` at its `decided_at` (at most the
  export head minus 10 s); an item changed after that time is `superseded` and keeps its value, so
  a hand correction stands. An equal row republished later keeps the earliest decision time, which
  `applied.json` keeps before any call, also after its file is refused or removed. A
  file with a row without `decided_at` (from before ANYM-536) is listed `legacy` and stays a hand
  `apply`; `--retire` records it complete without a write. A row skipped as `changed during read`
  or behind a pending call keeps its file open. A ledgered file whose SHA-256 changed is refused
  and named (exit 1); the other files and `follow` still run.
- Link: a `received` item without a card whose full ID is in a `Feedback:` line of exactly one
  card on Tiden main becomes `accepted` with that card and the note "Carded as ANYM-n; it follows
  that card."; the next run follows the card. Two or more cards: `several cards` with their IDs, no
  write. A line in a fenced block, an HTML comment or a quote does not link. An item never set is
  linked too, one call each; an unknown ID is reported `not linked` and the run goes on.
- Follow: as `follow` above. A card on main is recorded in `cards.json` with the first time seen;
  an `accepted` or `received` item whose card was seen and is gone is `card missing`, one whose
  card was never on main (a proposal branch match) is `waiting for main` with its age. Neither
  writes: a decline is a `declined` disposition (note "Card ANYM-n was closed without delivery;
  this report is closed.") in the next triage or by hand, which the queue then applies.
- Fail closed: the inventory, Tiden main, `gh`, `git fetch` and the receipts are all read before
  the first write; a failed, timed-out, non-JSON or truncated read ends the run with exit 1 and
  no write. ssh runs with `BatchMode=yes`, git and `gh` with prompts off, stdin is never a
  terminal, every subprocess has a timeout.
- `last-run.json` (mode 0600): time, exit code, export head, per reason the feedback and card
  IDs, items waiting for a card or for main with their age in hours, the files (complete, open,
  legacy, refused), the pending call, `last_ok`; never note text. `status` prints it.

### LaunchAgent `ai.deedkit.feedback-follow` (orchestrator machine)

Prerequisites: a clean checkout of `main` used only by this job (`git pull --ff-only` there
after a release; the run itself only fetches), an SSH key for `deedkit-prod` that needs no
prompt, stored `gh` and Tiden tokens, and a logged-in macOS session. A run missed in sleep runs
once on wake (launchd `StartInterval`). Install outside Git, in
`~/Library/LaunchAgents/ai.deedkit.feedback-follow.plist`, with your absolute paths:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.deedkit.feedback-follow</string>
  <key>ProgramArguments</key><array>
    <string>/Users/OWNER/.local/bin/uv</string><string>run</string><string>--no-project</string>
    <string>--python</string><string>3.13</string><string>python</string>
    <string>scripts/feedback_sync.py</string><string>auto</string><string>--apply</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/OWNER/CHECKOUT</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/Users/OWNER/.local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key><string>/Users/OWNER/.deedkit/feedback-sync/launchd.log</string>
  <key>StandardErrorPath</key><string>/Users/OWNER/.deedkit/feedback-sync/launchd.err.log</string>
</dict></plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.deedkit.feedback-follow.plist
launchctl kickstart gui/$(id -u)/ai.deedkit.feedback-follow    # first run now; then `status`
launchctl bootout gui/$(id -u)/ai.deedkit.feedback-follow      # undo; then delete the plist
```

Stopping it ends future runs only; writes already made stay. A hand `apply` or `follow` stays
possible (same lock).

## Tool-less reader (ANYM-570)

For the feedback autoloop (docs/operations/feedback-autoloop.md) the decisions come from
`scripts/autoloop.py read` instead of a session: after `snapshot`, `pull` and `match` it gives at
most 20 inbox items with their candidates, on stdin, to the pinned Claude Code CLI on the owner's
subscription login with `--tools ""`, `--setting-sources ""`, `--strict-mcp-config` and an empty
MCP config, `--disable-slash-commands`, `--no-session-persistence` and stream-json output, with
only `PATH`, `HOME`, `USER` (the login keychain needs it), `CLAUDE_CODE_DISABLE_ATTACHMENTS=1` and
`CLAUDE_CODE_DISABLE_FILE_MENTIONS=1` in an empty temporary directory. Every `@` on stdin is
escaped as `\u0040`, so an `@path` in feedback never pulls a local file into the request. It
takes the triage lock and its own (`~/.deedkit/autoloop`, or `AUTOLOOP_STATE`). With an empty
inbox it makes no model call; it only publishes ledger entries a stopped run left unreported.

- Before any answer is used, the stream's `init` must match the profile pinned with the CLI
  version: `tools`, `mcp_servers`, `slash_commands` and `skills` present and empty; plugins and
  agents exactly the pinned built-in sets (plugins with `path: builtin`). Any other event than init, assistant text,
  rate-limit events and one result (a hook event, a tool call) refuses the run; nothing is
  recorded.
- The answer is closed: `{"decisions": [...]}` with `id`, `classification` (the classes above
  plus `already-live`), `specs`, `primary`, `duplicate_of`, `new_card`, `reason_code` (an enum;
  the ledger reason is the code's fixed text), `answer` and `card_brief`. An unknown field, an ID
  outside the input or twice, a spec outside the snapshot, a bad slug or verdict, or `new_card` on
  anything but an unmatched, non-duplicate bug or feature refuses the whole answer, and so does
  a decision `record` would refuse: the batch is checked whole before anything is written.
  `already-live` needs one accepted Done spec on main and is recorded as an accepted bug or
  feature, which the ANYM-536 follower moves to `verify_requested`.
- `answer` and `card_brief` lose URLs, URIs, addresses, code fences, shell patterns and paths
  and stay untrusted; card briefs go to `briefs.json` in the autoloop state, and the card worker
  (ANYM-571) takes one only through `committed_briefs`: its item is in the ledger with the same
  new card.
- A new CLI version needs a new pin: update `CLI_VERSION` and `PROFILE` in `scripts/autoloop.py`
  after the canary run on the Mac: `AUTOLOOP_CANARY=1 uv run pytest tests/platform/test_autoloop.py
  -k canary` (hostile settings, hook, MCP config, plugin, memory file and symlink around the
  reader's directory; a read, `@` mentions of a file and a symlink, and a write outside asked by
  the item; the canary text must not come back; one valid decision expected).

## Hourly tick (ANYM-569)

`scripts/autoloop.py tick` moves feedback through the loop once (docs/operations/feedback-autoloop.md
holds the boundary, caps and undo). It takes the triage lock, the sync lock
(`~/.deedkit/feedback-sync`) and its own, non-blocking in that order; if one is held (a manual
triage, the ANYM-536 `auto`, another tick) it prints `busy: …`, writes nothing and exits 0.
Dispositions stay in the triage state; the tick keeps no second copy. Its state
(`~/.deedkit/autoloop`, or `AUTOLOOP_STATE`) must be 0700 outside every Git checkout, its files
are 0600, and `config.json` must be 0600:

```json
{"live": false, "principals": ["<agent principal from list-keys>"], "tz": "Europe/Moscow",
 "caps": {"items_per_principal": 20, "reader_runs": 24, "card_launches": 5, "build_launches": 3,
          "card_in_flight": 2, "build_in_flight": 2, "worker_hours": 12}}
```

Every key is optional (the values above are the defaults, with `tz` `UTC` and no principals).
One tick:

1. `PAUSE` present: fence only (below), no read, triage or launch.
2. Reads, all before the first write: the workspace list (only when an intent is open), Tiden main
   and open branches, the feedback export. A failed, truncated or timed-out read ends the tick with
   no write (exit 1).
3. Writes the snapshot, pulls into the inbox, matches; settles `unknown` intents by workspace name.
4. Control, from principals in `config.json` only and as the whole item text: `AUTOLOOP STOP`
   writes `PAUSE` and fences; `AUTOLOOP DROP <feedback-id>` (an item of the same principal) holds
   the item and its card and declines it "withdrawn by the reporter" (an inbox item with the batch,
   a triaged one through an `autoloop-drop-*.json` dispositions file that `auto` applies). Control
   items are recorded as declined `autoloop control`. Any other text, `AUTOLOOP START` included, is
   ordinary feedback: nothing resumes by feedback.
5. `live: true`: the reader (above) gets the oldest items within each principal's daily item cap,
   if a reader run is left today; the run is counted before it starts and never refunded.
   `live: false`: nothing runs; the plan lists what would.
6. Writes `plan.json` (IDs, counts, statuses, cluster provenance: `owner-agent` when every item of
   the cluster comes from a listed principal, else `report-only`) and `last-tick.json`.

The tick prints one line of IDs, counts and statuses, never feedback text. Days for the caps run
in `tz`.

- Launches (ANYM-571, ANYM-572) go through `admit`: the in-flight, daily-launch and worker-hour
  caps, then an intent `intents/autoloop-<card|build>-<card>.json` with its full wall-clock
  reservation (card 2 h, build 4 h) is written before the start. A start writes `unknown` before
  its call; an `unknown` intent becomes `started` or `lost` by its workspace name in
  `superset workspaces list`, never by a second start. A started intent whose workspace is gone
  without `autoloop.py finish <intent>` is `interrupted`. Lost and interrupted runs keep their
  reservation; `finish` returns the unused part to the day it started. One relaunch per card
  (`-r1`), as a new reservation.
- Fence (`PAUSE`): closes the terminals of workspaces named by `intents/` only, cancels queued or
  running ready merges (`tiden-sync.yml` dispatch) of `proposal/autoloop-` branches, disables
  auto-merge on and dequeues open `autoloop-` PRs, then reads all three back. `fence.json` says
  whether the stop is effective (clean read-back, nothing alive, no failed call) and lists what
  landed after the `PAUSE` time (merged PRs with their revert, successful ready merges) and holds
  those cards. Intents with no live terminal become `stopped`. The tick exits 1 while the stop is
  not effective. Resume: remove `PAUSE` (the owner, or the orchestrator on the owner's words).
- `autoloop.py gate <intent>` (workers, before every Tiden write, push, ready dispatch and merge
  arm) exits 1 on `PAUSE`, a held card, a dropped item or an intent that is not running.
  `autoloop.py hold|release ANYM-n` is the orchestrator's per-card control.
- `autoloop.py status` exits 1 when the last good tick is older than 2 hours, the stop is not
  effective, or `PAUSE` starts with `audit`.

### Superset automation `deedkit-autoloop` (owner Mac)

Prerequisites: `superset`, `gh` and `tiden` logged in on the Mac, the owner's SSH alias
`deedkit-prod`, the pinned reader CLI (above), `config.json` written by the owner. Create it once on
the local deedkit workspace on `main`:

```sh
superset automations create --name deedkit-autoloop --rrule 'FREQ=HOURLY;BYMINUTE=20' --local \
  --workspace <local deedkit workspace on main> --agent claude \
  --prompt 'Run exactly: git pull --ff-only && uv run python scripts/autoloop.py tick. Reply with its exit code and its last output line only. Run nothing else.'
```

The orchestrator's wake check runs `uv run python scripts/autoloop.py status`. Undo:
`superset automations pause deedkit-autoloop` (or `delete`); `touch ~/.deedkit/autoloop/PAUSE`
stops the loop itself.
