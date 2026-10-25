"""Classify whether each request asks the company to delete personal data."""

import argparse
import json
import time
from pathlib import Path

from google import genai
from google.genai import types

import extract_evidence as shared


ROOT_DIR = Path(__file__).resolve().parents[1]
DATASET_DIR = ROOT_DIR / "dataset"
INTENT_CACHE_PATH = ROOT_DIR / "intent_cache.json"

# Use the same model setting as test_gemini.py and extract_evidence.py.
MODEL = shared.MODEL
BATCH_SIZE = 40
MAX_ATTEMPTS = 3  # One initial call plus up to two retries.
PAUSE_BETWEEN_CALLS_SECONDS = shared.CALL_PAUSE_SECONDS
RETRY_WAIT_SECONDS = 2


def load_intent_cache():
    """Read prior labels so interrupted runs can continue safely."""
    if not INTENT_CACHE_PATH.exists():
        return {}
    cache = json.loads(INTENT_CACHE_PATH.read_text(encoding="utf-8"))
    if not isinstance(cache, dict):
        raise ValueError("intent_cache.json must contain a JSON object")
    return cache


def save_intent_cache(cache):
    """Save after each completed batch, using an atomic file replacement."""
    temporary_path = INTENT_CACHE_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(INTENT_CACHE_PATH)


def parse_reply(reply, expected_ids):
    """Validate one JSON list and make sure it labels this whole batch."""
    text = (reply or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    result = json.loads(text)
    if not isinstance(result, list):
        raise ValueError("reply must be a JSON list")

    labels = {}
    for item in result:
        if not isinstance(item, dict):
            raise ValueError("each item must be a JSON object")
        if set(item) != {"case_id", "authorizes_deletion"}:
            raise ValueError("each item must contain exactly the two requested fields")
        case_id = item["case_id"]
        label = item["authorizes_deletion"]
        if not isinstance(case_id, str) or type(label) is not bool:
            raise ValueError("case_id must be text and authorizes_deletion must be boolean")
        if case_id in labels:
            raise ValueError("reply contains a duplicate case_id")
        labels[case_id] = label

    if set(labels) != set(expected_ids):
        raise ValueError("reply case_ids do not match the requested batch")
    return labels


def make_prompt(batch):
    """Explain authorization and send only the relevant request text."""
    entries = [
        {"case_id": row["case_id"], "request_text": row.get("request_text", "")}
        for row in batch
    ]
    return (
        "Classify each request independently. Decide whether the person is asking "
        "Northstar Shop to delete, erase, remove, forget, or close an account to "
        "fulfill deletion of their personal data. This is the authorization "
        "decision described in the task: an access, information, correction, "
        "export, marketing opt-out, or unrelated request does not authorize "
        "deletion. A request to remove a remaining personal-data copy, such as "
        "an old search result or support details, authorizes deletion even if "
        "the main account is already gone. A follow-up request to audit, check, "
        "confirm, or verify whether an earlier deletion removed the account or "
        "identifiable records also authorizes deletion verification; wording "
        "such as 'tell me whether it was removed' is not merely an information "
        "request in that context. Interpret other "
        "languages by meaning. When the text does not ask for deletion, return "
        "false. Return JSON only: a list with exactly one object per input, each "
        "having case_id (string) and authorizes_deletion (boolean). Do not add "
        "explanations or other fields.\n\n"
        + json.dumps(entries, ensure_ascii=False)
    )


def classify_batch(client, batch, batch_number):
    """Call Gemini, validate its reply, and log token usage for each attempt."""
    expected_ids = [row["case_id"] for row in batch]
    batch_name = f"intent_batch_{batch_number:03d}"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(RETRY_WAIT_SECONDS * (attempt - 1))

        allowed, call_count = shared.reserve_api_call()
        if not allowed:
            return None, f"daily API call cap reached ({call_count}/{shared.MAX_DAILY_CALLS})"

        response = None
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=make_prompt(batch),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                ),
            )
            input_tokens, output_tokens = shared.token_counts(response)
            labels = parse_reply(response.text or "", expected_ids)
        except Exception as error:
            input_tokens, output_tokens = shared.token_counts(response)
            error_kind = type(error).__name__
            shared.append_usage(
                batch_name, attempt, input_tokens, output_tokens,
                f"error_{error_kind}",
            )

            if shared.is_rate_limit_error(error):
                quota_period, quota_name = shared.quota_details(error)
                if quota_period == "day":
                    return None, f"daily quota reached ({quota_name}); saved completed batches"
                if quota_period == "minute" and attempt < MAX_ATTEMPTS:
                    delay = shared.retry_delay_seconds(error) + 5
                    time.sleep(delay)
                    time.sleep(PAUSE_BETWEEN_CALLS_SECONDS)
                    continue

            if attempt == MAX_ATTEMPTS:
                return None, f"batch {batch_number} failed validation or API call"
            time.sleep(PAUSE_BETWEEN_CALLS_SECONDS)
            continue

        shared.append_usage(
            batch_name, attempt, input_tokens, output_tokens, "ok"
        )
        time.sleep(PAUSE_BETWEEN_CALLS_SECONDS)
        return labels, ""

    return None, f"batch {batch_number} did not complete"


def main():
    parser = argparse.ArgumentParser(
        description="Classify request intent with Gemini and cache the results."
    )
    parser.add_argument(
        "--refresh-samples",
        action="store_true",
        help="Reclassify the public sample requests using the current prompt.",
    )
    args = parser.parse_args()

    if args.refresh_samples:
        requests = shared.read_csv(DATASET_DIR / "sample_deletion_requests.csv")
    else:
        requests = []
        for filename in ("deletion_requests.csv", "sample_deletion_requests.csv"):
            requests.extend(shared.read_csv(DATASET_DIR / filename))

    # The two participant request files have distinct case IDs.
    seen_ids = set()
    unique_requests = []
    for row in requests:
        if row["case_id"] not in seen_ids:
            seen_ids.add(row["case_id"])
            unique_requests.append(row)

    cache = load_intent_cache()
    pending = (
        unique_requests
        if args.refresh_samples
        else [row for row in unique_requests if row["case_id"] not in cache]
    )
    if args.refresh_samples:
        print(
            f"Requests: {len(unique_requests)}; already cached: "
            f"{sum(row['case_id'] in cache for row in unique_requests)}; "
            f"to reclassify: {len(pending)}"
        )
    else:
        print(
            f"Requests: {len(unique_requests)}; already cached: "
            f"{len(unique_requests) - len(pending)}; to classify: {len(pending)}"
        )
    if not pending:
        print("Intent cache is already complete.")
        return

    api_key = shared.read_api_key()
    client = genai.Client(api_key=api_key)

    for batch_number, start in enumerate(range(0, len(pending), BATCH_SIZE), start=1):
        batch = pending[start:start + BATCH_SIZE]
        labels, reason = classify_batch(client, batch, batch_number)
        if labels is None:
            save_intent_cache(cache)
            print(f"Stopped: {reason}.")
            return
        cache.update(labels)
        save_intent_cache(cache)
        print(f"Saved batch {batch_number} ({len(batch)} requests).")

    print(f"Done. Cached {len(cache)} request labels in {INTENT_CACHE_PATH.name}.")


if __name__ == "__main__":
    main()
