# Agent Tasks

1. **Massively reduce Google Sheets reliance & rate limiting**
2. **Fix path/working-directory/config weirdness + the “duplicate .config/systemd vs repo systemd” confusion**
3. **Add WordPress publishing: create “content” CPT entries + push SCF fields as metadata**

I’m going to give you this in **two implementation tiers** for #1:

* **Tier A (quick win):** keep the Sheet state machine, but reduce write calls by *orders of magnitude* (batch update instead of `update_acell` per cell). This alone often stops quota pain immediately.
* **Tier B (real scale):** Sheet becomes *intake only* (or optional dashboard), and the pipeline state machine moves to SQLite (which you already have). That’s the “thousands/minute in theory” direction.

---

## 1) Reduce reliance on Google Sheets (and stop rate limits)

### First: what’s causing the quota burn in your code

Right now each stage writes to Sheets in the most expensive way possible: **one API call per cell**.

Examples:

* `src/agent.py` → `update_row()` calls `wks.update_acell(...)` repeatedly for **B,C,D,E,F,G,H**.
* `src/condense_queue.py` → writes **I,J,K** via per-cell updates.
* `src/ai_queue.py` → writes **L–T** in multiple step transitions via per-cell updates.

That means one processed URL can easily be **20–40 separate write requests** depending on how many transitions happen in a run.

Even if the *data* is “mostly path names”, the real killer is **request count**, not payload size.

---

## Tier A: Keep the Sheet pipeline, but rewrite updates to batch in 1 request

### Goal

Replace every “update 7–9 cells” sequence with **one** `batch_update()` call.

### Step A1 — Add a shared batch-update helper

Create a new file:

#### ✅ `src/sheets_batch.py` (new)

```python
from __future__ import annotations

import logging
from typing import Dict, Any, List

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from gspread import Worksheet
from gspread.exceptions import APIError

logger = logging.getLogger(__name__)

@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((APIError,)),
)
def batch_update_row_cells(
    wks: Worksheet,
    row: int,
    col_to_value: Dict[str, Any],
    *,
    value_input_option: str = "RAW",
) -> None:
    """
    Update multiple single-cell ranges in ONE Sheets API call.

    col_to_value keys are column letters like "B", "F", "K".
    """
    if not col_to_value:
        return

    data: List[dict] = []
    for col, value in col_to_value.items():
        a1 = f"{col}{row}"
        data.append({"range": a1, "values": [[value]]})

    # One request for all cells
    wks.batch_update(data, value_input_option=value_input_option)
```

This deliberately uses **multiple ranges** but still **one API call**.

> You could optimize further into contiguous ranges (like `B{row}:H{row}`), but this is already a massive win and is simpler/safer.

---

### Step A2 — Replace `update_acell` loops in all three stages

#### ✅ In `src/agent.py`

Find:

```python
def update_row(wks, row, updates):
    for col, val in updates.items():
        wks.update_acell(f"{col}{row}", val)
```

Replace with:

```python
from sheets_batch import batch_update_row_cells

def safe_update_row(wks, row, updates, *, item_id="", url=""):
    try:
        batch_update_row_cells(wks, row, updates)
    except Exception as e:
        logger.warning(f"sheet update failed row={row} item={item_id} err={e}")
```

Also delete/stop using `update_row()` entirely so nobody reintroduces per-cell updates.

---

#### ✅ In `src/condense_queue.py`

Find `update_cells()` and `safe_update_cells()` (they call `update_acell`).
Replace the internals to call `batch_update_row_cells(...)` exactly like above.

---

#### ✅ In `src/ai_queue.py`

Same replacement: `update_cells()` and `safe_update_cells()` should call `batch_update_row_cells(...)`.

---

### Step A3 — (Optional but big) Stop reading the *entire* sheet

You currently do `wks.get_all_values()` in all stages. That pulls the whole grid.

Even if you keep Sheets as the state machine, you can reduce read volume by fetching only the columns you need.

Example changes:

* `agent.py` only needs URL col + status col (and maybe a few others)
* `condense_queue.py` only needs URL + status + json_path + ai_status
* `ai_queue.py` only needs url + ai_status + ai_input_path + meta/claims/xaio status

