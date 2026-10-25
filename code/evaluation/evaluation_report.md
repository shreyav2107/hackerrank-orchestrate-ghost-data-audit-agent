# Evaluation report

## Public sample results

The metrics below were produced by `python evaluation/metrics.py sample_result.csv` after running the current pipeline on all 32 public requests. Step and field accuracy compare steps at the same ordinal position; missing and extra steps count as incorrect. Sequence accuracy requires the complete ordered JSON arrays to match.

```text
Sample cases: 32
Deletion status accuracy: 24/32 (75.00%)
Step accuracy (exact fields at the same step position; missing/extra steps count wrong): 100/197 (50.76%)
Field accuracy (same step position; missing/extra steps count wrong):
  phase: 149/1182 (12.61%)
  system_id: 103/1182 (8.71%)
  resource_id: 103/1182 (8.71%)
  operation: 111/1182 (9.39%)
  state: 112/1182 (9.48%)
  evidence_id: 140/1182 (11.84%)
Sequence accuracy (whole ordered steps_json exact): 6/32 (18.75%)
```

Field accuracy uses six requested fields across the union of expected and predicted step slots (197 slots, or 1,182 field comparisons). A slot missing from either side counts as six incorrect fields. The whole-sequence metric includes step order and the full step objects.

## Additional synthetic evaluation cases

These fixtures use fictional `.test` email addresses and isolated temporary datasets. `evaluation/run_synthetic_cases.py` copies the bundled pipeline into a temporary directory, runs the actual pipeline offline, and checks its output against the expected row.

### SYN_NON_DELETE

- Request: Please send me a copy of my information; do not delete my account.
- Evidence fixture: No evidence records are supplied; the request explicitly declines deletion.
- Fixture result: PASS

Expected output:

```json
{"deletion_status":"NOT_APPLICABLE","steps":[]}
```

Pipeline actual output:

```json
{"deletion_status":"NOT_APPLICABLE","steps":[]}
```

### SYN_TOMBSTONE

- Request: Please delete my account and personal data.
- Evidence fixture: One ACCOUNT_TOMBSTONE record has state DELETED and mentions only the stable user_id.
- Fixture result: PASS

Expected output:

```json
{"deletion_status":"COMPLETE","steps":[{"step_no":1,"phase":"VERIFICATION","system_id":"identity_service","resource_id":"account_tombstone_synth_002","operation":"VERIFY_RECORD_REDACTED","state":"REDACTED","evidence_id":"fixture_tombstone"}]}
```

Pipeline actual output:

```json
{"deletion_status":"COMPLETE","steps":[{"step_no":1,"phase":"VERIFICATION","system_id":"identity_service","resource_id":"account_tombstone_synth_002","operation":"VERIFY_RECORD_REDACTED","state":"REDACTED","evidence_id":"fixture_tombstone"}]}
```

### SYN_FLOW_RESTORE

- Request: Please erase my account and personal data.
- Evidence fixture: One active DATA_FLOW and one active BACKUP_ARCHIVE record both link to the synthetic user_id.
- Fixture result: PASS

Expected output:

```json
{"deletion_status":"INCOMPLETE","steps":[{"step_no":1,"phase":"FINDING","system_id":"integration_service","resource_id":"data_flow_synth_003","operation":"REPORT_ACTIVE_DATA_FLOW","state":"ACTIVE","evidence_id":"fixture_flow_restore"},{"step_no":2,"phase":"FINDING","system_id":"backup_service","resource_id":"backup_synth_003","operation":"REPORT_RESTORE_RISK","state":"ACTIVE","evidence_id":"fixture_flow_restore"},{"step_no":3,"phase":"REMEDIATION","system_id":"integration_service","resource_id":"data_flow_synth_003","operation":"DISABLE_DATA_FLOW","state":"DISABLED","evidence_id":""},{"step_no":4,"phase":"REMEDIATION","system_id":"backup_service","resource_id":"backup_synth_003","operation":"BLOCK_RESTORE","state":"BLOCKED","evidence_id":""},{"step_no":5,"phase":"VERIFICATION","system_id":"backup_service","resource_id":"backup_synth_003","operation":"VERIFY_RECORD_PRESENT","state":"PRESENT","evidence_id":""},{"step_no":6,"phase":"VERIFICATION","system_id":"integration_service","resource_id":"data_flow_synth_003","operation":"VERIFY_DATA_FLOW_DISABLED","state":"DISABLED","evidence_id":""},{"step_no":7,"phase":"VERIFICATION","system_id":"backup_service","resource_id":"backup_synth_003","operation":"VERIFY_RESTORE_BLOCKED","state":"BLOCKED","evidence_id":""}]}
```

