#!/usr/bin/env python3
"""
LandPortal DD Slack Bot
Handles /dd slash commands from Slack, runs full due diligence, posts PDF + summary back.

Environment variables required:
  SLACK_SIGNING_SECRET   - From Slack App > Basic Information
  SLACK_BOT_TOKEN        - Starts with xoxb-, from OAuth & Permissions
  LANDPORTAL_JWT         - Your LandPortal API token
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

import requests
from fastapi import BackgroundTasks, Request
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

app = FastAPI()

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
SLACK_BOT_TOKEN      = os.environ.get("SLACK_BOT_TOKEN", "")
LP_TOKEN             = os.environ.get("LANDPORTAL_JWT", "")
LP_BASE              = "https://landportal.com/wp-json/lp-rest-api/v1"
LP_HEADERS           = {
    "Authorization": f"Bearer {LP_TOKEN}",
    "Content-Type": "application/json",
}
SCRIPT_DIR = Path(__file__).parent


# ── Slack helpers ─────────────────────────────────────────────────────────────

def verify_slack(body: bytes, timestamp: str, signature: str) -> bool:
    """Reject requests not from Slack."""
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
        basestring = f"v0:{timestamp}:{body.decode()}"
        computed = "v0=" + hmac.new(
            SLACK_SIGNING_SECRET.encode(), basestring.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(computed, signature)
    except Exception:
        return False


def slack_post(channel: str, text: str):
    """Post a plain text message to a channel."""
    requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={"channel": channel, "text": text},
        timeout=10,
    )


def slack_upload_pdf(channel: str, pdf_path: str, filename: str, title: str):
    """Upload a PDF to Slack."""
    try:
        with open(pdf_path, "rb") as f:
            requests.post(
                "https://slack.com/api/files.upload",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                data={"channels": channel, "filename": filename, "title": title},
                files={"file": f},
                timeout=30,
            )
    except Exception as e:
        slack_post(channel, f"⚠️ PDF generated but upload failed: {e}")


# ── LandPortal API helpers ────────────────────────────────────────────────────

def lp_search(query: str, search_type: str, state: str = None, fips: str = None):
    params = {"type": search_type, "query": query}
    if fips:
        params["fips"] = fips
    elif state:
        params["state"] = state
    try:
        r = requests.get(f"{LP_BASE}/search", headers=LP_HEADERS, params=params, timeout=15)
        return r.json() if r.ok else None
    except Exception:
        return None


def lp_property_data(propertyid, fips: str):
    try:
        r = requests.get(
            f"{LP_BASE}/property-data",
            headers=LP_HEADERS,
            params={"propertyid": propertyid, "fips": fips},
            timeout=15,
        )
        return r.json() if r.ok else None
    except Exception:
        return None


def lp_get_report(propertyid, fips: str):
    try:
        r = requests.get(
            f"{LP_BASE}/reports",
            headers=LP_HEADERS,
            params={"propertyid": propertyid, "fips": fips},
            timeout=15,
        )
        if r.ok:
            data = r.json()
            if data.get("success"):
                return data.get("data")
    except Exception:
        pass
    return None


def lp_post_report(propertyid, fips: str):
    try:
        r = requests.post(
            f"{LP_BASE}/reports",
            headers=LP_HEADERS,
            json={"propertyid": str(propertyid), "fips": str(fips)},
            timeout=15,
        )
        return r.json() if r.ok else None
    except Exception:
        return None


# ── Input parser ──────────────────────────────────────────────────────────────

def parse_input(text: str):
    """
    Parse /dd command text. Handles:
      20-0857.000 Scioto OH      → parcel search
      John Smith OH              → owner search
      40444727 39145             → propertyid + FIPS directly
    """
    parts = text.strip().split()
    if not parts:
        return None

    # Detect propertyid + FIPS (both all-numeric)
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return {"type": "direct", "propertyid": int(parts[0]), "fips": parts[1]}

    # State is always the last token if it's 2 letters
    state = parts[-1].upper() if len(parts[-1]) == 2 and parts[-1].isalpha() else None

    # Parcel number heuristic: first token has digits
    if any(c.isdigit() for c in parts[0]):
        query = parts[0]
        return {"type": "parcel", "query": query, "state": state}
    else:
        # Owner name (all words before state)
        query = " ".join(parts[:-1] if state else parts)
        return {"type": "owner", "query": query, "state": state}


# ── Main DD workflow ──────────────────────────────────────────────────────────

def run_dd(channel: str, text: str, user_name: str):
    """Full DD workflow — runs in FastAPI background task."""
    log.info(f"run_dd started: channel={channel} text={text} user={user_name}")
    try:
        _run_dd_inner(channel, text, user_name)
    except Exception as e:
        log.error(f"run_dd unhandled exception: {e}\n{traceback.format_exc()}")
        slack_post(channel, f"❌ Unexpected error: {e}")


def _run_dd_inner(channel: str, text: str, user_name: str):
    parsed = parse_input(text)
    if not parsed:
        slack_post(channel, "❌ Couldn't parse input. Try: `/dd 20-0857 Scioto OH` or `/dd John Smith OH`")
        return

    slack_post(channel, f"🔍 @{user_name} — running DD on *{text}* ... results in ~30 sec")
    log.info(f"Parsed input: {parsed}")

    # ── Step 1: Resolve parcel ────────────────────────────────────────────────
    propertyid, fips, prop_meta = None, None, {}

    if parsed["type"] == "direct":
        propertyid = parsed["propertyid"]
        fips       = parsed["fips"]
    else:
        search_type = "parcelnumb" if parsed["type"] == "parcel" else "owner"
        result = lp_search(parsed["query"], search_type, state=parsed.get("state"))

        if not result or not result.get("success"):
            slack_post(channel, "❌ LandPortal search failed — check the parcel or owner name.")
            return

        features = result.get("data", {}).get("features", [])
        if not features:
            slack_post(channel, f"❌ No parcels found for: *{text}*")
            return

        if len(features) > 1:
            # List top 3 and use the first
            lines = [f"  {i+1}. {f['properties'].get('address','?')} — {f['properties'].get('owner','?')}"
                     for i, f in enumerate(features[:3])]
            slack_post(channel,
                f"Found {len(features)} matches — running DD on the top result:\n" + "\n".join(lines))

        prop_meta   = features[0]["properties"]
        propertyid  = prop_meta.get("propertyid")
        fips        = prop_meta.get("fips")

    if not propertyid or not fips:
        slack_post(channel, "❌ Couldn't resolve propertyid/FIPS.")
        return

    log.info(f"Resolved: propertyid={propertyid} fips={fips}")

    # ── Step 2: Property data ─────────────────────────────────────────────────
    log.info("Fetching property data...")
    pd_resp   = lp_property_data(propertyid, fips)
    log.info(f"Property data response: {str(pd_resp)[:200]}")
    pf        = pd_resp.get("data", {}).get("property", {}) if pd_resp else {}

    address   = pf.get("situsfullstreetaddress") or prop_meta.get("address", "Unknown")
    owner     = pf.get("ownername1full") or prop_meta.get("owner", "Unknown")
    apn       = pf.get("apn") or prop_meta.get("apn", "N/A")
    county    = pf.get("situscounty") or prop_meta.get("county", "")
    state_    = pf.get("situsstate") or prop_meta.get("state", "")

    # ── Step 3: Comp report ───────────────────────────────────────────────────
    log.info("Checking comp report...")
    comp_report = lp_get_report(propertyid, fips)
    log.info(f"Comp report exists: {comp_report is not None}")
    comp_report_pending = False

    if not comp_report:
        post_r = lp_post_report(propertyid, fips)
        if post_r and post_r.get("success"):
            slack_post(channel, "📊 Comp report queued — polling for results...")
            for _ in range(12):   # poll up to 60 seconds
                time.sleep(5)
                comp_report = lp_get_report(propertyid, fips)
                if comp_report:
                    break
        if not comp_report:
            comp_report_pending = True
            slack_post(channel, "⚠️ Comp report still processing — EV will be LP estimate only.")

    # ── Step 4: Assemble data payload ─────────────────────────────────────────
    cr    = comp_report or {}
    acres = _to_float(cr.get("size") or pf.get("acreageformatted") or pf.get("acreage"))

    data = {
        "property": {
            "propertyid":       propertyid,
            "apn":              apn,
            "address":          f"{address}, {county} County, {state_}",
            "owner":            owner,
            "county":           county,
            "fips":             fips,
            "state":            state_,
            "acres":            acres,
            "frontage_ft":      _to_float(cr.get("road_frontage") or pf.get("frontage")),
            "landlocked":       cr.get("land_locked"),
            "wetlands_pct":     _to_float(cr.get("wetlands_cover_percentage")),
            "fema_pct":         _to_float(cr.get("fema_cover_percentage")),
            "buildable_pct":    None,
            "use_code":         cr.get("landuse") or pf.get("landuse"),
            "last_sale_date":   pf.get("lastsaledate"),
            "last_sale_amount": _to_float(pf.get("lastsaleamount")),
            "lp_estimate":      _to_float(cr.get("total_our_estimation_values_base") or pf.get("lp_estimated_value")),
            "assessed_value":   _to_float(pf.get("totalassessedvalue")),
            "annual_tax":       _to_float(pf.get("annualtaxamount")),
            "mortgage_balance": _to_float(pf.get("mortgagebalance")),
            "improvement_value":_to_float(pf.get("improvementvalue")) or 0,
            "lp_property_url":  f"https://landportal.com/property/{propertyid}",
        },
        "comp_report":        cr,
        "comps":              [],
        "zip_avg_per_acre":   _to_float(cr.get("price_acre_county")),
        "skip_trace_phones":  [],
        "comp_report_pending": comp_report_pending,
    }

    # Derive comp_median from comp report's price_acre_mean
    price_acre_mean = _to_float(cr.get("price_acre_mean"))
    if price_acre_mean and acres:
        data["comps"] = [{
            "date":          cr.get("updated_at", "")[:10],
            "address":       "LandPortal comp median",
            "acres":         acres,
            "price_per_acre": price_acre_mean,
            "distance_mi":   0,
            "landlocked":    False,
            "kept":          True,
            "lp_url":        "",
        }]

    # ── Step 5: Generate PDF ──────────────────────────────────────────────────
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        json.dump(data, tf)
        data_path = tf.name

    safe_apn = apn.replace("/", "-").replace(" ", "_")
    pdf_path  = f"/tmp/DD_{safe_apn}_{propertyid}.pdf"

    proc = subprocess.run(
        ["python3", str(SCRIPT_DIR / "generate_pdf.py"), "--data", data_path, "--output", pdf_path],
        capture_output=True, text=True,
    )
    os.unlink(data_path)

    if proc.returncode != 0:
        slack_post(channel, f"❌ PDF failed: {proc.stderr[:300]}")
        return

    # ── Step 6: Score summary ─────────────────────────────────────────────────
    try:
        import sys
        sys.path.insert(0, str(SCRIPT_DIR))
        from generate_pdf import score_parcel
        scores = score_parcel(data["property"], data["comps"], data.get("zip_avg_per_acre"))
    except Exception as e:
        slack_post(channel, f"⚠️ Scoring error: {e}. PDF still attached.")
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

    top_flags = scores.get("red_flags", [])[:2]
    flags_str = "\n".join(f"  • {f}" for f in top_flags) if top_flags else "  None"

    summary = (
        f"{emoji} *Land Score {sc}/100 — {stance}*\n"
        f"*{address}*  |  Owner: {owner}  |  APN: {apn}\n"
        f"*Expected Value:* {ev_str}  |  *Offer:* {offer_str}\n"
        f"*Red flags:*\n{flags_str}\n"
        f"<https://landportal.com/property/{propertyid}|View on LandPortal>"
    )

    slack_post(channel, summary)
    slack_upload_pdf(channel, pdf_path, f"DD_{safe_apn}.pdf", f"DD Report — {address}")

    try:
        os.unlink(pdf_path)
    except Exception:
        pass


def _to_float(val):
    """Safely convert a value to float, return None on failure."""
    try:
        return float(val) if val not in (None, "", False) else None
    except (TypeError, ValueError):
        return None


# ── Routes ────────────────────────────────────────────────────────────────────

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
        return PlainTextResponse(
            "Usage: `/dd [parcel# county state]`  or  `/dd [owner name state]`\n"
            "Example: `/dd 20-0857.000 Scioto OH`"
        )

    background_tasks.add_task(run_dd, channel_id, text, user_name)
    return PlainTextResponse("⏳ Running DD — results will post here in ~30 seconds.")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "landportal-dd-bot"}
