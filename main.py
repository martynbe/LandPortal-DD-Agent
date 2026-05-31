#!/usr/bin/env python3
"""
LandPortal DD Slack Bot — /dd slash command handler
Environment variables: SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN, LANDPORTAL_JWT,
                       GOOGLE_SERVICE_ACCOUNT_JSON (optional), GOOGLE_DRIVE_FOLDER_ID (optional)
"""

import hashlib
import hmac
import json
import logging
import os
import subprocess
import tempfile
import time
import traceback
from pathlib import Path

import requests
from fastapi import BackgroundTasks, Request
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI()

SLACK_SIGNING_SECRET      = os.environ.get("SLACK_SIGNING_SECRET", "")
SLACK_BOT_TOKEN           = os.environ.get("SLACK_BOT_TOKEN", "")
LP_TOKEN                  = os.environ.get("LANDPORTAL_JWT", "")
GOOGLE_SA_JSON            = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_DRIVE_FOLDER_ID    = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
GDRIVE_FOLDER_NAME        = "LandPortal Due Diligence Agent Reports"
LP_BASE              = "https://landportal.com/wp-json/lp-rest-api/v1"
LP_HEADERS           = {"Authorization": f"Bearer {LP_TOKEN}", "Content-Type": "application/json"}
SCRIPT_DIR           = Path(__file__).parent


# ── Slack helpers ──────────────────────────────────────────────────────────────

def verify_slack(body: bytes, timestamp: str, signature: str) -> bool:
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
        base = f"v0:{timestamp}:{body.decode()}"
        computed = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed, signature)
    except Exception:
        return False


def slack_post(channel: str, text: str):
    log.info(f"Posting to Slack channel {channel}: {text[:80]}")
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={"channel": channel, "text": text},
        timeout=10,
    )
    log.info(f"Slack post response: {r.status_code} {r.text[:200]}")


def slack_upload_pdf(channel: str, pdf_path: str, filename: str, title: str):
    log.info(f"Uploading PDF to Slack: {pdf_path}")
    try:
        file_size = os.path.getsize(pdf_path)
        headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}

        # Step 1 — get upload URL
        r1 = requests.post(
            "https://slack.com/api/files.getUploadURLExternal",
            headers=headers,
            data={"filename": filename, "length": file_size},
            timeout=15,
        )
        log.info(f"getUploadURLExternal: {r1.status_code} {r1.text[:200]}")
        r1_data = r1.json()
        if not r1_data.get("ok"):
            slack_post(channel, f"⚠️ Could not get upload URL: {r1_data.get('error')}")
            return
        upload_url = r1_data["upload_url"]
        file_id    = r1_data["file_id"]

        # Step 2 — upload the file
        with open(pdf_path, "rb") as f:
            r2 = requests.post(upload_url, data=f, timeout=30)
        log.info(f"File upload: {r2.status_code}")

        # Step 3 — complete and share to channel
        r3 = requests.post(
            "https://slack.com/api/files.completeUploadExternal",
            headers=headers,
            json={"files": [{"id": file_id, "title": title}], "channel_id": channel},
            timeout=15,
        )
        log.info(f"completeUploadExternal: {r3.status_code} {r3.text[:200]}")
    except Exception as e:
        log.error(f"PDF upload failed: {e}\n{traceback.format_exc()}")
        slack_post(channel, f"⚠️ PDF generated but upload failed: {e}")


# ── Google Drive helper ────────────────────────────────────────────────────────

def _get_or_create_drive_folder(service, folder_name: str) -> str:
    """Find existing Drive folder by name, or create it. Returns folder ID."""
    q = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=q, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        log.info(f"Found existing Drive folder '{folder_name}': {files[0]['id']}")
        return files[0]["id"]
    folder_meta = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
    folder = service.files().create(body=folder_meta, fields="id").execute()
    log.info(f"Created Drive folder '{folder_name}': {folder['id']}")
    return folder["id"]


