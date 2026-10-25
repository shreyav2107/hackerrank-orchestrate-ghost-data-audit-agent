"""A deterministic first-pass deletion audit pipeline (no model calls)."""

import argparse
import csv
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from structured_reader import SYSTEM_ID_BY_CATEGORY, read_structured_file


ROOT_DIR = Path(__file__).resolve().parents[1]
DATASET_DIR = ROOT_DIR / "dataset"
IDENTITY_FIELDS = {"name", "email", "phone", "email_hash"}

# Convert evidence wording to the states used by the output contract. Context-
# dependent cases (ACTIVE and DELETED) are handled in normalize_state below.
STATE_MAP = {
    "FOUND": "PRESENT",
    "IDENTIFIABLE": "PRESENT",
    "NOT_FOUND": "ABSENT",
    "DELETION_PENDING": "PRESENT",
    "ACTIVE": "PRESENT",
    "DELETED": "ABSENT",
    "PRESENT": "PRESENT",
    "ABSENT": "ABSENT",
    "REDACTED": "REDACTED",
    "ANONYMIZED": "ANONYMIZED",
    "DISABLED": "DISABLED",
    "BLOCKED": "BLOCKED",
}

# These orders follow the Allowed Operations table in problem_statement.md.
OPERATION_ORDER = {
    "FINDING": {
        "REPORT_REMAINING_RECORD": 0,
        "REPORT_EXPOSED_PII": 1,
        "REPORT_ACTIVE_DATA_FLOW": 2,
        "REPORT_RESTORE_RISK": 3,
    },
    "REMEDIATION": {
        "DISABLE_DATA_FLOW": 0,
        "BLOCK_RESTORE": 1,
        "DELETE_RECORD": 2,
        "REDACT_RECORD": 3,
        "ANONYMIZE_RECORD": 4,
    },
    "VERIFICATION": {
        "VERIFY_RECORD_ABSENT": 0,
        "VERIFY_RECORD_PRESENT": 1,
        "VERIFY_RECORD_REDACTED": 2,
        "VERIFY_RECORD_ANONYMIZED": 3,
        "VERIFY_DATA_FLOW_DISABLED": 4,
        "VERIFY_RESTORE_BLOCKED": 5,
    },
}

# Plain keyword checks cover only clear requests. Unknown wording is left unhandled.
DELETE_WORDS = (
    "delete", "deletion", "erase", "erasure", "remove", "removal",
    "purge", "forget", "forgotten", "supprimer", "effacer", "eliminar",
    "borrar", "löschen", "deletar", "apagar", "削除", "删除", "刪除",
    "삭제", "удалить",
)
DELETE_TARGET_WORDS = (
    "account", "profile", "personal", "data", "information", "details",
    "myself", "me", "records",
)
NON_DELETE_PHRASES = (
    "access my data", "access request", "export my data", "download my data",
    "correct my", "update my", "change my", "opt out", "opt-out",
    "unsubscribe", "stop marketing", "marketing emails",
)
NEGATED_DELETE_PHRASES = (
    "do not delete", "don't delete", "do not erase", "don't erase",
    "not asking for deletion", "not a deletion request",
)


def read_csv(path):
    """Read a CSV file as dictionaries, accepting an optional UTF-8 BOM."""
    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def parse_time(value):
    """Parse the ISO timestamps used by the dataset."""
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))


def normalize_state(raw_state, category, record, user_id):
    """Normalize source wording, keeping process states only for real processes."""
    raw_state = (raw_state or "").strip().upper()
    category = (category or "").strip().upper()

    # ACTIVE describes a process only for enabled flows and restorable backups.
    if raw_state == "ACTIVE" and category in {"DATA_FLOW", "BACKUP_ARCHIVE"}:
        return "ACTIVE"

    # A DELETED tombstone can still expose PII. Keep only-user-id tombstones
    # as redacted; report a tombstone with another matched identifier as present.
    if raw_state == "DELETED" and category == "ACCOUNT_TOMBSTONE":
        mentioned = record.get("identifiers_mentioned", [])
        stable_id = (user_id or "").casefold()
        has_direct_identifier = any(
            str(value).strip().casefold() != stable_id
            for value in mentioned
            if str(value).strip()
        )
        return "PRESENT" if has_direct_identifier else "REDACTED"

    # A security audit row is itself retained evidence, even if its subject
    # account is marked deleted. The archive row also exists as a record.
    if raw_state == "DELETED" and category in {
        "SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE"
    }:
        return "PRESENT"

    return STATE_MAP.get(raw_state, raw_state)


