"""
dc_http.py — direct-HTTP implementation of DealerCenter's photo-replace
pipeline (search -> open -> delete old photos -> upload new ones -> save),
built entirely from evidence captured by recon.py against a real DealerCenter
session (see recon_dealercenter_International_Auto_*.json).

CONFIRMED FROM EVIDENCE (two independent recon runs, same shapes both times):
  - Auth: cookie jar + Authorization: Bearer <JWT> (harvested from a live
    request, NOT derivable from any cookie — confirmed by hash comparison,
    the JWT is NOT the dc_auth0_token cookie value) + X-XSRF-TOKEN header
    (confirmed byte-identical to the XSRF-TOKEN cookie — classic double
    submit) + dc-location / dc-user custom headers.
  - Document/DeleteFiles: POST, JSON array of document GUIDs.
  - Document/GetUploadInfoAndSAS: POST {companyId, expireSessionInMinutes,
    numberOfImages} -> {sas, basePath, imageInfo:[{fileName, etag}, ...]}.
  - Upload: PUT straight to Azure Blob Storage using basePath + fileName + the
    sas query string. This is Microsoft's own REST API, not DealerCenter's.
  - Document/AddImageDetails: POST per image, registers the blob's metadata
    against the vehicle record. CRITICAL: the "eTag" field must be
    DealerCenter's OWN pre-generated identifier from GetUploadInfoAndSAS's
    imageInfo (format "<YYYYMM>-<fileName>") — NOT the real Azure blob
    storage etag (format "0x8D...", hex). Confirmed by diffing the two
    side by side. An earlier version of this module sent the real Azure
    etag instead: every call still returned 200, so nothing looked wrong,
    but DealerCenter silently couldn't resolve any of the 24 uploaded
    photos afterward — every one showed as a placeholder icon on the real
    vehicle record, caught only by manually checking the DealerCenter UI
    after a "successful" run.
  - InventoryDocument/ReorderImages: POST [{imageId, order}, ...].
  - Document/GetInventoryImages: GET ?inventoryId=... -> plain JSON array,
    each item has a lowercase "id" field. Directly confirmed from a captured
    response body — not a guess.
  - Inventory/LoadInventoryById: POST {inventoryId, loadOption, ...} -> the
    full vehicle record, camelCase fields, includes "optimisticLockField".
  - Inventory/SaveInventory: POST {"changeSource": "web", "inventory":
    <full record>} — CONFIRMED wrapped in this envelope from a real
    browser-captured request body (an earlier pass sent the bare record as
    the top-level body, which is exactly why the first live test got a 400
    "Invalid request" with no field-level detail — wrong shape entirely, not
    a lock or data problem). The response itself is NOT wrapped — confirmed
    bare record back, same as LoadInventoryById's shape. Same
    optimistic-lock field must round-trip or the save is rejected — this is
    the same "another user/process had changed this inventory record" case
    the existing browser automation already detects.

NOT CONFIRMED — ONE REAL GAP:
  - Report-API/InventoryCustomReport/GetInventoryDetailedReport, filtered by
    {"Field": "StockVinSearch", "Value1": <vin>}, IS the real VIN search —
    confirmed directly from a captured request/response pair (an earlier pass
    had guessed a different, wrong URL path segment (/inventory/ instead of
    /report-api/), which is what actually caused a 404 on first real-world
    test, not anything about auth).
  - What's still genuinely unconfirmed: the exact field names for the VIN and
    the inventory id on each returned row. That endpoint's rows use
    PascalCase (Engine, TitleStatus, AskingPrice, ...) — a DIFFERENT casing
    convention than LoadInventoryById's camelCase — and even the
    VIN-filtered (so much smaller) response was still large enough that
    recon's capture truncated before reaching those two fields both times.
  - Rather than guess and hardcode a field name, `find_inventory_id_for_vin`
    below searches the returned row generically for the VIN string wherever
    it appears, then tries a short list of plausible ID-field-name
    candidates on that same row. If none match, it raises
    `SearchFieldMismatch` with the row's ACTUAL keys attached — so this one
    real unknown surfaces immediately and legibly on the first real test
    run, instead of silently guessing wrong.

SAVE SAFETY: this module never invents field values for SaveInventory. It
always does a fresh LoadInventoryById immediately before saving, and echoes
that exact record back with its current optimisticLockField — a pure
read-and-resend, not a hand-built payload. If the response doesn't look like
a clean success (non-2xx, or missing the expected "id"/lock field, or an
error-shaped body), it's treated as a failure — conservatively, since there's
no captured example of what an actual lock-conflict response looks like yet.

This is a STANDALONE module. Nothing in server.py calls it yet — validate it
against one real VIN with test_dc_http.py first.
"""

