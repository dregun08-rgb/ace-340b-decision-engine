"""
ACE 340B Decision Engine — Streamlit Dashboard
"""
from __future__ import annotations

import datetime
import io
import json
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

from ace_340b_audit.engine import run_audit_from_workbook
from ace_340b_audit.rules import DEFAULT_RULES, load_rules, save_rules
from ace_340b_audit.decisions import (
    CATEGORIES, CATEGORY_COLORS, CATEGORY_DESCRIPTIONS, SEVERITY,
    COMPLIANT, MISSING_ENCOUNTER, INELIGIBLE_PRESCRIBER,
    WRONG_SITE, DATA_MISMATCH, DUPLICATE_DISCOUNT,
)
from ace_340b_audit.report import generate_html_report

st.set_page_config(page_title="ACE 340B Decision Engine", page_icon="⚕️", layout="wide")

# ── disclaimer banner ─────────────────────────────────────────────────────────
st.info(
    "⚕️  **ACE 340B Decision Engine** — Corrective action guidance is based on HRSA programme "
    "integrity rules, the Medicaid Exclusion File (MEF) framework, and CMS billing guidance "
    "(including NDC Qualifier '20'). It is operational in nature and does not constitute legal "
    "or regulatory advice. Carve-in / carve-out elections may differ by entity and by state. "
    "Consult your 340B TPA and legal counsel for entity-specific determinations.",
    icon=None,
)

st.title("ACE 340B Decision Engine")
st.caption("Claim categorisation · Risk scoring · Corrective action plans · Carve-in / Carve-out guidance")

DEFAULT_SAMPLE = Path(__file__).resolve().parent / "MAP_340B_Compliance_Analytics_System_Jan2025.xlsx"
EXCEPTIONS_TEMPLATE_COLS = ["Prescription number", "Exception reason", "Reviewed by", "Review date"]
CATEGORY_ORDER = [DUPLICATE_DISCOUNT, INELIGIBLE_PRESCRIBER, WRONG_SITE,
                  MISSING_ENCOUNTER, DATA_MISMATCH, COMPLIANT]


# ── sidebar ───────────────────────────────────────────────────────────────────

st.sidebar.header("Data inputs")
uploaded_file        = st.sidebar.file_uploader("340B workbook (.xlsx)", type=["xlsx"])
provider_master_file = st.sidebar.file_uploader(
    "Provider master CSV (HRSA-eligible NPI list)",
    type=["csv"],
    help="CSV with 'NPI' or 'Provider NPI' column. Unlocks Ineligible Prescriber detection.",
)
mef_file = st.sidebar.file_uploader(
    "Medicaid Exclusion File (MEF) CSV",
    type=["csv"],
    help=(
        "HRSA's Medicaid Exclusion File listing covered entities that use 340B drugs "
        "for Medicaid FFS patients. Required columns: '340B ID' and optionally 'State', "
        "'Active'. Download from https://340bopais.hrsa.gov or use the template below."
    ),
)
st.sidebar.download_button(
    "⬇ MEF template CSV",
    data=pd.DataFrame(columns=["340B ID", "Entity Name", "State", "Medicaid Provider NPI", "Active"]).to_csv(index=False).encode(),
    file_name="mef_template.csv",
    mime="text/csv",
    help="Fill with your entity's 340B IDs and Medicaid Provider NPIs from HRSA OPA.",
)
exceptions_file = st.sidebar.file_uploader(
    "Exceptions CSV",
    type=["csv"],
    help="CSV with 'Prescription number' column. Approved claims become EXCEPTION status.",
)
st.sidebar.download_button(
    "⬇ Exceptions template",
    data=pd.DataFrame(columns=EXCEPTIONS_TEMPLATE_COLS).to_csv(index=False).encode(),
    file_name="exceptions_template.csv",
    mime="text/csv",
)

st.sidebar.markdown("---")