def add_once(values, value):
    """Add a non-empty value without case-only duplicates."""
    value = (value or "").strip()
    if value and value.casefold() not in {item.casefold() for item in values}:
        values.append(value)


def deletion_intent(request_text):
    """Return True for clear deletion, False for clear non-deletion, else None."""
    text = (request_text or "").casefold()
    if any(phrase in text for phrase in NEGATED_DELETE_PHRASES):
        return False

    # Account closure is an explicit deletion request even without the word delete.
    if re.search(r"\bclose\b.{0,30}\baccount\b", text):
        return True

    has_delete_word = any(word in text for word in DELETE_WORDS)
    has_delete_target = any(word in text for word in DELETE_TARGET_WORDS)
    if has_delete_word and has_delete_target:
        return True

    if any(phrase in text for phrase in NON_DELETE_PHRASES):
        return False
    return None


def load_identity_maps():
    """Build current and historical email lookups and identity history."""
    identity_rows = read_csv(DATASET_DIR / "user_identities.csv")
    audit_rows = read_csv(DATASET_DIR / "user_audit_log.csv")
    identities = {row["user_id"]: row for row in identity_rows}
    email_to_users = {}
    audit_by_user = {}

    def map_email(email, user_id):
        email = (email or "").strip().casefold()
        if email:
            email_to_users.setdefault(email, set()).add(user_id)

    for row in identity_rows:
        map_email(row["email"], row["user_id"])

    for row in audit_rows:
        user_id = row["user_id"]
        audit_by_user.setdefault(user_id, []).append(row)
        if row["field_name"].strip().casefold() == "email":
            map_email(row["old_value"], user_id)
            map_email(row["new_value"], user_id)

    return identities, audit_by_user, email_to_users


def get_identifiers(user_id, identity, audit_rows, request_email):
    """Collect the stable ID, current fields, and historical identity values."""
    identifiers = []
    add_once(identifiers, user_id)
    for field in IDENTITY_FIELDS:
        add_once(identifiers, identity.get(field, ""))
    add_once(identifiers, request_email)

    for row in audit_rows:
        if row["field_name"].strip().casefold() in IDENTITY_FIELDS:
            add_once(identifiers, row["old_value"])
            add_once(identifiers, row["new_value"])
    return identifiers


def contains_identifier(text, identifiers):
    """Check whether text contains any identifier, ignoring letter case."""
    text = text or ""
    for identifier in identifiers:
        # Boundaries keep one stable identifier from matching a longer one.
        pattern = r"(?<!\w)" + re.escape(identifier) + r"(?!\w)"
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def parse_resource_line(line, audit_at, identifiers):
    """Parse a handover row of the form time | system | resource | type | state | subject."""
    parts = [part.strip().lstrip("-").strip() for part in line.split("|")]
    if len(parts) < 6:
        return None

    try:
        event_time = parse_time(parts[0])
    except ValueError:
        return None
    if event_time > audit_at:
        return None

    # The last field is the subject reference in the supported handover format.
    subject = parts[5]
    if subject.casefold() not in {value.casefold() for value in identifiers}:
        return None

    return {
        "system_id": parts[1],
        "resource_id": parts[2],
        "category": parts[3],
        "state": parts[4].upper(),
        "identifiers_mentioned": [subject],
        "event_time": event_time,
    }