This is not as urgent as fixing writes, but it helps.

---

### What Tier A buys you

* You go from **~30 API write calls per row** → typically **3–6** (because you still do multiple transitions).
* If you also consolidate transitions (write “RUNNING + path” in a single update), you go lower.
* This usually stops rate limits immediately.

But… it still won’t get you to “thousands/min” because you’re still using Sheets as a transactional database.

So now:

---

## Tier B: Move the pipeline state machine OFF Sheets (keep Sheets only as intake/dashboard)

This is the “real solution” for scale.

### Core idea

* Sheets is just a **human-friendly intake**: column A = URL list.
* Your **SQLite DB becomes the queue/state machine** for:

  * capture status + capture json path
  * condense status + ai_input path
  * ai status + meta/claims/xaio paths
  * wordpress publish status + wp ids

And then **each stage queries SQLite**, not Sheets, for what to do next.

### Why this works for “thousands/min in theory”

* SQLite can handle huge write rates locally (especially with WAL mode).
* You stop spending API quota on “path strings for readability”.
* You can run multiple workers eventually (or move to Postgres later).

---

### Step B1 — Extend your existing SQLite schema (agent.db) to include all stage columns

Right now `src/agent.py` creates table `items` with only capture fields.

You want to migrate it so it can hold all pipeline stages.

#### ✅ Create `src/state_db.py` (new)

```python
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, Any, Dict, List

BASE_COLUMNS = {
    # existing-ish
    "sheet_row": "INTEGER",
    "ai_status": "TEXT",
    "ai_input_path": "TEXT",
    "ai_error": "TEXT",

    "meta_status": "TEXT",
    "meta_path": "TEXT",
    "meta_error": "TEXT",

    "claims_status": "TEXT",
    "claims_path": "TEXT",
    "claims_error": "TEXT",

    "xaio_status": "TEXT",
    "xaio_path": "TEXT",
    "xaio_error": "TEXT",

    "wp_status": "TEXT",
    "wp_post_id": "INTEGER",
    "wp_error": "TEXT",
}

def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Better concurrency + speed
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    init_and_migrate(conn)
    return conn

def init_and_migrate(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url_original TEXT NOT NULL,
            url_final TEXT,
            url_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            processed_at TEXT,
            method TEXT,
            json_path TEXT,
            error TEXT
        );
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_hash ON items(url_hash);")

    # Migrate columns
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    for col, coltype in BASE_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} {coltype};")

    # Helpful indexes for queueing
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_ai_status ON items(ai_status);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_xaio_status ON items(xaio_status);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_wp_status ON items(wp_status);")

    conn.commit()
```

This lets all stages share the same DB.

---

### Step B2 — Modify `src/agent.py` to store everything in DB and make sheet updates optional

#### Change 1: add argparse + config path sanity

At bottom of `agent.py`, replace:

```python
if __name__ == "__main__":
    cfg = load_config("config.yaml")
    run_once(cfg)
```

With:

```python
if __name__ == "__main__":
    import argparse, os
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.getenv("XAIO_CONFIG_PATH", "config.yaml"))
    args = ap.parse_args()
    cfg = load_config(args.config)
    run_once(cfg)
```

This directly addresses “wrong working dir” pain too.

#### Change 2: use `state_db.connect()` instead of local db_init

Replace your `db_init()` usage with:

```python
from state_db import connect as db_connect
...
conn = db_connect(cfg.sqlite_path)
```

#### Change 3: stop writing paths/status back to Sheets (or do minimal)

Add a config flag (in YAML) like:

```yaml
sheet:
  writeback_mode: "minimal"   # "full" | "minimal" | "none"
```

Then in `agent.py`, right before any `safe_update_row(...)`, gate it:

