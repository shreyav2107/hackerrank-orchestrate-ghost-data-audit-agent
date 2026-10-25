"""Run small offline fixtures through a fresh copy of the real pipeline."""

import argparse
import csv
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "synthetic_cases.json"


def write_csv(path, columns, rows):
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Exercise the pipeline on synthetic data only.")
    parser.add_argument(
        "--quiet", action="store_true", help="Print only pass/fail and the actual outputs."
    )
    args = parser.parse_args()
    fixture_data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    cases = fixture_data["cases"]

    with tempfile.TemporaryDirectory(prefix="deletion-audit-fixtures-") as temporary:
        root = Path(temporary)
        dataset = root / "dataset"
        code = root / "code"
        dataset.mkdir()
        code.mkdir()
        for filename in ("main.py", "pipeline.py", "structured_reader.py"):
            shutil.copy2(ROOT_DIR / "code" / filename, code / filename)

        requests = [case["request"] for case in cases]
        identities = [case["identity"] for case in cases]
        evidence_manifest = {}
        cache = {}
        for case in cases:
            for item in case["evidence"]:
                file_id = item["file_id"]
                evidence_manifest.setdefault(file_id, {
                    "file_id": file_id,
                    "file_path": item["file_path"],
                    "origin": "synthetic fixture",
                    "captured_at": item["captured_at"],
                })
                cache.setdefault(file_id, []).append(item["record"])

        write_csv(
            dataset / "deletion_requests.csv",
            ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"],
            requests,
        )
        write_csv(
            dataset / "user_identities.csv",
            ["user_id", "name", "email", "phone", "email_hash", "updated_at"],
            identities,
        )
        write_csv(
            dataset / "user_audit_log.csv",
            ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"],
            [],
        )
        write_csv(
            dataset / "evidence_files.csv",
            ["file_id", "file_path", "origin", "captured_at"],
            list(evidence_manifest.values()),
        )
        write_csv(
            dataset / "deletion_rules.csv",
            ["rule_id", "data_category", "condition", "required_action"],
            [{
                "rule_id": "fixture_tombstone_rule",
                "data_category": "ACCOUNT_TOMBSTONE",
                "condition": "Retain only the stable user reference",
                "required_action": "REDACT",
            }],
        )
        (root / "evidence_cache.json").write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        input_path = root / "synthetic_requests.csv"
        output_path = root / "synthetic_output.csv"
        # The pipeline reads dataset/deletion_requests.csv for supporting data,
        # and accepts the request CSV as its first command-line argument.
        input_path.write_text(
            (dataset / "deletion_requests.csv").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        subprocess.run(
            [sys.executable, str(code / "main.py"), str(input_path), str(output_path)],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )

        actual_rows = {}
        with output_path.open("r", encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                actual_rows[row["case_id"]] = {
                    "deletion_status": row["deletion_status"],
                    "steps": json.loads(row["steps_json"]),
                }

        failures = 0
        results = []
        for case in cases:
            case_id = case["request"]["case_id"]
            expected = case["expected"]
            actual = actual_rows.get(case_id)
            passed = actual == expected
            failures += not passed
            results.append({
                "case_id": case_id,
                "expected": expected,
                "actual": actual,
                "passed": passed,
            })
            print(f"{case_id}: {'PASS' if passed else 'FAIL'}")
            if not args.quiet or not passed:
                print("  expected:", json.dumps(expected, ensure_ascii=False, separators=(",", ":")))
                print("  actual:  ", json.dumps(actual, ensure_ascii=False, separators=(",", ":")))

        actuals_path = Path(__file__).resolve().parent / "synthetic_actuals.json"
        actuals_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Saved actual fixture outputs to {actuals_path}")
        print(f"Synthetic fixtures passed: {len(cases) - failures}/{len(cases)}")
        if failures:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
