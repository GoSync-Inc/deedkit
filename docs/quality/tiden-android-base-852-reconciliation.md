# Android base-interface record — 2026-11-13

ANYM-995 records the base Android service and gateway contract introduced by
[PR #852](https://github.com/GoSync-Inc/deedkit/pull/852), exact squash
`0e8354f0e25d3b3d6a1b2e854c750a0ba20b245b`. The original PR body named no spec;
its Tiden sync stopped at `anchors` because `.github/workflows/android.yml` and
`services/android/src/deedkit_android/__init__.py` had no spec anchor. This dated
receipt supplies the historical contract and assertion mapping for reconciliation.

The story belongs to ANYM-204 and the Platform Gateway component. It covers the
original scoped device service, reservations, durable receipts, screen observation,
file transfers and gateway boundary. ANYM-951 adds later reusable workflows;
ANYM-984 standardizes later refusal envelopes; ANYM-662 adds module diagnostics.
Those increments retain their own contracts. The original eight Android tool names
are historical; this receipt does not assert they remain exposed after PR #965.

## Existing assertion evidence

The original source assertions were read at the #852 squash. Their existing case
identities have passed results in published synthetic Tiden run **1501**, build
`51165861875c399794900a784c921422075fc51e`, environment
`github-actions-synthetic`, live-doc status `succeeded`. This is evidence from
existing tests, not a new execution of the old checkout or proof of live phone use.

| ANYM-995 criterion | Existing assertions at the original source baseline |
| --- | --- |
| Device/principal admission and exclusive reservations | `services/android/tests/test_device_service.py`: `test_auth_device_grants_and_reservation`, `test_expiry_revocation_and_busy_do_not_admit_a_second_command`, `test_expired_reservation_and_shutdown_receipt` |
| Durable request identity, replay refusal and uncertain outcomes | Same file: `test_replay_never_repeats_device_action_and_conflicts_are_rejected`, `test_restart_marks_inflight_unknown_and_keeps_receipt`; expiry/shutdown test above |
| Scoped files, recoverable chunks and validated APK operands | Same file: `test_binary_files_retry_offsets_scope_and_device_only_commands`, `test_partial_chunk_recovery_and_exclusive_store`, `test_install_stages_apks_and_rejects_unvalidated_host_operands` |
| Native observations and partial capture failure | Same file: `test_hierarchy_timeout_still_returns_screenshot`, `test_high_resolution_phone_screenshot_exceeds_command_output_limit`; authentication/reservation test also asserts password redaction and screen elements |
| Gateway admission, outage isolation, image validation and original request ID | `services/gateway/tests/test_android.py`: `test_android_tools_and_native_image_preserve_structured_screen`, `test_android_grants_and_outage_preserve_calendar`, `test_lost_android_reply_keeps_original_request_id_and_never_retries`, `test_invalid_android_image_is_refused`, `test_high_resolution_android_image_is_returned_as_native_content`; service test `test_gateway_catalog_matches_service_schemas` |
| Android dependency floor and module verification | `tests/test_lockfile_advisories.py::test_android_lockfile_pins_no_version_below_a_known_advisory_fix`; `deedkit-ci::android` records execution of the Android synthetic suite, rather than independently proving every criterion |

These are eighteen existing case identities, including the module verification;
parameter combinations retain their existing identities. No test path, name or
parameter is changed or imported again. The proposed links are limited to the
assertions above. This receipt does not establish that those links have reached
Tiden main; that requires the later guarded data repair and readback.

## Delivery boundary

This PR records one missing historical story through its own proposal branch.
It names `Spec: ANYM-995` and carries no `Delivers:` line. The original #852 PR
must likewise receive only a truthful Spec reference during its separately
authorized repair, without being relabeled as delivery of later Android features.

Main status/source/link writes still require the orchestrator's guarded maintenance
window under [TIDEN-CI.md](../../TIDEN-CI.md#main-sync). Ordinary proposal authoring
uses the building PR's branch under the
[product workflow](../operations/tiden-product-workflow.md#ready-merge), with sync
remaining enabled. This receipt changes no runtime, authority, required check,
test identity, drift threshold or production configuration, and claims no
production acceptance or new phone action.
