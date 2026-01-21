# Phase 0 — Safety + Baseline

## Task 0.1 — Create refactor branch + baseline run notes

**Goal:** Protect main and capture a “known-good” baseline.
**Steps:**

1. `git checkout main && git pull`
2. `git checkout -b refactor/prod-hardening`
3. Run current pipeline once on a single URL row (whatever works today).
4. Record outputs produced and sheet columns used (just a short note in `docs/baseline.md`).
   **Acceptance:**

* Branch exists.
* `docs/baseline.md` contains: how you ran it + which outputs appeared + what sheet columns changed.

---

# Phase 1 — “Fresh clone works” fixes (no behavior changes)

## Task 1.1 — Validate and fix config example YAML

**Goal:** Ensure `config/config.example.yaml` is valid YAML and matches the code’s actual expectations.
**Files:**

* `config/config.example.yaml`
  **Steps:**

1. Fix any YAML syntax errors (e.g., missing closing quote).
2. Ensure it includes keys referenced in:

   * `src/agent.py`
   * `src/condense_queue.py`
3. Add comments indicating which values are required vs optional.
4. Run: `python -c "import yaml; yaml.safe_load(open('config/config.example.yaml'))"`
   **Acceptance:**

* Loading the YAML returns no error.
* Developer can copy it to `config.yaml` and run without KeyError (with obvious placeholders).

---

## Task 1.2 — Make `requirements.txt` complete for current imports

**Goal:** Ensure pip install always produces a runnable env.
**Files:**

* `requirements.txt`
  **Steps:**

1. Add missing packages used in code (at minimum: `openai`, `pydantic`).
2. Run in venv: `pip install -r requirements.txt`
3. Smoke import:
   `python -c "import openai, pydantic, gspread, playwright, bs4, readability"`
   **Acceptance:**

* Clean venv install succeeds.
* Imports succeed.

---

## Task 1.3 — Confirm `scripts/bootstrap_ubuntu.sh` is executable and correct

**Goal:** Make sure your current bootstrap script runs end-to-end.
**Files:**

* `scripts/bootstrap_ubuntu.sh` (the one you pasted)
  **Steps:**

1. Ensure file uses LF line endings (not CRLF) if needed.
2. Make executable: `chmod +x scripts/bootstrap_ubuntu.sh`
3. Run from clean machine / clean repo: `./scripts/bootstrap_ubuntu.sh`
   **Acceptance:**

* Script completes without error.
* Creates venv, installs deps, creates dirs, creates `config.yaml` if absent.

---

# Phase 2 — systemd + scripts alignment (still no behavior change)

## Task 2.1 — Fix systemd unit validity and paths

**Goal:** systemd units run reliably against this repo layout.
**Files:**

* `systemd/user/url-agent.service`
* `systemd/user/condense-agent.service`
* `systemd/user/*.timer`
* (if exists) `scripts/install_systemd_user.sh`
  **Steps:**

1. Validate each unit: ensure only one `[Unit]`, `[Service]`, `[Install]`.
2. Ensure each unit:

   * sets `WorkingDirectory` to the repo directory
   * uses venv python explicitly: `ExecStart=%h/path/to/repo/venv/bin/python ...`
3. Add locking consistently:

   * Capture: `flock -n ./locks/capture.lock ...`
   * Condense: `flock -n ./locks/condense.lock ...`
4. Reload and validate:

   * `systemctl --user daemon-reload`
   * `systemd-analyze verify systemd/user/url-agent.service`
     **Acceptance:**

* `systemd-analyze verify` passes.
* `systemctl --user start url-agent.service` runs and logs output.

---

## Task 2.2 — Make subprocess calls use venv python (reproducible)

**Goal:** Remove reliance on bare `python` / PATH.
**Files:**

* `src/condense_queue.py` (and any others spawning python)
  **Steps:**

1. Replace any subprocess `"python"` with `sys.executable`.
2. Ensure relative paths work regardless of caller: use `Path(__file__).resolve()` patterns.
   **Acceptance:**