def collect_evidence(case, identifiers, evidence_cache, user_id):
    """Use cached extracted records, keeping the existing evidence_03 reader."""
    audit_at = parse_time(case["audit_at"])
    records = []
    limitations = []

    for evidence in read_csv(DATASET_DIR / "evidence_files.csv"):
        try:
            captured_at = parse_time(evidence["captured_at"])
            if captured_at > audit_at:
                continue
        except ValueError:
            limitations.append(f"{evidence['file_id']} has an invalid capture time")
            continue

        # This handover file has a simple row format with a reliable old reader.
        if evidence["file_id"] == "evidence_03":
            evidence_path = DATASET_DIR / evidence["file_path"]
            try:
                lines = evidence_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                limitations.append("evidence_03 could not be read")
                continue
            for line in lines:
                record = parse_resource_line(line, audit_at, identifiers)
                if record is not None:
                    if record["resource_id"].casefold().startswith("data_flow_"):
                        record["category"] = "DATA_FLOW"
                    record["system_id"] = SYSTEM_ID_BY_CATEGORY.get(
                        record["category"], record["system_id"]
                    )
                    record["raw_state"] = record["state"]
                    record["state"] = normalize_state(
                        record["raw_state"], record["category"], record, user_id
                    )
                    record["evidence_id"] = evidence["file_id"]
                    record["captured_at"] = evidence["captured_at"]
                    records.append(record)
            continue

        evidence_path = DATASET_DIR / evidence["file_path"]
        if evidence_path.suffix.casefold() in {".json", ".sql"}:
            try:
                structured_records = read_structured_file(
                    evidence_path,
                    evidence["file_id"],
                    evidence["captured_at"],
                    identifiers,
                )
            except (OSError, ValueError, UnicodeError, sqlite3.Error) as error:
                limitations.append(
                    f"{evidence['file_id']} could not be read as structured evidence "
                    f"({type(error).__name__})"
                )
                continue

            for structured_record in structured_records:
                try:
                    event_time = parse_time(structured_record["record_time"])
                except ValueError:
                    limitations.append(
                        f"{evidence['file_id']} has an invalid record_time"
                    )
                    continue
                if event_time > audit_at:
                    continue

                category = structured_record["category"]
                raw_state = structured_record["state"]
                records.append({
                    "system_id": SYSTEM_ID_BY_CATEGORY[category],
                    "resource_id": structured_record["resource_id"],
                    "category": category,
                    "linked_resource_ids": structured_record.get(
                        "linked_resource_ids", []
                    ),
                    "raw_state": raw_state,
                    "state": normalize_state(
                        raw_state, category, structured_record, user_id
                    ),
                    "event_time": event_time,
                    "evidence_id": structured_record["evidence_id"],
                    "captured_at": evidence["captured_at"],
                })
            continue

        file_id = evidence["file_id"]
        if file_id not in evidence_cache:
            limitations.append(f"{file_id} is missing from evidence_cache.json")
            continue
        file_records = evidence_cache[file_id]
        if not isinstance(file_records, list):
            limitations.append(f"{file_id} has invalid cached records")
            continue

        for cached_record in file_records:
            if not isinstance(cached_record, dict):
                continue
            # Flagged extractions stay in the cache for review, but cannot
            # create findings, fixes, or verification steps.
            if cached_record.get("flagged") is True:
                continue
            mentioned = cached_record.get("identifiers_mentioned", [])
            if not isinstance(mentioned, list):
                continue
            if not contains_identifier(" ".join(map(str, mentioned)), identifiers):
                continue
            if cached_record.get("resource_id_valid") is False:
                limitations.append(
                    f"{file_id} has a matching record with an invalid resource_id"
                )
                continue

            try:
                event_time = parse_time(
                    cached_record.get("record_time") or evidence["captured_at"]
                )
            except ValueError:
                limitations.append(f"{file_id} has an invalid record_time")
                continue
            if event_time > audit_at:
                continue

            required = ("system_id", "resource_id", "category", "state")
            if not all(cached_record.get(field) for field in required):
                limitations.append(f"{file_id} has an incomplete cached record")
                continue
            raw_state = str(
                cached_record.get("raw_state") or cached_record["state"]
            ).upper()
            category = str(cached_record["category"])
            resource_id = str(cached_record["resource_id"])

            # Extracted flow records were sometimes assigned a data category.
            # Their stable resource prefix identifies them as process records.
            if resource_id.casefold().startswith("data_flow_"):
                category = "DATA_FLOW"
            system_id = SYSTEM_ID_BY_CATEGORY.get(
                category, str(cached_record["system_id"])
            )

            records.append({
                "system_id": system_id,
                "resource_id": resource_id,
                "category": category,
                "raw_state": raw_state,
                "state": normalize_state(
                    raw_state, category, cached_record, user_id
                ),
                "event_time": event_time,
                "evidence_id": file_id,
                "captured_at": evidence["captured_at"],
            })

    # Keep the latest known state for each resource. Conflicts at the same time
    # are unsafe to guess about, so the caller will mark the case unhandled.
    latest = {}
    conflicts = []
    for record in records:
        key = (record["system_id"], record["resource_id"], record["category"])
        previous = latest.get(key)
        if previous is None:
            latest[key] = record
            continue
        old_order = (previous["event_time"], parse_time(previous["captured_at"]))
        new_order = (record["event_time"], parse_time(record["captured_at"]))
        if new_order == old_order and record["state"] != previous["state"]:
            conflicts.append(f"conflicting states for {key[0]}/{key[1]}")
        elif new_order > old_order:
            latest[key] = record

    limitations.extend(conflicts)
    return list(latest.values()), sorted(set(limitations))


