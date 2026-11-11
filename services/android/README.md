# Android device service

Runs on the Mac or Linux host connected to an Android phone over USB. The DeedKit
gateway calls this service over a private network. The gateway remains on its
existing server and exposes the `android` module through `/mcp/modules/android`.

```
Grok → DeedKit MCP gateway → private HTTP → USB host → ADB → Android apps
```

The client agent chooses each action. A persistent uiautomator2 worker on the USB
host executes selector-based actions and reusable workflows. It is the only UI
engine: the Maestro engine was removed on 2026-11-11.

## Tools

| Tool | Purpose |
| --- | --- |
| `android_capabilities` | Granted devices, connection states and command names |
| `android_session` | Acquire, renew or release exclusive control of a phone |
| `android_observe` | PNG screenshot plus Android UI hierarchy |
| `android_execute` | Run device-targeted ADB arguments and retain a result receipt |
| `android_maestro` | Read the supported dialect or validate a flow without execution |
| `android_run_flow` | Run inline or saved flow YAML with a durable receipt |
| `android_flows` | List, save, read and delete reusable owner-scoped flows |
| `android_get_operation` | Read or wait for the receipt using the original request UUID |
| `android_upload` | Upload an APK or another file in bounded chunks |
| `android_download` | Read command output or a pulled file in bounded chunks |
| `android_delete_file` | Release stored files after use |

`android_execute` accepts `shell`, `exec-out`, `reboot`, `logcat`, `get-state`,
`get-devpath`, `wait-for-device`, `root`, `unroot`, `remount`, `disable-verity`,
`enable-verity`, `reconnect`, `jdwp`, `push`, `pull`, `install`, `install-multiple`
and `uninstall`. Shell commands can run Android input, intents, package and settings
operations. Android's own permission checks still apply. `wait-for-device` and
`reconnect` take no further arguments. ADB server administration and arbitrary
commands on the USB host are outside this interface.

`push` takes a completed upload UUID and a device destination. `pull` takes one
device file path and returns a download UUID. It transfers regular file bytes.
`install` takes options followed by one upload UUID. `install-multiple` takes
options followed by one or more upload UUIDs. Each completed upload is staged
under a private `.apk` name for that command. Supported switches are `-r`, `-t`,
`-d`, `-g`, `-s`, `-f`, `-p`, `--instant`, `--full`, `--dont-kill`,
`--enable-rollback`, `--force-queryable`, `--no-streaming`, `--streaming`,
`--no-incremental`, `--staged` and `--force-non-staged`. The options `-i`,
`--abi`, `--user`, `--install-location` and `--install-reason` take one value.
Values contain letters, digits, underscores, dots or hyphens and cannot end
in `.apk` or `.apex`. Shell and `exec-out` retain ADB's command-line quoting
semantics.

The MCP observation result contains a native image content block. Its structured
result includes image size and SHA-256, plus the UI nodes. The REST result carries
the PNG as base64. Password nodes omit their text and content description.
Secure Android screens may withhold screenshots or hierarchy information.

## Install on the USB host

Install Android SDK Platform Tools from Google's official distribution and
authorize this host on the unlocked phone. Confirm its serial using `adb devices -l`.
Install the service from this checkout with Python 3.13 and uv.

```sh
cd services/android
uv sync --locked
```

Write a private configuration file with mode `0600`. The serial, binary path and
state directory below are examples. Hash a separate random backend token using
SHA-256 and put only its hash in this file.

```json
{
  "adb": "/opt/android/platform-tools/adb",
  "state_dir": "/private/deedkit-android/state",
  "devices": {"home-phone": "YOUR_DEVICE_SERIAL"},
  "principals": [{
    "id": "home-agent",
    "key_sha256": "REPLACE_WITH_64_LOWERCASE_HEX_CHARACTERS",
    "devices": ["home-phone"],
    "permissions": ["android:read", "android:control"]
  }]
}
```

