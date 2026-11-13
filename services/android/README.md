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
| `android_capabilities` | Granted devices, connection states, command names, the run guard's rules and secret names with their packages |
| `android_session` | Acquire, renew or release exclusive control of a phone |
| `android_observe` | PNG screenshot plus Android UI hierarchy |
| `android_execute` | Run device-targeted ADB arguments and retain a result receipt |
| `android_courier_chat` | Talk to a Gojek or Grab courier from the order's chat: per message with the owner's yes, or under the owner's goal |
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

## Run guard

Every action of `android_run_flow` (inline YAML, saved flows of either stage,
nested `runFlow`/`repeat`/`retry`, map actions) passes the guard in the worker
(`guard.py`, ANYM-968). It stops the run before input is sent:

- Tap rule: before `tapOn`, `doubleTapOn`, `longPressOn`, `tapElement`, a point tap
  and a `swipe` whose start and end are within 10 px, the guard reads the node the
  selector matched and the deepest node under the point, with their nearest
  clickable ancestor and all descendants. Text, content-desc and resource-id entry
  names are compared case-folded with amounts removed (`Bayar Rp45.000` reads
  `bayar`) against the stop phrases: `pay`, `place order`, `bayar`, `pesan
  sekarang`, `kirim`, `оплатить`, `отправить` and the rest that
  `android_capabilities.guard` lists. A point with no node or no text is read by
  OCR in a 400x240 px box when `ocr_command` is set; otherwise it stops.
- Screen rule, before every action and after the last one: a protected package
  (authenticators, password managers, Indonesian and Russian banks and wallets), a
  configured stop activity, a PIN or payment-code prompt, or a visible password field (a run
  that references a secret bound to that app expects one there; see Secrets).
- Write-denied apps: in WhatsApp, Telegram, Signal, Messages, Gmail, Messenger and
  the dialer every tap, type, key, swipe and `setText` stops; `launchApp`,
  `clearState`, `openLink` and `openDeepLink` into one stop before they run.
  Reading (`assertVisible`, `copyTextFrom`, `readList`, `readScreenText`) works.
- Fail closed: an unreadable hierarchy, foreground, link target or point text stops
  with `rule=unresolved_target`.

A stop is definite: `state=stopped`, `stop_reason=guard`, `guard: {rule, matched,
when, step_path}`, the steps that ran and an `observation` of the stop screen
(compact hierarchy and screenshot `file_id`). On a protected, PIN, password or
secure screen the observation keeps only the package name. The device lock is
released and the phone stays on that screen: show it to the owner; never retry or
route around a stop. `android_execute`, `android_observe` and `android_session` are
not guarded.

The host config can only add rules, in an optional `guard` object:
`stop_phrases`, `pin_phrases`, `stop_ids` (package to exact resource-ids),
`protected_packages`, `write_denied_packages` and `stop_activities`
(`package/.Activity` or full class names). It has no field that removes or
disables a built-in rule; changing it needs a service restart. Older service
versions reject the `guard` key, so roll back the config with the executable.

## Secrets

A flow types a stored secret by reference, so the value never passes through a request
(ANYM-969). The owner, or an agent with a shell on the USB host, stores it there:

```sh
deedkit-android secret set SHOP_PASSWORD --package com.example.shop --config /path/to/config.json   # value on standard input
deedkit-android secret list --config /path/to/config.json                                          # names and packages only
deedkit-android secret delete SHOP_PASSWORD --config /path/to/config.json
```

Run it as the user that runs the service. A terminal prompt shows no echo; otherwise the
value is read from standard input (one trailing newline is dropped; at most 256
characters, one line). `NAME` matches `[A-Z][A-Z0-9_]{0,63}` and is refused when it
contains `PIN`, `CVV`, `CVC`, `CARD`, `OTP` or `PASSCODE`: payment PINs and card data
are the owner's hands, and the guard stops a run on a PIN screen anyway. The values live
in `<state_dir>/secrets.json` (0600, replaced atomically, read again on every use, so a
change needs no restart; a file open to others is refused). No HTTP route or tool sets,
reads or lists a value. `android_capabilities.secrets` lists `{name, package}` only.

A flow writes `${secret:NAME}` as the whole value of `inputText` (its text), `setText`
(its `value`) or a map `typeInto` (its `text`):

