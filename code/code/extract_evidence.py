"""Extract structured resource records from the participant evidence files."""

import argparse
import csv
import difflib
import json
import mimetypes
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from google import genai
from google.genai import types


ROOT_DIR = Path(__file__).resolve().parents[1]
DATASET_DIR = ROOT_DIR / "dataset"
CACHE_PATH = ROOT_DIR / "evidence_cache.json"
USAGE_PATH = ROOT_DIR / "usage_log.csv"
ERRORS_PATH = ROOT_DIR / "extraction_errors.csv"
FAILED_REPLY_DIR = ROOT_DIR / "failed_replies"
CALL_COUNTER_PATH = ROOT_DIR / "daily_call_counter.json"

# Keep this setting in sync with MODEL in test_gemini.py.
MODEL = "gemini-3.5-flash-lite"
MAX_ATTEMPTS = 2
RETRY_WAIT_SECONDS = 2
MAX_DAILY_CALLS = 450
CALL_PAUSE_SECONDS = 6
MAX_CONSECUTIVE_FAILURES = 5
TEXT_EXTENSIONS = {".json", ".log", ".sql", ".txt"}
MEDIA_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
RECORD_FIELDS = {
    "system_id", "resource_id", "category", "state", "state_basis",
    "identifiers_mentioned", "record_time",
}
REQUIRED_RECORD_FIELDS = RECORD_FIELDS - {"state_basis"}
ALLOWED_STATES = {
    "PRESENT", "ACTIVE", "DISABLED", "BLOCKED", "ABSENT", "REDACTED",
    "ANONYMIZED",
}
USAGE_FIELDS = [
    "file_id", "attempt", "model", "input_tokens", "output_tokens", "outcome",
]
ERROR_FIELDS = ["file_id", "attempt", "error_type", "reason"]


def read_api_key():
    """Load the API key from .env without displaying or storing it elsewhere."""
    env_path = ROOT_DIR / ".env"
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if separator and name.strip() == "GEMINI_API_KEY":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                return value
    raise ValueError("GEMINI_API_KEY is missing from .env")


def read_csv(path):
    """Read a UTF-8 CSV file as dictionaries."""
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def load_cache():
    """Load completed file results, or return an empty cache."""
    if not CACHE_PATH.exists():
        return {}
    value = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("evidence_cache.json must contain an object")
    return value


def save_cache(cache):
    """Write the cache atomically so an interrupted run keeps its progress."""
    temporary_path = CACHE_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(CACHE_PATH)


def append_usage(file_id, attempt, input_tokens, output_tokens, outcome):
    """Append token counts for one API attempt without recording request data."""
    has_file = USAGE_PATH.exists()
    with USAGE_PATH.open("a", encoding="utf-8", newline="") as usage_file:
        writer = csv.DictWriter(
            usage_file,
            fieldnames=USAGE_FIELDS,
            lineterminator="\n",
        )
        if not has_file:
            writer.writeheader()
        writer.writerow({
            "file_id": file_id,
            "attempt": attempt,
            "model": MODEL,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "outcome": outcome,
        })


def sanitize_text(value, api_key="", limit=300):
    """Remove credentials from a message before saving or displaying it."""
    message = str(value)
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    message = re.sub(r"\bAIza[0-9A-Za-z_-]{20,}\b", "[REDACTED]", message)
    message = re.sub(
        r"(?i)(api[_-]?key|x-goog-api-key)(\s*[:=]\s*)[^\s,;\]}]+",
        r"\1\2[REDACTED]",
        message,
    )
    message = re.sub(r"(?i)Bearer\s+[^\s,;\]}]+", "Bearer [REDACTED]", message)
    return message[:limit]


def append_extraction_error(file_id, attempt, error_type, reason, api_key=""):
    """Save a short, credential-scrubbed explanation of one failed response."""
    has_file = ERRORS_PATH.exists()
    with ERRORS_PATH.open("a", encoding="utf-8", newline="") as errors_file:
        writer = csv.DictWriter(
            errors_file, fieldnames=ERROR_FIELDS, lineterminator="\n"
        )
        if not has_file:
            writer.writeheader()
        writer.writerow({
            "file_id": file_id,
            "attempt": attempt,
            "error_type": error_type,
            "reason": sanitize_text(reason, api_key),
        })


