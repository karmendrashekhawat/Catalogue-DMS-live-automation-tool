# DMS Media Suite — Spyne

One local control panel, three tools:

1. **DMS Live Catalogue** — bulk VIN photo replacement across **DealerCenter,
   VinMotion, and vAuto**. Async architecture: **max 3 Chrome windows total,
   one per DMS**, each processing one account at a time, with up to **3 real
   browser tabs** (not separate Chrome processes) working different VINs at
   once. Max concurrency: 3 windows × 3 tabs = 9 VIN operations in flight,
   using only 3 Chrome processes.
2. **VIN Organizer** — sorts failed VINs into `Master\Enterprise\VIN`,
   **live off Combined Activity** (no upload needed) or from an uploaded log.
   Failed VINs are also moved **automatically** the instant they exhaust
   their retries — the Organizer button is for manual re-runs.
3. **CSV Generator** — counts images in every leaf folder under a path.

Everything runs locally, driving real Chrome windows with Playwright's
**async** API. Credentials live only in `.env.local` (git-ignored).

---

## Why this fixes the "hangs my laptop" problem

The previous version opened a **separate Chrome process per VIN tab, per
login group** — with a few sliders turned up, that's dozens of real browser
processes at once. This version uses Playwright's async API instead of the
sync API, so multiple VIN tabs are genuine browser **tabs inside one Chrome
window**, driven concurrently from one event loop — not one OS process each.
Total Chrome processes running at any time = **Maximum DMS/Chrome windows**
(default 3, one per DMS type), never more.

---

## One-time setup

1. Install **Python 3.8+** (check "Add Python to PATH" on Windows).
2. Copy `.env.local.example` → `.env.local` and fill in:
   - Each account's username/password, under `DC_ACCOUNT_n_*` (DealerCenter),
     `VM_ACCOUNT_n_*` (VinMotion), or `VA_ACCOUNT_n_*` (vAuto).
   - `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` for the inbox that receives
     verification codes from all three platforms.
3. **Gmail app password:** turn on 2-Step Verification, then
   Google Account → Security → App passwords → generate one for "Mail."

## Run

- **Windows:** double-click `start_windows.bat`
- **Mac/Linux:** `chmod +x start_mac.sh && ./start_mac.sh`

Opens at **http://localhost:7433**.

---

## 1. DMS Live Catalogue

Paste your "Live" data folder and click **Start upload**. Account subfolder
names must match a `DC_ACCOUNT_n_NAME`, `VM_ACCOUNT_n_NAME`, or
`VA_ACCOUNT_n_NAME` entry — mix all three DMS platforms under the same root.

**Architecture (Target Architecture diagram, implemented exactly):**

```
DMS Live Catalogue — max 3 DMS/Chrome windows
        │
   ┌────┴────┬─────────┐
 Chrome #1  Chrome #2  Chrome #3
 (DMS #1)   (DMS #2)   (DMS #3)
   │           │           │
 one account  one account  one account   ← sequential per window,
 at a time    at a time    at a time       "One account per DMS window"
   │           │           │
 TAB 1,2,3   TAB 1,2,3   TAB 1,2,3        ← real browser tabs, async API
 VIN a,b,c   VIN d,e,f   VIN g,h,i
```

If a DMS has multiple logins (e.g. two different DealerCenter accounts),
they queue and run one at a time in that DMS's single Chrome window — they
don't get their own window each. Turn off **One account per DMS window** in
Settings to relax this and let accounts pool freely across all window slots
instead (still capped at **Maximum DMS/Chrome windows** total).

**Rooftop switching (Charlie Clark, Capitol, etc.) — fixed:**
- Already on the right rooftop → nothing happens, no wasted clicks.
- Login only has **one** rooftop → treated as already correct. It no longer
  tries to open a switcher that isn't there and skip the account.
- **2+** rooftops genuinely available → switches and verifies, as before.

**Live status strip** shows Chrome windows in use, VIN tabs running, queued /
running / completed / failed counts, and which DMS + account is active right
now — all updating live.

**Combined Activity 1** is the original flat activity feed, completely
unchanged in layout, log style, colors, and behavior.

