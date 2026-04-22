"""
ACE 340B Audit Report Generator
================================
Produces a complete, standalone HTML audit report containing:
  • Executive summary with KPIs and risk distribution
  • Compliance category breakdown
  • MEF verification summary (if MEF was uploaded)
  • Per-category deficiency sections with full claim-level corrective action plans
  • Exception claims listing
  • Store performance summary
  • Regulatory references and footer

Usage:
    from ace_340b_audit.report import generate_html_report
    html = generate_html_report(results, carve_status="carve-in",
                                workbook_name="my_workbook.xlsx", mef_loaded=True)
    Path("report.html").write_text(html, encoding="utf-8")
"""
from __future__ import annotations

import datetime
from typing import Any

import pandas as pd

from .decisions import (
    CATEGORY_COLORS,
    CATEGORY_DESCRIPTIONS,
    COMPLIANT,
    DUPLICATE_DISCOUNT,
    INELIGIBLE_PRESCRIBER,
    WRONG_SITE,
    MISSING_ENCOUNTER,
    DATA_MISMATCH,
)

# Severity order for report sections (highest risk first)
_REPORT_CATEGORY_ORDER = [
    DUPLICATE_DISCOUNT,
    INELIGIBLE_PRESCRIBER,
    WRONG_SITE,
    MISSING_ENCOUNTER,
    DATA_MISMATCH,
]

_TIER_COLORS = {
    "High":   "#d32f2f",
    "Medium": "#e65100",
    "Low":    "#2e7d32",
}