* Running `condense_queue.py` from any directory still works.
* It invokes reduce stage using the same venv python.

---

# Phase 3 — Repair and complete the two-call AI architecture

## Task 3.1 — Add “meta input” generator (strip body text)

**Goal:** Ensure meta AI call receives metadata-only input, never full text.
**Files:**

* Add: `src/strip_content_for_meta.py` (or `src/xaio_url_agent/reduce/meta_input.py` later)
  **Implementation spec:**
* Input: `out_ai/<id>.ai_input.json`
* Output: `out_ai_meta/<id>.meta_input.json`
* Behavior:

  * copy everything except remove `content.extracted_text_full`
  * retain `content.char_count`, `content.word_count`, `content.sha256`
    **Steps:**

1. Implement script with CLI: `--in`, `--outdir`
2. Test:

   * generate meta_input for one ai_input
   * verify meta_input JSON contains no `extracted_text_full`
     **Acceptance:**

* Meta input files are produced.
* Guaranteed no full text present.

---

## Task 3.2 — Fix `call_openai_claims.py` (must run cleanly)

**Goal:** Claims call works reliably and preserves output contract.
**Files:**

* `src/call_openai_claims.py`
  **Steps:**

1. Fix syntax errors (unclosed parentheses, etc.).
2. Fix variable naming bugs (`claims_input` vs `user_input`).
3. Ensure output schema is **only**:

   ```json
   {"claims":[{"claim_text":"...","claim_type":"..."}]}
   ```

   (No verdicts, no scores unless stored elsewhere.)
4. Add minimal deterministic normalizer:

   * strip whitespace
   * drop empty claim_text
   * dedupe exact duplicates
5. Smoke test on one ai_input containing full text.
   **Acceptance:**

* Script runs without exception.
* Output JSON contains only `{claim_text, claim_type}` per claim.

---

## Task 3.3 — Create AI queue worker (Stage C1+C2+D runner)

**Goal:** A systemd timer can continuously process AI-ready rows end-to-end.
**Files:**

* Add: `src/ai_queue.py`
* Update: `config.yaml` (new columns), `config/config.example.yaml`
  **Behavior:**
* For each row with `ai_status == AI_READY`:

  1. Ensure meta_input exists (generate if missing)
  2. Run meta call → write `out_meta/<id>.meta_parsed.json`
  3. Run claims call → write `out_claims/<id>.claims_parsed.json`
  4. Run merge → write `out_xaio/<id>.xaio_parsed.json`
  5. Update sheet columns for paths/statuses/errors
* Must be idempotent:

  * if output exists and matches sha, skip stage
    **Steps:**

1. Implement the worker with `max_per_run` config knob.
2. Add sheet columns in config (meta_status/path, claims_status/path, xaio_status/path).
3. Add systemd unit/timer `ai-agent.service` / `ai-agent.timer`.
   **Acceptance:**

* On a single URL row:

  * capture → ai_input → meta/claims → merged xaio JSON produced
  * sheet updated with paths and statuses
* Running again does not reprocess unnecessarily.

---

## Task 3.4 — Ensure merge output includes full extracted text and counts

**Goal:** Final output remains WP-ready and includes necessary fields for SCF mapping.
**Files:**

* `src/merge_xaio.py`
  **Steps:**

1. Verify merge includes:

   * extracted_text_full
   * char_count / word_count
   * canonical_url/domain/site_name
2. Add assertion checks:

   * if meta_parsed missing required fields → fail with clear error
     **Acceptance:**

* `out_xaio/*.xaio_parsed.json` contains `claims` and `extracted_text_full`.

---

# Phase 4 — Debuggability + Reliability

## Task 4.1 — Add structured logging conventions

**Goal:** Every script logs consistent, greppable context.
**Files:**

* `src/*.py` scripts
  **Steps:**

1. Standardize logging format (even if just `logging` with consistent prefix):

   * `stage`, `item_id`, `row`, `url`, `elapsed_ms`
