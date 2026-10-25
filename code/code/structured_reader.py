"""Read participant JSON and SQL evidence without using a language model."""

import csv
import copy
from functools import lru_cache
import json
import re
import sqlite3
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DATASET_DIR = ROOT_DIR / "dataset"

# One canonical service name for each policy category.
SYSTEM_ID_BY_CATEGORY = {
    "USER_PROFILE": "account_service",
    "ACCOUNT_TOMBSTONE": "identity_service",
    "SECONDARY_IDENTIFIER": "identity_service",
    "SEARCH_INDEX_ENTRY": "search_service",
    "CACHE_ENTRY": "cache_service",
    "SUPPORT_MESSAGE": "support_platform",
    "SUPPORT_ATTACHMENT": "support_platform",
    "ANALYTICS_PROFILE": "analytics_platform",
    "PRODUCT_TELEMETRY": "analytics_platform",
    "USER_EXPORT": "export_service",
    "MARKETING_PROFILE": "marketing_platform",
    "EMAIL_DELIVERY_LOG": "email_delivery_service",
    "SECURITY_AUDIT_LOG": "security_service",
    "LEGAL_HOLD_RECORD": "legal_service",
    "BACKUP_ARCHIVE": "backup_service",
    "DATA_FLOW": "integration_service",
}

# These source names appear in the supplied JSON snapshots and SQL tables.
SOURCE_CATEGORY = {
    "customer_profiles": "USER_PROFILE",
    "customer_profiles_primary": "USER_PROFILE",
    "customer_profiles_failover_replica": "USER_PROFILE",
    "profile_lookups": "USER_PROFILE",
    "deleted_customer_references": "ACCOUNT_TOMBSTONE",
    "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "search_documents_primary": "SEARCH_INDEX_ENTRY",
    "customer_analytics_reports": "ANALYTICS_PROFILE",
    "customer_analytics_profiles": "ANALYTICS_PROFILE",
    "profiles": "ANALYTICS_PROFILE",
    "backup_catalog": "BACKUP_ARCHIVE",
    "backup_restore_entries": "BACKUP_ARCHIVE",
    "restore_entries": "BACKUP_ARCHIVE",
    "customer_export_jobs": "USER_EXPORT",
    "customer_exports": "USER_EXPORT",
    "exports": "USER_EXPORT",
    "connector_configurations": "DATA_FLOW",
    "connector_subscriptions": "DATA_FLOW",
    "connectors": "DATA_FLOW",
    "index_queries": "SEARCH_INDEX_ENTRY",
    "security_audit_events": "SECURITY_AUDIT_LOG",
    "security_audit_records": "SECURITY_AUDIT_LOG",
    "audit_records": "SECURITY_AUDIT_LOG",
    "support_conversations": "SUPPORT_MESSAGE",
    "support_records": "SUPPORT_MESSAGE",
    "tickets": "SUPPORT_MESSAGE",
    "ticket_attachments": "SUPPORT_ATTACHMENT",
    "retained_order_records": "LEGAL_HOLD_RECORD",
    "operational_record_history": "",
}

IDENTITY_KEYS = {
    "userid", "customerreference", "subjectreference", "subjectselector",
    "customerid", "accountid", "accountreference", "email", "customeremail",
    "owneremail", "requesteremail", "phone", "customerphone", "ownerphone",
    "requesterphone", "emailhash", "fullname", "name", "displayname",
    "requestername", "lookupvalue", "uploadedby",
}
RESOURCE_LINK_KEYS = {
    "sourceresourceid", "accountid", "accountresourceid", "linkedresourceid",
    "relatedresourceid", "primaryresourceid",
}
TEXT_KEYS = {
    "messagetext", "messageexcerpt", "description", "details", "notes",
    "lookupvalue", "query",
}
TIME_KEYS = (
    "record_time", "recorded_at", "observed_at", "updated_at", "created_at",
    "indexed_at", "event_time", "captured_at", "timestamp",
)
ALLOWED_STATES = {
    "PRESENT", "ACTIVE", "DISABLED", "BLOCKED", "ABSENT", "REDACTED",
    "ANONYMIZED",
}
STATE_ALIASES = {
    "FOUND": "PRESENT",
    "IDENTIFIABLE": "PRESENT",
    "NOT_FOUND": "ABSENT",
    "DELETED": "ABSENT",
    "DELETION_PENDING": "PRESENT",
}


