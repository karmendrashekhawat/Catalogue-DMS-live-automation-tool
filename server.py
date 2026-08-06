"""
DMS Media Uploader — Spyne
Local automation across two DMS platforms (DealerCenter and VinMotion): for every
account folder, log in (auto MFA/verification-code from Gmail), then for every VIN
folder: filter the VIN, open the record, clear old photos, upload the folder's
images, save, reset, next VIN.

Everything runs on your machine. Credentials live only in .env.local (git-ignored).
"""

import os
import re
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
VM_LOGIN_URL = CFG.get("vinmotion_url", "https://vinmotion.vehicledata.com/Inventory")
VA_LOGIN_URL = CFG.get("vauto_url", "https://provision.vauto.app.coxautoinc.com")
VA_MEDIA_URL = CFG.get("vauto_media_url",
                       "https://provision.vauto.app.coxautoinc.com/Va/Merchandising/MediaManagement.aspx")
PORT         = int(CFG.get("port", 7433))
IMG_EXTS     = set(CFG.get("image_extensions", [".jpg", ".jpeg", ".png"]))
SAVE_RETRIES = int(CFG.get("save_retry_attempts", 6))
OTP_TIMEOUT  = int(CFG.get("otp_timeout_seconds", 120))

GMAIL_ADDR   = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_PASS   = os.environ.get("GMAIL_APP_PASSWORD", "")

# Sender/subject used to auto-fetch each DMS's verification code from Gmail.
DC_OTP_SENDER  = "do-not-reply@dealercenter.net"
DC_OTP_SUBJECT = "Your authentication code"
VM_OTP_SENDER  = "msonlineservicesteam@microsoftonline.com"
VM_OTP_SUBJECT = "Dealer Specialties account email verification code"
VA_OTP_SENDER  = "no-reply@signin.coxautoinc.com"
VA_OTP_SUBJECT = "One-time Bridge ID code"


def load_accounts_credentials():
    """Read account credentials from env into {normalized_name: {...}}.

    Three prefixes are supported side by side:
      DC_ACCOUNT_n_{NAME,USER,PASS,ROOFTOP,COMPANY}  -> DealerCenter accounts
      VM_ACCOUNT_n_{NAME,USER,PASS,ROOFTOP}          -> VinMotion accounts
      VA_ACCOUNT_n_{NAME,USER,PASS}                  -> vAuto accounts

    ROOFTOP (and, for DealerCenter, COMPANY) are optional — set them when several
    account folders share one login and switch dealership/rooftop in-app instead
    of logging out and back in (see .env.local.example for the Capitol and
    Charlie Clark groups). vAuto accounts are always one login per enterprise,
    so there's no rooftop/company concept for them."""
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


def safe_wait(page, timeout=15000, settle=2.0, rezoom=True):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass
    time.sleep(settle)
    if rezoom:
        apply_zoom(page)
        dismiss_popups(page)


def dismiss_popups(page):
    """Close promotional/interstitial modals (webinar invites, 'what's new' dialogs, etc.)
    that can appear at any point and block clicks underneath them. Best-effort and
    silent — if nothing matches, this is a harmless no-op.

    Deliberately excludes anything belonging to Uppy (the VinMotion photo-upload
    widget) — its own close button also matches aria-label*="close", and closing
    it was accidentally dismissing the upload dialog we're trying to use."""
    for sel in [
        'button[aria-label*="close" i]', '[aria-label="Close" i]',
        '.modal button:has-text("×")', '[class*="modal" i] [class*="close" i]',
        'button:has-text("×")',
    ]:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=600):
                cls = (el.get_attribute("class") or "").lower()
                if "uppy" in cls:
                    continue
                el.click(timeout=1200)
                log("info", "Dismissed a popup")
                time.sleep(0.3)
        except Exception:
            continue


def apply_zoom(page, target_w=1600, target_h=900):
    """
    Zoom the page out so its effective content area is roughly target_w x target_h,
    without resizing the actual browser window (resizing the window is what caused
    the earlier "zoomed in" / flicker problem). Uses CSS zoom, which is cheap and
    doesn't reload or reflow-flash the page the way a real window resize does.
    Safe to call repeatedly — it's a no-op if the page is already at a good size.
    """
    try:
        dims = page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
        w, h = dims.get("w") or target_w, dims.get("h") or target_h
        pct = min(1.0, max(0.5, min(target_w / w, target_h / h)))
        page.evaluate(f"document.body.style.zoom = '{pct * 100:.0f}%'")
    except Exception:
        pass  # zoom is cosmetic — never let it break the automation


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
def _click_first(page, selectors, timeout=5000, label="", force=False):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.click(timeout=timeout, force=force)
            if label:
                log("ok", label)
            return True
        except Exception:
            continue
    return False