```python
writeback = data["sheet"].get("writeback_mode", "full")
...
if writeback != "none":
    if writeback == "minimal":
        safe_update_row(wks, sheet_row_idx, {
            cfg.col_status: "DONE",
            cfg.col_processed_at: now_iso(),
            cfg.col_error: "",
        }, ...)
    else:
        safe_update_row(wks, sheet_row_idx, {
            cfg.col_status: "DONE",
            cfg.col_processed_at: now_iso(),
            cfg.col_final_url: final_url,
            cfg.col_method: method,
            cfg.col_json_path: str(out_path),
            cfg.col_title: title,
            cfg.col_error: "",
        }, ...)
```

**Key:** once you switch Stage 2+3 to DB (next steps), you no longer need json_path written into Sheets at all.

---

### Step B3 — Rewrite `src/condense_queue.py` to query DB instead of Sheets

Right now it does:

* read sheet rows
* look for `status == DONE` and `ai_status != AI_READY`
* reads `json_path` from sheet

Instead:

#### ✅ Condense logic using SQLite as queue

1. Remove gspread entirely from `condense_queue.py`.
2. Load config for `sqlite_path`, `out_ai_dir`, `prompt_set_id`, `max_per_run`.
3. Query DB for items ready to condense.

Add:

```python
import sqlite3
from state_db import connect as db_connect

def fetch_ready_for_condense(conn: sqlite3.Connection, limit: int):
    cur = conn.execute("""
        SELECT url_hash, url_original, json_path
        FROM items
        WHERE status = 'DONE'
          AND json_path IS NOT NULL
          AND (ai_status IS NULL OR ai_status = '' OR ai_status IN ('AI_FAILED'))
        ORDER BY processed_at DESC
        LIMIT ?;
    """, (limit,))
    return cur.fetchall()
```

Then in `main()`:

```python
conn = db_connect(Path(cfg.sqlite_path))
rows = fetch_ready_for_condense(conn, cfg.max_per_run)

for r in rows:
    url_hash = r["url_hash"]
    json_path = r["json_path"]

    conn.execute("UPDATE items SET ai_status = ?, ai_error = ? WHERE url_hash = ?",
                 ("CONDENSING", "", url_hash))
    conn.commit()

    ok, msg = run_reduce4ai(json_path, cfg.out_ai_dir, cfg.prompt_set_id)

    if ok:
        conn.execute("""
            UPDATE items
            SET ai_status = ?, ai_input_path = ?, ai_error = ?
            WHERE url_hash = ?
        """, ("AI_READY", msg, "", url_hash))
    else:
        conn.execute("""
            UPDATE items
            SET ai_status = ?, ai_error = ?
            WHERE url_hash = ?
        """, ("AI_FAILED", msg, url_hash))

    conn.commit()
```

Now Stage 2 is fully off Sheets.

---

### Step B4 — Rewrite `src/ai_queue.py` to query DB instead of Sheets

Right now it:

* reads rows from sheet where `AI_READY`
* uses ai_input_path from sheet
* writes meta/claims/xaio statuses back to sheet

Instead, use DB fields:

* `ai_status = AI_READY`
* `ai_input_path` stored by Stage 2
* write `meta_status`, `claims_status`, `xaio_status`, paths, errors into DB

Add query:

```python
def fetch_ready_for_ai(conn, limit: int):
    cur = conn.execute("""
        SELECT url_hash, url_original, ai_input_path,
               meta_status, claims_status, xaio_status
        FROM items
        WHERE ai_status = 'AI_READY'
          AND ai_input_path IS NOT NULL
        ORDER BY processed_at DESC
        LIMIT ?;
    """, (limit,))
    return cur.fetchall()
```

Then for each row, instead of `safe_update_cells(wks,...)` you do DB updates like:

```python
conn.execute("UPDATE items SET meta_status=?, meta_error=?, meta_path=? WHERE url_hash=?",
             ("META_RUNNING", "", str(meta_parsed_path), url_hash))
conn.commit()
...
if meta_ok:
   conn.execute("UPDATE items SET meta_status=?, meta_error=? WHERE url_hash=?",
                ("META_DONE","",url_hash))
else:
   conn.execute("UPDATE items SET meta_status=?, meta_error=? WHERE url_hash=?",
                ("META_FAILED", err, url_hash))
```

Repeat for claims + xaio.

At the end:

```python
conn.execute("UPDATE items SET xaio_status=?, xaio_path=?, xaio_error=? WHERE url_hash=?",
             ("XAIO_DONE", str(xaio_parsed_path), "", url_hash))
conn.commit()
```

Now Stage 3 is fully off Sheets.

---

### Step B5 — What happens to the Google Sheet after Tier B?

You have choices:

* **Mode 1 (recommended):** Sheet is intake only; pipeline never writes back → basically no quota issues.
* **Mode 2:** Write back only status/error (minimal) so humans can see progress.
* **Mode 3:** Run a separate “dashboard sync” job every N minutes that batch-updates many rows at once (super low request count).

---

### Reality check on “thousands/min” with a Sheet intake

Even if you stop writing to Sheets, **Sheets is still not a viable intake mechanism at thousands/min**. Humans won’t, and the API won’t.

So your “in theory” scalable intake becomes:

* a simple HTTP endpoint (FastAPI/Flask)
* or a queue (SQS / PubSub / Redis)
* or just directly inserting URLs into your DB table

But Tier B gives you the *internal architecture* needed for any of those.

---

# 2) Path + dependency issues (and the “duplicate .config vs repo config” thing)

You described:

* repo has a local `systemd/user` folder ✅
* but your machine is using `~/.config/systemd/user` ✅
* and you’re seeing “duplicates” and mismatched working directories

### What’s actually going on

This part is normal:

* `systemd/user/*.service` files in your repo are **templates**
* `scripts/install_systemd_user.sh` **copies** them into:

  * `~/.config/systemd/user/`
* That’s the *correct* place for **user-level systemd units**

So having both is expected:

* repo copy = source-of-truth templates
* `~/.config/systemd/user` copy = what systemd actually loads

### The real bug you’re hitting

Your services assume repo lives at **`%h/xAIO-URL-Agent`** unless you run the install script, which does a sed replace.

If your repo is actually at:

* `~/XAIO/xAIO-URL-Agent`
  …but your installed units still point at:
* `~/xAIO-URL-Agent`
  …then systemd will run from the wrong directory, and relative paths like `config.yaml` / `secrets/service_account.json` will resolve wrong.

### Fix it cleanly (step-by-step)

Run these commands *once* on the box where systemd is running:

1. See what systemd is currently using:

```bash
systemctl --user cat pipeline.service
systemctl --user cat url-agent.service
```

2. Stop everything:

```bash
systemctl --user disable --now pipeline.timer pipeline.service || true
systemctl --user disable --now url-agent.timer condense-agent.timer ai-agent.timer || true
systemctl --user disable --now brave-agent.service || true
```

3. Delete the installed units (the ones in ~/.config):

```bash
rm -f ~/.config/systemd/user/pipeline.*
rm -f ~/.config/systemd/user/url-agent.*
rm -f ~/.config/systemd/user/condense-agent.*
rm -f ~/.config/systemd/user/ai-agent.*
rm -f ~/.config/systemd/user/brave-agent.service
```

4. Reload systemd:

```bash
systemctl --user daemon-reload
```

5. From inside the repo *at the correct location*:

```bash
cd ~/XAIO/xAIO-URL-Agent   # <-- adjust to your real path
./scripts/install_systemd_user.sh
```

That script calculates your real `$PROJECT_DIR` and rewrites the unit files accordingly.

6. Enable the timers/services you want:

```bash
systemctl --user enable --now brave-agent.service
systemctl --user enable --now pipeline.timer
```

---

### Extra: make your Python scripts resilient even if cwd is wrong

Right now:

* `condense_queue.py` and `ai_queue.py` accept `--config`
* `agent.py` does not (it hardcodes `config.yaml` at the bottom)

I already showed the exact change to add argparse to `agent.py`.

Also, your `.env.example` advertises `XAIO_CONFIG_PATH`, but the scripts mostly don’t honor it yet. After the argparse change, they will.

---

### Bonus: Brave profile location (why you see stuff in `~/.config`)

Your `systemd/user/brave-agent.service` uses:

```ini
--user-data-dir=%h/.config/brave-agent-profile
```