def _key_name(value):
    """Compare source field names without punctuation or case differences."""
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _walk_fields(value):
    """Yield nested JSON key/value pairs, including objects inside arrays."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from _walk_fields(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_fields(child)


def _flatten(value):
    """Return all nested scalar fields as (key, value) pairs."""
    return list(_walk_fields(value))


def _clean_identifier(value):
    if value is None or isinstance(value, (dict, list)):
        return ""
    text = str(value).strip()
    if text.casefold() in {"", "none", "null", "[redacted]", "redacted"}:
        return ""
    return text


def _truthy_flag(value):
    if value is True or value == 1:
        return True
    return isinstance(value, str) and value.strip().casefold() in {
        "true", "yes", "1", "redacted", "anonymized", "anonymised",
    }


def _tombstone_has_direct_identifiers(record):
    direct_keys = {
        "fullname", "name", "email", "customeremail", "phone",
        "customerphone", "emailhash", "displayname", "requestername",
    }
    return any(
        _key_name(key) in direct_keys and _clean_identifier(value)
        for key, value in _flatten(record)
    )


def _source_category(value):
    """Return a policy category when a source label is recognized."""
    if not value:
        return ""
    text = str(value).strip()
    if text.upper() in SYSTEM_ID_BY_CATEGORY:
        return text.upper()
    wanted = _key_name(text)
    for source_name, category in SOURCE_CATEGORY.items():
        if _key_name(source_name) == wanted:
            return category
    return ""


@lru_cache(maxsize=1)
def _allowed_categories():
    path = DATASET_DIR / "deletion_rules.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        # DATA_FLOW is a process category in the output contract, even though
        # it is represented by process-specific rules rather than a row here.
        rule_categories = {row["data_category"] for row in csv.DictReader(source)}
        return rule_categories | {"DATA_FLOW"}


def _category_for(record, hints, allowed_categories):
    """Resolve category from explicit source fields, then a known source name."""
    for key in ("data_category", "category", "record_type", "record_category"):
        category = _source_category(record.get(key))
        if category in allowed_categories:
            return category
    for hint in hints:
        category = _source_category(hint)
        if category in allowed_categories:
            return category
    resource_id = str(record.get("resource_id", "")).casefold()
    for prefix, category in (
        ("account_", "USER_PROFILE"),
        ("search_", "SEARCH_INDEX_ENTRY"),
        ("analytics_", "ANALYTICS_PROFILE"),
        ("backup_", "BACKUP_ARCHIVE"),
        ("data_flow_", "DATA_FLOW"),
        ("export_", "USER_EXPORT"),
        ("support_", "SUPPORT_MESSAGE"),
        ("audit_record_", "SECURITY_AUDIT_LOG"),
    ):
        if resource_id.startswith(prefix):
            return category
    return ""


def _derive_state(record, category):
    """Normalize only explicit fields; unknown records default to PRESENT."""
    fields = [(_key_name(key), value) for key, value in _flatten(record)]

    # A missing profile and an HTTP 404 explicitly establish absence.
    if any(key == "profile" and value is None for key, value in fields):
        return "ABSENT"
    if any(key == "statuscode" and str(value) == "404" for key, value in fields):
        return "ABSENT"

    # Explicit privacy-treatment flags have precedence over general status.
    for key, value in fields:
        if "redact" in key and _truthy_flag(value):
            return "REDACTED"
    for key, value in fields:
        if "anonym" in key and _truthy_flag(value):
            return "ANONYMIZED"

    # These booleans describe live processes, not ordinary customer records.
    for key, value in fields:
        if key == "enabled" and category == "DATA_FLOW" and isinstance(value, (bool, int)):
            return "ACTIVE" if bool(value) else "DISABLED"
        if key == "restoreallowed" and category == "BACKUP_ARCHIVE" and isinstance(value, (bool, int)):
            return "ACTIVE" if bool(value) else "BLOCKED"

    # A deleted profile is absent; a pending deletion still exists.
    for key, value in fields:
        if key == "accountstatus":
            status = str(value).strip().upper()
            if category == "ACCOUNT_TOMBSTONE" and status == "DELETED":
                return (
                    "PRESENT" if _tombstone_has_direct_identifiers(record)
                    else "REDACTED"
                )
            if status == "DELETED":
                return "ABSENT"
            if status in {"ACTIVE", "DELETION_PENDING"}:
                return "PRESENT"

    for key, value in fields:
        if key not in {"state", "result", "status"}:
            continue
        status = str(value).strip().upper()
        if status in ALLOWED_STATES:
            if status == "ACTIVE" and category not in {"DATA_FLOW", "BACKUP_ARCHIVE"}:
                return "PRESENT"
            return status
        if status in STATE_ALIASES:
            if status == "DELETED" and category == "ACCOUNT_TOMBSTONE":
                return (
                    "PRESENT" if _tombstone_has_direct_identifiers(record)
                    else "REDACTED"
                )
            if status == "DELETED" and category in {
                "SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE",
            }:
                return "PRESENT"
            return STATE_ALIASES[status]

    return "PRESENT"


def _collect_identifiers(record):
    """Collect values from fields that explicitly refer to a person."""
    values = []
    for key, value in _flatten(record):
        if _key_name(key) not in IDENTITY_KEYS:
            continue
        cleaned = _clean_identifier(value)
        if cleaned and cleaned.casefold() not in {item.casefold() for item in values}:
            values.append(cleaned)
    return values


def _collect_resource_links(record):
    values = []
    for key, value in _flatten(record):
        if _key_name(key) not in RESOURCE_LINK_KEYS:
            continue
        cleaned = _clean_identifier(value)
        if cleaned:
            values.append(cleaned)
    return values


def _find_target_mentions(record, target_identifiers):
    """Find exact subject identifiers inside designated note or query fields."""
    if not target_identifiers:
        return []
    mentions = []
    for key, value in _flatten(record):
        if _key_name(key) not in TEXT_KEYS or not isinstance(value, str):
            continue
        for target in target_identifiers:
            target = _clean_identifier(target)
            if not target:
                continue
            pattern = r"(?<!\w)" + re.escape(target) + r"(?!\w)"
            if re.search(pattern, value, flags=re.IGNORECASE):
                mentions.append(target)
    return mentions


def _matches_target(identifiers, target_identifiers):
    targets = {str(value).strip().casefold() for value in target_identifiers if str(value).strip()}
    return any(str(value).strip().casefold() in targets for value in identifiers)


def _record_time(record, fallback_time):
    fields = dict((_key_name(key), value) for key, value in _flatten(record))
    for name in TIME_KEYS:
        value = fields.get(_key_name(name))
        if value:
            return str(value)
    return str(fallback_time or "")


def _candidate(record, evidence_id, hints, fallback_time, allowed_categories):
    resource_id = _clean_identifier(record.get("resource_id"))
    category = _category_for(record, hints, allowed_categories)
    if not resource_id or category not in allowed_categories:
        return None
    identifiers = _collect_identifiers(record)
    return {
        "system_id": SYSTEM_ID_BY_CATEGORY.get(category, ""),
        "resource_id": resource_id,
        "category": category,
        "state": _derive_state(record, category),
        "identifiers_mentioned": identifiers,
        "record_time": _record_time(record, fallback_time),
        "evidence_id": evidence_id,
        "_resource_links": _collect_resource_links(record),
        "_source_record": record,
    }


def _json_candidates(document, evidence_id, fallback_time, allowed_categories):
    """Flatten the supplied JSON shapes into individual resource candidates."""
    output = []
    file_time = (
        document.get("captured_at") or document.get("exported_at")
        or document.get("recorded_at") or fallback_time
    )

    if isinstance(document.get("snapshots"), list):
        for snapshot in document["snapshots"]:
            collection = snapshot.get("collection", "")
            data_store = snapshot.get("data_store", "")
            snapshot_time = snapshot.get("captured_at") or file_time
            for row in snapshot.get("records", []):
                if isinstance(row, dict):
                    output.append(_candidate(
                        row, evidence_id, (collection, data_store), snapshot_time,
                        allowed_categories,
                    ))

    elif isinstance(document.get("service_exports"), dict):
        for service, groups in document["service_exports"].items():
            for group, rows in groups.items():
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    # Search exports nest actual documents under hits.documents.
                    if group == "index_queries":
                        hit_docs = row.get("hits", {}).get("documents", [])
                        for hit in hit_docs:
                            if isinstance(hit, dict):
                                merged = dict(row)
                                merged.update(hit)
                                output.append(_candidate(
                                    merged, evidence_id, (group, service),
                                    row.get("observed_at") or file_time,
                                    allowed_categories,
                                ))
                    else:
                        output.append(_candidate(
                            row, evidence_id, (group, service), file_time,
                            allowed_categories,
                        ))

    elif isinstance(document.get("records"), list):
        for row in document["records"]:
            if isinstance(row, dict):
                output.append(_candidate(
                    row, evidence_id, (document.get("collection", ""),),
                    file_time, allowed_categories,
                ))

    # Keep only recognized, user-linked records with canonical systems.
    return [row for row in output if row and row["system_id"]]


def _sql_candidates(path, evidence_id, fallback_time, allowed_categories):
    """Load a SQL snapshot into SQLite memory, then normalize its rows."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(path.read_text(encoding="utf-8", errors="replace"))
        tables = [
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        snapshot_time = fallback_time
        if "snapshot_scope" in tables:
            scope = connection.execute("SELECT * FROM snapshot_scope LIMIT 1").fetchone()
            if scope and "captured_at" in scope.keys():
                snapshot_time = scope["captured_at"] or snapshot_time

        output = []
        for table in tables:
            if table == "snapshot_scope":
                continue
            for row in connection.execute(f'SELECT * FROM "{table}"'):
                source_row = dict(row)
                record = source_row
                if source_row.get("record_json"):
                    try:
                        record = json.loads(source_row["record_json"])
                    except (TypeError, json.JSONDecodeError):
                        continue
                    # The SQL envelope supplies resource type and row timestamp.
                    for field in ("resource_id", "origin_system", "collection_name", "updated_at"):
                        if source_row.get(field) and not record.get(field):
                            record[field] = source_row[field]
                output.append(_candidate(
                    record, evidence_id,
                    (source_row.get("collection_name", ""), table,
                     source_row.get("origin_system", "")),
                    source_row.get("updated_at") or snapshot_time,
                    allowed_categories,
                ))
        return [row for row in output if row and row["system_id"]]
    finally:
        connection.close()


def _select_user_records(candidates, target_identifiers):
    """Keep direct identity matches and records explicitly linked to them."""
    if target_identifiers is None:
        selected = {i for i, row in enumerate(candidates) if row["identifiers_mentioned"]}
    else:
        selected = set()
        for i, row in enumerate(candidates):
            mentions = _find_target_mentions(row["_source_record"], target_identifiers)
            for value in mentions:
                if value.casefold() not in {v.casefold() for v in row["identifiers_mentioned"]}:
                    row["identifiers_mentioned"].append(value)
            if _matches_target(row["identifiers_mentioned"], target_identifiers):
                selected.add(i)

    linked_resources = set()
    for i in selected:
        row = candidates[i]
        linked_resources.add(row["resource_id"].casefold())
        linked_resources.update(value.casefold() for value in row["_resource_links"])
        for value in row["_resource_links"]:
            if value.casefold() not in {
                item.casefold() for item in row["identifiers_mentioned"]
            }:
                row["identifiers_mentioned"].append(value)

    # Follow only explicit resource-to-resource references, such as a search
    # document's source_resource_id pointing at an identified account record.
    changed = True
    while changed:
        changed = False
        for i, row in enumerate(candidates):
            if i in selected:
                continue
            references = {value.casefold() for value in row["_resource_links"]}
            if row["resource_id"].casefold() in linked_resources or references & linked_resources:
                selected.add(i)
                linked_resources.add(row["resource_id"].casefold())
                linked_resources.update(references)
                for value in references:
                    if value not in {v.casefold() for v in row["identifiers_mentioned"]}:
                        row["identifiers_mentioned"].append(value)
                changed = True

    public_fields = (
        "system_id", "resource_id", "category", "state",
        "identifiers_mentioned", "linked_resource_ids", "record_time",
        "evidence_id",
    )
    return [
        {
            **{field: candidates[i][field] for field in public_fields if field != "linked_resource_ids"},
            "linked_resource_ids": list(candidates[i]["_resource_links"]),
        }
        for i in sorted(selected)
    ]


def read_structured_file(path, evidence_id, captured_at, target_identifiers=None):
    """Read one JSON/SQL evidence file and return records linked to a user."""
    path = Path(path)
    candidates = copy.deepcopy(
        _load_candidates(str(path.resolve()), evidence_id, captured_at)
    )
    return _select_user_records(candidates, target_identifiers)


@lru_cache(maxsize=128)
def _load_candidates(path_text, evidence_id, captured_at):
    """Parse each source file once; case-specific matching happens afterward."""
    path = Path(path_text)
    allowed_categories = _allowed_categories()
    if path.suffix.casefold() == ".json":
        document = json.loads(path.read_text(encoding="utf-8"))
        candidates = _json_candidates(
            document, evidence_id, captured_at, allowed_categories
        )
    elif path.suffix.casefold() == ".sql":
        candidates = _sql_candidates(
            path, evidence_id, captured_at, allowed_categories
        )
    else:
        return []
    return candidates