import asyncio
import json
import mimetypes
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

BASE_URL = "https://app.dealercenter.net"
API_GATEWAY = f"{BASE_URL}/api-gateway"

# Candidates tried, in order, when hunting for the inventory-id field on a
# matched grid row (see the big module docstring above — this is the one
# genuinely unconfirmed piece).
ID_FIELD_CANDIDATES = ["Id", "InventoryId", "ID", "id", "inventoryId"]


class DCHttpError(Exception):
    """Base class for anything that should trigger a fallback to browser
    automation for this VIN, rather than being treated as a hard failure."""


class SearchFieldMismatch(DCHttpError):
    """Raised when a grid row matching the VIN was found, but none of the
    candidate ID field names were present on it. `row_keys` is the row's
    ACTUAL keys — use this to fix ID_FIELD_CANDIDATES above in one line."""
    def __init__(self, vin, row_keys):
        self.vin = vin
        self.row_keys = row_keys
        super().__init__(
            f"Found a grid row for VIN {vin}, but none of {ID_FIELD_CANDIDATES} "
            f"matched. The row's actual keys are: {sorted(row_keys)}"
        )


class VinNotFound(DCHttpError):
    def __init__(self, vin):
        self.vin = vin
        super().__init__(f"VIN {vin} was not found in the active-inventory grid.")


class LockConflict(DCHttpError):
    """The optimistic-lock check failed on save — equivalent to the browser
    flow's 'another user/process had changed this inventory record'."""


class UnexpectedResponse(DCHttpError):
    def __init__(self, step, response):
        self.step = step
        self.status = getattr(response, "status_code", None)
        self.body_preview = (getattr(response, "text", "") or "")[:500]
        super().__init__(f"{step}: unexpected response (status={self.status}): {self.body_preview}")


@dataclass
class DCSession:
    """Everything an httpx client needs to authenticate as an
    already-logged-in DealerCenter browser session. Harvested once from
    Playwright right after login, reused for the rest of that account's run."""
    cookies: dict
    authorization: str
    dc_location: str
    dc_user: str
    company_id: Optional[str] = None
    incomplete: bool = False  # True if only a partial header match was ever seen

    def headers(self, extra=None):
        h = {
            "authorization": self.authorization,
            "dc-location": self.dc_location,
            "dc-user": self.dc_user,
            "content-type": "application/json",
            "x-xsrf-token": self.cookies.get("XSRF-TOKEN", ""),
        }
        if extra:
            h.update(extra)
        return h


