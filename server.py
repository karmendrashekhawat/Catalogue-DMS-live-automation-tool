"""
DealerCenter Media Uploader — Spyne
Local automation: for every account folder, log in (auto MFA from Gmail),
then for every VIN folder: filter the VIN, open the record, clear old photos,
upload the folder's images, save & close, reset, next VIN.

Everything runs on your machine. Credentials live only in .env.local (git-ignored).
"""

import os
import re
import sys
import json
import time
import threading
import unicodedata
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# ── config / env ────────────────────────────────────────────────────────────
BASE = Path(__file__).parent

# load .env.local (no dependency required, but python-dotenv is used if present)
def _load_env():
    env_path = BASE / ".env.local"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
        return
    except Exception:
        pass
    # minimal fallback parser
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

_load_env()

def _cfg():
    try:
        return json.loads((BASE / "config.json").read_text())
    except Exception:
        return {}

CFG          = _cfg()
LOGIN_URL    = CFG.get("login_url", "https://app.dealercenter.net/apps/shell/reports/home")
PORT         = int(CFG.get("port", 7433))
IMG_EXTS     = set(CFG.get("image_extensions", [".jpg", ".jpeg", ".png"]))
SAVE_RETRIES = int(CFG.get("save_retry_attempts", 6))
OTP_TIMEOUT  = int(CFG.get("otp_timeout_seconds", 120))

GMAIL_ADDR   = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_PASS   = os.environ.get("GMAIL_APP_PASSWORD", "")


def load_accounts_credentials():
    """Read DC_ACCOUNT_n_{NAME,USER,PASS} from env into {normalized_name: {...}}."""
    creds = {}
    for i in range(1, 26):
        name = os.environ.get(f"DC_ACCOUNT_{i}_NAME")
        user = os.environ.get(f"DC_ACCOUNT_{i}_USER")
        pw   = os.environ.get(f"DC_ACCOUNT_{i}_PASS")
        if name and user and pw:
            creds[_norm(name)] = {"name": name, "user": user, "pass": pw}
    return creds


def _norm(s: str) -> str:
    """Case/space-insensitive key so 'Capitol of smithfield' == 'Capitol of Smithfield'."""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", s).strip().lower()


# ── shared state (polled by the UI) ─────────────────────────────────────────
state = {
    "status": "idle",           # idle | running | otp_wait | done | error | stopped
    "step": 0,
    "log": [],
    "accounts": [],             # [{name, user, total, done, failed, current_vin, status}]
    "current_account": None,
    "otp_prompt": None,         # message shown when we fall back to manual OTP
}
stop_flag       = threading.Event()
otp_manual_evt  = threading.Event()
otp_manual_code = {"code": None}
worker          = None


def log(kind, msg):
    entry = {"t": time.strftime("%H:%M:%S"), "kind": kind, "msg": str(msg)}
    state["log"].append(entry)
    print(f"[{entry['t']}] [{kind.upper():5}] {msg}")


def set_step(n):
    state["step"] = n


def safe_wait(page, timeout=15000, settle=2.0):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass
    time.sleep(settle)


# ── data folder scanning ─────────────────────────────────────────────────────
def scan_root(root: Path, creds: dict):
    """
    root/
      <Account Name>/
        <VIN>/ *.jpg
    Returns list of accounts with matched credentials + their VIN folders.
    """
    accounts = []
    for acc_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("__")):
        key = _norm(acc_dir.name)
        cred = creds.get(key)
        vins = []
        for vin_dir in sorted(p for p in acc_dir.iterdir() if p.is_dir() and not p.name.startswith("__")):
            imgs = sorted(f for f in vin_dir.iterdir()
                          if f.is_file() and f.suffix.lower() in IMG_EXTS)
            if imgs:
                vins.append({"vin": vin_dir.name, "images": [str(f) for f in imgs]})
        accounts.append({
            "folder_name": acc_dir.name,
            "cred": cred,
            "vins": vins,
        })
    return accounts


# ── DealerCenter page actions ────────────────────────────────────────────────
def _click_first(page, selectors, timeout=5000, label=""):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.click(timeout=timeout)
            if label:
                log("ok", label)
            return True
        except Exception:
            continue
    return False


