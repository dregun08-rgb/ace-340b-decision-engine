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

from ace_340b_audit.engine import run_audit_from_workbook, audit_dataframe
from ace_340b_audit.ingest import detect_rx_log, map_rx_log
from ace_340b_audit.rules import DEFAULT_RULES, load_rules, save_rules
from ace_340b_audit.decisions import (
    CATEGORIES, CATEGORY_COLORS, CATEGORY_DESCRIPTIONS, SEVERITY,
    COMPLIANT, MISSING_ENCOUNTER, INELIGIBLE_PRESCRIBER,
    WRONG_SITE, DATA_MISMATCH, DUPLICATE_DISCOUNT,
)
from ace_340b_audit.report import generate_html_report

st.set_page_config(page_title="ACE 340B Decision Engine", page_icon="⚕️", layout="wide")

# ── password gate ─────────────────────────────────────────────────────────────
def _check_password() -> bool:
    """Returns True once the correct password has been entered."""
    correct = st.secrets.get("APP_PASSWORD", "")
    if not correct:          # no secret set → open access (local dev)
        return True
    if st.session_state.get("_authenticated"):
        return True
    st.markdown("## ⚕️ ACE 340B Decision Engine")
    st.markdown("Enter the access password to continue.")
    pwd = st.text_input("Password", type="password", key="_pwd_input")
    if st.button("Enter", key="_pwd_btn"):
        if pwd == correct:
            st.session_state["_authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False

if not _check_password():
    st.stop()

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

# ── provider registry (persisted to disk) ─────────────────────────────────────
_PROVIDER_REGISTRY = Path(__file__).resolve().parent / "provider_registry.json"

def _load_registry() -> pd.DataFrame | None:
    """Load saved provider registry from disk; returns None if not present."""
    if _PROVIDER_REGISTRY.exists():
        try:
            df = pd.read_json(_PROVIDER_REGISTRY, dtype=str)
            if not df.empty and "NPI" in df.columns:
                return df
        except Exception:
            pass
    return None

def _save_registry(df: pd.DataFrame) -> None:
    """Persist provider registry to disk and clear the cached resource."""
    df.to_json(_PROVIDER_REGISTRY, orient="records")
    _cached_registry.clear()

@st.cache_resource
def _cached_registry():
    """Cache the registry across reruns so it isn't re-read every script run."""
    return {"df": _load_registry()}


# ── sidebar ───────────────────────────────────────────────────────────────────

st.sidebar.header("Data inputs")
uploaded_file = st.sidebar.file_uploader(
    "340B workbook (.xlsx) or RX log (.csv) ✳ Required",
    type=["xlsx", "csv"],
    help=(
        "• Excel workbook (.xlsx) with sheets: Raw_Data, Store_Map, Site_Entity_Map\n"
        "• OR a pharmacy RX-log CSV (columns: RXNBR, FILLDATE, DRUG NAME, NDC, "
        "RX STOREID, DR NPI). The engine will auto-map and run the audit."
    ),
)
st.sidebar.caption("All files below are optional — the audit runs without them.")

# Show registry status in sidebar
_registry_df = _cached_registry()["df"]
if _registry_df is not None and not _registry_df.empty:
    st.sidebar.success(
        f"👨‍⚕️ Provider registry: {len(_registry_df):,} providers in memory. "
        "Ineligible Prescriber check is active."
    )
else:
    st.sidebar.info("👨‍⚕️ No provider registry saved. Go to the Provider Registry tab to add your site providers.")

provider_master_file = st.sidebar.file_uploader(
    "Provider master CSV — one-time upload (optional)",
    type=["csv"],
    help=(
        "CSV with 'NPI' or 'Provider NPI' column for a one-time session upload. "
        "To persist providers across sessions, use the Provider Registry tab."
    ),
)
mef_file = st.sidebar.file_uploader(
    "Medicaid Exclusion File — MEF (optional)",
    type=["csv", "xlsx", "xls"],
    help=(
        "HRSA's Medicaid Exclusion File listing covered entities that use 340B drugs "
        "for Medicaid FFS patients. Optional — audit runs without it, but MEF cross-reference "
        "and Duplicate Discount MEF flags will show N/A. "
        "Required columns: '340B ID' and optionally 'State', 'Active'."
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
    "Exceptions CSV (optional)",
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
input_format: str = "excel"   # "excel" | "csv"
if uploaded_file is not None:
    # Persist temp file path for the lifetime of this upload so re-runs don't
    # create a new path (which would bust @st.cache_data on _load_results).
    file_id = uploaded_file.file_id
    if (st.session_state.get("_wb_file_id") != file_id
            or "_wb_format" not in st.session_state):
        wb_bytes = uploaded_file.getbuffer().tobytes()
        fname    = uploaded_file.name.lower()
        if fname.endswith(".csv"):
            suffix = ".csv"
        elif wb_bytes[:4] in (b"PK\x03\x04", b"\xd0\xcf\x11\xe0"):
            suffix = ".xlsx"
        else:
            st.error(
                "⚠️  Unrecognised file format. Upload a .xlsx workbook or a pharmacy RX-log .csv file."
            )
            st.stop()
        st.session_state["_wb_file_id"]  = file_id
        st.session_state["_wb_path"]     = _write_temp(wb_bytes, suffix)
        st.session_state["_wb_format"]   = "csv" if suffix == ".csv" else "excel"
    source_path  = st.session_state["_wb_path"]
    input_format = st.session_state.get("_wb_format", "excel")
    if input_format == "csv":
        st.sidebar.info(
            "RX-log CSV detected. Columns auto-mapped to the 340B audit engine. "
            "Store/entity mapping checks will show REVIEW until 340B registration "
            "data is available — all other checks (NPI, NDC, encounter date, "
            "duplicate discount) run on live claim data."
        )
elif DEFAULT_SAMPLE.exists():
    source_path = str(DEFAULT_SAMPLE)
    st.sidebar.success("Loaded bundled MAP sample workbook")
else:
    st.warning("Upload a 340B workbook to begin.")
    st.stop()

# Build provider_master_df: registry (persisted) + optional session upload, merged
provider_master_df: pd.DataFrame | None = _cached_registry()["df"]

if provider_master_file is not None:
    try:
        _upload_pm = pd.read_csv(provider_master_file)
        if provider_master_df is not None and not provider_master_df.empty:
            # Merge session upload on top of registry (union, deduplicated)
            provider_master_df = (
                pd.concat([provider_master_df, _upload_pm], ignore_index=True)
                .drop_duplicates(subset=["NPI"])
                .reset_index(drop=True)
            )
            st.sidebar.success(
                f"Provider master: {len(provider_master_df):,} records "
                f"(registry + {len(_upload_pm):,} from upload)"
            )
        else:
            provider_master_df = _upload_pm
            st.sidebar.success(f"Provider master: {len(provider_master_df):,} records (session upload)")
    except Exception as _e:
        st.sidebar.warning(f"Could not read provider master: {_e}")

mef_df: pd.DataFrame | None = None
if mef_file is not None:
    try:
        _mef_name = mef_file.name.lower()
        if _mef_name.endswith((".xlsx", ".xls")):
            mef_df = pd.read_excel(mef_file)
        else:
            mef_df = pd.read_csv(mef_file)
        st.sidebar.success(f"MEF loaded: {len(mef_df):,} entities")
    except Exception as _e:
        st.sidebar.warning(f"Could not read MEF file: {_e}")

exceptions_df: pd.DataFrame | None = None
if exceptions_file is not None:
    exceptions_df = pd.read_csv(exceptions_file)
    st.sidebar.success(f"Exceptions: {len(exceptions_df):,} records")


# ── run audit ─────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _load_results(path, pm_json, mef_json, exc_json, rules_json, carve, fmt="excel") -> dict:
    rules = json.loads(rules_json)

    def _rj(j):
        df = pd.read_json(j)
        return df.astype({c: object for c in df.columns})

    pm    = _rj(pm_json)  if pm_json  else None
    mef   = _rj(mef_json) if mef_json else None
    exc   = _rj(exc_json) if exc_json else None

    if fmt == "csv":
        raw_df = pd.read_csv(path, dtype=str, low_memory=False)
        # Streamlit Cloud uses PyArrow-backed dtypes by default; arithmetic on
        # bool[pyarrow] + int64 raises 'radd not supported'. Convert every
        # column to plain numpy object dtype before entering the engine.
        raw_df = raw_df.astype({c: object for c in raw_df.columns})
        if not detect_rx_log(raw_df):
            raise ValueError(
                "CSV does not match the expected RX-log format. "
                "Required columns: RXNBR, FILLDATE, DRUG NAME, NDC, RX STOREID, DR NPI."
            )
        raw_df, store_map_df, site_entity_df = map_rx_log(raw_df)
        res = audit_dataframe(
            raw=raw_df,
            store_map=store_map_df,
            site_entity_map=site_entity_df,
            provider_master=pm,
            mef=mef,
            exceptions=exc,
            rules=rules,
            carve_status=carve,
        )
    else:
        res = run_audit_from_workbook(path, rules=rules, exceptions=exc,
                                      provider_master=pm, mef=mef, carve_status=carve)
    return {k: v.to_dict(orient="split") for k, v in res.items()}


def _reconstruct(cached: dict) -> dict[str, pd.DataFrame]:
    return {k: pd.DataFrame(**v) for k, v in cached.items()}


rules_json = json.dumps(load_rules())
pm_json    = provider_master_df.to_json() if provider_master_df is not None else None
mef_json   = mef_df.to_json()             if mef_df             is not None else None
exc_json   = exceptions_df.to_json()      if exceptions_df       is not None else None

with st.spinner("Running decision engine…"):
    try:
        cached = _load_results(source_path, pm_json, mef_json, exc_json, rules_json, carve_status, input_format)
    except ValueError as _ve:
        _msg = str(_ve)
        if "not found" in _msg.lower() or "worksheet" in _msg.lower():
            st.error(
                "⚠️  **Workbook sheet not found.** Your Excel file must contain sheets named exactly: "
                "`Raw_Data`, `Store_Map`, `Site_Entity_Map`. "
                f"Detail: {_msg}"
            )
        elif "rx-log format" in _msg.lower() or "required columns" in _msg.lower():
            st.error(
                "⚠️  **CSV format not recognised.** The uploaded CSV does not match the RX-log format. "
                "Required columns: `RXNBR`, `FILLDATE`, `DRUG NAME`, `NDC`, `RX STOREID`, `DR NPI`. "
                f"Detail: {_msg}"
            )
        else:
            st.error(f"⚠️  Error reading workbook: {_msg}")
        st.stop()
    except Exception as _exc:
        st.error(f"⚠️  Unexpected error running audit: {_exc}")
        st.stop()

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

# ── Pharmacy Risk Scorecard ────────────────────────────────────────────────────

def _risk_grade(score: float) -> tuple[str, str, str]:
    """Return (grade, label, hex_color) for a 0-100 risk score."""
    if score >= 90: return "A", "Excellent",        "#27ae60"
    if score >= 80: return "B", "Good",             "#2ecc71"
    if score >= 70: return "C", "Acceptable",       "#f39c12"
    if score >= 60: return "D", "Needs Improvement","#e67e22"
    return             "F", "Critical Risk",    "#e74c3c"

_pharmacy_score = float(m.get("Average risk score", 0))
_grade, _label, _grade_color = _risk_grade(_pharmacy_score)
_bar_pct = max(2, int(_pharmacy_score))
_bar_color = _grade_color

# Breakdowns for the scorecard detail line
_n_total    = int(m.get("Total claims imported", 0))
_n_review   = int(m.get("REVIEW claims", 0))
_n_high     = int(m.get("High risk claims", 0))
_n_dd       = int(m.get("Duplicate Discount", 0))
_n_ws       = int(m.get("Wrong Site", 0))
_n_ip       = int(m.get("Ineligible Prescriber", 0))
_n_me       = int(m.get("Missing Encounter", 0))
_n_dm       = int(m.get("Data Mismatch", 0))
_review_pct = (_n_review / _n_total * 100) if _n_total else 0

_deficiency_bits = []
if _n_dd: _deficiency_bits.append(f"🚨 {_n_dd:,} Duplicate Discount")
if _n_ip: _deficiency_bits.append(f"⚕️ {_n_ip:,} Ineligible Prescriber")
if _n_ws: _deficiency_bits.append(f"🏥 {_n_ws:,} Wrong Site")
if _n_me: _deficiency_bits.append(f"🔍 {_n_me:,} Missing Encounter")
if _n_dm: _deficiency_bits.append(f"📋 {_n_dm:,} Data Mismatch")
_deficiency_str = " &nbsp;·&nbsp; ".join(_deficiency_bits) if _deficiency_bits else "No deficiencies detected"

st.markdown(
    f"""
    <div style="background:#1a2e4a;border-radius:12px;padding:22px 28px;margin:4px 0 18px 0">
      <div style="color:#8daabf;font-size:0.75em;text-transform:uppercase;
                  letter-spacing:1.5px;margin-bottom:6px">Pharmacy Compliance Risk Score</div>
      <div style="display:flex;align-items:flex-end;gap:20px;flex-wrap:wrap">
        <div>
          <span style="font-size:3.6em;font-weight:800;color:{_grade_color};line-height:1">
            {_pharmacy_score:.0f}
          </span>
          <span style="color:#8daabf;font-size:1.1em;margin-left:4px">/ 100</span>
        </div>
        <div style="margin-bottom:6px">
          <div style="font-size:1.6em;font-weight:700;color:{_grade_color}">
            Grade {_grade} &nbsp;—&nbsp; {_label}
          </div>
          <div style="color:#8daabf;font-size:0.85em">
            {_review_pct:.1f}% of claims flagged for review &nbsp;·&nbsp;
            {_n_high:,} high-risk claims
          </div>
        </div>
      </div>
      <div style="background:#0d1e30;border-radius:8px;height:14px;
                  overflow:hidden;margin:14px 0 6px 0">
        <div style="background:linear-gradient(90deg,{_grade_color}cc,{_grade_color});
                    width:{_bar_pct}%;height:100%;border-radius:8px;
                    transition:width 0.5s"></div>
      </div>
      <div style="display:flex;justify-content:space-between;
                  color:#4a6a84;font-size:0.72em;margin-bottom:12px">
        <span>0 — Critical</span><span>50</span><span>100 — Fully Compliant</span>
      </div>
      <div style="color:#8daabf;font-size:0.82em">{_deficiency_str}</div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Per-store risk scores (visible when multiple stores or always as a reference row)
_store_scores = store_status[["Store number","scripts","review_claims","avg_risk_score","review_rate"]].copy()
_store_scores["Risk grade"] = _store_scores["avg_risk_score"].apply(lambda s: _risk_grade(float(s))[0])
_store_scores["Risk label"] = _store_scores["avg_risk_score"].apply(lambda s: _risk_grade(float(s))[1])
_store_scores["avg_risk_score"] = _store_scores["avg_risk_score"].round(1)
_store_scores["review_rate"]    = (_store_scores["review_rate"] * 100).round(1).astype(str) + "%"
_store_scores = _store_scores.rename(columns={
    "scripts":        "Total claims",
    "review_claims":  "Review claims",
    "avg_risk_score": "Avg risk score",
    "review_rate":    "Review rate",
})

with st.expander("Store risk scores", expanded=(len(_store_scores) > 1)):
    st.dataframe(_store_scores, width="stretch")

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

tab_queue, tab_all, tab_store, tab_exc, tab_prov, tab_dl = st.tabs([
    "⚠️ Review Queue",
    "📄 All Claims",
    "🏪 Store Status",
    "📋 Exception Management",
    "👨‍⚕️ Provider Registry",
    "⬇ Downloads",
])


# ── TAB 1: Review Queue (bucketed by deficiency category) ─────────────────────

_BUCKET_ORDER = [c for c in CATEGORY_ORDER if c != COMPLIANT]
_BUCKET_ICONS = {
    DUPLICATE_DISCOUNT:    "🚨",
    INELIGIBLE_PRESCRIBER: "⚕️",
    WRONG_SITE:            "🏥",
    MISSING_ENCOUNTER:     "🔍",
    DATA_MISMATCH:         "📋",
}

with tab_queue:
    total_review = len(reviewed_claims)

    if total_review == 0:
        st.success("✅ All claims passed — nothing in the review queue.")
    else:
        # ── bucket summary bar ─────────────────────────────────────────────────
        st.markdown(
            f"<h3 style='margin-bottom:4px'>⚠️ Review Queue"
            f" <span style='color:#e74c3c;font-size:0.85em'>"
            f"{total_review:,} claims require evaluation</span></h3>",
            unsafe_allow_html=True,
        )
        st.caption(
            "Claims are grouped by deficiency category, ordered highest-severity first. "
            "Select a bucket to drill into its claims, then pick an Rx# to see the full corrective action plan."
        )

        # Compute per-bucket counts (only buckets that have claims)
        bucket_counts = {
            cat: int((reviewed_claims["Compliance category"] == cat).sum())
            for cat in _BUCKET_ORDER
        }
        active_buckets = [c for c in _BUCKET_ORDER if bucket_counts[c] > 0]

        # Bucket summary pills
        pill_html = "<div style='display:flex;flex-wrap:wrap;gap:10px;margin:12px 0'>"
        for cat in active_buckets:
            color = CATEGORY_COLORS.get(cat, "#555")
            icon  = _BUCKET_ICONS.get(cat, "•")
            cnt   = bucket_counts[cat]
            pill_html += (
                f"<div style='background:{color};color:white;padding:5px 14px;"
                f"border-radius:20px;font-weight:600;font-size:0.85em'>"
                f"{icon} {cat}: {cnt:,}</div>"
            )
        pill_html += "</div>"
        st.markdown(pill_html, unsafe_allow_html=True)

        # ── bucket selector ────────────────────────────────────────────────────
        bucket_labels = [
            f"{_BUCKET_ICONS.get(c,'•')} {c} ({bucket_counts[c]:,})"
            for c in active_buckets
        ]
        selected_label = st.radio(
            "Select deficiency bucket to evaluate",
            options=bucket_labels,
            horizontal=True,
            key="bucket_radio",
        )
        selected_bucket = active_buckets[bucket_labels.index(selected_label)]

        bucket_df = reviewed_claims[
            reviewed_claims["Compliance category"] == selected_bucket
        ].copy()

        # ── bucket table ───────────────────────────────────────────────────────
        bucket_color = CATEGORY_COLORS.get(selected_bucket, "#555")
        st.markdown(
            f"<div style='background:{bucket_color};color:white;padding:6px 16px;"
            f"border-radius:6px;font-weight:700;font-size:1em;margin:8px 0'>"
            f"{_BUCKET_ICONS.get(selected_bucket,'')} {selected_bucket} — "
            f"{len(bucket_df):,} claims</div>",
            unsafe_allow_html=True,
        )
        st.caption(CATEGORY_DESCRIPTIONS.get(selected_bucket, ""))

        # Bucket-level risk summary line
        _b_avg  = bucket_df["Risk score"].astype(float).mean() if len(bucket_df) else 0
        _b_grade, _b_label, _b_color = _risk_grade(_b_avg)
        _b_high = int((bucket_df["Risk tier"] == "High").sum())   if "Risk tier" in bucket_df.columns else 0
        _b_med  = int((bucket_df["Risk tier"] == "Medium").sum()) if "Risk tier" in bucket_df.columns else 0
        st.markdown(
            f"<div style='background:#f8f9fa;border:1px solid #dee2e6;"
            f"border-radius:8px;padding:10px 16px;margin:6px 0 10px 0;"
            f"display:flex;gap:32px;align-items:center'>"
            f"<div><span style='color:#666;font-size:0.78em'>Bucket avg risk score</span><br>"
            f"<span style='font-size:1.6em;font-weight:800;color:{_b_color}'>{_b_avg:.0f}</span>"
            f"<span style='color:#888;font-size:0.85em'> / 100 &nbsp; Grade {_b_grade} — {_b_label}</span></div>"
            f"<div><span style='color:#666;font-size:0.78em'>High risk</span><br>"
            f"<span style='font-size:1.3em;font-weight:700;color:#e74c3c'>{_b_high:,}</span></div>"
            f"<div><span style='color:#666;font-size:0.78em'>Medium risk</span><br>"
            f"<span style='font-size:1.3em;font-weight:700;color:#e67e22'>{_b_med:,}</span></div>"
            f"<div><span style='color:#666;font-size:0.78em'>Claims in bucket</span><br>"
            f"<span style='font-size:1.3em;font-weight:700;color:#1a2e4a'>{len(bucket_df):,}</span></div>"
            f"</div>",
            unsafe_allow_html=True,
        )

        BUCKET_COLS = [
            "Risk score", "Risk tier",
            "Prescription number", "Fill date", "Drug name",
            "Prescribing provider", "Provider NPI", "Store number",
            "Duplicate reason", "NDC check", "NPI check",
            "Encounter date check", "Missing fields list",
        ]
        bucket_display = bucket_df[
            [c for c in BUCKET_COLS if c in bucket_df.columns]
        ].copy()
        st.dataframe(bucket_display, width="stretch", height=300)

        # ── export this bucket ─────────────────────────────────────────────────
        export_bucket = bucket_df[
            [c for c in [
                "Prescription number", "Fill date", "Drug name",
                "Prescribing provider", "Provider NPI", "Store number",
                "Compliance category", "Risk score", "Risk tier",
                "Duplicate reason", "Missing fields list", "Action plan",
            ] if c in bucket_df.columns]
        ].copy()
        st.download_button(
            f"⬇ Export {selected_bucket} claims ({len(bucket_df):,})",
            data=export_bucket.to_csv(index=False).encode(),
            file_name=f"ace_340b_{selected_bucket.lower().replace(' ','_')}.csv",
            mime="text/csv",
            key="bucket_export",
        )

        # ── corrective action plan panel ───────────────────────────────────────
        st.markdown("---")
        st.subheader("Corrective action plan")
        st.caption("Pick an Rx# from this bucket to see the full remediation steps.")

        rx_options = bucket_df["Prescription number"].astype(str).unique().tolist()
        selected_rx = st.selectbox(
            "Rx# to evaluate",
            options=["— select —"] + rx_options,
            key="selected_rx",
        )

        if selected_rx and selected_rx != "— select —":
            row = bucket_df[
                bucket_df["Prescription number"].astype(str) == selected_rx
            ].iloc[0]

            c1, c2, c3 = st.columns(3)
            c1.markdown(f"**Rx#:** `{row.get('Prescription number','N/A')}`")
            c2.markdown(f"**Patient:** {row.get('Patient name','N/A')}")
            c3.markdown(f"**Fill date:** {str(row.get('Fill date','N/A'))[:10]}")
            c4, c5, c6 = st.columns(3)
            c4.markdown(f"**Drug:** {row.get('Drug name','N/A')}")
            c5.markdown(f"**Prescriber:** {row.get('Prescribing provider','N/A')}")
            c6.markdown(f"**Store:** {row.get('Store number','N/A')}")

            sc1, sc2, sc3 = st.columns(3)
            sc1.metric("Risk score", int(row.get("Risk score", 0)))
            sc2.metric("Risk tier",  str(row.get("Risk tier", "N/A")))
            sc3.metric("NPI",        str(row.get("NPI check", "N/A")))

            st.markdown("**Action plan**")
            plan = str(row.get("Action plan", "No action plan available."))
            st.code(plan, language=None)

        # ── full queue export ──────────────────────────────────────────────────
        st.markdown("---")
        full_export = reviewed_claims[
            [c for c in [
                "Prescription number", "Fill date", "Drug name",
                "Prescribing provider", "Provider NPI", "Store number",
                "Compliance category", "Risk score", "Risk tier",
                "Duplicate reason", "Missing fields list", "Action plan",
            ] if c in reviewed_claims.columns]
        ].copy()
        st.download_button(
            f"⬇ Export full review queue — all categories ({total_review:,} claims)",
            data=full_export.to_csv(index=False).encode(),
            file_name="ace_340b_full_review_queue.csv",
            mime="text/csv",
            key="full_queue_export",
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
    st.subheader("Store performance & risk scores")

    # Enrich store_status with grade columns for display
    _ss_display = store_status.copy()
    _ss_display["Risk score"] = _ss_display["avg_risk_score"].round(1)
    _ss_display["Grade"]      = _ss_display["avg_risk_score"].apply(lambda s: _risk_grade(float(s))[0])
    _ss_display["Risk level"] = _ss_display["avg_risk_score"].apply(lambda s: _risk_grade(float(s))[1])
    _ss_display["Review rate"] = (_ss_display["review_rate"] * 100).round(1).astype(str) + "%"
    _ss_display = _ss_display.rename(columns={
        "scripts":       "Total claims",
        "review_claims": "Review claims",
        "avg_risk_score": "_drop",
    }).drop(columns=["_drop", "review_rate"], errors="ignore")

    # Visual store cards (one per store)
    for _, srow in _ss_display.iterrows():
        _s_score  = float(srow.get("Risk score", 0))
        _s_grade, _s_label, _s_col = _risk_grade(_s_score)
        _s_pct    = max(2, int(_s_score))
        _s_num    = srow.get("Store number", "—")
        _s_ploc   = srow.get("Pharmacy location", "") or ""
        _s_total  = int(srow.get("Total claims", 0))
        _s_review = int(srow.get("Review claims", 0))
        _s_rrate  = srow.get("Review rate", "—")
        st.markdown(
            f"""<div style="border:1px solid #dee2e6;border-radius:10px;
                padding:16px 20px;margin:8px 0;background:#fff">
              <div style="display:flex;align-items:center;
                          justify-content:space-between;flex-wrap:wrap;gap:12px">
                <div>
                  <div style="font-size:1.05em;font-weight:700;color:#1a2e4a">
                    Store {_s_num}</div>
                  <div style="color:#888;font-size:0.82em">{_s_ploc}</div>
                </div>
                <div style="text-align:center">
                  <div style="font-size:2.2em;font-weight:800;color:{_s_col};line-height:1">
                    {_s_score:.0f}</div>
                  <div style="font-size:0.75em;color:#888">/ 100 risk score</div>
                </div>
                <div style="background:{_s_col};color:white;padding:4px 14px;
                            border-radius:20px;font-weight:700;font-size:1em">
                  Grade {_s_grade} — {_s_label}</div>
                <div style="display:flex;gap:20px">
                  <div style="text-align:center">
                    <div style="font-size:1.3em;font-weight:700;color:#1a2e4a">{_s_total:,}</div>
                    <div style="font-size:0.75em;color:#888">Total claims</div>
                  </div>
                  <div style="text-align:center">
                    <div style="font-size:1.3em;font-weight:700;color:#e74c3c">{_s_review:,}</div>
                    <div style="font-size:0.75em;color:#888">Review</div>
                  </div>
                  <div style="text-align:center">
                    <div style="font-size:1.3em;font-weight:700;color:#e67e22">{_s_rrate}</div>
                    <div style="font-size:0.75em;color:#888">Review rate</div>
                  </div>
                </div>
              </div>
              <div style="background:#eee;border-radius:6px;height:10px;
                          overflow:hidden;margin-top:12px">
                <div style="background:{_s_col};width:{_s_pct}%;height:100%;
                            border-radius:6px"></div>
              </div>
            </div>""",
            unsafe_allow_html=True,
        )

    st.markdown("---")
    st.caption("Full store data table")
    st.dataframe(_ss_display, width="stretch", height=300)


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


# ── TAB 5: Provider Registry ──────────────────────────────────────────────────

with tab_prov:
    st.subheader("Site Provider Registry")
    st.caption(
        "Upload your covered entity's eligible prescriber NPIs. "
        "Once saved, the engine remembers them across sessions and automatically flags "
        "any claim where the prescriber is not in your registry as Ineligible Prescriber."
    )

    _reg = _cached_registry()["df"]
    _reg_count = len(_reg) if _reg is not None else 0

    # ── Status card ───────────────────────────────────────────────────────────
    if _reg_count > 0:
        st.markdown(
            f"<div style='background:#e8f5e9;border:1px solid #a5d6a7;border-radius:8px;"
            f"padding:14px 20px;margin-bottom:16px'>"
            f"<span style='font-size:1.5em;font-weight:800;color:#2e7d32'>{_reg_count:,}</span>"
            f"<span style='color:#388e3c;font-size:0.95em;margin-left:8px'>"
            f"providers committed to memory — Ineligible Prescriber check is ACTIVE</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
    else:
        st.warning(
            "No providers in registry. Upload a file below and click **Commit to registry** "
            "to enable the Ineligible Prescriber check across all audit runs."
        )

    st.markdown("---")

    # ── Upload section ────────────────────────────────────────────────────────
    st.markdown("#### Add providers")
    st.caption(
        "Upload a CSV or Excel file containing your site's eligible prescriber NPIs. "
        "Required column: `NPI` (10-digit). Optional columns: `Provider name`, `Specialty`, `Site`, `Active`."
    )

    _prov_upload = st.file_uploader(
        "Provider NPI file (CSV or Excel)",
        type=["csv", "xlsx", "xls"],
        key="prov_reg_upload",
        help="Must contain an 'NPI' column with 10-digit provider NPIs.",
    )

    if _prov_upload is not None:
        try:
            _fname = _prov_upload.name.lower()
            if _fname.endswith((".xlsx", ".xls")):
                _new_providers = pd.read_excel(_prov_upload, dtype=str)
            else:
                _new_providers = pd.read_csv(_prov_upload, dtype=str)

            # Normalize NPI column name
            _npi_col = next(
                (c for c in _new_providers.columns
                 if c.strip().upper() in ("NPI", "PROVIDER NPI", "PROVIDER_NPI", "DR NPI", "DR NPI")),
                None,
            )
            if _npi_col is None:
                st.error(
                    "Could not find an NPI column. "
                    "Ensure your file has a column named `NPI`, `Provider NPI`, or `DR NPI`."
                )
            else:
                if _npi_col != "NPI":
                    _new_providers = _new_providers.rename(columns={_npi_col: "NPI"})
                # Strip non-digits from NPI
                _new_providers["NPI"] = (
                    _new_providers["NPI"].astype(str).str.strip().str.replace(r"\D", "", regex=True)
                )
                _new_providers = _new_providers[_new_providers["NPI"].str.fullmatch(r"\d{10}")]

                st.success(f"Found {len(_new_providers):,} valid 10-digit NPIs in uploaded file.")
                st.dataframe(
                    _new_providers.head(20),
                    width="stretch",
                    height=250,
                )
                if len(_new_providers) > 20:
                    st.caption(f"Showing first 20 of {len(_new_providers):,} rows.")

                _col_add, _col_replace = st.columns(2)

                # Merge into existing registry
                with _col_add:
                    if st.button(
                        f"➕ Add to registry ({len(_new_providers):,} providers)",
                        type="primary",
                        key="prov_add_btn",
                    ):
                        if _reg is not None and not _reg.empty:
                            _merged = (
                                pd.concat([_reg, _new_providers], ignore_index=True)
                                .drop_duplicates(subset=["NPI"])
                                .reset_index(drop=True)
                            )
                        else:
                            _merged = _new_providers.copy()
                        _save_registry(_merged)
                        st.success(
                            f"Registry updated — {len(_merged):,} providers committed to memory. "
                            "Re-run the audit to apply."
                        )
                        st.rerun()

                # Replace existing registry
                with _col_replace:
                    if st.button(
                        "Replace registry with this file",
                        key="prov_replace_btn",
                    ):
                        _save_registry(_new_providers.copy())
                        st.success(
                            f"Registry replaced — {len(_new_providers):,} providers committed to memory. "
                            "Re-run the audit to apply."
                        )
                        st.rerun()

        except Exception as _pe:
            st.error(f"Could not read provider file: {_pe}")

    # ── Download template ─────────────────────────────────────────────────────
    _tmpl = pd.DataFrame(columns=["NPI", "Provider name", "Specialty", "Site", "Active"])
    st.download_button(
        "⬇ Download provider template CSV",
        data=_tmpl.to_csv(index=False).encode(),
        file_name="provider_registry_template.csv",
        mime="text/csv",
        key="prov_tmpl_dl",
    )

    # ── Current registry ──────────────────────────────────────────────────────
    if _reg_count > 0:
        st.markdown("---")
        st.markdown(f"#### Current registry — {_reg_count:,} providers")

        _search = st.text_input(
            "Search registry (NPI or name)",
            placeholder="Type NPI or provider name…",
            key="prov_search",
        )
        _reg_display = _reg.copy()
        if _search.strip():
            _mask = _reg_display.apply(
                lambda col: col.astype(str).str.contains(_search.strip(), case=False, na=False)
            ).any(axis=1)
            _reg_display = _reg_display[_mask]

        st.dataframe(_reg_display, width="stretch", height=350)
        st.caption(f"Showing {len(_reg_display):,} of {_reg_count:,} registered providers.")

        # Export registry
        st.download_button(
            "⬇ Export registry as CSV",
            data=_reg.to_csv(index=False).encode(),
            file_name="provider_registry.csv",
            mime="text/csv",
            key="prov_export_dl",
        )

        st.markdown("---")
        # Impact on current audit
        if "claims" in dir():
            _flagged_ip = int((claims.get("Compliance category", pd.Series()) == INELIGIBLE_PRESCRIBER).sum()) \
                if "Compliance category" in claims.columns else 0
            _reg_npis = set(_reg["NPI"].astype(str).str.strip())
            _claim_npis = set(claims["Provider NPI"].astype(str).str.strip()) \
                if "Provider NPI" in claims.columns else set()
            _unmatched = _claim_npis - _reg_npis - {""}
            st.info(
                f"**Registry impact on current audit:** "
                f"{_flagged_ip:,} claims flagged as Ineligible Prescriber · "
                f"{len(_unmatched):,} unique NPIs in claims not found in registry."
            )

        # Clear registry
        with st.expander("Danger zone"):
            st.warning("This will permanently delete the provider registry from memory.")
            if st.button("Clear registry", type="secondary", key="prov_clear_btn"):
                if _PROVIDER_REGISTRY.exists():
                    _PROVIDER_REGISTRY.unlink()
                _cached_registry.clear()
                st.success("Registry cleared.")
                st.rerun()


# ── TAB 6: Downloads ──────────────────────────────────────────────────────────

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