class SessionHarvester:
    """Attach BEFORE calling the browser login function, so it's guaranteed
    not to miss the first authenticated request. Usage:

        harvester = SessionHarvester()
        harvester.attach(page)
        await dc_login(page, user, pw, time.time(), lane)
        session = await harvester.build(ctx, timeout=15)

    Waits specifically for a request that has Authorization AND dc-location
    AND dc-user all present — some early post-login calls carry a bearer
    token but not the location/user context headers (e.g. lightweight
    admin/userauth checks that fire before the Home page's widgets have
    loaded), and using one of those produces a session that 404s against the
    inventory API because the gateway can't route it. If nothing with the
    full set ever shows up before the timeout, falls back to the most
    complete partial match seen and flags it via `incomplete`.
    """
    def __init__(self):
        self._headers = None
        self._best_partial = None

    def attach(self, page):
        page.on("request", self._on_request)

    def _on_request(self, request):
        if self._headers is not None:
            return
        if "/api-gateway/" not in request.url:
            return
        asyncio.ensure_future(self._capture(request))

    async def _capture(self, request):
        if self._headers is not None:
            return
        try:
            headers = await request.all_headers()
        except Exception:
            return
        if "authorization" not in headers:
            return
        has_location = bool(headers.get("dc-location"))
        has_user = bool(headers.get("dc-user"))
        if has_location and has_user:
            self._headers = headers
            return
        completeness = int(has_location) + int(has_user)
        best_completeness = 0
        if self._best_partial is not None:
            best_completeness = (int(bool(self._best_partial.get("dc-location"))) +
                                 int(bool(self._best_partial.get("dc-user"))))
        if self._best_partial is None or completeness > best_completeness:
            self._best_partial = headers

    async def build(self, ctx, timeout=15.0) -> Optional[DCSession]:
        deadline = time.time() + timeout
        while self._headers is None and time.time() < deadline:
            await asyncio.sleep(0.2)

        headers = self._headers or self._best_partial
        if headers is None:
            return None
        cookies_list = await ctx.cookies()
        cookies = {c["name"]: c["value"] for c in cookies_list}
        return DCSession(
            cookies=cookies,
            authorization=headers.get("authorization", ""),
            dc_location=headers.get("dc-location", ""),
            dc_user=headers.get("dc-user", ""),
            incomplete=(self._headers is None),
        )


def make_client(session: DCSession) -> httpx.AsyncClient:
    return httpx.AsyncClient(cookies=session.cookies, timeout=30.0, follow_redirects=True)


# ── inventory lookup ─────────────────────────────────────────────────────
# CORRECTED from a second look at the evidence: the endpoint lives under
# /report-api/, not /inventory/ (my first pass had the wrong path segment —
# that's what was actually causing the 404, not anything about auth). AND
# there IS a dedicated VIN-search filter after all: "StockVinSearch". No
# pagination/row-walking needed — one filtered call returns just the match.
INVENTORY_REPORT_URL = f"{API_GATEWAY}/report-api/InventoryCustomReport/GetInventoryDetailedReport"


async def _search_by_vin(client, session, vin):
    body = {
        "Skip": 0, "Take": 5,
        "Filters": [
            {"Field": "DateInStock", "Operator": "", "Value1": "", "Value2": "", "DataType": "DATE"},
            {"Field": "InventoryStatus", "Operator": "Equals", "Value1": "0"},
            {"Field": "VehiclePrice", "Operator": "Equals", "Value1": " "},
            {"Field": "Mileage", "Operator": "Equals", "Value1": " "},
            {"Field": "NeedAttention", "Operator": "Equals", "Value1": " "},
            {"Field": "CustomStatus", "Operator": "Equals", "Value1": ""},
            {"Field": "Location", "Operator": "Equals", "Value1": " "},
            {"Field": "VehicleSaleType", "Operator": "Equals", "Value1": " "},
            {"Field": "YMMTSearch", "Operator": "", "Value1": "", "Value2": ""},
            {"Field": "StockVinSearch", "Operator": "", "Value1": vin, "Value2": ""},
            {"Field": "SearchTags", "Operator": "Equals", "Value1": " "},
        ],
        "Sort": "", "SortDirection": "",
        "Sorting": [{"Field": "createddate", "Direction": "DESC"}],
        "Group": None,
    }
    r = await client.post(INVENTORY_REPORT_URL, json=body, headers=session.headers())
    if r.status_code != 200:
        raise UnexpectedResponse("GetInventoryDetailedReport", r)
    data = r.json()
    return data.get("Data") or data.get("data") or []


