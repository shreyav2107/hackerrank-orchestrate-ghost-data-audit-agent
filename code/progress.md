# Sample optimization progress

## Baseline
- Pipeline and scorer: 24/32 correct statuses, 5/32 exact step lists.
- Difference classes before the first rule change: step missing/extra 16, missing record 10, wrong operation name 1.

## Iteration 1: explicit parent account verification
- Trace: the SQL search row in evidence_57 explicitly points from a child search resource to an account resource using `source_resource_id`; export_001 in evidence_59 and support_002 in evidence_61 have identity fields but no explicit parent account pointer.
- Rule: when a structured record explicitly links to an `account_` resource, add a planned absence verification for that parent unless a profile record already has its own step. This avoids inventing links from email alone.
- Score before: 24/32 status, 5/32 exact steps.
- Score after: 24/32 status, 6/32 exact steps.
- Decision: kept; exact matches increased and status accuracy did not fall.
## Iteration 2: remaining differences reviewed; no rule retained
- Largest remaining class after iteration 1: step missing/extra 15, missing record 10, wrong operation name 1.
- Traces: evidence_94's analytics lookup line has null subject identifiers, so the cache cannot tie that resource to the request. The export and support examples contain a person's email but no explicit account resource link, so deriving an account ID would be a guess. The case_24 audit row is in a manifest file captured after that case's audit cutoff, so the date filter correctly excludes it.
- Link scan: the only explicit resource-link fields in structured JSON/SQL are three `source_resource_id` values in evidence_57; the general parent-check rule already handles them.
- State scan: `account_status` values are ACTIVE (44), DELETED (18), and DELETION_PENDING (8); `status_code` is 404 (37) or 200 (10); exact `status` fields do not occur. `result` includes FOUND (125), NOT_FOUND (43), IDENTIFIABLE (24), ACTIVE (16), ANONYMIZED (9), BLOCKED (4), DISABLED (4), REDACTED (4), plus query outcomes hit (75) and miss (39). `record_type` values are category labels. Existing mapping handles the state values; hit/miss rows are query outcomes, not linked resource records. No CLOSED, DEACTIVATED, or INACTIVE values occur.
- Time and duplicates: timestamps are ISO UTC and the cutoff is inclusive; files captured later are skipped. Seven resources have different states at different record times and the later record wins; no equal-time state conflicts occur. A tie-break proposal is to use record time first, then prefer the category's stronger compliant state (ABSENT over PRESENT; REDACTED or ANONYMIZED over PRESENT; DISABLED over ACTIVE; BLOCKED over ACTIVE), leaving unresolved ties unhandled. It was not added because it has no sample case to improve under the score gate.
- Trial not retained: treating a subject-linked backup as ACTIVE based only on a matching activity claim fixes one status in an isolated case trial, but leaves its step list non-exact and depends on activity as state proof. The contract says activity is a claim, so no code change was kept.
- Decision: no further generalizable fix was supported by the raw evidence and the sample-score gate. Current score remains 24/32 statuses and 6/32 exact steps.
 
## Final full run
- Reran the full pipeline after the comment cleanup; runtime was 13.358 seconds.
- Validation: 518 output rows for 518 requests; exact columns case_id,deletion_status,steps_json; 518 unique IDs; sorted by case_id; all requested IDs present; every steps_json value parsed as a JSON list.
- Final sample score: 24/32 deletion statuses and 6/32 exact steps_json matches.
- Source scan found no sample_output reference or concrete sample case, user, or resource identifiers in pipeline.py or structured_reader.py.
 
## Scenario signatures and hold-out check
- Added read-only analyze.py; it reads sample_output.csv only for analysis and summarizes the 32 expected step lists as 21 operation/category signatures, with evidence categories, states, exact account-row matches, policy rows, and cited-evidence timing.
- Holdout split: 22 build cases and 10 held-out cases. One member of each repeated signature group is held out; four singleton signatures fill the remaining slots. Every repeated group has members in both sets.
- Final split scores: build 16/22 correct statuses and 4/22 exact steps; held out 8/10 correct statuses and 2/10 exact steps; all samples 24/32 correct statuses and 6/32 exact steps. The held-out exact rate (20%) is close to the build rate (18%), so this split does not indicate memorized rules.
- Candidate trial: omit VERIFY_RECORD_PRESENT for active or blocked BACKUP_ARCHIVE records when VERIFY_RESTORE_BLOCKED is planned. Build score before/after remained 16/22 statuses and 4/22 exact steps, so the candidate was reverted.
- No new pipeline rule was retained. The repeated scenario shapes are already handled by category policy, state, and process rules; remaining differences require missing evidence, an unsupported identity/resource link, or a conflict between sample steps and deletion_rules.csv.

