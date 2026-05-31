#!/usr/bin/env python3
"""
Land Portal Due Diligence PDF Generator
Generates a 3-page branded PDF report for a U.S. land parcel.

Usage:
    python3 generate_pdf.py --data /tmp/lp_dd_data.json --output /path/to/report.pdf
"""

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable, KeepTogether, PageBreak
    )
    from reportlab.platypus.flowables import Flowable
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
except ImportError:
    print("Installing reportlab...")
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "reportlab", "--break-system-packages", "-q"])
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable, KeepTogether, PageBreak
    )
    from reportlab.platypus.flowables import Flowable
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

# ─── Brand Colors ─────────────────────────────────────────────────────────────
LP_GREEN     = colors.HexColor("#1B5E20")   # dark green — headers, badges
LP_GREEN_MID = colors.HexColor("#2E7D32")   # mid green — subheaders
LP_GREEN_LT  = colors.HexColor("#E8F5E9")   # light green — row fills
AMBER        = colors.HexColor("#F57F17")   # amber — caution
AMBER_LT     = colors.HexColor("#FFF8E1")
RED_DARK     = colors.HexColor("#B71C1C")   # red — pass / red flags
RED_LT       = colors.HexColor("#FFEBEE")
WHITE        = colors.white
DARK_BG      = colors.HexColor("#1A1A1A")   # DD Opinion box background
TEXT_DARK    = colors.HexColor("#212121")
TEXT_MID     = colors.HexColor("#555555")
BORDER       = colors.HexColor("#CCCCCC")


# ─── Scoring Engine ───────────────────────────────────────────────────────────