Start one process, using the host's actual Tailscale IPv4 address for a gateway
on another machine. Loopback is the default and is suitable for an SSH tunnel.

```sh
uv run deedkit-android --config /private/deedkit-android/config.json --host 100.64.0.10
```

Use launchd on macOS or a service manager on Linux for persistent operation.
Supply absolute paths and one worker. A file lock prevents two service processes
from sharing the state directory. Expose the port only to the gateway through
the private network. Keep ADB connected over USB.

## Connect the gateway

Deploy a gateway build containing the Android catalog before adding `android` to
its policy. Add the private service URL under `deployment.backends.android`, include
`android` in `deployment.modules`, and add the eleven exact tool names to the chosen
profile. Grant that principal `android:read` and `android:control`. Its Android
credential uses a private `key_file` holding the backend token and `key_sha256`
matching that token. The MCP client continues using its separate gateway token.

Follow the gateway's existing policy replacement procedure and increment its
revision. Backend URL changes require a gateway restart. For rollback, remove
the Android policy entries before returning to an older gateway build.

Use one backend principal and token for each independently authorized gateway
principal. Receipts, reservations and files are scoped to that backend identity.
Grant both scopes to an agent that operates the phone. A read-only principal can
inspect capabilities and its previously created receipts and files.

## Execution and recovery

1. Read capabilities and acquire a session for the chosen device.
2. Observe the screen, then execute one intended action with a fresh request UUID.
3. Keep that UUID and read its receipt until it leaves `running`.
4. Observe again before deciding on another UI action.
5. Release the reservation when finished. Valid activity and running commands renew
   it automatically. The default inactivity interval is 900 seconds. Explicit
   `seconds` remains supported from 30 to 900.

A duplicate request UUID with the same device, arguments and command timeout
returns its existing receipt. Different arguments produce `request_id_conflict`.
Completed receipts remain readable after reservation expiry. Only one command or
observation runs on a device at a time, including after its reservation expires.

The service records admission before starting a command. Timeout, service shutdown
or restart may leave `unknown` because the phone can already have acted. Inspect
the phone and recover using the original UUID. The service never replays such a
command. An interrupted HTTP response can leave a command running in the service.
The gateway includes the original UUID in its unknown-outcome response.

An acquire whose reply is lost keeps its reservation until the bounded expiry.
Acquire a new session after that time. Uploads accept identical chunk retries,
including a partially written final chunk after a crash. File deletion is idempotent
and waits until device commands have finished.

Commands have a 1–600 second timeout and can be polled with a 0–25 second wait.
Uploads hold up to 32 KiB per chunk. Downloads accept up to 4 MiB per call, with
the previous 32 KiB default retained. Files hold up to 256 MiB and stored files up to 512 MiB
in total. Delete completed transfers to free the quota. Command stdout and stderr
are bounded at 4 MiB each. Large or binary stdout becomes a downloadable file.
Observation PNGs have a separate 16 MiB ceiling, to preserve full-resolution phone screenshots.
Receipts persist in SQLite. Preserve the state directory across upgrades to retain
duplicate-request protection.

## Device workflows

The recommended engine is persistent uiautomator2 3.7.1. Build the patched device
server with `tools/build_uiautomator.py` on a host with JDK 17 and Android SDK 35.
The script pins upstream source, binds the server to `127.0.0.1`, removes request
body logging and suppresses clipboard/toast information logs. Record the printed
SHA256, stage the artifact beside the service release, and set absolute
`uiautomator2_jar` and matching `uiautomator2_jar_sha256` in the private config.
The service verifies the artifact before starting. Each device gets one lazy,
persistent worker and a loopback-only server reachable through USB ADB.

