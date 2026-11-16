# Android protected-input canary (ANYM-998)

A root-only synthetic canary for the existing protected password-EditText path. It
measures what the ordinary readers show while the disposable fixture's password
field holds runner-generated synthetic digits. It is not a fence and a PASS does
not unblock real automatic PIN entry: the global native/OS read fence and a
native `protected_window` remain unavailable
([enforceability plan](android-protected-input.md#trusted-window-and-one-shot-operation)).

## What it exercises

Source modules are used unchanged and read-only:

1. `OwnedToolInputBinding` (`android_order_control` / `request_protected_input`)
   with canary IDs: task `canary-RUN`, call `canary-RUN-call-positive|lost-reply`,
   request IDs 1 and 2, purpose `login`, target `fixture-protected-input`.
2. `ManualProtectedInput` journal in `STATE/journal`: durable `unknown` before the
   reference is read; one-shot; failed/unknown outcomes stay held.
3. `enter_password_once` with a `LocalInputReference` the runner writes (0600, in
   0700 `STATE/refs`, removed after each scenario).
4. `ExistingInputChannel.set_password_once`: one raw `setText` on the existing
   uiautomator2 server, never ADB argv, shell, clipboard or IME.

`CanaryWindow` is fixture-only (`app.deedkit.protectedfixture`, login purpose).
It reads the target from `dumpWindowHierarchy`, measures readers in
`sensitive_field_gone`, then presses HOME (the fixture clears its field on pause).
The native route still refuses: `codex_session.queue_control` returns
`protected_input_unavailable` and the canary does not go through native Codex.

Scenarios: `positive` (direct socket, expects `owner_done` + `resume_once`), then
`lost-reply` (a relay forwards the request, lets the server finish, drops the
reply: expects `unknown`, journal `unknown`, one forwarded write, no retry).

Readers × checkpoints (`after_write`, `after_receipt`, `lost_reply_hold`):
`ui_xml` (raw `dumpWindowHierarchy(true, 50)`, what the agent receives),
`agent_compact` (`agent_screen.compact` of it), `screenshot_ocr` (`screencap -p`
through Tesseract), `logcat` (since the run's device epoch). Each row has
`absent|present|unavailable`, byte count and SHA-256; never the text or value.
`ui_xml` also records whether the field is present, masked and of the value's length.

## Plan (default, offline)

```sh
cd services/android
uv run python ../../scripts/android_protected_input_canary.py
```

Prints JSON: interface, exact invocation, device commands, undo, typed
prerequisites and acceptance. No process, socket, device, model or credential.

## Root execution (not run by workers)

Prerequisites, all root-supplied: an exclusive device allocation with no native,
agent or other UiAutomation client during the run; the existing uiautomator2
server running; a forwarded host port; the fixture APK matching the pinned
SHA-256 `d2970eaa…97ab` (thread-storage `thr_v7cjp524rq/protected-edittext-fixture`)
with the package absent from the device; an absolute Tesseract path.

```sh
adb -s SERIAL forward tcp:PORT tcp:9008
cd services/android
uv run python ../../scripts/android_protected_input_canary.py execute --serial SERIAL --u2-port PORT --tesseract /ABSOLUTE/tesseract --apk APK --state-dir NEW_EMPTY_DIR --run-id RUN_ID --report REPORT.json
```

Device commands, in order: `get-state`, `shell pm path PKG` (must be absent),
`shell date +%s`, one hierarchy read, `install APK`, `shell am start -W -n
PKG/.FixtureActivity`, readers (`exec-out screencap -p`, `logcat -d -v epoch -T
EPOCH.000`), `shell input keyevent KEYCODE_HOME`, and the same for `lost-reply`.

Undo (always run by the runner after an install attempt): `shell am force-stop
PKG`, `uninstall PKG`. Root then removes the forward (`adb -s SERIAL forward
--remove tcp:PORT`) and, after recording the report, the synthetic state dir. The
lost-reply journal row stays `unknown` by design; a reused state dir is refused.

## Verdicts

- `BLOCKED`: offline/preflight refusal before install (`invalid_options`,
  `state_dir_reused`, `fixture_apk_mismatch`, `ocr_reader_unavailable`,
  `device_unavailable`, `fixture_already_installed`, `u2_rpc_failed`).
- `LEAK`: any reader shows the synthetic digits (exact or separator-split).
- `PASS`: positive `owner_done` with one write; lost-reply held with one forwarded
  write; every reader `absent` at every checkpoint; masked field of the value's
  length at write and hold; fixture gone after each scenario; uninstall exit 0.
- `INCONCLUSIVE`: anything else (rejected, timeout, target change, reader
  unavailable, device command failure). Rejected/unknown positive skips lost-reply.

## Does not prove

The global native/descendant/device read fence, reference isolation from a
same-UID native shell, Grab/custom keypad/phone-unlock support or real PIN
delivery. Partial digit leaks (for example a briefly shown last character) are not
detected. Synthetic tests (`services/android/tests/test_protected_input_canary.py`)
cover the plan, both scenarios, reader leaks, rejected/error/timeout/lost replies,
target and source changes, reused state and mid-run device failure with fakes only.