## Scenario analysis final full run
- Ran the full pipeline to output_final.csv; runtime was 43.370 seconds.
- Validation: 518 output rows for 518 requests; columns match the sample; IDs are unique, sorted, and match the request set; all steps_json values are valid JSON lists.
- The final source scan found no sample_output reference or concrete sample case/user/resource IDs in pipeline.py or structured_reader.py.
- Final sample score remains 24/32 correct statuses and 6/32 exact step lists.
## Audit-time and limitations trials
- Problem statement rule checked: “Determine the resource state at `audit_at` using the available evidence. Reported workflow statuses and operation results are claims.” It does not explicitly require dropping a file based only on its later `captured_at`, so Step 1 was trialed.
- Baseline sample score: 24/32 correct deletion statuses; 6/32 exact `steps_json` matches.
- Step 1 trial removed the file-level capture cutoff and used each structured record's `record_time`, falling back to manifest `captured_at` when missing. Trial score: 23/32 statuses; 6/32 exact. Reverted because status accuracy dropped.
- Step 2 diagnostic: 0/32 samples had both records and a non-empty limitations list, so no case or limitation reached the early-return path. Four cases were unhandled for having no matching evidence (one had only a related activity claim), not because of this branch.
- Step 2 trial built steps when records exist and retained limitation text as the unhandled reason; sample score stayed 24/32 statuses and 6/32 exact. Reverted because exact matches did not rise.
- Final full run wrote 518 rows to `output_final.csv`; validation found matching columns, unique sorted IDs matching all requests, and 0 invalid or non-list `steps_json` values. The initial output was copied to `output_final_v1.csv` as requested.
- Final sample score remains 24/32 statuses and 6/32 exact. No trial change was retained.

## Submission history summary
- Early Gemini extraction replies failed validation or omitted some useful resource rows. A deterministic JSON/SQL reader was added for structured evidence so those formats do not depend on model extraction; Gemini cache records remain for the other evidence formats.
- A raw-evidence resource-ID recall scan compared IDs visible in evidence text with cached IDs and exposed coverage gaps. It was used as a diagnostic, not as proof that an ID belongs to a person.
- The account-link rule uses `source_resource_id` only when the evidence explicitly supplies that parent link. An email match alone does not create a parent-account link.
- Evidence remains subject to the `audit_at` cutoff. Removing the file-level capture cutoff was trialed in the latest iteration and reverted because status accuracy fell from 24/32 to 23/32 while exact steps stayed 6/32.
- Scenario signatures were checked with a 22-case build split and a 10-case hold-out split. The recorded split results were 16/22 status and 4/22 exact on build cases, 8/10 status and 2/10 exact on held-out cases, and 24/32 status and 6/32 exact overall.

## Final submission package
- Copied the best `output_final.csv` to `output.csv`; their SHA-256 hashes match. The final validator reports 518/518 rows, the required header, sorted unique request IDs, valid JSON arrays, and no evidence-ID contract errors.
- Added sample status/step/field/sequence metrics and three offline synthetic request/evidence fixtures. All three synthetic fixtures pass against the pipeline.
- Added the Gemini intent and evidence prompts, setup/run documentation, dependency list, and a key-free `.env.example`.
- Built `code.zip` from an explicit allowlist containing source, prompts, evaluation files, README, requirements, progress, and the two required caches. Dataset, `.env`, backups, failed replies, and bytecode caches are excluded.
- Verified the archive extracts, contains no key-shaped string, passes packaged-source grep checks, and passes all three synthetic fixtures. No API calls were made while preparing the submission.