def _fill_and_submit_login(page, user, pw):
    """One attempt at filling username/password and submitting. Returns True if it
    looks like the form was submitted successfully (no lingering validation error)."""
    # username — type real keystrokes; DealerCenter's form validates on keyup/keydown,
    # and .fill() (which just sets the value + one 'input' event) doesn't satisfy it,
    # which is why Continue was staying permanently disabled.
    filled_u = False
    for sel in ['input[placeholder*="Username" i]', 'input[name*="user" i]',
                'input[id*="user" i]', 'input[type="text"]']:
        try:
            box = page.locator(sel).first
            box.click(timeout=4000)
            box.press("Control+A")
            box.press("Backspace")
            box.press_sequentially(user, delay=45)
            filled_u = True
            break
        except Exception:
            continue
    # password
    filled_p = False
    pw_box = None
    for sel in ['input[type="password"]', 'input[placeholder*="Password" i]',
                'input[name*="pass" i]']:
        try:
            box = page.locator(sel).first
            box.click(timeout=4000)
            box.press("Control+A")
            box.press("Backspace")
            box.press_sequentially(pw, delay=45)
            filled_p = True
            pw_box = box
            break
        except Exception:
            continue
    if not (filled_u and filled_p):
        log("warn", "Could not auto-fill login — fill it manually in the browser.")

    # Submit with Enter first — simplest and most reliable, since it's the browser's
    # own native form-submit path rather than us guessing at button clickability.
    submitted_via_enter = False
    if pw_box is not None:
        try:
            pw_box.press("Enter")
            submitted_via_enter = True
            log("info", "Pressed Enter to submit login")
        except Exception:
            pass
    time.sleep(1.5)

    # Did a field silently fail to fill? DealerCenter shows "Username is required"
    # (or similarly for password) right on the form when that happens — if so, this
    # whole attempt is a bust and the caller should reload and try again fresh.
    try:
        if page.locator('text=Username is required').first.is_visible(timeout=1500):
            log("warn", "Username field came out empty (validation error shown) — "
                        "this attempt failed")
            return False
    except Exception:
        pass

    # Did Enter already get us off the login page? If the username field is gone
    # (moved to MFA, or errored, or navigated), don't bother clicking anything.
    still_on_login = True
    try:
        still_on_login = page.locator('input[type="password"]').first.is_visible(timeout=2000)
    except Exception:
        still_on_login = False

    clicked = submitted_via_enter and not still_on_login
    if clicked:
        log("ok", "Enter submitted the login form")

    if not clicked:
        # Blur the password field so the form's JS validation runs and un-disables Continue
        try:
            page.keyboard.press("Tab")
        except Exception:
            pass
        time.sleep(0.5)

        # Continue can stay disabled for a moment while the form validates — retry.
        for _ in range(10):
            if _click_first(page, ['button:has-text("Continue")', 'button[type="submit"]',
                                   'button:has-text("Login")', 'button:has-text("Sign in")'],
                            timeout=3000):
                clicked = True
                break
            time.sleep(0.8)

    if not clicked:
        # The button is often genuinely enabled and clickable to a human (confirmed by
        # zooming out and clicking it manually) — Playwright's default click() just
        # refuses because its own "receives events" check gets confused by page layout.
        # force=True skips that check and clicks at the element's coordinates directly.
        for sel in ['button:has-text("Continue")', 'button[type="submit"]']:
            try:
                page.locator(sel).first.click(timeout=4000, force=True)
                clicked = True
                log("ok", "Clicked Continue (forced)")
                break
            except Exception:
                continue

    if not clicked:
        # last resort: click at the element's own screen coordinates via JS, which
        # sidesteps Playwright's actionability checks entirely.
        try:
            page.evaluate("""() => {
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
    safe_wait(page, 20000, 3.0)

    # one more check after submitting — the error can also appear post-click
    try:
        if page.locator('text=Username is required').first.is_visible(timeout=1500):
            log("warn", "Username validation error appeared after submit")
            return False
    except Exception:
        pass
    return True


def dc_login(page, user, pw, after_epoch):
    """Fill username/password, submit via Enter, then satisfy MFA."""
    set_step(2)
    log("info", f"Opening DealerCenter login for {user} ...")

    ok = False
    for attempt in range(2):
        if attempt > 0:
            log("info", "Reopening the DealerCenter login page and retrying ...")
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        safe_wait(page, 30000, 2.5)

        # Match the manual fix the user found: zoom the page out to 75%. At 100% zoom
        # something about the layout makes Playwright refuse to register the Continue
        # click as valid, even though a human click works fine.
        try:
            page.evaluate("document.body.style.zoom = '75%'")
            log("info", "Set page zoom to 75%")
        except Exception:
            pass

        if _fill_and_submit_login(page, user, pw):
            ok = True
            break

    if not ok:
        log("error", "Login form kept failing validation after a retry — giving up on this login")
        return False

    # ── MFA method selection ──────────────────────────────────────────────────
    # DealerCenter defaults to SMS ("Verify Your Identity — We've sent a text message").
    # Switch to email so the code lands where we can read it.
    set_step(3)
    if _click_first(page, ['text=Try another method', 'a:has-text("Try another method")',
                          'button:has-text("Try another method")'],
                    timeout=6000, label="Clicked 'Try another method'"):
        safe_wait(page, 10000, 1.5)
        _click_first(page, ['text=Email', 'button:has-text("Email")',
                            '[role="button"]:has-text("Email")'],
                     timeout=6000, label="Selected Email verification")
        safe_wait(page, 15000, 2.5)
    else:
        log("info", "No 'Try another method' link seen — assuming email MFA is already active")

    otp = None
    if GMAIL_ADDR and GMAIL_PASS:
        log("info", "Fetching MFA code from Gmail ...")
        try:
            from otp_reader import get_otp
            otp = get_otp(GMAIL_ADDR, GMAIL_PASS, after_epoch,
                          timeout=OTP_TIMEOUT, log=log,
                          sender=DC_OTP_SENDER, subject_hint=DC_OTP_SUBJECT)
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
    otp_box = None
    for sel in ['input[name*="otp" i]', 'input[placeholder*="code" i]',
                'input[autocomplete="one-time-code"]', 'input[type="tel"]']:
        try:
            box = page.locator(sel).first
            box.click(timeout=4000)
            box.press("Control+A")
            box.press("Backspace")
            box.press_sequentially(otp, delay=45)
            typed = True
            otp_box = box
            break
        except Exception:
            continue
    if not typed:
        # split boxes: type digit by digit into the visible short inputs
        try:
            boxes = page.locator('input[maxlength="1"]').all()
            if len(boxes) >= len(otp):
                for b, d in zip(boxes, otp):
                    b.click(timeout=2000)
                    b.press_sequentially(d, delay=45)
                typed = True
                otp_box = boxes[-1]
        except Exception:
            pass
    if not typed:
        # last resort: focus body and type
        try:
            page.keyboard.type(otp, delay=80)
            typed = True
        except Exception:
            pass

    # Submit with Enter first, same as the password step — only click if that fails.
    submitted = False
    if otp_box is not None:
        try:
            otp_box.press("Enter")
            submitted = True
            log("info", "Pressed Enter to submit MFA code")
        except Exception:
            pass
    time.sleep(1.5)

    still_on_otp = True
    try:
        still_on_otp = page.locator('input[placeholder*="code" i]').first.is_visible(timeout=2000)
    except Exception:
        still_on_otp = False

    if not submitted or still_on_otp:
        if _click_first(page, ['button:has-text("Verify")', 'button:has-text("Submit")',
                               'button:has-text("Continue")', 'button[type="submit"]'],
                        label="Submitted MFA code (clicked)"):
            submitted = True

    safe_wait(page, 40000, 4.0)
    log("ok", f"Logged in: {user}")
    return True


def dc_switch_rooftop(page, company_label, target_label):
    """
    For accounts that share one DealerCenter login across several rooftops (e.g. the
    Capitol group): click the COMPANY name in the top bar (this stays constant no
    matter which rooftop is currently active — DealerCenter itself labels it
    "Company Name" inside the dropdown), then pick the target rooftop from the
    "Switch Dealership" list. No logout/login/OTP needed.
    Returns True if the switch appears to have worked.
    """
    set_step(4)
    log("info", f"Switching rooftop: '{company_label}' → '{target_label}' ...")

    opened = False
    for sel in [f'text="{company_label}"', f'text={company_label}']:
        try:
            page.locator(sel).first.click(timeout=5000)
            if page.locator('text=Switch Dealership').first.is_visible(timeout=2500):
                opened = True
                break
        except Exception:
            continue

    if not opened:
        # fallback: try a few generic header candidates in case the company label
        # text didn't match exactly (e.g. extra whitespace/casing on the live page)
        for sel in ['[class*="dealer" i]', '[class*="rooftop" i]', 'header button', 'nav button']:
            try:
                candidates = page.locator(sel)
                for i in range(min(candidates.count(), 5)):
                    try:
                        candidates.nth(i).click(timeout=1500)
                        if page.locator('text=Switch Dealership').first.is_visible(timeout=1500):
                            opened = True
                            break
                        page.keyboard.press("Escape")
                    except Exception:
                        continue
                if opened:
                    break
            except Exception:
                continue

    if not opened:
        log("warn", f"Could not open the dealership switcher (looked for '{company_label}') — "
                     f"falling back to a fresh login instead")
        return False

    # Target uses an exact quoted match against the full label (including the
    # "(ID)" suffix DealerCenter shows) so it can't accidentally match a different
    # rooftop whose name is a substring of another (e.g. "Capitol Auto" vs
    # "Capitol Auto Of Smithfield").
    try:
        page.locator(f'text="{target_label}"').first.click(timeout=6000)
    except Exception as e:
        log("warn", f"Found the switcher but couldn't click '{target_label}': {e}")
        return False

    safe_wait(page, 30000, 3.0)
    log("ok", f"Switched to rooftop: {target_label}")
    return True


_ACTIVE_INVENTORY_FALLBACK_URL = (
    "https://app.dealercenter.net/apps/shell/reports/custom/"
    "inventoryreport/active-inventory-report?inventorystatus=0"
)


def _click_active_inventory_count(page):
    # The clickable element is the NUMBER just to the left of the "Active Inventory"
    # label, not the label itself — clicking the label text does nothing.
    for sel in [
        'xpath=//*[normalize-space(text())="Active Inventory"]/preceding::*[1]',
        'xpath=//*[contains(normalize-space(.),"Active Inventory")]/preceding-sibling::*[1]',
        'xpath=(//*[contains(normalize-space(.),"Active Inventory")])[1]'
        '/ancestor::*[self::a or self::button or @role="button"][1]',
    ]:
        try:
            page.locator(sel).first.click(timeout=4000)
            return True
        except Exception:
            continue
    return False


def _looks_like_404(page):
    try:
        return (page.locator('text=404 Page').first.is_visible(timeout=1500)
                or page.locator('text=cannot be found').first.is_visible(timeout=800))
    except Exception:
        return False


def dc_open_active_inventory(page):
    set_step(4)
    log("info", "Opening Active Inventory ...")

    if _click_active_inventory_count(page):
        log("ok", "Clicked the Active Inventory count")
    else:
        # fallback: the report opens at this route directly. This URL only resolves
        # cleanly right after a fresh login — after an in-app rooftop switch it can
        # 404, in which case a plain refresh fixes it (the SPA re-resolves the route
        # using the now-current rooftop session).
        try:
            page.goto(_ACTIVE_INVENTORY_FALLBACK_URL, wait_until="domcontentloaded",
                      timeout=60000)
            log("ok", "Navigated to inventory report (fallback URL)")
        except Exception as e:
            log("warn", f"Could not open inventory: {e}")
    safe_wait(page, 30000, 3.0)

    if _looks_like_404(page):
        if "not-found" in page.url:
            # A plain reload just re-shows the same 404 route — the URL itself has to
            # change. Stripping back to the app shell's base URL and landing on Home
            # is the fix the user found works.
            log("warn", f"Landed on the 404 route ({page.url}) — "
                        f"navigating to the app shell base URL instead of reloading")
            try:
                page.goto("https://app.dealercenter.net/apps/shell/",
                          wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log("warn", f"Could not navigate to the shell base URL: {e}")
            safe_wait(page, 30000, 3.0)
            if _click_active_inventory_count(page):
                log("ok", "Clicked the Active Inventory count after shell-base recovery")
                safe_wait(page, 30000, 3.0)
        else:
            log("warn", "Landed on a 404 page — refreshing")
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            safe_wait(page, 30000, 3.0)
            if _looks_like_404(page) and _click_active_inventory_count(page):
                log("ok", "Clicked the Active Inventory count after refresh")
                safe_wait(page, 30000, 3.0)

    # The fallback URL route in particular can take longer to hydrate than clicking
    # through the UI — wait for the filter panel itself before returning, once,
    # rather than letting every VIN afterward fail and silently retry.
    try:
        page.wait_for_selector('input[placeholder*="Stock" i], input[placeholder*="VIN" i]',
                               timeout=20000)
        log("ok", "Inventory filter panel is ready")
    except Exception:
        log("warn", "Inventory filter panel didn't appear within 20s — "
                     "VIN filtering below may fail")


def dc_filter_vin(page, vin):
    set_step(5)
    # the inventory filter field placeholder is "Stock# or VIN#"
    filled = False
    for sel in ['input[placeholder*="Stock" i]', 'input[placeholder*="VIN" i]']:
        try:
            box = page.locator(sel).first
            box.wait_for(state="visible", timeout=8000)
            box.click(timeout=4000)
            box.press("Control+A")
            box.press("Backspace")
            box.press_sequentially(vin, delay=30)
            filled = True
            break
        except Exception:
            continue
    if not filled:
        log("warn", f"Could not find Stock#/VIN# field for {vin} (page url: {page.url})")
        return False
    log("ok", f"Entered VIN: {vin}")
    _click_first(page, ['button:has-text("Run")'], label="Clicked Run")
    safe_wait(page, 25000, 3.0)
    return True


def dc_open_vehicle(page, vin):
    tail = re.escape(vin[-6:])
    # The reliable way in: click the vehicle's thumbnail PHOTO on the result row —
    # the title text itself isn't what opens the record.
    for sel in [
        # image inside the same row as the VIN tail text
        f'xpath=//*[contains(text(),"{vin[-6:]}")]/ancestor::*[self::tr or contains(@class,"row")][1]//img',
        # first image in the results area generically (usually one result after filtering)
        'table img', '.report-list img', '[class*="result" i] img',
    ]:
        try:
            page.locator(sel).first.click(timeout=5000)
            log("ok", "Opened vehicle record (clicked thumbnail)")
            safe_wait(page, 30000, 3.0)
            return True
        except Exception:
            continue

    # fallback: title link / text strategies
    for sel in [
        'a:has-text("Vin")',
        f'text=/{tail}/',
        '.report-list a', 'table a', 'a[href*="vehicle"]',
    ]:
        try:
            page.locator(sel).first.click(timeout=5000)
            log("ok", "Opened vehicle record (clicked title link)")
            safe_wait(page, 30000, 3.0)
            return True
        except Exception:
            continue
    # last resort: click the first vehicle-title-looking link (starts with a 4-digit year)
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

    def _try_upload_once():
        # Preferred: set files straight onto the hidden <input type=file>. Clicking
        # the dropzone first helps on the empty-Photos-tab layout, where the real
        # file input isn't always ready to accept files until the drop area itself
        # has been interacted with.
        try:
            page.locator('text=Drop your image here').first.click(timeout=2000)
        except Exception:
            pass
        for sel in ['input[type="file"]']:
            try:
                inp = page.locator(sel).first
                inp.set_input_files(images, timeout=30000)
                log("ok", f"Set {len(images)} files on upload input")
                return True
            except Exception:
                continue
        # Fallback: click Browse/Upload to open a file chooser
        try:
            with page.expect_file_chooser(timeout=20000) as fc:
                _click_first(page, ['text=Browse', 'button:has-text("Upload")',
                                    'text=Upload'])
            fc.value.set_files(images)
            log("ok", f"Set {len(images)} files via file chooser")
            return True
        except Exception as e:
            log("warn", f"Upload attempt failed: {e}")
            return False

    done = _try_upload_once()
    if not done:
        log("info", "Retrying the upload once more ...")
        time.sleep(2.0)
        done = _try_upload_once()
    if not done:
        log("error", f"Upload failed for all {len(images)} image(s) after a retry")
        return False

    target = len(images)
    log("info", f"Waiting for all {target} image(s) to finish uploading ...")
    deadline = time.time() + 180  # generous cap — large batches can take a while
    last_count = -1
    while time.time() < deadline:
        count = None
        try:
            txt = page.locator('text=/Photos \\(\\d+\\)/').first.inner_text(timeout=2000)
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
        time.sleep(2.0)

    log("warn", f"Only saw {last_count if last_count >= 0 else '?'}/{target} photos after "
                f"3 minutes — proceeding to save anyway")
    return True


def dc_save_and_close(page):
    """Click Save and Close, then verify the 'Inventory Successfully Saved' banner
    actually appears. Returns True only on confirmed success."""
    set_step(7)
    for _ in range(SAVE_RETRIES):
        if stop_flag.is_set():
            return False
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
            log("ok", "Clicked Save and Close")
            safe_wait(page, 30000, 3.0)
            try:
                if page.locator('text=Inventory Successfully Saved').first.is_visible(timeout=8000):
                    log("ok", "Confirmed: Inventory Successfully Saved")
                    return True
            except Exception:
                pass
            # Name the specific failure if it's the known save-conflict error, and
            # close its dialog so it doesn't sit on screen blocking the retry.
            try:
                if page.locator('text=Error saving Inventory').first.is_visible(timeout=1500) or \
                   page.locator('text=Changes have been made to this inventory record'
                                ).first.is_visible(timeout=1000):
                    log("warn", "Save failed: another user/process had already changed "
                                "this inventory record")
                    try:
                        page.locator('[class*="modal" i] [class*="close" i], '
                                    'button[aria-label*="close" i]').first.click(timeout=1500)
                    except Exception:
                        pass
                    return False
            except Exception:
                pass
            log("warn", "Save and Close was clicked but no success confirmation appeared")
            return False
        time.sleep(1.5)
    log("warn", "Save and Close button not confirmed")
    return False


def dc_go_home(page):
    """Navigate all the way back to the DealerCenter home page. This is the most
    reliable reset point we've seen — Back to Report List / Reset can leave the app
    on a stale or 404 state, but Home always loads clean."""
    log("info", "Returning to DealerCenter home ...")
    try:
        page.goto("https://app.dealercenter.net/apps/shell/reports/home",
                  wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("warn", f"Could not navigate home: {e}")
    safe_wait(page, 30000, 3.0)


# ── VinMotion page actions ───────────────────────────────────────────────────
def vm_login(page, user, pw, after_epoch):
    """Fill username/password on the VinMotion login screen, submit, then satisfy
    the email verification step."""
    set_step(2)
    log("info", f"Opening VinMotion login for {user} ...")
    try:
        page.goto(VM_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("warn", f"Could not open the VinMotion login page: {e}")
    safe_wait(page, 30000, 2.5)

    filled_u = False
    for sel in ['input[type="text"]', 'input[type="email"]', 'input[name*="user" i]']:
        try:
            box = page.locator(sel).first
            box.click(timeout=4000)
            box.press("Control+A"); box.press("Backspace")
            box.press_sequentially(user, delay=45)
            filled_u = True
            break
        except Exception:
            continue

    filled_p = False
    pw_box = None
    for sel in ['input[type="password"]']:
        try:
            box = page.locator(sel).first
            box.click(timeout=4000)
            box.press("Control+A"); box.press("Backspace")
            box.press_sequentially(pw, delay=45)
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
            pw_box.press("Enter")
            clicked = True
            log("info", "Pressed Enter to submit login")
        except Exception:
            pass
    if not clicked:
        clicked = _click_first(page, ['button:has-text("GO")', 'button[type="submit"]'],
                               label="Clicked GO")
    safe_wait(page, 20000, 3.0)

    # ── email verification ──────────────────────────────────────────────────
    otp = None
    if GMAIL_ADDR and GMAIL_PASS:
        log("info", "Fetching verification code from Gmail ...")
        try:
            from otp_reader import get_otp
            otp = get_otp(GMAIL_ADDR, GMAIL_PASS, after_epoch, timeout=OTP_TIMEOUT,
                          log=log, sender=VM_OTP_SENDER, subject_hint=VM_OTP_SUBJECT,
                          sender_domain="microsoftonline.com")
        except Exception as e:
            log("warn", f"Auto-OTP failed: {e}")

    if not otp:
        state["status"] = "otp_wait"
        state["otp_prompt"] = f"Enter the verification code emailed for '{user}', then click Submit OTP."
        log("warn", "Waiting for the verification code from the UI (manual fallback) ...")
        otp_manual_evt.clear()
        otp_manual_code["code"] = None
        otp_manual_evt.wait()
        state["status"] = "running"
        state["otp_prompt"] = None
        otp = otp_manual_code["code"]
        if stop_flag.is_set():
            return False

    if not otp:
        log("error", "No verification code available.")
        return False

    typed = False
    for sel in ['input[placeholder*="code" i]', 'input[type="text"]']:
        try:
            box = page.locator(sel).first
            box.click(timeout=4000)
            box.press("Control+A"); box.press("Backspace")
            box.press_sequentially(otp, delay=45)
            typed = True
            break
        except Exception:
            continue
    if not typed:
        try:
            page.keyboard.type(otp, delay=80)
        except Exception:
            pass

    _click_first(page, ['button:has-text("Verify code")', 'button:has-text("Verify")'],
                 label="Clicked Verify code")
    safe_wait(page, 20000, 3.0)

    # "E-mail address verified. You can now continue." -> Continue
    _click_first(page, ['button:has-text("CONTINUE")', 'button:has-text("Continue")'],
                 label="Clicked Continue")
    safe_wait(page, 30000, 3.0)
    log("ok", f"Logged in: {user}")
    return True


def _vm_select_native_rooftop(page, target_label):
    """If the rooftop switcher is (or contains) a native <select>, this is far more
    reliable than click-driven dropdown handling. Try every <select> on the page
    until one has a matching option."""
    try:
        selects = page.locator("select")
        count = selects.count()
        for i in range(count):
            try:
                selects.nth(i).select_option(label=target_label, timeout=3000)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _vm_click_by_text_js(page, exact_text=None, pattern=None):
    """Find an element by exact text or regex pattern via JS (reliable even when
    the label is split across nested spans — JS textContent concatenates
    descendants regardless), get its real screen coordinates, then perform a
    genuine Playwright mouse click there. A real mouse click fires the full
    mousedown/mouseup/click sequence, which many custom dropdown components
    require — a synthetic JS el.click() (or a Playwright locator that finds
    nothing) often does not. Returns True if a click was performed."""
    js = """(args) => {
        const [exact, patternSrc] = args;
        const re = patternSrc ? new RegExp(patternSrc) : null;
        const all = Array.from(document.querySelectorAll('body *'));
        let best = null;
        for (const el of all) {
            const t = (el.textContent || '').trim();
            const matches = exact ? (t === exact) : (re && re.test(t));
            if (matches && el.querySelectorAll('*').length < 6) {
                best = el;
                break;
            }
        }
        if (!best) return null;
        const r = best.getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    }"""
    try:
        coords = page.evaluate(js, [exact_text, pattern])
    except Exception:
        coords = None
    if not coords:
        return False
    try:
        page.mouse.click(coords["x"], coords["y"])
        return True
    except Exception:
        return False


def _vm_get_current_rooftop(page):
    """Read the rooftop currently shown in the dropdown toggle's own value —
    the ground truth for 'where are we right now', not an assumption."""
    try:
        val = page.locator('input.dropdown-toggle[data-bs-toggle="dropdown"]') \
                  .first.get_attribute("value", timeout=5000)
        return (val or "").strip()
    except Exception:
        return None


def _vm_switch_rooftop_once(page, target_label):
    """One attempt at opening the dropdown and clicking target_label. Returns
    True only if the click succeeded (not yet re-verified against the toggle)."""
    toggle_sel = 'input.dropdown-toggle[data-bs-toggle="dropdown"]'
    clicked_toggle = False
    for attempt in range(4):
        try:
            page.locator(toggle_sel).first.click(timeout=6000)
            clicked_toggle = True
            break
        except Exception:
            if attempt == 0:
                log("info", "Rooftop dropdown not ready yet, waiting and retrying ...")
            time.sleep(2.0)
    if not clicked_toggle:
        log("warn", "Could not click the rooftop dropdown toggle after retrying")
        return False

    try:
        page.locator(".dropdown-menu.show").first.wait_for(state="visible", timeout=8000)
    except Exception:
        log("warn", "Rooftop dropdown menu did not open")
        return False

    try:
        page.locator(".dropdown-menu.show button.dropdown-item") \
            .locator(f'text="{target_label}"').first.click(timeout=6000)
    except Exception as e:
        log("warn", f"Could not click '{target_label}' in the rooftop dropdown: {e}")
        return False

    safe_wait(page, 20000, 3.0)
    return True


def vm_switch_rooftop(page, target_label, current_label=None):
    """
    For accounts that share one VinMotion login across several rooftops (e.g. the
    Charlie Clark group): this is a standard Bootstrap dropdown — an
    <input class="dropdown-toggle" data-bs-toggle="dropdown"> that, once clicked,
    reveals a <div class="dropdown-menu show"> containing one
    <button class="dropdown-item"> per rooftop, each with the rooftop's exact
    label as its text. No logout/login/verification-code needed.

    Always checks the ACTUAL current rooftop (read from the toggle's own value)
    before switching, and re-checks it after — a click "succeeding" isn't proof
    the app actually changed rooftop, so we verify against ground truth either way.
    Returns True only once the current rooftop is confirmed to match target_label.
    """
    set_step(4)

    # Right after a fresh login the page can still be re-rendering, which was
    # causing the very first switch in a group to fail. Let it settle first.
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(1.5)

    before = _vm_get_current_rooftop(page)
    log("info", f"Current rooftop is '{before or 'unknown'}' — target is '{target_label}'")
    if before and before == target_label:
        log("ok", f"Already on rooftop: {target_label}")
        return True

    # Fastest path: some accounts may render this as a plain native <select>.
    if _vm_select_native_rooftop(page, target_label):
        safe_wait(page, 20000, 3.0)
        after = _vm_get_current_rooftop(page)
        if after == target_label:
            log("ok", f"Switched to rooftop: {target_label} (native select, verified)")
            return True

    for attempt in range(1, 4):
        log("info", f"Switching rooftop -> '{target_label}' (attempt {attempt}) ...")
        _vm_switch_rooftop_once(page, target_label)
        after = _vm_get_current_rooftop(page)
        if after and after == target_label:
            log("ok", f"Switched to rooftop: {target_label} (verified)")
            return True
        log("warn", f"After switching, rooftop reads '{after or 'unknown'}' — "
                    f"not yet '{target_label}', retrying ...")
        time.sleep(1.5)

    log("error", f"Could not verify rooftop switched to '{target_label}' after retries")
    return False


def vm_open_inventory(page):
    """Navigate to the Inventory list. This is both the post-login landing page
    and the reset point between VINs. Skips the reload if we're already there —
    e.g. right after a rooftop switch — since re-navigating into an in-flight
    load can abort it (net::ERR_ABORTED) and leave the page in a stale state."""
    set_step(4)
    already_there = "/inventory" in (page.url or "").lower()
    if already_there:
        log("info", "Already on Inventory — waiting for it to settle ...")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
    else:
        log("info", "Opening VinMotion Inventory ...")
        try:
            page.goto(VM_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log("warn", f"Could not navigate to Inventory: {e}")
    safe_wait(page, 30000, 3.0)
    try:
        page.wait_for_selector('text=/\\d+\\s+Vehicle\\(s\\)/', timeout=20000)
        log("ok", "Inventory page is ready")
    except Exception:
        log("warn", "Inventory page didn't confirm ready within 20s")


def _click_vm_search_icon(page):
    """Click the magnifying-glass icon that reveals the VIN search box. It's an
    unlabeled icon in the far-right of the toolbar row (same row as the
    "N Vehicle(s)" count), so try attribute-based selectors first, then fall
    back to clicking by position relative to that count text."""
    for sel in ['[aria-label="Search" i]', 'button[title*="search" i]',
               '.search-icon', '[class*="fa-search" i]', '[class*="icon-search" i]',
               'svg[class*="search" i]', '[data-icon="search" i]',
               'span[class*="search" i]', 'i[class*="search" i]',
               '[class*="search" i]']:
        try:
            page.locator(sel).first.click(timeout=2500)
            return True
        except Exception:
            continue
    # Positional fallback: the icon sits in the same row as the "N Vehicle(s)"
    # count, near the right edge of the page.
    try:
        anchor = page.locator('text=/\\d+\\s+Vehicle\\(s\\)/').first
        box = anchor.bounding_box()
        vw = page.evaluate("() => window.innerWidth")
        if box and vw:
            page.mouse.click(vw - 80, box["y"] + box["height"] / 2)
            return True
    except Exception:
        pass
    return False


def vm_filter_vin(page, vin):
    set_step(5)
    # The search box (#txtSearch) is hidden until the magnifying-glass icon
    # (#iconSearch) is clicked — confirmed from the page's actual DOM.
    for sel in ['#iconSearch', 'button#iconSearch']:
        try:
            page.locator(sel).first.click(timeout=4000)
            break
        except Exception:
            continue

    filled = False
    for sel in ['#txtSearch', 'input[placeholder="Search..."]',
               'input[placeholder*="search" i]', 'input[type="text"]']:
        try:
            box = page.locator(sel).first
            box.wait_for(state="visible", timeout=6000)
            box.click(timeout=4000)
            box.press("Control+A"); box.press("Backspace")
            box.press_sequentially(vin, delay=30)
            filled = True
            break
        except Exception:
            continue

    if not filled:
        log("warn", f"Could not find the VIN search field for {vin} (page url: {page.url})")
        return False

    log("ok", f"Entered VIN: {vin}")
    try:
        page.keyboard.press("Enter")
    except Exception:
        pass
    safe_wait(page, 25000, 3.0)
    return True


def vm_open_vehicle(page, vin):
    """After a VIN search there's exactly one result row (a jqGrid row). Click
    its vehicle-title text to navigate into the record."""
    for sel in ['tr.jqgrow', '.ui-jqgrid-btable tr[role="row"]']:
        try:
            page.locator(sel).first.click(timeout=5000)
            log("ok", "Opened vehicle record")
            safe_wait(page, 30000, 3.0)
            return True
        except Exception:
            continue

    # fallback: click the vehicle title link (starts with a 4-digit year)
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


def vm_open_merchandising_tab(page):
    """The Merchandising tab is a real, stable element: <a id="aMerchandising">."""
    try:
        page.locator("#aMerchandising").first.click(timeout=8000)
        log("ok", "Opened Merchandising tab")
    except Exception as e:
        log("warn", f"Could not click #aMerchandising: {e}")
        _click_first(page, ['text=Merchandising', 'a:has-text("Merchandising")'],
                     label="Opened Merchandising tab (fallback)")
    safe_wait(page, 15000, 2.5)
    try:
        page.wait_for_selector("#divPhotosAndVideosBody, text=Photos", timeout=10000)
    except Exception:
        pass
    # Photos + Add/Delete/Reorder are the default sub-tabs, but click explicitly
    # in case a previous VIN left another sub-tab active.
    _click_first(page, ['text=Photos', 'button:has-text("Photos")'])
    _click_first(page, ['text=Add/Delete/Reorder', 'button:has-text("Add/Delete/Reorder")'])
    safe_wait(page, 10000, 1.5)


def vm_remove_all_photos(page):
    """Click #btnSelectAll then #btnDelete. The confirmation
    ("Are you sure you want to Delete Selected Photos?") is a NATIVE browser
    confirm() dialog, not a DOM element — Playwright auto-DISMISSES (cancels)
    unhandled dialogs, which would silently keep the old photos. We register a
    one-shot handler that accepts it before clicking Delete."""
    try:
        if page.locator("#btnSelectAll").first.is_visible(timeout=2000):
            page.locator("#btnSelectAll").first.click(timeout=5000)
            log("ok", "Clicked Select All")
        else:
            log("info", "Select All not visible — assuming no existing photos")
            return
    except Exception:
        log("info", "Select All not found — assuming no existing photos")
        return
    time.sleep(0.5)

    def _accept_dialog(dialog):
        try:
            dialog.accept()
        except Exception:
            pass

    page.once("dialog", _accept_dialog)
    try:
        page.locator("#btnDelete").first.click(timeout=6000)
        log("info", "Clicked Delete (native confirm accepted)")
    except Exception as e:
        log("info", f"Delete button not clickable ({e}) — assuming no photos to delete")
        return

    safe_wait(page, 20000, 3.0)


def vm_upload_images(page, images):
    set_step(6)
    log("info", f"Uploading {len(images)} image(s) ...")

    try:
        page.locator("#btnUpload").first.click(timeout=6000)
    except Exception as e:
        log("error", f"Could not click #btnUpload: {e}")
        return False
    # rezoom=False: skip the generic popup-cleanup pass right here — the Uppy
    # dialog we just opened is the thing we need to interact with next.
    safe_wait(page, 10000, 1.5, rezoom=False)

    def _try_upload_once():
        for sel in ['input[type="file"]']:
            try:
                inp = page.locator(sel).first
                inp.set_input_files(images, timeout=30000)
                log("ok", f"Set {len(images)} files on upload input")
                return True
            except Exception:
                continue
        try:
            with page.expect_file_chooser(timeout=20000) as fc:
                _click_first(page, ['text=browse files'])
            fc.value.set_files(images)
            log("ok", f"Set {len(images)} files via file chooser")
            return True
        except Exception as e:
            log("warn", f"Upload attempt failed: {e}")
            return False

    done = _try_upload_once()
    if not done:
        log("info", "Retrying the upload once more ...")
        time.sleep(2.0)
        done = _try_upload_once()
    if not done:
        log("error", f"Upload failed for all {len(images)} image(s) after a retry")
        return False

    target = len(images)
    try:
        page.wait_for_selector(f'text=/{target} files selected/', timeout=15000)
        log("ok", f"Confirmed {target} file(s) staged for upload")
    except Exception:
        log("warn", "Didn't see the file-count confirmation — proceeding anyway")

    # Uppy's own final upload button has this stable class regardless of the
    # file count in its label.
    if not _click_first(page, ['button.uppy-StatusBar-actionBtn--upload',
                               f'button:has-text("Upload {target} file")',
                               'button:has-text("Upload")'],
                        label=f"Clicked Upload {target} file(s)"):
        log("error", "Could not click the final Upload button in the file-picker dialog")
        return False

    log("info", "Waiting for the upload + auto-redirect back to the photo grid ...")
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            still_uploading = page.locator('text=files selected').first.is_visible(timeout=500)
        except Exception:
            still_uploading = False
        if not still_uploading:
            try:
                if page.locator('text=Add/Delete/Reorder').first.is_visible(timeout=2000):
                    log("ok", "Upload finished — back on the photo grid")
                    return True
            except Exception:
                pass
        time.sleep(2.0)

    log("warn", "Didn't confirm the redirect back to the photo grid within 3 minutes — "
                "proceeding anyway")
    return True


def vm_save(page):
    """Real, stable ID: <button id="btnSave">."""
    set_step(7)
    for _ in range(SAVE_RETRIES):
        if stop_flag.is_set():
            return False
        try:
            page.locator("#btnSave").first.click(timeout=5000)
            log("ok", "Clicked Save")
            safe_wait(page, 20000, 3.0)
            return True
        except Exception:
            pass
        if _click_first(page, ['button:has-text("SAVE")', 'button:has-text("Save")']):
            log("ok", "Clicked Save (fallback)")
            safe_wait(page, 20000, 3.0)
            return True
        time.sleep(1.5)
    log("warn", "Save button not found")
    return False


def vm_go_inventory(page):
    """Left-nav Inventory link: <a href="/Inventory" class="dds-app-nav-link">."""
    log("info", "Returning to VinMotion Inventory ...")
    try:
        page.locator('a[href="/Inventory"]').first.click(timeout=6000)
    except Exception:
        _click_first(page, ['text=Inventory', 'a:has-text("Inventory")'],
                     label="Clicked Inventory (fallback)")
    safe_wait(page, 20000, 2.5)


def _vm_do_media_and_save(page, vin, images):
    """Merchandising tab -> select all -> delete -> upload -> save -> back to
    Inventory. Assumes the caller has already confirmed we're on the correct
    vehicle's record. Returns True only if the save was actually confirmed."""
    vm_open_merchandising_tab(page)
    vm_remove_all_photos(page)
    if not vm_upload_images(page, images):
        return False
    if not vm_save(page):
        return False
    vm_go_inventory(page)
    return True


# ── vAuto page actions ───────────────────────────────────────────────────────
def va_login(page, user, pw, after_epoch):
    """vAuto/Cox Automotive login: username -> Next -> password -> Sign in ->
    email verification (Select -> Gmail code -> Verify). Each vAuto account is
    its own separate login — there's no shared-login rooftop switching here."""
    set_step(2)
    log("info", f"Opening vAuto login for {user} ...")
    try:
        page.goto(VA_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("warn", f"Could not open the vAuto login page: {e}")
    safe_wait(page, 30000, 2.5)

    filled_u = False
    for sel in ['input#username', 'input[name="username"]', 'input[type="text"]']:
        try:
            box = page.locator(sel).first
            box.click(timeout=4000)
            box.press("Control+A"); box.press("Backspace")
            box.press_sequentially(user, delay=45)
            filled_u = True
            break
        except Exception:
            continue
    if not filled_u:
        log("warn", "Could not fill the vAuto username field")

    if not _click_first(page, ['button:has-text("Next")'], label="Clicked Next"):
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass
    safe_wait(page, 20000, 2.5)

    filled_p = False
    for sel in ['input#password', 'input[name="password"]', 'input[type="password"]']:
        try:
            box = page.locator(sel).first
            box.wait_for(state="visible", timeout=10000)
            box.click(timeout=4000)
            box.press("Control+A"); box.press("Backspace")
            box.press_sequentially(pw, delay=45)
            filled_p = True
            break
        except Exception:
            continue
    if not filled_p:
        log("warn", "Could not fill the vAuto password field")

    if not _click_first(page, ['button:has-text("Sign in")'], label="Clicked Sign in"):
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass
    safe_wait(page, 20000, 3.0)

    # "Verify your identity" -> Select (email)
    _click_first(page, ['#button-verify-by-email', 'button:has-text("Select")'],
                 label="Selected email verification")
    safe_wait(page, 15000, 2.0)

    otp = None
    if GMAIL_ADDR and GMAIL_PASS:
        log("info", "Fetching verification code from Gmail ...")
        try:
            from otp_reader import get_otp
            otp = get_otp(GMAIL_ADDR, GMAIL_PASS, after_epoch, timeout=OTP_TIMEOUT,
                          log=log, sender=VA_OTP_SENDER, subject_hint=VA_OTP_SUBJECT,
                          sender_domain="coxautoinc.com")
        except Exception as e:
            log("warn", f"Auto-OTP failed: {e}")

    if not otp:
        state["status"] = "otp_wait"
        state["otp_prompt"] = f"Enter the verification code emailed for '{user}', then click Submit OTP."
        log("warn", "Waiting for the verification code from the UI (manual fallback) ...")
        otp_manual_evt.clear()
        otp_manual_code["code"] = None
        otp_manual_evt.wait()
        state["status"] = "running"
        state["otp_prompt"] = None
        otp = otp_manual_code["code"]
        if stop_flag.is_set():
            return False

    if not otp:
        log("error", "No verification code available.")
        return False

    typed = False
    for sel in ['#input-verification-code', 'input[placeholder*="one time code" i]']:
        try:
            box = page.locator(sel).first
            box.click(timeout=4000)
            box.press("Control+A"); box.press("Backspace")
            box.press_sequentially(otp, delay=45)
            typed = True
            break
        except Exception:
            continue
    if not typed:
        try:
            page.keyboard.type(otp, delay=80)
        except Exception:
            pass

    if not _click_first(page, ['button:has-text("Verify")'], label="Clicked Verify"):
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass
    safe_wait(page, 30000, 3.0)
    log("ok", f"Logged in: {user}")

    # Navigate straight to Media Management instead of hovering the Merchandising
    # menu — same destination, far more reliable than simulating a mouse hover.
    va_open_inventory(page)
    return True


def va_open_inventory(page):
    """Navigate to Media Management -> Unassigned Photos. This is both the
    post-login landing point and the reset point between VINs."""
    set_step(4)
    already_there = "mediamanagement" in (page.url or "").lower()
    if already_there:
        log("info", "Already on Media Management — waiting for it to settle ...")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
    else:
        log("info", "Opening vAuto Media Management ...")
        try:
            page.goto(VA_MEDIA_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log("warn", f"Could not navigate to Media Management: {e}")
    safe_wait(page, 30000, 3.0)
    try:
        page.wait_for_selector('input[placeholder="VIN or Stock Number"]', timeout=20000)
        log("ok", "Media Management page is ready")
    except Exception:
        log("warn", "Media Management page didn't confirm ready within 20s")


def va_filter_vin(page, vin):
    set_step(5)
    filled = False
    for sel in ['input[placeholder="VIN or Stock Number"]', 'input[placeholder*="VIN" i]']:
        try:
            box = page.locator(sel).first
            box.wait_for(state="visible", timeout=8000)
            box.click(timeout=4000)
            box.press("Control+A"); box.press("Backspace")
            box.press_sequentially(vin, delay=30)
            filled = True
            break
        except Exception:
            continue

    if not filled:
        log("warn", f"Could not find the VIN search field for {vin} (page url: {page.url})")
        return False

    log("ok", f"Entered VIN: {vin}")
    try:
        page.keyboard.press("Enter")
    except Exception:
        pass
    safe_wait(page, 20000, 2.5)
    return True


def va_open_vehicle(page, vin):
    for sel in ['.vehicle-card', '.listing-card']:
        try:
            page.locator(sel).first.click(timeout=6000)
            log("ok", "Opened vehicle record")
            safe_wait(page, 20000, 2.5)
            return True
        except Exception:
            continue

    # fallback: click the vehicle title link (starts with a 4-digit year)
    try:
        links = page.locator("a").all()
        for lk in links:
            txt = (lk.inner_text(timeout=500) or "").strip()
            if re.match(r"^(19|20)\d{2}\s+\S+", txt):
                lk.click(timeout=4000)
                log("ok", f"Opened vehicle record ({txt[:30]})")
                safe_wait(page, 20000, 2.5)
                return True
    except Exception:
        pass
    log("error", f"Could not open the vehicle record for {vin}")
    return False


def va_open_media_tab(page):
    _click_first(page, ['a:has-text("Media")', 'button:has-text("Media")',
                        'li:has-text("Media")'], label="Opened Media tab")
    safe_wait(page, 12000, 2.0)

    # "Vehicle Photos" under Base Media is the default, but click explicitly in
    # case a previous VIN left another sub-view active (e.g. Appraisal Photos).
    # The vehicle-detail modal's Photos panel renders inside its own iframe
    # (src contains "Va/Ranking/Vehicl"). page.frame_locator() is Playwright's
    # built-in way to reach into a specific iframe by a selector for the
    # <iframe> element itself — it has its own auto-waiting/retrying, so no
    # manual frame-scanning or retry loop is needed.
    vp = page.frame_locator('iframe[src*="Va/Ranking/Vehicl"]').locator("#vehicle-photos-subnav")
    try:
        vp.first.click(timeout=15000, force=True)
        log("ok", "Selected Vehicle Photos")
    except Exception as e:
        log("warn", f"Could not click Vehicle Photos in the vehicle-detail iframe ({e}) — "
                     f"trying the main page directly")
        _click_first(page, ['#vehicle-photos-subnav a', '#vehicle-photos-subnav',
                            'a:has-text("Vehicle Photos")', 'text=Vehicle Photos'],
                     timeout=6000, force=True, label="Selected Vehicle Photos")

    safe_wait(page, 10000, 1.5)


def va_remove_all_photos(page):
    """Check the Select All checkbox, click #delete-photos-btn, confirm in the
    custom DOM modal (button.confirm-btn) — NOT a native dialog, a real element.

    All three live inside the same vehicle-detail iframe as Vehicle Photos
    above, reached the same way via frame_locator. Uses a plain .click()
    rather than Playwright's .check(): .check() adds its own follow-up
    assertion that the underlying checkbox's native `checked` property
    flipped, which fails here even though the click itself lands and does
    select all photos — this is a custom web-component checkbox, not a plain
    <input>, so that verification doesn't reliably apply."""
    va_frame = page.frame_locator('iframe[src*="Va/Ranking/Vehicl"]')

    try:
        va_frame.locator("input.merch-select-all").first.click(timeout=6000, force=True)
        log("ok", "Clicked Select All")
    except Exception as e:
        log("info", f"Select All checkbox not clickable ({e}) — assuming no existing photos")
        return

    time.sleep(0.5)
    try:
        va_frame.locator("#delete-photos-btn").first.click(timeout=6000)
        log("info", "Clicked Delete")
    except Exception as e:
        log("info", f"Delete button not clickable ({e}) — assuming no photos to delete")
        return

    time.sleep(0.8)
    try:
        va_frame.locator("button.confirm-btn").first.click(timeout=8000)
        log("ok", "Confirmed delete in the dialog")
    except Exception as e:
        log("warn", f"Could not confirm the delete dialog: {e}")
    safe_wait(page, 20000, 3.0)


def va_upload_images(page, images):
    set_step(6)
    log("info", f"Uploading {len(images)} image(s) ...")

    va_frame = page.frame_locator('iframe[src*="Va/Ranking/Vehicl"]')

    # After deleting all existing photos, vAuto shows the "Let's Add Some
    # Photos" drop-zone directly on the page — there's no separate "Upload
    # Photos" button to click first in that state. Go straight for the file
    # input under it.
    done = False
    try:
        va_frame.locator('input[type="file"]').first.set_input_files(images, timeout=15000)
        log("ok", f"Set {len(images)} files on upload input")
        done = True
    except Exception:
        pass

    if not done:
        # Some vehicles may still show the photo grid with its own "Upload
        # Photos" button instead (e.g. if the delete step didn't fully clear) —
        # try clicking that, then look for the file input again.
        try:
            va_frame.locator('button:has-text("Upload Photos")').first.click(timeout=4000)
            safe_wait(page, 6000, 1.0, rezoom=False)
            va_frame.locator('input[type="file"]').first.set_input_files(images, timeout=15000)
            log("ok", f"Set {len(images)} files on upload input (after clicking Upload Photos)")
            done = True
        except Exception:
            pass

    if not done:
        # Last resort: click the "Upload Photos from your device" text link,
        # which opens a native OS file chooser instead of a hidden input.
        try:
            with page.expect_file_chooser(timeout=15000) as fc:
                va_frame.locator('text=Upload Photos from your device').first.click(timeout=6000)
            fc.value.set_files(images)
            log("ok", f"Set {len(images)} files via file chooser")
            done = True
        except Exception as e:
            log("error", f"Upload attempt failed: {e}")
            return False

    # Uploading starts automatically on file selection (no separate confirm
    # step). Wait for the progress bar to finish, scaling the timeout with the
    # image count since large batches take longer.
    log("info", "Waiting for the upload to complete ...")
    deadline = time.time() + max(180, len(images) * 6)
    finished = False
    while time.time() < deadline:
        try:
            if va_frame.locator('text=/uploaded successfully/i').first.is_visible(timeout=1000):
                finished = True
                break
            if va_frame.locator(f'text=/Upload Complete {len(images)} of {len(images)}/') \
                    .first.is_visible(timeout=1000):
                finished = True
                break
        except Exception:
            pass
        time.sleep(2.0)

    if finished:
        log("ok", "Upload finished")
    else:
        log("warn", "Didn't confirm upload completion within the timeout — proceeding anyway")

    # Dismiss the "N Photos uploaded successfully!" banner if it's still showing.
    try:
        banner_close = page.locator('text=/uploaded successfully/i') \
                            .locator('xpath=following::button[1]')
        if banner_close.is_visible(timeout=2000):
            banner_close.click(timeout=2000)
    except Exception:
        pass
    return True


def va_close_vehicle(page):
    """Close the vehicle detail overlay via its own close icon (identified by
    the onCloseClick handler), falling back to a direct navigation back to
    Media Management if the icon can't be found."""
    log("info", "Closing vehicle record ...")
    if _click_first(page, ['a[onclick*="onCloseClick"]'], timeout=6000):
        safe_wait(page, 15000, 2.0)
        return
    log("info", "Close icon not found — navigating back to Media Management directly")
    va_open_inventory(page)


def _va_do_media_and_save(page, vin, images):
    """Media tab -> Vehicle Photos -> select all -> delete -> upload -> close.
    vAuto has no separate Save step — delete and upload each take effect
    immediately. Returns True only if the upload was confirmed."""
    va_open_media_tab(page)
    va_remove_all_photos(page)
    if not va_upload_images(page, images):
        return False
    va_close_vehicle(page)
    return True


# ── per-DMS driver tables ────────────────────────────────────────────────────
# process_account_vins() below is DMS-agnostic — it just calls whichever set of
# functions matches the account's "dms" tag. Adding a third DMS later means
# writing its own dc_*/vm_*/va_*-style functions and adding one more entry here.
LOGIN_FN = {
    "dealercenter": dc_login,
    "vinmotion": vm_login,
    "vauto": va_login,
}

DRIVERS = {
    "dealercenter": {
        "go_home": dc_go_home,
        "open_inventory": dc_open_active_inventory,
        "filter_vin": dc_filter_vin,
        "open_vehicle": dc_open_vehicle,
        "media_and_save": lambda page, vin, images: _do_media_and_save(page, vin, images),
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


def switch_rooftop(dms, page, cred):
    """Dispatch to the right rooftop-switch function for this DMS."""
    rooftop = cred.get("rooftop") or cred.get("name")
    if dms == "dealercenter":
        company = cred.get("company") or rooftop
        return dc_switch_rooftop(page, company, rooftop)
    elif dms == "vinmotion":
        return vm_switch_rooftop(page, rooftop)
    elif dms == "vauto":
        log("warn", "vAuto accounts are one login per enterprise — "
                     "rooftop switching isn't supported/needed for this DMS")
        return False
    return False


# ── orchestration ────────────────────────────────────────────────────────────
def _do_media_and_save(page, vin, images):
    """Media tab → remove existing photos → upload → save. Assumes the caller has
    already confirmed we're on the correct vehicle's record. Returns True only if
    the save was actually confirmed successful."""
    dc_open_media_tab(page)
    dc_remove_all_photos(page)
    if not dc_upload_images(page, images):
        return False
    return dc_save_and_close(page)


def _process_single_vin(page, vin, images):
    """One VIN, start to finish. Assumes the caller has already put us on a fresh
    Active Inventory search (see process_account_vins) — no in-function recovery
    needed here anymore, since every attempt now starts from a known-good state."""
    if not dc_filter_vin(page, vin):
        return False
    if not dc_open_vehicle(page, vin):
        return False
    return _do_media_and_save(page, vin, images)


def process_account_vins(page, acc, ui, driver):
    """Run the filter → open → clear photos → upload → save loop for every VIN
    folder in one account, using the given DMS's driver functions. Shared by both
    the first login and any rooftop-switch that follows it, so the VIN-handling
    logic exists in exactly one place per DMS.

    Every single VIN — including retries — starts by going all the way back to
    the DMS's home/reset point and reopening its inventory view. That's more
    thorough than a lighter "back" navigation, but it's what actually stays
    reliable across a long run instead of accumulating stale/404 states."""
    for v in acc["vins"]:
        if stop_flag.is_set():
            break
        vin, images = v["vin"], v["images"]
        ui["current_vin"] = vin
        log("info", f"── VIN {vin} ({len(images)} images) ──")

        success = False
        last_error = None
        for attempt in range(1, 3):  # 1 initial attempt + 1 retry
            if stop_flag.is_set():
                break
            try:
                if attempt > 1:
                    log("info", f"Retrying {vin}: back to inventory and re-entering VIN ...")
                driver["go_home"](page)
                driver["open_inventory"](page)
                if (driver["filter_vin"](page, vin)
                        and driver["open_vehicle"](page, vin)
                        and driver["media_and_save"](page, vin, images)):
                    success = True
                    break
                log("warn", f"Attempt {attempt} for {vin} did not confirm a successful save")
            except Exception as e:
                last_error = e
                log("error", f"Attempt {attempt} for {vin} errored: {e}")

        if success:
            ui["done"] += 1
            log("ok", f"✓ {vin} complete")
        else:
            ui["failed"] += 1
            suffix = f": {last_error}" if last_error else ""
            log("error", f"✗ {vin} failed after {attempt} attempt(s){suffix}")
        ui["current_vin"] = None
    ui["status"] = "done"


def run_automation(root_str):
    root = Path(root_str)
    stop_flag.clear()
    state.update({"status": "running", "step": 0, "log": [], "accounts": [],
                  "current_account": None, "otp_prompt": None})

    creds = load_accounts_credentials()
    if not creds:
        log("error", "No accounts in .env.local (DC_ACCOUNT_1_NAME/USER/PASS ... "
                     "and/or VM_ACCOUNT_1_NAME/USER/PASS ...)")
        state["status"] = "error"; return
    if not (GMAIL_ADDR and GMAIL_PASS):
        log("warn", "No Gmail configured — MFA/verification codes will fall back to "
                     "manual entry each login.")

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
            "dms": a["cred"]["dms"] if a["cred"] else None,
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

    # Group accounts that share one login (same username + password) — e.g. the
    # three Capitol rooftops. The first account in a group gets a real login; the
    # rest are reached by switching dealership in-app, no repeat OTP needed.
    usable = [i for i, a in enumerate(accounts) if a["cred"] and a["vins"]]
    for i in usable:
        acc = accounts[i]
        if not acc["cred"]:
            continue
        if not acc["vins"]:
            state["accounts"][i]["status"] = "skipped"
            log("warn", f"'{acc['folder_name']}' has no VIN folders — skipping")

    groups, seen = [], {}
    for i in usable:
        cred = accounts[i]["cred"]
        key = (cred["dms"], cred["user"], cred["pass"])
        if key not in seen:
            seen[key] = []
            groups.append(seen[key])
        seen[key].append(i)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--start-maximized"])

        for group in groups:
            if stop_flag.is_set():
                break

            first_i = group[0]
            first_acc = accounts[first_i]
            first_ui = state["accounts"][first_i]
            dms = first_acc["cred"]["dms"]
            driver = DRIVERS[dms]
            login_fn = LOGIN_FN[dms]
            state["current_account"] = first_acc["folder_name"]
            first_ui["status"] = "running"
            log("info", f"Logging in ({dms}) as '{first_acc['cred']['user']}' "
                        f"for the '{first_acc['folder_name']}' login group ...")

            ctx = browser.new_context(no_viewport=True)
            page = ctx.new_page()
            page.set_default_timeout(60000)

            try:
                if not login_fn(page, first_acc["cred"]["user"], first_acc["cred"]["pass"],
                                time.time()):
                    first_ui["status"] = "error"
                    log("error", f"Login failed for {first_acc['folder_name']} — "
                                 f"skipping this whole login group")
                    ctx.close()
                    continue

                # Multi-rooftop groups (e.g. Capitol on DealerCenter, Charlie Clark on
                # VinMotion): login always lands on whatever the platform's OWN default
                # rooftop is — which may not be any of the rooftops actually present in
                # this run's data. So every account in the group, including the first,
                # must explicitly switch to its matching rooftop rather than assuming
                # login already landed on the right one.
                multi_rooftop = len(group) > 1

                for pos, i in enumerate(group):
                    if stop_flag.is_set():
                        break
                    acc = accounts[i]
                    ui = state["accounts"][i]
                    state["current_account"] = acc["folder_name"]
                    ui["status"] = "running"
                    if pos == 0:
                        log("info", f"══════ ACCOUNT: {acc['folder_name']} "
                                    f"({acc['cred']['user']}) ══════")
                    else:
                        log("info", f"══════ ACCOUNT: {acc['folder_name']} "
                                    f"(same login, switching rooftop) ══════")

                    if multi_rooftop:
                        rooftop = acc["cred"].get("rooftop") or acc["folder_name"]
                        switched = switch_rooftop(dms, page, acc["cred"])
                        if not switched:
                            log("info", f"Retrying rooftop switch for '{rooftop}' "
                                        f"after reloading Inventory ...")
                            driver["open_inventory"](page)
                            switched = switch_rooftop(dms, page, acc["cred"])
                        if not switched:
                            ui["status"] = "error"
                            log("error", f"Could not switch to rooftop '{rooftop}' for "
                                         f"{acc['folder_name']} — skipping this account "
                                         f"(re-logging in would land on the same default "
                                         f"rooftop, so it can't fix this)")
                            continue

                    process_account_vins(page, acc, ui, driver)

            except Exception as e:
                log("error", f"Login group starting at '{first_acc['folder_name']}' "
                              f"errored: {e}")
                for i in group:
                    if state["accounts"][i]["status"] == "running":
                        state["accounts"][i]["status"] = "error"
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
    print("\n  DealerCenter Media Uploader — Spyne")
    print(f"  Control panel:  http://localhost:{PORT}\n")
    srv = HTTPServer(("localhost", PORT), Handler)
    threading.Thread(target=open_browser, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
