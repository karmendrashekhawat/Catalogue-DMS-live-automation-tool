r"""
recon.py — Network recon harness for ONE account/enterprise.

Purpose: before building any direct-HTTP replacement for browser automation,
capture the REAL network traffic a DMS's own web app makes when logging in,
searching a VIN, opening the record, deleting old photos, uploading new ones,
and saving. This turns "the platform probably has an API for X" into an
actual list of URLs/methods/payloads/response shapes to design against.

This intentionally targets ONE account at a time — test small before touching
the real multi-DMS architecture.

USAGE
-----
Automated pass (runs the existing filter -> open -> delete -> upload -> save
flow for one VIN, capturing traffic the whole way):

    python recon.py --account "International Auto" --data-root "C:\Live" --vin 1FMEU73EX8UB24500

Manual pass (just logs in, then leaves the browser open for you to click
through by hand — useful if you want to poke around beyond one VIN, or the
automated flow itself is what's unreliable and you want to see what a human
click actually triggers):

    python recon.py --account "International Auto" --data-root "C:\Live" --manual-seconds 180 --skip-automated

Both at once (automated cycle, then leaves the browser open afterward too):

    python recon.py --account "International Auto" --data-root "C:\Live" --vin 1FMEU73EX8UB24500 --manual-seconds 60

OUTPUT
------
Two files land next to this script:
  recon_<dms>_<account>_<timestamp>.json          — every captured request/response, full detail
  recon_<dms>_<account>_<timestamp>_summary.txt    — the short version: just the likely
                                                      mutation calls (POST/PUT/DELETE/PATCH
                                                      XHR/fetch), which is what matters for
                                                      designing the HTTP-direct layer

Credentials and Gmail OTP auto-fetch are read from .env.local exactly like
the main app — nothing extra to configure. If no Gmail is configured and a
manual OTP is needed, this script prompts for it right in the console.
"""

import argparse
import asyncio
import json
import threading
import time
from pathlib import Path

import server  # reuses load_accounts_credentials, scan_root, LOGIN_FN, DRIVERS, log, stop_flag

BASE = Path(__file__).parent

# Static-asset resource types we don't care about for API design purposes.
NOISE_TYPES = {"image", "stylesheet", "font", "media"}
MUTATING_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


def redact(value, keep=6):
    if not value:
        return value
    value = str(value)
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + f"...<redacted, {len(value)} chars total>"


def looks_like_token_key(key):
    key_l = key.lower()
    return any(w in key_l for w in ("token", "auth", "session", "jwt", "bearer"))


async def dump_client_auth_state(page, ctx):
    """After a successful login, capture WHERE the session actually lives —
    cookies vs. an Authorization header vs. a client-side token in
    localStorage/sessionStorage — so we know exactly what an httpx client
    needs to carry. Values are redacted in what gets printed/summarized;
    full values only go in the JSON file, which stays local."""
    findings = {"cookies": [], "local_storage_token_keys": [], "session_storage_token_keys": []}

    try:
        cookies = await ctx.cookies()
        for c in cookies:
            findings["cookies"].append({
                "name": c.get("name"), "domain": c.get("domain"),
                "httpOnly": c.get("httpOnly"), "secure": c.get("secure"),
                "sameSite": c.get("sameSite"),
                "value_redacted": redact(c.get("value")),
                "value_full": c.get("value"),  # only ever written to the JSON file
            })
    except Exception as e:
        findings["cookies_error"] = str(e)

    try:
        ls = await page.evaluate("() => JSON.stringify(Object.entries(localStorage))")
        for k, v in json.loads(ls):
            if looks_like_token_key(k):
                findings["local_storage_token_keys"].append({
                    "key": k, "value_redacted": redact(v), "value_full": v,
                })
    except Exception as e:
        findings["local_storage_error"] = str(e)

    try:
        ss = await page.evaluate("() => JSON.stringify(Object.entries(sessionStorage))")
        for k, v in json.loads(ss):
            if looks_like_token_key(k):
                findings["session_storage_token_keys"].append({
                    "key": k, "value_redacted": redact(v), "value_full": v,
                })
    except Exception as e:
        findings["session_storage_error"] = str(e)

    return findings