def save_failed_reply(file_id, reply_text, api_key=""):
    """Keep the final failed reply for diagnosis, scrubbing any credential."""
    if reply_text is None:
        return
    FAILED_REPLY_DIR.mkdir(parents=True, exist_ok=True)
    reply_path = FAILED_REPLY_DIR / f"{file_id}.txt"
    reply_path.write_text(
        sanitize_text(reply_text, api_key, limit=None), encoding="utf-8"
    )


def clear_failed_reply(file_id):
    """Remove a stale reply if a later attempt successfully caches the file."""
    reply_path = FAILED_REPLY_DIR / f"{file_id}.txt"
    if reply_path.exists():
        reply_path.unlink()


def ensure_usage_log_schema():
    """Add any missing usage columns without losing the existing call history."""
    if not USAGE_PATH.exists():
        return

    with USAGE_PATH.open("r", encoding="utf-8-sig", newline="") as usage_file:
        reader = csv.DictReader(usage_file)
        old_fields = reader.fieldnames or []
        if all(field in old_fields for field in USAGE_FIELDS):
            return
        old_rows = list(reader)

    temporary_path = USAGE_PATH.with_suffix(".csv.tmp")
    with temporary_path.open("w", encoding="utf-8", newline="") as usage_file:
        writer = csv.DictWriter(
            usage_file, fieldnames=USAGE_FIELDS, lineterminator="\n"
        )
        writer.writeheader()
        for row in old_rows:
            writer.writerow({field: row.get(field, "") for field in USAGE_FIELDS})
    temporary_path.replace(USAGE_PATH)


def utc_today():
    """Return today's date in UTC for the daily API-call limit."""
    return datetime.now(timezone.utc).date().isoformat()


def usage_row_count():
    """Count previous calls when creating a counter for the first time."""
    if not USAGE_PATH.exists():
        return 0
    with USAGE_PATH.open("r", encoding="utf-8-sig", newline="") as usage_file:
        return max(0, sum(1 for _ in csv.DictReader(usage_file)))