def score_parcel(p, comps, zip_avg_per_acre=None):
    """
    Returns: {
        'land_score': int,
        'stance': str,
        'auto_pass_reason': str | None,
        'factors': {name: {'score': int, 'max': int, 'note': str}},
        'tier_downgrade': bool,
        'ev': float | None,
        'ev_low': float | None,
        'ev_high': float | None,
        'offer': dict,
        'red_flags': [str],
        'green_flags': [str],
    }
    """
    red_flags = []
    green_flags = []
    factors = {}

    acres       = p.get("acres")
    frontage    = p.get("frontage_ft")
    landlocked  = p.get("landlocked")
    wetlands    = p.get("wetlands_pct")
    fema        = p.get("fema_pct")
    buildable   = p.get("buildable_pct")
    lp_estimate = p.get("lp_estimate")
    imp_value   = p.get("improvement_value", 0) or 0
    use_code    = (p.get("use_code") or "").lower()
    ann_tax     = p.get("annual_tax")
    assessed    = p.get("assessed_value")

    # ── Anomaly checks ──────────────────────────────────────────────────────
    if imp_value > 0 or any(k in use_code for k in ["resid", "commercial", "struct", "improv"]):
        red_flags.append("⚠️ IMPROVED PROPERTY — LP comps/valuations are land-only. "
                         "Land Score is misleading. Verify with residential comps or consider passing.")

    if ann_tax and assessed and assessed > 0 and ann_tax > assessed:
        red_flags.append(f"⚠️ TAX ANOMALY — Annual tax (${ann_tax:,.0f}) exceeds assessed value "
                         f"(${assessed:,.0f}). Possible delinquency or data error.")

    # ── AUTO-PASS checks ─────────────────────────────────────────────────────
    auto_pass_reason = None
    if landlocked is True:
        auto_pass_reason = "Parcel is land-locked (no road access)"
    elif frontage is not None and frontage == 0:
        auto_pass_reason = "Road frontage = 0 ft"
    elif fema is not None and fema >= 75:
        auto_pass_reason = f"FEMA flood zone ≥ 75% ({fema}%)"
    elif wetlands is not None and wetlands >= 75:
        auto_pass_reason = f"Wetlands ≥ 75% ({wetlands}%)"
    elif acres is not None and acres <= 1:
        auto_pass_reason = f"Size ≤ 1 acre ({acres} ac)"
    elif not lp_estimate and not comps:
        auto_pass_reason = "No LP valuation and no comp data available"

    if auto_pass_reason:
        red_flags.insert(0, f"🔴 AUTO-PASS: {auto_pass_reason}")
        return {
            "land_score": 0, "stance": "PASS", "auto_pass_reason": auto_pass_reason,
            "factors": {}, "tier_downgrade": False,
            "ev": None, "ev_low": None, "ev_high": None,
            "offer": {"strategy": "PASS", "low": None, "high": None, "double_close": None},
            "red_flags": red_flags, "green_flags": green_flags
        }

    # ── Factor 1: Access ──────────────────────────────────────────────────────
    if frontage is None:
        f1, f1_note = 10, "Frontage unknown (unverified)"
    elif frontage >= 500:
        f1, f1_note = 20, f"{frontage} ft"
    elif frontage >= 200:
        f1, f1_note = 15, f"{frontage} ft"
    elif frontage >= 50:
        f1, f1_note = 8, f"{frontage} ft"
    else:
        f1, f1_note = 3, f"{frontage} ft (limited)"
    factors["Access"] = {"score": f1, "max": 20, "note": f1_note}
    if f1 == 3:
        red_flags.append(f"Limited road frontage: {frontage} ft")
    elif f1 >= 15:
        green_flags.append(f"Good road frontage: {frontage} ft")

    # ── Factor 2: Wetlands ────────────────────────────────────────────────────
    if wetlands is None:
        f2, f2_note = 10, "Wetlands % unknown (unverified)"
    elif wetlands == 0:
        f2, f2_note = 18, "0% wetlands (bonus)"
        green_flags.append("No wetlands")
    elif wetlands < 10:
        f2, f2_note = 15, f"{wetlands}% wetlands"
    elif wetlands < 30:
        f2, f2_note = 10, f"{wetlands}% wetlands"
    elif wetlands < 75:
        f2, f2_note = 4, f"{wetlands}% wetlands"
        red_flags.append(f"High wetlands coverage: {wetlands}%")
    factors["Wetlands"] = {"score": f2, "max": 18, "note": f2_note}

    # ── Factor 3: FEMA ────────────────────────────────────────────────────────
    if fema is None:
        f3, f3_note = 8, "FEMA % unknown (unverified)"
    elif fema == 0:
        f3, f3_note = 15, "No flood zone exposure"
        green_flags.append("Outside FEMA flood zone")
    elif fema < 10:
        f3, f3_note = 12, f"{fema}% FEMA"
    elif fema < 30:
        f3, f3_note = 8, f"{fema}% FEMA"
    elif fema < 75:
        f3, f3_note = 3, f"{fema}% FEMA"
        red_flags.append(f"High FEMA flood zone: {fema}%")
    factors["FEMA Flood Zone"] = {"score": f3, "max": 15, "note": f3_note}

    # ── Factor 4: Slope / Buildability ────────────────────────────────────────
    if buildable is None:
        f4, f4_note = 5, "Buildable % unknown (unverified)"
    elif buildable >= 80:
        f4, f4_note = 10, f"{buildable}% buildable"
        green_flags.append(f"Highly buildable: {buildable}%")
    elif buildable >= 50:
        f4, f4_note = 7, f"{buildable}% buildable"
    elif buildable >= 25:
        f4, f4_note = 4, f"{buildable}% buildable"
        red_flags.append(f"Limited buildable area: {buildable}%")
    else:
        f4, f4_note = 1, f"{buildable}% buildable (very limited)"
        red_flags.append(f"Severely limited buildable area: {buildable}%")
    factors["Slope/Buildability"] = {"score": f4, "max": 10, "note": f4_note}

    # ── Factor 5: Valuation Gap ───────────────────────────────────────────────
    clean_comps = _get_clean_comps(comps, acres)
    comp_median_per_acre = _iqr_median(clean_comps) if clean_comps else None
    comp_median_total = (comp_median_per_acre * acres) if (comp_median_per_acre and acres) else None

    if comp_median_total and lp_estimate and lp_estimate > 0:
        gap_pct = (comp_median_total - lp_estimate) / lp_estimate * 100
        if gap_pct > 30:
            f5, f5_note = 25, f"+{gap_pct:.0f}% comp upside vs LP estimate"
            green_flags.append(f"Strong valuation upside: comps {gap_pct:.0f}% above LP estimate")
        elif gap_pct >= 10:
            f5, f5_note = 20, f"+{gap_pct:.0f}% comp upside"
            green_flags.append(f"Valuation upside: {gap_pct:.0f}%")
        elif gap_pct >= 0:
            f5, f5_note = 15, f"+{gap_pct:.0f}% vs LP estimate"
        else:
            f5, f5_note = 8, f"{gap_pct:.0f}% — comps below LP estimate"
            red_flags.append("Comps below LP estimate — LP may be overvaluing")
    elif comp_median_total or lp_estimate:
        f5, f5_note = 12, "Only one valuation source"
    else:
        f5, f5_note = 0, "No valuation data"
        red_flags.append("No valuation data available")
    factors["Valuation Gap"] = {"score": f5, "max": 25, "note": f5_note}

    # ── Factor 6: Size ────────────────────────────────────────────────────────
    if acres is None:
        f6, f6_note = 7, "Size unknown"
    elif acres >= 40:
        f6, f6_note = 15, f"{acres} ac"
        green_flags.append(f"Large parcel: {acres} ac")
    elif acres >= 20:
        f6, f6_note = 12, f"{acres} ac"
    elif acres >= 10:
        f6, f6_note = 9, f"{acres} ac"
    elif acres >= 5:
        f6, f6_note = 7, f"{acres} ac"
    elif acres >= 2:
        f6, f6_note = 5, f"{acres} ac"
    else:
        f6, f6_note = 2, f"{acres} ac (small)"
    factors["Size"] = {"score": f6, "max": 15, "note": f6_note}

    # ── Total & Tier Downgrade ────────────────────────────────────────────────
    max_possible = sum(f["max"] for f in factors.values())
    raw_score = sum(f["score"] for f in factors.values())
    land_score = round(raw_score / max_possible * 100) if max_possible > 0 else 0

    # Count lowest-tier scores
    lowest_tier_count = 0
    if f1 == 3:      lowest_tier_count += 1
    if f2 == 4:      lowest_tier_count += 1
    if f3 == 3:      lowest_tier_count += 1
    if f4 <= 1:      lowest_tier_count += 1
    if f5 == 8:      lowest_tier_count += 1
    if f6 == 2:      lowest_tier_count += 1

    tier_downgrade = lowest_tier_count >= 2

    if land_score >= 75:
        stance = "PURSUE"
    elif land_score >= 50:
        stance = "PURSUE WITH CAUTION"
    else:
        stance = "PASS"

    if tier_downgrade:
        if stance == "PURSUE":
            stance = "PURSUE WITH CAUTION"
        elif stance == "PURSUE WITH CAUTION":
            stance = "PASS"

    # ── EV Calculation ────────────────────────────────────────────────────────
    ev = _calculate_ev(lp_estimate, comp_median_total, zip_avg_per_acre, acres)

    # Comp quality flags
    if comp_median_total:
        local_count = len(clean_comps)
        if local_count < 3:
            red_flags.append(f"Sparse comp set: only {local_count} clean comps")
        if comps:
            min_dist = min(
                (c["distance_mi"] if c.get("distance_mi") is not None else 999)
                for c in comps
            )
            if min_dist > 15:
                red_flags.append(f"Distant comps — closest is {min_dist:.1f} mi away")

    # ── Offer Strategy ────────────────────────────────────────────────────────
    offer = _calc_offer(stance, ev, acres, frontage, buildable, wetlands, fema, landlocked,
                        len(clean_comps) if clean_comps else 0)

    return {
        "land_score": land_score,
        "stance": stance,
        "auto_pass_reason": None,
        "factors": factors,
        "tier_downgrade": tier_downgrade,
        "ev": ev,
        "ev_low": round(ev * 0.95) if ev else None,
        "ev_high": round(ev * 1.05) if ev else None,
        "comp_median_per_acre": comp_median_per_acre,
        "comp_median_total": comp_median_total,
        "offer": offer,
        "red_flags": red_flags,
        "green_flags": green_flags,
    }