def make_fake_lane():
    """login_fn (dc_login/vm_login/va_login) needs a "lane" dict for OTP
    handling. This is the minimal shape it actually touches."""
    return {
        "status": "running",
        "otp_prompt": None,
        "otp_event": threading.Event(),
        "otp_code": {"code": None},
    }


def start_console_otp_watcher(lane, stop_event):
    """The real app shows an OTP box in the browser UI. This script has no
    UI, so if manual OTP entry is needed, prompt for it right here in the
    terminal instead."""
    def watch():
        already_prompted = False
        while not stop_event.is_set():
            if lane["status"] == "otp_wait" and not already_prompted:
                already_prompted = True
                prompt = lane.get("otp_prompt") or "Enter the verification code:"
                try:
                    code = input(f"\n>>> {prompt} ")
                except EOFError:
                    code = ""
                lane["otp_code"]["code"] = code.strip()
                lane["otp_event"].set()
            if lane["status"] != "otp_wait":
                already_prompted = False
            time.sleep(0.4)
    t = threading.Thread(target=watch, daemon=True)
    t.start()
    return t


def classify(method, resource_type):
    if resource_type in NOISE_TYPES:
        return "asset"
    if method in MUTATING_METHODS and resource_type in ("xhr", "fetch", "document"):
        return "likely_mutation"
    if resource_type in ("xhr", "fetch"):
        return "api_read"
    return "other"


def safe_post_data(request):
    try:
        data = request.post_data
    except Exception:
        return None
    if not data:
        return None
    # Multipart file uploads can be megabytes of base64 image bytes — no
    # point capturing those. But a JSON body (like SaveInventory's full
    # vehicle record) is exactly what we need to actually see, so keep a
    # real prefix rather than replacing it with nothing but a size note.
    if len(data) > 20000:
        return data[:20000] + f"...<{len(data)} bytes total, truncated>"
    return data