def _row_contains_vin(row, vin):
    """Generic, field-name-agnostic search: does this row have the VIN as a
    value ANYWHERE (not assuming a specific key)? The StockVinSearch filter
    should already narrow the server-side result to just this VIN, but this
    stays as a sanity check in case the filter is looser than expected
    (e.g. partial match) and returns more than one row."""
    target = vin.strip().upper()

    def walk(value):
        if isinstance(value, str):
            return value.strip().upper() == target
        if isinstance(value, dict):
            return any(walk(v) for v in value.values())
        if isinstance(value, list):
            return any(walk(v) for v in value)
        return False

    return walk(row)


def _extract_id(row, vin):
    for key in ID_FIELD_CANDIDATES:
        if key in row and row[key]:
            return row[key]
    raise SearchFieldMismatch(vin, list(row.keys()))


async def find_inventory_id_for_vin(client, session, vin):
    """Uses the confirmed StockVinSearch filter — one server-side-filtered
    call, no pagination needed. Still verifies the returned row actually
    contains the VIN (not just trusting the filter blindly) before extracting
    an id, and raises SearchFieldMismatch with the row's real keys if none of
    the candidate id field names match."""
    rows = await _search_by_vin(client, session, vin)
    for row in rows:
        if _row_contains_vin(row, vin):
            return _extract_id(row, vin)
    raise VinNotFound(vin)


# ── confirmed-safe steps ──────────────────────────────────────────────────
async def load_inventory(client, session, inventory_id):
    url = f"{API_GATEWAY}/inventory/Inventory/LoadInventoryById"
    body = {"inventoryId": inventory_id, "loadOption": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 17, 18, 16, 11, 20],
            "setIsCurrentForBook": False}
    r = await client.post(url, json=body, headers=session.headers())
    if r.status_code != 200:
        raise UnexpectedResponse("LoadInventoryById", r)
    record = r.json()
    if "id" not in record or "optimisticLockField" not in record:
        raise UnexpectedResponse("LoadInventoryById (missing expected fields)", r)
    return record


async def fetch_photo_ids(client, session, inventory_id):
    url = f"{API_GATEWAY}/inventory/Document/GetInventoryImages"
    r = await client.get(url, params={"inventoryId": inventory_id}, headers=session.headers())
    if r.status_code != 200:
        raise UnexpectedResponse("GetInventoryImages", r)
    data = r.json()
    images = data if isinstance(data, list) else data.get("images") or data.get("Data") or []
    ids = []
    for img in images:
        for key in ("id", "Id", "documentId", "DocumentId"):
            if key in img and img[key]:
                ids.append(img[key])
                break
    return ids


async def delete_photos(client, session, document_ids):
    if not document_ids:
        return
    url = f"{API_GATEWAY}/inventory/Document/DeleteFiles"
    r = await client.post(url, json=document_ids, headers=session.headers())
    if r.status_code not in (200, 204):
        raise UnexpectedResponse("DeleteFiles", r)


async def get_upload_sas(client, session, company_id, num_images):
    url = f"{API_GATEWAY}/inventory/Document/GetUploadInfoAndSAS"
    body = {"companyId": company_id, "expireSessionInMinutes": 10, "numberOfImages": num_images}
    r = await client.post(url, json=body, headers=session.headers())
    if r.status_code != 200:
        raise UnexpectedResponse("GetUploadInfoAndSAS", r)
    data = r.json()
    if data.get("errorMessage"):
        raise UnexpectedResponse("GetUploadInfoAndSAS (errorMessage set)", r)
    return data  # {"sas": "...", "basePath": "...", "imageInfo": [{"fileName","etag"}, ...]}


