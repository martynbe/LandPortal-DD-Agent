#!/usr/bin/env python3
"""
LandPortal DD Slack Bot — /dd slash command handler
Environment variables: SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN, LANDPORTAL_JWT
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

SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
SLACK_BOT_TOKEN      = os.environ.get("SLACK_BOT_TOKEN", "")
LP_TOKEN             = os.environ.get("LANDPORTAL_JWT", "")
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
        with open(pdf_path, "rb") as f:
            r = requests.post(
                "https://slack.com/api/files.upload",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                data={"channels": channel, "filename": filename, "title": title},
                files={"file": f},
                timeout=30,
            )
        log.info(f"Slack upload response: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log.error(f"PDF upload failed: {e}")
        slack_post(channel, f"⚠️ PDF generated but upload failed: {e}")


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

    address = pf.get("situsfullstreetaddress") or prop_meta.get("address", "Unknown")
    owner   = pf.get("ownername1full") or prop_meta.get("owner", "Unknown")
    apn     = pf.get("apn") or prop_meta.get("apn", "N/A")
    county  = pf.get("situscounty") or prop_meta.get("county", "")
    state_  = pf.get("situsstate") or prop_meta.get("state", "")

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

    log.info(f"Comp report data: {str(comp_report)[:300] if comp_report else 'None'}")

    # Step 5 — assemble data
    log.info("Assembling data payload...")
    cr    = comp_report or {}
    acres = _to_float(cr.get("size") or pf.get("acreageformatted") or pf.get("acreage"))

    price_acre_mean = _to_float(cr.get("price_acre_mean"))
    comps = []
    if price_acre_mean and acres:
        comps = [{"date": str(cr.get("updated_at", ""))[:10], "address": "LandPortal comp median",
                  "acres": acres, "price_per_acre": price_acre_mean, "distance_mi": 0,
                  "landlocked": False, "kept": True, "lp_url": ""}]

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
            "frontage_ft":       _to_float(cr.get("road_frontage") or pf.get("frontage")),
            "landlocked":        cr.get("land_locked"),
            "wetlands_pct":      _to_float(cr.get("wetlands_cover_percentage")),
            "fema_pct":          _to_float(cr.get("fema_cover_percentage")),
            "buildable_pct":     None,
            "use_code":          cr.get("landuse") or pf.get("landuse"),
            "last_sale_date":    pf.get("lastsaledate"),
            "last_sale_amount":  _to_float(pf.get("lastsaleamount")),
            "lp_estimate":       _to_float(cr.get("total_our_estimation_values_base") or pf.get("lp_estimated_value")),
            "assessed_value":    _to_float(pf.get("totalassessedvalue")),
            "annual_tax":        _to_float(pf.get("annualtaxamount")),
            "mortgage_balance":  _to_float(pf.get("mortgagebalance")),
            "improvement_value": _to_float(pf.get("improvementvalue")) or 0,
            "lp_property_url":   f"https://landportal.com/property/{propertyid}",
        },
        "comp_report":         cr,
        "comps":               comps,
        "zip_avg_per_acre":    _to_float(cr.get("price_acre_county")),
        "skip_trace_phones":   [],
        "comp_report_pending": comp_report_pending,
    }
    log.info(f"Data assembled. acres={acres} lp_estimate={data['property']['lp_estimate']}")

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
        f"<https://landportal.com/property/{propertyid}|View on LandPortal>"
    )

    log.info("Posting summary to Slack...")
    slack_post(channel, summary)

    log.info("Uploading PDF...")
    slack_upload_pdf(channel, pdf_path, f"DD_{safe_apn}.pdf", f"DD Report — {address}")

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