def save_daily_call_count(calls):
    """Save the call counter atomically."""
    temporary_path = CALL_COUNTER_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps({"date": utc_today(), "calls": calls}, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(CALL_COUNTER_PATH)


def load_daily_call_count():
    """Read today's count, conservatively seeding it from the old usage log."""
    today = utc_today()
    if CALL_COUNTER_PATH.exists():
        counter = json.loads(CALL_COUNTER_PATH.read_text(encoding="utf-8"))
        if counter.get("date") == today:
            return int(counter.get("calls", 0))
        # The quota window is a new UTC day.
        save_daily_call_count(0)
        return 0

    # The existing log has no dates, so count all rows to avoid undercounting.
    calls = usage_row_count()
    save_daily_call_count(calls)
    return calls


def reserve_api_call():
    """Count a call before sending it and refuse calls after the daily ceiling."""
    calls = load_daily_call_count()
    if calls >= MAX_DAILY_CALLS:
        return False, calls
    calls += 1
    save_daily_call_count(calls)
    return True, calls


def quota_details(error):
    """Extract a quota name and classify it as per-day or per-minute."""
    message = str(error)
    named_quotas = re.findall(
        r"[\"']?(?:quotaId|quotaMetric|quotaName|quota_id|quota_metric|quota_name)"
        r"[\"']?\s*[:=]\s*[\"']([^\"']+)",
        message,
        flags=re.IGNORECASE,
    )
    period_names = re.findall(
        r"[A-Za-z0-9_.-]*(?:per[\s_-]*day|per[\s_-]*minute)[A-Za-z0-9_.-]*",
        message,
        flags=re.IGNORECASE,
    )
    for name in named_quotas + period_names:
        normalized = re.sub(r"[^a-z]", "", name.casefold())
        if "perday" in normalized:
            return "day", name
        if "perminute" in normalized:
            return "minute", name

    if re.search(r"per[\s_-]*day", message, flags=re.IGNORECASE):
        return "day", named_quotas[0] if named_quotas else "per-day quota"
    if re.search(r"per[\s_-]*minute", message, flags=re.IGNORECASE):
        return "minute", named_quotas[0] if named_quotas else "per-minute quota"
    return None, named_quotas[0] if named_quotas else "unclassified quota"


def retry_delay_seconds(error):
    """Read the requested retry delay from an API error message."""
    message = str(error)
    patterns = (
        r"retry(?:Delay|_delay| delay)\s*[\"']?\s*[:=]\s*[\"']?"
        r"(\d+(?:\.\d+)?)\s*(ms|s|sec|seconds)?",
        r"retry\s+in\s+(\d+(?:\.\d+)?)\s*(ms|s|sec|seconds)",
    )
    for pattern in patterns:
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if match:
            seconds = float(match.group(1))
            unit = (match.group(2) or "s").casefold()
            return seconds / 1000 if unit == "ms" else seconds
    return 0


def is_rate_limit_error(error):
    """Recognize 429 errors without printing their raw messages."""
    code = getattr(error, "code", None)
    return str(code) == "429" or bool(re.search(r"\b429\b", str(error)))


def token_counts(response):
    """Return reported input/output token counts, blank when unavailable."""
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return "", ""
    return (
        getattr(usage, "prompt_token_count", ""),
        getattr(usage, "candidates_token_count", ""),
    )


def is_valid_time(value):
    """Accept an empty time or a parseable ISO timestamp."""
    if value == "":
        return True
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None
    except (TypeError, ValueError):
        return False


def map_category(value, categories):
    """Map exact or unmistakably close category spellings to policy names."""
    if value in categories:
        return value

    normalize = lambda item: re.sub(r"[^a-z0-9]", "", item.casefold())
    wanted = normalize(value)
    normalized_categories = {normalize(item): item for item in categories}
    if wanted in normalized_categories:
        return normalized_categories[wanted]

    scored = sorted(
        [
            (
                difflib.SequenceMatcher(None, wanted, normalize(category)).ratio(),
                category,
            )
            for category in categories
        ],
        reverse=True,
    )
    if scored and scored[0][0] >= 0.92:
        if len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05:
            return scored[0][1]
    raise ValueError("category does not match an allowed policy category")


def parse_json_array(reply_text):
    """Find the best JSON array inside fences or surrounding explanatory text."""
    text = str(reply_text).lstrip("\ufeff").strip()
    # Remove Markdown fence markers while leaving their enclosed JSON unchanged.
    text = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE)
    text = text.replace("```", "")

    decoder = json.JSONDecoder()
    candidates = []
    for position, character in enumerate(text):
        if character not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            continue

        if isinstance(value, list):
            # Prefer arrays that look like record arrays over nested value arrays.
            record_count = sum(isinstance(item, dict) for item in value)
            candidates.append((record_count, len(value), -position, value))
        elif isinstance(value, dict):
            for key in ("records", "items", "resources", "data"):
                nested = value.get(key)
                if isinstance(nested, list):
                    record_count = sum(isinstance(item, dict) for item in nested)
                    candidates.append((record_count, len(nested), -position, nested))

    if not candidates:
        raise ValueError("no JSON array found in reply")
    return max(candidates, key=lambda item: item[:3])[3]