That’s why you see Brave artifacts under `~/.config`.

If you want **everything** inside the repo (no “duplicate config-ish stuff”), do this:

1. Edit: `systemd/user/brave-agent.service` and change ExecStart to:

```ini
ExecStart=/usr/bin/brave-browser-beta --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 --user-data-dir=%h/xAIO-URL-Agent/.runtime/brave-agent-profile --no-first-run --no-default-browser-check
```

2. Add `.runtime/` to `.gitignore`.

3. Re-run:

```bash
./scripts/install_systemd_user.sh
systemctl --user daemon-reload
systemctl --user restart brave-agent.service
```

---

# 3) WordPress: create “content” CPT posts + upload SCF fields as metadata

You said the site is **xaio.org** and the fields are SCF-backed for a CPT called **`content`**.

There’s one key constraint:

### Important constraint

Your pipeline **cannot** truly “create a custom post type” dynamically from the outside.
In WordPress, a CPT must be registered by PHP code (theme/plugin) or via SCF UI. What your pipeline *can* do is:

* create **posts of an existing CPT** (`content`)
* set SCF/ACF fields for that post

To expose a CPT in the REST API, WordPress requires `show_in_rest => true` when registering the post type. ([WordPress Developer Resources][1])

---

## WordPress side: one-time setup

### Step WP1 — Ensure the CPT exists and is REST-enabled

If SCF created your CPT via UI, confirm:

* CPT slug is `content`
* it’s visible in REST (SCF/WordPress setting)

If you’re doing it in code, it looks like this:

```php
register_post_type('content', [
  'label' => 'Content',
  'public' => true,
  'show_in_rest' => true,
  'supports' => ['title', 'editor', 'custom-fields'],
]);
```

Again, `show_in_rest` is the crucial part. ([WordPress Developer Resources][1])

Also: your SCF export shows related post object fields pointing to:

* `organization`
* `contributor`

So those CPTs need to exist & be REST-enabled too if you want to auto-create/link them.

---

### Step WP2 — Pick an auth method (use Application Passwords)

WordPress supports Application Passwords (WP 5.6+) which work with HTTPS + Basic Auth. ([WordPress Developer Resources][2])

You’ll create:

* a dedicated WP user like `xaio_ingest`
* generate an **application password**
* store it in `.env` on the agent machine (not in git)

---

## Two ways to write SCF fields from your pipeline

### Option 1 (best): Custom WP REST endpoint that calls SCF/ACF functions server-side

This is the most reliable for repeaters and post-object fields.

**Why:** SCF is a fork of ACF and includes deep field handling; pushing raw meta can be tricky. SCF/ACF can also integrate with REST depending on settings, but repeaters/post_object updates are where custom endpoints save you pain. ([WordPress Developer Resources][3])

**High-level:**

* You POST your `*.xaio_parsed.json` to `/wp-json/xaio/v1/ingest`
* WP code:

  * creates/updates the `content` post
  * uses `update_field()` (SCF/ACF function family) to set values
  * looks up/creates `organization` and `contributor` posts
  * attaches them

### Option 2: Use SCF/ACF REST integration directly

ACF (and likely SCF) supports REST field integration (ACF notes REST integration since 5.11). ([ACF][4])
But in your `config/scf-export-content.json`, your field groups show `show_in_rest: 0` for the main “Content …” groups — so you’d need to toggle that in WP/SCF for those groups, or they won’t be writable/readable via REST.

---

## Pipeline side: implement a WordPress upload stage

Your final artifact is written here:

* `src/merge_xaio.py` → `out_xaio/<id>.xaio_parsed.json`

So the cleanest design is a new stage:

✅ **Stage 4: `wp_upload_queue.py`**
Reads DB rows where `xaio_status == XAIO_DONE` and `wp_status != WP_DONE`, then uploads.

### Step P1 — Add WordPress settings to config

Update `config/config.example.yaml` with:

```yaml
wordpress:
  base_url: "https://xaio.org"
  api_base: "https://xaio.org/wp-json"
  username: "xaio_ingest"
  app_password_env: "WP_APP_PASSWORD"
  post_type: "content"
  post_status: "draft"
  timeout_s: 30
```