async def upload_one_image(client, sas_info, file_name, image_bytes, content_type="image/jpeg"):
    """Straight PUT to Azure Blob Storage — Microsoft's own API, not
    DealerCenter's. No DealerCenter auth headers needed or wanted here.

    Returns the REAL Azure storage etag (hex, e.g. "0x8DE7EEDDA59A8B7") —
    for logging/diagnostics only. Do NOT pass this to add_image_details:
    DealerCenter's AddImageDetails expects its OWN pre-generated identifier
    from the GetUploadInfoAndSAS response's imageInfo[i]["etag"] (format
    "<YYYYMM>-<fileName>"), a completely different value that happens to
    share the field name "etag". Confirmed by diffing a real SAS response
    against the real blob PUT response for the same file — sending the real
    Azure etag instead of DealerCenter's own is exactly what silently broke
    every uploaded photo's display (registration "succeeded" with a 200,
    but DealerCenter couldn't actually resolve the image afterward — it
    showed as a placeholder icon for all 24 photos on a real test run)."""
    url = f"{sas_info['basePath']}/{file_name}{sas_info['sas']}"
    r = await client.put(url, content=image_bytes, headers={
        "x-ms-blob-type": "BlockBlob",
        "content-type": content_type,
    })
    if r.status_code not in (200, 201):
        raise UnexpectedResponse(f"blob upload ({file_name})", r)
    return r.headers.get("etag", "").strip('"')


async def add_image_details(client, session, company_id, inventory_id, file_name, dc_etag,
                             content_length, width, height, order, name):
    """`dc_etag` must be DealerCenter's own pre-generated identifier from
    GetUploadInfoAndSAS's imageInfo — NOT the real Azure blob storage etag."""
    url = f"{API_GATEWAY}/inventory/Document/AddImageDetails"
    body = {
        "contentLength": str(content_length),
        "eTag": dc_etag,
        "entityId": inventory_id,
        "imageDimension": {"height": height, "width": width},
        "name": name,
        "order": order,
        "companyId": company_id,
        "documentType": 2,
    }
    r = await client.post(url, json=body, headers=session.headers())
    if r.status_code != 200:
        raise UnexpectedResponse("AddImageDetails", r)
    data = r.json()
    doc = data.get("document") or data
    image_id = doc.get("id") or doc.get("Id")
    if not image_id:
        raise UnexpectedResponse("AddImageDetails (no image id in response)", r)
    return image_id


async def reorder_images(client, session, ordered_image_ids):
    url = f"{API_GATEWAY}/inventory/InventoryDocument/ReorderImages"
    body = {"imagesOrder": [{"imageId": iid, "order": i} for i, iid in enumerate(ordered_image_ids)]}
    r = await client.post(url, json=body, headers=session.headers())
    if r.status_code != 200:
        raise UnexpectedResponse("ReorderImages", r)


def _dump_failed_save(inventory_id, record, response):
    """Writes the exact record we tried to POST, plus the server's response,
    to a local file next to this module — for diffing against a real
    browser-captured SaveInventory body when one becomes available."""
    try:
        path = f"dc_http_save_failure_{inventory_id}_{int(time.time())}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "inventory_id": inventory_id,
                "request_body_we_sent": record,
                "request_field_count": len(record) if isinstance(record, dict) else None,
                "response_status": response.status_code,
                "response_headers": dict(response.headers),
                "response_body": (getattr(response, "text", "") or "")[:2000],
            }, f, indent=2, default=str)
        print(f"\n[dc_http] Wrote the failed save's exact request body to: {path}")
        print("[dc_http] Compare this against a real browser-captured SaveInventory "
              "body (recon.py, after its own truncation fix) to see what differs.")
    except Exception as e:
        print(f"[dc_http] Could not write save-failure debug dump: {e}")


