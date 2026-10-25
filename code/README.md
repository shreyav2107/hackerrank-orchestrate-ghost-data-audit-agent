# Ghost Data Deletion Audit Agent

This project checks whether a deletion request is authorized, resolves the
person's current and historical identifiers, finds relevant records, applies
the deletion policy, and writes an ordered audit plan to `output.csv`.

## Setup

Use Python 3.10 or newer. From the repository root, install the dependency
needed only for Gemini-backed extraction and intent classification:

```powershell
python -m pip install -r requirements.txt
```

The deterministic pipeline reads the participant files under `dataset/` and
the saved `evidence_cache.json` and `intent_cache.json`. It does not need an
API key. To use Gemini to refresh either cache, copy `.env.example` to `.env`
and set `GEMINI_API_KEY` to your own key. Never commit `.env`.

## Run the final pipeline

From the repository root:

```powershell
python code/main.py dataset/deletion_requests.csv output.csv
```

The output has exactly `case_id,deletion_status,steps_json`, one sorted row per
request. The pipeline writes `unhandled.txt` beside the output with reasons
when it cannot produce evidence-backed steps.

You can also call the packaged pipeline module directly:

```powershell
python code/pipeline.py dataset/deletion_requests.csv output.csv
```

## Optional Gemini cache refresh

These commands make Gemini API calls and are not needed to reproduce the
included output:

```powershell
python code/classify_intent.py
python code/extract_evidence.py
```

`classify_intent.py` labels request authorization in batches and saves
`intent_cache.json`. `extract_evidence.py` reads each uncached manifest file
once per run, sends text formats as text and PDFs/images as file parts, and saves valid
records after each file to `evidence_cache.json`. Both scripts use the model
configured in `extract_evidence.py`, retry bounded failures, keep a daily call
counter, and log usage. Extraction can be resumed; cached file IDs are skipped.
An individual file can be selected with:

```powershell
python code/extract_evidence.py --only evidence_61
```

The prompt templates used by those calls are in `prompts/`.

## Architecture

1. The pipeline checks intent from `intent_cache.json`, falling back to clear
   plain-code wording rules when a label is missing.
2. It matches request email addresses to `user_identities.csv` and recovers
   old identifiers from `user_audit_log.csv`.
3. `structured_reader.py` reads JSON and SQL snapshots with deterministic
   field mappings. Other evidence records come from the saved Gemini cache.
4. The pipeline filters evidence at the case's `audit_at`, ignores flagged
   cached records, and applies `deletion_rules.csv`.
5. It orders findings, planned remediation, and verification steps by the
   operation table, then canonical system and resource IDs.

The included caches let the final pipeline run without a model. Gemini is used
only for reading unstructured evidence and classifying request intent; policy,
identity matching, state normalization, ordering, and CSV writing are plain
Python.

## Failure handling

- When evidence is missing or cannot be matched, the case is `INCOMPLETE` and
  its reason is written to `unhandled.txt`.
- Invalid extracted records are dropped or flagged individually; other valid
  records from the same file are kept.
- Evidence extraction retries at most twice per file, stops after five
  consecutive failed files, saves after each file, and stops at the configured
  daily call limit. Per-day quota errors stop without retry; per-minute errors
  wait and retry within the attempt limit.
- The reader does not invent account links from a shared email alone. It adds
  parent-account verification only when evidence explicitly provides a
  `source_resource_id` link.
- Activity rows are treated as claims, not proof that a record was deleted or
  that a process changed state.

## Evaluation and testing

Run the offline synthetic fixtures and final output checks with:

```powershell
python evaluation/run_synthetic_cases.py
python evaluation/validate_output.py output.csv dataset/deletion_requests.csv
```

After generating a sample output, calculate status, step, field, and sequence
metrics with:

```powershell
python code/pipeline.py dataset/sample_deletion_requests.csv sample_result.csv
python evaluation/metrics.py sample_result.csv
```

`evaluation/metrics.py` is the only added evaluation script that reads
`dataset/sample_output.csv`. Metrics and the actual synthetic expected/output
pairs are documented in `evaluation/evaluation_report.md`. Usage accounting is
in `evaluation/usage_report.md`.

## Known gaps

Some files are not represented in the current evidence cache, and some records
have no exact identity link. Those cases remain incomplete rather than being
linked by guesswork. The sample evaluation also has unresolved record and step
differences; see the evaluation report and `progress.md` for the traced causes
and score history.

## Dataset
The participant `dataset/` folder is not included in this zip. Place it at the repository root before running.

## Results (32 samples)
- Status accuracy: 24/32 (75%)
- Step accuracy: 100/197 (50.8%)
- Full sequence matches: 6/32

## Design decisions
JSON and SQL evidence is read by exact parsers, not by Gemini. An earlier version had Gemini summarize every evidence file, and a recall scan found 164 record ids (accounts, data flows, analytics) in the raw files but missing from the cache. Gemini is now used only for request intent and for logs, images and PDFs.

## Experiments kept and reverted
- Judging evidence by each record's own date instead of the file's capture date lowered status accuracy from 24 to 23, so it was reverted.
- Building steps even when a limitation was logged gave no exact-match gain, so it was reverted.

## Integrity
Pipeline code never reads `sample_output.csv` and contains no sample ids. Only `evaluation/metrics.py` reads it. Links between records and users are made only by exact identifier match, never by guessing.

## Known gaps
- Cases 01, 02, 04, 07 and 28 have no exact link to the user in any file, so they are left unlinked.
- Some records are linked to the user only through child rows (search, export, support), with no explicit account link.
- Some PDF and log evidence is missing or thin in the cache.
- Case 12: the sample expects deletion while deletion_rules.csv says anonymize; the written policy is followed.
See `progress.md` and `evaluation/evaluation_report.md` for details.
