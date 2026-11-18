# Reconcile criteria with adopted contracts and linked tests

This records six content-only corrections on the PR-owned Tiden branch
`review/2026-11-19-criteria-evidence-reconciliation`. It reconciles existing
behaviour and evidence; it changes no runtime code, grants, configuration,
privacy choice or test identity.

| Spec | Correction | Authority or existing proof |
|---|---|---|
| ANYM-391 | State the receipt journal's existing 1000-entry and 4 MiB bounds; replace the stale statement that the entry bound is untested. | `TestNativeSendReceiptJournalIsBoundedTo1000Entries`, test UUID `a5aa5041-1cb8-40cb-acf7-63d761aa9a36`: entry 1000 succeeds, the next is `spool_full`, and 1000 remain. The older byte-bound proof stays. |
| ANYM-393 | Remove the stale “not asserted” annotation from the 1024-credential upload-rate bound. | `test_upload_rate_state_tracks_at_most_1024_credentials`, test UUID `df8045e5-5501-4403-8c27-38413fe87a02`: a new credential is refused while all 1024 are active and admitted after the idle interval. |
| ANYM-402 | Remove the stale “not asserted” annotations for API and calendar probes using a specific bind address. | `test_api_healthcheck_requests_a_specific_bind_address`, test UUID `700d57e2-08ec-4b58-bec9-3be3b9c1336d`, and `test_calendar_healthcheck_requests_a_specific_bind_address`, test UUID `52e3d870-b9bd-4737-bc81-16bc4fc899c5`. |
| ANYM-70 | Include `deedkit_inference`, including audio, under its existing local-inference contract; state the adopted document default, audio maximum and mini timeout. | ANYM-685 criterion 1; ANYM-697 “Profile limits”; ANYM-702 “Document bound”. |
| ANYM-185 | Reconcile the broad matcher-role denials with the already adopted, narrow retention reads, deletes and progress fields. | ANYM-446 criterion 7 and ANYM-447 criteria 1, 3 and 6. Other denials remain. |
| ANYM-130 | Describe the migration range and newer-database refusal relative to the runtime's newest schema, retaining version 35 as the dated 2026-10-16 source review. | Existing immutable-migration rule and `test_a_database_newer_than_the_runtime_is_refused_and_nothing_changes`, test UUID `09a9e1e0-b20e-445e-ab5c-67cac935b270`. |

The evidence annotations for ANYM-391, ANYM-393 and ANYM-402 retain their dated
History entries and append the later linked proof. Their statuses remain
Review. ANYM-70, ANYM-185 and ANYM-130 retain their Done statuses; this PR does
not make a new completion decision.

## Contract details

The ANYM-70 correction retains the existing privacy opt-in and provider
boundaries. `deedkit_inference` requires `local_inference: true`, its prescribed
HTTPS `.ts.net` `/v1` endpoint, an absolute key-file path and the kind's model
alias; it takes no `allow_cloud`. Its timeout maximum is 900 seconds. A document
profile defaults to 32 MiB; an audio profile may accept up to 200 MiB while its
default remains 20 MiB. ANYM-697 and ANYM-702 retain their own open delivery
outcomes. The non-mini audio tests keep their identities and do not purport to
be the complete provider allowlist.

The ANYM-185 correction permits only the retention access already specified in
ANYM-446 and ANYM-447. It keeps the denials on message mutation, sending,
credentials, provider sessions, hint receivers, dispatch, receipt contents and
receipt or consumption insertion/update. No database privilege changes here.

## Preservation and limits

Only each target's `content` changes. IDs, titles, types, statuses, priorities,
parents, components, complete source objects and existing test links are
preserved. The six current pre-images and full before/after contents are kept
in the private reconciliation receipt; the PR check previews the same six
specs, and normal sync B merges the branch with this squash.

ANYM-78 criterion 2 needs no correction: its write list is not exhaustive.
The optional ANYM-851 table-name clarification and the unestablished adoption
of `media_reference_expired` for ANYM-400 remain separate. ANYM-254 and ANYM-805
remain Review with their production outcomes unproved. This PR supplies no
live or production acceptance and no new permission or owner quotation.

Decided by agents: reconcile stale evidence annotations, already adopted
follow-ups and a dated schema number without changing what the user gets.
Undo is a new PR-owned review branch restoring the previous content, with the
same identities, statuses, sources and test links.

Evidence references use persistent test UUIDs. A trace view's `TC-*` display
caption is not used as the identity or as a canonical case number.
