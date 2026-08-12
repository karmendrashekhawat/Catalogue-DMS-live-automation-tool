"""
DMS Media Suite — Spyne
Three tools in one control panel:

  1. DMS Live Catalogue — bulk VIN photo replacement across DealerCenter,
     VinMotion, and vAuto. Uses Playwright's ASYNC API so each DMS gets its
     own single Chrome window (max_dms_windows, default 3) that processes
     one account at a time, with up to max_tabs_per_dms real browser TABS
     (not separate browser processes) working different VINs concurrently.
  2. VIN Organizer — sorts failed VINs into Master/Enterprise/VIN, either
     live off the current run's Combined Activity, or from an uploaded log.
  3. CSV Generator — counts images in every leaf folder under a path.

Everything runs on your machine. Credentials live only in .env.local (git-ignored).
"""

import os
import re
import io
import csv
import json
import time
import shutil
import asyncio
import threading
import contextvars
import unicodedata
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, render_template

# ── config / env ────────────────────────────────────────────────────────────
BASE = Path(__file__).parent


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
VM_LOGIN_URL = CFG.get("vinmotion_url", "https://vinmotion.vehicledata.com/Inventory")
VA_LOGIN_URL = CFG.get("vauto_url", "https://provision.vauto.app.coxautoinc.com")
VA_MEDIA_URL = CFG.get("vauto_media_url",
                       "https://provision.vauto.app.coxautoinc.com/Va/Merchandising/MediaManagement.aspx")
PORT         = int(CFG.get("port", 7433))

GMAIL_ADDR   = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_PASS   = os.environ.get("GMAIL_APP_PASSWORD", "")

DC_OTP_SENDER  = "do-not-reply@dealercenter.net"
DC_OTP_SUBJECT = "Your authentication code"
VM_OTP_SENDER  = "msonlineservicesteam@microsoftonline.com"
VM_OTP_SUBJECT = "Dealer Specialties account email verification code"
VA_OTP_SENDER  = "no-reply@signin.coxautoinc.com"
VA_OTP_SUBJECT = "One-time Bridge ID code"

DMS_TYPES = ["dealercenter", "vinmotion", "vauto"]
DMS_LABELS = {"dealercenter": "DealerCenter", "vinmotion": "VinMotion", "vauto": "vAuto"}


# ── settings (user-tunable, persisted to settings.json) ─────────────────────
SETTINGS_PATH = BASE / "settings.json"
DEFAULT_SETTINGS = {
    "theme": "light",
    "auto_open_browser": True,
    "image_extensions": [".jpg", ".jpeg", ".png"],

    # DMS Live Catalogue settings
    "max_dms_windows": 3,             # max concurrent Chrome windows (one per DMS)
    "max_tabs_per_dms": 3,            # max concurrent VIN tabs inside one Chrome window
    "max_total_vin_concurrency": 9,   # hard ceiling across all Chrome windows combined
    "one_account_per_dms_window": True,
    "headless": False,
    "auto_queue_when_full": True,
    "browser_reuse": True,
    "auto_retry_failed_vins": True,
    "retry_count": 2,
    "auto_move_failed_vins": True,
    "save_retry_attempts": 6,
    "otp_timeout_seconds": 120,
}
SETTINGS_LOCK = threading.RLock()


def load_settings():
    try:
        data = json.loads(SETTINGS_PATH.read_text())
        return {**DEFAULT_SETTINGS, **data}
    except Exception:
        return dict(DEFAULT_SETTINGS)


SETTINGS = load_settings()
SAVE_RETRIES = int(SETTINGS.get("save_retry_attempts", 6))
OTP_TIMEOUT  = int(SETTINGS.get("otp_timeout_seconds", 120))
IMG_EXTS     = set(x.lower() for x in SETTINGS.get("image_extensions", [".jpg", ".jpeg", ".png"]))


def save_settings():
    try:
        SETTINGS_PATH.write_text(json.dumps(SETTINGS, indent=2))
    except Exception as e:
        print("Could not save settings.json:", e)


def update_settings(patch):
    global SAVE_RETRIES, OTP_TIMEOUT, IMG_EXTS
    with SETTINGS_LOCK:
        if isinstance(patch.get("theme"), str) and patch["theme"]:
            SETTINGS["theme"] = patch["theme"]
        if "auto_open_browser" in patch:
            SETTINGS["auto_open_browser"] = bool(patch["auto_open_browser"])
        if isinstance(patch.get("image_extensions"), list):
            exts = []
            for e in patch["image_extensions"]:
                if isinstance(e, str) and e.strip():
                    e = e.strip().lower()
                    exts.append(e if e.startswith(".") else f".{e}")
            if exts:
                SETTINGS["image_extensions"] = exts
                IMG_EXTS = set(exts)

        int_fields_1_5 = ["max_dms_windows", "max_tabs_per_dms"]
        for f in int_fields_1_5:
            if f in patch:
                try:
                    SETTINGS[f] = max(1, min(int(patch[f]), 5))
                except (TypeError, ValueError):
                    pass
        if "max_total_vin_concurrency" in patch:
            try:
                SETTINGS["max_total_vin_concurrency"] = max(1, min(int(patch["max_total_vin_concurrency"]), 25))
            except (TypeError, ValueError):
                pass
        bool_fields = ["one_account_per_dms_window", "headless", "auto_queue_when_full",
                       "browser_reuse", "auto_retry_failed_vins", "auto_move_failed_vins"]
        for f in bool_fields:
            if f in patch:
                SETTINGS[f] = bool(patch[f])
        if "retry_count" in patch:
            try:
                SETTINGS["retry_count"] = max(0, min(int(patch["retry_count"]), 10))
            except (TypeError, ValueError):
                pass
        if "save_retry_attempts" in patch:
            try:
                SETTINGS["save_retry_attempts"] = max(1, min(int(patch["save_retry_attempts"]), 20))
                SAVE_RETRIES = SETTINGS["save_retry_attempts"]
            except (TypeError, ValueError):
                pass
        if "otp_timeout_seconds" in patch:
            try:
                SETTINGS["otp_timeout_seconds"] = max(30, min(int(patch["otp_timeout_seconds"]), 600))
                OTP_TIMEOUT = SETTINGS["otp_timeout_seconds"]
            except (TypeError, ValueError):
                pass
        save_settings()


def load_accounts_credentials():
    """Read account credentials from env into {normalized_name: {...}}.

    Three prefixes are supported side by side:
      DC_ACCOUNT_n_{NAME,USER,PASS,ROOFTOP,COMPANY}  -> DealerCenter accounts
      VM_ACCOUNT_n_{NAME,USER,PASS,ROOFTOP}          -> VinMotion accounts
      VA_ACCOUNT_n_{NAME,USER,PASS}                  -> vAuto accounts

    ROOFTOP (and, for DealerCenter, COMPANY) are optional — set them when several
    account folders share one login and switch dealership/rooftop in-app instead
    of logging out and back in. vAuto accounts are always one login per
    enterprise, so there's no rooftop/company concept for them."""
    creds = {}
    for i in range(1, 26):
        name = os.environ.get(f"DC_ACCOUNT_{i}_NAME")
        user = os.environ.get(f"DC_ACCOUNT_{i}_USER")
        pw   = os.environ.get(f"DC_ACCOUNT_{i}_PASS")
        rooftop = os.environ.get(f"DC_ACCOUNT_{i}_ROOFTOP") or name
        company = os.environ.get(f"DC_ACCOUNT_{i}_COMPANY") or rooftop
        if name and user and pw:
            creds[_norm(name)] = {"name": name, "user": user, "pass": pw,
                                   "rooftop": rooftop, "company": company,
                                   "dms": "dealercenter"}
    for i in range(1, 26):
        name = os.environ.get(f"VM_ACCOUNT_{i}_NAME")
        user = os.environ.get(f"VM_ACCOUNT_{i}_USER")
        pw   = os.environ.get(f"VM_ACCOUNT_{i}_PASS")
        rooftop = os.environ.get(f"VM_ACCOUNT_{i}_ROOFTOP") or name
        if name and user and pw:
            creds[_norm(name)] = {"name": name, "user": user, "pass": pw,
                                   "rooftop": rooftop, "company": rooftop,
                                   "dms": "vinmotion"}
    for i in range(1, 26):
        name = os.environ.get(f"VA_ACCOUNT_{i}_NAME")
        user = os.environ.get(f"VA_ACCOUNT_{i}_USER")
        pw   = os.environ.get(f"VA_ACCOUNT_{i}_PASS")
        if name and user and pw:
            creds[_norm(name)] = {"name": name, "user": user, "pass": pw,
                                   "rooftop": name, "company": name,
                                   "dms": "vauto"}
    return creds


def _norm(s: str) -> str:
    """Case/space-insensitive key so 'Capitol of smithfield' == 'Capitol of Smithfield'."""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", s).strip().lower()


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


# ── shared state (polled by the UI) ──────────────────────────────────────────
# "lanes"   -> one per active Chrome window (DMS Live Catalogue "Combined Activity 2"
#              needs Chrome/Tab identity, so each lane is a real Chrome window).
# "log"     -> Combined Activity 1 — a flat, chronological feed exactly like the
#              original tool. Never restructured, never filtered.
# "vin_ops" -> Combined Activity 2 — one row per VIN attempt, structured, with
#              live status/duration/retry/error, for the DMS | Account | Chrome |
#              Tab | VIN | ... table.
state = {
    "run_status": "idle",     # idle | running | done | stopped | error
    "root_path": None,         # remembered so auto-move knows where Master/ goes
    "lanes": [],
    "vin_ops": [],
    "log": [],
}
STATE_LOCK = threading.RLock()
stop_flag  = threading.Event()
worker_thread = None
_vin_op_seq = 0

# Combined Activity 1 attribution: which lane is this OS thread driving right now.
_lane_ctx = threading.local()
# Combined Activity 2 attribution: which VIN operation is the CURRENT asyncio task
# working on. Needs contextvars (not threading.local) because several tabs/VINs
# run as concurrent asyncio Tasks on the SAME thread within one Chrome window.
_tab_ctx = contextvars.ContextVar("tab_ctx", default=None)


def _current_lane():
    return getattr(_lane_ctx, "lane", None)


def log(kind, msg):
    entry = {"t": time.strftime("%H:%M:%S"), "kind": kind, "msg": str(msg)}
    lane = _current_lane()
    with STATE_LOCK:
        if lane is not None:
            lane["log"].append(entry)
            state["log"].append({**entry, "job": lane["label"]})
        else:
            state["log"].append(entry)
    print(f"[{entry['t']}] [{kind.upper():5}] {msg}")


def set_step(n):
    lane = _current_lane()
    if lane is not None:
        lane["step"] = n


def _new_vin_op(dms, enterprise, account, lane, chrome_label, tab_no, vin, total_attempts):
    global _vin_op_seq
    with STATE_LOCK:
        _vin_op_seq += 1
        op = {
            "id": _vin_op_seq, "dms": dms, "dms_label": DMS_LABELS.get(dms, dms),
            "enterprise": enterprise, "account": account,
            "rooftop": (lane.get("current_rooftop") or "") if lane else "",
            "chrome": chrome_label, "tab": tab_no, "vin": vin,
            "current_action": "Queued", "status": "queued",
            "start_ts": None, "end_ts": None,
            "retry": 0, "total_attempts": total_attempts, "error": None,
        }
        state["vin_ops"].append(op)
    return op