def make_step(phase, system_id, resource_id, operation, state, evidence_id=""):
    """Create a step using only the fields allowed by the output contract."""
    return {
        "step_no": 0,
        "phase": phase,
        "system_id": system_id,
        "resource_id": resource_id,
        "operation": operation,
        "state": state,
        "evidence_id": evidence_id,
    }


def build_steps(records, rules):
    """Apply policy actions to supported evidence rows and order the result."""
    findings = []
    remediations = []
    verifications = []
    linked_parent_accounts = set()
    profile_resource_ids = {
        record["resource_id"].casefold()
        for record in records
        if record["category"] == "USER_PROFILE"
    }

    for record in records:
        system_id = record["system_id"]
        resource_id = record["resource_id"]
        category = record["category"]
        state = record["state"]
        evidence_id = record["evidence_id"]
        action = rules.get(category)

        # Preserve explicit child-to-account links for a post-remediation check.
        for linked_id in record.get("linked_resource_ids", []):
            if re.fullmatch(r"account_\d+", str(linked_id), flags=re.IGNORECASE):
                linked_parent_accounts.add(str(linked_id))

        # Stop active flows before changing any records they could recreate.
        if category == "DATA_FLOW":
            if state == "ACTIVE":
                findings.append(make_step(
                    "FINDING", system_id, resource_id,
                    "REPORT_ACTIVE_DATA_FLOW", "ACTIVE", evidence_id
                ))
                remediations.append(make_step(
                    "REMEDIATION", system_id, resource_id,
                    "DISABLE_DATA_FLOW", "DISABLED"
                ))
                verifications.append(make_step(
                    "VERIFICATION", system_id, resource_id,
                    "VERIFY_DATA_FLOW_DISABLED", "DISABLED"
                ))
            elif state == "DISABLED":
                verifications.append(make_step(
                    "VERIFICATION", system_id, resource_id,
                    "VERIFY_DATA_FLOW_DISABLED", "DISABLED", evidence_id
                ))
            continue

        # Retain backup archives but prevent an active restore from recreating data.
        if category == "BACKUP_ARCHIVE":
            if state == "ACTIVE":
                findings.append(make_step(
                    "FINDING", system_id, resource_id,
                    "REPORT_RESTORE_RISK", "ACTIVE", evidence_id
                ))
                remediations.append(make_step(
                    "REMEDIATION", system_id, resource_id,
                    "BLOCK_RESTORE", "BLOCKED"
                ))
                verifications.append(make_step(
                    "VERIFICATION", system_id, resource_id,
                    "VERIFY_RESTORE_BLOCKED", "BLOCKED"
                ))
                verifications.append(make_step(
                    "VERIFICATION", system_id, resource_id,
                    "VERIFY_RECORD_PRESENT", "PRESENT"
                ))
            elif state == "BLOCKED":
                verifications.append(make_step(
                    "VERIFICATION", system_id, resource_id,
                    "VERIFY_RESTORE_BLOCKED", "BLOCKED", evidence_id
                ))
                verifications.append(make_step(
                    "VERIFICATION", system_id, resource_id,
                    "VERIFY_RECORD_PRESENT", "PRESENT", evidence_id
                ))
            elif state == "PRESENT":
                verifications.append(make_step(
                    "VERIFICATION", system_id, resource_id,
                    "VERIFY_RECORD_PRESENT", "PRESENT", evidence_id
                ))
            continue

        if action == "DELETE":
            if state == "ABSENT":
                verifications.append(make_step(
                    "VERIFICATION", system_id, resource_id,
                    "VERIFY_RECORD_ABSENT", "ABSENT", evidence_id
                ))
            else:
                findings.append(make_step(
                    "FINDING", system_id, resource_id,
                    "REPORT_REMAINING_RECORD", "PRESENT", evidence_id
                ))
                remediations.append(make_step(
                    "REMEDIATION", system_id, resource_id,
                    "DELETE_RECORD", "ABSENT"
                ))
                verifications.append(make_step(
                    "VERIFICATION", system_id, resource_id,
                    "VERIFY_RECORD_ABSENT", "ABSENT"
                ))

        elif action == "REDACT":
            if state == "PRESENT":
                findings.append(make_step(
                    "FINDING", system_id, resource_id,
                    "REPORT_EXPOSED_PII", "PRESENT", evidence_id
                ))
                remediations.append(make_step(
                    "REMEDIATION", system_id, resource_id,
                    "REDACT_RECORD", "REDACTED"
                ))
                verifications.append(make_step(
                    "VERIFICATION", system_id, resource_id,
                    "VERIFY_RECORD_REDACTED", "REDACTED"
                ))
            elif state == "REDACTED":
                verifications.append(make_step(
                    "VERIFICATION", system_id, resource_id,
                    "VERIFY_RECORD_REDACTED", "REDACTED", evidence_id
                ))

        elif action == "ANONYMIZE":
            if state == "PRESENT":
                findings.append(make_step(
                    "FINDING", system_id, resource_id,
                    "REPORT_EXPOSED_PII", "PRESENT", evidence_id
                ))
                remediations.append(make_step(
                    "REMEDIATION", system_id, resource_id,
                    "ANONYMIZE_RECORD", "ANONYMIZED"
                ))
                verifications.append(make_step(
                    "VERIFICATION", system_id, resource_id,
                    "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"
                ))
            elif state == "ANONYMIZED":
                verifications.append(make_step(
                    "VERIFICATION", system_id, resource_id,
                    "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED", evidence_id
                ))

        elif action == "RETAIN" and state in {"PRESENT", "REDACTED", "ANONYMIZED"}:
            verifications.append(make_step(
                "VERIFICATION", system_id, resource_id,
                "VERIFY_RECORD_PRESENT", "PRESENT", evidence_id
            ))

    has_findings = bool(findings)
    status = "INCOMPLETE" if has_findings else "COMPLETE"
    if has_findings:
        # If a child explicitly names its parent account, verify that parent
        # after remediation unless a profile record already has its own step.
        for account_id in linked_parent_accounts:
            if account_id.casefold() in profile_resource_ids:
                continue
            verifications.append(make_step(
                "VERIFICATION", SYSTEM_ID_BY_CATEGORY["USER_PROFILE"],
                account_id, "VERIFY_RECORD_ABSENT", "ABSENT"
            ))
        # In an incomplete case these checks are planned for after remediation.
        for step in verifications:
            step["evidence_id"] = ""

    ordered_steps = []
    for phase_steps in (findings, remediations, verifications):
        phase_steps.sort(
            key=lambda step: (
                OPERATION_ORDER[step["phase"]][step["operation"]],
                step["system_id"],
                step["resource_id"],
            )
        )
        ordered_steps.extend(phase_steps)

    for step_number, step in enumerate(ordered_steps, start=1):
        step["step_no"] = step_number
    return status, ordered_steps


