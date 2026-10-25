"""Check the required output.csv contract without consulting sample answers."""

import argparse
import csv
import json
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
EXPECTED_HEADER = ["case_id", "deletion_status", "steps_json"]
ALLOWED_STATUSES = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}


def read_rows(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        return reader.fieldnames, list(reader)


def main():
    parser = argparse.ArgumentParser(description="Validate a generated output CSV.")
    parser.add_argument("output_csv", nargs="?", default=str(ROOT_DIR / "output.csv"))
    parser.add_argument(
        "requests_csv", nargs="?", default=str(ROOT_DIR / "dataset" / "deletion_requests.csv")
    )
    args = parser.parse_args()

    header, rows = read_rows(args.output_csv)
    _request_header, requests = read_rows(args.requests_csv)
    errors = []
    ids = [row.get("case_id", "") for row in rows]
    expected_ids = {row["case_id"] for row in requests}

    if header != EXPECTED_HEADER:
        errors.append(f"header is {header!r}, expected {EXPECTED_HEADER!r}")
    if len(rows) != len(requests):
        errors.append(f"row count is {len(rows)}, expected {len(requests)}")
    if len(ids) != len(set(ids)):
        errors.append("case_id values are not unique")
    if ids != sorted(ids):
        errors.append("rows are not sorted by case_id")
    if set(ids) != expected_ids:
        errors.append("output case IDs do not match the request CSV")

    valid_json = 0
    for row in rows:
        case_id = row.get("case_id", "")
        status = row.get("deletion_status", "")
        if status not in ALLOWED_STATUSES:
            errors.append(f"{case_id}: invalid deletion_status {status!r}")
        try:
            steps = json.loads(row.get("steps_json", ""))
        except json.JSONDecodeError:
            errors.append(f"{case_id}: steps_json is invalid JSON")
            continue
        if not isinstance(steps, list):
            errors.append(f"{case_id}: steps_json is not a JSON array")
            continue
        valid_json += 1

        if status == "NOT_APPLICABLE" and steps:
            errors.append(f"{case_id}: NOT_APPLICABLE must have an empty steps list")
        if status == "COMPLETE":
            for number, step in enumerate(steps, start=1):
                if not step.get("evidence_id"):
                    errors.append(f"{case_id}: COMPLETE step {number} lacks evidence_id")
        if status == "INCOMPLETE":
            for number, step in enumerate(steps, start=1):
                if step.get("phase") in {"REMEDIATION", "VERIFICATION"} and step.get("evidence_id") != "":
                    errors.append(
                        f"{case_id}: planned INCOMPLETE step {number} must have empty evidence_id"
                    )

    print(f"Rows: {len(rows)}/{len(requests)}")
    print(f"Header: {'OK' if header == EXPECTED_HEADER else 'FAIL'}")
    print(f"Sorted and unique case IDs: {'OK' if ids == sorted(ids) and len(ids) == len(set(ids)) else 'FAIL'}")
    print(f"Valid JSON arrays: {valid_json}/{len(rows)}")
    print(f"Contract errors: {len(errors)}")
    for error in errors:
        print(f"- {error}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