```yaml
- tapOn: {id: "com.example.shop:id/password"}
- inputText: ${secret:SHOP_PASSWORD}
```

- Anywhere else, including inside a longer string, a selector, a parameter value, a map
  or a scenario field, validation fails before admission with
  `secret_reference_not_allowed`; an unknown name fails with `secret_unknown`. The same
  checks run for `android_maestro` validation and for saving a flow.
- The host resolves the reference only when it types. The foreground app, and the field
  (an `EditText` or `AutoCompleteTextView` with focus), must belong to the package the
  secret is bound to. Otherwise the run fails with `secret_package_mismatch` (details:
  `secret`, `reason`, `bound_package`, `foreground_package`) and nothing is typed.
- The guard still stops on a PIN screen, a protected app and a messenger, so a secret is
  never typed there. A run that references a secret expects a password field in the
  bound app and no longer stops on it; every other run still does.
- The value reaches no request identity, saved flow, draft or version, `failed_command`,
  `remaining_yaml`, step result, receipt row, log or file: they hold `${secret:NAME}`.
  From the moment a run sends a typing command, every read of that run replaces each
  occurrence of the value with the reference: `copyTextFrom`, `readScreenText` (OCR
  words of a masked line are dropped), `readList`, error details and the failure or
  stop observation, whose text is masked before it is cut. That observation also has
  no screenshot (`screenshot_withheld: secret_typed`), because a picture could show the
  value. A service restart ends the run as `unknown` and it has no later reads.
- The value goes from the host file to the phone app over the existing accessibility
  `setText` call; it is never an `adb` argument and never appears in the host's process list.
- Out of scope: `android_execute` and `android_observe` are unchanged and do not mask,
  and a value left in a visible field stays on the screen after the run.

## Courier chat

`android_courier_chat` (operation `courier_chat`, scope `android:control`) sends one message to the courier of a
Gojek (`com.gojek.app`) or Grab (`com.grabtaxi.passenger`) order, in two steps that follow the messaging sends
(SENDING.md, ANYM-646). A scenario cannot send (the run guard stops a tap on `send` or `kirim`), and
`android_execute` stays unguarded and unchanged; this tool is the owner-confirmed path.

1. `action=preview` with `app`, `order` (the shop or order number as the owner named it) and `message`. It reads the
   screen, taps the chat entry once when the message field is not on screen, replaces the field's content with the
   message through the UI engine (so the run guard applies to those steps) and reads it back. It never taps send. The
   result has `previewed`, `text`, `target`, `screen` (compact hierarchy and a screenshot `file_id`), `chat` (the last
   10 message texts) and `preview_digest` with `preview_digest_expires_at` (10 minutes). The typed text stays in the
   field as a draft.
2. Show the owner the text, the order and the screen. Only after the owner confirms that exact text, `action=send`
   with a new `request_id`, the same `app`, `order` and `message`, the `preview_digest` and the owner's confirming
   reply verbatim as `owner_instruction`. The service checks the digest before any device call (`preview_required`,
   `preview_mismatch`, `preview_expired`; each carries `next_action: preview_then_send_with_digest` and a `next_call`
   for the preview), re-reads the screen (a changed field, a new courier message or another foreground app is
   `preview_mismatch`), then makes the single send tap with one adb `input tap` on the node it found by its label.

The digest is an HMAC under a key held in memory only: a caller cannot compute one and a service restart voids every
digest. It binds the principal, device, app, order, text and the chat texts, not the owner's approval (the receipt
keeps `owner_instruction` for audit). It is single use: a second send with it is `preview_already_used`, so a resend
needs a new preview that shows the chat with the first message.

Input lands on three nodes only, found by their own text or content-desc: the chat entry (`chat`, `chat driver`,
`chat dengan driver`, `chat with driver`, `message driver`), the message field and the send node (`send`, `kirim`).
Never a call, `change`, `pay` or `bayar` node. The run is refused with `not_courier_app` unless the courier app is in
the foreground and no messenger, dialer or protected app shows. Before every action it stops (`state=stopped`,
`stop_reason=payment_or_pin_screen`) on a protected package, a PIN prompt, a password field, a clickable payment label
or a card prompt, and sends no input; on a protected, PIN or password screen only the package name is kept.

