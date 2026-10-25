"""Calculate sample metrics without changing the pipeline or its output."""

import argparse
import csv
import json
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SAMPLE_REQUESTS = ROOT_DIR / "dataset" / "sample_deletion_requests.csv"
SAMPLE_ANSWERS = ROOT_DIR / "dataset" / "sample_output.csv"
FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def read_csv(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def read_steps(row):
    """Parse one steps_json value and require an array."""
    steps = json.loads(row["steps_json"])
    if not isinstance(steps, list):
        raise ValueError(f"{row['case_id']} steps_json is not a list")
    return steps


def percentage(correct, total):
    return 100.0 * correct / total if total else 0.0


def calculate(output_path):
    expected_rows = read_csv(SAMPLE_ANSWERS)
    predicted_rows = read_csv(output_path)
    expected = {row["case_id"]: row for row in expected_rows}
    predicted = {row["case_id"]: row for row in predicted_rows}

    if len(expected) != len(expected_rows):
        raise ValueError("sample_output.csv has duplicate case IDs")
    if len(predicted) != len(predicted_rows):
        raise ValueError("output CSV has duplicate case IDs")

    status_correct = 0
    sequence_correct = 0
    step_correct = 0
    step_slots = 0
    field_correct = {field: 0 for field in FIELDS}
    field_slots = 0

    for case_id, expected_row in expected.items():
        predicted_row = predicted.get(case_id)
        if predicted_row is None:
            expected_steps = read_steps(expected_row)
            predicted_steps = []
        else:
            expected_steps = read_steps(expected_row)
            predicted_steps = read_steps(predicted_row)
            status_correct += (
                predicted_row.get("deletion_status") == expected_row.get("deletion_status")
            )
            sequence_correct += predicted_steps == expected_steps

        slot_count = max(len(expected_steps), len(predicted_steps))
        for index in range(slot_count):
            expected_step = expected_steps[index] if index < len(expected_steps) else None
            predicted_step = predicted_steps[index] if index < len(predicted_steps) else None
            exact = expected_step is not None and predicted_step is not None
            for field in FIELDS:
                same = (
                    expected_step is not None
                    and predicted_step is not None
                    and expected_step.get(field) == predicted_step.get(field)
                )
                field_correct[field] += same
                field_slots += 1
                exact = exact and same
            step_correct += exact
            step_slots += 1

    # Extra prediction cases are wrong status/sequence cases, and their steps
    # count as extra ordinal slots for the field and step metrics.
    extra_cases = set(predicted) - set(expected)
    for case_id in extra_cases:
        steps = read_steps(predicted[case_id])
        for _step in steps:
            step_slots += 1
            field_slots += len(FIELDS)

    return {
        "case_count": len(expected_rows),
        "status_correct": status_correct,
        "sequence_correct": sequence_correct,
        "step_correct": step_correct,
        "step_slots": step_slots,
        "field_correct": field_correct,
        "field_slots": field_slots,
    }


def main():
    parser = argparse.ArgumentParser(description="Score pipeline output against public samples.")
    parser.add_argument(
        "output_csv", nargs="?", default=str(ROOT_DIR / "sample_result.csv"),
        help="Pipeline output CSV (default: sample_result.csv in the repository root)",
    )
    args = parser.parse_args()
    result = calculate(args.output_csv)

    print(f"Sample cases: {result['case_count']}")
    n = result["case_count"]
    print(
        f"Deletion status accuracy: {result['status_correct']}/{n} "
        f"({percentage(result['status_correct'], n):.2f}%)"
    )
    print(
        "Step accuracy (exact fields at the same step position; missing/extra steps count wrong): "
        f"{result['step_correct']}/{result['step_slots']} "
        f"({percentage(result['step_correct'], result['step_slots']):.2f}%)"
    )
    print("Field accuracy (same step position; missing/extra steps count wrong):")
    for field in FIELDS:
        correct = result["field_correct"][field]
        total = result["field_slots"]
        print(f"  {field}: {correct}/{total} ({percentage(correct, total):.2f}%)")
    print(
        f"Sequence accuracy (whole ordered steps_json exact): "
        f"{result['sequence_correct']}/{n} "
        f"({percentage(result['sequence_correct'], n):.2f}%)"
    )


if __name__ == "__main__":
    main()