def validate_records(reply_text, categories, source_text=None):
    """Keep valid array entries and return issues for invalid individual entries."""
    records = parse_json_array(reply_text)

    checked = []
    issues = []
    for index, original in enumerate(records, start=1):
        try:
            if not isinstance(original, dict):
                raise ValueError("record must be a JSON object")
            fields = set(original)
            if not REQUIRED_RECORD_FIELDS.issubset(fields) or not fields.issubset(
                RECORD_FIELDS
            ):
                raise ValueError("record has missing or unexpected fields")
            for field in ("system_id", "resource_id", "category", "state", "record_time"):
                if not isinstance(original[field], str):
                    raise ValueError(f"{field} must be a string")
            if not original["system_id"] or not original["resource_id"]:
                raise ValueError("system_id and resource_id must not be empty")
            if not isinstance(original["identifiers_mentioned"], list) or not all(
                isinstance(item, str) for item in original["identifiers_mentioned"]
            ):
                raise ValueError("identifiers_mentioned must be a list of strings")
            if "state_basis" in original and not isinstance(
                original["state_basis"], str
            ):
                raise ValueError("state_basis must be a string")
            if not is_valid_time(original["record_time"]):
                raise ValueError("record_time is not an ISO timestamp")

            record = dict(original)
            record["category"] = map_category(record["category"], categories)

            # An existing resource with no stated condition is present by
            # default. Mark this as inferred so it is distinct from source text.
            state = record["state"].strip().upper()
            basis = record.get("state_basis", "").strip().casefold()
            if not state:
                state = "PRESENT"
                basis = "inferred"
            elif state not in ALLOWED_STATES:
                raise ValueError("state is not one of the allowed operation states")
            elif not basis:
                # Older saved replies do not have state_basis. Treat their
                # origin as uncertain instead of claiming an explicit state.
                basis = "inferred"

            if basis not in {"explicit", "inferred"}:
                raise ValueError("state_basis must be explicit or inferred")
            record["state"] = state
            record["state_basis"] = basis
            record["raw_state"] = state

            if source_text is not None:
                # Whole-token matching avoids accepting a short ID inside a longer one.
                resource_id = record["resource_id"].strip()
                is_placeholder = resource_id.casefold() in {
                    "", "-", "unknown", "none", "null", "n/a", "na"
                }
                pattern = r"(?<!\w)" + re.escape(resource_id) + r"(?!\w)"
                record["resource_id_valid"] = bool(
                    not is_placeholder
                    and re.search(pattern, source_text, re.IGNORECASE)
                )
                if not record["resource_id_valid"]:
                    record["flagged"] = True
                    if is_placeholder:
                        record["flag_reason"] = (
                            "placeholder resource_id; lookup-start-only evidence "
                            "does not prove a resource state"
                        )
                    else:
                        record["flag_reason"] = (
                            "resource_id not found in source text"
                        )
                    record["validation_error"] = record["flag_reason"]
            checked.append(record)
        except ValueError as error:
            issues.append((index, str(error)))

    return checked, issues, len(records)


def make_prompt(file_id, categories):
    """Ask for a JSON array using only the categories defined by policy."""
    category_list = ", ".join(categories)
    return f"""Extract only records that mention a person through an email, phone, name, user_id, or hash.
Return compact JSON only: an array, with no markdown or explanation. Use [] if no matching records exist.
Each record must contain exactly: system_id, resource_id, category, state,
state_basis, identifiers_mentioned, record_time. Use strings for all fields except
identifiers_mentioned, which is an array of strings.
Use category exactly from: {category_list}. Copy values from the evidence; do not invent them.
Use the record's event or snapshot time, or an empty string when no time is shown.
Use only these state values: PRESENT, ACTIVE, DISABLED, BLOCKED, ABSENT, REDACTED, ANONYMIZED.
Set state_basis to exactly "explicit" when the evidence directly states a condition that maps
to one of those values. Set it to exactly "inferred" when applying the default below.
For a resource record that appears in the evidence but has no explicit state, use
state="PRESENT" and state_basis="inferred". Map a clear explicit absence, redaction,
anonymization, active/disabled flow, or blocked/allowed restore condition only when the
evidence supports the corresponding allowed state. Do not output FOUND, NOT_FOUND,
IDENTIFIABLE, or other state names. Omit a record if its state cannot be grounded.
Evidence file: {file_id}."""