Pipeline actual output:

```json
{"deletion_status":"INCOMPLETE","steps":[{"step_no":1,"phase":"FINDING","system_id":"integration_service","resource_id":"data_flow_synth_003","operation":"REPORT_ACTIVE_DATA_FLOW","state":"ACTIVE","evidence_id":"fixture_flow_restore"},{"step_no":2,"phase":"FINDING","system_id":"backup_service","resource_id":"backup_synth_003","operation":"REPORT_RESTORE_RISK","state":"ACTIVE","evidence_id":"fixture_flow_restore"},{"step_no":3,"phase":"REMEDIATION","system_id":"integration_service","resource_id":"data_flow_synth_003","operation":"DISABLE_DATA_FLOW","state":"DISABLED","evidence_id":""},{"step_no":4,"phase":"REMEDIATION","system_id":"backup_service","resource_id":"backup_synth_003","operation":"BLOCK_RESTORE","state":"BLOCKED","evidence_id":""},{"step_no":5,"phase":"VERIFICATION","system_id":"backup_service","resource_id":"backup_synth_003","operation":"VERIFY_RECORD_PRESENT","state":"PRESENT","evidence_id":""},{"step_no":6,"phase":"VERIFICATION","system_id":"integration_service","resource_id":"data_flow_synth_003","operation":"VERIFY_DATA_FLOW_DISABLED","state":"DISABLED","evidence_id":""},{"step_no":7,"phase":"VERIFICATION","system_id":"backup_service","resource_id":"backup_synth_003","operation":"VERIFY_RESTORE_BLOCKED","state":"BLOCKED","evidence_id":""}]}
```


## Failures found and improvements

- Gemini extraction sometimes failed validation or omitted useful resource rows. The structured reader now parses supported JSON and SQL snapshots deterministically, while the Gemini cache remains for other formats.
- A raw resource-ID recall scan compared IDs in evidence files with IDs in the cache and exposed coverage gaps. It was used to locate missing coverage; it did not create identity links.
- The account-link rule adds parent-account verification only when an evidence row explicitly supplies `source_resource_id`. A shared email by itself does not prove which account resource is the parent.
- The pipeline applies the `audit_at` cutoff. A trial that removed the manifest capture-time cutoff fell from 24/32 to 23/32 correct statuses and stayed at 6/32 exact step matches, so it was reverted.
- Scenario signatures were evaluated with 22 build cases and 10 held-out cases. Build results were 16/22 statuses and 4/22 exact sequences; held-out results were 8/10 statuses and 2/10 exact sequences. Overall results were 24/32 statuses and 6/32 exact sequences.

## Known gaps

- Some relevant resources are absent from the current cache, and some evidence records do not contain an exact identifier link. These cases remain incomplete rather than receiving guessed links.
- The raw analytics lookup example in `evidence_94` has null subject identifiers, so it cannot be tied to a user from that row.
- Some export and support records mention a person's email but provide no explicit account parent pointer. The pipeline reports the supported records and does not infer an account ID from the email.
- Evidence captured after `audit_at` is excluded. The progress log traced a late-captured audit row that therefore could not support the earlier case state.
- Remaining sample differences include missing or extra steps and state/operation mismatches. The current rules cannot resolve these when source evidence is missing, links are ambiguous, or the sample step interpretation conflicts with the evidence and policy.