# ── rules editor ──────────────────────────────────────────────────────────────
with st.sidebar.expander("⚙️ Scoring rules", expanded=False):
    current_rules = load_rules()
    scoring    = current_rules.get("scoring",    DEFAULT_RULES["scoring"])
    thresholds = current_rules.get("thresholds", DEFAULT_RULES["thresholds"])

    st.markdown("**Penalty per flag**")
    penalty_labels = {
        "missing_field_penalty":                    "Missing field (per field)",
        "invalid_npi_penalty":                      "Invalid NPI",
        "invalid_ndc_penalty":                      "Invalid NDC",
        "store_map_penalty":                        "Store mapping incomplete",
        "entity_map_penalty":                       "Entity mapping incomplete",
        "prescriber_not_in_master_penalty":         "Prescriber not in master",
        "encounter_date_out_of_window_penalty":     "Encounter date out of window",
        "duplicate_discount_penalty":               "Duplicate discount",
    }
    new_scoring: dict[str, int] = {
        key: st.number_input(label, 0, 100,
                             int(scoring.get(key, DEFAULT_RULES["scoring"][key])),
                             step=1, key=f"rule_{key}")
        for key, label in penalty_labels.items()
    }
    st.markdown("**Risk tier thresholds**")
    new_high   = st.number_input("High risk max score",   0, 100, int(thresholds.get("high_risk_max", 69)),   step=1, key="thr_high")
    new_med    = st.number_input("Medium risk max score", 0, 100, int(thresholds.get("medium_risk_max", 89)), step=1, key="thr_med")
    new_window = st.number_input("Encounter date window (days)", 1, 3650,
                                 int(thresholds.get("encounter_date_window_days", 365)), step=1, key="thr_window")

    c1, c2 = st.columns(2)
    if c1.button("Save rules"):
        save_rules({**current_rules,
                    "scoring": new_scoring,
                    "thresholds": {"high_risk_max": new_high, "medium_risk_max": new_med,
                                   "encounter_date_window_days": new_window}})
        st.success("Saved — re-run audit to apply.")
    if c2.button("Reset"):
        save_rules(DEFAULT_RULES)
        st.success("Reset to defaults.")


# ── carve status (prominent) ──────────────────────────────────────────────────

st.markdown("---")
st.subheader("🏥  Site Carve Status")
carve_cols = st.columns([2, 3])
with carve_cols[0]:
    carve_status = st.radio(
        "Select your covered entity's Medicaid FFS carve-in / carve-out election",
        options=["unknown", "carve-in", "carve-out"],
        format_func=lambda x: {
            "unknown":   "⚠️  Not set — select before acting on Duplicate Discount claims",
            "carve-in":  "✅  Carve-In — Entity uses 340B drugs for Medicaid FFS; listed on HRSA MEF",
            "carve-out": "🚫  Carve-Out — Entity purchases at WAC for Medicaid FFS; NOT on MEF",
        }[x],
        index=0,
    )
with carve_cols[1]:
    carve_help = {
        "unknown": (
            "**Select your election** to unlock specific corrective action plans for "
            "Duplicate Discount claims."
        ),
        "carve-in": (
            "**Carve-In:** Your entity uses 340B discounted drugs for Medicaid fee-for-service "
            "(FFS) patients. To comply, your **Medicaid Provider NPI must be listed on HRSA's "
            "Medicaid Exclusion File (MEF)** — this tells states not to request manufacturer "
            "rebates on drugs you dispense. When billing Medicaid, submit with "
            "**NDC Qualifier Code '20'** to identify the claim as 340B and suppress the rebate request."
        ),
        "carve-out": (
            "**Carve-Out:** Your entity does **not** use 340B drugs for Medicaid FFS patients. "
            "You purchase those drugs at WAC (standard price), are **not listed on the MEF** "
            "for those claims, and the manufacturer still receives the Medicaid rebate from the state. "
            "Any 340B-priced drug dispensed to a Medicaid FFS patient must be **reversed, repurchased "
            "at WAC, and reprocessed as a standard retail claim**."
        ),
    }
    st.info(carve_help[carve_status])
    st.caption(
        "⚠️  Carve elections are made per entity and may differ by state Medicaid programme. "
        "An entity can be carve-in for some states and carve-out for others. "
        "This setting applies globally to this audit run — consult your TPA and 340B legal counsel "
        "for entity- and state-specific elections. Verify your MEF status at "
        "https://340bopais.hrsa.gov"
    )