async def save_inventory(client, session, inventory_id, debug_dump_on_failure=True):
    """The careful part. Always re-fetches the record fresh right before
    saving (so the lock field is current). Never hand-builds the record's
    own fields — but DOES wrap it in the envelope the real API actually
    expects: {"changeSource": "web", "inventory": <record>}. Confirmed from
    a real browser-captured SaveInventory request body — sending the bare
    record as the top-level body (what this function did before) is exactly
    why the first live test got a 400 "Invalid request" with no field-level
    detail: it wasn't the right shape for the model binder to even attempt
    validation on. The response itself is NOT wrapped — confirmed from an
    earlier successful capture, it's the bare record back — so only the
    request needs the envelope.

    Treats any non-clean response as a lock conflict/failure rather than
    assuming success. If the save fails, optionally dumps the exact envelope
    we tried to send to a local file for further diagnosis."""
    record = await load_inventory(client, session, inventory_id)
    lock_before = record["optimisticLockField"]

    url = f"{API_GATEWAY}/inventory/Inventory/SaveInventory"
    envelope = {"changeSource": "web", "inventory": record}
    r = await client.post(url, json=envelope, headers=session.headers())

    if r.status_code not in (200,):
        if debug_dump_on_failure:
            _dump_failed_save(inventory_id, envelope, r)
        if r.status_code == 409:
            raise LockConflict(f"SaveInventory returned 409 for inventory {inventory_id}")
        raise UnexpectedResponse("SaveInventory", r)

    try:
        result = r.json()
    except Exception:
        raise UnexpectedResponse("SaveInventory (non-JSON response)", r)

    if result.get("id") != inventory_id:
        raise UnexpectedResponse("SaveInventory (response id doesn't match)", r)
    if any(k in result for k in ("error", "errorMessage", "Message")):
        raise LockConflict(f"SaveInventory response body looked like an error: {str(result)[:300]}")

    new_lock = result.get("optimisticLockField")
    if new_lock is not None and new_lock == lock_before:
        # A genuine save normally advances the lock field; an unchanged lock
        # after a "successful" 200 is suspicious enough to treat as a
        # conflict rather than assume it worked.
        raise LockConflict(
            f"SaveInventory returned 200 but optimisticLockField didn't change "
            f"({lock_before!r} -> {new_lock!r}) for inventory {inventory_id}"
        )
    return result


# ── the full pipeline for one VIN ────────────────────────────────────────
async def replace_photos_for_vin(client, session: DCSession, company_id, vin, images, log=print):
    """images: list of (file_path, bytes, width, height) tuples — width/height
    are needed for AddImageDetails; caller is responsible for reading them
    (e.g. with Pillow) before calling this, since this module has no image
    dependency of its own.

    Returns True on confirmed success. Raises a DCHttpError subclass on
    anything that should fall back to browser automation for this VIN."""
    log("info", f"[http] Locating inventory record for {vin} ...")
    inventory_id = await find_inventory_id_for_vin(client, session, vin)

    log("info", f"[http] Fetching current photo list for {vin} ...")
    existing_ids = await fetch_photo_ids(client, session, inventory_id)
    if existing_ids:
        log("info", f"[http] Deleting {len(existing_ids)} existing photo(s) ...")
        await delete_photos(client, session, existing_ids)
    else:
        log("info", "[http] No existing photos to delete.")

    log("info", f"[http] Requesting {len(images)} upload slot(s) ...")
    sas_info = await get_upload_sas(client, session, company_id, len(images))
    image_infos = sas_info["imageInfo"]
    if len(image_infos) < len(images):
        raise DCHttpError(
            f"GetUploadInfoAndSAS returned {len(image_infos)} upload slot(s) "
            f"for {len(images)} requested image(s) — refusing to proceed."
        )

    new_image_ids = []
    for i, (path, img_bytes, width, height) in enumerate(images):
        file_name = image_infos[i]["fileName"]
        dc_etag = image_infos[i].get("etag", "")
        content_type = mimetypes.guess_type(path)[0] or "image/jpeg"
        log("info", f"[http] Uploading {path} -> {file_name} ...")
        azure_etag = await upload_one_image(client, sas_info, file_name, img_bytes, content_type=content_type)
        log("info", f"[http]   blob stored (azure etag {azure_etag}), registering with "
                     f"DealerCenter's own id {dc_etag} ...")
        image_id = await add_image_details(
            client, session, company_id, inventory_id, file_name, dc_etag,
            content_length=len(img_bytes), width=width, height=height, order=i,
            name=os.path.basename(path),
        )
        new_image_ids.append(image_id)

    log("info", "[http] Reordering images ...")
    await reorder_images(client, session, new_image_ids)

    log("info", f"[http] Saving inventory record for {vin} ...")
    await save_inventory(client, session, inventory_id)

    log("ok", f"[http] ✓ {vin} complete via direct HTTP")
    return True