Read `android_capabilities.ui.engine`, then `android_maestro(action=reference)`.
The latter keeps its existing compatibility name and reports the active engine's
commands. Validation rejects unsupported commands before the first action. On
uiautomator2, `text` and `id` are regular expressions, while `textExact` and
`resourceId` are exact. `setText: {resourceId: example:id/search, value: '${QUERY}'}`
replaces an input's value directly. `inputText` appends to the focused text field
(`EditText`/`AutoCompleteTextView`); a focused keyboard key is never the target, and with no
focused text field the step fails with `focused_field_not_found` before any action.
`exists` returns a boolean in the corresponding step result. Legacy
`retryTapIfNoChange` is accepted without automatic repeated taps. Explicit
`waitToSettleTimeoutMs` adds a post-tap wait. Relative/image selectors, clipboard,
location and display settings use the existing ADB entry point on this engine.

The batch response adds `engine`, `elapsed_ms` and `steps`. Each step records its
command name, nested path, elapsed milliseconds and result or error. One
`android_run_flow` call executes the whole batch on the USB host, including waits.
Use several local steps per call to avoid network/model round trips. Original
PNG download and failure capture follow the same contract below.

Some apps draw text that the accessibility tree does not expose. Two read-only
tools help. An app map may set `hierarchy: {compressed: false, max_depth: 120}`;
every snapshot for that map then dumps the full tree instead of the default
compressed tree of depth 50. When the text is still missing, `readScreenText`
captures one PNG and recognizes it on the USB host: with Apple Vision on a
macOS host that has the `vision` extra, otherwise with Tesseract (`--psm 11 tsv`).
It accepts `region: {x, y, w, h}` or `within: <selector>`, `min_confidence`
(default 50 with Vision, 60 with Tesseract), `limit` (default 100, at most 300) and `key`
(default `screen_text`). It returns lines with `text`, device-pixel `bounds` and
`confidence`, top to bottom, in the step result and in `receipt.data.<key>`. The
PNG lives in a private temporary directory removed after each call; nothing leaves
the host except the authenticated receipt. It never taps. Enable it with absolute
`ocr_command` (for example `/opt/homebrew/bin/tesseract`) and `ocr_languages`
(default `eng`; `eng+ind` needs the `ind` language data, Homebrew
`tesseract-lang`) in the private config. Without them, or when recognition fails,
the step fails with `ocr_unavailable` or `ocr_failed` as a definite failure.
Older service versions reject these keys, so roll back the config with the
executable.

`structure: true` adds a layout built from the OCR engine's own words and their
geometry, never from a model: `blocks`, each with `lines` (with a `row` index for
lines that share a row) and `words`, all with bounds and confidence. A clickable
or identified tree container (for example a textless order card) groups the
text inside it; elsewhere a vertical gap above 1.5 line heights starts a new
block. Each block names the smallest tree node that contains it, and `source:
tree` marks text the tree already exposes. Consecutive blocks of the same shape
form `lists` of item block indexes. `tree` says whether the hierarchy was merged.
The receipt then keeps `{lines, blocks, lists, tree, truncated}` under the key.

**Engine.** There is no engine option for callers; the host decides and `ocr.engine`
names the one that read. Install the service with `uv sync --locked --extra vision`
on macOS to read with Apple Vision (VNRecognizeTextRequest, accurate, language
correction on, in the worker process; PyObjC pinned in `pyproject.toml`). The
config's `ocr_languages` codes map to Vision's (`eng+ind` to en-US and id-ID), and
`ocr_command` stays required: a Vision error or a call over 20 s falls back to
Tesseract. Vision's word boxes are tightened to their ink (`ocr.bounds: ink`) so
the structure below groups rows as with Tesseract. Vision reports confidence 30,
50 or 100. Bake-off on the mini, 11-11, four real Tokopedia screens, 31 expected
strings: Tesseract 27, Vision 30; 0.7-1.0 s per screen warm on the mini (first call about 8 s), Tesseract 1.3-1.9 s. Without the extra
(Linux, CI) nothing changes. Roll back by installing the previous release.