def _get_clean_comps(comps, subject_acres):
    if not comps or not subject_acres:
        return []
    lo, hi = subject_acres * 0.5, subject_acres * 2.0
    return [c for c in comps
            if c.get("kept", True)
            and not c.get("landlocked", False)
            and c.get("acres") and lo <= c["acres"] <= hi
            and c.get("price_per_acre") and c["price_per_acre"] > 0]


def _iqr_median(comps):
    if not comps:
        return None
    vals = sorted(c["price_per_acre"] for c in comps)
    n = len(vals)
    if n == 1:
        return vals[0]
    q1 = vals[n // 4]
    q3 = vals[(3 * n) // 4]
    iqr = q3 - q1
    filtered = [v for v in vals if q1 - 1.5 * iqr <= v <= q3 + 1.5 * iqr]
    if not filtered:
        filtered = vals
    mid = len(filtered) // 2
    if len(filtered) % 2 == 0:
        return (filtered[mid - 1] + filtered[mid]) / 2
    return filtered[mid]


def _calculate_ev(lp_estimate, comp_median_total, zip_avg_per_acre, acres):
    sources = []
    weights = []
    if comp_median_total:
        sources.append(comp_median_total)
        weights.append(0.50)
    if lp_estimate:
        sources.append(lp_estimate)
        weights.append(0.30)
    if zip_avg_per_acre and acres:
        sources.append(zip_avg_per_acre * acres)
        weights.append(0.20)

    if not sources:
        return None

    # Renormalize weights
    total_w = sum(weights)
    ev = sum(s * w for s, w in zip(sources, weights)) / total_w
    return round(ev)


def _calc_offer(stance, ev, acres, frontage, buildable, wetlands, fema, landlocked, comp_count):
    if not ev or stance == "PASS":
        return {"strategy": "PASS", "low": None, "high": None, "double_close": None,
                "double_close_viable": False}

    # Check SUBDIVIDE conditions
    can_subdivide = (
        frontage and frontage >= 1000 and
        acres and acres >= 5 and
        buildable and buildable >= 50 and
        wetlands is not None and wetlands < 30 and
        fema is not None and fema < 30 and
        not landlocked and
        stance == "PURSUE"
    )

    if can_subdivide:
        # Subdivide: 55–70% of EV (highest value strategy)
        low = round(ev * 0.55 / 1000) * 1000
        high = round(ev * 0.70 / 1000) * 1000
        strategy = "SUBDIVIDE"
    elif stance == "PURSUE":
        # Straight flip: offer 30–50% of calculated market value
        low = round(ev * 0.30 / 1000) * 1000
        high = round(ev * 0.50 / 1000) * 1000
        strategy = "FLIP"
    else:
        # Cautious flip: offer 30–50% of calculated market value
        low = round(ev * 0.30 / 1000) * 1000
        high = round(ev * 0.50 / 1000) * 1000
        strategy = "FLIP CAUTIOUS"

    # Double close: end buyer pays 90% of EV (incentivizes quick sale)
    # You offer seller (90% of EV - $12k) → gross $12k → net $10k after ~$2k closing costs
    dc_end_buyer_price = round(ev * 0.90 / 1000) * 1000
    dc_seller_price    = dc_end_buyer_price - 12000
    dc_viable          = dc_seller_price > 0

    dc_reason = None
    if not dc_viable:
        dc_reason = (
            f"EV too low — 90% of market value is only ${dc_end_buyer_price:,.0f}, "
            f"less than the $12,000 needed to cover costs and net $10k profit"
        )

    return {
        "strategy":               strategy,
        "low":                    low,
        "high":                   high,
        "double_close":           dc_seller_price if dc_viable else None,
        "double_close_end_buyer": dc_end_buyer_price,
        "double_close_viable":    dc_viable,
        "double_close_reason":    dc_reason,
    }


# ─── PDF Builder ──────────────────────────────────────────────────────────────

def build_pdf(data: dict, output_path: str):
    p = data.get("property", {})
    comps = data.get("comps", [])
    zip_avg = data.get("zip_avg_per_acre")
    avg_dom = data.get("avg_dom")
    phones = data.get("skip_trace_phones", [])
    comp_report_pending = data.get("comp_report_pending", False)

    scores = score_parcel(p, comps, zip_avg)

    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.6 * inch,
    )

    story = []
    styles = _build_styles()

    # ── PAGE 1: COVER ─────────────────────────────────────────────────────────
    story.extend(_page_cover(p, scores, styles, comp_report_pending))
    story.append(PageBreak())

    # ── PAGE 2: DETAIL ────────────────────────────────────────────────────────
    story.extend(_page_detail(p, comps, scores, styles))
    story.append(PageBreak())

    # ── PAGE 3: DECISION ──────────────────────────────────────────────────────
    story.extend(_page_decision(scores, styles, phones))

    doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)
    print(f"✅ PDF saved: {output_path}")