def matching_activity_count(case, identifiers):
    """Count related activity claims without treating them as proof of state."""
    audit_at = parse_time(case["audit_at"])
    count = 0
    for row in read_csv(DATASET_DIR / "system_activity.csv"):
        try:
            if parse_time(row["event_time"]) > audit_at:
                continue
        except ValueError:
            continue
        searchable = f"{row.get('resource_id', '')} {row.get('details', '')}"
        if contains_identifier(searchable, identifiers):
            count += 1
    return count


def process_case(
    case, identities, audit_by_user, email_to_users, rules,
    evidence_cache, intent_cache,
):
    """Return one output row and an optional explanation for unhandled.txt."""
    case_id = case["case_id"]
    cached_intent = intent_cache.get(case_id)
    if type(cached_intent) is bool:
        intent = cached_intent
    else:
        # Use the old keyword rule only when the cache has no valid label.
        intent = deletion_intent(case.get("request_text", ""))
    if intent is False:
        return {"case_id": case_id, "deletion_status": "NOT_APPLICABLE", "steps_json": "[]"}, ""
    if intent is None:
        return (
            {"case_id": case_id, "deletion_status": "INCOMPLETE", "steps_json": "[]"},
            "request wording is not covered by the plain keyword rules",
        )

    matches = email_to_users.get(case.get("email", "").strip().casefold(), set())
    if len(matches) != 1:
        reason = "no matching identity" if not matches else "email maps to multiple users"
        return (
            {"case_id": case_id, "deletion_status": "INCOMPLETE", "steps_json": "[]"},
            reason,
        )

    user_id = next(iter(matches))
    identity = identities.get(user_id)
    if identity is None:
        return (
            {"case_id": case_id, "deletion_status": "INCOMPLETE", "steps_json": "[]"},
            f"identity row is missing for {user_id}",
        )

    identifiers = get_identifiers(
        user_id, identity, audit_by_user.get(user_id, []), case.get("email", "")
    )
    records, limitations = collect_evidence(
        case, identifiers, evidence_cache, user_id
    )
    if not records:
        if limitations:
            reason = "; ".join(limitations)
        else:
            reason = "no matching evidence was found"
        activity_count = matching_activity_count(case, identifiers)
        if activity_count and "no matching evidence was found" in reason:
            reason = (
                f"only {activity_count} related activity claim(s) found; "
                "no supported evidence record proves current state"
            )
        return (
            {"case_id": case_id, "deletion_status": "INCOMPLETE", "steps_json": "[]"},
            reason,
        )
    if limitations:
        return (
            {"case_id": case_id, "deletion_status": "INCOMPLETE", "steps_json": "[]"},
            "; ".join(limitations),
        )

    status, steps = build_steps(records, rules)
    return (
        {
            "case_id": case_id,
            "deletion_status": status,
            "steps_json": json.dumps(steps, ensure_ascii=False, separators=(",", ":")),
        },
        "",
    )