def _update_vin_op(**fields):
    ctx = _tab_ctx.get()
    if not ctx:
        return
    op = ctx.get("op")
    if not op:
        return
    with STATE_LOCK:
        op.update(fields)


# ── async Playwright helpers (shared by all three DMS integrations) ─────────
async def safe_wait(page, timeout=15000, settle=2.0, rezoom=True):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass
    await asyncio.sleep(settle)
    if rezoom:
        await apply_zoom(page)
        await dismiss_popups(page)


async def dismiss_popups(page):
    """Close promotional/interstitial modals that can appear at any point and
    block clicks underneath them. Best-effort and silent. Deliberately excludes
    anything belonging to Uppy (VinMotion's upload widget)."""
    for sel in [
        'button[aria-label*="close" i]', '[aria-label="Close" i]',
        '.modal button:has-text("×")', '[class*="modal" i] [class*="close" i]',
        'button:has-text("×")',
    ]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=600):
                cls = (await el.get_attribute("class") or "").lower()
                if "uppy" in cls:
                    continue
                await el.click(timeout=1200)
                log("info", "Dismissed a popup")
                await asyncio.sleep(0.3)
        except Exception:
            continue


async def apply_zoom(page, target_w=1600, target_h=900):
    """Zoom the page out so its effective content area is roughly target_w x
    target_h, without resizing the actual browser window. Cosmetic only —
    never let it break the automation."""
    try:
        dims = await page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
        w, h = dims.get("w") or target_w, dims.get("h") or target_h
        pct = min(1.0, max(0.5, min(target_w / w, target_h / h)))
        await page.evaluate(f"document.body.style.zoom = '{pct * 100:.0f}%'")
    except Exception:
        pass


async def _click_first(page, selectors, timeout=5000, label="", force=False):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.click(timeout=timeout, force=force)
            if label:
                log("ok", label)
            return True
        except Exception:
            continue
    return False


async def _fill_and_submit_login(page, user, pw):
    """One attempt at filling username/password and submitting. Returns True if it
    looks like the form was submitted successfully (no lingering validation error)."""
    filled_u = False
    for sel in ['input[placeholder*="Username" i]', 'input[name*="user" i]',
                'input[id*="user" i]', 'input[type="text"]']:
        try:
            box = page.locator(sel).first
            await box.click(timeout=4000)
            await box.press("Control+A")
            await box.press("Backspace")
            await box.press_sequentially(user, delay=45)
            filled_u = True
            break
        except Exception:
            continue
    filled_p = False
    pw_box = None
    for sel in ['input[type="password"]', 'input[placeholder*="Password" i]',
                'input[name*="pass" i]']:
        try:
            box = page.locator(sel).first
            await box.click(timeout=4000)
            await box.press("Control+A")
            await box.press("Backspace")
            await box.press_sequentially(pw, delay=45)
            filled_p = True
            pw_box = box
            break
        except Exception:
            continue
    if not (filled_u and filled_p):
        log("warn", "Could not auto-fill login — fill it manually in the browser.")

    submitted_via_enter = False
    if pw_box is not None:
        try:
            await pw_box.press("Enter")
            submitted_via_enter = True
            log("info", "Pressed Enter to submit login")
        except Exception:
            pass
    await asyncio.sleep(1.5)

    try:
        if await page.locator('text=Username is required').first.is_visible(timeout=1500):
            log("warn", "Username field came out empty (validation error shown) — "
                        "this attempt failed")
            return False
    except Exception:
        pass

    still_on_login = True
    try:
        still_on_login = await page.locator('input[type="password"]').first.is_visible(timeout=2000)
    except Exception:
        still_on_login = False

    clicked = submitted_via_enter and not still_on_login
    if clicked:
        log("ok", "Enter submitted the login form")

    if not clicked:
        try:
            await page.keyboard.press("Tab")
        except Exception:
            pass
        await asyncio.sleep(0.5)
        for _ in range(10):
            if await _click_first(page, ['button:has-text("Continue")', 'button[type="submit"]',
                                        'button:has-text("Login")', 'button:has-text("Sign in")'],
                                  timeout=3000):
                clicked = True
                break
            await asyncio.sleep(0.8)

    if not clicked:
        for sel in ['button:has-text("Continue")', 'button[type="submit"]']:
            try:
                await page.locator(sel).first.click(timeout=4000, force=True)
                clicked = True
                log("ok", "Clicked Continue (forced)")
                break
            except Exception:
                continue

    if not clicked:
        try:
            await page.evaluate("""() => {
                const btn = [...document.querySelectorAll('button')]
                    .find(b => b.textContent.trim().toLowerCase().includes('continue'));
                if (btn) btn.click();
            }""")
            clicked = True
            log("ok", "Clicked Continue (JS fallback)")
        except Exception:
            pass

    log("ok" if clicked else "warn",
        "Clicked Continue" if clicked else "Continue never became clickable — check the fields manually")
    await safe_wait(page, 20000, 3.0)

    try:
        if await page.locator('text=Username is required').first.is_visible(timeout=1500):
            log("warn", "Username validation error appeared after submit")
            return False
    except Exception:
        pass
    return True


async def _wait_for_lane_otp(lane, user, prompt_prefix="Enter the MFA code emailed for"):
    """Block (without freezing the event loop) until an OTP is available for
    this lane — either fetched automatically (caller handles that separately)
    or typed into the UI. Lane-scoped so several Chrome windows waiting on
    OTP at the same time don't collide."""
    with STATE_LOCK:
        lane["status"] = "otp_wait"
        lane["otp_prompt"] = f"{prompt_prefix} '{user}', then click Submit OTP."
    log("warn", "Waiting for the verification code from the UI (manual fallback) ...")
    lane["otp_event"].clear()
    lane["otp_code"]["code"] = None
    await asyncio.get_event_loop().run_in_executor(None, lane["otp_event"].wait)
    with STATE_LOCK:
        lane["status"] = "running"
        lane["otp_prompt"] = None
    return lane["otp_code"]["code"]


# ── DealerCenter page actions ────────────────────────────────────────────────
async def dc_login(page, user, pw, after_epoch, lane):
    """Fill username/password, submit via Enter, then satisfy MFA."""
    set_step(2)
    log("info", f"Opening DealerCenter login for {user} ...")

    ok = False
    for attempt in range(2):
        if attempt > 0:
            log("info", "Reopening the DealerCenter login page and retrying ...")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        await safe_wait(page, 30000, 2.5)
        try:
            await page.evaluate("document.body.style.zoom = '75%'")
            log("info", "Set page zoom to 75%")
        except Exception:
            pass
        if await _fill_and_submit_login(page, user, pw):
            ok = True
            break

    if not ok:
        log("error", "Login form kept failing validation after a retry — giving up on this login")
        return False

    set_step(3)
    if await _click_first(page, ['text=Try another method', 'a:has-text("Try another method")',
                                 'button:has-text("Try another method")'],
                          timeout=6000, label="Clicked 'Try another method'"):
        await safe_wait(page, 10000, 1.5)
        await _click_first(page, ['text=Email', 'button:has-text("Email")',
                                  '[role="button"]:has-text("Email")'],
                           timeout=6000, label="Selected Email verification")
        await safe_wait(page, 15000, 2.5)
    else:
        log("info", "No 'Try another method' link seen — assuming email MFA is already active")

    otp = None
    if GMAIL_ADDR and GMAIL_PASS:
        log("info", "Fetching MFA code from Gmail ...")
        try:
            from otp_reader import get_otp
            otp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: get_otp(GMAIL_ADDR, GMAIL_PASS, after_epoch,
                                       timeout=OTP_TIMEOUT, log=log,
                                       sender=DC_OTP_SENDER, subject_hint=DC_OTP_SUBJECT))
        except Exception as e:
            log("warn", f"Auto-OTP failed: {e}")

    if not otp:
        otp = await _wait_for_lane_otp(lane, user, "Enter the MFA code emailed for")
        if stop_flag.is_set():
            return False

    if not otp:
        log("error", "No MFA code available.")
        return False

    typed = False
    otp_box = None
    for sel in ['input[name*="otp" i]', 'input[placeholder*="code" i]',
                'input[autocomplete="one-time-code"]', 'input[type="tel"]']:
        try:
            box = page.locator(sel).first
            await box.click(timeout=4000)
            await box.press("Control+A")
            await box.press("Backspace")
            await box.press_sequentially(otp, delay=45)
            typed = True
            otp_box = box
            break
        except Exception:
            continue
    if not typed:
        try:
            boxes = await page.locator('input[maxlength="1"]').all()
            if len(boxes) >= len(otp):
                for b, d in zip(boxes, otp):
                    await b.click(timeout=2000)
                    await b.press_sequentially(d, delay=45)
                typed = True
                otp_box = boxes[-1]
        except Exception:
            pass
    if not typed:
        try:
            await page.keyboard.type(otp, delay=80)
            typed = True
        except Exception:
            pass

    submitted = False
    if otp_box is not None:
        try:
            await otp_box.press("Enter")
            submitted = True
            log("info", "Pressed Enter to submit MFA code")
        except Exception:
            pass
    await asyncio.sleep(1.5)

    still_on_otp = True
    try:
        still_on_otp = await page.locator('input[placeholder*="code" i]').first.is_visible(timeout=2000)
    except Exception:
        still_on_otp = False

    if not submitted or still_on_otp:
        if await _click_first(page, ['button:has-text("Verify")', 'button:has-text("Submit")',
                                     'button:has-text("Continue")', 'button[type="submit"]'],
                              label="Submitted MFA code (clicked)"):
            submitted = True

    await safe_wait(page, 40000, 4.0)
    log("ok", f"Logged in: {user}")
    return True


async def dc_switch_rooftop(page, company_label, target_label):
    """
    For accounts that share one DealerCenter login across several rooftops:
    click the COMPANY name in the top bar, then pick the target rooftop from
    the "Switch Dealership" list. No logout/login/OTP needed.

      - Already on the target rooftop → no-op success, no switcher opened.
      - No "Switch Dealership" panel exists at all → this login only has one
        rooftop; treated as success, never skips the account.
      - Only one rooftop actually listed → same as above.
      - 2+ rooftops genuinely listed → clicks the target and verifies.
    """
    set_step(4)

    try:
        if await page.locator(f'text="{target_label}"').first.is_visible(timeout=2000):
            log("ok", f"Already on rooftop: {target_label}")
            return True
    except Exception:
        pass

    log("info", f"Switching rooftop: '{company_label}' → '{target_label}' ...")

    opened = False
    for sel in [f'text="{company_label}"', f'text={company_label}']:
        try:
            await page.locator(sel).first.click(timeout=5000)
            if await page.locator('text=Switch Dealership').first.is_visible(timeout=2500):
                opened = True
                break
        except Exception:
            continue

    if not opened:
        for sel in ['[class*="dealer" i]', '[class*="rooftop" i]', 'header button', 'nav button']:
            try:
                candidates = page.locator(sel)
                count = await candidates.count()
                for i in range(min(count, 5)):
                    try:
                        await candidates.nth(i).click(timeout=1500)
                        if await page.locator('text=Switch Dealership').first.is_visible(timeout=1500):
                            opened = True
                            break
                        await page.keyboard.press("Escape")
                    except Exception:
                        continue
                if opened:
                    break
            except Exception:
                continue

    if not opened:
        log("info", "No dealership switcher found — this login appears to have "
                     "only one rooftop, nothing to switch")
        return True

    try:
        count = await page.locator('.dropdown-menu button, [role="menuitem"], '
                                   '[class*="dealer-list" i] li, [class*="rooftop-list" i] li').count()
    except Exception:
        count = None
    if count is not None and count <= 1:
        log("info", "Only one rooftop listed under Switch Dealership — nothing to switch")
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return True

    try:
        await page.locator(f'text="{target_label}"').first.click(timeout=6000)
    except Exception as e:
        log("warn", f"Found the switcher but couldn't click '{target_label}': {e}")
        return False

    await safe_wait(page, 30000, 3.0)
    log("ok", f"Switched to rooftop: {target_label}")
    return True