2. Make log level configurable via env `XAIO_LOG_LEVEL`.
   **Acceptance:**

* `journalctl --user -u url-agent.service` shows consistent structured-ish logs.

---

## Task 4.2 — Implement retries for Sheets writes + OpenAI calls

**Goal:** Transient failures don’t break runs.
**Files:**

* wherever gspread updates happen (agent, condense, ai_queue)
* `call_openai_meta.py`, `call_openai_claims.py`
  **Steps:**

1. Add retry wrapper for:

   * gspread update calls (exponential backoff, max 5)
   * OpenAI errors (rate limit / timeout)
2. Ensure errors are written to sheet error columns and do not crash entire run.
   **Acceptance:**

* Simulated transient errors (disconnect) lead to retry logs, and script continues or fails gracefully.

---

## Task 4.3 — Add `scripts/doctor.sh`

**Goal:** One command verifies machine readiness.
**Files:**

* Add: `scripts/doctor.sh`
  **Checks:**
* venv active + required imports
* secrets file exists
* `.env` exists or OPENAI_API_KEY set
* CDP endpoint reachable if configured
* can open sheet and read header row
  **Acceptance:**
* `./scripts/doctor.sh` exits 0 when healthy, nonzero with actionable messages when not.

---

# Phase 5 — Author + organization extraction (deterministic-first)

## Task 5.1 — Add JSON-LD extraction during capture

**Goal:** Extract publisher/author candidates before AI.
**Files:**

* `src/agent.py` (or new helper module)
  **Steps:**

1. Parse `<script type="application/ld+json">` blocks.
2. Extract candidates:

   * publisher org name(s)
   * author person name(s)
   * datePublished/dateModified
3. Store in capture JSON under a new key (e.g. `page.jsonld_extracted`).
   **Acceptance:**

* For known article URLs, capture JSON includes publisher/author candidates.

---

## Task 5.2 — Carry identity candidates into ai_input and meta_input

**Goal:** Meta-only AI call can pick org/author without body text.
**Files:**

* `src/reduce4ai.py`
* meta_input generator from Task 3.1
  **Steps:**

1. Add `meta.identity_candidates` fields.
2. Ensure meta_input includes candidates.
   **Acceptance:**

* `*.ai_input.json` and `*.meta_input.json` both contain identity candidates.

---

## Task 5.3 — Update meta AI schema to output org/author explicitly

**Goal:** Meta parse produces reliable `organization_name` and `author_name(s)`.
**Files:**

* `src/call_openai_meta.py`
* prompt schema / scf alignment (as needed)
  **Steps:**

1. In the meta-only schema, add fields:

   * `organization_name` (nullable)
   * `author_names` (array) or `author_name` (nullable)
2. Update prompt to:

   * choose from candidates or return null
   * no invention
     **Acceptance:**

* Meta output includes org/author fields even if null.
* It does not hallucinate outside candidate set (spot-check).

---

# Phase 6 — WordPress / SCF upload path (optional but planned)

## Task 6.1 — Decide ingestion approach (WP plugin endpoint vs WP-CLI)

**Goal:** Choose the simplest reliable local-first upload method.
**Deliverable:** `docs/wordpress-upload.md` with chosen approach and payload mapping.
**Acceptance:**

* Document states exactly how `.xaio_parsed.json` maps to CPT `content` + SCF fields.

---

# Definition of Done (for this whole initiative)

When these are complete, you’ll have a “production-ready local MVP”:

* Fresh machine: `bootstrap_ubuntu.sh` + `doctor.sh` works
* systemd timers run without overlap (locks)
* Stage separation remains:

  * A capture truth JSON
  * B reduction AI input
  * C1 meta-only AI call (no body)
  * C2 claims-only AI call (body)
  * D merge WP-ready JSON
* Claims remain `{claim_text, claim_type}` only
* Org/author extraction is deterministic-first with AI only selecting/normalizing
