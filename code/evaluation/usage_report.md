# Usage report

## Final run that produced `output.csv`

- Provider: Google Gemini was configured for cache preparation, but the final output run did not invoke a provider.
- Models invoked in the final run: none. The model names found in the saved preparation logs are `gemini-3.5-flash` and `gemini-3.5-flash-lite`.
- Requests processed: 518.
- Gemini calls for this run: 0.
- Input tokens: 0; output tokens: 0; total tokens: 0.
- Average tokens per request: 0 / 518 = 0.
- Estimated total cost: **$0.00**. Estimated cost per request: **$0.00**. These are exact for this run because it made no Gemini calls.

The final CSV is generated from local Python, the structured JSON/SQL reader, and the saved intent/evidence caches. Re-running this pipeline does not require a Gemini key.

## Existing cache-preparation usage records

The included `usage_log.csv` was used to summarize earlier Gemini preparation attempts; it has no timestamp column, so these calls cannot be attributed to the final output run. It contains 339 logged attempts: 18 intent-classification attempts and 321 evidence-extraction attempts. The model split is 105 attempts on `gemini-3.5-flash` and 234 on `gemini-3.5-flash-lite` (216 evidence plus 18 intent attempts). The daily call counter currently records 42 reserved calls for 2026-10-25; because the log entries are undated, that value cannot be reconciled against the 339 historical rows.

Token counts are present on 238 of the 339 log rows. The recorded sums are 1,067,932 input tokens and 516,508 output tokens. The other 101 rows have missing token fields. For a rough historical estimate only, each missing row was imputed with the mean recorded input and output tokens for the same task type and model:

| Task and model | Attempts | Rows with token counts | Estimated input tokens | Estimated output tokens |
|---|---:|---:|---:|---:|
| Intent, `gemini-3.5-flash-lite` | 18 | 15 | 40,310 | 15,850 |
| Evidence, `gemini-3.5-flash` | 105 | 9 | 590,450 | 291,538 |
| Evidence, `gemini-3.5-flash-lite` | 216 | 214 | 992,924 | 482,781 |
| **Historical preparation total (estimated)** | **339** | **238** | **1,623,684** | **790,169** |

The estimate assumes every missing-token attempt used the observed per-task/model mean. Many missing rows are connection or quota errors, so some may have consumed fewer or no tokens; this estimate can overstate provider usage. The repository has no dated billing export or price-plan record, so historical preparation charges cannot be priced reliably and are not included in the exact $0.00 final-run cost above.