**Combined Activity 2** is new: a structured table — DMS, Account, Chrome,
Tab, VIN, Enterprise, Rooftop, Current Action, Status, Start, Duration,
Retry, Error — with its own live counters (Chrome usage, VIN tabs, Running,
Queued, Completed, Failed). Both panels update independently and simultaneously.

**Download** — a dropdown next to Combined Activity 1 lets you download the
combined feed, or any single Chrome window's own activity, as a `.txt` file.
There are no more separate per-account log panels cluttering the page —
Combined Activity is the only activity monitor.

---

## 2. VIN Organizer

**Live & Manual Process** (top of the page): reads directly off the current
run's Combined Activity — no file to upload. Paste a path (or leave blank to
use the current run's data folder automatically), optionally tick **Preview
only**, and click **Process Now (Live)**.

Auto-move already runs in the background the moment a VIN exhausts its
retries during a live run, so this button is mainly for: re-running on
demand, catching VINs from earlier in a long run, or when auto-move is
turned off in Settings.

The old upload-a-log-file workflow is still there, collapsed under "Or
analyze an uploaded log file instead" — useful for logs saved from previous
sessions.

---

## 3. CSV Generator

Unchanged — paste a path, it writes `<folder name>_image_counts.csv` there
and shows the results in a table you can also download directly.

---

## Settings

**DMS Live Catalogue Settings:**
- Maximum DMS / Chrome windows (1–5, default **3**)
- Maximum duplicate VIN tabs per Chrome (1–5, default **3**)
- Maximum total VIN concurrency (default **9**)
- One account per DMS window (default **ON**)
- Headless mode (default **OFF**)
- Auto-queue when slots are full (default **ON**)
- Browser reuse (default **ON**)
- Auto-retry failed VINs (default **ON**) + Retry count (default **2**)
- Auto-move failed VINs to Master (default **ON**)
- Save/upload retry attempts, OTP wait timeout, image extensions

**Theme:** 6 options (Midnight, Slate, Sunset, Forest, Ocean, Light).

All settings save to `settings.json` and take effect on the next run — no
restart needed.

---

## How verification codes are handled

Each Chrome window has its own OTP box — several windows can be waiting on
separate verification codes at the same time. Auto-fetch from Gmail is tried
first; if that fails, type the code into that window's OTP box in the UI.

---

## Files

```
server.py             Flask app + async Playwright automation (3 DMS) + Organizer + CSV Generator
templates/index.html   control panel UI (themes, 3 tools, settings, dual activity panels)
otp_reader.py          Gmail IMAP -> verification code
config.json            non-secret settings (URLs, port)
settings.json           user settings — editable from the UI
.env.local               YOUR secrets (git-ignored — never commit)
.env.local.example      template
start_windows.bat / start_mac.sh   launchers
```

---

## Network Recon (experimental) — evidence before we build the HTTP-direct layer

Before replacing any part of the browser automation with direct HTTP calls,
`recon.py` captures the *actual* network traffic one account's DMS makes for
login, VIN search, delete-photos, upload, and save — so the HTTP-direct layer
gets designed against real evidence, not guesses. Test one account first.

```
python recon.py --account "International Auto" --data-root "C:\Live" --vin 1FMEU73EX8UB24500
```

Add `--manual-seconds 120` to also leave the browser open afterward for you
to click around by hand — every request is still captured. Use
`--skip-automated --manual-seconds 120` to skip the automated cycle entirely
and only capture what you do manually.

Two files land next to the script:
- `recon_<dms>_<account>_<timestamp>.json` — every request/response, full detail
- `recon_<dms>_<account>_<timestamp>_summary.txt` — just the likely delete /
  upload / save / search calls, which is what actually matters for designing
  the direct-HTTP layer

Credentials and Gmail OTP auto-fetch work exactly like the main app. If a
manual OTP is needed and Gmail isn't configured, this script prompts for it
right in the terminal.

**What to look for in the summary:** repeatable REST/GraphQL-style endpoints
under `/api/...` with predictable JSON bodies are the good case — those are
what get replayed directly with `httpx` instead of clicking through the UI.
Endpoints that look like signed/expiring upload URLs, or requests that only
succeed with a freshly-rendered anti-CSRF token pulled from the page, are the
signal to keep that specific step on browser automation and only fast-path
the rest.