async def run_recon(account_name, data_root, vin, manual_seconds, headless, skip_automated):
    creds = server.load_accounts_credentials()
    accounts = server.scan_root(Path(data_root), creds)
    acc = next((a for a in accounts if a["folder_name"] == account_name), None)
    if not acc:
        print(f"No folder named '{account_name}' found under {data_root}")
        print("Folders found:", [a["folder_name"] for a in accounts])
        return
    if not acc["cred"]:
        print(f"'{account_name}' has no matching credentials in .env.local "
              f"(DC_ACCOUNT_n_NAME / VM_ACCOUNT_n_NAME / VA_ACCOUNT_n_NAME).")
        return

    dms = acc["cred"]["dms"]
    login_fn = server.LOGIN_FN[dms]
    driver = server.DRIVERS[dms]
    print(f"Target: '{account_name}' on {server.DMS_LABELS.get(dms, dms)} "
          f"({len(acc['vins'])} VIN folder(s) available)")

    captured = []
    console_msgs = []

    def on_request(request):
        captured.append({
            "kind": "request",
            "ts": time.time(),
            "method": request.method,
            "url": request.url,
            "resource_type": request.resource_type,
            "class": classify(request.method, request.resource_type),
            "post_data": safe_post_data(request),
            "headers_pending": True,  # filled in by on_request_headers below
        })
        entry = captured[-1]
        asyncio.ensure_future(fill_request_headers(request, entry))

    async def fill_request_headers(request, entry):
        try:
            headers = await request.all_headers()
        except Exception:
            headers = {}
        entry.pop("headers_pending", None)
        has_auth = any(k.lower() == "authorization" for k in headers)
        has_cookie = any(k.lower() == "cookie" for k in headers)
        entry["has_authorization_header"] = has_auth
        entry["has_cookie_header"] = has_cookie
        entry["authorization_header_redacted"] = (
            redact(headers.get("authorization") or headers.get("Authorization")) if has_auth else None
        )
        entry["headers_full"] = headers  # only ever written to the JSON file

    async def on_response(response):
        entry = {
            "kind": "response",
            "ts": time.time(),
            "status": response.status,
            "url": response.url,
            "headers": dict(response.headers),
        }
        try:
            ctype = response.headers.get("content-type", "")
            if "json" in ctype or "text" in ctype or "xml" in ctype:
                body = await response.text()
                entry["body"] = body[:20000]
                entry["body_truncated"] = len(body) > 20000
        except Exception:
            pass
        captured.append(entry)

    def on_console(msg):
        console_msgs.append({"ts": time.time(), "type": msg.type, "text": msg.text})

    from playwright.async_api import async_playwright

    lane = make_fake_lane()
    otp_stop = threading.Event()
    start_console_otp_watcher(lane, otp_stop)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--start-maximized"])
        ctx = await browser.new_context(no_viewport=True)
        page = await ctx.new_page()
        page.set_default_timeout(60000)
        page.on("request", on_request)
        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        page.on("console", on_console)

        try:
            print("Logging in ...")
            ok = await login_fn(page, acc["cred"]["user"], acc["cred"]["pass"], time.time(), lane)
            if not ok:
                print("Login did not confirm success — capturing whatever traffic happened anyway.")

            auth_findings = {}
            if ok:
                print("Checking where the session actually lives (cookies vs. header vs. localStorage) ...")
                auth_findings = await dump_client_auth_state(page, ctx)

            if not skip_automated and ok:
                target_vin = None
                if vin:
                    target_vin = next((v for v in acc["vins"] if v["vin"] == vin), None)
                    if not target_vin:
                        print(f"VIN '{vin}' not found in this account's folders — "
                              f"falling back to the first available VIN.")
                if not target_vin and acc["vins"]:
                    target_vin = acc["vins"][0]

                if target_vin:
                    print(f"Running the automated cycle for VIN {target_vin['vin']} "
                          f"({len(target_vin['images'])} image(s)) ...")
                    await driver["go_home"](page)
                    await driver["open_inventory"](page)
                    if await driver["filter_vin"](page, target_vin["vin"]):
                        if await driver["open_vehicle"](page, target_vin["vin"]):
                            await driver["media_and_save"](page, target_vin["vin"], target_vin["images"])
                else:
                    print("No VIN folders with images found for this account — skipping the automated cycle.")

            if manual_seconds > 0:
                print(f"\nBrowser is open. Click around manually for {manual_seconds} second(s) — "
                      f"every request is still being captured ...")
                await asyncio.sleep(manual_seconds)

        finally:
            otp_stop.set()
            await asyncio.sleep(0.3)  # let any in-flight response handlers finish
            await browser.close()

    ts = int(time.time())
    safe_account = "".join(c if c.isalnum() or c in "-_" else "_" for c in account_name)
    json_path = BASE / f"recon_{dms}_{safe_account}_{ts}.json"
    summary_path = BASE / f"recon_{dms}_{safe_account}_{ts}_summary.txt"

    # The JSON file carries FULL secret values (cookie/token values, request
    # headers) on purpose — that's what's needed to actually build the HTTP
    # client. Treat this file like a password. Never paste its raw contents
    # anywhere; the summary below is the redacted, shareable version.
    json_path.write_text(json.dumps(
        {"captured": captured, "console": console_msgs, "auth_findings": auth_findings},
        indent=2, default=str))

    mutations = [c for c in captured if c.get("kind") == "request" and c.get("class") == "likely_mutation"]
    reads = [c for c in captured if c.get("kind") == "request" and c.get("class") == "api_read"]
    seen_mut, seen_read = set(), set()

    lines = [
        f"Recon summary — {account_name} ({server.DMS_LABELS.get(dms, dms)})",
        f"Captured {len(captured)} total network events.",
        "",
        "=== Where does the session live? (values redacted — safe to share this section) ===",
    ]
    cookies = auth_findings.get("cookies", [])
    if cookies:
        lines.append(f"  {len(cookies)} cookie(s) set after login:")
        for c in cookies:
            flags = ", ".join(f for f in
                              [("HttpOnly" if c.get("httpOnly") else None),
                               ("Secure" if c.get("secure") else None),
                               (f"SameSite={c.get('sameSite')}" if c.get("sameSite") else None)]
                              if f)
            lines.append(f"    {c['name']}  (domain={c.get('domain')}"
                         f"{', ' + flags if flags else ''})  value={c['value_redacted']}")
    else:
        lines.append("  No cookies were set after login.")

    any_auth_header = any(r.get("has_authorization_header") for r in captured if r.get("kind") == "request")
    if any_auth_header:
        sample = next(r for r in captured if r.get("kind") == "request" and r.get("has_authorization_header"))
        lines.append(f"  An Authorization header WAS seen on requests, e.g.: "
                     f"{sample['authorization_header_redacted']}")
    else:
        lines.append("  No Authorization header was seen on any captured request.")

    ls_tokens = auth_findings.get("local_storage_token_keys", [])
    ss_tokens = auth_findings.get("session_storage_token_keys", [])
    if ls_tokens:
        lines.append(f"  localStorage has {len(ls_tokens)} token-like key(s): " +
                     ", ".join(f"{t['key']}={t['value_redacted']}" for t in ls_tokens))
    if ss_tokens:
        lines.append(f"  sessionStorage has {len(ss_tokens)} token-like key(s): " +
                     ", ".join(f"{t['key']}={t['value_redacted']}" for t in ss_tokens))
    if not ls_tokens and not ss_tokens:
        lines.append("  No token-like keys found in localStorage or sessionStorage.")

    lines += ["", "=== Likely mutation calls (delete / upload / save candidates) ==="]
    for m in mutations:
        key = (m["method"], m["url"].split("?")[0])
        if key in seen_mut:
            continue
        seen_mut.add(key)
        lines.append(f"  {m['method']:6} {m['url']}")
        if m.get("post_data"):
            lines.append(f"         body: {m['post_data'][:300]}")
    if not mutations:
        lines.append("  (none seen — try increasing --manual-seconds and actually deleting/uploading by hand)")

    lines += ["", "=== Other API reads (search / open-record candidates) ==="]
    for r in reads:
        key = (r["method"], r["url"].split("?")[0])
        if key in seen_read:
            continue
        seen_read.add(key)
        lines.append(f"  {r['method']:6} {r['url']}")
    if not reads:
        lines.append("  (none seen)")

    lines += ["", f"Full detail (headers, response bodies, timings, FULL secret values — keep this file "
                  f"local, don't share it): {json_path.name}"]
    summary_path.write_text("\n".join(lines))

    print(f"\nCaptured {len(captured)} network events.")
    print(f"  Full detail (has real secrets — keep local):  {json_path}")
    print(f"  Summary (redacted, safe to share):            {summary_path}")
    print(f"\n{len(seen_mut)} unique likely-mutation endpoint(s), {len(seen_read)} unique API-read endpoint(s) found.")



def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", required=True, help="Account folder name, exactly as it appears under --data-root")
    ap.add_argument("--data-root", required=True, help="The 'Live' folder containing account subfolders")
    ap.add_argument("--vin", default=None, help="Specific VIN to run the automated cycle against (default: first available)")
    ap.add_argument("--manual-seconds", type=int, default=0, help="Keep the browser open this many seconds for manual clicking after login (or instead of the automated cycle, with --skip-automated)")
    ap.add_argument("--skip-automated", action="store_true", help="Don't run the automated filter/open/delete/upload/save cycle — just log in and capture whatever you do manually")
    ap.add_argument("--headless", action="store_true", help="Run headless (not recommended for recon — you want to see what's happening)")
    args = ap.parse_args()

    if args.skip_automated and args.manual_seconds == 0:
        print("--skip-automated with no --manual-seconds means this would just log in and immediately "
              "close. Add --manual-seconds N to actually give yourself time to click around.")
        return

    asyncio.run(run_recon(
        account_name=args.account,
        data_root=args.data_root,
        vin=args.vin,
        manual_seconds=args.manual_seconds,
        headless=args.headless,
        skip_automated=args.skip_automated,
    ))


if __name__ == "__main__":
    main()
