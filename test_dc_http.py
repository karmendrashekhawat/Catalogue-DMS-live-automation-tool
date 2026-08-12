r"""
test_dc_http.py — validate dc_http.py against ONE real VIN before it goes
anywhere near the main automation.

This logs in through the exact same browser flow the main app uses (so login/
MFA is unchanged and untouched), harvests the session right after, and then
runs the direct-HTTP pipeline (search -> delete -> upload -> save) against
one VIN you choose.

Nothing gets deleted or uploaded unless you pass --confirm-write. Without it,
this only reads: finds the inventory record, fetches the current photo list,
and stops there — so you can check the "found inventory" and "found N
existing photos" numbers look right before trusting it with a real write.

USAGE
-----
Read-only check first (recommended):

    python test_dc_http.py --account "International Auto" --data-root "C:\Live" --vin 1FMEU73EX8UB24500

Only once that looks right, actually run the write (delete + upload + save)
for real, against that one VIN:

    python test_dc_http.py --account "International Auto" --data-root "C:\Live" --vin 1FMEU73EX8UB24500 --confirm-write
"""

import argparse
import asyncio
import time
from pathlib import Path

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

import server
import dc_http
from recon import make_fake_lane, start_console_otp_watcher, redact


def read_image_dims(path):
    if HAVE_PIL:
        try:
            with Image.open(path) as im:
                return im.size  # (width, height)
        except Exception:
            pass
    return (0, 0)  # DealerCenter accepts this; it just won't have real dimensions on record


async def run_test(account_name, data_root, vin, confirm_write, headless):
    creds = server.load_accounts_credentials()
    accounts = server.scan_root(Path(data_root), creds)
    acc = next((a for a in accounts if a["folder_name"] == account_name), None)
    if not acc or not acc["cred"] or acc["cred"]["dms"] != "dealercenter":
        print(f"'{account_name}' not found, has no credentials, or isn't a DealerCenter "
              f"account — this test tool only covers DealerCenter so far.")
        return

    target = next((v for v in acc["vins"] if v["vin"] == vin), None)
    if not target:
        print(f"VIN '{vin}' not found in '{account_name}''s folders. Available: "
              f"{[v['vin'] for v in acc['vins']]}")
        return

    lane = make_fake_lane()
    otp_stop = __import__("threading").Event()
    start_console_otp_watcher(lane, otp_stop)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--start-maximized"])
        ctx = await browser.new_context(no_viewport=True)
        page = await ctx.new_page()
        page.set_default_timeout(60000)

        harvester = dc_http.SessionHarvester()
        harvester.attach(page)

        print("Logging in ...")
        ok = await server.dc_login(page, acc["cred"]["user"], acc["cred"]["pass"], time.time(), lane)
        if not ok:
            print("Login did not confirm success — stopping.")
            await browser.close()
            return

        print("Harvesting the HTTP session from the live browser page ...")
        session = await harvester.build(ctx, timeout=20.0)
        if not session:
            print("Could not harvest an authenticated request within 20s — "
                  "the app may not have made any api-gateway calls yet. Try again, "
                  "or increase the timeout in this script.")
            await browser.close()
            return
        if session.incomplete:
            print("\n  WARNING: only a partial header set was ever seen (missing dc-location "
                  "and/or dc-user). This session will likely 404 against the inventory API. "
                  "Try increasing the harvest timeout, or make sure the account's Home page "
                  "has fully loaded (widgets, inventory count, etc.) before this check runs.")
        print(f"  dc-location: {redact(session.dc_location) if session.dc_location else '(EMPTY)'}")
        print(f"  dc-user: {redact(session.dc_user) if session.dc_user else '(EMPTY)'}")
        print(f"  {len(session.cookies)} cookie(s) captured, Authorization header captured.")

        # We still need a companyId for GetUploadInfoAndSAS — pull it from the
        # record itself once we've found the vehicle, rather than guessing it.
        client = dc_http.make_client(session)

        try:
            print(f"\nLocating inventory record for {vin} via the HTTP pipeline ...")
            inventory_id = await dc_http.find_inventory_id_for_vin(client, session, vin)
            print(f"  Found inventory id: {inventory_id}")

            record = await dc_http.load_inventory(client, session, inventory_id)
            company_id = record.get("companyId")
            print(f"  companyId: {company_id}")
            print(f"  optimisticLockField: {record.get('optimisticLockField')}")

            existing = await dc_http.fetch_photo_ids(client, session, inventory_id)
            print(f"  {len(existing)} existing photo(s) found.")

            if not confirm_write:
                print("\n--confirm-write not set — stopping here. Nothing was deleted, "
                      "uploaded, or saved. If the numbers above look right, re-run with "
                      "--confirm-write to actually process this VIN's photos via HTTP.")
                return

            print(f"\n--confirm-write set. Reading {len(target['images'])} image file(s) "
                  f"from disk and running the full pipeline for real ...")
            images = []
            for img_path in target["images"]:
                data = Path(img_path).read_bytes()
                w, h = read_image_dims(img_path)
                images.append((img_path, data, w, h))

            await dc_http.replace_photos_for_vin(
                client, session, company_id, vin, images, log=server.log)
            print(f"\nSUCCESS — {vin} was processed entirely via direct HTTP, no browser "
                  f"clicks needed for the photo-replace itself.")

        except dc_http.SearchFieldMismatch as e:
            print(f"\nSEARCH FIELD MISMATCH — the VIN was found in the grid, but the ID "
                  f"field guess was wrong. Actual row keys: {e.row_keys}")
            print("Fix: add the correct key to ID_FIELD_CANDIDATES at the top of dc_http.py.")
        except dc_http.LockConflict as e:
            print(f"\nLOCK CONFLICT — {e}")
            print("This is the direct-HTTP equivalent of DealerCenter's own "
                  "'another user/process had changed this inventory record' error.")
        except dc_http.DCHttpError as e:
            print(f"\n{type(e).__name__}: {e}")
            print("This is exactly the kind of failure that should fall back to browser "
                  "automation for this VIN once wired into the main app.")
        finally:
            await client.aclose()
            otp_stop.set()
            await browser.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", required=True)
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--vin", required=True)
    ap.add_argument("--confirm-write", action="store_true",
                     help="Actually delete/upload/save. Without this flag, only reads.")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()
    asyncio.run(run_test(args.account, args.data_root, args.vin, args.confirm_write, args.headless))


if __name__ == "__main__":
    main()
