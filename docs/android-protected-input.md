# Android protected input (ANYM-998; native integration ANYM-993)

## Source and binding contract

The helpers provide durable metadata receipts and a guarded one-shot password
EditText transport. Automatic PIN acceptance remains OPEN. No actual
`protected_window` exists yet; without it the transport refuses before reference
access/channel use. Native `secrets_not_supported`, legacy PIN refusal and existing
grants remain.

`ProtectedBinding` has two distinct frozen types:

- `InputBinding`: released legacy owner/device/thread/turn, original typed RPC ID,
  genuine blocking requestUserInput item/question IDs, package/target/purpose.
- `OwnedToolInputBinding`: authenticated owner/device, `task_id` (original external
  task request ID), thread/turn, original `native_request_id: str | int` (not bool),
  actual `native_call_id`, optional exact `native_namespace`, package/target/purpose.
  Only `tool_name="android_order_control"` and
  `operation="request_protected_input"` are accepted. There is no question/item ID.

Pinned0.166 DynamicToolCallParams supplies callId/threadId/turnId/tool/arguments and
optional namespace, with no independent itemId. A registers and dispatches only
this owned operation, validates the actual namespace and injects authenticated
identity/stable intent operation ID from trusted server state. Arguments carry
only target/purpose/opaque reference metadata; never a value or basket approval.
Default async clarification remains its natural message/array path: no fabricated
blocking RPC/question ID or forced Plan mode.

`ProtectedInputRequest(request_id, operation_id, binding, expires_at, transport)`
keeps external request/operation IDs distinct from native IDs. New tool bindings
serialize `binding_kind="owned_tool_call", binding_version=2`; untagged released
rows decode only as legacy bindings. Unknown/incomplete tags refuse. The SQLite
schema, original legacy identity keys, pending/unknown records and tombstones are
retained without rewriting/coercion. Operation and held-device exclusion span kinds.

`ProtectedInputResult.metadata()` preserves status/original IDs and `resume_once`.
Legacy metadata remains unchanged; tool metadata includes task/call/namespace/kind/
version and omits question/item fields. Completion goes only to the original typed
native tool call. The resume hint is not login/payment success or replay authority.
Never put a secret into native answers or ordinary webhooks/events.

## Trusted window and one-shot operation

```python
await enter_password_once(journal, request, reference,
                          private_directory=private_directory,
                          protected_window=owned_native_guard)
```

A owns `protected_window(ProtectedBinding) -> AsyncContextManager[ProtectedWindow]`.
Before yielding it proves the exact pending blocking registered tool RPC, or genuine
blocking requestUserInput with no auto-resolution; authenticated owner/device/task/
thread/turn, lease, zero active/background commands and service-wide read/capture/
screenshot/diagnostic/new-turn/steer/reply fence. A supplies current binding,
existing connected `ExistingInputChannel`, trusted complete-basket provenance,
`hold()`, `allow_resume()`, async `recheck()` and `sensitive_field_gone()`.
Reference-directory isolation from native shell access is part of the dependency.
IDs/targets come from native state, never field text/XML/screenshots/model aliases.

Manual `ManualProtectedInput.request/complete/revoke` needs the same actual fence.
Its capture_suspended/safe_to_resume/basket_approved flags are internal observed
conditions, never HTTP/tool/caller assertions. Owner DONE is an owner report,
not proof of success. Manual entry is interim and does not complete automatic input.

The private0700 directory/0600 SQLite journal stores IDs/binding/expiry/status only,
not values or a vault. Keep it outside artifacts/backups absent existing policy.
Commit uncertainty before value read/write. Same operation/original request cannot
retry under new IDs, including restart/concurrency. Rejected, failed, cancelled,
expired, timed-out, changed-target and unknown outcomes retain the hold. A reconstructs
the fence after restart and reconciles/revokes before lock release; revocation keeps
tombstones. DONE commits before the one resume hint; a lost hint needs reconciliation.
Only trusted sensitive-field-gone permits capture/model continuation.

Automatic LOCAL_REFERENCE uses read-only owner-supplied references, two focus checks
and final reference validity recheck before one raw HTTP setText body. Supported
fields are enabled/focused password android.widget.EditText with exact package/
resourceID/opaque target; values are4–12 ASCII digits. Existing socket only: no SDK
retry/debug/fallback, ADB/argv/shell/clipboard, IME install/server startup or competing
UiAutomation client. Lost reply/timeout/cancel/server error never retries. Payment
needs the owner's existing confirmed complete basket; no model boolean or new
per-Pay-button gate. Phone unlock/custom keypads remain unsupported. A stored PIN
cannot restore USB after reboot; no lock/update/security setting changes.

## Native protected window: pinned 0.166 evidence (DK-8)