def _build_styles():
    s = getSampleStyleSheet()
    base = dict(fontName="Helvetica", fontSize=10, leading=14, textColor=TEXT_DARK)

    return {
        "h1":       ParagraphStyle("h1",       **{**base, "fontName": "Helvetica-Bold", "fontSize": 20, "textColor": LP_GREEN, "leading": 24}),
        "h2":       ParagraphStyle("h2",       **{**base, "fontName": "Helvetica-Bold", "fontSize": 13, "textColor": LP_GREEN, "leading": 18}),
        "h3":       ParagraphStyle("h3",       **{**base, "fontName": "Helvetica-Bold", "fontSize": 11, "textColor": LP_GREEN_MID, "leading": 14}),
        "body":     ParagraphStyle("body",     **base),
        "small":    ParagraphStyle("small",    **{**base, "fontSize": 8, "textColor": TEXT_MID}),
        "label":    ParagraphStyle("label",    **{**base, "fontName": "Helvetica-Bold", "fontSize": 9, "textColor": TEXT_MID}),
        "center":   ParagraphStyle("center",   **{**base, "alignment": TA_CENTER}),
        "flag_red": ParagraphStyle("flag_red", **{**base, "textColor": RED_DARK}),
        "flag_grn": ParagraphStyle("flag_grn", **{**base, "textColor": LP_GREEN}),
        "opinion":  ParagraphStyle("opinion",  **{**base, "fontName": "Helvetica-Bold", "textColor": WHITE, "fontSize": 11, "leading": 16}),
        "op_body":  ParagraphStyle("op_body",  **{**base, "textColor": WHITE, "fontSize": 10}),
        "link":     ParagraphStyle("link",     **{**base, "textColor": LP_GREEN_MID, "fontSize": 9}),
    }


def _stance_color(stance):
    if stance == "PURSUE":       return LP_GREEN, LP_GREEN_LT
    if stance == "PURSUE WITH CAUTION": return AMBER, AMBER_LT
    return RED_DARK, RED_LT


def _fmt_money(v):
    if v is None: return "N/A"
    return f"${v:,.0f}"


def _fmt_pct(v):
    if v is None: return "Unknown"
    return f"{v:.1f}%" if isinstance(v, float) else f"{v}%"