_ACTIVE_INVENTORY_FALLBACK_URL = (
    "https://app.dealercenter.net/apps/shell/reports/custom/"
    "inventoryreport/active-inventory-report?inventorystatus=0"
)


async def _click_active_inventory_count(page):
    for sel in [
        'xpath=//*[normalize-space(text())="Active Inventory"]/preceding::*[1]',
        'xpath=//*[contains(normalize-space(.),"Active Inventory")]/preceding-sibling::*[1]',
        'xpath=(//*[contains(normalize-space(.),"Active Inventory")])[1]'
        '/ancestor::*[self::a or self::button or @role="button"][1]',
    ]:
        try:
            await page.locator(sel).first.click(timeout=4000)
            return True
        except Exception:
            continue
    return False


async def _looks_like_404(page):
    try:
        return (await page.locator('text=404 Page').first.is_visible(timeout=1500)
                or await page.locator('text=cannot be found').first.is_visible(timeout=800))
    except Exception:
        return False


async def dc_open_active_inventory(page):
    set_step(4)
    log("info", "Opening Active Inventory ...")

    if await _click_active_inventory_count(page):
        log("ok", "Clicked the Active Inventory count")
    else:
        try:
            await page.goto(_ACTIVE_INVENTORY_FALLBACK_URL, wait_until="domcontentloaded",
                            timeout=60000)
            log("ok", "Navigated to inventory report (fallback URL)")
        except Exception as e:
            log("warn", f"Could not open inventory: {e}")
    await safe_wait(page, 30000, 3.0)

    if await _looks_like_404(page):
        if "not-found" in page.url:
            log("warn", f"Landed on the 404 route ({page.url}) — "
                        f"navigating to the app shell base URL instead of reloading")
            try:
                await page.goto("https://app.dealercenter.net/apps/shell/",
                                wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log("warn", f"Could not navigate to the shell base URL: {e}")
            await safe_wait(page, 30000, 3.0)
            if await _click_active_inventory_count(page):
                log("ok", "Clicked the Active Inventory count after shell-base recovery")
                await safe_wait(page, 30000, 3.0)
        else:
            log("warn", "Landed on a 404 page — refreshing")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            await safe_wait(page, 30000, 3.0)
            if await _looks_like_404(page) and await _click_active_inventory_count(page):
                log("ok", "Clicked the Active Inventory count after refresh")
                await safe_wait(page, 30000, 3.0)

    try:
        await page.wait_for_selector('input[placeholder*="Stock" i], input[placeholder*="VIN" i]',
                                     timeout=20000)
        log("ok", "Inventory filter panel is ready")
    except Exception:
        log("warn", "Inventory filter panel didn't appear within 20s — "
                     "VIN filtering below may fail")


async def dc_filter_vin(page, vin):
    set_step(5)
    filled = False
    for sel in ['input[placeholder*="Stock" i]', 'input[placeholder*="VIN" i]']:
        try:
            box = page.locator(sel).first
            await box.wait_for(state="visible", timeout=8000)
            await box.click(timeout=4000)
            await box.press("Control+A")
            await box.press("Backspace")
            await box.press_sequentially(vin, delay=30)
            filled = True
            break
        except Exception:
            continue
    if not filled:
        log("warn", f"Could not find Stock#/VIN# field for {vin} (page url: {page.url})")
        return False
    log("ok", f"Entered VIN: {vin}")
    await _click_first(page, ['button:has-text("Run")'], label="Clicked Run")
    await safe_wait(page, 25000, 3.0)
    return True


async def dc_open_vehicle(page, vin):
    tail = re.escape(vin[-6:])
    for sel in [
        f'xpath=//*[contains(text(),"{vin[-6:]}")]/ancestor::*[self::tr or contains(@class,"row")][1]//img',
        'table img', '.report-list img', '[class*="result" i] img',
    ]:
        try:
            await page.locator(sel).first.click(timeout=5000)
            log("ok", "Opened vehicle record (clicked thumbnail)")
            await safe_wait(page, 30000, 3.0)
            return True
        except Exception:
            continue

    for sel in [
        'a:has-text("Vin")',
        f'text=/{tail}/',
        '.report-list a', 'table a', 'a[href*="vehicle"]',
    ]:
        try:
            await page.locator(sel).first.click(timeout=5000)
            log("ok", "Opened vehicle record (clicked title link)")
            await safe_wait(page, 30000, 3.0)
            return True
        except Exception:
            continue
    try:
        links = await page.locator("a").all()
        for lk in links:
            txt = (await lk.inner_text(timeout=500) or "").strip()
            if re.match(r"^(19|20)\d{2}\s+\S+", txt):
                await lk.click(timeout=4000)
                log("ok", f"Opened vehicle record ({txt[:30]})")
                await safe_wait(page, 30000, 3.0)
                return True
    except Exception:
        pass
    log("error", f"Could not open the vehicle record for {vin}")
    return False


async def dc_open_media_tab(page):
    await _click_first(page, ['[role="tab"]:has-text("Media")', 'text=Media',
                              'button:has-text("Media")', 'a:has-text("Media")'],
                       label="Opened Media tab")
    await safe_wait(page, 15000, 2.5)


async def dc_remove_all_photos(page):
    """Click 'Remove All' then confirm 'Yes'. Skips silently if no photos."""
    try:
        if await page.locator('text=/Photos \\(0\\)/').first.is_visible(timeout=2000):
            log("info", "No existing photos — skipping delete")
            return
    except Exception:
        pass

    if not await _click_first(page, ['text=Remove All', 'button:has-text("Remove All")',
                                     '[aria-label*="remove all" i]']):
        log("info", "Remove All not found — assuming no photos")
        return
    log("info", "Clicked Remove All")
    await asyncio.sleep(1.0)
    await _click_first(page, ['[role="dialog"] button:has-text("Yes")',
                              '.modal button:has-text("Yes")',
                              'button:has-text("Yes")'],
                       label="Confirmed delete (Yes)")
    await safe_wait(page, 20000, 3.0)


async def dc_upload_images(page, images):
    set_step(6)
    log("info", f"Uploading {len(images)} image(s) ...")

    async def _try_upload_once():
        try:
            await page.locator('text=Drop your image here').first.click(timeout=2000)
        except Exception:
            pass
        for sel in ['input[type="file"]']:
            try:
                inp = page.locator(sel).first
                await inp.set_input_files(images, timeout=30000)
                log("ok", f"Set {len(images)} files on upload input")
                return True
            except Exception:
                continue
        try:
            async with page.expect_file_chooser(timeout=20000) as fc_info:
                await _click_first(page, ['text=Browse', 'button:has-text("Upload")',
                                          'text=Upload'])
            fc = await fc_info.value
            await fc.set_files(images)
            log("ok", f"Set {len(images)} files via file chooser")
            return True
        except Exception as e:
            log("warn", f"Upload attempt failed: {e}")
            return False

    done = await _try_upload_once()
    if not done:
        log("info", "Retrying the upload once more ...")
        await asyncio.sleep(2.0)
        done = await _try_upload_once()
    if not done:
        log("error", f"Upload failed for all {len(images)} image(s) after a retry")
        return False

    target = len(images)
    log("info", f"Waiting for all {target} image(s) to finish uploading ...")
    deadline = time.time() + 180
    last_count = -1
    while time.time() < deadline:
        count = None
        try:
            txt = await page.locator('text=/Photos \\(\\d+\\)/').first.inner_text(timeout=2000)
            m = re.search(r"\((\d+)\)", txt)
            if m:
                count = int(m.group(1))
        except Exception:
            pass
        if count is not None and count != last_count:
            log("info", f"  {count}/{target} photos uploaded so far ...")
            last_count = count
        if count is not None and count >= target:
            log("ok", f"All {target} photos finished uploading")
            return True
        await asyncio.sleep(2.0)

    log("warn", f"Only saw {last_count if last_count >= 0 else '?'}/{target} photos after "
                f"3 minutes — proceeding to save anyway")
    return True


async def dc_save_and_close(page):
    """Click Save and Close, then verify the 'Inventory Successfully Saved' banner
    actually appears. Returns True only on confirmed success."""
    set_step(7)
    for _ in range(SAVE_RETRIES):
        if stop_flag.is_set():
            return False
        for ok in ("Okay", "OK", "Ok"):
            try:
                b = page.locator(f'button:has-text("{ok}")').first
                if await b.is_visible(timeout=1000):
                    await b.click()
                    await asyncio.sleep(1.0)
            except Exception:
                pass
        if await _click_first(page, ['button:has-text("Save and Close")',
                                     'button:has-text("Save & Close")']):
            log("ok", "Clicked Save and Close")
            await safe_wait(page, 30000, 3.0)
            try:
                if await page.locator('text=Inventory Successfully Saved').first.is_visible(timeout=8000):
                    log("ok", "Confirmed: Inventory Successfully Saved")
                    return True
            except Exception:
                pass
            try:
                if await page.locator('text=Error saving Inventory').first.is_visible(timeout=1500) or \
                   await page.locator('text=Changes have been made to this inventory record'
                                      ).first.is_visible(timeout=1000):
                    log("warn", "Save failed: another user/process had already changed "
                                "this inventory record")
                    try:
                        await page.locator('[class*="modal" i] [class*="close" i], '
                                           'button[aria-label*="close" i]').first.click(timeout=1500)
                    except Exception:
                        pass
                    return False
            except Exception:
                pass
            log("warn", "Save and Close was clicked but no success confirmation appeared")
            return False
        await asyncio.sleep(1.5)
    log("warn", "Save and Close button not confirmed")
    return False


async def dc_go_home(page):
    """Navigate all the way back to the DealerCenter home page — the most
    reliable reset point between VINs."""
    log("info", "Returning to DealerCenter home ...")
    try:
        await page.goto("https://app.dealercenter.net/apps/shell/reports/home",
                        wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("warn", f"Could not navigate home: {e}")
    await safe_wait(page, 30000, 3.0)


# ── VinMotion page actions ───────────────────────────────────────────────────
async def vm_login(page, user, pw, after_epoch, lane):
    """Fill username/password on the VinMotion login screen, submit, then satisfy
    the email verification step."""
    set_step(2)
    log("info", f"Opening VinMotion login for {user} ...")
    try:
        await page.goto(VM_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("warn", f"Could not open the VinMotion login page: {e}")
    await safe_wait(page, 30000, 2.5)

    filled_u = False
    for sel in ['input[type="text"]', 'input[type="email"]', 'input[name*="user" i]']:
        try:
            box = page.locator(sel).first
            await box.click(timeout=4000)
            await box.press("Control+A"); await box.press("Backspace")
            await box.press_sequentially(user, delay=45)
            filled_u = True
            break
        except Exception:
            continue

    filled_p = False
    pw_box = None
    for sel in ['input[type="password"]']:
        try:
            box = page.locator(sel).first
            await box.click(timeout=4000)
            await box.press("Control+A"); await box.press("Backspace")
            await box.press_sequentially(pw, delay=45)
            filled_p = True
            pw_box = box
            break
        except Exception:
            continue

    if not (filled_u and filled_p):
        log("warn", "Could not auto-fill VinMotion login — fill it manually in the browser.")

    clicked = False
    if pw_box is not None:
        try:
            await pw_box.press("Enter")
            clicked = True
            log("info", "Pressed Enter to submit login")
        except Exception:
            pass
    if not clicked:
        clicked = await _click_first(page, ['button:has-text("GO")', 'button[type="submit"]'],
                                     label="Clicked GO")
    await safe_wait(page, 20000, 3.0)

    otp = None
    if GMAIL_ADDR and GMAIL_PASS:
        log("info", "Fetching verification code from Gmail ...")
        try:
            from otp_reader import get_otp
            otp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: get_otp(GMAIL_ADDR, GMAIL_PASS, after_epoch, timeout=OTP_TIMEOUT,
                                       log=log, sender=VM_OTP_SENDER, subject_hint=VM_OTP_SUBJECT,
                                       sender_domain="microsoftonline.com"))
        except Exception as e:
            log("warn", f"Auto-OTP failed: {e}")

    if not otp:
        otp = await _wait_for_lane_otp(lane, user, "Enter the verification code emailed for")
        if stop_flag.is_set():
            return False

    if not otp:
        log("error", "No verification code available.")
        return False

    typed = False
    for sel in ['input[placeholder*="code" i]', 'input[type="text"]']:
        try:
            box = page.locator(sel).first
            await box.click(timeout=4000)
            await box.press("Control+A"); await box.press("Backspace")
            await box.press_sequentially(otp, delay=45)
            typed = True
            break
        except Exception:
            continue
    if not typed:
        try:
            await page.keyboard.type(otp, delay=80)
        except Exception:
            pass

    await _click_first(page, ['button:has-text("Verify code")', 'button:has-text("Verify")'],
                       label="Clicked Verify code")
    await safe_wait(page, 20000, 3.0)

    await _click_first(page, ['button:has-text("CONTINUE")', 'button:has-text("Continue")'],
                       label="Clicked Continue")
    await safe_wait(page, 30000, 3.0)
    log("ok", f"Logged in: {user}")
    return True


async def _vm_select_native_rooftop(page, target_label):
    """If the rooftop switcher is (or contains) a native <select>, this is far more
    reliable than click-driven dropdown handling."""
    try:
        selects = page.locator("select")
        count = await selects.count()
        for i in range(count):
            try:
                await selects.nth(i).select_option(label=target_label, timeout=3000)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _vm_get_current_rooftop(page):
    """Read the rooftop currently shown in the dropdown toggle's own value —
    the ground truth for 'where are we right now'."""
    try:
        val = await page.locator('input.dropdown-toggle[data-bs-toggle="dropdown"]') \
                        .first.get_attribute("value", timeout=5000)
        return (val or "").strip()
    except Exception:
        return None


async def vm_list_rooftops(page):
    """Best-effort read of every rooftop label this login can switch to, WITHOUT
    actually switching anything. Returns [] if the dropdown can't be opened at
    all — a strong sign this login only has one rooftop. Always leaves the
    dropdown closed again before returning."""
    toggle_sel = 'input.dropdown-toggle[data-bs-toggle="dropdown"]'
    try:
        if await page.locator(toggle_sel).first.count() == 0:
            return []
        await page.locator(toggle_sel).first.click(timeout=4000)
    except Exception:
        return []

    try:
        await page.locator(".dropdown-menu.show").first.wait_for(state="visible", timeout=5000)
    except Exception:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return []

    labels = []
    try:
        items = page.locator(".dropdown-menu.show button.dropdown-item")
        count = await items.count()
        for i in range(count):
            try:
                labels.append((await items.nth(i).inner_text(timeout=1500) or "").strip())
            except Exception:
                continue
    except Exception:
        pass
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    return [l for l in labels if l]


async def _vm_switch_rooftop_once(page, target_label):
    """One attempt at opening the dropdown and clicking target_label. Returns
    True only if the click succeeded (not yet re-verified against the toggle)."""
    toggle_sel = 'input.dropdown-toggle[data-bs-toggle="dropdown"]'
    clicked_toggle = False
    for attempt in range(4):
        try:
            await page.locator(toggle_sel).first.click(timeout=6000)
            clicked_toggle = True
            break
        except Exception:
            if attempt == 0:
                log("info", "Rooftop dropdown not ready yet, waiting and retrying ...")
            await asyncio.sleep(2.0)
    if not clicked_toggle:
        log("warn", "Could not click the rooftop dropdown toggle after retrying")
        return False

    try:
        await page.locator(".dropdown-menu.show").first.wait_for(state="visible", timeout=8000)
    except Exception:
        log("warn", "Rooftop dropdown menu did not open")
        return False

    try:
        await page.locator(".dropdown-menu.show button.dropdown-item") \
                  .locator(f'text="{target_label}"').first.click(timeout=6000)
    except Exception as e:
        log("warn", f"Could not click '{target_label}' in the rooftop dropdown: {e}")
        return False

    await safe_wait(page, 20000, 3.0)
    return True


async def vm_switch_rooftop(page, target_label):
    """
    Fixed behavior:
      - Already on target_label → immediate no-op success.
      - 2+ rooftops genuinely available → switch and verify.
      - 0 or 1 rooftop available (no dropdown, or a single entry) → treated as
        success; there's nothing to switch to, so it no longer errors out and
        skips the account.
    """
    set_step(4)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(1.5)

    before = await _vm_get_current_rooftop(page)
    log("info", f"Current rooftop is '{before or 'unknown'}' — target is '{target_label}'")
    if before and before.strip().lower() == target_label.strip().lower():
        log("ok", f"Already on rooftop: {target_label}")
        return True

    available = await vm_list_rooftops(page)
    if len(available) <= 1:
        log("info", "This login only has one rooftop available — nothing to switch, "
                     "proceeding as-is")
        return True

    if not any(a.strip().lower() == target_label.strip().lower() for a in available):
        log("warn", f"'{target_label}' wasn't in this login's rooftop list "
                     f"({', '.join(available)}) — will still try clicking it")

    if await _vm_select_native_rooftop(page, target_label):
        await safe_wait(page, 20000, 3.0)
        after = await _vm_get_current_rooftop(page)
        if after and after.strip().lower() == target_label.strip().lower():
            log("ok", f"Switched to rooftop: {target_label} (native select, verified)")
            return True

    for attempt in range(1, 4):
        log("info", f"Switching rooftop -> '{target_label}' (attempt {attempt}) ...")
        await _vm_switch_rooftop_once(page, target_label)
        after = await _vm_get_current_rooftop(page)
        if after and after.strip().lower() == target_label.strip().lower():
            log("ok", f"Switched to rooftop: {target_label} (verified)")
            return True
        log("warn", f"After switching, rooftop reads '{after or 'unknown'}' — "
                    f"not yet '{target_label}', retrying ...")
        await asyncio.sleep(1.5)

    log("error", f"Could not verify rooftop switched to '{target_label}' after retries")
    return False


async def vm_open_inventory(page):
    """Navigate to the Inventory list. Skips the reload if we're already there."""
    set_step(4)
    already_there = "/inventory" in (page.url or "").lower()
    if already_there:
        log("info", "Already on Inventory — waiting for it to settle ...")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
    else:
        log("info", "Opening VinMotion Inventory ...")
        try:
            await page.goto(VM_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log("warn", f"Could not navigate to Inventory: {e}")
    await safe_wait(page, 30000, 3.0)
    try:
        await page.wait_for_selector('text=/\\d+\\s+Vehicle\\(s\\)/', timeout=20000)
        log("ok", "Inventory page is ready")
    except Exception:
        log("warn", "Inventory page didn't confirm ready within 20s")


async def vm_filter_vin(page, vin):
    set_step(5)
    for sel in ['#iconSearch', 'button#iconSearch']:
        try:
            await page.locator(sel).first.click(timeout=4000)
            break
        except Exception:
            continue

    filled = False
    for sel in ['#txtSearch', 'input[placeholder="Search..."]',
               'input[placeholder*="search" i]', 'input[type="text"]']:
        try:
            box = page.locator(sel).first
            await box.wait_for(state="visible", timeout=6000)
            await box.click(timeout=4000)
            await box.press("Control+A"); await box.press("Backspace")
            await box.press_sequentially(vin, delay=30)
            filled = True
            break
        except Exception:
            continue

    if not filled:
        log("warn", f"Could not find the VIN search field for {vin} (page url: {page.url})")
        return False

    log("ok", f"Entered VIN: {vin}")
    try:
        await page.keyboard.press("Enter")
    except Exception:
        pass
    await safe_wait(page, 25000, 3.0)
    return True


async def vm_open_vehicle(page, vin):
    for sel in ['tr.jqgrow', '.ui-jqgrid-btable tr[role="row"]']:
        try:
            await page.locator(sel).first.click(timeout=5000)
            log("ok", "Opened vehicle record")
            await safe_wait(page, 30000, 3.0)
            return True
        except Exception:
            continue

    try:
        links = await page.locator("a").all()
        for lk in links:
            txt = (await lk.inner_text(timeout=500) or "").strip()
            if re.match(r"^(19|20)\d{2}\s+\S+", txt):
                await lk.click(timeout=4000)
                log("ok", f"Opened vehicle record ({txt[:30]})")
                await safe_wait(page, 30000, 3.0)
                return True
    except Exception:
        pass
    log("error", f"Could not open the vehicle record for {vin}")
    return False


async def vm_open_merchandising_tab(page):
    try:
        await page.locator("#aMerchandising").first.click(timeout=8000)
        log("ok", "Opened Merchandising tab")
    except Exception as e:
        log("warn", f"Could not click #aMerchandising: {e}")
        await _click_first(page, ['text=Merchandising', 'a:has-text("Merchandising")'],
                           label="Opened Merchandising tab (fallback)")
    await safe_wait(page, 15000, 2.5)
    try:
        await page.wait_for_selector("#divPhotosAndVideosBody, text=Photos", timeout=10000)
    except Exception:
        pass
    await _click_first(page, ['text=Photos', 'button:has-text("Photos")'])
    await _click_first(page, ['text=Add/Delete/Reorder', 'button:has-text("Add/Delete/Reorder")'])
    await safe_wait(page, 10000, 1.5)


async def vm_remove_all_photos(page):
    """Click #btnSelectAll then #btnDelete. Accepts the native confirm() dialog
    (Playwright would otherwise auto-dismiss it, silently keeping old photos)."""
    try:
        if await page.locator("#btnSelectAll").first.is_visible(timeout=2000):
            await page.locator("#btnSelectAll").first.click(timeout=5000)
            log("ok", "Clicked Select All")
        else:
            log("info", "Select All not visible — assuming no existing photos")
            return
    except Exception:
        log("info", "Select All not found — assuming no existing photos")
        return
    await asyncio.sleep(0.5)

    async def _accept_dialog(dialog):
        try:
            await dialog.accept()
        except Exception:
            pass

    page.once("dialog", _accept_dialog)
    try:
        await page.locator("#btnDelete").first.click(timeout=6000)
        log("info", "Clicked Delete (native confirm accepted)")
    except Exception as e:
        log("info", f"Delete button not clickable ({e}) — assuming no photos to delete")
        return

    await safe_wait(page, 20000, 3.0)


async def vm_upload_images(page, images):
    set_step(6)
    log("info", f"Uploading {len(images)} image(s) ...")

    try:
        await page.locator("#btnUpload").first.click(timeout=6000)
    except Exception as e:
        log("error", f"Could not click #btnUpload: {e}")
        return False
    await safe_wait(page, 10000, 1.5, rezoom=False)

    async def _try_upload_once():
        for sel in ['input[type="file"]']:
            try:
                inp = page.locator(sel).first
                await inp.set_input_files(images, timeout=30000)
                log("ok", f"Set {len(images)} files on upload input")
                return True
            except Exception:
                continue
        try:
            async with page.expect_file_chooser(timeout=20000) as fc_info:
                await _click_first(page, ['text=browse files'])
            fc = await fc_info.value
            await fc.set_files(images)
            log("ok", f"Set {len(images)} files via file chooser")
            return True
        except Exception as e:
            log("warn", f"Upload attempt failed: {e}")
            return False

    done = await _try_upload_once()
    if not done:
        log("info", "Retrying the upload once more ...")
        await asyncio.sleep(2.0)
        done = await _try_upload_once()
    if not done:
        log("error", f"Upload failed for all {len(images)} image(s) after a retry")
        return False

    target = len(images)
    try:
        await page.wait_for_selector(f'text=/{target} files selected/', timeout=15000)
        log("ok", f"Confirmed {target} file(s) staged for upload")
    except Exception:
        log("warn", "Didn't see the file-count confirmation — proceeding anyway")

    if not await _click_first(page, ['button.uppy-StatusBar-actionBtn--upload',
                                     f'button:has-text("Upload {target} file")',
                                     'button:has-text("Upload")'],
                              label=f"Clicked Upload {target} file(s)"):
        log("error", "Could not click the final Upload button in the file-picker dialog")
        return False

    log("info", "Waiting for the upload + auto-redirect back to the photo grid ...")
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            still_uploading = await page.locator('text=files selected').first.is_visible(timeout=500)
        except Exception:
            still_uploading = False
        if not still_uploading:
            try:
                if await page.locator('text=Add/Delete/Reorder').first.is_visible(timeout=2000):
                    log("ok", "Upload finished — back on the photo grid")
                    return True
            except Exception:
                pass
        await asyncio.sleep(2.0)

    log("warn", "Didn't confirm the redirect back to the photo grid within 3 minutes — "
                "proceeding anyway")
    return True


async def vm_save(page):
    set_step(7)
    for _ in range(SAVE_RETRIES):
        if stop_flag.is_set():
            return False
        try:
            await page.locator("#btnSave").first.click(timeout=5000)
            log("ok", "Clicked Save")
            await safe_wait(page, 20000, 3.0)
            return True
        except Exception:
            pass
        if await _click_first(page, ['button:has-text("SAVE")', 'button:has-text("Save")']):
            log("ok", "Clicked Save (fallback)")
            await safe_wait(page, 20000, 3.0)
            return True
        await asyncio.sleep(1.5)
    log("warn", "Save button not found")
    return False


async def vm_go_inventory(page):
    log("info", "Returning to VinMotion Inventory ...")
    try:
        await page.locator('a[href="/Inventory"]').first.click(timeout=6000)
    except Exception:
        await _click_first(page, ['text=Inventory', 'a:has-text("Inventory")'],
                           label="Clicked Inventory (fallback)")
    await safe_wait(page, 20000, 2.5)


async def _vm_do_media_and_save(page, vin, images):
    await vm_open_merchandising_tab(page)
    await vm_remove_all_photos(page)
    if not await vm_upload_images(page, images):
        return False
    if not await vm_save(page):
        return False
    await vm_go_inventory(page)
    return True


# ── vAuto page actions ───────────────────────────────────────────────────────
async def va_login(page, user, pw, after_epoch, lane):
    """vAuto/Cox Automotive login: username -> Next -> password -> Sign in ->
    email verification. Each vAuto account is its own separate login — there's
    no shared-login rooftop switching here."""
    set_step(2)
    log("info", f"Opening vAuto login for {user} ...")
    try:
        await page.goto(VA_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("warn", f"Could not open the vAuto login page: {e}")
    await safe_wait(page, 30000, 2.5)

    filled_u = False
    for sel in ['input#username', 'input[name="username"]', 'input[type="text"]']:
        try:
            box = page.locator(sel).first
            await box.click(timeout=4000)
            await box.press("Control+A"); await box.press("Backspace")
            await box.press_sequentially(user, delay=45)
            filled_u = True
            break
        except Exception:
            continue
    if not filled_u:
        log("warn", "Could not fill the vAuto username field")

    if not await _click_first(page, ['button:has-text("Next")'], label="Clicked Next"):
        try:
            await page.keyboard.press("Enter")
        except Exception:
            pass
    await safe_wait(page, 20000, 2.5)

    filled_p = False
    for sel in ['input#password', 'input[name="password"]', 'input[type="password"]']:
        try:
            box = page.locator(sel).first
            await box.wait_for(state="visible", timeout=10000)
            await box.click(timeout=4000)
            await box.press("Control+A"); await box.press("Backspace")
            await box.press_sequentially(pw, delay=45)
            filled_p = True
            break
        except Exception:
            continue
    if not filled_p:
        log("warn", "Could not fill the vAuto password field")

    if not await _click_first(page, ['button:has-text("Sign in")'], label="Clicked Sign in"):
        try:
            await page.keyboard.press("Enter")
        except Exception:
            pass
    await safe_wait(page, 20000, 3.0)

    await _click_first(page, ['#button-verify-by-email', 'button:has-text("Select")'],
                       label="Selected email verification")
    await safe_wait(page, 15000, 2.0)

    otp = None
    if GMAIL_ADDR and GMAIL_PASS:
        log("info", "Fetching verification code from Gmail ...")
        try:
            from otp_reader import get_otp
            otp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: get_otp(GMAIL_ADDR, GMAIL_PASS, after_epoch, timeout=OTP_TIMEOUT,
                                       log=log, sender=VA_OTP_SENDER, subject_hint=VA_OTP_SUBJECT,
                                       sender_domain="coxautoinc.com"))
        except Exception as e:
            log("warn", f"Auto-OTP failed: {e}")

    if not otp:
        otp = await _wait_for_lane_otp(lane, user, "Enter the verification code emailed for")
        if stop_flag.is_set():
            return False

    if not otp:
        log("error", "No verification code available.")
        return False

    typed = False
    for sel in ['#input-verification-code', 'input[placeholder*="one time code" i]']:
        try:
            box = page.locator(sel).first
            await box.click(timeout=4000)
            await box.press("Control+A"); await box.press("Backspace")
            await box.press_sequentially(otp, delay=45)
            typed = True
            break
        except Exception:
            continue
    if not typed:
        try:
            await page.keyboard.type(otp, delay=80)
        except Exception:
            pass

    if not await _click_first(page, ['button:has-text("Verify")'], label="Clicked Verify"):
        try:
            await page.keyboard.press("Enter")
        except Exception:
            pass
    await safe_wait(page, 30000, 3.0)
    log("ok", f"Logged in: {user}")

    await va_open_inventory(page)
    return True


async def va_open_inventory(page):
    """Navigate to Media Management. Post-login landing point and the reset
    point between VINs."""
    set_step(4)
    already_there = "mediamanagement" in (page.url or "").lower()
    if already_there:
        log("info", "Already on Media Management — waiting for it to settle ...")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
    else:
        log("info", "Opening vAuto Media Management ...")
        try:
            await page.goto(VA_MEDIA_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log("warn", f"Could not navigate to Media Management: {e}")
    await safe_wait(page, 30000, 3.0)
    try:
        await page.wait_for_selector('input[placeholder="VIN or Stock Number"]', timeout=20000)
        log("ok", "Media Management page is ready")
    except Exception:
        log("warn", "Media Management page didn't confirm ready within 20s")


async def va_filter_vin(page, vin):
    set_step(5)
    filled = False
    for sel in ['input[placeholder="VIN or Stock Number"]', 'input[placeholder*="VIN" i]']:
        try:
            box = page.locator(sel).first
            await box.wait_for(state="visible", timeout=8000)
            await box.click(timeout=4000)
            await box.press("Control+A"); await box.press("Backspace")
            await box.press_sequentially(vin, delay=30)
            filled = True
            break
        except Exception:
            continue

    if not filled:
        log("warn", f"Could not find the VIN search field for {vin} (page url: {page.url})")
        return False

    log("ok", f"Entered VIN: {vin}")
    try:
        await page.keyboard.press("Enter")
    except Exception:
        pass
    await safe_wait(page, 20000, 2.5)
    return True


async def va_open_vehicle(page, vin):
    for sel in ['.vehicle-card', '.listing-card']:
        try:
            await page.locator(sel).first.click(timeout=6000)
            log("ok", "Opened vehicle record")
            await safe_wait(page, 20000, 2.5)
            return True
        except Exception:
            continue

    try:
        links = await page.locator("a").all()
        for lk in links:
            txt = (await lk.inner_text(timeout=500) or "").strip()
            if re.match(r"^(19|20)\d{2}\s+\S+", txt):
                await lk.click(timeout=4000)
                log("ok", f"Opened vehicle record ({txt[:30]})")
                await safe_wait(page, 20000, 2.5)
                return True
    except Exception:
        pass
    log("error", f"Could not open the vehicle record for {vin}")
    return False


async def va_open_media_tab(page):
    await _click_first(page, ['a:has-text("Media")', 'button:has-text("Media")',
                              'li:has-text("Media")'], label="Opened Media tab")
    await safe_wait(page, 12000, 2.0)

    # The vehicle-detail modal's Photos panel renders inside its own iframe
    # (src contains "Va/Ranking/Vehicl"). frame_locator() reaches into it with
    # its own auto-waiting, so no manual frame-scanning/retry loop is needed.
    vp = page.frame_locator('iframe[src*="Va/Ranking/Vehicl"]').locator("#vehicle-photos-subnav")
    try:
        await vp.first.click(timeout=15000, force=True)
        log("ok", "Selected Vehicle Photos")
    except Exception as e:
        log("warn", f"Could not click Vehicle Photos in the vehicle-detail iframe ({e}) — "
                     f"trying the main page directly")
        await _click_first(page, ['#vehicle-photos-subnav a', '#vehicle-photos-subnav',
                                  'a:has-text("Vehicle Photos")', 'text=Vehicle Photos'],
                           timeout=6000, force=True, label="Selected Vehicle Photos")

    await safe_wait(page, 10000, 1.5)


async def va_remove_all_photos(page):
    """Check the Select All checkbox, click #delete-photos-btn, confirm in the
    custom DOM modal (button.confirm-btn) — not a native dialog, a real
    element. All three live inside the same vehicle-detail iframe as Vehicle
    Photos above. Uses a plain .click() rather than .check(): this is a custom
    web-component checkbox, so .check()'s native-property assertion doesn't
    reliably apply even though the click itself lands and selects everything."""
    va_frame = page.frame_locator('iframe[src*="Va/Ranking/Vehicl"]')

    try:
        await va_frame.locator("input.merch-select-all").first.click(timeout=6000, force=True)
        log("ok", "Clicked Select All")
    except Exception as e:
        log("info", f"Select All checkbox not clickable ({e}) — assuming no existing photos")
        return

    await asyncio.sleep(0.5)
    try:
        await va_frame.locator("#delete-photos-btn").first.click(timeout=6000)
        log("info", "Clicked Delete")
    except Exception as e:
        log("info", f"Delete button not clickable ({e}) — assuming no photos to delete")
        return

    await asyncio.sleep(0.8)
    try:
        await va_frame.locator("button.confirm-btn").first.click(timeout=8000)
        log("ok", "Confirmed delete in the dialog")
    except Exception as e:
        log("warn", f"Could not confirm the delete dialog: {e}")
    await safe_wait(page, 20000, 3.0)


async def va_upload_images(page, images):
    set_step(6)
    log("info", f"Uploading {len(images)} image(s) ...")

    va_frame = page.frame_locator('iframe[src*="Va/Ranking/Vehicl"]')

    done = False
    try:
        await va_frame.locator('input[type="file"]').first.set_input_files(images, timeout=15000)
        log("ok", f"Set {len(images)} files on upload input")
        done = True
    except Exception:
        pass

    if not done:
        try:
            await va_frame.locator('button:has-text("Upload Photos")').first.click(timeout=4000)
            await safe_wait(page, 6000, 1.0, rezoom=False)
            await va_frame.locator('input[type="file"]').first.set_input_files(images, timeout=15000)
            log("ok", f"Set {len(images)} files on upload input (after clicking Upload Photos)")
            done = True
        except Exception:
            pass

    if not done:
        try:
            async with page.expect_file_chooser(timeout=15000) as fc_info:
                await va_frame.locator('text=Upload Photos from your device').first.click(timeout=6000)
            fc = await fc_info.value
            await fc.set_files(images)
            log("ok", f"Set {len(images)} files via file chooser")
            done = True
        except Exception as e:
            log("error", f"Upload attempt failed: {e}")
            return False

    log("info", "Waiting for the upload to complete ...")
    deadline = time.time() + max(180, len(images) * 6)
    finished = False
    while time.time() < deadline:
        try:
            if await va_frame.locator('text=/uploaded successfully/i').first.is_visible(timeout=1000):
                finished = True
                break
            if await va_frame.locator(f'text=/Upload Complete {len(images)} of {len(images)}/') \
                    .first.is_visible(timeout=1000):
                finished = True
                break
        except Exception:
            pass
        await asyncio.sleep(2.0)

    if finished:
        log("ok", "Upload finished")
    else:
        log("warn", "Didn't confirm upload completion within the timeout — proceeding anyway")

    try:
        banner_close = page.locator('text=/uploaded successfully/i') \
                            .locator('xpath=following::button[1]')
        if await banner_close.is_visible(timeout=2000):
            await banner_close.click(timeout=2000)
    except Exception:
        pass
    return True


async def va_close_vehicle(page):
    """Close the vehicle detail overlay via its own close icon, falling back
    to a direct navigation back to Media Management if it can't be found."""
    log("info", "Closing vehicle record ...")
    if await _click_first(page, [
        'a[onclick*="onCloseClick"]',
        'button[aria-label="Close" i]',
        '[aria-label="Close" i]',
        'button:has-text("×")',
        'a:has-text("×")',
        'svg[aria-label="Close" i]',
    ], timeout=3000):
        await safe_wait(page, 15000, 2.0)
        return
    log("info", "Close icon not found — navigating back to Media Management directly")
    await va_open_inventory(page)


async def _va_do_media_and_save(page, vin, images):
    """Media tab -> Vehicle Photos -> select all -> delete -> upload -> close.
    vAuto has no separate Save step — delete and upload each take effect
    immediately. Returns True only if the upload was confirmed."""
    await va_open_media_tab(page)
    await va_remove_all_photos(page)
    if not await va_upload_images(page, images):
        return False
    await va_close_vehicle(page)
    return True


# ── per-DMS driver tables ────────────────────────────────────────────────────
LOGIN_FN = {
    "dealercenter": dc_login,
    "vinmotion": vm_login,
    "vauto": va_login,
}


async def _do_media_and_save(page, vin, images):
    await dc_open_media_tab(page)
    await dc_remove_all_photos(page)
    if not await dc_upload_images(page, images):
        return False
    return await dc_save_and_close(page)


DRIVERS = {
    "dealercenter": {
        "go_home": dc_go_home,
        "open_inventory": dc_open_active_inventory,
        "filter_vin": dc_filter_vin,
        "open_vehicle": dc_open_vehicle,
        "media_and_save": _do_media_and_save,
    },
    "vinmotion": {
        "go_home": vm_open_inventory,
        "open_inventory": vm_open_inventory,
        "filter_vin": vm_filter_vin,
        "open_vehicle": vm_open_vehicle,
        "media_and_save": _vm_do_media_and_save,
    },
    "vauto": {
        "go_home": va_open_inventory,
        "open_inventory": va_open_inventory,
        "filter_vin": va_filter_vin,
        "open_vehicle": va_open_vehicle,
        "media_and_save": _va_do_media_and_save,
    },
}


async def switch_rooftop(dms, page, cred):
    """Dispatch to the right rooftop-switch function for this DMS. Always
    called for every account (even the first, even single-account groups) —
    the guard logic inside each dc_/vm_ switch function decides whether a
    real switch is needed."""
    rooftop = cred.get("rooftop") or cred.get("name")
    if dms == "dealercenter":
        company = cred.get("company") or rooftop
        return await dc_switch_rooftop(page, company, rooftop)
    elif dms == "vinmotion":
        return await vm_switch_rooftop(page, rooftop)
    elif dms == "vauto":
        log("info", "vAuto accounts are one login per enterprise — "
                     "rooftop switching isn't needed for this DMS")
        return True
    return False


# ── VIN Organizer core (also used for the auto-move + live/manual features) ──
ACCOUNT_HEADER_RE = re.compile(r'ACCOUNT:\s*(.+?)\s*\(')
FAILED_VIN_RE = re.compile(r'✗\s*([A-Z0-9]{6,17})\s*failed after\s*\d+\s*attempt')
INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*]')
MASTER_FOLDER_NAME = "Master"


def sanitize_folder_name(name):
    cleaned = INVALID_CHARS_RE.sub('_', name).strip()
    return cleaned or "Unknown_Enterprise"


def find_vin_folder(root, vin, master_path):
    """Recursively search `root` for a directory whose name matches `vin`
    (case-insensitive), skipping anything already inside Master/ so re-runs
    (or auto-move racing a manual run) can't double-move or self-match."""
    root_abs = os.path.abspath(root)
    master_abs = os.path.abspath(master_path)
    for dirpath, dirnames, _filenames in os.walk(root_abs, topdown=True):
        dp_abs = os.path.abspath(dirpath)
        if dp_abs == master_abs or dp_abs.startswith(master_abs + os.sep):
            dirnames[:] = []
            continue
        for d in dirnames:
            if d.upper() == vin.upper():
                return os.path.join(dirpath, d)
    return None


def move_vin_to_master(root_path, enterprise_name, vin):
    """The actual move: find <root_path>/.../<VIN> (skipping Master/), and
    move it to <root_path>/Master/<enterprise_name>/<VIN>. Returns a result
    dict with vin/enterprise/source_path/dest_path/status — same shape the
    VIN Organizer's table already expects."""
    enterprise = sanitize_folder_name(enterprise_name)
    master_path = os.path.join(root_path, MASTER_FOLDER_NAME)
    row = {"vin": vin, "enterprise": enterprise_name, "source_path": None,
           "dest_path": None, "status": ""}
    try:
        src = find_vin_folder(root_path, vin, master_path)
        if not src:
            row["status"] = "not found"
            return row
        row["source_path"] = src
        dest_dir = os.path.join(master_path, enterprise)
        dest_path = os.path.join(dest_dir, os.path.basename(src))
        row["dest_path"] = dest_path
        if os.path.exists(dest_path):
            row["status"] = "skipped (already exists at destination)"
        else:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.move(src, dest_path)
            row["status"] = "moved"
    except Exception as e:
        row["status"] = f"error: {e}"
    return row


def parse_failed_vins(log_text):
    """Used by the live/manual Organizer (parses reconstructed Combined
    Activity text) and by the file-upload Organizer."""
    current_enterprise = "Unknown Enterprise"
    results = []
    seen = set()
    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        acct_match = ACCOUNT_HEADER_RE.search(line)
        if acct_match:
            current_enterprise = acct_match.group(1).strip()
            continue
        vin_match = FAILED_VIN_RE.search(line)
        if vin_match:
            vin = vin_match.group(1).strip()
            key = (vin, current_enterprise)
            if key not in seen:
                seen.add(key)
                results.append({"vin": vin, "enterprise": current_enterprise})
    return results


def run_organizer(log_text, path, preview_only):
    """File-upload / manual-process path: parses ACCOUNT headers + failed-VIN
    lines out of a block of log text, then moves (or previews moving) each."""
    if not os.path.isdir(path):
        return {"ok": False, "error": f"That path was not found on this machine: {path}"}

    failed_vins = parse_failed_vins(log_text)
    master_path = os.path.join(path, MASTER_FOLDER_NAME)

    if not failed_vins:
        return {
            "ok": True,
            "summary": {"total": 0, "moved": 0, "skipped": 0, "not_found": 0, "errors": 0},
            "results": [], "preview": preview_only, "master_path": master_path,
        }

    if not preview_only:
        os.makedirs(master_path, exist_ok=True)

    results, counts = [], {"moved": 0, "skipped": 0, "not_found": 0, "errors": 0}
    for item in failed_vins:
        vin, enterprise_raw = item["vin"], item["enterprise"]
        if preview_only:
            master_p = os.path.join(path, MASTER_FOLDER_NAME)
            src = find_vin_folder(path, vin, master_p)
            if not src:
                results.append({"vin": vin, "enterprise": enterprise_raw, "source_path": None,
                                "dest_path": None, "status": "not found"})
                counts["not_found"] += 1
                continue
            enterprise = sanitize_folder_name(enterprise_raw)
            dest_path = os.path.join(master_p, enterprise, os.path.basename(src))
            results.append({"vin": vin, "enterprise": enterprise_raw, "source_path": src,
                            "dest_path": dest_path, "status": "would move"})
            counts["moved"] += 1
            continue

        row = move_vin_to_master(path, enterprise_raw, vin)
        results.append(row)
        if row["status"] == "moved":
            counts["moved"] += 1
        elif row["status"].startswith("skipped"):
            counts["skipped"] += 1
        elif row["status"] == "not found":
            counts["not_found"] += 1
        else:
            counts["errors"] += 1

    return {
        "ok": True,
        "summary": {"total": len(failed_vins), **counts},
        "results": results, "preview": preview_only, "master_path": master_path,
    }


def build_live_activity_text():
    """Reconstruct a log-text blob for the Organizer's parser out of the
    CURRENT run's Combined Activity — one lane's entries at a time, in order,
    so ACCOUNT headers stay correctly adjacent to their own VIN lines even
    though lanes ran concurrently and their entries interleaved in real time."""
    with STATE_LOCK:
        lines = []
        for lane in state["lanes"]:
            for entry in lane.get("log", []):
                lines.append(f"{entry['t']} {entry['msg']}")
    return "\n".join(lines)


# ── auto-move (Combined Activity → Master/Enterprise/VIN, no upload needed) ──
def _maybe_auto_move(root_path, enterprise, vin):
    if not SETTINGS.get("auto_move_failed_vins", True):
        return
    if not root_path:
        return
    try:
        row = move_vin_to_master(root_path, enterprise, vin)
        if row["status"] == "moved":
            log("ok", f"Auto-moved {vin} -> Master/{sanitize_folder_name(enterprise)}/")
        elif row["status"] == "not found":
            log("warn", f"Auto-move: could not find a folder for {vin} under {root_path}")
        elif row["status"].startswith("skipped"):
            log("info", f"Auto-move: {vin} already present at destination — skipped")
        else:
            log("warn", f"Auto-move failed for {vin}: {row['status']}")
    except Exception as e:
        log("warn", f"Auto-move errored for {vin}: {e}")


# ── one VIN, start to finish (with configurable retry) ──────────────────────
async def _process_one_vin_async(page, driver, vin, images):
    retries = int(SETTINGS.get("retry_count", 2)) if SETTINGS.get("auto_retry_failed_vins", True) else 0
    total_attempts = 1 + max(0, retries)
    last_error = None
    for attempt in range(1, total_attempts + 1):
        if stop_flag.is_set():
            break
        _update_vin_op(retry=attempt - 1, status="running",
                       current_action=f"Attempt {attempt}/{total_attempts}: resetting to inventory")
        try:
            if attempt > 1:
                log("info", f"Retrying {vin}: back to inventory and re-entering VIN ...")
            await driver["go_home"](page)
            await driver["open_inventory"](page)
            _update_vin_op(current_action="Filtering VIN")
            if not await driver["filter_vin"](page, vin):
                log("warn", f"Attempt {attempt} for {vin} did not confirm a successful save")
                continue
            _update_vin_op(current_action="Opening vehicle record")
            if not await driver["open_vehicle"](page, vin):
                log("warn", f"Attempt {attempt} for {vin} did not confirm a successful save")
                continue
            _update_vin_op(current_action="Clearing old photos & uploading")
            if await driver["media_and_save"](page, vin, images):
                return True, None, attempt, total_attempts
            log("warn", f"Attempt {attempt} for {vin} did not confirm a successful save")
        except Exception as e:
            last_error = e
            log("error", f"Attempt {attempt} for {vin} errored: {e}")
    return False, last_error, total_attempts, total_attempts


async def process_account_vins_async(ctx, acc, dms, lane, chrome_label, max_tabs, ui,
                                      enterprise_label, rooftop_label, root_path):
    """Distributes one account's VIN folders across up to `max_tabs` real
    browser tabs (Playwright pages) inside the SAME context/session — this is
    the actual concurrency mechanism, not separate browser processes."""
    driver = DRIVERS[dms]
    vins = list(acc["vins"])
    if not vins:
        ui["status"] = "done"
        return

    max_tabs = max(1, min(max_tabs, len(vins)))
    queue = asyncio.Queue()
    ops_by_vin = {}
    for v in vins:
        op = _new_vin_op(dms, enterprise_label, acc["folder_name"], lane, chrome_label,
                         None, v["vin"],
                         1 + (int(SETTINGS.get("retry_count", 2)) if SETTINGS.get("auto_retry_failed_vins", True) else 0))
        op["rooftop"] = rooftop_label or ""
        ops_by_vin[v["vin"]] = op
        queue.put_nowait(v)

    async def worker(tab_no):
        page = await ctx.new_page()
        page.set_default_timeout(60000)
        try:
            while not stop_flag.is_set():
                try:
                    v = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                vin, images = v["vin"], v["images"]
                op = ops_by_vin[vin]
                op["tab"] = tab_no
                op["start_ts"] = time.time()
                op["status"] = "running"
                token = _tab_ctx.set({"op": op})
                log("info", f"── VIN {vin} ({len(images)} images) [tab {tab_no}] ──")
                try:
                    ok, err, attempt, total = await _process_one_vin_async(page, driver, vin, images)
                finally:
                    _tab_ctx.reset(token)

                with STATE_LOCK:
                    op["end_ts"] = time.time()
                    op["status"] = "success" if ok else "failed"
                    op["error"] = str(err) if err else None
                    op["current_action"] = "Complete" if ok else "Failed"
                    if ok:
                        ui["done"] += 1
                    else:
                        ui["failed"] += 1

                if ok:
                    log("ok", f"✓ {vin} complete [tab {tab_no}]")
                else:
                    suffix = f": {err}" if err else ""
                    log("error", f"✗ {vin} failed after {total} attempt(s){suffix} [tab {tab_no}]")
                    _maybe_auto_move(root_path, enterprise_label, vin)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    await asyncio.gather(*(worker(i + 1) for i in range(max_tabs)))
    ui["status"] = "done"


# ── one Chrome window ("lane") — processes its assigned login-groups
#    sequentially; each account's VINs run across up to max_tabs_per_dms tabs ──
async def run_dms_lane(lane, accounts, login_groups, root_path):
    browser = None
    async_pw = None
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log("error", "Playwright isn't installed — run: pip install -r requirements.txt "
                     "&& python -m playwright install chromium")
        lane["status"] = "error"
        return

    async with async_playwright() as p:
        for group in login_groups:
            if stop_flag.is_set():
                break
            first_i = group[0]
            first_acc = accounts[first_i]
            dms = first_acc["cred"]["dms"]
            login_fn = LOGIN_FN[dms]
            driver = DRIVERS[dms]
            lane["dms"] = dms

            need_new_browser = browser is None or not SETTINGS.get("browser_reuse", True)
            if need_new_browser:
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        pass
                browser = await p.chromium.launch(headless=SETTINGS.get("headless", False),
                                                  args=["--start-maximized"])
                with STATE_LOCK:
                    lane["chrome_open"] = True

            ctx = await browser.new_context(no_viewport=True)
            page = await ctx.new_page()
            page.set_default_timeout(60000)

            lane["current_account"] = first_acc["folder_name"]
            log("info", f"Logging in ({dms}) as '{first_acc['cred']['user']}' for the "
                        f"'{first_acc['folder_name']}' login group ...")

            ok = await login_fn(page, first_acc["cred"]["user"], first_acc["cred"]["pass"],
                                time.time(), lane)
            if not ok:
                log("error", f"Login failed for {first_acc['folder_name']} — skipping this login group")
                for i in group:
                    ui = lane["accounts_by_name"].get(accounts[i]["folder_name"])
                    if ui:
                        ui["status"] = "error"
                await ctx.close()
                continue

            enterprise_label = first_acc["folder_name"]

            for pos, i in enumerate(group):
                if stop_flag.is_set():
                    break
                acc = accounts[i]
                ui = lane["accounts_by_name"].get(acc["folder_name"])
                if ui is None:
                    continue
                ui["status"] = "running"
                lane["current_account"] = acc["folder_name"]
                if pos == 0:
                    log("info", f"══════ ACCOUNT: {acc['folder_name']} ({acc['cred']['user']}) ══════")
                else:
                    log("info", f"══════ ACCOUNT: {acc['folder_name']} (same login, switching rooftop) ══════")

                rooftop = acc["cred"].get("rooftop") or acc["folder_name"]
                lane["current_rooftop"] = rooftop
                switched = await switch_rooftop(dms, page, acc["cred"])
                if not switched:
                    log("info", f"Retrying rooftop switch for '{rooftop}' after reloading Inventory ...")
                    await driver["open_inventory"](page)
                    switched = await switch_rooftop(dms, page, acc["cred"])
                if not switched:
                    ui["status"] = "error"
                    log("error", f"Could not switch to rooftop '{rooftop}' for "
                                 f"{acc['folder_name']} — skipping this account")
                    continue

                with STATE_LOCK:
                    active_lanes = max(1, sum(1 for l in state["lanes"] if l.get("chrome_open")))
                per_lane_cap = max(1, int(SETTINGS.get("max_total_vin_concurrency", 9)) // active_lanes)
                max_tabs = max(1, min(int(SETTINGS.get("max_tabs_per_dms", 3)), per_lane_cap))

                await process_account_vins_async(ctx, acc, dms, lane, lane["label"], max_tabs, ui,
                                                 enterprise_label, rooftop, root_path)

            await ctx.close()

        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        with STATE_LOCK:
            lane["chrome_open"] = False

    lane["current_account"] = None
    lane["current_rooftop"] = None
    if lane["status"] != "error":
        lane["status"] = "stopped" if stop_flag.is_set() else "done"


def _lane_thread_entry(lane, accounts, login_groups, root_path):
    _lane_ctx.lane = lane
    lane["status"] = "running"
    try:
        asyncio.run(run_dms_lane(lane, accounts, login_groups, root_path))
    except Exception as e:
        log("error", f"Chrome window '{lane['label']}' crashed: {e}")
        lane["status"] = "error"


def build_lane_plan(accounts, usable_indices):
    """Groups usable accounts by shared (dms, user, pass) login first, then
    decides how those login-groups are bundled into Chrome-window "lanes"
    depending on one_account_per_dms_window."""
    groups, seen = [], {}
    for i in usable_indices:
        cred = accounts[i]["cred"]
        key = (cred["dms"], cred["user"], cred["pass"])
        if key not in seen:
            seen[key] = []
            groups.append(seen[key])
        seen[key].append(i)

    if not groups:
        return []

    if SETTINGS.get("one_account_per_dms_window", True):
        by_dms, order = {}, []
        for g in groups:
            dms = accounts[g[0]]["cred"]["dms"]
            by_dms.setdefault(dms, []).append(g)
            if dms not in order:
                order.append(dms)
        return [{"label_hint": DMS_LABELS.get(dms, dms), "groups": by_dms[dms]} for dms in order]
    else:
        # One login-group per lane — the ThreadPoolExecutor's worker cap
        # (max_dms_windows) is what actually throttles concurrency, so
        # different DMS types can share the pool of Chrome windows freely.
        return [{"label_hint": DMS_LABELS.get(accounts[g[0]]["cred"]["dms"], "DMS"), "groups": [g]}
                for g in groups]


def run_automation(root_str):
    root = Path(root_str)
    stop_flag.clear()
    with STATE_LOCK:
        state.update({"run_status": "running", "root_path": str(root),
                      "lanes": [], "vin_ops": [], "log": []})

    creds = load_accounts_credentials()
    if not creds:
        log("error", "No accounts in .env.local (DC_ACCOUNT_1_NAME/USER/PASS ..., "
                     "VM_ACCOUNT_1_NAME/USER/PASS ..., and/or VA_ACCOUNT_1_NAME/USER/PASS ...)")
        state["run_status"] = "error"
        return
    if not (GMAIL_ADDR and GMAIL_PASS):
        log("warn", "No Gmail configured — MFA/verification codes will fall back to "
                     "manual entry each login.")

    accounts = scan_root(root, creds)
    for a in accounts:
        if not a["cred"]:
            log("warn", f"  '{a['folder_name']}' — no matching credentials, will skip")
        elif not a["vins"]:
            log("warn", f"'{a['folder_name']}' has no VIN folders — skipping")
        else:
            log("info", f"  '{a['folder_name']}' -> {len(a['vins'])} VIN(s) [{a['cred']['dms']}]")

    usable = [i for i, a in enumerate(accounts) if a["cred"] and a["vins"]]
    log("ok", f"Found {len(accounts)} account folder(s), {len(usable)} usable")

    lane_plans = build_lane_plan(accounts, usable)
    if not lane_plans:
        log("warn", "No usable accounts with matching credentials and VIN folders were found.")
        state["run_status"] = "done"
        return

    max_windows = max(1, min(int(SETTINGS.get("max_dms_windows", 3)), 5))
    if not SETTINGS.get("auto_queue_when_full", True) and len(lane_plans) > max_windows:
        accepted, rejected = lane_plans[:max_windows], lane_plans[max_windows:]
        for plan in rejected:
            for g in plan["groups"]:
                for i in g:
                    log("warn", f"Skipping '{accounts[i]['folder_name']}' — no free DMS window "
                                 f"and auto-queue is off")
        lane_plans = accepted

    lanes = []
    for idx, plan in enumerate(lane_plans):
        lane = {
            "id": f"lane_{idx + 1}", "label": f"Chrome #{idx + 1} — {plan['label_hint']}",
            "dms": None, "status": "pending", "otp_prompt": None,
            "otp_event": threading.Event(), "otp_code": {"code": None},
            "current_account": None, "current_rooftop": None, "step": 0,
            "chrome_open": False, "log": [], "accounts_by_name": {},
        }
        for g in plan["groups"]:
            for i in g:
                acc = accounts[i]
                lane["accounts_by_name"][acc["folder_name"]] = {
                    "name": acc["folder_name"], "user": acc["cred"]["user"],
                    "dms": acc["cred"]["dms"], "total": len(acc["vins"]),
                    "done": 0, "failed": 0, "status": "pending",
                }
        lanes.append(lane)
        with STATE_LOCK:
            state["lanes"].append(lane)

    log("info", f"Starting {len(lanes)} Chrome window(s) (max {max_windows} at a time), "
                f"up to {SETTINGS.get('max_tabs_per_dms', 3)} VIN tab(s) each ...")

    with ThreadPoolExecutor(max_workers=max_windows) as executor:
        futures = [executor.submit(_lane_thread_entry, lane, accounts, plan["groups"], str(root))
                  for lane, plan in zip(lanes, lane_plans)]
        for f in futures:
            try:
                f.result()
            except Exception as e:
                log("error", f"A Chrome window crashed: {e}")

    with STATE_LOCK:
        total_done = sum(a["done"] for l in state["lanes"] for a in l["accounts_by_name"].values())
        total_failed = sum(a["failed"] for l in state["lanes"] for a in l["accounts_by_name"].values())
    log("ok", f"━━━ Finished — {total_done} uploaded, {total_failed} failed ━━━")
    state["run_status"] = "stopped" if stop_flag.is_set() else "done"


# ── CSV Generator (counts images in every leaf folder under a path) ─────────
CSVGEN_DEFAULT_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp",
                        ".heic", ".heif"}


def _csvgen_is_image(name: str, exts: set) -> bool:
    return Path(name).suffix.lower() in exts


def _csvgen_count_images(dir_path: Path, recursive: bool, exts: set) -> int:
    if not recursive:
        return sum(1 for f in dir_path.iterdir()
                   if f.is_file() and _csvgen_is_image(f.name, exts))
    count = 0
    for _, _, files in os.walk(dir_path):
        for f in files:
            if _csvgen_is_image(f, exts):
                count += 1
    return count


def _csvgen_find_leaf_dirs(root: Path):
    for current, dirs, _ in os.walk(root):
        cur_path = Path(current)
        if cur_path == root:
            continue
        if len(dirs) == 0:
            yield cur_path


def run_csv_generator(root_str: str, recursive: bool, exts: set):
    root = Path(root_str)
    leaves = list(_csvgen_find_leaf_dirs(root))

    if not leaves:
        return {
            "ok": True, "rows": [], "columns": ["Images_Count"],
            "total_leaf_folders": 0, "total_images": 0, "csv_path": None,
            "message": f"No leaf folders (folders with no subfolders) were found under {root}.",
        }

    rel_parts_list = [leaf.relative_to(root).parts for leaf in leaves]
    max_depth = max(len(parts) for parts in rel_parts_list)
    layer_cols = [f"Layer{i + 1}" for i in range(max_depth)]

    rows = []
    total_images = 0
    for leaf, parts in zip(leaves, rel_parts_list):
        img_count = _csvgen_count_images(leaf, recursive, exts)
        total_images += img_count
        row = {layer_cols[i]: (parts[i] if i < len(parts) else "") for i in range(max_depth)}
        row["Images_Count"] = img_count
        rows.append(row)

    rows.sort(key=lambda r: tuple(r[c] for c in layer_cols))
    columns = layer_cols + ["Images_Count"]

    csv_filename = f"{root.name}_image_counts.csv"
    csv_path = root / csv_filename
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        csv_path_str = str(csv_path)
        csv_write_error = None
    except Exception as e:
        csv_path_str = None
        csv_write_error = str(e)

    return {
        "ok": True, "rows": rows, "columns": columns,
        "total_leaf_folders": len(rows), "total_images": total_images,
        "csv_path": csv_path_str, "csv_write_error": csv_write_error,
    }


# ── Flask app ─────────────────────────────────────────────────────────────
app = Flask(__name__)


def public_state():
    with STATE_LOCK:
        now = time.time()
        vin_ops_out = []
        counters = {"running": 0, "queued": 0, "completed": 0, "failed": 0}
        for op in state["vin_ops"][-1000:]:
            duration = None
            if op["start_ts"]:
                end = op["end_ts"] or now
                duration = round(end - op["start_ts"], 1)
            vin_ops_out.append({**op, "duration": duration})
            if op["status"] in counters:
                counters[op["status"] if op["status"] != "success" else "completed"] = \
                    counters.get(op["status"] if op["status"] != "success" else "completed", 0) + 1

        # recompute counters cleanly (avoid double counting above)
        counters = {"running": 0, "queued": 0, "completed": 0, "failed": 0}
        for op in state["vin_ops"]:
            if op["status"] == "running":
                counters["running"] += 1
            elif op["status"] == "queued":
                counters["queued"] += 1
            elif op["status"] == "success":
                counters["completed"] += 1
            elif op["status"] == "failed":
                counters["failed"] += 1

        lanes_out = []
        chrome_open_count = 0
        for l in state["lanes"]:
            if l.get("chrome_open"):
                chrome_open_count += 1
            lanes_out.append({
                "id": l["id"], "label": l["label"], "dms": l.get("dms"),
                "status": l["status"], "otp_prompt": l["otp_prompt"],
                "current_account": l["current_account"], "current_rooftop": l.get("current_rooftop"),
                "chrome_open": l.get("chrome_open", False),
                "accounts": list(l["accounts_by_name"].values()),
                "log": l["log"][-2000:],
            })

        max_windows = max(1, min(int(SETTINGS.get("max_dms_windows", 3)), 5))
        max_tabs = max(1, min(int(SETTINGS.get("max_tabs_per_dms", 3)), 5))

        return {
            "run_status": state["run_status"],
            "root_path": state["root_path"],
            "lanes": lanes_out,
            "vin_ops": vin_ops_out[-500:],
            "log": state["log"][-1000:],
            "counters": {
                **counters,
                "chrome_open": chrome_open_count,
                "chrome_max": max_windows,
                "tabs_max": max_windows * max_tabs,
            },
        }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    return jsonify(public_state())


@app.route("/api/start", methods=["POST"])
def api_start():
    global worker_thread
    body = request.get_json(force=True) or {}
    folder = (body.get("folder") or "").strip()
    if not folder or not Path(folder).is_dir():
        return jsonify({"ok": False, "error": f"Folder not found: {folder}"}), 400
    if worker_thread and worker_thread.is_alive():
        return jsonify({"ok": False, "error": "A run is already in progress"}), 400
    worker_thread = threading.Thread(target=run_automation, args=(folder,), daemon=True)
    worker_thread.start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    stop_flag.set()
    with STATE_LOCK:
        for l in state["lanes"]:
            l["otp_event"].set()
        state["run_status"] = "stopped"
    return jsonify({"ok": True})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    if worker_thread and worker_thread.is_alive():
        return jsonify({"ok": False, "error": "Stop the current run first"}), 400
    with STATE_LOCK:
        state.update({"run_status": "idle", "root_path": None, "lanes": [], "vin_ops": [], "log": []})
    stop_flag.clear()
    return jsonify({"ok": True})


@app.route("/api/otp", methods=["POST"])
def api_otp():
    body = request.get_json(force=True) or {}
    lane_id = body.get("lane_id") or body.get("job_id")
    code = (body.get("code") or "").strip()
    with STATE_LOCK:
        lane = next((l for l in state["lanes"] if l["id"] == lane_id), None)
    if not lane:
        return jsonify({"ok": False, "error": "Unknown Chrome window"}), 404
    lane["otp_code"]["code"] = code
    lane["otp_event"].set()
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify(SETTINGS)


@app.route("/api/settings", methods=["POST"])
def api_post_settings():
    body = request.get_json(force=True) or {}
    update_settings(body)
    return jsonify(SETTINGS)


@app.route("/api/organize", methods=["POST"])
def api_organize():
    """File-upload path — kept for analyzing older/saved logs."""
    try:
        uploaded = request.files.get("log_file")
        path = (request.form.get("path") or "").strip()
        preview_only = request.form.get("preview_only") == "true"

        if not uploaded or uploaded.filename == "":
            return jsonify({"ok": False, "error": "No activity log file was uploaded."}), 400
        if not path:
            return jsonify({"ok": False, "error": "Please paste the path where the VIN folders live."}), 400

        raw = uploaded.read()
        try:
            log_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            log_text = raw.decode("utf-8", errors="replace")

        result = run_organizer(log_text, path, preview_only)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/organize_live", methods=["POST"])
def api_organize_live():
    """Live/manual path — no upload needed. Reads straight off the current
    run's Combined Activity and sorts every failed VIN it finds."""
    try:
        body = request.get_json(force=True) or {}
        preview_only = bool(body.get("preview_only"))
        path = (body.get("path") or "").strip() or state.get("root_path")

        if not path:
            return jsonify({"ok": False, "error": "No path available — start a run first, "
                                                    "or paste a path manually."}), 400

        log_text = build_live_activity_text()
        if not log_text.strip():
            return jsonify({"ok": True,
                            "summary": {"total": 0, "moved": 0, "skipped": 0, "not_found": 0, "errors": 0},
                            "results": [], "preview": preview_only,
                            "master_path": os.path.join(path, MASTER_FOLDER_NAME)})

        result = run_organizer(log_text, path, preview_only)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/csvgen", methods=["POST"])
def api_csvgen():
    try:
        body = request.get_json(force=True) or {}
        path = (body.get("path") or "").strip()
        recursive = bool(body.get("recursive", False))
        exts_in = body.get("image_extensions")

        if not path:
            return jsonify({"ok": False, "error": "Please paste the path to scan."}), 400
        if not os.path.isdir(path):
            return jsonify({"ok": False, "error": f"Path not found on this machine: {path}"}), 400

        if isinstance(exts_in, list) and exts_in:
            exts = set()
            for e in exts_in:
                if isinstance(e, str) and e.strip():
                    e = e.strip().lower()
                    exts.add(e if e.startswith(".") else f".{e}")
        else:
            exts = set(CSVGEN_DEFAULT_EXTS)

        result = run_csv_generator(path, recursive, exts)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def open_browser():
    time.sleep(1.2)
    if not SETTINGS.get("auto_open_browser", True):
        return
    try:
        import webbrowser
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception:
        pass


if __name__ == "__main__":
    print("\n  DMS Media Suite — Spyne")
    print(f"  Control panel:  http://localhost:{PORT}\n")
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
