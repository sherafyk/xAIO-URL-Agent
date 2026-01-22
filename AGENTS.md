# Agent Tasks

## WordPress: create “content” CPT posts + upload SCF fields as metadata

### 1) One-time WordPress setup

#### 1.1 Use an Application Password (recommended)

Even if you already have a user+password, use an **Application Password** for REST calls (it’s the intended path for API clients). WordPress documents using Application Passwords with REST requests and Basic Authorization headers. ([WordPress Developer Resources][1])

#### 1.2 Confirm your REST endpoints exist

Because your CPTs are REST-enabled, you should be able to hit (examples):

* `GET /wp-json/wp/v2/content`
* `GET /wp-json/wp/v2/contributor`
* `GET /wp-json/wp/v2/organization`

WordPress’ REST API uses the `show_in_rest` flag to expose post types in `/wp/v2/...`. ([WordPress Developer Resources][2])

#### 1.3 Topics taxonomy: resolve the slug mismatch now

Your SCF export defines a taxonomy slug **`topic`** (REST-enabled), 
but your **content** post type lists **`xaio_topic_tax`** in its taxonomies list. 

So: your pipeline should not guess. Verify on the site via:

* `GET /wp-json/wp/v2:contentReference[oaicite:4]{index=4}mies`)
* `GET /wp-json/wp/v2/taxonomies` (find your “Topics” taxonomy slug)

(If you sfully” send topics that never attach.)

---

### 2) Best architecture: add one WP ingest endpoint (so the pipeline stays simple)

**Why:** In your SCF export, most Content field groups are `show_in_rest: 0` (meaning they won’t reliably show up / be writable via the standard `/wp/v2/content` schema without extra work). So instead of fighting REST meta exposure, do a tiny WP-side “ingest” endpoint that *writes posts + SCF fields server-side*.

#### 2.1 Create an MU-plugin: `wp-content/mu-plugins/xaio-ingest.php`

```php
<?php
/**
 * Plugin Name: xAIO Ingest API
 * Description: Ingests xAIO pipeline JSON and upserts content/contributor/organization + topics.
 */

add_action('rest_api_init', function () {
  register_rest_route('xaio/v1', '/ingest', [
    'methods'  => 'POST',
    'permission_callback' => function () {
      return current_user_can('edit_posts');
    },
    'callback' => 'xaio_ingest_handler',
  ]);
});

function xaio_ingest_handler(WP_REST_Request $req) {
  $p = $req->get_json_params();

  $xaio_id = isset($p['xaio_id']) ? sanitize_text_field($p['xaio_id']) : '';
  if (!$xaio_id) return new WP_REST_Response(['error' => 'xaio_id is required'], 400);

  // REQUIRED by your SCF schema for Content
  $canonical_url = isset($p['canonical_url']) ? esc_url_raw($p['canonical_url']) : '';
  if (!$canonical_url) return new WP_REST_Response(['error' => 'canonical_url is required'], 400);

  // In your schema, content_mode is required; default to "url"
  $content_mode = isset($p['content_mode']) ? sanitize_text_field($p['content_mode']) : 'url';

  $content_title = isset($p['content_title']) ? sanitize_text_field($p['content_title']) : '';
  $post_body     = isset($p['post_body']) ? (string)$p['post_body'] : '';

  // --- Upsert contributor stub ---
  $contrib_name = isset($p['contributor_name']) ? sanitize_text_field($p['contributor_name']) : '';
  $contrib_id = 0;
  if ($contrib_name) {
    $contrib_slug = sanitize_title($contrib_name);
    $contrib_id = xaio_upsert_post('contributor', $contrib_slug, $contrib_name, 'draft');

    // Your schema requires xaio_code; generate deterministically from slug
    xaio_update_scf_field($contrib_id, 'display_name', $contrib_name);
    xaio_update_scf_field($contrib_id, 'xaio_code', $contrib_slug);
  }

  // --- Upsert organization stub ---
  $org_name   = isset($p['org_name']) ? sanitize_text_field($p['org_name']) : '';
  $org_domain = isset($p['org_domain']) ? sanitize_text_field($p['org_domain']) : '';
  $org_id = 0;
  if ($org_name || $org_domain) {
    $org_slug = sanitize_title($org_domain ?: $org_name);
    $org_id = xaio_upsert_post('organization', $org_slug, ($org_name ?: $org_domain), 'draft');

    // Your schema requires org_website_primary; normalize from domain if needed
    $org_url = isset($p['org_website_primary']) ? esc_url_raw($p['org_website_primary']) : '';
    if (!$org_url && $org_domain) $org_url = 'https://' . preg_replace('/^https?:\/\//', '', $org_domain);

    if ($org_url) xaio_update_scf_field($org_id, 'org_website_primary', $org_url);
    if ($org_domain) xaio_update_scf_field($org_id, 'primary_domain', $org_domain);
  }

  // --- Upsert content ---
  $content_slug = sanitize_title($xaio_id);
  $content_id = xaio_upsert_post('content', $content_slug, $xaio_id, 'draft');

  // Store the body as the WP post content (so themes/editors can render it)
  wp_update_post([
    'ID' => $content_id,
    'post_content' => $post_body,
  ]);

  // Map your SCF fields (names come from your SCF export)
  xaio_update_scf_field($content_id, 'xaio_id', $xaio_id);
  xaio_update_scf_field($content_id, 'canonical_url', $canonical_url);
  xaio_update_scf_field($content_id, 'content_mode', $content_mode);
  if ($content_title) xaio_update_scf_field($content_id, 'url_content_title', $content_title);

  // Relationships (SCF Post Object fields)
  if ($org_id) xaio_update_scf_field($content_id, 'primary_organization', $org_id);
  if ($contrib_id) xaio_update_scf_field($content_id, 'related_contributors', [$contrib_id]);

  // Topics / tags (taxonomy)
  $topics = isset($p['topics']) && is_array($p['topics']) ? $p['topics'] : [];
  xaio_set_topics($content_id, $topics);

  return new WP_REST_Response([
    'ok' => true,
    'content_id' => $content_id,
    'contributor_id' => $contrib_id,
    'organization_id' => $org_id,
    'xaio_id' => $xaio_id,
  ], 200);
}

function xaio_upsert_post($post_type, $slug, $title, $status='draft') {
  $existing = get_page_by_path($slug, OBJECT, $post_type);
  if ($existing && !is_wp_error($existing)) {
    wp_update_post(['ID' => $existing->ID, 'post_title' => $title]);
    return (int)$existing->ID;
  }

  $id = wp_insert_post([
    'post_type'   => $post_type,
    'post_title'  => $title,
    'post_name'   => $slug,
    'post_status' => $status,
  ], true);

  if (is_wp_error($id)) {
    throw new Exception($id->get_error_message());
  }
  return (int)$id;
}

function xaio_update_scf_field($post_id, $field_name, $value) {
  // Secure Custom Fields is a fork of ACF; if ACF-style helpers exist, prefer them.
  if (function_exists('update_field')) {
    update_field($field_name, $value, $post_id);
  } else {
    update_post_meta($post_id, $field_name, $value);
  }
}

function xaio_set_topics($post_id, $topics) {
  if (!$topics) return;

  // Robust: pick the first taxonomy that looks like your topics taxonomy.
  $taxes = get_object_taxonomies('content');
  $topic_tax = in_array('xaio_topic_tax', $taxes, true) ? 'xaio_topic_tax' : (in_array('topic', $taxes, true) ? 'topic' : 'topic');

  $term_ids = [];
  foreach ($topics as $t) {
    $t = sanitize_text_field($t);
    if (!$t) continue;
    $exists = term_exists($t, $topic_tax);
    if (!$exists) $exists = wp_insert_term($t, $topic_tax);
    if (is_array($exists) && isset($exists['term_id'])) $term_ids[] = (int)$exists['term_id'];
    else if (is_int($exists)) $term_ids[] = $exists;
  }

  if ($term_ids) wp_set_object_terms($post_id, $term_ids, $topic_tax, false);
}
```