def _fmt_date(v):
    """Format dates like 20060615 or 2006-06-15 → 06/15/2006."""
    if not v:
        return "N/A"
    s = str(v).replace("-", "").replace("/", "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[4:6]}/{s[6:8]}/{s[:4]}"
    return str(v)


def _page_cover(p, scores, styles, comp_report_pending):
    els = []
    stance = scores["stance"]
    sc = scores["land_score"]
    ev_low = scores.get("ev_low")
    ev_high = scores.get("ev_high")
    st_color, st_bg = _stance_color(stance)

    # Address
    addr = p.get("address") or f"APN {p.get('apn', 'Unknown')}"
    els.append(Paragraph(addr, styles["h1"]))
    els.append(Spacer(1, 4))
    els.append(Paragraph(
        f"{p.get('county', '')} County, {p.get('state', '')}  •  APN: {p.get('apn', 'N/A')}",
        styles["small"]
    ))
    els.append(Spacer(1, 12))
    els.append(HRFlowable(width="100%", thickness=2, color=LP_GREEN))
    els.append(Spacer(1, 12))

    # Score badge + EV side-by-side
    badge_data = [[
        Paragraph(f"<b>LAND SCORE</b>", styles["label"]),
        "",
        Paragraph(f"<b>EXPECTED VALUE</b>", styles["label"]),
    ], [
        Paragraph(f"<b>{sc}/100</b>", ParagraphStyle("sc_num",
            fontName="Helvetica-Bold", fontSize=36, textColor=st_color,
            alignment=TA_CENTER, leading=40)),
        Paragraph(f"<b>{stance}</b>", ParagraphStyle("stance",
            fontName="Helvetica-Bold", fontSize=13, textColor=st_color,
            alignment=TA_CENTER, leading=18, spaceBefore=4)),
        Paragraph(
            f"<b>{_fmt_money(ev_low)} – {_fmt_money(ev_high)}</b>" if ev_low else "<b>N/A</b>",
            ParagraphStyle("ev_num", fontName="Helvetica-Bold", fontSize=24,
                           textColor=TEXT_DARK, alignment=TA_CENTER, leading=30)
        ),
    ]]

    badge_tbl = Table(badge_data, colWidths=[2.5 * inch, 2.5 * inch, 2.5 * inch])
    badge_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), st_bg),
        ("BACKGROUND", (2, 0), (2, -1), LP_GREEN_LT),
        ("BACKGROUND", (1, 0), (1, -1), WHITE),
        ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 0), (-1, 0), [colors.HexColor("#F5F5F5")]),
        ("TOPPADDING",  (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("BOX",        (0, 0), (-1, -1), 1, BORDER),
        ("INNERGRID",  (0, 0), (-1, -1), 0.5, BORDER),
        ("SPAN",       (1, 0), (1, -1)),
    ]))
    els.append(badge_tbl)
    els.append(Spacer(1, 16))

    # Key facts strip
    acres_str   = f"{p['acres']} ac"       if p.get("acres")      else "Unknown"
    owner_str   = p.get("owner")           or "Unknown"
    use_str     = p.get("use_code")        or "N/A"
    lp_url      = p.get("lp_property_url") or ""

    facts = [["Size", "Owner", "Land Use", "APN"],
             [acres_str, owner_str, use_str, p.get("apn", "N/A")]]
    facts_tbl = Table(facts, colWidths=[1.8 * inch, 2.5 * inch, 1.8 * inch, 1.6 * inch])
    facts_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), LP_GREEN),
        ("TEXTCOLOR",     (0, 0), (-1, 0), WHITE),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [LP_GREEN_LT]),
        ("BOX",           (0, 0), (-1, -1), 1, BORDER),
        ("INNERGRID",     (0, 0), (-1, -1), 0.5, BORDER),
    ]))
    els.append(facts_tbl)
    els.append(Spacer(1, 10))

    if lp_url:
        els.append(Paragraph(f'<a href="{lp_url}" color="#2E7D32">🔗 View on Land Portal</a>', styles["link"]))
        els.append(Spacer(1, 10))

    # Satellite / aerial image
    sat_path = p.get("satellite_image_path")
    if sat_path:
        import os as _os
        if _os.path.exists(sat_path):
            try:
                from reportlab.platypus import Image as _RLImage
                sat_img = _RLImage(sat_path, width=7.65 * inch, height=4.0 * inch, kind="bound")
                els.append(Paragraph("Property Aerial View", styles["h3"]))
                els.append(Spacer(1, 4))
                els.append(sat_img)
                els.append(Spacer(1, 8))
            except Exception as _e:
                els.append(Paragraph(f"[Aerial image unavailable: {_e}]", styles["small"]))
                els.append(Spacer(1, 4))

    if comp_report_pending:
        els.append(Paragraph(
            "⚠️ Comp report still generating — EV and comp data may be incomplete.",
            ParagraphStyle("warn", fontName="Helvetica-Bold", fontSize=9, textColor=AMBER)
        ))
        els.append(Spacer(1, 8))

    # Valuation sources table
    els.append(Paragraph("Valuation Sources", styles["h3"]))
    els.append(Spacer(1, 4))
    lp_est  = p.get("lp_estimate")
    comp_med = scores.get("comp_median_total")
    zip_total = (p.get("zip_avg_per_acre") or 0) * (p.get("acres") or 0) or None

    val_data = [
        ["Source", "Weight", "Value", "Notes"],
        ["Comp Median", "50%", _fmt_money(comp_med), f"{len(scores.get('comp_median_per_acre') and [1] or [])} clean comps"],
        ["LP AVM Estimate", "30%", _fmt_money(lp_est), "LandPortal valuation"],
        ["ZIP Average", "20%", _fmt_money(zip_total) if zip_total else "N/A", "From comp report"],
        ["EV Blend (±5%)", "—", f"{_fmt_money(scores.get('ev_low'))} – {_fmt_money(scores.get('ev_high'))}", ""],
    ]
    val_tbl = Table(val_data, colWidths=[2.0 * inch, 1.0 * inch, 1.8 * inch, 2.9 * inch])
    val_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), LP_GREEN),
        ("TEXTCOLOR",     (0, 0), (-1, 0), WHITE),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME",      (0, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND",    (0, -1), (-1, -1), LP_GREEN_LT),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("ALIGN",         (1, 0), (2, -1), "CENTER"),
        ("ALIGN",         (0, 0), (0, -1), "LEFT"),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [WHITE, LP_GREEN_LT]),
        ("BOX",           (0, 0), (-1, -1), 1, BORDER),
        ("INNERGRID",     (0, 0), (-1, -1), 0.5, BORDER),
    ]))
    els.append(val_tbl)

    return els