**Security note:** the `_summary.txt` file redacts every secret value it
shows. The `.json` file does not — it carries full cookie/token values on
purpose, because that's what's needed to actually build the HTTP layer. Treat
it like a password: keep it local, never paste its raw contents anywhere.

---

## Direct-HTTP DealerCenter pipeline (experimental, DealerCenter only)

Built from two independent recon captures against a real DealerCenter
session. **Not wired into the main automation yet** — validate it against one
real VIN with `test_dc_http.py` first.

### What's confirmed from evidence (not guessed)

- **Auth**: full cookie jar + an `Authorization: Bearer <JWT>` header
  (confirmed to be a *different* token than any cookie — has to be harvested
  from a live authenticated request, not derived) + `X-XSRF-TOKEN` header
  (confirmed byte-identical to the `XSRF-TOKEN` cookie — classic double-submit
  CSRF) + `dc-location` / `dc-user` custom headers. **Important**: several
  early post-login calls (`validaterefreshtoken`, `getuserinfo`, consent
  checks) carry a bearer token but NOT `dc-location`/`dc-user` — grabbing one
  of those produces a session that 404s against the inventory API. The
  harvester specifically waits for a request that has all three; if nothing
  complete ever shows up before the timeout it falls back to the most
  complete partial match and flags `session.incomplete = True` rather than
  silently handing back something that won't route correctly.