def upload_to_google_drive(pdf_path: str, filename: str) -> str | None:
    """Upload PDF to Google Drive. Auto-creates 'LandPortal Due Diligence Agent Reports' folder if needed."""
    if not GOOGLE_SA_JSON:
        log.info("Google Drive not configured (GOOGLE_SERVICE_ACCOUNT_JSON missing) — skipping")
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        creds = service_account.Credentials.from_service_account_info(
            json.loads(GOOGLE_SA_JSON),
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        service = build("drive", "v3", credentials=creds, cache_discovery=False)

        # Use env-var folder ID if set, otherwise auto-find/create the named folder
        folder_id = GOOGLE_DRIVE_FOLDER_ID or _get_or_create_drive_folder(service, GDRIVE_FOLDER_NAME)

        file_meta = {"name": filename, "parents": [folder_id]}
        media = MediaFileUpload(pdf_path, mimetype="application/pdf")
        f = service.files().create(body=file_meta, media_body=media, fields="id,webViewLink").execute()
        link = f.get("webViewLink")
        log.info(f"Google Drive upload OK: {link}")
        return link
    except Exception as e:
        log.error(f"Google Drive upload failed: {e}\n{traceback.format_exc()}")
        return None


def make_report_filename(address: str, owner: str, apn: str, county: str, state: str) -> str:
    """Build the standard report filename: address, owner, APN, county state.
    If no address is available, uses '0 Street Name' as placeholder."""
    empty = ("Unknown", "N/A", "", None)
    addr_clean = address.strip() if address and address.strip() not in empty else "0 Street Name"
    owner_clean = owner.strip() if owner and owner.strip() not in empty else "Unknown Owner"
    apn_clean   = apn.strip()   if apn   and apn.strip()   not in empty else "No APN"
    loc_clean   = f"{county} {state}".strip() if (county or state) else "Unknown Location"

    name = f"{addr_clean}, {owner_clean}, {apn_clean}, {loc_clean}"
    # Sanitize for filesystem / Drive
    for ch in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        name = name.replace(ch, '-')
    return f"{name}.pdf"


# ── LandPortal API helpers ─────────────────────────────────────────────────────

def lp_search(query, search_type, state=None, fips=None):
    params = {"type": search_type, "query": query}
    if fips:   params["fips"] = fips
    elif state: params["state"] = state
    try:
        r = requests.get(f"{LP_BASE}/search", headers=LP_HEADERS, params=params, timeout=15)
        log.info(f"LP search {search_type}={query}: {r.status_code}")
        return r.json() if r.ok else None
    except Exception as e:
        log.error(f"LP search error: {e}")
        return None


def lp_property_data(propertyid, fips):
    try:
        r = requests.get(f"{LP_BASE}/property-data", headers=LP_HEADERS,
                         params={"propertyid": propertyid, "fips": fips}, timeout=15)
        log.info(f"LP property-data: {r.status_code}")
        return r.json() if r.ok else None
    except Exception as e:
        log.error(f"LP property-data error: {e}")
        return None


def lp_get_report(propertyid, fips):
    try:
        r = requests.get(f"{LP_BASE}/reports", headers=LP_HEADERS,
                         params={"propertyid": propertyid, "fips": fips}, timeout=15)
        log.info(f"LP get-report: {r.status_code} {r.text[:100]}")
        if r.ok:
            d = r.json()
            if d.get("success"):
                return d.get("data")
    except Exception as e:
        log.error(f"LP get-report error: {e}")
    return None


def lp_post_report(propertyid, fips):
    try:
        r = requests.post(f"{LP_BASE}/reports", headers=LP_HEADERS,
                          json={"propertyid": str(propertyid), "fips": str(fips)}, timeout=15)
        log.info(f"LP post-report: {r.status_code} {r.text[:100]}")
        return r.json() if r.ok else None
    except Exception as e:
        log.error(f"LP post-report error: {e}")
        return None


# ── Input parser ───────────────────────────────────────────────────────────────

def parse_input(text):
    parts = text.strip().split()
    if not parts:
        return None
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return {"type": "direct", "propertyid": int(parts[0]), "fips": parts[1]}
    state = parts[-1].upper() if len(parts[-1]) == 2 and parts[-1].isalpha() else None
    if any(c.isdigit() for c in parts[0]):
        return {"type": "parcel", "query": parts[0], "state": state}
    query = " ".join(parts[:-1] if state else parts)
    return {"type": "owner", "query": query, "state": state}


def _to_float(val):
    try:
        return float(val) if val not in (None, "", False) else None
    except (TypeError, ValueError):
        return None


# ── Comp parser ───────────────────────────────────────────────────────────────

def _parse_similars(similars_raw, subject_acres, subject_county):
    """Parse the similars field from property data into comp list."""
    if not similars_raw:
        return []
    try:
        # May be a JSON string or already a list/dict
        if isinstance(similars_raw, str):
            similars = json.loads(similars_raw)
        else:
            similars = similars_raw

        if not isinstance(similars, list):
            similars = [similars]

        comps = []
        for s in similars:
            if not isinstance(s, dict):
                continue
            acres       = _to_float(s.get("calc_acres") or s.get("lotsizeacres") or s.get("size") or s.get("acres"))
            price       = _to_float(s.get("currentsalesprice") or s.get("saleprice") or s.get("price"))
            price_acre  = _to_float(s.get("price_per_acre") or s.get("ppa"))
            distance    = _to_float(s.get("distance") or s.get("distance_mi") or s.get("dist"))
            county      = s.get("situscounty") or s.get("county") or ""
            landlocked  = s.get("land_locked", False)
            date        = s.get("currentsalerecordingdate") or s.get("sale_date") or s.get("date") or ""
            address     = s.get("situsfullstreetaddress") or s.get("address") or "Unknown"
            prop_id     = s.get("propertyid") or s.get("id")
            lp_url      = s.get("link") or (f"https://landportal.com/property/{prop_id}" if prop_id else "")

            # Calculate price_per_acre if missing
            if not price_acre and price and acres and acres > 0:
                price_acre = price / acres

            if not price_acre or not acres:
                continue

            # Proximity filter — hard cap 50 miles; must be same county OR within 30 miles
            same_county = (subject_county.lower() in county.lower()) if (subject_county and county) else True
            hard_too_far = distance is not None and distance > 50   # never use comps > 50 mi
            diff_county_and_far = (distance is not None and distance > 30 and not same_county)

            # Size filter: 0.3x to 3x subject size
            if subject_acres:
                size_ok = (subject_acres * 0.3) <= acres <= (subject_acres * 3.0)
            else:
                size_ok = True

            if hard_too_far or diff_county_and_far:
                kept = False
            else:
                kept = size_ok and not landlocked

            comps.append({
                "date":          str(date)[:10] if date else "",
                "address":       address,
                "acres":         acres,
                "price_per_acre": price_acre,
                "distance_mi":   distance if distance is not None else 0,
                "landlocked":    landlocked,
                "kept":          kept,
                "lp_url":        lp_url,
            })

        # Sort: same-county first, then by distance
        comps.sort(key=lambda c: (not (subject_county.lower() in (c.get("address","").lower())),
                                   c.get("distance_mi") or 999))
        log.info(f"_parse_similars: {len(comps)} total, {sum(1 for c in comps if c['kept'])} kept")
        return comps
    except Exception as e:
        log.error(f"_parse_similars error: {e}\n{traceback.format_exc()}")
        return []


# ── Main DD workflow ───────────────────────────────────────────────────────────

def run_dd(channel: str, text: str, user_name: str):
    log.info(f"=== run_dd START channel={channel} text={text} user={user_name} ===")
    try:
        _run_dd(channel, text, user_name)
    except Exception as e:
        log.error(f"run_dd FATAL: {e}\n{traceback.format_exc()}")
        try:
            slack_post(channel, f"❌ Error: {e}")
        except Exception:
            pass
    log.info("=== run_dd END ===")


def _run_dd(channel: str, text: str, user_name: str):
    # Step 1 — parse input
    parsed = parse_input(text)
    log.info(f"Parsed: {parsed}")
    if not parsed:
        slack_post(channel, "❌ Couldn't parse input. Try: `/dd 20-0857 Scioto OH`")
        return

    # Step 2 — resolve parcel
    propertyid, fips, prop_meta = None, None, {}

    if parsed["type"] == "direct":
        propertyid, fips = parsed["propertyid"], parsed["fips"]
    else:
        search_type = "parcelnumb" if parsed["type"] == "parcel" else "owner"
        result = lp_search(parsed["query"], search_type, state=parsed.get("state"))
        if not result or not result.get("success"):
            slack_post(channel, "❌ LandPortal search failed.")
            return
        features = result.get("data", {}).get("features", [])
        if not features:
            slack_post(channel, f"❌ No parcels found for: *{text}*")
            return
        prop_meta  = features[0]["properties"]
        propertyid = prop_meta.get("propertyid")
        fips       = prop_meta.get("fips")

    log.info(f"Resolved: propertyid={propertyid} fips={fips}")
    if not propertyid or not fips:
        slack_post(channel, "❌ Could not resolve propertyid/FIPS.")
        return

    # Step 3 — property data
    log.info("Fetching property data...")
    pd_resp = lp_property_data(propertyid, fips)
    pf = pd_resp.get("data", {}).get("property", {}) if pd_resp else {}
    log.info(f"Property fields: {list(pf.keys())}")
    # Deep field dump — used to identify correct LP API field names
    _debug_fields = [
        'tlp_estimate','avm_value','estimated_value','lp_estimate','estimate_price',
        'lp_avm','property_value','avm_estimate','land_value','total_value',
        'buildability_total_perc','buildable_pct','slope_buildable_pct','buildable_percentage',
        'buildability_area_acres','buildable_area_acres','slope_avg','avg_slope',
        'slope_flat_pct','slope_minimal_pct','slope_moderate_pct','slope_heavy_pct','slope_extreme_pct',
        'currentsalerecordingdate','lastsaledate','last_sale_date','sale_date','saledate',
        'currentsalesprice','lastsaleprice','last_sale_price','sale_price',
        'assdtotalvalue','markettotalvalue','assessedvalue','assessed_value','land_assessed_value',
        'taxamt','tax_amount','annualtax','annual_tax','taxyear',
        'concurrentmtgloanamt','mortgage_balance','mtgamt','mortgage_amount',
        'similars','comparables','comps',
        'centroidlatitude','centroidlongitude','lat','lon','latitude','longitude',
    ]
    for _f in _debug_fields:
        _v = pf.get(_f)
        if _v is not None:
            log.info(f"  pf[{_f!r}] = {str(_v)[:120]}")

    address = pf.get("situsfullstreetaddress") or prop_meta.get("address", "Unknown")
    owner   = pf.get("ownername1full") or prop_meta.get("owner", "Unknown")
    apn     = pf.get("apn") or prop_meta.get("apn", "N/A")
    county  = pf.get("situscounty") or prop_meta.get("county", "")
    state_  = pf.get("situsstate") or prop_meta.get("state", "")
    log.info(f"Similars field type: {type(pf.get('similars'))} value: {str(pf.get('similars'))[:500]}")

    # Step 4 — comp report
    log.info("Fetching comp report...")
    comp_report = lp_get_report(propertyid, fips)
    comp_report_pending = False

    if not comp_report:
        log.info("No existing comp report — queuing...")
        post_r = lp_post_report(propertyid, fips)
        if post_r and post_r.get("success"):
            slack_post(channel, "📊 Comp report queued — polling...")
            for i in range(12):
                time.sleep(5)
                comp_report = lp_get_report(propertyid, fips)
                log.info(f"Poll {i+1}/12: comp_report={'found' if comp_report else 'pending'}")
                if comp_report:
                    break
        if not comp_report:
            comp_report_pending = True
            log.info("Comp report still pending — continuing without it")

    log.info(f"Comp report keys: {list(comp_report.keys()) if comp_report else 'None'}")
    if comp_report:
        _cr_debug = [
            'similars','comparables','comps','sales',
            'total_our_estimation_values_base','avm_value','estimated_value','estimation_value',
            'buildability_total_perc','buildable_pct','assessed_value','tax_amount',
            'price_acre_mean','price_acre_county','size','updated_at',
        ]
        for _f in _cr_debug:
            _v = comp_report.get(_f)
            if _v is not None:
                log.info(f"  cr[{_f!r}] = {str(_v)[:120]}")

    # Step 5 — assemble data
    log.info("Assembling data payload...")
    cr    = comp_report or {}
    acres = _to_float(cr.get("size") or pf.get("calc_acres") or pf.get("lotsizeacres") or pf.get("acreageformatted"))

    # Parse similars — try property data first, then comp report fields
    similars_raw = (pf.get("similars") or cr.get("similars") or
                    cr.get("comparables") or cr.get("comps") or cr.get("sales"))
    comps = _parse_similars(similars_raw, acres, county)
    log.info(f"Parsed {len(comps)} comps from similars (source: {'pf' if pf.get('similars') else 'cr'})")

    # Fallback: use price_acre_mean from comp report as a synthetic comp
    if not comps:
        price_acre_mean = _to_float(cr.get("price_acre_mean"))
        if price_acre_mean and acres:
            comps = [{"date": str(cr.get("updated_at", ""))[:10],
                      "address": "LandPortal comp median (county avg)",
                      "acres": acres, "price_per_acre": price_acre_mean,
                      "distance_mi": None,   # None so it doesn't trigger 999-mile flag
                      "landlocked": False, "kept": True, "lp_url": ""}]

    # Build LP property URL — try multiple field names, fall back to constructed URL
    lp_url = (
        pf.get("property_url") or pf.get("url") or pf.get("link") or
        cr.get("property_url") or cr.get("url") or cr.get("link") or
        f"https://landportal.com/property/{propertyid}"
    )
    log.info(f"LP property URL: {lp_url}")

    data = {
        "property": {
            "propertyid":        propertyid,
            "apn":               apn,
            "address":           f"{address}, {county} County, {state_}",
            "owner":             owner,
            "county":            county,
            "fips":              str(fips),
            "state":             state_,
            "acres":             acres,
            "frontage_ft":       _to_float(pf.get("road_frontage") or cr.get("road_frontage") or
                                           pf.get("frontage") or cr.get("frontage")),
            "landlocked":        pf.get("land_locked") if pf.get("land_locked") is not None else cr.get("land_locked"),
            "wetlands_pct":      _to_float(pf.get("wetlands_cover_percentage") or cr.get("wetlands_cover_percentage") or
                                           pf.get("wetlands_pct") or cr.get("wetlands_pct")),
            "fema_pct":          _to_float(pf.get("fema_cover_percentage") or cr.get("fema_cover_percentage") or
                                           pf.get("fema_pct") or cr.get("fema_pct")),
            "buildable_pct":     _to_float(pf.get("buildability_total_perc") or cr.get("buildability_total_perc") or
                                           pf.get("buildable_pct") or cr.get("buildable_pct") or
                                           pf.get("slope_buildable_pct") or cr.get("slope_buildable_pct") or
                                           pf.get("buildable_percentage") or cr.get("buildable_percentage")),
            "use_code":          pf.get("landusecodedescription") or cr.get("landuse") or pf.get("landuse"),
            "last_sale_date":    (pf.get("currentsalerecordingdate") or pf.get("lastsaledate") or
                                  pf.get("last_sale_date") or cr.get("sale_date") or
                                  cr.get("currentsalerecordingdate")),
            "last_sale_amount":  _to_float(cr.get("currentsalesprice") or pf.get("currentsalesprice") or
                                           cr.get("sale_price") or pf.get("sale_price") or
                                           cr.get("lastsaleprice") or pf.get("lastsaleprice")),
            "lp_estimate":       _to_float(pf.get("tlp_estimate") or pf.get("avm_value") or
                                           pf.get("estimated_value") or pf.get("land_value_estimate") or
                                           pf.get("lp_estimate") or
                                           cr.get("total_our_estimation_values_base") or
                                           cr.get("avm_value") or cr.get("estimated_value") or
                                           cr.get("estimation_value")),
            "assessed_value":    _to_float(pf.get("assdtotalvalue") or pf.get("markettotalvalue") or
                                           pf.get("assessedvalue") or pf.get("assessed_value") or
                                           pf.get("total_assessed_value") or cr.get("assessed_value") or
                                           cr.get("assdtotalvalue")),
            "annual_tax":        _to_float(pf.get("taxamt") or pf.get("tax_amount") or
                                           pf.get("annualtax") or pf.get("annual_tax") or
                                           cr.get("taxamt") or cr.get("tax_amount")),
            "mortgage_balance":  _to_float(pf.get("concurrentmtgloanamt") or pf.get("mortgage_balance") or
                                           pf.get("mtgamt") or pf.get("mortgage_amount") or
                                           cr.get("concurrentmtgloanamt") or cr.get("mortgage_balance")),
            "improvement_value": _to_float(pf.get("assdimprovementvalue")) or 0,
            "lp_property_url":   lp_url,
            # Slope / buildability detail
            "buildable_area_acres": _to_float(
                pf.get("buildability_area_acres") or cr.get("buildability_area_acres") or
                pf.get("buildable_area_acres")   or cr.get("buildable_area_acres")),
            "slope_avg_pct":     _to_float(pf.get("slope_avg") or cr.get("slope_avg") or
                                           pf.get("avg_slope") or cr.get("avg_slope")),
            "slope_flat_pct":    _to_float(pf.get("slope_flat_pct") or pf.get("flat_slope_pct") or
                                           cr.get("slope_flat_pct")),
            "slope_minimal_pct": _to_float(pf.get("slope_minimal_pct") or pf.get("minimal_slope_pct") or
                                           cr.get("slope_minimal_pct")),
            "slope_moderate_pct":_to_float(pf.get("slope_moderate_pct") or pf.get("moderate_slope_pct") or
                                           cr.get("slope_moderate_pct")),
            "slope_heavy_pct":   _to_float(pf.get("slope_heavy_pct") or pf.get("heavy_slope_pct") or
                                           cr.get("slope_heavy_pct")),
            "slope_extreme_pct": _to_float(pf.get("slope_extreme_pct") or pf.get("extreme_slope_pct") or
                                           cr.get("slope_extreme_pct")),
            # Coordinates for map image
            "lat": _to_float(pf.get("centroidlatitude") or pf.get("lat") or pf.get("latitude")),
            "lon": _to_float(pf.get("centroidlongitude") or pf.get("lon") or pf.get("longitude")),
        },
        "comp_report":         cr,
        "comps":               comps,
        "zip_avg_per_acre":    _to_float(cr.get("price_acre_county")),
        "skip_trace_phones":   [],
        "comp_report_pending": comp_report_pending,
    }
    log.info(f"Data assembled. acres={acres} lp_estimate={data['property']['lp_estimate']} buildable={data['property']['buildable_pct']}")

    # Step 6 — generate PDF
    log.info("Writing data JSON...")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        json.dump(data, tf)
        data_path = tf.name

    safe_apn = apn.replace("/", "-").replace(" ", "_")
    pdf_path  = f"/tmp/DD_{safe_apn}_{propertyid}.pdf"
    pdf_script = str(SCRIPT_DIR / "generate_pdf.py")
    log.info(f"Running PDF script: {pdf_script} --data {data_path} --output {pdf_path}")

    proc = subprocess.run(
        ["python3", pdf_script, "--data", data_path, "--output", pdf_path],
        capture_output=True, text=True, timeout=60,
    )
    log.info(f"PDF script stdout: {proc.stdout[:300]}")
    log.info(f"PDF script stderr: {proc.stderr[:300]}")
    log.info(f"PDF script returncode: {proc.returncode}")

    try:
        os.unlink(data_path)
    except Exception:
        pass

    if proc.returncode != 0:
        slack_post(channel, f"❌ PDF generation failed:\n```{proc.stderr[:500]}```")
        return

    # Step 7 — score and summarize
    log.info("Scoring parcel...")
    try:
        import sys
        sys.path.insert(0, str(SCRIPT_DIR))
        from generate_pdf import score_parcel
        scores = score_parcel(data["property"], data["comps"], data.get("zip_avg_per_acre"))
        log.info(f"Scores: {scores.get('land_score')} stance={scores.get('stance')}")
    except Exception as e:
        log.error(f"Scoring error: {e}\n{traceback.format_exc()}")
        scores = {}

    stance   = scores.get("stance", "UNKNOWN")
    sc       = scores.get("land_score", "?")
    ev_low   = scores.get("ev_low")
    ev_high  = scores.get("ev_high")
    offer    = scores.get("offer", {})
    emoji    = {"PURSUE": "✅", "PURSUE WITH CAUTION": "⚠️", "PASS": "🔴"}.get(stance, "❓")
    ev_str   = f"${ev_low:,.0f}–${ev_high:,.0f}" if ev_low else "N/A"
    offer_lo = offer.get("low")
    offer_hi = offer.get("high")
    offer_st = offer.get("strategy", "N/A")
    offer_str = f"{offer_st}: ${offer_lo:,.0f}–${offer_hi:,.0f}" if offer_lo else offer_st
    flags_str = "\n".join(f"  • {f}" for f in scores.get("red_flags", [])[:2]) or "  None"

    summary = (
        f"{emoji} *Land Score {sc}/100 — {stance}*\n"
        f"*{address}*  |  Owner: {owner}  |  APN: {apn}\n"
        f"*EV:* {ev_str}  |  *Offer:* {offer_str}\n"
        f"*Red flags:*\n{flags_str}\n"
        f"<{lp_url}|View on LandPortal>"
    )

    # Build descriptive filename
    report_filename = make_report_filename(address, owner, apn, county, state_)
    log.info(f"Report filename: {report_filename}")

    # Upload to Google Drive
    gdrive_link = upload_to_google_drive(pdf_path, report_filename)
    if gdrive_link:
        summary += f"\n<{gdrive_link}|📁 View in Google Drive>"

    log.info("Posting summary to Slack...")
    slack_post(channel, summary)

    log.info("Uploading PDF to Slack...")
    slack_upload_pdf(channel, pdf_path, report_filename, f"DD Report — {address}")

    try:
        os.unlink(pdf_path)
    except Exception:
        pass

    log.info("run_dd completed successfully")


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.post("/slack/command")
async def slash_dd(request: Request, background_tasks: BackgroundTasks):
    body      = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if SLACK_SIGNING_SECRET and not verify_slack(body, timestamp, signature):
        return PlainTextResponse("Invalid signature", status_code=401)

    form       = await request.form()
    text       = form.get("text", "").strip()
    channel_id = form.get("channel_id", "")
    user_name  = form.get("user_name", "someone")

    if not text:
        return PlainTextResponse("Usage: `/dd [parcel# county state]`  or  `/dd [owner name state]`")

    background_tasks.add_task(run_dd, channel_id, text, user_name)
    return PlainTextResponse("⏳ Running DD — results post here in ~30 seconds.")


@app.get("/health")
async def health():
    return {"status": "ok"}