def _page_detail(p, comps, scores, styles):
    els = []

    # ── Parcel Overview ───────────────────────────────────────────────────────
    els.append(Paragraph("Parcel Overview", styles["h2"]))
    els.append(Spacer(1, 6))

    def row(label, val): return [label, str(val) if val is not None else "N/A"]

    overview_rows = [
        row("Address",        p.get("address")),
        row("APN",            p.get("apn")),
        row("County",         p.get("county")),
        row("FIPS",           p.get("fips")),
        row("State",          p.get("state")),
        row("Size",           f"{p['acres']} acres" if p.get("acres") else None),
        row("Road Frontage",  f"{p['frontage_ft']} ft" if p.get("frontage_ft") is not None else None),
        row("Land-Locked",    "Yes ⚠️" if p.get("landlocked") else "No"),
        row("Wetlands",       _fmt_pct(p.get("wetlands_pct"))),
        row("FEMA Flood Zone",_fmt_pct(p.get("fema_pct"))),
        row("Buildable %",    _fmt_pct(p.get("buildable_pct"))),
        row("Buildable Acres", f"{p['buildable_area_acres']:.2f} ac" if p.get("buildable_area_acres") else None),
        row("Avg Slope",      f"{p['slope_avg_pct']:.1f}%" if p.get("slope_avg_pct") else None),
        row("Flat (0–5%)",    _fmt_pct(p.get("slope_flat_pct"))),
        row("Mod (5–10%)",    _fmt_pct(p.get("slope_moderate_pct"))),
        row("Heavy (10–15%)", _fmt_pct(p.get("slope_heavy_pct"))),
        row("Extreme (15%+)", _fmt_pct(p.get("slope_extreme_pct"))),
        row("Use Code",       p.get("use_code")),
        row("Last Sale Date", _fmt_date(p.get("last_sale_date"))),
        row("Last Sale Amt",  _fmt_money(p.get("last_sale_amount"))),
        row("LP AVM Estimate",_fmt_money(p.get("lp_estimate"))),
        row("Assessed Value", _fmt_money(p.get("assessed_value"))),
        row("Annual Tax",     _fmt_money(p.get("annual_tax"))),
        row("Mortgage Bal.",  _fmt_money(p.get("mortgage_balance"))),
        row("Owner",          p.get("owner")),
    ]

    # Split into 2 columns
    mid = math.ceil(len(overview_rows) / 2)
    left_rows  = overview_rows[:mid]
    right_rows = overview_rows[mid:]
    # Pad right side
    while len(right_rows) < len(left_rows):
        right_rows.append(["", ""])

    combined = [[l[0], l[1], r[0], r[1]] for l, r in zip(left_rows, right_rows)]

    ov_tbl = Table(combined, colWidths=[1.5*inch, 2.1*inch, 1.5*inch, 2.1*inch])
    ov_style = [
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("FONTNAME",      (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME",      (2, 0), (2, -1), "Helvetica-Bold"),
        ("TEXTCOLOR",     (0, 0), (0, -1), TEXT_MID),
        ("TEXTCOLOR",     (2, 0), (2, -1), TEXT_MID),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, LP_GREEN_LT]),
        ("BOX",           (0, 0), (-1, -1), 1, BORDER),
        ("INNERGRID",     (0, 0), (-1, -1), 0.5, BORDER),
        ("LINEAFTER",     (1, 0), (1, -1), 1.5, BORDER),
    ]
    ov_tbl.setStyle(TableStyle(ov_style))
    els.append(ov_tbl)
    els.append(Spacer(1, 16))

    # ── Comparable Sales ──────────────────────────────────────────────────────
    els.append(Paragraph("Comparable Sales", styles["h2"]))
    els.append(Spacer(1, 6))

    if comps:
        comp_header = ["Date", "Address", "Size (ac)", "$/ac", "Dist (mi)", "✓"]
        comp_rows   = [comp_header]
        for c in comps:
            kept = "✓" if c.get("kept", True) else "✗"
            url  = c.get("lp_url", "")
            addr = c.get("address", "N/A")
            if url:
                addr = f'<a href="{url}" color="#2E7D32">{addr}</a>'
            comp_rows.append([
                c.get("date", "N/A"),
                Paragraph(addr, ParagraphStyle("comp_addr", fontSize=8, leading=10)),
                f"{c['acres']:.1f}" if c.get("acres") else "N/A",
                f"${c['price_per_acre']:,.0f}" if c.get("price_per_acre") else "N/A",
                f"{c['distance_mi']:.1f}" if c.get("distance_mi") else "N/A",
                kept,
            ])
        comp_tbl = Table(comp_rows,
                         colWidths=[0.85*inch, 2.8*inch, 0.8*inch, 0.75*inch, 0.75*inch, 0.3*inch])
        comp_style = [
            ("BACKGROUND",    (0, 0), (-1, 0), LP_GREEN),
            ("TEXTCOLOR",     (0, 0), (-1, 0), WHITE),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, -1), 8),
            ("ALIGN",         (2, 0), (-1, -1), "CENTER"),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LP_GREEN_LT]),
            ("BOX",           (0, 0), (-1, -1), 1, BORDER),
            ("INNERGRID",     (0, 0), (-1, -1), 0.5, BORDER),
        ]
        # Color ✗ rows red
        for i, c in enumerate(comps, 1):
            if not c.get("kept", True):
                comp_style.append(("TEXTCOLOR", (5, i), (5, i), RED_DARK))
        comp_tbl.setStyle(TableStyle(comp_style))
        els.append(comp_tbl)
    else:
        els.append(Paragraph("No comp data available.", styles["body"]))

    els.append(Spacer(1, 16))

    # ── Land Score Breakdown ──────────────────────────────────────────────────
    els.append(Paragraph("Land Score Breakdown", styles["h2"]))
    els.append(Spacer(1, 6))

    score_header = ["Factor", "Score", "Max", "Bar", "Notes"]
    score_rows   = [score_header]
    for name, f in scores["factors"].items():
        sc, mx = f["score"], f["max"]
        pct  = sc / mx if mx else 0
        bar_fill   = colors.Color(0.11 + pct * 0.1, 0.37 + pct * 0.13, 0.13)
        score_rows.append([name, str(sc), str(mx), f"{'█' * round(pct * 12)}", f["note"]])

    sc_tbl = Table(score_rows,
                   colWidths=[1.6*inch, 0.55*inch, 0.45*inch, 1.3*inch, 3.8*inch])
    sc_style = [
        ("BACKGROUND",    (0, 0), (-1, 0), LP_GREEN),
        ("TEXTCOLOR",     (0, 0), (-1, 0), WHITE),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("ALIGN",         (1, 0), (2, -1), "CENTER"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LP_GREEN_LT]),
        ("BOX",           (0, 0), (-1, -1), 1, BORDER),
        ("INNERGRID",     (0, 0), (-1, -1), 0.5, BORDER),
        ("TEXTCOLOR",     (3, 1), (3, -1), LP_GREEN),
        ("FONTNAME",      (3, 1), (3, -1), "Helvetica-Bold"),
    ]
    sc_tbl.setStyle(TableStyle(sc_style))
    els.append(sc_tbl)

    # Total row
    total_score = scores["land_score"]
    stance = scores["stance"]
    st_color, _ = _stance_color(stance)
    els.append(Spacer(1, 4))
    els.append(Paragraph(
        f"<b>Total: {total_score}/100 — {stance}</b>"
        + (" (tier-downgrade applied)" if scores.get("tier_downgrade") else ""),
        ParagraphStyle("total", fontName="Helvetica-Bold", fontSize=10,
                       textColor=st_color, leading=14)
    ))

    return els