Then in your real `.env`:

```bash
export WP_APP_PASSWORD="xxxx xxxx xxxx xxxx xxxx xxxx"
```

(Keep it out of git.)

---

### Step P2 — Create `src/wp_client.py` (new)

```python
from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional
import requests

@dataclass
class WPConfig:
    api_base: str
    username: str
    app_password: str
    timeout_s: int = 30

class WPClient:
    def __init__(self, cfg: WPConfig):
        self.cfg = cfg

    def _auth_header(self) -> Dict[str, str]:
        token = base64.b64encode(f"{self.cfg.username}:{self.cfg.app_password}".encode("utf-8")).decode("utf-8")
        return {"Authorization": f"Basic {token}"}

    def post(self, path: str, json: Dict[str, Any]) -> requests.Response:
        url = self.cfg.api_base.rstrip("/") + "/" + path.lstrip("/")
        return requests.post(
            url,
            headers={**self._auth_header(), "Content-Type": "application/json"},
            json=json,
            timeout=self.cfg.timeout_s,
        )

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        url = self.cfg.api_base.rstrip("/") + "/" + path.lstrip("/")
        return requests.get(
            url,
            headers=self._auth_header(),
            params=params or {},
            timeout=self.cfg.timeout_s,
        )
```

Application Password + Basic Auth is documented in WP REST auth docs. ([WordPress Developer Resources][2])

---

### Step P3 — Create `src/wp_upload_queue.py` (new)

This assumes you implemented Tier B and DB has `xaio_path` + `wp_status`.

Skeleton:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from state_db import connect as db_connect
from wp_client import WPClient, WPConfig

def load_yaml_config(path: Path) -> dict:
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))

def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.getenv("XAIO_CONFIG_PATH", "config.yaml"))
    args = ap.parse_args()

    cfg_path = Path(args.config).expanduser().resolve()
    data = load_yaml_config(cfg_path)

    db_path = Path(data["agent"]["sqlite_path"]).expanduser().resolve()
    conn = db_connect(db_path)

    wp = data["wordpress"]
    app_pw = os.getenv(wp["app_password_env"])
    if not app_pw:
        raise SystemExit(f"Missing env var {wp['app_password_env']}")

    client = WPClient(WPConfig(
        api_base=wp["api_base"],
        username=wp["username"],
        app_password=app_pw,
        timeout_s=int(wp.get("timeout_s", 30)),
    ))

    limit = int(wp.get("max_per_run", 50))

    cur = conn.execute("""
        SELECT url_hash, xaio_path
        FROM items
        WHERE xaio_status = 'XAIO_DONE'
          AND xaio_path IS NOT NULL
          AND (wp_status IS NULL OR wp_status = '' OR wp_status IN ('WP_FAILED'))
        ORDER BY processed_at DESC
        LIMIT ?;
    """, (limit,))
    items = cur.fetchall()

    for r in items:
        url_hash = r["url_hash"]
        xaio_path = Path(r["xaio_path"])

        conn.execute("UPDATE items SET wp_status=?, wp_error=? WHERE url_hash=?",
                     ("WP_RUNNING", "", url_hash))
        conn.commit()

        xaio = load_json(xaio_path)

        # Build WP payload.
        # If using a custom endpoint:
        payload = {
            "source_id": url_hash,
            "post_type": wp.get("post_type", "content"),
            "post_status": wp.get("post_status", "draft"),
            "xaio": xaio,
        }

        resp = client.post("/xaio/v1/ingest", json=payload)

        if resp.status_code >= 300:
            conn.execute("UPDATE items SET wp_status=?, wp_error=? WHERE url_hash=?",
                         ("WP_FAILED", f"{resp.status_code}: {resp.text[:2000]}", url_hash))
            conn.commit()
            continue

        out = resp.json()
        post_id = out.get("post_id")

        conn.execute("UPDATE items SET wp_status=?, wp_post_id=?, wp_error=? WHERE url_hash=?",
                     ("WP_DONE", post_id, "", url_hash))
        conn.commit()

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