**Why this works for your schema:**

* Your **Content** schema requires `canonical_url` and `content_mode`.  
* You added `xaio_id` and `url_content_title` as identity fields. 
* Your **Content relationships** fields are `primary_organization:contentReference[oaicite:9]{index=9}:contentReference[oaicite:10]{index=10}:contentReference[oaicite:11]{index=11}- Your **Contributor** schema requires `display_name`and`xaio_code`om a slug). 
* Your **Organization** schema requires `org_websi from a domain when needed). 
* SCF is a fork of ACF, so using `update_field()` wheo ensure SCF stores values the way it expects.

---

### 3) Pipeline step: call the ingest ent item)

#### 3.1 Example payload your pipeline should send

```json
{
  "xaio_id": "NYT_2026_01_22_foo",
  "canonical_url": "https://example.com/article",
  "content_mode": "url",
  "content_title": "The human-readable article title",
  "post_body": "<p>Full article body…</p>",
  "contributor_name": "Jane Doe",
  "org_name": "Example News",
  "org_domain": "example.com",
  "topics": ["AI Policy", "Compute", "Energy"]
}
```

#### 3.2 Example Python call (requests)

```python
import base64, requests

wp_base = "https://YOUR_SITE"
user = "api-user"
app_password = "xxxx xxxx xxxx xxxx xxxx xxxx"  # from WP profile screen

token = base64.b64encode(f"{user}:{app_password}".encode()).decode()
headers = {
    "Authorization": f"Basic {token}",
    "Content-Type": "application/json",
}

payload = {...}  # the JSON above
r = requests.post(f"{wp_base}/wp-json/xaio/v1/ingest", json=payload, headers=headers, timeout=30)
r.raise_for_status()
print(r.json())
```

(WordPress’ official Application Password guidance uses this Basic Authorization pattern for REST calls.) ([WordPress Developer Resources][1])

---

### 4) Operational details that will save you pain (a.k.a. Thelen being boring on purpose)

#### 4.1 Idempotency: your xAIO ID should upsert, not duplicate

The plugin uses `post_name` (slug) = `xaio_id`, so reruns update the same record.

#### 4.2 Don’t create “required repeater” rows unless you have the data

Your schema makes `claim_text`/`claim_type` required **per row**, 
so: send an **empty array** for the repeater until claim extraction is ready, rather than sending a half-filled row.

#### 4.3 Topics: treat taxonomy slug as config

Because of the `topic` vs `xaio_topic_tax` mismatch, keep a pipeline config value (or rely on the plugin’s detection) so you can change it oncede.  

---


## 1) Reduce reliance on Google Sheets (and stop rate limits)

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