def _page_decision(scores, styles, phones):
    els = []
    stance  = scores["stance"]
    offer   = scores["offer"]
    st_color, st_bg = _stance_color(stance)

    # ── Red Flags ─────────────────────────────────────────────────────────────
    els.append(Paragraph("🔴 Red Flags", styles["h2"]))
    els.append(Spacer(1, 4))
    if scores["red_flags"]:
        for flag in scores["red_flags"]:
            els.append(Paragraph(f"• {flag}", styles["flag_red"]))
            els.append(Spacer(1, 2))
    else:
        els.append(Paragraph("None identified.", styles["body"]))
    els.append(Spacer(1, 12))

    # ── Green Flags ───────────────────────────────────────────────────────────
    els.append(Paragraph("✅ Green Flags", styles["h2"]))
    els.append(Spacer(1, 4))
    if scores["green_flags"]:
        for flag in scores["green_flags"]:
            els.append(Paragraph(f"• {flag}", styles["flag_grn"]))
            els.append(Spacer(1, 2))
    else:
        els.append(Paragraph("None identified.", styles["body"]))
    els.append(Spacer(1, 12))

    # ── Verify Before Offering Checklist ──────────────────────────────────────
    els.append(Paragraph("Verify Before Offering", styles["h2"]))
    els.append(Spacer(1, 6))
    checklist = [
        ["☐  Title search (no liens, clouds)", "☐  Zoning confirmation"],
        ["☐  Neighbor interviews",              "☐  Utilities / well / septic access"],
        ["☐  Market velocity (DOM trend)",      "☐  Seller motivation & timeline"],
    ]
    cl_tbl = Table(checklist, colWidths=[3.8 * inch, 3.9 * inch])
    cl_tbl.setStyle(TableStyle([
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, LP_GREEN_LT]),
        ("BOX",           (0, 0), (-1, -1), 1, BORDER),
        ("INNERGRID",     (0, 0), (-1, -1), 0.5, BORDER),
    ]))
    els.append(cl_tbl)
    els.append(Spacer(1, 16))

    # ── DD Agent Opinion Box ──────────────────────────────────────────────────
    els.append(Paragraph("DD Agent Opinion", styles["h2"]))
    els.append(Spacer(1, 6))

    strategy_str = offer.get("strategy", "PASS")
    if offer.get("low") and offer.get("high"):
        primary_str = f"{strategy_str} — offer ${offer['low']:,.0f} – ${offer['high']:,.0f}"
    else:
        primary_str = strategy_str

    dc_reason = offer.get("double_close_reason", "")
    dc_str = f"Not viable — {dc_reason}" if dc_reason else "Not viable"
    if offer.get("double_close_viable") and offer.get("double_close"):
        end_buyer = offer.get("double_close_end_buyer", 0)
        dc_str = (
            f"Offer seller ${offer['double_close']:,.0f} → end buyer pays ${end_buyer:,.0f} "
            f"(90% of market value) → you net ~$10,000 after closing costs"
        )

    reasoning = _build_reasoning(scores)

    opinion_data = [
        [Paragraph(f"<b>STANCE: {stance}</b>", styles["opinion"])],
        [Paragraph(f"Reasoning: {reasoning}", styles["op_body"])],
        [Paragraph(f"<b>PRIMARY STRATEGY:</b> {primary_str}", styles["opinion"])],
        [Paragraph(f"IF SELLER RESISTS: {dc_str}", styles["op_body"])],
    ]
    if phones:
        phone_lines = []
        for ph in phones:
            dnc_tag = " 🚫 DNC" if ph.get("dnc") else ""
            phone_lines.append(f"{ph['phone']} ({ph.get('line_type','?')}){dnc_tag}")
        opinion_data.append([Paragraph(
            "Owner phones: " + "  |  ".join(phone_lines), styles["op_body"]
        )])

    op_tbl = Table(opinion_data, colWidths=[7.65 * inch])
    op_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), DARK_BG),
        ("TOPPADDING",    (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING",   (0, 0), (-1, -1), 14),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 14),
        ("BOX",           (0, 0), (-1, -1), 2, st_color),
        ("LINEBELOW",     (0, 0), (-1, 0), 1, colors.HexColor("#444444")),
        ("LINEBELOW",     (0, 2), (-1, 2), 1, colors.HexColor("#444444")),
    ]))
    els.append(op_tbl)

    return els


