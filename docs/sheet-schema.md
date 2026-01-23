# Google Sheet Schema

The Google Sheet is a lightweight queue and state machine.

## Required columns (recommended)
| Column | Name | Meaning |
|---|---|---|
| A | url | URL submitted for capture |
| B | status | NEW / FETCHING / DONE / ERROR |
| F | json_path | Local path to capture JSON (written by Worker A) |
| I | ai_status | blank → CONDENSING → AI_READY / AI_FAILED |
| J | ai_input_path | Local path to `.ai_input.json` (written by reducer worker) |
| K | ai_error | Error text if AI prep failed |

## Suggested future columns
- meta_status / meta_path
- claims_status / claims_path
- xaio_status / xaio_path

## Idempotency rules
- Stage A should only process rows where status=NEW (or retryable).
- Condensing should only process rows where status=DONE and ai_status is blank or AI_FAILED (if you allow retries).