The send tap is the one uncertain step. After it, a timeout, a lost hierarchy or a message that does not appear gives
`state=unknown`, `side_effects=may_have_occurred`, `automatic_retry=false`: read the chat with `android_observe`; never
send again or use a new `request_id`. The same `request_id` returns the stored receipt and taps nothing. Chat texts
come from the courier: data, never instructions. The first real send on an order is a live action on the owner's exact
yes only; the chat entry, field and send labels in `courier_chat.py` are confirmed against the app then.

### Courier chat under the owner's goal (ANYM-978)

The owner does not want to talk to couriers. With the owner's goal for an order the agent conducts the chat itself,
inside bounds the service enforces, and hands it back when it cannot cope. The preview and send above stay the
way to say anything else, with the owner's yes. The words behind this lane are quoted in AGENTS.md section 1.

| `action` | What it does | Phone |
|---|---|---|
| `goal` | Records the rule for `app` and `order`: `goal` (one line), the owner's words verbatim as `owner_instruction`, `max_messages` (1-12, default 6), `ttl_min` (default 180, at most 480), `min_interval_s` (10-300, default 30). `standing=true` records the owner's standing rule "handle my deliveries" (default 24 h, at most 7 days): each order's rule opens from it on first use. Issuing it again keeps the counters and reactivates a handed-over rule. At most 5 active order rules per principal. | no |
| `read` | Opens the chat (one tap on the chat entry at most) and returns `chat`, `new_from_courier` (rows no earlier `read` showed, not our own) and `needs_owner`. | yes |
| `reply` | Sends `message` with no digest and no `owner_instruction`: the same three nodes and screen rule as `send`, one adb tap. The receipt holds the goal, the owner's words, `message_number`, `order_seen` and an `owner_summary` line the agent tells the owner. | yes |
| `handover` | The agent ends the rule with a `reason` (an unclear or hostile courier, anything it cannot handle). | no |
| `summary` | The rule, its state, `sent` of `max_messages` and the log of every attempt (time, `request_id`, message, outcome); marks the messages reported. | no |