_CARVE_LABEL = {
    "carve-in":  "Carve-In — Entity uses 340B drugs for Medicaid FFS; listed on HRSA MEF",
    "carve-out": "Carve-Out — Entity purchases at WAC for Medicaid FFS; NOT on MEF",
    "unknown":   "Not set — select before acting on Duplicate Discount claims",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _h(text: Any) -> str:
    """HTML-escape a value, converting to string first."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _gv(row: Any, col: str, default: str = "N/A") -> str:
    """Safely get a string value from a Series or dict-like row."""
    try:
        v = row[col] if hasattr(row, "__getitem__") else getattr(row, col, default)
        s = str(v).strip()
        return s if s not in ("", "nan", "None", "<NA>", "NaT") else default
    except (KeyError, TypeError, AttributeError):
        return default


def _fmt_date(val: Any) -> str:
    """Format a date value to YYYY-MM-DD, returning 'N/A' on failure."""
    s = str(val)
    if s in ("NaT", "None", "nan", "N/A", ""):
        return "N/A"
    try:
        return pd.Timestamp(val).strftime("%Y-%m-%d")
    except Exception:
        return s[:10] if len(s) >= 10 else s


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(float(str(val))) if str(val) not in ("nan", "None", "N/A", "") else default
    except (ValueError, TypeError):
        return default


# ── CSS ───────────────────────────────────────────────────────────────────────

def _css() -> str:
    return """
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', Helvetica Neue, Arial, sans-serif;
    font-size: 13px;
    color: #222;
    background: #f0f2f5;
    line-height: 1.5;
  }
  .page {
    max-width: 1120px;
    margin: 0 auto;
    padding: 36px 44px 80px;
    background: #fff;
    box-shadow: 0 2px 12px rgba(0,0,0,.08);
  }
  h1  { font-size: 24px; color: #1a237e; margin-bottom: 4px; }
  h2  {
    font-size: 16px; color: #1a237e;
    border-bottom: 2px solid #e3e8f4;
    padding-bottom: 6px;
    margin: 32px 0 14px;
  }
  h3  { font-size: 14px; color: #333; }
  .header-meta { color: #555; font-size: 12px; margin: 6px 0 16px; line-height: 1.8; }
  .disclaimer {
    background: #e8eaf6; border-left: 4px solid #3949ab;
    padding: 10px 14px; font-size: 11px; color: #1a237e;
    margin-bottom: 24px; border-radius: 4px;
  }

  /* ── KPI grid ── */
  .kpi-row { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
  .kpi {
    background: #f5f7ff;
    border: 1px solid #dde3f5;
    border-radius: 8px;
    padding: 14px 18px;
    min-width: 130px;
    flex: 1;
  }
  .kpi .lbl { font-size: 10px; color: #666; text-transform: uppercase; letter-spacing: .6px; }
  .kpi .val { font-size: 24px; font-weight: 700; color: #1a237e; }
  .kpi .sub { font-size: 10px; color: #888; margin-top: 2px; }
  .kpi.green { border-color: #a5d6a7; background: #f1f8f1; }
  .kpi.green .val { color: #2e7d32; }
  .kpi.red   { border-color: #ef9a9a; background: #fff5f5; }
  .kpi.red   .val { color: #c62828; }
  .kpi.amber { border-color: #ffe082; background: #fffde7; }
  .kpi.amber .val { color: #e65100; }

  /* ── Badges ── */
  .badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    color: #fff;
    font-size: 11px;
    font-weight: 700;
    white-space: nowrap;
  }
  .tier-high   { background: #d32f2f; }
  .tier-medium { background: #e65100; }
  .tier-low    { background: #2e7d32; }

  /* ── Tables ── */
  table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 10px; }
  th {
    background: #1a237e;
    color: #fff;
    padding: 8px 10px;
    text-align: left;
    font-weight: 600;
    font-size: 11px;
  }
  td { padding: 7px 10px; border-bottom: 1px solid #eaeff7; vertical-align: top; }
  tr:nth-child(even) td { background: #f8f9fd; }
  tr:hover td { background: #eef2fb; }
  .store-tbl th { background: #37474f; }
  .exc-tbl  th { background: #4a148c; }

  /* ── Category sections ── */
  .cat-section {
    border: 1px solid #dde3f5;
    border-radius: 8px;
    overflow: hidden;
    margin-bottom: 20px;
  }
  .cat-header {
    padding: 12px 18px;
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .cat-header .count {
    font-size: 14px;
    font-weight: 700;
    background: rgba(0,0,0,.25);
    padding: 2px 10px;
    border-radius: 10px;
  }
  .cat-desc {
    background: #fafbfe;
    padding: 9px 18px;
    font-size: 11px;
    color: #555;
    border-bottom: 1px solid #eee;
  }

  /* ── Claim cards ── */
  .claim-card {
    padding: 12px 18px;
    border-bottom: 1px solid #eef0f7;
  }
  .claim-card:last-child { border-bottom: none; }
  .claim-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 20px;
    margin-bottom: 6px;
    font-size: 12px;
  }
  .claim-meta .field { color: #888; }
  .claim-meta .val   { color: #222; font-weight: 600; }

  /* ── Collapsible action plans ── */
  details { margin-top: 7px; }
  details > summary {
    cursor: pointer;
    font-size: 12px;
    font-weight: 700;
    color: #1565c0;
    list-style: none;
    padding: 5px 0;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  details > summary::-webkit-details-marker { display: none; }
  details > summary::before {
    content: '▶';
    font-size: 9px;
    transition: transform .2s;
  }
  details[open] > summary::before { content: '▼'; }
  details > summary:hover { text-decoration: underline; }
  pre.action-plan {
    margin: 8px 0 0;
    background: #f4f6fb;
    border: 1px solid #dde3f5;
    border-radius: 6px;
    padding: 12px 14px;
    font-family: 'Cascadia Code', 'Courier New', monospace;
    font-size: 11px;
    white-space: pre-wrap;
    word-break: break-word;
    color: #1a1a2e;
    line-height: 1.55;
  }

  /* ── Alerts ── */
  .alert-green {
    background: #e8f5e9; border-left: 4px solid #2e7d32;
    padding: 11px 16px; border-radius: 4px; margin: 14px 0;
    color: #1b5e20; font-size: 12px;
  }
  .alert-amber {
    background: #fff8e1; border-left: 4px solid #f57c00;
    padding: 11px 16px; border-radius: 4px; margin: 14px 0;
    color: #e65100; font-size: 12px;
  }
  .alert-red {
    background: #ffebee; border-left: 4px solid #c62828;
    padding: 11px 16px; border-radius: 4px; margin: 14px 0;
    color: #b71c1c; font-size: 12px;
  }
  .alert-blue {
    background: #e3f2fd; border-left: 4px solid #1565c0;
    padding: 11px 16px; border-radius: 4px; margin: 14px 0;
    color: #0d47a1; font-size: 12px;
  }

  /* ── Footer ── */
  .footer {
    margin-top: 48px;
    padding-top: 16px;
    border-top: 1px solid #dde3f5;
    font-size: 10px;
    color: #999;
    line-height: 1.8;
  }

  /* ── Print ── */
  @media print {
    body  { background: #fff; }
    .page { box-shadow: none; padding: 0; max-width: 100%; }
    details { display: block; }
    details > summary { display: none; }
    pre.action-plan { break-inside: avoid; }
    .cat-section { break-inside: avoid; }
  }
</style>
"""


# ── section builders ──────────────────────────────────────────────────────────

def _kpi(label: str, value: str, sub: str = "", css_class: str = "") -> str:
    cls = f'kpi {css_class}'.strip()
    return (
        f'<div class="{cls}">'
        f'<div class="lbl">{_h(label)}</div>'
        f'<div class="val">{_h(value)}</div>'
        f'<div class="sub">{_h(sub)}</div>'
        f'</div>'
    )


def _cat_badge(cat: str) -> str:
    color = CATEGORY_COLORS.get(cat, "#555")
    return f'<span class="badge" style="background:{color}">{_h(cat)}</span>'


def _tier_badge(score: Any, tier: str) -> str:
    tier_class = f"tier-{tier.lower()}" if tier in _TIER_COLORS else ""
    score_val = _safe_int(score, -1)
    score_str = str(score_val) if score_val >= 0 else "N/A"
    return f'<span class="badge {tier_class}">{score_str} / {_h(tier)}</span>'


def _category_section(cat_df: pd.DataFrame, cat: str, mef_loaded: bool) -> str:
    """Build the HTML section for a single deficiency category."""
    color = CATEGORY_COLORS.get(cat, "#555")
    desc  = CATEGORY_DESCRIPTIONS.get(cat, "")
    parts = [
        f'<div class="cat-section">',
        f'<div class="cat-header" style="background:{color}">',
        f'<h3 style="color:#fff;font-size:15px">{_h(cat)}</h3>',
        f'<span class="count">{len(cat_df):,} claims</span>',
        f'</div>',
        f'<div class="cat-desc">{_h(desc)}</div>',
    ]

    for _, row in cat_df.iterrows():
        rx         = _gv(row, "Prescription number")
        patient    = _gv(row, "Patient name")
        drug       = _gv(row, "Drug name")
        ndc        = _gv(row, "NDC")
        store      = _gv(row, "Store number")
        fill_dt    = _fmt_date(row["Fill date"] if "Fill date" in row.index else "N/A")
        prescriber = _gv(row, "Prescribing provider")
        score      = row["Risk score"] if "Risk score" in row.index else "N/A"
        tier       = _gv(row, "Risk tier")
        plan       = _gv(row, "Action plan", "No action plan available.")
        dup_reason = _gv(row, "Duplicate reason", "")
        mef_check  = _gv(row, "MEF check", "N/A")
        mef_incon  = str(_gv(row, "MEF inconsistency", "False")).lower() in ("true", "1")
        miss_list  = _gv(row, "Missing fields list", "")

        tier_color = _TIER_COLORS.get(tier, "#555")
        score_val  = _safe_int(score, -1)
        score_str  = str(score_val) if score_val >= 0 else "N/A"

        parts.append('<div class="claim-card">')
        parts.append('<div class="claim-meta">')
        parts.append(f'<span><span class="field">Rx#</span> <span class="val">{_h(rx)}</span></span>')
        parts.append(f'<span><span class="field">Patient:</span> <span class="val">{_h(patient)}</span></span>')
        parts.append(f'<span><span class="field">Drug:</span> <span class="val">{_h(drug)}</span></span>')
        parts.append(f'<span><span class="field">NDC:</span> <span class="val">{_h(ndc)}</span></span>')
        parts.append(f'<span><span class="field">Store:</span> <span class="val">{_h(store)}</span></span>')
        parts.append(f'<span><span class="field">Fill date:</span> <span class="val">{_h(fill_dt)}</span></span>')
        parts.append(f'<span><span class="field">Prescriber:</span> <span class="val">{_h(prescriber)}</span></span>')
        parts.append(
            f'<span><span class="field">Risk:</span> '
            f'<span class="badge" style="background:{tier_color}">{_h(score_str)} / {_h(tier)}</span>'
            f'</span>'
        )
        if dup_reason and dup_reason not in ("N/A", ""):
            parts.append(f'<span><span class="field">Signal:</span> <span class="val">{_h(dup_reason)}</span></span>')
        if miss_list and miss_list not in ("N/A", ""):
            parts.append(f'<span><span class="field">Missing:</span> <span class="val" style="color:#c62828">{_h(miss_list)}</span></span>')
        if mef_loaded:
            mef_color = "#2e7d32" if mef_check == "ON_MEF" else "#c62828" if mef_check == "NOT_ON_MEF" else "#888"
            parts.append(f'<span><span class="field">MEF:</span> <span style="color:{mef_color};font-weight:700">{_h(mef_check)}</span></span>')
        if mef_incon:
            parts.append('<span style="color:#b71c1c;font-weight:700">⚠️ MEF inconsistency</span>')
        parts.append('</div>')  # claim-meta

        parts.append(
            f'<details>'
            f'<summary>View corrective action plan for Rx# {_h(rx)}</summary>'
            f'<pre class="action-plan">{_h(plan)}</pre>'
            f'</details>'
        )
        parts.append('</div>')  # claim-card

    parts.append('</div>')  # cat-section
    return "\n".join(parts)


# ── main public function ──────────────────────────────────────────────────────

def generate_html_report(
    results: dict[str, pd.DataFrame],
    carve_status: str = "unknown",
    workbook_name: str = "Unknown",
    mef_loaded: bool = False,
) -> str:
    """
    Generate a complete, standalone HTML audit report.

    Parameters
    ----------
    results       : dict returned by run_audit_from_workbook / audit_dataframe
                    Keys: "claims", "summary", "issue_summary", "store_status"
    carve_status  : "carve-in" | "carve-out" | "unknown"
    workbook_name : name of the source workbook / data file
    mef_loaded    : whether an MEF file was uploaded for cross-referencing

    Returns
    -------
    str : complete, standalone HTML document (UTF-8)
    """
    claims        = results["claims"]
    summary       = results["summary"]
    store_status  = results["store_status"]

    m = {row.metric: row.value for row in summary.itertuples(index=False)}

    run_date       = datetime.datetime.now().strftime("%B %d, %Y at %H:%M")
    carve_label    = _CARVE_LABEL.get(carve_status, carve_status)
    total_claims   = _safe_int(m.get("Total claims imported", 0))
    pass_rate      = float(m.get("Pass rate", 0.0))
    review_count   = _safe_int(m.get("REVIEW claims", 0))
    exception_count = _safe_int(m.get("EXCEPTION claims", 0))
    compliant_count = _safe_int(m.get("Compliant claims", 0))
    avg_score      = float(m.get("Average risk score", 0.0))
    high_risk      = _safe_int(m.get("High risk claims", 0))
    medium_risk    = _safe_int(m.get("Medium risk claims", 0))
    low_risk       = _safe_int(m.get("Low risk claims", 0))

    # Compute per-category counts from live data (not every category is in summary)
    cat_counts: dict[str, int] = {}
    if "Compliance category" in claims.columns:
        cat_counts = claims["Compliance category"].value_counts().to_dict()
        cat_counts = {k: int(v) for k, v in cat_counts.items()}

    html: list[str] = [
        '<!DOCTYPE html>',
        '<html lang="en">',
        '<head>',
        '<meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
        f'<title>ACE 340B Audit Report — {_h(workbook_name)}</title>',
        _css(),
        '</head>',
        '<body>',
        '<div class="page">',
    ]

    # ── report header ─────────────────────────────────────────────────────────
    carve_icon = {"carve-in": "✅", "carve-out": "🚫"}.get(carve_status, "⚠️")
    html.append(f"""
<h1>⚕️ ACE 340B Compliance Audit Report</h1>
<div class="header-meta">
  <strong>Source workbook:</strong> {_h(workbook_name)}<br>
  <strong>Report generated:</strong> {_h(run_date)}<br>
  <strong>Carve status:</strong> {carve_icon} {_h(carve_label)}<br>
  <strong>MEF verification:</strong> {"✅ Uploaded and active" if mef_loaded else "❌ Not uploaded"}
</div>
<div class="disclaimer">
  <strong>Operational disclaimer:</strong> Corrective action guidance is based on HRSA programme
  integrity rules, the Medicaid Exclusion File (MEF) framework, and CMS billing guidance
  (NDC Qualifier Code &#39;20&#39;). This report is operational in nature and does
  <strong>not</strong> constitute legal or regulatory advice. Carve-in/carve-out elections
  may differ by entity and by state. Consult your 340B TPA and legal counsel for
  entity-specific determinations.
</div>
""")

    # ── executive summary ─────────────────────────────────────────────────────
    html.append('<h2>Executive Summary</h2>')
    html.append('<div class="kpi-row">')
    html.append(_kpi("Total Claims",     f"{total_claims:,}"))
    html.append(_kpi("Pass Rate",        f"{pass_rate:.1%}",  f"{compliant_count:,} compliant claims", "green"))
    html.append(_kpi("Require Review",   f"{review_count:,}", "Claims needing corrective action", "red"))
    html.append(_kpi("Exceptions",       f"{exception_count:,}", "Manually approved overrides"))
    html.append(_kpi("Avg Risk Score",   f"{avg_score:.1f}",  "0 = critical · 100 = perfect"))
    html.append('</div>')

    html.append('<div class="kpi-row">')
    html.append(_kpi("High Risk",    f"{high_risk:,}",   "Score 0–69 (immediate action)",  "red"))
    html.append(_kpi("Medium Risk",  f"{medium_risk:,}", "Score 70–89 (action required)",  "amber"))
    html.append(_kpi("Low Risk",     f"{low_risk:,}",    "Score 90–100 (monitor)",         "green"))
    html.append('</div>')

    # ── compliance category overview ──────────────────────────────────────────
    html.append('<h2>Compliance Category Breakdown</h2>')
    html.append('<table>')
    html.append('<tr><th>Category</th><th>Claims</th><th>% of Total</th><th>Severity</th><th>Description</th></tr>')

    severity_labels = {
        DUPLICATE_DISCOUNT:    ("5 — Critical",   "#d32f2f"),
        INELIGIBLE_PRESCRIBER: ("4 — High",       "#e65100"),
        WRONG_SITE:            ("3 — High",       "#f57c00"),
        MISSING_ENCOUNTER:     ("2 — Medium",     "#fbc02d"),
        DATA_MISMATCH:         ("1 — Low",        "#1565c0"),
        COMPLIANT:             ("0 — None",       "#2e7d32"),
    }

    for cat in _REPORT_CATEGORY_ORDER:
        count = cat_counts.get(cat, _safe_int(m.get(cat, 0)))
        pct   = f"{count / total_claims:.1%}" if total_claims > 0 else "0.0%"
        color = CATEGORY_COLORS.get(cat, "#555")
        desc  = CATEGORY_DESCRIPTIONS.get(cat, "")
        desc_short = desc[:140] + ("…" if len(desc) > 140 else "")
        sev_label, sev_color = severity_labels.get(cat, ("N/A", "#555"))
        html.append(
            f'<tr>'
            f'<td><span class="badge" style="background:{color}">{_h(cat)}</span></td>'
            f'<td><strong>{count:,}</strong></td>'
            f'<td>{pct}</td>'
            f'<td><span style="color:{sev_color};font-weight:700;font-size:11px">{_h(sev_label)}</span></td>'
            f'<td style="font-size:11px;color:#555">{_h(desc_short)}</td>'
            f'</tr>'
        )

    comp_count = cat_counts.get(COMPLIANT, compliant_count)
    comp_pct   = f"{comp_count / total_claims:.1%}" if total_claims > 0 else "0.0%"
    html.append(
        f'<tr>'
        f'<td><span class="badge" style="background:#2e7d32">{_h(COMPLIANT)}</span></td>'
        f'<td><strong>{comp_count:,}</strong></td>'
        f'<td>{comp_pct}</td>'
        f'<td><span style="color:#2e7d32;font-weight:700;font-size:11px">0 — None</span></td>'
        f'<td style="font-size:11px;color:#555">Claim meets all 340B programme eligibility requirements. No action required.</td>'
        f'</tr>'
    )
    html.append('</table>')

    # ── MEF verification summary ──────────────────────────────────────────────
    if mef_loaded:
        on_mef    = _safe_int(m.get("Claims with 340B ID on MEF",     0))
        not_mef   = _safe_int(m.get("Claims with 340B ID NOT on MEF", 0))
        mef_incon = _safe_int(m.get("MEF inconsistency flags",         0))

        html.append('<h2>Medicaid Exclusion File (MEF) Verification</h2>')
        html.append('<div class="kpi-row">')
        html.append(_kpi("✅ On MEF",         f"{on_mef:,}",    "340B ID registered on HRSA MEF", "green"))
        html.append(_kpi("❌ Not on MEF",     f"{not_mef:,}",   "340B ID not found in MEF"))
        html.append(_kpi("⚠️ Inconsistency", f"{mef_incon:,}", "Carve-out entity but ON MEF", "red" if mef_incon > 0 else ""))
        html.append('</div>')

        if mef_incon > 0:
            html.append(
                f'<div class="alert-red">'
                f'<strong>⚠️ MEF Inconsistency Detected — {mef_incon:,} claim(s):</strong> '
                f'Your entity is designated as <strong>CARVE-OUT</strong> but the 340B ID '
                f'appears <strong>ON</strong> the HRSA Medicaid Exclusion File. This is '
                f'contradictory — carve-out entities must NOT be listed on the MEF. '
                f'Contact your 340B TPA and legal counsel immediately. '
                f'Verify at <a href="https://340bopais.hrsa.gov">https://340bopais.hrsa.gov</a>'
                f'</div>'
            )
        elif carve_status == "carve-in" and on_mef > 0:
            html.append(
                f'<div class="alert-green">'
                f'<strong>✅ MEF Status Consistent:</strong> {on_mef:,} claims have their '
                f'340B ID verified on the HRSA MEF, consistent with your carve-in election. '
                f'Ensure all Medicaid FFS claims carry NDC Qualifier Code \'20\' when submitted.'
                f'</div>'
            )
        elif carve_status == "carve-out" and not_mef > 0:
            html.append(
                f'<div class="alert-green">'
                f'<strong>✅ MEF Status Consistent:</strong> {not_mef:,} claims confirm '
                f'your 340B ID is NOT listed on the MEF, consistent with your carve-out election. '
                f'Ensure NO 340B-priced drugs are dispensed to Medicaid FFS patients.'
                f'</div>'
            )
    else:
        html.append(
            '<div class="alert-blue">'
            '<strong>💡 MEF Verification Not Active:</strong> Upload the HRSA Medicaid Exclusion '
            'File (MEF) CSV to activate cross-referencing of 340B IDs. This unlocks '
            'MEF-specific guidance in all Duplicate Discount corrective action plans.'
            '</div>'
        )

    # ── deficiency summary alert ───────────────────────────────────────────────
    if review_count > 0:
        html.append(
            f'<div class="alert-amber">'
            f'<strong>⚠️ {review_count:,} claims require corrective action.</strong> '
            f'The Detailed Findings section below provides claim-level corrective action plans '
            f'for every deficiency, grouped by compliance category from highest to lowest severity. '
            f'High-risk claims require immediate attention.'
            f'</div>'
        )
    else:
        html.append(
            '<div class="alert-green">'
            '<strong>✅ All claims are compliant.</strong> '
            'No corrective action required for this audit run.'
            '</div>'
        )

    # ── detailed findings ─────────────────────────────────────────────────────
    html.append('<h2>Detailed Findings &amp; Corrective Action Plans</h2>')

    deficiency_df = claims[claims["Overall status"] == "REVIEW"].copy() if "Overall status" in claims.columns else pd.DataFrame()
    if "Severity" in deficiency_df.columns:
        deficiency_df = deficiency_df.sort_values(["Severity", "Risk score"], ascending=[False, True])

    if len(deficiency_df) == 0:
        html.append('<div class="alert-green"><strong>✅ No deficiency claims found.</strong></div>')
    else:
        for cat in _REPORT_CATEGORY_ORDER:
            if "Compliance category" not in deficiency_df.columns:
                continue
            cat_df = deficiency_df[deficiency_df["Compliance category"] == cat].copy()
            if len(cat_df) == 0:
                continue
            html.append(_category_section(cat_df, cat, mef_loaded))

    # ── compliant claims note ─────────────────────────────────────────────────
    if comp_count > 0:
        html.append(
            f'<div class="alert-green">'
            f'<strong>✅ {comp_count:,} Compliant Claims</strong> — '
            f'These claims meet all 340B programme eligibility requirements. '
            f'No action is required for these claims.'
            f'</div>'
        )

    # ── exception claims ──────────────────────────────────────────────────────
    exc_df = (
        claims[claims["Overall status"] == "EXCEPTION"].copy()
        if "Overall status" in claims.columns
        else pd.DataFrame()
    )
    if len(exc_df) > 0:
        html.append(f'<h2>Exception Claims ({len(exc_df):,})</h2>')
        html.append(
            '<p style="font-size:12px;color:#555;margin-bottom:10px">'
            'These claims were manually reviewed and approved via the exceptions list. '
            'No further action is required.</p>'
        )
        html.append('<table class="exc-tbl">')
        html.append('<tr><th>Rx#</th><th>Patient</th><th>Drug</th><th>NDC</th><th>Store</th><th>Risk Score</th><th>Exception Reason</th></tr>')
        for _, row in exc_df.iterrows():
            score     = _safe_int(row.get("Risk score", 0) if hasattr(row, "get") else 0, 0)
            tier      = _gv(row, "Risk tier")
            tier_color = _TIER_COLORS.get(tier, "#555")
            html.append(
                f'<tr>'
                f'<td>{_h(_gv(row, "Prescription number"))}</td>'
                f'<td>{_h(_gv(row, "Patient name"))}</td>'
                f'<td>{_h(_gv(row, "Drug name"))}</td>'
                f'<td>{_h(_gv(row, "NDC"))}</td>'
                f'<td>{_h(_gv(row, "Store number"))}</td>'
                f'<td><span class="badge" style="background:{tier_color}">{score} / {_h(tier)}</span></td>'
                f'<td>{_h(_gv(row, "Exception reason"))}</td>'
                f'</tr>'
            )
        html.append('</table>')

    # ── store performance ─────────────────────────────────────────────────────
    html.append('<h2>Store Performance Summary</h2>')
    html.append('<table class="store-tbl">')
    html.append(
        '<tr>'
        '<th>Store #</th><th>Pharmacy</th><th>Site</th>'
        '<th>Scripts</th><th>Review Claims</th><th>Review Rate</th><th>Avg Risk Score</th>'
        '</tr>'
    )
    for _, row in store_status.iterrows():
        review_rate = float(row.get("review_rate", 0) if hasattr(row, "get") else 0)
        avg_rs      = float(row.get("avg_risk_score", 0) if hasattr(row, "get") else 0)
        scripts     = _safe_int(row.get("scripts", 0) if hasattr(row, "get") else 0)
        rev_claims  = _safe_int(row.get("review_claims", 0) if hasattr(row, "get") else 0)
        rate_color  = (
            "#d32f2f" if review_rate >= 0.3
            else "#e65100" if review_rate >= 0.1
            else "#2e7d32"
        )
        html.append(
            f'<tr>'
            f'<td><strong>{_h(_gv(row, "Store number"))}</strong></td>'
            f'<td>{_h(_gv(row, "Pharmacy location"))}</td>'
            f'<td>{_h(_gv(row, "Site location"))}</td>'
            f'<td>{scripts:,}</td>'
            f'<td>{rev_claims:,}</td>'
            f'<td style="color:{rate_color};font-weight:700">{review_rate:.1%}</td>'
            f'<td>{avg_rs:.1f}</td>'
            f'</tr>'
        )
    html.append('</table>')

    # ── regulatory references ─────────────────────────────────────────────────
    html.append("""
<h2>Regulatory References</h2>
<ul style="font-size:12px;color:#444;line-height:2;padding-left:20px">
  <li><strong>42 U.S.C. § 256b(a)(5)(A)(i)</strong> — Statutory prohibition on duplicate discounts
      under the 340B Drug Pricing Programme.</li>
  <li><strong>HRSA 340B Programme Integrity Manual</strong> — Patient eligibility, prescriber
      eligibility, and covered entity requirements.</li>
  <li><strong>HRSA Medicaid Exclusion File (MEF)</strong> — Required registration for carve-in
      entities so state Medicaid agencies do not request manufacturer rebates.
      Verify at <a href="https://340bopais.hrsa.gov">https://340bopais.hrsa.gov</a></li>
  <li><strong>HRSA OPAIS</strong> — 340B covered entity database. Verify registration, pharmacy
      contracts, and MEF status at <a href="https://340bopais.hrsa.gov">https://340bopais.hrsa.gov</a></li>
  <li><strong>CMS Informational Bulletin (July 2020)</strong> — NDC Qualifier Code &#39;20&#39;
      (NCPDP Field 436-E1) for identifying 340B claims to Medicaid and suppressing rebate requests.</li>
  <li><strong>NPI Registry</strong> — Verify National Provider Identifiers at
      <a href="https://npiregistry.cms.hhs.gov">https://npiregistry.cms.hhs.gov</a></li>
</ul>
""")

    # ── footer ────────────────────────────────────────────────────────────────
    html.append(f"""
<div class="footer">
  <strong>ACE 340B Decision Engine</strong> &mdash;
  Report generated: {_h(run_date)} &mdash;
  Source: {_h(workbook_name)} &mdash;
  Carve status: {_h(carve_status)} &mdash;
  MEF loaded: {"Yes" if mef_loaded else "No"} &mdash;
  Total claims: {total_claims:,} &mdash;
  Review: {review_count:,} &mdash;
  Compliant: {compliant_count:,}<br>
  This report is for operational compliance review purposes only. It does not constitute legal
  or regulatory advice. Consult your 340B TPA and legal counsel for entity-specific determinations.
  &nbsp;&bull;&nbsp; <a href="https://340bopais.hrsa.gov">HRSA OPAIS</a>
  &nbsp;&bull;&nbsp; <a href="https://npiregistry.cms.hhs.gov">NPI Registry</a>
</div>
""")

    html.append('</div></body></html>')
    return "\n".join(html)
