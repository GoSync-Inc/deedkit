# Android protected input (ANYM-998; native integration ANYM-993)

## Available source slice

`deedkit_android.protected_input` provides **correlated one-shot receipts** and manual
owner completion. `protected_input_transport.enter_password_once` implements a
dedicated source transport; it is unavailable without A's real `protected_window`.
Automatic PIN acceptance remains open. Existing native
`secrets_not_supported` and legacy PIN refusal stay in place. No legacy tool or grant
is restored.

The native integration owns authentication, the device lease and exact active
question. Under that lock it constructs:

```python
InputBinding(owner, device_id, native_thread_id, native_turn_id, question_id,
             package, target_id, purpose, native_request_id, native_item_id)
ProtectedInputRequest(request_id, operation_id, binding, expires_at,
                      transport=InputTransport.MANUAL)
broker = ManualProtectedInput(private_state_directory)
broker.request(request, current=binding, capture_suspended=True)
broker.complete(request_id, current=binding, outcome=Completion.DONE,
                capture_suspended=True, safe_to_resume=True)
broker.revoke(request_id, current=binding)
```

These booleans are internal **observed preconditions**, never HTTP/tool arguments or
assertions from a caller. Before offering entry, A must quiesce native screen reads,
commands, screenshots and diagnostics, including in-flight work. If that cannot be
proved, do not offer entry. IDs/target identity come from authenticated native state,
never field content, XML, screenshots or a model-created replacement. Preserve the
native JSON-RPC ID's string/number type and item/question IDs; they are distinct from
external request and operation IDs. Ordinary clarification has its own path.

The owner enters directly on the phone and finishes the sensitive screen before
reporting DONE. A verifies the protected window is safe to leave before resuming.
Receipt status `owner_done` is an owner report, not proof of login/payment success.
Only its first delivery has `resume_once=True`, for that original native question.
Never put a secret into `requestUserInput` answers. Payment requires the existing
confirmed complete-basket provenance; access to its PIN grants no purchase. Login,
payment and phone unlock have separate purposes. No new per-button approval is added.

The journal stores only IDs, binding, expiry and outcome in a 0600 SQLite file under
a 0700 directory. It is not a vault. Keep it outside artifacts/backups unless an
existing policy covers that metadata. It rejects concurrent handoffs for a held
device, unrelated owners/devices/threads/turns/RPC items/questions and changed
targets. Same operation or original native question cannot be retried under new IDs.
Operation IDs must remain stable across the original protected intent.

Rejected, failed, cancelled, expired and uncertain outcomes never resume. They keep
the device held until the native owner reconciles and revokes before releasing the
lock/changing the question. Revocation preserves the operation/question tombstone.
After a process crash, an awaiting receipt remains awaiting until reconciled; it
does not replay input. DONE commits before its resume hint: a crash after commit
requires native-state reconciliation, not redispatch. No raw exception is exported.
If USB disappears after reboot, a stored PIN cannot restore that missing channel.
No lock, update or security setting is changed.

## Verified transport boundary — 2026-11-13

Official [Grok Vault documentation](https://docs.x.ai/grok-bot/vault) describes
model-hidden web-page filling from Vault/Secure Forms and secure API-key cards.
[Remote MCP](https://docs.x.ai/developers/tools/remote-mcp) documents server auth,
headers and tool discovery. Neither inspected interface specifies a protected
credential-reference-to-DeedKit/USB-host transport. This is a missing documented
contract, not proof Grok can never support it. Caller environment is not host
environment. `SECRET_REFERENCE` therefore returns `reference_transport_unavailable`
without a resolver or a value/reference payload. Invalid, expired and revoked
**handoff receipt IDs** cannot resume; credential-reference validity is not claimed.

Pinned [uiautomator2 3.7.1 source](https://github.com/openatx/uiautomator2/blob/3.7.1/uiautomator2/core.py)
supports `setText` through JSON-RPC, but its generic client logs request data and
includes parameters in exceptions; `jsonrpc_call` restarts/retries after transport
errors. `send_keys` can fall back to installing an IME. None is a safe protected
one-shot primitive as currently invoked.

## Automatic source contract; not enabled

`enter_password_once(journal, request, LocalInputReference(...), private_directory=...,
protected_window=...)` requires `LOCAL_REFERENCE`. The trusted window supplies the
exact binding, existing connected `ExistingInputChannel` socket, basket provenance,
`hold()`, `recheck()`, `sensitive_field_gone()` and `allow_resume()`. Without it, no
reference is read. A hold survives failure/cancellation/restart; only verified
safe completion releases it. Consumption commits before reading/sending. Two focus
checks and a final reference recheck precede one raw HTTP `setText` body. Supported
targets are enabled, focused `android.widget.EditText`, password=true, exact package/
resource ID/target identity; values are 4–12 ASCII digits. Timeout, lost reply or
server error holds UNKNOWN without retry; accepted text alone is not login success.

1. **Preferred existing Grok storage:** a documented client extension must exchange
   an opaque, expiring, revocable owner/device/operation/target-bound capability for
   a value directly to a private host executor, bypassing tool arguments, model
   context, ordinary webhooks, traces and screenshots. Its issuer/audience, auth,
   revocation and accepted/lost-reply semantics need a real contract. No vault key
   lookup or new grant is included here.
2. **Small host alternative:** an owner-supplied existing 0600 local file reference,
   confined to a private directory, read only during the protected operation; no
   duplicate vault or encryption claim. **0600 does not hide a file from a
   native shell with the same UID**: deployment needs verified executor isolation
   from native reads (or Grok's direct channel). Actual compatible existing-server
   availability, isolation and native quiescence remain unverified. No new account,
   permission, service or generic shell is authorized by this source.
3. **If that field cannot accept text:** Android supports
   [InputConnection.commitText](https://developer.android.com/reference/android/view/inputmethod/InputConnection)
   through an [IME](https://developer.android.com/develop/ui/views/touch-and-input/creating-input-method).
   A companion would need explicit installation/input-method selection and a new
   device/data boundary; notification access grants no input authority. Custom PIN
   keypads and phone unlock are not demonstrated. Root must allocate this expansion.

Owner prerequisites are the exact private reference/value placement or manual phone
step, any new credential egress and any companion/input permission. Root coordinates
them only against a concrete candidate; no owner action was performed for this slice.
Undo is disconnecting the integration and reverting its source, retaining receipt
tombstones for reconciliation; no auth, vault, device or security change to undo.

## Proof limits

Isolated fixtures exercise one-shot/restart/concurrency, mismatched IDs/targets,
expiry/revocation, held failures, raw socket input with synthetic values and absence
of process/log/artifact values. They do not prove native capture suspension,
installed automatic PIN input, secure Grok transport or real-order performance.
Those require A's installed integration and root's exclusive acceptance window.