Before Tesseract, the screenshot is normalized on the host (`ocr.preprocess:
local_contrast`): each pixel's distance from its local background, so light text
on coloured buttons and dark themes becomes dark on white at the same size and
coordinates. Quality is measured on the committed synthetic corpus
(`tests/fixtures/screens`, rendered by `tools/make_screen_corpus.py`):
`tests/test_screen_quality.py` holds character error rate at most 2% and exact
list item counts. For real screens, `tools/evaluate_ocr.py SET_DIR --command
<tesseract> [--engine vision]` prints the same numbers, never text, for a labelled set kept only in
the host's private state directory; `--tsv-command` runs another local engine
that prints Tesseract TSV for a bake-off.

A screen signature can match while content still loads. Add the spinner to the
screen's `forbid` list so screen waits continue until it is gone. `waitSettled`
(`stable_ms` 100-5000, default 500, and `timeout`) waits until the map's
hierarchy (class, id, text, description, bounds) stays unchanged for `stable_ms`
and fails with `screen_not_settled`, a definite failure. `openApp` and
`openDeepLink` accept `settle_ms` (0-5000) to settle after their screen check.
`openDeepLink` returns `phases` (`open_ms`, `screen_ms`, optional `settle_ms`) and
the number of screen `polls`, which explains warm-run variance. `- sleep: 500`
pauses for milliseconds (at most 10000) and is a last resort.

`inspectMap` keeps its match counts by default. `hierarchy: true` adds compact
nodes (index, depth, class, id, text, description, bounds, clickable,
scrollable; password text omitted; `max_nodes` 1-500, default 200).
`suggest: true` adds unique selectors (id, then exact text, then description)
and list candidates: an id or class repeated under one parent, with its count,
field ids present in at least half of the items and `has_text`. A candidate
without text points to the full-hierarchy option and `readScreenText`.

The Maestro engine was removed. A private config that still sets `maestro` or
`java_home` stops the service at start with `maestro_engine_removed`; delete both
keys when upgrading, and restore the previous wheel and config together on
rollback. Without `uiautomator2_jar` the service keeps raw ADB and observation,
and `android_run_flow` returns `ui_engine_not_configured`. Saved command lists run
unchanged on uiautomator2. A Maestro-only command (for example `travel`,
`setLocation` or `setAirplaneMode`) fails validation before any device action
with `unsupported_uiautomator_command` and a hint to use `android_execute`. The
tool keeps its compatibility name `android_maestro`.

`android_run_flow` takes `device_id`, `session_id`, `request_id` and exactly one of
`yaml` or a saved `name`. `parameters` supplies literal `${NAME}` substitutions.
`version` pins a saved flow revision. `timeout_s` bounds the entire run from 1 to
600 seconds and `wait_s` controls how long the HTTP call waits for its receipt.
Use `android_get_operation` to poll after a `running` response or lost reply.
A duplicate UUID never repeats admitted actions.

Missing-element probes use `element_timeout_ms=5000`, or
`optional_timeout_ms=1000` for optional selectors and `when`/`while` conditions.
Both accept 100–60000 ms. A selector's `timeout` or a condition's `timeout`
overrides the run default. Native `extendedWaitUntil` keeps its explicit timeout.
The worker waits for the selector with the bounded timeout, then acts only after
a match; nested flow, repeat and explicit retry blocks retain this behavior.
Probe limits exclude cold driver startup and an action after the probe (whose
element may disappear in the meantime). The total `timeout_s` still bounds
execution. Completed lookup misses keep the warm worker; transport errors fence
and restart it.

```yaml
- tapOn: {text: Optional popup, optional: true, timeout: 300}
- runFlow:
    when: {visible: {id: known_dialog_button}, timeout: 500}
    commands:
      - tapOn: {id: known_dialog_button, timeout: 500}
```

Use a saved `runFlow: {name: dismiss-known-dialog}` for a reusable handler of
known dialogs. The service does not dismiss arbitrary permission or payment
screens automatically. Input is a command list, without native `appId`/`---`
headers, and checkpoints use the scalar form `- checkpoint: review`.