def extract_one(client, evidence, categories, cache, api_key):
    """Read one source once, retry twice at most, and cache usable records."""
    evidence_path = DATASET_DIR / evidence["file_path"]
    extension = evidence_path.suffix.casefold()
    source_text = None

    # Read each source file once; retrying reuses these contents.
    if extension in TEXT_EXTENSIONS:
        source_text = evidence_path.read_text(encoding="utf-8", errors="replace")
        contents = make_prompt(evidence["file_id"], categories)
        contents += "\n\nEvidence text follows:\n" + source_text
    elif extension in MEDIA_EXTENSIONS:
        source_bytes = evidence_path.read_bytes()
        mime_type = mimetypes.guess_type(evidence_path.name)[0]
        if not mime_type:
            raise ValueError("cannot determine media type")
        contents = [
            make_prompt(evidence["file_id"], categories),
            types.Part.from_bytes(data=source_bytes, mime_type=mime_type),
        ]
    else:
        raise ValueError("unsupported evidence file extension")

    last_reply = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if getattr(extract_one, "called_api", False):
            time.sleep(CALL_PAUSE_SECONDS)

        allowed, call_count = reserve_api_call()
        if not allowed:
            save_cache(cache)
            save_failed_reply(evidence["file_id"], last_reply, api_key)
            print(
                f"Stopped: the daily call limit is {MAX_DAILY_CALLS}; "
                f"the counter is already at {call_count}."
            )
            return "stop"
        extract_one.called_api = True

        response = None
        reply_text = None
        input_tokens = ""
        output_tokens = ""
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                ),
            )
            input_tokens, output_tokens = token_counts(response)
            reply_text = response.text or ""
            last_reply = reply_text
            records, issues, record_count = validate_records(
                reply_text, categories, source_text
            )

            # Log bad individual records, but preserve every valid record.
            for index, reason in issues:
                append_extraction_error(
                    evidence["file_id"],
                    attempt,
                    "RecordValidationError",
                    f"record {index} dropped: {reason}",
                    api_key,
                )
        except Exception as error:
            code = getattr(error, "code", None)
            outcome = f"error_{code}" if code else f"error_{type(error).__name__}"
            if response is not None and not input_tokens and not output_tokens:
                input_tokens, output_tokens = token_counts(response)
            if response is not None and reply_text is None:
                try:
                    last_reply = response.text
                except Exception:
                    pass
            append_usage(
                evidence["file_id"], attempt, input_tokens, output_tokens, outcome
            )
            append_extraction_error(
                evidence["file_id"], attempt, type(error).__name__, str(error), api_key
            )

            if is_rate_limit_error(error):
                period, quota_name = quota_details(error)
                quota_name = sanitize_text(quota_name, api_key)
                if period == "day":
                    save_cache(cache)
                    save_failed_reply(evidence["file_id"], last_reply, api_key)
                    print(
                        f"Stopped: Gemini daily quota reached ({quota_name}). "
                        "The cache has been saved; no retry was made."
                    )
                    return "stop"
                if period == "minute":
                    if attempt >= MAX_ATTEMPTS:
                        save_cache(cache)
                        save_failed_reply(evidence["file_id"], last_reply, api_key)
                        print(
                            f"Stopped: per-minute quota retries were exhausted "
                            f"for {quota_name}. The cache has been saved."
                        )
                        return "stop"
                    wait_seconds = retry_delay_seconds(error) + 5
                    print(
                        f"Per-minute quota ({quota_name}); retrying in "
                        f"{wait_seconds:g} seconds."
                    )
                    time.sleep(wait_seconds)
                    continue

                save_cache(cache)
                save_failed_reply(evidence["file_id"], last_reply, api_key)
                print(
                    "Stopped: received a 429 response but could not identify "
                    f"its quota period ({quota_name}). The cache has been saved."
                )
                return "stop"

            if attempt >= MAX_ATTEMPTS:
                save_failed_reply(evidence["file_id"], last_reply, api_key)
                print(
                    f"Failed {evidence['file_id']} after {attempt} attempts "
                    f"({outcome})."
                )
                return "failed"
            time.sleep(RETRY_WAIT_SECONDS * attempt)
            continue

        outcome = "ok_with_dropped_records" if issues else "ok"
        append_usage(evidence["file_id"], attempt, input_tokens, output_tokens, outcome)
        cache[evidence["file_id"]] = records
        save_cache(cache)
        clear_failed_reply(evidence["file_id"])
        print(
            f"Cached {evidence['file_id']}: {len(records)}/{record_count} "
            "valid record(s); "
            f"dropped {len(issues)} invalid record(s)."
        )
        return "cached"

    return "failed"