def _build_reasoning(scores):
    parts = []
    stance = scores["stance"]
    sc     = scores["land_score"]

    if scores.get("auto_pass_reason"):
        return f"Auto-PASS: {scores['auto_pass_reason']}."

    top_factors = sorted(scores["factors"].items(),
                         key=lambda x: x[1]["score"] / x[1]["max"],
                         reverse=True)
    if top_factors:
        best  = top_factors[0]
        worst = top_factors[-1]
        parts.append(f"Strongest factor: {best[0]} ({best[1]['score']}/{best[1]['max']}). "
                     f"Weakest: {worst[0]} ({worst[1]['score']}/{worst[1]['max']}).")

    if scores.get("tier_downgrade"):
        parts.append("Tier-downgrade applied — 2+ factors in lowest tier.")

    parts.append(f"Overall Land Score: {sc}/100.")
    return " ".join(parts)


def _header_footer(canvas, doc):
    canvas.saveState()
    # Header bar
    canvas.setFillColor(LP_GREEN)
    canvas.rect(0, letter[1] - 32, letter[0], 32, fill=1, stroke=0)
    canvas.setFillColor(WHITE)
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawString(0.6 * inch, letter[1] - 20, "LAND PORTAL · DUE DILIGENCE REPORT")
    canvas.setFont("Helvetica", 9)
    canvas.drawRightString(letter[0] - 0.6 * inch, letter[1] - 20,
                           f"Generated {datetime.now().strftime('%B %d, %Y')}")
    # Footer
    canvas.setFillColor(TEXT_MID)
    canvas.setFont("Helvetica", 7)
    canvas.drawString(0.6 * inch, 0.35 * inch,
                      "For informational purposes only. Not legal, title, or investment advice. Verify all data before offering.")
    canvas.drawRightString(letter[0] - 0.6 * inch, 0.35 * inch,
                           f"Page {doc.page}")
    canvas.restoreState()


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate LandPortal DD PDF")
    parser.add_argument("--data",   required=True,  help="Path to JSON data file")
    parser.add_argument("--output", required=True,  help="Output PDF path")
    args = parser.parse_args()

    with open(args.data) as f:
        data = json.load(f)

    build_pdf(data, args.output)