```yaml
- launchApp: com.example.app
- tapOn:
    id: com.example.app:id/search
- inputText: ${QUERY}
- pressKey: Enter
- extendedWaitUntil:
    visible: Results
    timeout: 10000
- checkpoint: review_results
- back
```

Save this through `android_flows` with `action=put`, `name` and `yaml`, then pass
its returned `version` on subsequent reads or runs. Save, read and delete accept
a version for optimistic concurrency. Each principal can store 128 flows, each
up to 32 KiB. `android_capabilities` includes supported commands and input schemas.

The default `launchApp` resumes the app and leaves runtime permission states
unchanged. Explicit launch options can override these defaults. The supported
subset includes text and resource-ID selectors, Unicode input, key presses,
swipes, waits, inline `runFlow`, `repeat` and `retry`. Host scripts, JavaScript
expressions, external YAML paths and image-file selectors are rejected before
admission. Nested command sequences share the outer timeout and device lock.

A top-level `checkpoint` returns a `paused` receipt, its label and `remaining_yaml`.
The calling agent can observe the phone, handle the checkpoint, then deliberately
submit the remaining commands with a new request UUID. This supports variable UI
interruptions such as app verification screens. Detection and interpretation of
such screens belongs to the calling agent. The service does not retry a failed
or timed-out flow. An `unknown` receipt requires observing the device before any
new action because a prefix of the flow may have executed.

A definite missing-element probe returns `state=failed`, `error=element_not_found`,
zero-based `failed_command_index`, nested `failed_command_path`, and the original
`failed_command`. This does not undo previous steps. Other uncertain failures
retain `state=unknown`. Terminal failures include a best-effort `observation`
with at most 80 compact hierarchy nodes and `image_file.file_id`; fetch the PNG
with `android_download`. Capture runs under the same device lock, adds up to
eight seconds, and is saved once with the receipt. Polling never captures again.
Secure screens, disconnection or exhausted file quota produce capture error
fields without replacing the original failure. Service shutdown skips capture.

Use `seconds` for the session's idle interval. The legacy `lease_s` input remains
accepted by the gateway and host, but cannot be combined with `seconds`.
`device_id` is required for acquire, renew and release. If `backend_unavailable`
explicitly includes `operation_sent:false`, wait `retry_after_seconds` and retry
with the same request UUID. If dispatch may have happened, read the original
receipt; never mint another UUID as a recovery attempt.

Saved flows, operation receipts and checkpoint remainders persist in private
SQLite state. The worker stops its token-identified device server when the
service's pipe closes, and a later service stops a leftover server by its private
marker before the next operation. No model API is called by this runtime. Device interactions
can cause the apps themselves to contact their services.

## Observation and image coordinates

Screenshot capture and hierarchy inspection run concurrently with bounded waits.
With uiautomator2 configured, the hierarchy uses the persistent accessibility driver.
Nodes keep the existing ADB-style attribute names and password text is omitted.
Without it, the existing ADB hierarchy path remains available.

PNGs retain their source pixels. `device_size` gives the full frame dimensions,
`image.width` and `image.height` give delivered image dimensions, and `image_origin`
gives the top-left position within the full frame. Bounds and ADB coordinates use
device pixels. A client's smaller preview can have a different pixel scale.
Both engines accept percentage points for taps and swipes.

`crop={x,y,w,h}` returns only that region. It preserves pixel values and reports
its origin. Use a region whose longest side is within the client preview limit
to inspect small text. `image_mode=file` saves the exact PNG as an owner-scoped
file and returns `image_file.file_id`. `image_mode=both` also includes the native
MCP image. Fetch it through `android_download` with `length=4194304` to avoid
many small round trips, then delete it through `android_delete_file`.

Upgrading a host to an engine-enabled config must update the executable and
configuration together. Rollback must restore both originals because older
service versions reject the new optional keys.

## Verification