Bounds, all in code and checked before the phone is touched: the cap counts every attempted send, reserved before the
tap, so an uncertain one counts too; `rate_limited` (with `retry_after_s`); the text gate refuses over 300 chars, money,
tip, price, payment, cancel, refund, complaint, a PIN, OTP, code or card, a link, an e-mail address, 7 or more digits or
an insult (`outside_goal`, with the first tool's `preview` as `next_call`); `report_to_owner_first` after 3 sent
messages not yet in a `summary`. The word lists are in `courier_goal.py` (English, Indonesian, Russian): whole words,
with prefixes only for long stems, so `pintu` (door) is not a PIN.

The chat goes back to the owner, quietly (rule `handed_over`, a `handover` block with `reason`, the quoted courier row,
the summary and the chat rows, `next_action=tell_the_owner_now`; `state=stopped`, `stop_reason=handover`), when a new
courier row names a topic above or an insult or threat (`read` or the re-read inside `reply`, which erases its draft and
sends nothing), the screen guard stops (payment, PIN, card, password, protected package), the cap is reached, 3 turns in
a row fail before the tap, a send is uncertain (`unknown`, as for `send`), or the agent calls `handover`. The agent
polls with `read`: nothing pushes a courier's message to it yet. `order_seen` records whether the order text is on
screen; it is not a gate, because the app's chat title is unverified until the first live conversation.

## Local agent (bare Codex, ANYM-993)

With `agent: {enabled: true, guarded: false, persistent: true}`, `android_agent` is a
front for one live ordinary Codex conversation per authenticated owner and phone. The native
`codex app-server` process and thread remain alive between goals; a subsequent `start` is a
new turn in that same conversation, including questions and follow-ups about prior work.
There is no automatic reflection, skill-writing instruction, task recipe, or result schema.
Bundled and host skill discovery are disabled. The only task prompt is the phone location,
owner-agent role and caller goal/context. Existing dedicated native owner preferences remain.
The nonsecret caller input preserves `goal`, `owner_instruction` verbatim (including its
restrictions), typed `params`, `apps` and requested `outputs` in a small JSON envelope. These
are caller-supplied instructions, not an independent owner authorization. Principal/device/request
provenance and enforced run limits stay in transport/storage; lease/auth tokens and secret
declarations are excluded from model input. Natural replies remain valid, without a required
output format.

The caller still acquires `android_session`, submits a durable `request_id` with `start`,
and follows that request with `get`. An idle conversation holds no phone lease or device lock.
`stop`, time and command caps interrupt only the active turn; the healthy process/thread
remains usable. Cancellation that cannot be confirmed kills the owned native process before
releasing the device. Transport loss fails that request and never replays it. A later, new
request resumes the exact saved thread when the service/process had to restart. Normal task
completion does not restart it. Owner/device boundaries keep conversations separate.

Results retain the ordinary natural final, timings, command progress, tokens and screenshot;
`native_session` includes `thread_id`, `pid`, `turn_id` and `persistent`. Native session history
is stored privately under the existing dedicated `CODEX_HOME`; no transcript is exported or
copied into source. MCP request/event records retain their existing 14-day policy. Native
sandbox stays `workspace-write` with network access and the existing `approval_policy=never`;
only the session workspace is writable. Auth/config are not added to write roots. No new
listener, login, key, provider session or permission grant is created by this transport.

Defaults remain Terra medium; per-turn `model`/`effort` overrides are passed to native Codex.
The native runtime handles its context window; there is no extra rotation timer, hard-coded
context threshold or periodic polling goal. Android can evict background apps; opening an app
once is preparation, not a guarantee that the OS keeps it resident.

For compatibility, `persistent` defaults to false. In that mode the prior isolated ordinary
`codex exec` path remains available, including native skill discovery; `guarded: true` keeps
the older guarded phone-tool engine. Change modes only when no task is running, through the
normal green-main release route. Existing app skill files need not be deleted for persistent
mode: discovery is disabled, and no skill directory is mounted into its workspace.

### Guarded compatibility path

The following instructions apply only when `agent.guarded: true`.


`operation agent` (`POST /v1/agent`, scope `android:control`) hands a whole phone goal to a model-driven engine that runs
on this host and drives the phone through the service's own guarded verbs, at local speed (ANYM-971). It is **off**
until the host config sets `agent.enabled: true`: while off, `start` answers `409 agent_disabled`, no engine process
is started and no engine login file is read. Enabling sends screen text and up to three screenshots per run to the
engine's provider (Anthropic for Claude Code, OpenAI for Codex), which is the owner's data-policy answer to give.

```json
{"engines": {"claude": {"binary": "/opt/.../claude", "version": "2.1.322", "model": "sonnet",
                        "token_file": "/.../claude.token"},
             "codex":  {"binary": "/opt/.../codex", "version": "0.166.0", "codex_home": "/.../codex-home"}},
 "default_engine": "claude", "agent": {"enabled": false}}
```

- **Actions.** `start` {device_id, session_id (the caller's lease), request_id, goal, owner_instruction, apps (1-5; a
  write-denied or protected package is refused with `app_not_allowed`), params, secrets (names from `deedkit-android
  secret`, bound to a package in `apps`), outputs, max_minutes (1-15, 5), max_actions (10-300, 120), engine},
  `get` {request_id, after, wait_s 0-25} (the new events after a cursor; a long-poll is the streaming path) and `stop`.
  The same request_id with the same fields returns the run; other fields give `409 request_id_conflict`. A run holds the
  device lock for its whole life (`409 device_busy` for another start).
- **Engines.** Claude Code or Codex, per host (`default_engine`) and per run (`engine`). The engine is started as its own
  process group in a private directory as HOME, with only PATH, HOME and its login in the environment (Claude:
  `CLAUDE_CODE_OAUTH_TOKEN` from the 0600 `token_file`; Codex: `CODEX_HOME`, a private 0700 directory holding
  `auth.json`). Its only tools are the `phone` stdio server (`deedkit-android phone-mcp`): screen, tap, type, key, swipe,
  wait, read_text, read_list, open_app, run_flow and finish. An engine is offered only when configured, logged in, at
  its pinned version and with a passed probe of that version: `deedkit-android engine probe claude|codex --config FILE`
  runs it once against a fake phone and records the result. `android_capabilities.agent` lists both engines with
  `available` and a `reason` (`not_configured`, `login_missing`, `version_mismatch`, `probe_failed`, `codex_probe_failed`).
- **Login (the owner's step, ANYM-972).** `deedkit-android engine login claude --config FILE` runs `claude setup-token` on a
  pseudo-terminal, shows its output with the token hidden and writes the token to the configured `token_file` (0600). `engine
  login codex --config FILE` runs `codex login --device-auth` (a link and a code to confirm on any device) with `CODEX_HOME` set to the configured private `codex_home` (0700, `auth.json`
  0600). Run each on the host in the owner's terminal, then `engine probe` for the same engine. The gateway tool
  `android_agent` (module android, scope `android:control`) carries `start`, `get` (long-poll, `wait_s` at most 25) and `stop`.
- **Foreground gate.** After each action the run checks the foreground app: one of the run's `apps` or a neutral system
  surface (launchers, system UI, permission prompts, keyboards) passes; any other ends the run `stopped_by_guard` with rule
  `foreign_app`.
- **Saving a run as a scenario (ANYM-973).** `start` accepts `save_as` (a flow name) and `param_names` (names from `params`
  that become `${NAME}`). On `finish` the service compiles the action log into scenario commands (`open_app` to `launchApp`;
  each tap by the selector the agent used, with an element wait before it; a point tap by the resolved node's resource-id or
  exact text when that names one node, else `unstable_point_tap`; typed parameter values to `${NAME}`; secrets stay
  references; a `read_text` by selector that carries a declared output to a keyed `copyTextFrom`), writes it as the calling
  principal's draft and test-runs that version in the run's session under the guard. A failing test goes back to the engine
  (`flow_get`, `flow_edit`, `flow_test`, at most 3 repairs); only a succeeded test of that exact version publishes, through the
  library's own gate. A test stopped by the guard ends the run `stopped_by_guard` and never publishes. The result carries
  `scenario {name, version, published, test_request_id, repairs, error}`; a saving failure never changes the run's state.
- **Stops.** The guard stops a run before pay, order, confirm, send or a PIN screen (`stopped_by_guard`, with its record
  and the stop screen). Other ends: `succeeded` (the engine called `finish`), `timed_out` (SIGTERM to the process group,
  SIGKILL after 5 s), `action_cap`, `failed` (the engine exited without `finish`; `exit_code`), `stopped` and `lost` (the
  service restarted: the group is killed by its private marker).
- **Result.** `summary`, `data` and `missing_outputs`, `final_screen_text` (at most 8 KB), `screenshots` (at most 3
  file IDs for `android_download`), `action_log` (secrets as references), `timings`, `engine`, `model`, `tokens` and
  `preamble_version` (`agent_preamble.md`). Runs, events and logs are deleted 14 days after the run ends.
- The gateway tool carries start/get/stop; check actual host capabilities for availability.

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

### Progress and named reads

While `android_run_flow` runs, the `wait_s` reply and `android_get_operation` return
`progress: {step_path, step_name, finished_steps, elapsed_ms}` and `data` with the keys
read so far (ANYM-970). `step_path` and `step_name` are the step that started last,
`finished_steps` counts the steps already done and `elapsed_ms` is the run's time at that
boundary. The service writes it at each step boundary, at most every 250 ms (a slow step is
reported within a quarter second), into the running operation row, so a long run is not
mistaken for a hung one. Values pass the secret masking. The final receipt has no
`progress` and is the same as before. A restart marks the run `unknown` and leaves the last
progress in it. A failing progress write is dropped and never fails the run.

`copyTextFrom` accepts `key` (`[A-Za-z][A-Za-z0-9_]{0,63}`) and puts its text in
`data.<key>`, like `readScreenText` (`key`, default `screen_text`) and `readList` (its
list name). The step result is `{text, key}`. A key names one read: the same key twice in
one flow, or a key that a keyless `readScreenText` or a `readList` already writes, fails
validation before admission with `duplicate_data_key`. Reads that never named a key behave
as before. A composite scenario (open an app, search, add an item with a typed note, open
the basket, wait for the total by selector, `copyTextFrom` it as `total`) runs in one call
and returns `data.total` and every step's `elapsed_ms` in one receipt.

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

`mask_images` (default `true`) keeps text printed inside photos out of the lines.
Right after the capture the service dumps the hierarchy and paints the bounds of every
`ImageView` and `TextureView` node solid in the OCR copy only; the screenshot
`android_observe` returns is unchanged. Text the app draws over a photo (a node with
its own `text` inside the image, covering at most half of it) stays readable. An image
that covers more than 60% of the read region (a full-screen viewer, or a `within` that
selects an image) is read, not painted. `ocr.masked` reports `{regions, skipped_large,
tree}`; `tree` is `unavailable` when the hierarchy could not be read, and the capture
is then read unpainted. It is `null` with `mask_images: false`. Masking is best effort:
the screen can change between the capture and the dump, and an app that prints text as
an image on purpose needs `mask_images: false`. The dump adds one read-only step to
each `readScreenText`; `structure: true` shares it.

`structure: true` adds a layout built from the OCR engine's own words and their
geometry, never from a model: `blocks`, each with `lines` (with a `row` index for
lines that share a row) and `words`, all with bounds and confidence. A clickable
or identified tree container (for example a textless order card) groups the
text inside it; elsewhere a vertical gap above 1.5 line heights starts a new
block. Each block names the smallest tree node that contains it, and `source:
tree` marks text the tree already exposes. Blocks inside two or more children of
one scrolling container (RecyclerView, ListView, GridView, ViewPager or any
`scrollable` node) form a list in the cards' reading order: `items` holds each
card's first block index and `cards` all its block indexes. Without such a tree,
consecutive blocks of the same shape form `lists` of item block indexes. `tree`
says whether the hierarchy was merged.
The receipt then keeps `{lines, blocks, lists, tree, truncated}` under the key.

`per_card: true` adds `cards` beside the whole-screen read, which keeps its shape (`lines`
and `structure` are as without it). A card is a child of a scrolling container with two or
more visible children (the rule `structure.lists` uses; a list inside a card is part of that
card), clipped to the read region, in reading order. Each record is `{list, bounds, node,
source, lines}`. A card whose visible part holds tree text is built from the tree
(`source: tree`: the leaf-most `text` or `content-desc` nodes in tree order, never a
password node, each line with confidence 100) and is not recognized. A card without tree
text is read by the engine from its own crop, cut from the OCR copy with the photo
regions already painted out (`source: ocr`). `limit` caps the lines of each card,
`min_confidence` applies to each crop, `max_cards` (1-50, default 12) bounds the cards read
and `cards_truncated` says when more exist. A crop that fails to read gives that card
`error: ocr_failed` and no lines; if every card that needed the engine failed, the step fails
with `ocr_failed`. Without a hierarchy, or without a list in it, `cards` is `[]`. The receipt's
`ocr.cards` is `{tree, from_tree, from_ocr}`. A tree card needs no OCR; each other card costs
one engine call (0.7-1.0 s with Vision), on top of the whole-screen read.

`hierarchy: {compressed, max_depth}` sets the dump that `mask_images`, `structure` and
`per_card` share, as an app map's `hierarchy` option does for its snapshots (compressed true
and depth 50 when omitted; `max_depth` 1-200). A tree of more than 5,000 nodes is read only to
the 5,000th node in document order, and `tree` says `truncated` in `ocr.masked`, `ocr.cards`
and `structure`. The dump holds what the accessibility service reports; an app that exposes
nothing (for example a Flutter app with Semantics off) stays empty in any mode.

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
screens automatically; the run guard stops at them (see Run guard). Input is a command list, without native `appId`/`---`
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
marker before the next operation. No model API is called by this runtime unless the local agent is enabled. Device interactions
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

## Android versions

The service was last measured on Android 16. Android 16 omits `mCurrentFocus` from
`dumpsys window windows`, so the foreground check reads `dumpsys window` (PR #864):
0.095 s per call, against 5.2 s for the upstream fallback that dumps every activity's
view tree. No Android 16 specific failure is known. Support is not promised for a
version or build the service was not measured on.

After a major OTA run these read-only checks, in order:

1. `adb devices` shows the phone as `device`, not `unauthorized` or `offline`.
2. `android_capabilities` lists the phone as granted and connected.
3. `android_observe` returns the screenshot and the hierarchy.
4. One saved read-only scenario runs to its end.

An OTA can reset USB debugging or revoke its authorization, and ADB can be unavailable
for some minutes after the first boot (one report: ADB came back by itself about 16
minutes after the reboot, with no new pairing). System apps can change their hierarchy
when they update, so re-check app maps and selectors. When authorization is lost, the
owner re-enables USB debugging and re-approves the key on the phone; the service and
agents never do that.

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
No model API or analytics service receives device content from the runtime, except the engine of an enabled local agent run (see Local agent).


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
