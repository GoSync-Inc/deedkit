# Android app navigation skills

`skills/android-{gojek,grab,halodoc,tokopedia}-navigation/SKILL.md` are ordinary Codex skills
for the bare Android agent (ANYM-993). Each holds short, private-data-free facts from one
read-only audit: observed routes, untested candidates, auth barriers, and where the UI
hierarchy text is complete or a screenshot is needed. The agent selects them by description;
nothing injects them into the prompt and they impose no per-step rule.

## How the agent finds them

`BareRun` (`agent: {guarded: false, persistent: false}`) runs `codex exec` with the configured
`codex_home`, links `<codex_home>/skills` into the run's `.agents/skills` and adds it with
`--add-dir`; Codex discovers skills there natively. The persistent app-server mode is offered an
exact versioned copy of these skills as a read-only extra root
([android-app-skill-delivery.md](android-app-skill-delivery.md)); native 0.166 also keeps its
ordinary host discovery there.

The repository has no install step for this directory into `<codex_home>/skills`: the Android
wheel carries only the versioned copy for persistent mode, and `scripts/production.py` does not
ship it. `services/android/tests/test_app_navigation_skills.py` checks
the Codex frontmatter and that a copy under `<codex_home>/skills/` is discovered unchanged.

## Install (root, on the Android host)

Only while no agent task runs, as the service user:

1. Back up: `cp -a <codex_home>/skills <codex_home>/skills.bak-<date>`.
2. For each of the four names, if `<codex_home>/skills/<name>` exists, diff it first: the agent
   may have refined it, and a run reports such edits in `skills_updated`.
3. `cp -R skills/android-<app>-navigation <codex_home>/skills/` from a green `main` checkout;
   keep the directory 0700 and owned by the service user.

Undo: remove the four directories or restore the backup. Learned skills with other names stay
untouched.

## Sources

Approved audit reports, 2026-11-15; versions from static metadata:

- GoJek 5.82.1: GoFood text audit (@thread:thr_i7bnn26iq2) and offline efficiency analysis
  (@thread:thr_rvtmd7svmc). GoFood-versus-GoMart and the Pay wallet shortcut are provisional,
  caller-described hints.
- Grab 5.430.0: signed-out audit stopped at the Food login gate (@thread:thr_tv5eek76wr).
- GrabMart, 2026-11-16 (app version not recorded): retained screens of three signed-in owner
  runs, read privately on the phone host (DK-48, @thread:thr_5mz4jwqkh6). They show the Home
  search field and Mart entry, the store-only "Search this store" and merchant results; a
  cross-store product search was not observed. The skill also quotes the owner's preference to
  look for one first. Merchant names and frames stay on the host.
- Halodoc 31.000: doctor directory and Health Store audit (@thread:thr_69aesa8kiz).
- Tokopedia 45.0.7: search, product, filter and Transaction audit (@thread:thr_dnb4442wqb).

No deep link was verified in any app. Restaurant, menu, product, seller, account and order
values from the audits stay out of the skills. Update a skill from a new approved audit or a
learned edit that contains no private data.