The service tests use a synthetic ADB executable and no phone or user accounts.

```sh
uv run --locked --group dev ruff check src tests
uv run --locked --group dev pytest -q
```

Gateway tests verify tool discovery, permission intersections, native image
delivery, isolated backend failure and recovery after a lost command response.


### Authoring and composition

Call `android_maestro` with `action=reference` for the versioned command and selector
reference. `action=validate` accepts inline `yaml` or a saved `name`, optional
`version`, and literal `parameters`. It validates the supported dialect and expands
saved references without taking control of the device.

Relative selectors, clipboard, location and display-mode commands are not part
of the uiautomator2 dialect; use `android_execute` for such device operations.
Saved flows compose through `runFlow` with a `name` and optional `version`.
They share the outer call's parameters. References resolve within the authenticated
principal's store before admission to device execution. Expansion shares the
200-command limit and bounded nesting. A checkpoint belongs at the top level.

```yaml
- runFlow:
    name: open_search
    version: <saved SHA256>
- inputText: ${QUERY}
- pressKey: Enter
```

The API exposes local device workflows. Host scripts, JavaScript expressions,
cloud execution and a browser viewer are outside this dialect.

### Runtime cleanup

The UIA2 worker sends every RPC once. Lost responses stop the owned worker and
its token-identified Android process before another operation is admitted.
Cleanup verifies the PID identity and leaves a private marker if the device is
unreachable, so later operations must complete cleanup first. It never kills a
generic `app_process` or adopts another server. Rollback to an older service must
finish this cleanup while the new service still understands those markers.
No model API or analytics service receives device content from the runtime.


### Application maps and scenario versions

`android_flows` also stores maps with `kind=map`. Both maps and structured
scenarios use immutable SQLite versions and separate draft/published heads.
`action=list` includes the draft version. `get` accepts `stage=draft` or a
historical `version`, including after deletion. `history` pages audit events.

A map contains `package`, named selector candidate lists, screen signatures,
optional `deep_links`, `popups`, `lists` and `meta`. A point candidate is `{point: "x,y"}`,
`[x, y]` or `{x, y}`; a list with `scope: parent` reads fields that are siblings of its item
node. A scenario contains `map`,
optional `map_version`, `params`, advisory `side_effects`, and `steps`.
The full schema and worked authoring sequence are returned by
`android_maestro(action=reference).library`.

```yaml
map: shop
side_effects: none
steps:
  - openApp: {cold: false}
  - tapElement: {selector: orders, expect_screen: orders}
```

Save with `action=draft` and `base_version` equal to the current draft version,
or published version when no draft exists. First creation uses null.
Run with `stage=draft`, the draft flow's `version` and a fresh request UUID.
Only terminal `succeeded` is publication evidence. Publish the map, then the flow,
using `action=publish`, the tested `version`, `test_request_id`, and the expected
published `base_version`. Changed floating dependencies require another test.
A normal run supplies the published `name` and a new request UUID. Each receipt
records its complete `library.pins`; a repeated request retrieves the same receipt.
`rollback` switches to a previously published revision with the current
published `base_version`.

Generic actions are `openApp`, `openDeepLink`, `tapElement`, `typeInto`,
`waitScreen`, `assertScreen`, `inspectMap`, `dismissPopups`, and `readList`.
Selectors are tried in order and an ambiguous candidate stops before an action.
Explicit `index` selects one match. Coordinate fallback requires a resulting
screen check. Screen signatures share one hierarchy snapshot per poll.
List fields are extracted locally from each snapshot, with password text masked.
Every visible row is retained. An optional list `key` names fields identifying
the same item across pages and enables deduplication during scrolling.
Checkpoint `remaining_yaml` retains explicit historical map versions and can be
submitted under a new request UUID after the checkpoint is resolved.
Application-specific navigation stays in scenario data. Existing command lists,
raw ADB, sessions, receipt polling and screenshot downloads retain their entry points.
