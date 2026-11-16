# Android wake binding: inspection and paused enrollment

DK-53 (ANYM-997). Two operator commands in the messaging CLI read one Android wake binding
exactly and create a new one only in the PAUSED, unverified state. They run with the
messaging service's ordinary operator connection (`DATABASE_URL_FILE`), like `principal-info`.
There is no MCP tool, HTTP route or gateway action for them.

Migration 097 keeps activation for root's separate routine and installed verification. Neither
command activates, rebinds, revokes or marks a binding verified, and neither has a flag for it. They
create no endpoint, key, grant, secret or policy change. They also send nothing, read no source
journal and touch no outbox row, cursor or generation.

## Inspect one binding

```sh
python -m messaging_service android-wake-binding-info \
  --principal PRINCIPAL --key-id PUBLIC_KEY_UUID --device DEVICE_ID
```

All three coordinates are required, so the command never lists bindings. It runs as one read-only
transaction with no row lock or lock function, so a runtime holder of the row never blocks it.
A miss returns `{"found": false, ...}` with the three coordinates only.
A binding of another principal, key or device is never shown.

A hit returns:

- `binding`: owner/source IDs, `auth_epoch`, `endpoint_id`, `generation`, `state`,
  `routine_verified`, `installed_verified`, `valid_until`, `live`, `journal_id` and `after_seq`;
- `key`: whether the same principal's key exists, is revoked or expired, whether its principal is
  enabled, and `wake_rights` (effective `read` and `delivery-write`);
- `endpoint`: for the same principal and key only, its ID, `state`, `version`, timestamps and
  `at_most_once_under_policy`. The endpoint's URL, host, headers, label, secrets and fingerprints
  are never printed;
- `outbox`: counts of this device's wake intents by state, never their identities;
- `activation_blockers`: the conditions `android_wake._binding` and its dispatcher would refuse
  on now, for example `state_paused`, `routine_unverified`, `installed_unverified`,
  `binding_expired`, `endpoint_not_enabled` or `endpoint_not_at_most_once`.

`stored_flags_are_not_delivery_proof` is always `true`. An empty blocker list repeats stored
values; it does not show that a receiver accepts wakes. Endpoint verification and the real routine
and installed proofs stay mandatory before any activation.

## Enroll a paused binding

The request is a JSON file of at most 4 KiB with exactly these fields:

```json
{"principal": "PRINCIPAL", "key_id": "PUBLIC_KEY_UUID", "device_id": "DEVICE_ID",
 "owner_id": "OWNER_ID", "source_id": "SOURCE_ID", "auth_epoch": 1,
 "endpoint_id": "ENDPOINT_UUID", "valid_until": "2026-12-01T00:00:00+00:00"}
```

```sh
python -m messaging_service android-wake-binding-enroll --file binding.json --check
python -m messaging_service android-wake-binding-enroll --file binding.json
```

`--check` runs every validation in a read-only transaction and prints the exact `would_create`
row, or the existing row. Without it, the command validates again under the principal's shared
ACL lock, then inserts the row. The new row has migration 097's defaults: `state=paused`,
`routine_verified=false`, `installed_verified=false`, `generation=1`, `after_seq=0`, no journal.

Validation, each refusal a fixed `{"error": code}` on stdout and exit status 1:

| Check | Error |
| --- | --- |
| Exact field set, ID syntax, `auth_epoch` a positive integer, `valid_until` with an offset | `invalid_binding_request` |
| The key exists and belongs to `principal` | `android_wake_key_not_found` |
| Principal enabled; key neither revoked nor expired | `android_wake_principal_disabled`, `android_wake_key_revoked`, `android_wake_key_expired` |
| Key's effective permissions include `read` and `delivery-write` | `android_wake_rights_missing` |
| `valid_until` is in the future and not after the key's expiry | `invalid_binding_lifetime` |
| The endpoint exists for the same principal and key | `android_wake_endpoint_not_same_key` |
| That endpoint is `enabled` | `android_wake_endpoint_not_enabled` |
| No binding exists for the principal, key and device | see below |

An existing binding is never changed. A paused row with the same owner, source, epoch, endpoint
and `valid_until` replays as `{"created": false, "binding": ...}`. An active or revoked row is
refused with `android_wake_binding_active` or `android_wake_binding_revoked`. A paused row with
different values is refused with `android_wake_binding_conflict`. Each of these refusals includes
the existing binding's metadata. Concurrent identical requests create one row, and the others
replay it. Of several different concurrent requests, one wins and the rest get the conflict.

If enrollment is refused, nothing is written. Fix the input, or report the gap: this path never
replaces, reactivates or verifies a binding.

## Proof

`tests/test_android_wake_binding_admin.py` uses a disposable database and synthetic values. It covers
the exact read with its no-lock and no-write proof and the absence of secrets, a miss, plan and
create, idempotent replay, every refusal above, existing active, revoked and conflicting rows, and
concurrent enrollment. No network, callback, device or model is involved.
