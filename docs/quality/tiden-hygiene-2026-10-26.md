# Tiden hygiene receipts, 2026-10-26 (ANYM-505..509)

Closes the 2026-10-24 Tiden hygiene epic after ANYM-503 and ANYM-504 (#285). Every Tiden write ran in an
orchestrator-held maintenance window: `tiden-sync.yml` was disabled and pushes were held, and the window
used before and after snapshots with `scripts/tiden_snapshot.py`. `scripts/verify_tiden_preservation.py`
checked each window against an allowed file built by a checker. Every write was read back. Private logs and
plans are kept in the orchestrator's research directory, not in Git.

## ANYM-509: local Tiden CLI 0.41.0
The owner's prepared install ran on 2026-10-26. The checksum of `tiden_darwin_arm64.tar.gz` equals the pinned
value, and the installed binary is identical to the verified one. `tiden version` reports 0.41.0. The backup
reports 0.38.0. Claude settings and Tiden config are unchanged. The step is test-free by nature: it changes
the owner's tools, not the repository. Undo: reinstall the kept 0.38.0 backup.

## ANYM-506 and ANYM-505: window 1 (05:15-07:21Z)
- The plan was reviewed by Astra r1 and Sol r2 and then frozen.
- Component paths were set as a union on 6 components. `spec derive-components` attributed exactly the 9
  predicted specs. `spec update --component-id` set 63 specs, including the fixes 430 → Gateway & Access,
  473 → Python Client & Operator CLI and 446 → Subscriptions & Events.
- 60 `repo_file` anchors were added on 39 specs. Each source list was resent with every existing source and
  its provenance.
- Preservation: 108 requirements and 6 components were allowed, with 0 problems; `preserved: true`.
- After the window: sync of c518e3e succeeded, drift succeeded, health passed (0 stale anchors of 1951).
- Step 10, after the next sync B (4b69376): all 69 planned specs keep their component.
- The intake rule (criterion 5) is item 6 of docs/operations/tiden-product-workflow.md (#289). Each
  unowned code file got a component path or an anchor in the reviewed plan, except that
  11 files stay unanchored on purpose (configuration and repository metadata): `.gitattributes`,
  `.github/dco.yml`, `.github/workflows/security.yml`, `config/modules/{calendar,messaging}.toml`,
  `config/stack.toml`, `scripts/check_stack.py`, `services/calendar/.gitattributes`,
  `services/calendar/.github/workflows/ci.yml`, `services/calls/.gitignore` and
  `services/calls/frontend/package.json`.

## Window 2 (08:00-11:00Z): components of the newer Backlog specs
- `componentId` was set on 28 specs: 517-522, 528-535 and 543-556. The rule is item 6 of
  docs/operations/tiden-product-workflow.md: the component that owns the card's expected paths in
  ARCHITECTURE.md.
- Sol r1 changed two picks: 518 → Platform Gateway and 534 → Deployment & Verification. The orchestrator
  picked the two ambiguous ones: 521 → Subscriptions & Events and 543 → Platform Integration & Operations.
- Preservation: 28 requirements were allowed, with 0 problems; `preserved: true`. `tiden gate check` names
  no "No component" subject.
- After the window: drift succeeded and health succeeded. No main SHA was missed.

## ANYM-507: specs with no linked test
- Start read (`coverage-gaps`): none 42, stale 93, partial 96, covered 217. `derive-links` was not run.
- 225 and 255 (Review) are not implemented on main, so they get no link. Card candidates: implement them or
  return them to Backlog.
- 314 already states its reason on the spec. 260 (index) and 330 (research) get Boundary reasons on the
  Tiden branch `review/2026-10-26-tiden-hygiene-close`, which this PR merges.
- The stale 424 and 501 are now partial. Their links were re-checked against the current text and confirmed.
- Backlog specs are left as they are; their tests come with their implementation. No link was written or
  removed. The 93 stale specs came from the window's metadata update.
- End read (`coverage-gaps` after window 2, 469 specs): covered 288, partial 117, none 62, stale 2. The
  none count includes the 21 newer Backlog specs. 501 is now covered, and 424 is partial.

## ANYM-508: partial-coverage pilot on Archive & Search
- 32 specs and 293 criteria were classified. A (linked test asserts it) 169, B (unlinked test asserts it) 53,
  C (implemented, no test) 55, L (not testable) 16.
- 86 add-only links were added (62 tests → 20 specs). A reviewer checked each pair against the test code, and
  each link was read back with `tiden test links`.
- Missing tests: 55 criteria on 18 specs. On 34 and 35 the tests assert only the error type, not the code.
- Time: about 2 h of agent work, inside the 4 h box.
- The end read has 117 partial specs. 22 of them are in the sample (10 sample specs are now covered), so
  95 are others. They are not declined; they become two follow-up card candidates:
  - a partial-coverage pass by component group, one session each, with the same method;
  - the missing Archive & Search tests.

## ANYM-405 (delivered here)
All criteria are met on main:
- check A: #122, #135;
- sync B: #136 (dry run), #139 (writes after three green A runs);
- U+2028 fix: #203;
- ready-merge job: #288.

Two lines were superseded by later accepted cards. CLI 0.41.0 is pinned by #272. Go tests are now linked by
ANYM-421 (#171).

## Card candidates found on the way
- 225 and 255 are in Review without an implementation.
- The window procedure should require the push hold from the before snapshot on: CI test imports broke the
  snapshot in window 1.
- A snapshot read can fail on a transient `test links` error (window 2); the snapshot could retry one read.