---

### Step P4 — Wire it into the pipeline

Update `src/pipeline_run.py`:

From:

```python
run([py, "src/agent.py"])
run([py, "src/condense_queue.py"])
run([py, "src/ai_queue.py"])
```

To:

```python
run([py, "src/agent.py", "--config", "config.yaml"])
run([py, "src/condense_queue.py", "--config", "config.yaml"])
run([py, "src/ai_queue.py", "--config", "config.yaml"])
run([py, "src/wp_upload_queue.py", "--config", "config.yaml"])
```

Then add systemd units/timer if you want it separate (optional).

---

## Mapping SCF fields correctly (based on your SCF export)

From `config/scf-export-content.json`, your SCF field names include:

* `canonical_url`
* `content_mode`
* `domain`, `site_name`
* `language`
* `published_at`, `modified_time`, `collected_at_utc`
* `workflow_status`, `intake_kind`
* `extracted_text_full`, `char_count`, `word_count`
* repeater `claims` with subfields:

  * `claim_text`, `claim_type`, `verdict`, etc.
* post_object:

  * `primary_organization` → post type `organization`, return `id`
  * `related_contributors` → post type `contributor`, return `id`

Your pipeline output (`out_xaio/*.xaio_parsed.json`) contains:

* `organization_name` (string)
* `author_names` (list of strings)
  …but WP fields want **IDs**, so your WP ingest endpoint should:
* search/create org/contributor posts by title
* set post_object fields to their IDs

This is exactly why the **custom WP endpoint** is the easiest: you can do the lookup inside WordPress where you have WP_Query and permissions.

---

## WordPress REST exposure and auth references (so you don’t fight 403/404s forever)

* WordPress: Application Passwords + Basic Auth for REST (WP 5.6+) ([WordPress Developer Resources][2])
* WordPress: `show_in_rest => true` creates `/wp/v2/<type>` routes for CPTs ([WordPress Developer Resources][1])
* ACF REST integration exists and is documented (SCF is a fork with similar internals, but your groups need REST enabled) ([ACF][4])

---

# What I would do in your shoes (practical sequence)

### ✅ Day 1 (fast relief)

1. Implement **Tier A batch updates** → you’ll likely stop hitting Sheets write limits immediately.
2. Fix systemd unit paths by reinstalling with `./scripts/install_systemd_user.sh`.
3. Add argparse `--config` support to `agent.py` + honor `XAIO_CONFIG_PATH`.

### ✅ Day 2 (real scale)

4. Implement **Tier B** (DB is the state machine; Sheets optional/minimal).
5. Add `wp_upload_queue.py` as a fourth stage (DB-driven).
6. Add a WP custom endpoint for ingestion to handle repeaters + post_object linking cleanly.

---

If you want, I can also write you a **drop-in WordPress plugin file** (`xaio-ingest.php`) that registers `/wp-json/xaio/v1/ingest` and:

* creates/updates `content` by `canonical_url`
* auto-creates/links `organization` + `contributor`
* writes the SCF fields (including repeater `claims`) in the format SCF expects

…but I didn’t include it inline yet because I don’t want to guess your exact CPT slugs/capabilities beyond what’s in your SCF export (I *can* still make a solid default version without guessing if you’re fine with “content / organization / contributor” as the slugs).

[1]: https://developer.wordpress.org/rest-api%2Fextending-the-rest-api%2Fadding-rest-api-support-for-custom-content-types%2F/ "https://developer.wordpress.org/rest-api%2Fextending-the-rest-api%2Fadding-rest-api-support-for-custom-content-types%2F/"
[2]: https://developer.wordpress.org/rest-api/using-the-rest-api/authentication/ "https://developer.wordpress.org/rest-api/using-the-rest-api/authentication/"
[3]: https://developer.wordpress.org/secure-custom-fields/ "https://developer.wordpress.org/secure-custom-fields/"
[4]: https://www.advancedcustomfields.com/resources/wp-rest-api-integration/ "https://www.advancedcustomfields.com/resources/wp-rest-api-integration/"