- **Delete existing photos** — `Document/DeleteFiles`, array of GUIDs.
- **Get upload slots** — `Document/GetUploadInfoAndSAS`, returns signed URLs.
- **Upload** — a plain `PUT` straight to Azure Blob Storage (Microsoft's own
  API, not DealerCenter's — the most stable part of this whole pipeline).
- **Register each image** — `Document/AddImageDetails`, confirmed 1:1 per image.
- **Reorder** — `InventoryDocument/ReorderImages`.
- **Fetch existing photo list** — `Document/GetInventoryImages`: confirmed to
  return a plain JSON array with a lowercase `id` field on each item — not a
  guess, this one was directly visible in the capture.
- **Save** — `Inventory/SaveInventory`. Confirmed to use optimistic
  concurrency (an `optimisticLockField` that must round-trip). This module
  never hand-builds that payload: it always does a fresh `LoadInventoryById`
  immediately before saving and echoes the record back unchanged, then
  verifies the lock field actually advanced before calling it a success.

### Fixed after the first real test run

Two real bugs surfaced testing this against a live account, both now fixed:

1. **Wrong URL path.** The first version pointed at
   `/api-gateway/inventory/InventoryCustomReport/...` — the real endpoint
   lives under `/api-gateway/report-api/InventoryCustomReport/...`. This, not
   anything about auth, was the actual cause of the first 404.
2. **The session harvester could grab an incomplete header set.** Several
   early post-login calls carry a bearer token but not `dc-location`/
   `dc-user` (lightweight admin/consent checks that fire before the Home
   page's widgets load). Grabbing one of those produced a session that
   404'd against the inventory API regardless of the URL being right. The
   harvester now specifically waits for a request with all three headers
   present, falling back to the most complete partial match (flagged via
   `session.incomplete`) only if nothing complete ever appears.

### There IS a dedicated VIN-search filter after all

A closer read of the second bug's fix turned up something better than
expected: `GetInventoryDetailedReport` accepts a
`{"Field": "StockVinSearch", "Value1": "<vin>"}` filter — a real, confirmed,
server-side VIN search. No client-side pagination/row-walking needed; one
filtered call returns just the match.

### What's still genuinely unconfirmed

The exact field names for the VIN and the inventory ID on the returned row.
That endpoint's rows use PascalCase (`Engine`, `AskingPrice`, ...) — a
different casing convention than the vehicle-record endpoints (`id`, `vin`,
camelCase) — and even the VIN-filtered (much smaller) response was still
large enough that both recon captures truncated before reaching those two
fields.

Rather than hardcode a guess, `find_inventory_id_for_vin()` still verifies
the returned row actually contains the VIN, then tries a short list of
plausible ID field names. If none match, it raises `SearchFieldMismatch`
with **the row's actual keys attached** — so if this last guess is wrong,
`test_dc_http.py` tells you exactly what to fix in one line, instead of
failing silently or corrupting something.

### Real-world result — root cause found and fixed

Tested against one live vehicle record: search, load, delete (24 photos),
get-SAS, all 24 blob uploads, all 24 `AddImageDetails` registrations, and
reorder **all succeeded**. `SaveInventory` returned a **400 Bad Request**
("Invalid request", no field-level detail).

Root cause: `recon.py` had been silently replacing any request body over
2000 bytes with just a size note (`<74723 bytes, truncated>`) — so neither
prior recon capture ever actually showed the real `SaveInventory` request
body. Fixed that (same real-prefix approach already used for responses),
re-ran recon, and found the actual shape:

```json
{"changeSource": "web", "inventory": { ...the full vehicle record... }}
```

**The request is wrapped in an envelope.** The original implementation
posted the bare record as the top-level body — exactly the kind of mismatch
that produces a generic "Invalid request" with no field-level errors,
because the model binder can't even match the shape to attempt validation.
`save_inventory()` now wraps the record in `{"changeSource": "web",
"inventory": <record>}` before posting. The response itself is confirmed
**not** wrapped — bare record back, same shape as `LoadInventoryById` —
so only the request needed the fix.

A new test (`test_dc_http_failures.py`) asserts the exact envelope shape on
every call, so this can't silently regress.

### A second, more dangerous bug — one that looked like a success

With the envelope fix, a full run against the same live record reported
**complete success**: 24/24 uploads, save confirmed. But checking the
DealerCenter UI directly afterward showed every one of the 24 photos as a
generic placeholder icon — the photos weren't actually displaying.

Cause: `AddImageDetails` needs an `"eTag"` field, and there are **two
completely different values that both happen to be called "etag"**:
DealerCenter's own pre-generated identifier from `GetUploadInfoAndSAS`
(format `"202608-<fileName>"`) and the real Azure Blob Storage etag returned
by the blob PUT itself (format `"0x8DE7EEDDA59A8B7"`, hex). The code was
sending the real Azure one. Every `AddImageDetails` call still returned 200
— it doesn't validate the etag against the actual blob — so the logs showed
a clean success while the photos silently failed to resolve for display.

Fixed: `AddImageDetails` now always uses DealerCenter's own identifier from
the SAS response, never the blob's real etag. Also fixed while in there: the
uploaded file's `name` field was being built with `path.split("/")[-1]`,
which does nothing useful on a Windows path (no forward slashes to split
on) — it was sending the *entire local file path* as the photo's name,
not just the filename. Both are covered by new assertions in
`test_dc_http.py`.

**The lesson this leaves behind**: a 200 response is not proof a step
actually worked end-to-end for this kind of pipeline. Before trusting this
tool's "success" on a real account, it's worth a habit of manually checking
the vehicle's Media tab once per session — this class of bug (registration
succeeds, display silently fails) won't surface any other way, and neither
`AddImageDetails` nor `SaveInventory` will tell you about it.

### Testing it (same philosophy as recon.py — read first, write only when you say so)

```
# Read-only: finds the record, reports existing photo count, stops there.
python test_dc_http.py --account "International Auto" --data-root "C:\Live" --vin 1FMEU73EX8UB24500

# Only once the numbers above look right — actually deletes/uploads/saves for real.
python test_dc_http.py --account "International Auto" --data-root "C:\Live" --vin 1FMEU73EX8UB24500 --confirm-write
```

### Files

```
dc_http.py         the HTTP client + pipeline (search, delete, upload, register, reorder, save)
test_dc_http.py     standalone CLI tester — logs in via browser, harvests the session, runs the pipeline
```

---

## Security

- `.env.local` holds real credentials and is git-ignored. Never commit or
  share it.
- Only automate accounts you own or are authorized to manage.
- Automating logins/verification codes may be restricted by each platform's
  Terms of Service — confirm you have permission before running.