def print_file_progress(cache, evidence_rows):
    """Show cache progress against the manifest."""
    total = len(evidence_rows)
    cached = sum(1 for row in evidence_rows if row["file_id"] in cache)
    print(f"Files cached: {cached}/{total}; remaining: {total - cached}.")


def main(argv=None):
    """Run extraction for all uncached files or one chosen manifest file."""
    parser = argparse.ArgumentParser(description="Extract evidence records.")
    parser.add_argument(
        "--only",
        metavar="FILE_ID",
        help="process only this evidence file from the manifest",
    )
    args = parser.parse_args(argv)

    cache = load_cache()
    evidence_rows = read_csv(DATASET_DIR / "evidence_files.csv")
    # Process text formats before images and PDFs.
    evidence_rows.sort(
        key=lambda row: (
            0 if Path(row["file_path"]).suffix.casefold() in TEXT_EXTENSIONS else 1,
            row["file_id"],
        )
    )
    print_file_progress(cache, evidence_rows)

    if args.only:
        selected = [row for row in evidence_rows if row["file_id"] == args.only]
        if not selected:
            raise SystemExit(f"Unknown evidence file_id: {args.only}")
        evidence_rows_to_process = selected
    else:
        evidence_rows_to_process = evidence_rows

    call_count = load_daily_call_count()
    if call_count >= MAX_DAILY_CALLS:
        print(
            f"Stopped: the daily call limit is {MAX_DAILY_CALLS}; "
            f"the counter is already at {call_count}."
        )
        return

    pending = [
        row for row in evidence_rows_to_process if row["file_id"] not in cache
    ]
    if not pending:
        print("All selected evidence files are already cached.")
        return

    categories = sorted({
        row["data_category"]
        for row in read_csv(DATASET_DIR / "deletion_rules.csv")
    })
    ensure_usage_log_schema()
    api_key = read_api_key()
    try:
        client = genai.Client(api_key=api_key)
    except Exception:
        raise SystemExit("Could not initialize the Gemini client.") from None

    consecutive_failures = 0
    for evidence in pending:
        file_id = evidence["file_id"]
        try:
            result = extract_one(client, evidence, categories, cache, api_key)
            # Write after every manifest entry, including calls that need retry.
            save_cache(cache)
            print_file_progress(cache, evidence_rows)
            if result == "stop":
                break
            if result == "failed":
                consecutive_failures += 1
            else:
                consecutive_failures = 0
        except OSError:
            # Keep the file out of the cache so a later run can try again.
            save_cache(cache)
            print(f"Could not read {file_id}; it can be retried later.")
            append_extraction_error(
                file_id, 0, "OSError", "could not read the evidence file", api_key
            )
            consecutive_failures += 1
        except ValueError as error:
            save_cache(cache)
            append_extraction_error(
                file_id, 0, type(error).__name__, str(error), api_key
            )
            print(f"Could not process {file_id}; see extraction_errors.csv.")
            consecutive_failures += 1
        print_file_progress(cache, evidence_rows)
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            print(
                f"Stopped after {MAX_CONSECUTIVE_FAILURES} consecutive file failures."
            )
            break

    save_cache(cache)
    print_file_progress(cache, evidence_rows)
    print(f"Extraction run finished; {len(pending)} file(s) were pending at start.")


if __name__ == "__main__":
    main()