def main():
    parser = argparse.ArgumentParser(
        description="Audit deletion requests using deterministic local rules."
    )
    parser.add_argument("input_csv", help="Path to the requests CSV")
    parser.add_argument("output_csv", help="Path for the generated output CSV")
    args = parser.parse_args()

    input_path = Path(args.input_csv).resolve()
    output_path = Path(args.output_csv).resolve()
    if input_path == output_path:
        raise SystemExit("Input and output paths must be different")

    requests = read_csv(input_path)
    identities, audit_by_user, email_to_users = load_identity_maps()
    cache_path = ROOT_DIR / "evidence_cache.json"
    if not cache_path.exists():
        raise SystemExit("evidence_cache.json is missing; run extract_evidence.py first")
    evidence_cache = json.loads(cache_path.read_text(encoding="utf-8"))
    intent_cache_path = ROOT_DIR / "intent_cache.json"
    if intent_cache_path.exists():
        intent_cache = json.loads(intent_cache_path.read_text(encoding="utf-8"))
        if not isinstance(intent_cache, dict):
            raise SystemExit("intent_cache.json must contain a JSON object")
    else:
        intent_cache = {}
    rules = {
        row["data_category"]: row["required_action"]
        for row in read_csv(DATASET_DIR / "deletion_rules.csv")
    }

    output_rows = []
    unhandled = []
    for case in requests:
        output_row, reason = process_case(
            case, identities, audit_by_user, email_to_users, rules,
            evidence_cache, intent_cache,
        )
        output_rows.append(output_row)
        if reason:
            unhandled.append(f"{case['case_id']}\t{reason}")

    # Sort all cases consistently before writing the required three columns.
    output_rows.sort(key=lambda row: row["case_id"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(
            output_file,
            fieldnames=["case_id", "deletion_status", "steps_json"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(output_rows)

    # Keep the current run's reasons beside its output CSV.
    unhandled_path = output_path.with_name("unhandled.txt")
    unhandled_path.write_text(
        "\n".join(unhandled) + ("\n" if unhandled else ""),
        encoding="utf-8",
    )
    print(f"Wrote {len(output_rows)} rows to {output_path}")
    print(f"Wrote {len(unhandled)} unhandled reason(s) to {unhandled_path}")


if __name__ == "__main__":
    main()