`codex_session.queue_control` keeps refusing `request_protected_input` with
`protected_input_unavailable`. It refuses before any host callback, reference read or channel
use, and answers once under the original typed RPC ID. Grounded in official
[rust-v0.166.0](https://github.com/openai/codex/tree/rust-v0.166.0/codex-rs) source:

- Holding the call does not stop execution. `DynamicToolHandler::supports_parallel_tool_calls`
  returns `true` (`core/src/tools/handlers/dynamic.rs:88`), and so does `exec_command`
  (`core/src/tools/handlers/unified_exec/exec_command.rs`). Both take the shared read lock in
  `core/src/tools/parallel.rs:286-296`, so sibling commands in the same model response run
  while the host holds the protected RPC.
- The original call cannot outlive cancellation. `finishes_on_cancellation` is `true`
  (`dynamic.rs:177`), and the pending response is dropped through `or_cancel` (`dynamic.rs:240`).
  After a turn transition, `app-server/src/dynamic_tools.rs:26` discards the client response. So
  `turn/interrupt` or a stopped foreground turn cannot act as a fence that keeps the exact call.
- The background-terminal APIs are not a barrier. `thread/backgroundTerminals/clean` only submits
  `Op::CleanBackgroundTerminals`, with no termination acknowledgement. That op drains the
  process store; a command that was admitted but not yet stored is stored afterwards.
  `list` reports `os_pid: None`, and the store holds only spawned non-exited processes. Neither
  covers setsid/double-forked descendants or work on the device.
- Commands get direct ADB through `PATH`/`ANDROID_SERIAL`, with network access
  (`codex_session.connect`). Device-side shell processes and queued requests on the shared
  adb server are outside every host process scope.

`tests/test_native_protected_window.py` covers this on the real `CodexSession` stdio path. It
uses a live sibling command, zero tracked commands and a completed item with a possibly detached
child. In each case the call is refused while the ordinary sibling call is still dispatched
exactly once. After an interrupt and a restart the call is not answered again, no row can resume
it, and the original ID keeps its type. A mutant that treats the held call as a fence fails all
four tests.

**Invariant requirements (none is met, authorized or configured here).** The refusal can only
be removed when one window gives all of the following:

1. Enrolled host scope: every native process and descendant, including setsid/double-forked ones,
   stays in one scope that was set up before spawn and that it cannot leave.
2. Atomic barrier: the host stops execution throughout that scope and gets an acknowledgement,
   without cancelling app-server's pending call. The barrier is released only after a trusted
   `sensitive_field_gone` result and one original response. Unknown/cancel/restart keeps the
   barrier, or ends the scope without a reply, and keeps the tombstones.
3. Device containment: after the barrier, the device-side observer set must be proven equal to
   the host-owned baseline. That set covers shell-uid processes, in-flight adb services,
   accessibility/notification listeners and media projection. The proof runs on the existing host
   adb server, which stays outside the barrier because it carries the `setText` channel. No
   current primitive provides this proof atomically. This is the fundamental gap.
4. Service fence: other service capture paths (`BareRun.final` screenshot/XML, owner reads) stay
   refused while the window is held.

**No OS mechanism for items 1–2 exists on the current deployment.** The USB/native executor runs
on macOS (mini, launchd). Linux cgroup v2 is hypothetical research only: a delegated leaf created
before spawn, `cgroup.freeze` with the `frozen 1` acknowledgement in `cgroup.events`, and
`cgroup.kill`. It is unavailable on mini. It is not a recipe root can provision there, and
nothing here proposes a Linux migration or a new executor. Even if items 1–2 were met, item 3
would still block.

Acceptance is root-owned, on an installed no-model sentinel. The sentinel attempts setsid,
double-fork, inherited-descriptor and adb-shell `nohup` bypasses during acquisition, and all of
them must be contained. A synthetic focused-field canary then shows that only the original call
continues. Undo: release the barrier only after reconciliation, keep tombstones and keep the
refusal.

## Storage, platform evidence and remaining acceptance

Official [Grok Vault](https://docs.x.ai/grok-bot/vault) describes model-hidden page
filling and secure API-key cards; [Remote MCP](https://docs.x.ai/developers/tools/remote-mcp)
documents auth/headers/discovery. Neither inspected contract provides a protected
reference-to-DeedKit/USB-host channel. Caller environment is not host environment;
SECRET_REFERENCE remains unavailable. This is a documented capability gap, not proof
Grok can never support it. A usable extension needs an opaque expiring/revocable
owner/device/intent/target-bound reference, accepted/lost-reply semantics and direct
host value delivery outside prompts/traces/events. No vault/API-key lookup is included.

[uiautomator2 3.7.1](https://github.com/openatx/uiautomator2/blob/3.7.1/uiautomator2/core.py)
supports setText, but generic client logging/exceptions/retries and send_keys IME
fallback are unsafe here. The dedicated source avoids these paths. Local0600 files
are not hidden from a same-UID native shell: actual executor isolation and owner
reference placement remain required, without a duplicated vault/encryption promise.
Android [InputConnection.commitText](https://developer.android.com/reference/android/view/inputmethod/InputConnection)
through an [IME](https://developer.android.com/develop/ui/views/touch-and-input/creating-input-method)
would add installation/input authority; notification access grants none. Root must
allocate any concrete permission expansion.

Synthetic tests cover both binding kinds, v1 upgrade/restart/tombstones, mismatches,
concurrency, raw socket timeout/lost reply, target/reference changes and value-free
results/diagnostics/artifacts. They do not prove the actual native fence, applied
executor isolation/profile, provisioned Grok/local reference, installed compatible
server/target or automatic PIN acceptance. All remain OPEN for root's allocated
integration/live proof; no real secret/phone/model run occurs in fixtures. Undo is
disconnecting/reverting integration while preserving unresolved tombstones; no
credential/access/device setting was changed.