def dc_login(page, user, pw, after_epoch):
    """Fill username/password, click Continue, then satisfy MFA."""
    set_step(2)
    log("info", f"Opening DealerCenter login for {user} ...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    safe_wait(page, 30000, 2.5)

    # username
    filled_u = False
    for sel in ['input[name*="user" i]', 'input[placeholder*="user" i]',
                'input[id*="user" i]', 'input[type="text"]']:
        try:
            page.fill(sel, user, timeout=6000)
            filled_u = True
            break
        except Exception:
            continue
    # password
    filled_p = False
    for sel in ['input[type="password"]', 'input[name*="pass" i]',
                'input[placeholder*="pass" i]']:
        try:
            page.fill(sel, pw, timeout=6000)
            filled_p = True
            break
        except Exception:
            continue
    if not (filled_u and filled_p):
        log("warn", "Could not auto-fill login — fill it manually in the browser.")

    _click_first(page, ['button:has-text("Continue")', 'button[type="submit"]',
                         'button:has-text("Login")', 'button:has-text("Sign in")'],
                 label="Clicked Continue")
    safe_wait(page, 20000, 3.0)

    # ── MFA ──────────────────────────────────────────────────────────────────
    set_step(3)
    otp = None
    if GMAIL_ADDR and GMAIL_PASS:
        log("info", "Fetching MFA code from Gmail ...")
        try:
            from otp_reader import get_otp
            otp = get_otp(GMAIL_ADDR, GMAIL_PASS, after_epoch,
                          timeout=OTP_TIMEOUT, log=log)
        except Exception as e:
            log("warn", f"Auto-OTP failed: {e}")

    if not otp:
        # manual fallback — pause and let the user type it in the UI
        state["status"] = "otp_wait"
        state["otp_prompt"] = f"Enter the MFA code emailed for '{user}', then click Submit OTP."
        log("warn", "Waiting for MFA code from the UI (manual fallback) ...")
        otp_manual_evt.clear()
        otp_manual_code["code"] = None
        otp_manual_evt.wait()
        state["status"] = "running"
        state["otp_prompt"] = None
        otp = otp_manual_code["code"]
        if stop_flag.is_set():
            return False

    if not otp:
        log("error", "No MFA code available.")
        return False

    # type the code — DealerCenter may use one box or 6 single-digit boxes
    typed = False
    for sel in ['input[name*="otp" i]', 'input[placeholder*="code" i]',
                'input[autocomplete="one-time-code"]', 'input[type="tel"]']:
        try:
            page.fill(sel, otp, timeout=4000)
            typed = True
            break
        except Exception:
            continue
    if not typed:
        # split boxes: type digit by digit into the visible short inputs
        try:
            boxes = [b for b in page.locator('input[maxlength="1"]').all()]
            if len(boxes) >= len(otp):
                for b, d in zip(boxes, otp):
                    b.fill(d)
                typed = True
        except Exception:
            pass
    if not typed:
        # last resort: focus body and type
        try:
            page.keyboard.type(otp, delay=80)
            typed = True
        except Exception:
            pass

    _click_first(page, ['button:has-text("Verify")', 'button:has-text("Submit")',
                         'button:has-text("Continue")', 'button[type="submit"]'],
                 label="Submitted MFA code")
    safe_wait(page, 40000, 4.0)
    log("ok", f"Logged in: {user}")
    return True


def dc_open_active_inventory(page):
    set_step(4)
    log("info", "Opening Active Inventory ...")
    # try clicking the count / tile labelled "Active Inventory"
    clicked = _click_first(page, [
        'text=Active Inventory',
        ':text("Active Inventory")',
        'a:has-text("Active Inventory")',
    ], label="Clicked Active Inventory")
    if not clicked:
        # fallback: the report opens at this route
        try:
            page.goto("https://app.dealercenter.net/apps/shell/reports/custom/"
                      "inventoryreport/active-inventory-report?inventorystatus=0",
                      wait_until="domcontentloaded", timeout=60000)
            log("ok", "Navigated to inventory report (fallback URL)")
        except Exception as e:
            log("warn", f"Could not open inventory: {e}")
    safe_wait(page, 30000, 3.0)


def dc_filter_vin(page, vin):
    set_step(5)
    # the inventory filter field placeholder is "Stock# or VIN#"
    filled = False
    for sel in ['input[placeholder*="Stock" i]', 'input[placeholder*="VIN" i]']:
        try:
            box = page.locator(sel).first
            box.click(timeout=4000)
            box.fill("", timeout=2000)
            box.fill(vin, timeout=3000)
            filled = True
            break
        except Exception:
            continue
    if not filled:
        log("warn", f"Could not find Stock#/VIN# field for {vin}")
        return False
    log("ok", f"Entered VIN: {vin}")
    _click_first(page, ['button:has-text("Run")'], label="Clicked Run")
    safe_wait(page, 25000, 3.0)
    return True


def dc_open_vehicle(page, vin):
    # after Run there should be one result; click its title link
    for sel in [
        'a:has-text("Vin")',                       # generic
        f'text=/{re.escape(vin[-6:])}/',           # tail of VIN often shown bold
        '.report-list a', 'table a', 'a[href*="vehicle"]',
    ]:
        try:
            page.locator(sel).first.click(timeout=5000)
            log("ok", "Opened vehicle record")
            safe_wait(page, 30000, 3.0)
            return True
        except Exception:
            continue
    # fallback: click the first vehicle-title-looking link (starts with a 4-digit year)
    try:
        links = page.locator("a").all()
        for lk in links:
            txt = (lk.inner_text(timeout=500) or "").strip()
            if re.match(r"^(19|20)\d{2}\s+\S+", txt):
                lk.click(timeout=4000)
                log("ok", f"Opened vehicle record ({txt[:30]})")
                safe_wait(page, 30000, 3.0)
                return True
    except Exception:
        pass
    log("error", f"Could not open the vehicle record for {vin}")
    return False


def dc_open_media_tab(page):
    _click_first(page, ['[role="tab"]:has-text("Media")', 'text=Media',
                        'button:has-text("Media")', 'a:has-text("Media")'],
                 label="Opened Media tab")
    safe_wait(page, 15000, 2.5)


def dc_remove_all_photos(page):
    """Click 'Remove All' then confirm 'Yes'. Skips silently if no photos."""
    # If Photos (0), the Remove All control isn't present -> nothing to do.
    try:
        if page.locator('text=/Photos \\(0\\)/').first.is_visible(timeout=2000):
            log("info", "No existing photos — skipping delete")
            return
    except Exception:
        pass

    if not _click_first(page, ['text=Remove All', 'button:has-text("Remove All")',
                               '[aria-label*="remove all" i]']):
        log("info", "Remove All not found — assuming no photos")
        return
    log("info", "Clicked Remove All")
    time.sleep(1.0)
    # confirm dialog: "Are you sure you would like to delete all images?" -> Yes
    _click_first(page, ['[role="dialog"] button:has-text("Yes")',
                        '.modal button:has-text("Yes")',
                        'button:has-text("Yes")'],
                 label="Confirmed delete (Yes)")
    safe_wait(page, 20000, 3.0)


def dc_upload_images(page, images):
    set_step(6)
    log("info", f"Uploading {len(images)} image(s) ...")
    # Preferred: set files straight onto the hidden <input type=file>
    done = False
    for sel in ['input[type="file"]']:
        try:
            inp = page.locator(sel).first
            inp.set_input_files(images, timeout=15000)
            done = True
            log("ok", f"Set {len(images)} files on upload input")
            break
        except Exception:
            continue
    if not done:
        # Fallback: click Browse/Upload to open a file chooser
        try:
            with page.expect_file_chooser(timeout=10000) as fc:
                _click_first(page, ['text=Browse', 'button:has-text("Upload")',
                                    'text=Upload'])
            fc.value.set_files(images)
            done = True
            log("ok", f"Set {len(images)} files via file chooser")
        except Exception as e:
            log("error", f"Upload failed: {e}")
            return False

    log("info", "Waiting for uploads to finish ...")
    safe_wait(page, 120000, 5.0)
    return True


def dc_save_and_close(page):
    set_step(7)
    for _ in range(SAVE_RETRIES):
        if stop_flag.is_set():
            return
        # dismiss any interim OK/Okay popup
        for ok in ("Okay", "OK", "Ok"):
            try:
                b = page.locator(f'button:has-text("{ok}")').first
                if b.is_visible(timeout=1000):
                    b.click()
                    time.sleep(1.0)
            except Exception:
                pass
        if _click_first(page, ['button:has-text("Save and Close")',
                               'button:has-text("Save & Close")']):
            log("ok", "Save and Close")
            safe_wait(page, 30000, 3.0)
            return
        time.sleep(1.5)
    log("warn", "Save and Close button not confirmed")


def dc_reset_filter(page):
    _click_first(page, ['button:has-text("Reset")'], label="Reset filter")
    safe_wait(page, 15000, 2.0)


# ── orchestration ────────────────────────────────────────────────────────────
def run_automation(root_str):
    root = Path(root_str)
    stop_flag.clear()
    state.update({"status": "running", "step": 0, "log": [], "accounts": [],
                  "current_account": None, "otp_prompt": None})

    creds = load_accounts_credentials()
    if not creds:
        log("error", "No accounts in .env.local (DC_ACCOUNT_1_NAME/USER/PASS ...)")
        state["status"] = "error"; return
    if not (GMAIL_ADDR and GMAIL_PASS):
        log("warn", "No Gmail configured — MFA will fall back to manual entry each login.")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("error", "Run: pip install playwright && python -m playwright install chromium")
        state["status"] = "error"; return

    accounts = scan_root(root, creds)
    # build UI-visible account list
    for a in accounts:
        state["accounts"].append({
            "name": a["folder_name"],
            "user": a["cred"]["user"] if a["cred"] else None,
            "matched": bool(a["cred"]),
            "total": len(a["vins"]),
            "done": 0, "failed": 0,
            "current_vin": None,
            "status": "pending" if a["cred"] and a["vins"] else "skipped",
        })
    log("ok", f"Found {len(accounts)} account folder(s)")
    for a, s in zip(accounts, state["accounts"]):
        if not a["cred"]:
            log("warn", f"  '{a['folder_name']}' — no matching credentials, will skip")
        else:
            log("info", f"  '{a['folder_name']}' -> {len(a['vins'])} VIN(s)")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--start-maximized"])

        for idx, acc in enumerate(accounts):
            if stop_flag.is_set():
                break
            ui = state["accounts"][idx]
            if not acc["cred"]:
                continue
            if not acc["vins"]:
                ui["status"] = "skipped"
                log("warn", f"'{acc['folder_name']}' has no VIN folders — skipping")
                continue

            state["current_account"] = acc["folder_name"]
            ui["status"] = "running"
            log("info", f"══════ ACCOUNT: {acc['folder_name']} ({acc['cred']['user']}) ══════")

            # fresh context per account = clean session + predictable login
            ctx = browser.new_context(no_viewport=True)
            page = ctx.new_page()
            page.set_default_timeout(60000)

            try:
                if not dc_login(page, acc["cred"]["user"], acc["cred"]["pass"], time.time()):
                    ui["status"] = "error"
                    log("error", f"Login failed for {acc['folder_name']} — skipping account")
                    ctx.close(); continue

                dc_open_active_inventory(page)

                for v in acc["vins"]:
                    if stop_flag.is_set():
                        break
                    vin, images = v["vin"], v["images"]
                    ui["current_vin"] = vin
                    log("info", f"── VIN {vin} ({len(images)} images) ──")
                    try:
                        if not dc_filter_vin(page, vin):
                            ui["failed"] += 1; continue
                        if not dc_open_vehicle(page, vin):
                            ui["failed"] += 1
                            dc_reset_filter(page); continue
                        dc_open_media_tab(page)
                        dc_remove_all_photos(page)
                        if not dc_upload_images(page, images):
                            ui["failed"] += 1
                            dc_reset_filter(page); continue
                        dc_save_and_close(page)
                        dc_reset_filter(page)
                        ui["done"] += 1
                        log("ok", f"✓ {vin} complete")
                    except Exception as e:
                        ui["failed"] += 1
                        log("error", f"✗ {vin} failed: {e}")
                        try: dc_reset_filter(page)
                        except Exception: pass
                    finally:
                        ui["current_vin"] = None

                ui["status"] = "done"
            except Exception as e:
                ui["status"] = "error"
                log("error", f"Account {acc['folder_name']} errored: {e}")
            finally:
                ctx.close()
                state["current_account"] = None

        browser.close()

    total_done   = sum(a["done"] for a in state["accounts"])
    total_failed = sum(a["failed"] for a in state["accounts"])
    log("ok", f"━━━ Finished — {total_done} uploaded, {total_failed} failed ━━━")
    state["status"] = "done"; state["step"] = 8


# ── tiny HTTP server for the UI ──────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence default logging
        pass

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _file(self, path, ct):
        try:
            body = Path(path).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        except Exception:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._file(BASE / "ui" / "index.html", "text/html")
        elif path == "/state":
            self._json(state)
        elif path == "/stop":
            stop_flag.set(); otp_manual_evt.set()
            state["status"] = "stopped"; self._json({"ok": True})
        elif path == "/reset":
            state.update({"status": "idle", "step": 0, "log": [], "accounts": [],
                          "current_account": None, "otp_prompt": None})
            stop_flag.clear(); otp_manual_evt.clear(); self._json({"ok": True})
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        if path == "/start":
            folder = (body.get("folder") or "").strip()
            if not folder or not Path(folder).is_dir():
                self._json({"ok": False, "error": f"Folder not found: {folder}"}, 400); return
            global worker
            if worker and worker.is_alive():
                self._json({"ok": False, "error": "Already running"}); return
            worker = threading.Thread(target=run_automation, args=(folder,), daemon=True)
            worker.start(); self._json({"ok": True})
        elif path == "/otp":
            otp_manual_code["code"] = (body.get("code") or "").strip()
            otp_manual_evt.set(); self._json({"ok": True})
        else:
            self.send_error(404)


def open_browser():
    time.sleep(1.2)
    try:
        import webbrowser
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception:
        pass


if __name__ == "__main__":
    print(f"\n  DealerCenter Media Uploader — Spyne")
    print(f"  Control panel:  http://localhost:{PORT}\n")
    srv = HTTPServer(("localhost", PORT), Handler)
    threading.Thread(target=open_browser, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