st.markdown("---")


# ── resolve data sources ──────────────────────────────────────────────────────

def _write_temp(data: bytes, suffix: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        return f.name


source_path: str | None = None
if uploaded_file is not None:
    source_path = _write_temp(uploaded_file.getbuffer().tobytes(), ".xlsx")
elif DEFAULT_SAMPLE.exists():
    source_path = str(DEFAULT_SAMPLE)
    st.sidebar.success("Loaded bundled MAP sample workbook")
else:
    st.warning("Upload a 340B workbook to begin.")
    st.stop()

provider_master_df: pd.DataFrame | None = None
if provider_master_file is not None:
    provider_master_df = pd.read_csv(provider_master_file)
    st.sidebar.success(f"Provider master: {len(provider_master_df):,} records")

mef_df: pd.DataFrame | None = None
if mef_file is not None:
    mef_df = pd.read_csv(mef_file)
    st.sidebar.success(f"MEF loaded: {len(mef_df):,} entities")

exceptions_df: pd.DataFrame | None = None
if exceptions_file is not None:
    exceptions_df = pd.read_csv(exceptions_file)
    st.sidebar.success(f"Exceptions: {len(exceptions_df):,} records")


# ── run audit ─────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _load_results(path, pm_json, mef_json, exc_json, rules_json, carve) -> dict:
    rules = json.loads(rules_json)
    pm    = pd.read_json(pm_json)  if pm_json  else None
    mef   = pd.read_json(mef_json) if mef_json else None
    exc   = pd.read_json(exc_json) if exc_json else None
    res   = run_audit_from_workbook(path, rules=rules, exceptions=exc,
                                    provider_master=pm, mef=mef, carve_status=carve)
    return {k: v.to_dict(orient="split") for k, v in res.items()}


def _reconstruct(cached: dict) -> dict[str, pd.DataFrame]:
    return {k: pd.DataFrame(**v) for k, v in cached.items()}


rules_json = json.dumps(load_rules())
pm_json    = provider_master_df.to_json() if provider_master_df is not None else None
mef_json   = mef_df.to_json()             if mef_df             is not None else None
exc_json   = exceptions_df.to_json()      if exceptions_df       is not None else None

with st.spinner("Running decision engine…"):
    cached  = _load_results(source_path, pm_json, mef_json, exc_json, rules_json, carve_status)

results        = _reconstruct(cached)
claims         = results["claims"]
summary        = results["summary"]
issue_summary  = results["issue_summary"]
store_status   = results["store_status"]
reviewed_claims = results["reviewed_claims"]
m = {row.metric: row.value for row in summary.itertuples(index=False)}


# ── KPI rows ──────────────────────────────────────────────────────────────────

r1 = st.columns(5)
r1[0].metric("Total claims",    f"{int(m['Total claims imported']):,}")
r1[1].metric("Pass rate",       f"{m['Pass rate']:.1%}")
r1[2].metric("REVIEW",          f"{int(m['REVIEW claims']):,}")
r1[3].metric("EXCEPTION",       f"{int(m.get('EXCEPTION claims', 0)):,}")
r1[4].metric("Avg risk score",  f"{m['Average risk score']:.1f}")

st.markdown("**Decision categories**")
r2 = st.columns(6)
cat_metrics = [
    ("Duplicate Discount",    "🚨"),
    ("Ineligible Prescriber", "⚕️"),
    ("Wrong Site",            "🏥"),
    ("Missing Encounter",     "🔍"),
    ("Data Mismatch",         "📋"),
    ("Compliant",             "✅"),
]
for col, (cat, icon) in zip(r2, cat_metrics):
    col.metric(f"{icon} {cat}", f"{int(m.get(cat, 0)):,}")

r3 = st.columns(3)
r3[0].metric("High risk",   f"{int(m['High risk claims']):,}")
r3[1].metric("Medium risk", f"{int(m['Medium risk claims']):,}")
r3[2].metric("Low risk",    f"{int(m['Low risk claims']):,}")

# MEF status row (only shown when MEF file is loaded)
if mef_df is not None:
    st.markdown("**MEF verification**")
    mef_r = st.columns(3)
    mef_r[0].metric(
        "✅ On MEF",
        f"{int(m.get('Claims with 340B ID on MEF', 0)):,}",
        help="Claims whose 340B ID was found on the uploaded MEF file.",
    )
    mef_r[1].metric(
        "❌ Not on MEF",
        f"{int(m.get('Claims with 340B ID NOT on MEF', 0)):,}",
        help="Claims whose 340B ID was NOT found on the uploaded MEF file.",
    )
    mef_r[2].metric(
        "⚠️ MEF Inconsistency",
        f"{int(m.get('MEF inconsistency flags', 0)):,}",
        delta=None,
        help="Carve-out site but 340B ID found on MEF — election and MEF registration are contradictory.",
    )
    if int(m.get("MEF inconsistency flags", 0)) > 0:
        st.warning(
            f"⚠️  **MEF Inconsistency:** {int(m.get('MEF inconsistency flags', 0)):,} claim(s) show your "
            f"entity as CARVE-OUT but its 340B ID is listed on the MEF. This is contradictory. "
            f"Contact your TPA and 340B legal counsel to reconcile your carve-status election and "
            f"MEF registration at https://340bopais.hrsa.gov"
        )
else:
    st.caption(
        "💡 **MEF verification not active.** Upload the Medicaid Exclusion File (MEF) CSV in the "
        "sidebar to verify whether your entity's 340B ID is registered — this unlocks MEF-specific "
        "guidance in all Duplicate Discount action plans."
    )

st.markdown("---")

# ── charts ─────────────────────────────────────────────────────────────────────

ch_left, ch_right = st.columns([1.2, 1])
with ch_left:
    st.subheader("Compliance category distribution")
    cat_chart = (
        issue_summary
        .set_index("Compliance category")["claims"]
        .reindex([c for c in CATEGORY_ORDER if c in issue_summary["Compliance category"].values])
    )
    st.bar_chart(cat_chart)
with ch_right:
    st.subheader("Store review rate")
    ch = store_status[["Store number", "review_rate"]].copy()
    ch["Store number"] = ch["Store number"].astype(str)
    st.bar_chart(ch.set_index("Store number"))

st.markdown("---")

# ═══════════════════════════════════════════════════════════════════════════════
# TABS
# ═══════════════════════════════════════════════════════════════════════════════

tab_queue, tab_all, tab_store, tab_exc, tab_dl = st.tabs([
    "🚦 Decision Queue",
    "📄 All Claims",
    "🏪 Store Status",
    "📋 Exception Management",
    "⬇ Downloads",
])


# ── TAB 1: Decision Queue ─────────────────────────────────────────────────────

with tab_queue:
    st.subheader("Decision Queue — prioritised by category severity")
    st.caption(
        "Claims are ordered highest-severity first (Duplicate Discount → "
        "Ineligible Prescriber → Wrong Site → Missing Encounter → Data Mismatch). "
        "Select a category to filter, then expand a row to view its full corrective action plan."
    )

    # category filter
    q_cats = st.multiselect(
        "Filter by category",
        options=[c for c in CATEGORY_ORDER if c != COMPLIANT],
        default=[c for c in CATEGORY_ORDER if c != COMPLIANT],
        key="q_cats",
    )

    queue_df = (
        reviewed_claims[reviewed_claims["Compliance category"].isin(q_cats)]
        if q_cats else reviewed_claims
    ).copy()

    # truncate action plan for table display
    QUEUE_COLS = [
        "Prescription number", "Fill date", "Drug name",
        "Prescribing provider", "Provider NPI", "Store number",
        "Compliance category", "Risk score", "Risk tier",
        "Duplicate reason", "MEF check", "MEF inconsistency", "Missing fields list",
    ]
    queue_display = queue_df[[c for c in QUEUE_COLS if c in queue_df.columns]].copy()

    st.caption(f"Showing {len(queue_display):,} claims requiring action")
    st.dataframe(queue_display, width="stretch", height=340)

    # ── full action plan viewer ────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("Full corrective action plan")

    if len(queue_df) == 0:
        st.success("No claims require action in the selected categories.")
    else:
        rx_options = queue_df["Prescription number"].astype(str).unique().tolist()
        selected_rx = st.selectbox(
            "Select Rx# to view corrective action plan",
            options=["— select —"] + rx_options,
            key="selected_rx",
        )

        if selected_rx and selected_rx != "— select —":
            row = queue_df[queue_df["Prescription number"].astype(str) == selected_rx].iloc[0]
            cat = str(row.get("Compliance category", ""))
            color = CATEGORY_COLORS.get(cat, "#555")

            c1, c2, c3 = st.columns(3)
            c1.markdown(f"**Rx#:** {row.get('Prescription number', 'N/A')}")
            c2.markdown(f"**Patient:** {row.get('Patient name', 'N/A')}")
            c3.markdown(f"**Fill date:** {str(row.get('Fill date', 'N/A'))[:10]}")
            c4, c5, c6 = st.columns(3)
            c4.markdown(f"**Drug:** {row.get('Drug name', 'N/A')}")
            c5.markdown(f"**Prescriber:** {row.get('Prescribing provider', 'N/A')}")
            c6.markdown(f"**Store:** {row.get('Store number', 'N/A')}")

            st.markdown(
                f"<div style='background:{color};color:white;padding:6px 14px;"
                f"border-radius:6px;display:inline-block;font-weight:700;"
                f"margin:8px 0'>{cat}</div>",
                unsafe_allow_html=True,
            )
            st.caption(CATEGORY_DESCRIPTIONS.get(cat, ""))

            score_col, tier_col = st.columns(2)
            score_col.metric("Risk score", int(row.get("Risk score", 0)))
            tier_col.metric("Risk tier",   str(row.get("Risk tier", "N/A")))

            st.markdown("---")
            plan = str(row.get("Action plan", "No action plan available."))
            st.code(plan, language=None)

        # ── bulk export with plans ─────────────────────────────────────────────
        if len(queue_df) > 0:
            export_df = queue_df[
                [c for c in [
                    "Prescription number", "Fill date", "Drug name",
                    "Prescribing provider", "Provider NPI", "Store number",
                    "Compliance category", "Risk score", "Risk tier",
                    "Duplicate reason", "Missing fields list", "Action plan",
                ] if c in queue_df.columns]
            ].copy()
            st.download_button(
                "⬇ Export decision queue with action plans",
                data=export_df.to_csv(index=False).encode(),
                file_name="ace_340b_decision_queue.csv",
                mime="text/csv",
            )


# ── TAB 2: All Claims ─────────────────────────────────────────────────────────

with tab_all:
    st.subheader("All claims — filterable")
    fc1, fc2, fc3, fc4 = st.columns(4)
    all_statuses  = sorted(claims["Overall status"].dropna().unique().tolist())
    all_tiers     = sorted(claims["Risk tier"].dropna().unique().tolist())
    all_cats      = sorted(claims["Compliance category"].dropna().unique().tolist())
    all_stores    = sorted(claims["Store number"].astype(str).dropna().unique().tolist())

    s_filter = fc1.multiselect("Status",   all_statuses, default=all_statuses, key="f_status")
    t_filter = fc2.multiselect("Tier",     all_tiers,    default=all_tiers,    key="f_tier")
    c_filter = fc3.multiselect("Category", all_cats,     default=all_cats,     key="f_cat")
    st_filter= fc4.multiselect("Store",    all_stores,   default=all_stores,   key="f_store")

    filtered = claims[
        claims["Overall status"].isin(s_filter)
        & claims["Risk tier"].isin(t_filter)
        & claims["Compliance category"].isin(c_filter)
        & claims["Store number"].astype(str).isin(st_filter)
    ].copy()

    DISPLAY_COLS = [
        "Prescription number", "Fill date", "Drug name", "NDC",
        "Prescribing provider", "Provider NPI", "Store number",
        "Overall status", "Compliance category", "Risk score", "Risk tier",
        "NPI check", "NDC check", "Store map", "Entity map",
        "Prescriber check", "Encounter date check", "Duplicate check",
        "Duplicate reason", "MEF check", "MEF detail", "MEF inconsistency",
        "Missing fields list", "Exception flag", "Exception reason",
    ]
    display_cols = [c for c in DISPLAY_COLS if c in filtered.columns]
    st.caption(f"{len(filtered):,} of {len(claims):,} claims")
    st.dataframe(filtered[display_cols], width="stretch", height=450)


# ── TAB 3: Store Status ───────────────────────────────────────────────────────

with tab_store:
    st.subheader("Store performance")
    st.dataframe(store_status, width="stretch", height=400)


# ── TAB 4: Exception Management ──────────────────────────────────────────────

with tab_exc:
    st.subheader("Exception management")
    if exceptions_df is not None and not exceptions_df.empty:
        st.success(f"{len(exceptions_df):,} exceptions loaded — matching REVIEW claims overridden to EXCEPTION.")
        st.dataframe(exceptions_df, width="stretch", height=200)
    else:
        st.info(
            "No exceptions file uploaded. Upload one via the sidebar to mark specific "
            "Rx numbers as reviewed/approved."
        )

    st.markdown("**Generate exceptions draft from current REVIEW claims**")
    st.caption("Fill in 'Reviewed by' and 'Review date', then re-upload as the exceptions CSV.")
    if len(reviewed_claims) > 0:
        exc_export = reviewed_claims[
            [c for c in ["Prescription number", "Compliance category", "Risk score"] if c in reviewed_claims.columns]
        ].copy()
        exc_export.columns = (
            ["Prescription number", "Exception reason", "Risk score"]
            if "Compliance category" in reviewed_claims.columns
            else exc_export.columns.tolist()
        )
        exc_export["Reviewed by"] = ""
        exc_export["Review date"]  = ""
        st.download_button(
            "⬇ Download REVIEW claims as exceptions draft",
            data=exc_export.to_csv(index=False).encode(),
            file_name="ace_340b_exceptions_draft.csv",
            mime="text/csv",
        )


# ── TAB 5: Downloads ──────────────────────────────────────────────────────────

with tab_dl:
    st.subheader("Downloads")

    def to_csv(df: pd.DataFrame) -> bytes:
        return df.to_csv(index=False).encode("utf-8")

    # ── Final Audit Report ────────────────────────────────────────────────────
    st.markdown("### 📊 Final Audit Report")
    st.caption(
        "Generates a complete, standalone HTML report containing the executive summary, "
        "compliance category breakdown, per-claim corrective action plans for all REVIEW claims, "
        "MEF verification status, store performance, and regulatory references. "
        "Open in any browser or use browser Print → Save as PDF."
    )

    # Resolve workbook display name
    _wb_name = (
        uploaded_file.name
        if uploaded_file is not None
        else DEFAULT_SAMPLE.name
        if DEFAULT_SAMPLE.exists()
        else "audit_workbook"
    )

    with st.spinner("Building final report…"):
        _report_html = generate_html_report(
            results,
            carve_status=carve_status,
            workbook_name=_wb_name,
            mef_loaded=(mef_df is not None),
        )

    _report_filename = (
        f"ace_340b_audit_report_{datetime.date.today().isoformat()}.html"
    )

    # Summary preview before download
    _review_cnt = int(m.get("REVIEW claims", 0))
    _total_cnt  = int(m.get("Total claims imported", 0))
    _avg_score  = float(m.get("Average risk score", 0.0))
    _high_cnt   = int(m.get("High risk claims", 0))

    rpt_c1, rpt_c2, rpt_c3, rpt_c4 = st.columns(4)
    rpt_c1.metric("Total claims",  f"{_total_cnt:,}")
    rpt_c2.metric("Review required", f"{_review_cnt:,}")
    rpt_c3.metric("High risk",     f"{_high_cnt:,}")
    rpt_c4.metric("Avg risk score", f"{_avg_score:.1f}")

    st.download_button(
        label="⬇  Download Final Audit Report (HTML)",
        data=_report_html.encode("utf-8"),
        file_name=_report_filename,
        mime="text/html",
        width="stretch",
        type="primary",
        help=(
            "Standalone HTML — no internet connection required. "
            "Open in Chrome, Firefox, or Safari. "
            "Use File → Print → Save as PDF for a print-ready version."
        ),
    )

    st.info(
        "📋 **Report includes:** Executive summary KPIs · Risk-tier distribution · "
        "Compliance category breakdown with severity ratings · "
        "Claim-level corrective action plans for every REVIEW claim (grouped by category, "
        "sorted highest severity first) · MEF verification summary · "
        "Store performance table · Regulatory references (42 U.S.C. § 256b, HRSA MEF, "
        "CMS NDC Qualifier '20').",
        icon=None,
    )

    st.markdown("---")

    # ── Data Exports ──────────────────────────────────────────────────────────
    st.markdown("### 📁 Data Exports")
    dl1, dl2, dl3, dl4 = st.columns(4)
    dl1.download_button(
        "Decision queue + plans",
        data=to_csv(reviewed_claims[[c for c in [
            "Prescription number", "Fill date", "Drug name",
            "Prescribing provider", "Store number",
            "Compliance category", "Risk score", "Risk tier",
            "Duplicate reason", "Missing fields list", "Action plan",
        ] if c in reviewed_claims.columns]]),
        file_name="ace_340b_decision_queue.csv",
        mime="text/csv",
    )
    dl2.download_button(
        "Issue summary CSV",
        data=to_csv(issue_summary),
        file_name="ace_340b_issue_summary.csv",
        mime="text/csv",
    )
    dl3.download_button(
        "Store status CSV",
        data=to_csv(store_status),
        file_name="ace_340b_store_status.csv",
        mime="text/csv",
    )
    dl4.download_button(
        "All claims CSV",
        data=to_csv(claims),
        file_name="ace_340b_all_claims.csv",
        mime="text/csv",
    )

    st.markdown("---")

    # ── Source code download ───────────────────────────────────────────────────
    st.markdown("### 💾 Source Code")

    _SKIP = {".venv", "__pycache__", ".pyc", "rules_config.json"}
    _PROJECT_ROOT = Path(__file__).resolve().parent

    def _build_zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(_PROJECT_ROOT.rglob("*")):
                if f.is_dir():
                    continue
                parts = f.parts
                if any(skip in parts for skip in _SKIP) or any(
                    p.endswith(".pyc") for p in parts
                ):
                    continue
                zf.write(f, f.relative_to(_PROJECT_ROOT.parent))
        return buf.getvalue()

    _zip_bytes = _build_zip()
    st.download_button(
        label="⬇  Download Full Source Code (.zip)",
        data=_zip_bytes,
        file_name="ace_340b_mvp_source.zip",
        mime="application/zip",
        help="Includes app.py, ace_340b_audit package, requirements.txt, sample workbook, and .claude/launch.json",
    )
    st.caption(
        f"📦 {len(_zip_bytes) // 1024:,} KB · "
        "Contains: app.py · ace_340b_audit/ (engine, decisions, rules, api, report) · "
        "requirements.txt · MAP sample workbook · .claude/launch.json"
    )

st.markdown("---")
st.caption("ACE 340B Decision Engine · API: http://localhost:8502 · `uvicorn ace_340b_audit.api:app --port 8502`")
