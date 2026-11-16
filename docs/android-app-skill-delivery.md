# Android app skill delivery (persistent Codex)

Spec: ANYM-993. Source: `services/android/src/deedkit_android/app_skill_bundle.py`.

## What ships

`deedkit_android/app_skills/v1/` holds exact copies of the four app navigation skills from
PR #1007 at commit `96e2be8429cbb4cef50f4d8eee5f75b2741f2cce`. `SKILLS` records each file's
package, SHA-256 and Git blob ID. The bundle `DIGEST` covers the version, the source and those
records. The wheel and the editable `uv sync` install carry the directory as package data, so
installing the service needs no extra step.

## Native contract (Codex 0.166.0, verified from source at tag `rust-v0.166.0`)

- `app-server/src/extensions.rs` installs the skills extension with a host provider. Its
  `requires_host_skill_discovery()` is therefore true, so `features.skip_host_skill_discovery=true`
  (`core/src/session/turn_context.rs`) does not empty the host catalog in app-server. The current
  session already discovers `$CODEX_HOME/skills`, `$HOME/.agents/skills` (`HOME` is the session
  directory), the `.agents/skills` directories between the project root and the session directory,
  and `/etc/codex/skills`. `skills.bundled.enabled=false` still removes system skills. This change
  keeps both flags.
- The `skills/extraRoots/set` app-server request (`SkillsExtraRootsSetParams { extraRoots }`)
  replaces the process's extra host roots and clears the catalog cache. The next turn lists those
  skills in the ordinary catalog (`app-server/tests/suite/v2/host_skills.rs`). Explicit
  `UserInput::Skill` selection reads only that same catalog, and plugin or executor roots would
  widen scope, so neither is used.

## Behaviour

Before each persistent turn, `PersistentRun.execute` calls `offer(request.apps, codex_home, cwd)`.

- If `apps` is empty or lists none of the four packages, there are no roots and no receipt. If the
  process holds no roots, no request is sent, so the request, result, sandbox and argv stay
  exactly as before.
- If `apps` lists one of the packages, the bundle must hold exactly the recorded files, with no
  symlinks or extra files, and every hash must match. The bundle must also lie outside the
  native-writable session directory. Finally, no skill named like one of the four (by directory or
  front matter) may exist in a root native already discovers. If every check passes, the session
  sends `skills/extraRoots/set` with the read-only package root, and the result carries
  `app_skills: {version, source, skills, digest, apps, status: "offered"}`. Otherwise it sends an
  empty root list (only if roots were set before), and the result carries `status: "refused"` with
  a `reason`: `bundle_unavailable`, `bundle_foreign_asset`, `bundle_digest_mismatch`,
  `bundle_native_writable`, `same_name_skill_present` or `skill_scan_incomplete`.
- A later turn with unrelated apps resets the roots to `[]`. An unchanged root list sends nothing.
- Nothing is written into `CODEX_HOME` or the session directory, so a user-authored or
  agent-authored file is never overwritten. The skill text is never added to the turn input, the
  thread start, the instructions or the owner goal. The reader sees ordinary catalog entries and
  may decline them.

## Rollout

Deploy through the normal release route. A running app-server process is never changed or
re-attached. The roots apply when an existing process starts its next relevant turn, or when a
process starts after a restart (`thread/resume` keeps the same thread). Root owns installation,
detection of the installed version, and live A/B measurement.

Rollback: install the previous release. That version never sends `skills/extraRoots/set`, and a new
process starts with no extra roots.

## Refreshing the skills

When the reviewed skill sources change, add `app_skills/v2/` from the exact new blobs, set
`VERSION`, `SOURCE` and `SKILLS`, and keep `v1` until the release that drops it.
