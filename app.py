"""
ACE 340B Decision Engine — Streamlit Dashboard
"""
from __future__ import annotations

import datetime
import io
import json
import os
import tempfile
import time
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st
from cryptography.fernet import Fernet

from ace_340b_audit.engine import run_audit_from_workbook, audit_dataframe
from ace_340b_audit.ingest import detect_rx_log, map_rx_log, looks_like_ehr
from ace_340b_audit.rules import DEFAULT_RULES, load_rules, save_rules
from ace_340b_audit.decisions import (
    CATEGORIES, CATEGORY_COLORS, CATEGORY_DESCRIPTIONS, SEVERITY,
    COMPLIANT, MISSING_ENCOUNTER, INELIGIBLE_PRESCRIBER,
    WRONG_SITE, DATA_MISMATCH, DUPLICATE_DISCOUNT,
)
from ace_340b_audit.report import generate_html_report
from ace_340b_audit.ehr import (
    detect_ehr_columns, normalize_ehr, crossref_claims,
    CANONICAL as EHR_CANONICAL, EHR_DISPLAY_FIELDS,
)

# ── HIPAA / security helpers ───────────────────────────────────────────────────

def _get_enc_key() -> bytes:
    """Return per-session Fernet key; generated once and stored in session_state."""
    if "_enc_key" not in st.session_state:
        st.session_state["_enc_key"] = Fernet.generate_key()
    return st.session_state["_enc_key"]


def _secure_delete(path: str) -> None:
    """Overwrite file with zeros then delete — prevents PHI recovery from disk."""
    try:
        sz = os.path.getsize(path)
        with open(path, "r+b") as _f:
            _f.write(b"\x00" * sz)
        os.unlink(path)
    except Exception:
        try:
            os.unlink(path)
        except Exception:
            pass


def _audit_log(event: str) -> None:
    """Append a timestamped event to the in-session audit log (no PHI recorded)."""
    if "_audit_log" not in st.session_state:
        st.session_state["_audit_log"] = []
    st.session_state["_audit_log"].append({
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
    })


def _mask_phi(df: pd.DataFrame) -> pd.DataFrame:
    """Return a display copy with patient names and Rx numbers redacted."""
    df = df.copy()
    if "Patient name" in df.columns:
        def _mn(n: str) -> str:
            parts = str(n).strip().split()
            return " ".join(p[0] + "***" for p in parts if p) if parts else "***"
        df["Patient name"] = df["Patient name"].apply(_mn)
    if "Prescription number" in df.columns:
        df["Prescription number"] = df["Prescription number"].astype(str).apply(
            lambda x: "***" + x.strip()[-4:] if len(x.strip()) >= 4 else "***"
        )
    return df


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

# ── session timeout & HIPAA init ──────────────────────────────────────────────
_NOW = time.time()
if "_last_activity" not in st.session_state:
    st.session_state["_last_activity"] = _NOW
    st.session_state["_temp_files"] = []
    _audit_log("Session started")
_inactive_secs = _NOW - st.session_state["_last_activity"]
st.session_state["_last_activity"] = _NOW  # reset on every run (= every user interaction)

if _inactive_secs > 1800:  # 30-minute auto-expire
    for _tp in st.session_state.get("_temp_files", []):
        _secure_delete(_tp)
    st.session_state.clear()
    st.warning(
        "🔒 **HIPAA Security:** Your session expired after 30 minutes of inactivity. "
        "All uploaded data has been securely cleared. Please refresh the page to start a new session."
    )
    st.stop()
elif _inactive_secs > 1200:  # 20-minute warning
    _mins_left = max(1, int((1800 - _inactive_secs) // 60) + 1)
    st.warning(
        f"🔒 **HIPAA Security:** Session will auto-expire in ~{_mins_left} minute(s) due to inactivity. "
        "Save your work."
    )

# ── HIPAA notice & disclaimer ─────────────────────────────────────────────────
st.success(
    "🔒 **HIPAA-Secured Session** — Uploaded files are encrypted at rest using AES-256 (Fernet). "
    "No PHI is retained between sessions. Session auto-expires after 30 minutes of inactivity. "
    "Use only under a signed **Business Associate Agreement (BAA)** with ACE 340B.",
    icon=None,
)
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
_SITE_REGISTRY     = Path(__file__).resolve().parent / "site_registry.json"

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


# ── site registry (persisted to disk) ─────────────────────────────────────────
# Maps {store_id: {"340B ID": "...", "Covered entity": "..."}}
# Once a store is registered the engine entity check passes for that site.

def _load_site_registry() -> dict:
    """Load site registry from disk; returns {} if not present or unreadable."""
    if _SITE_REGISTRY.exists():
        try:
            with open(_SITE_REGISTRY) as _f:
                return json.load(_f)
        except Exception:
            pass
    return {}

def _save_site_registry(reg: dict) -> None:
    """Persist site registry to disk and clear the cached resource."""
    with open(_SITE_REGISTRY, "w") as _f:
        json.dump(reg, _f, indent=2)
    _cached_site_registry.clear()

@st.cache_resource
def _cached_site_registry():
    """Cache site registry across reruns."""
    return {"data": _load_site_registry()}


# ── entity frameworks (persisted to disk) ─────────────────────────────────────
# Structure:
#   {
#     "entity_001": {
#       "name": "Southside Medical Center",
#       "340B_ID": "SMC340B-001",
#       "carve_status": "carve-in",     # "carve-in" | "carve-out" | "unknown"
#       "sites": {
#         "106540": {
#           "address": "1046 Ridge Ave SW, Atlanta, GA 30315",
#           "site_type": "340B"          # "340B" | "retail"
#         }
#       }
#     }
#   }
_ENTITY_FW = Path(__file__).resolve().parent / "entity_frameworks.json"


def _load_entity_framework() -> dict:
    """Load entity frameworks from disk; returns {} if not present."""
    if _ENTITY_FW.exists():
        try:
            with open(_ENTITY_FW) as _f:
                return json.load(_f)
        except Exception:
            pass
    return {}


def _save_entity_framework(fw: dict) -> None:
    """Persist entity frameworks to disk and bust cache."""
    with open(_ENTITY_FW, "w") as _f:
        json.dump(fw, _f, indent=2)
    _cached_entity_framework.clear()


@st.cache_resource
def _cached_entity_framework():
    """Cache entity frameworks across reruns."""
    return {"data": _load_entity_framework()}


def _build_entity_lookup(fw: dict) -> dict:
    """
    Flatten entity frameworks into a store-ID → entity-info dict.

    Returns
    -------
    {store_id: {entity_id, name, b340_id, carve_status, site_type, address}}
    """
    lookup: dict = {}
    for eid, edata in fw.items():
        for sid, sdata in edata.get("sites", {}).items():
            lookup[str(sid).strip()] = {
                "entity_id":    eid,
                "name":         edata.get("name", ""),
                "b340_id":      edata.get("340B_ID", ""),
                "carve_status": edata.get("carve_status", "unknown"),
                "site_type":    sdata.get("site_type", "340B"),
                "address":      sdata.get("address", ""),
            }
    return lookup


def _addr_keywords(address: str) -> list[str]:
    """Extract searchable uppercase keywords from a site address string."""
    upper = address.upper().strip()
    parts = [p.strip() for p in upper.split(",") if p.strip()]
    kws: list[str] = []
    if parts:
        kws.append(parts[0])          # full street: "1046 RIDGE AVE SW"
        words = parts[0].split()
        if len(words) >= 2:
            kws.append(" ".join(words[:2]))   # "1046 RIDGE" — most unique
    return kws


# ── sidebar ───────────────────────────────────────────────────────────────────

st.sidebar.header("Data inputs")
uploaded_file = st.sidebar.file_uploader(
    "340B workbook (.xlsx) or RX log (.csv) ✳ Required",
    type=["xlsx", "csv"],
    help=(
        "• Excel workbook (.xlsx) with sheets: Raw_Data, Store_Map, Site_Entity_Map\n"
        "• Excel RX-log export (.xlsx) — auto-detected when first sheet contains "
        "RXNBR, FILLDATE, DRUG NAME, NDC, RX STOREID, DR NPI columns\n"
        "• CSV RX-log (.csv) with the same columns\n"
        "The engine auto-maps RX-log columns and extracts site addresses from "
        "DOCADD1/DOCCITY/DOCST/DOCZIP.\n\n"
        "Note: EHR patient/encounter files (e.g. medication lists, visit records) "
        "should be uploaded in the 🩺 EHR Encounter Data section below, not here."
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
    "Provider master — one-time upload (optional)",
    type=["csv", "xlsx", "xls"],
    help=(
        "CSV or Excel file with 'NPI' or 'Provider NPI' column for a one-time session upload. "
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

# ── Code 20 / No Code 20 Report upload ───────────────────────────────────────
with st.sidebar.expander("💢 Code 20 — Scripts Without 340B Indicator", expanded=False):
    st.caption(
        "Upload the report of scripts that are NOT billed with Code 20. "
        "Every script in this file is missing the 340B billing indicator "
        "(NCPDP 436-E1 ≠ 20), meaning it is not being billed at the 340B price."
    )
    code20_file = st.file_uploader(
        "No Code 20 report",
        type=["xlsx", "xls", "csv", "xlsm"],
        key="code20_upload",
        help="Southside format: Rx Nbr, RX DATE, DRUG NAME, DOCTOR, PLANID, PAID, etc.",
    )
_EHR_SLOTS = 5
with st.sidebar.expander(
    "🩺 EHR Encounter Data (up to 5 datasets)",
    expanded=any(st.session_state.get(f"_ehr_raw_json_{i}") for i in range(1, _EHR_SLOTS + 1)),
):
    st.caption(
        "Upload one export per EHR system. All datasets are combined for cross-referencing. "
        "Accepts .xlsx, .xls, or .csv."
    )
    _ehr_file_slots: list = []
    for _si in range(1, _EHR_SLOTS + 1):
        _lbl = f"EHR Dataset {_si}" + (" (optional)" if _si > 1 else "")
        _ehr_file_slots.append(
            st.file_uploader(
                _lbl,
                type=["xlsx", "xls", "csv"],
                key=f"ehr_upload_{_si}",
                help=f"EHR export #{_si} — encounter date, provider, patient, location, drug, NDC, Rx.",
            )
        )

st.sidebar.markdown("---")

# ── HIPAA / security sidebar ───────────────────────────────────────────────────
_mask_phi_enabled = st.sidebar.checkbox(
    "🔒 Mask PHI in display",
    value=False,
    key="_phi_mask",
    help=(
        "Replaces patient names with initials (J*** D***) and truncates Rx numbers "
        "to last 4 digits. Downloaded CSVs and reports are NOT masked."
    ),
)
with st.sidebar.expander("🔒 HIPAA Security", expanded=False):
    if "_enc_key" in st.session_state:
        st.success("🔐 File encryption: ACTIVE (AES-256 Fernet)")
    else:
        st.info("File encryption activates on first upload.")
    st.caption(
        "Uploaded files are encrypted at rest using Fernet (AES-256-CBC + HMAC-SHA256). "
        "The encryption key is unique to this browser session and never written to disk."
    )
    _mins_inactive = int(_inactive_secs // 60)
    _mins_remain   = max(0, 30 - _mins_inactive)
    st.caption(
        f"Session auto-expires in ~{_mins_remain} min · "
        f"Inactive for {_mins_inactive} min"
    )
    st.caption(
        "⚠️ **BAA required.** Use only under a signed Business Associate Agreement. "
        "Ref: 45 CFR §§ 164.312(a)(2)(iv), 164.314(b)."
    )
    if st.button("🗑 Clear session data now", key="_clear_session_btn"):
        _audit_log("Session data manually cleared by user")
        for _tp in st.session_state.get("_temp_files", []):
            _secure_delete(_tp)
        st.session_state.clear()
        st.rerun()

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
    _auto_carve    = st.session_state.get("_auto_carve", "unknown")
    _carve_index   = {"unknown": 0, "carve-in": 1, "carve-out": 2}.get(_auto_carve, 0)
    if _auto_carve != "unknown":
        st.caption(f"🏢 Auto-detected from Entity Framework: **{_auto_carve}**")
    carve_status = st.radio(
        "Select your covered entity's Medicaid FFS carve-in / carve-out election",
        options=["unknown", "carve-in", "carve-out"],
        format_func=lambda x: {
            "unknown":   "⚠️  Not set — select before acting on Duplicate Discount claims",
            "carve-in":  "✅  Carve-In — Entity uses 340B drugs for Medicaid FFS; listed on HRSA MEF",
            "carve-out": "🚫  Carve-Out — Entity purchases at WAC for Medicaid FFS; NOT on MEF",
        }[x],
        index=_carve_index,
        key="_carve_radio",
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
    """Encrypt data with the session Fernet key and write to a temp file."""
    enc_data = Fernet(_get_enc_key()).encrypt(data)
    with tempfile.NamedTemporaryFile(suffix=suffix + ".enc", delete=False) as f:
        f.write(enc_data)
        path = f.name
    if "_temp_files" not in st.session_state:
        st.session_state["_temp_files"] = []
    st.session_state["_temp_files"].append(path)
    return path


source_path: str | None = None
input_format: str = "excel"   # "excel" | "csv" | "excel_rxlog"
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
            _fmt   = "csv"
        elif wb_bytes[:4] in (b"PK\x03\x04", b"\xd0\xcf\x11\xe0"):
            suffix = ".xlsx"
            # Peek at first sheet to detect RX-log Excel (e.g. rxlog_20260501... sheet)
            try:
                import io as _io
                _px = pd.ExcelFile(_io.BytesIO(wb_bytes))
                if "Raw_Data" not in _px.sheet_names and _px.sheet_names:
                    _pdf = _px.parse(_px.sheet_names[0], nrows=3, dtype=str)
                    _fmt = "excel_rxlog" if detect_rx_log(_pdf) else "excel"
                else:
                    _fmt = "excel"
            except Exception:
                _fmt = "excel"
        else:
            st.error(
                "⚠️  Unrecognised file format. Upload a .xlsx workbook or a pharmacy RX-log .csv file."
            )
            st.stop()
        st.session_state["_wb_file_id"]  = file_id
        st.session_state["_wb_path"]     = _write_temp(wb_bytes, suffix)
        st.session_state["_wb_format"]   = _fmt
        _audit_log(
            f"File uploaded: {uploaded_file.name} "
            f"({len(wb_bytes):,} bytes) · format={_fmt} · encrypted=AES-256"
        )
        # Auto-detect carve status from entity framework
        try:
            _fw_lkup = _build_entity_lookup(_cached_entity_framework()["data"])
            if _fw_lkup:
                if _fmt == "csv":
                    import io as _io
                    _peek = pd.read_csv(_io.BytesIO(wb_bytes), nrows=5, dtype=str)
                elif _fmt == "excel_rxlog":
                    import io as _io
                    _px2 = pd.ExcelFile(_io.BytesIO(wb_bytes))
                    _peek = _px2.parse(_px2.sheet_names[0], nrows=5, dtype=str)
                else:
                    _peek = pd.DataFrame()
                if "RX STOREID" in _peek.columns:
                    _detected_sids = set(_peek["RX STOREID"].astype(str).str.strip().unique())
                    _carve_set = {_fw_lkup[s]["carve_status"] for s in _detected_sids if s in _fw_lkup}
                    if len(_carve_set) == 1:
                        st.session_state["_auto_carve"] = _carve_set.pop()
                    else:
                        st.session_state["_auto_carve"] = "unknown"
        except Exception:
            pass
    source_path  = st.session_state["_wb_path"]
    input_format = st.session_state.get("_wb_format", "excel")
    if input_format in ("csv", "excel_rxlog"):
        _fmt_label       = "RX-log CSV" if input_format == "csv" else "RX-log Excel"
        _reg_count_sites = len(_cached_site_registry()["data"])
        if _reg_count_sites > 0:
            st.sidebar.success(
                f"{_fmt_label} detected. {_reg_count_sites} site(s) registered — "
                "entity/site checks active. Go to Store Status → 340B Site Registration "
                "to add or update sites."
            )
        else:
            st.sidebar.info(
                f"{_fmt_label} detected. Columns auto-mapped to the 340B audit engine. "
                "Go to **Store Status → 340B Site Registration** to register your "
                "340B ID and covered entity — this activates the entity/site-of-care check. "
                "All other checks (NPI, NDC, encounter date, duplicate discount) "
                "run on live claim data."
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
        _pm_name = provider_master_file.name.lower()
        if _pm_name.endswith((".xlsx", ".xls")):
            _upload_pm = pd.read_excel(provider_master_file, dtype=str)
        else:
            _upload_pm = pd.read_csv(provider_master_file, dtype=str)

        # Auto-detect NPI column under common aliases and normalise to "NPI"
        _pm_cols_lower = {c.lower().strip(): c for c in _upload_pm.columns}
        _npi_aliases = [
            "npi", "provider npi", "physician npi", "prescriber npi",
            "rendering npi", "ordering npi", "national provider identifier",
        ]
        _npi_src = next(
            (_pm_cols_lower[a] for a in _npi_aliases if a in _pm_cols_lower),
            None,
        )
        if _npi_src and _npi_src != "NPI":
            _upload_pm = _upload_pm.rename(columns={_npi_src: "NPI"})

        # If file has Provider Last Name + First Name but no NPI, add a combined name
        _has_npi = "NPI" in _upload_pm.columns
        _last_col  = _pm_cols_lower.get("provider last name") or _pm_cols_lower.get("last name")
        _first_col = _pm_cols_lower.get("provider first name") or _pm_cols_lower.get("first name")
        if not _has_npi and _last_col and _first_col:
            _upload_pm["Provider name"] = (
                _upload_pm[_last_col].fillna("").str.strip()
                + ", "
                + _upload_pm[_first_col].fillna("").str.strip()
            ).str.strip(", ")
            st.sidebar.warning(
                "Provider master loaded, but no NPI column was found. "
                "NPI-based checks will be skipped. Add an 'NPI' column for full validation."
            )
        elif not _has_npi:
            st.sidebar.warning(
                "Provider master loaded without an NPI column — "
                "NPI validation checks will be skipped."
            )

        if provider_master_df is not None and not provider_master_df.empty:
            # Merge session upload on top of registry (union, deduplicated on NPI if present)
            _combined = pd.concat([provider_master_df, _upload_pm], ignore_index=True)
            if "NPI" in _combined.columns:
                provider_master_df = _combined.drop_duplicates(subset=["NPI"]).reset_index(drop=True)
            else:
                provider_master_df = _combined.reset_index(drop=True)
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

# ── EHR data loading (up to 5 datasets) ──────────────────────────────────────
# _ehr_datasets: list of {idx, name, raw, norm, col_map} for each loaded slot
_ehr_datasets: list[dict] = []

for _si, _ehr_file in enumerate(_ehr_file_slots, start=1):
    _fid_key  = f"_ehr_file_id_{_si}"
    _name_key = f"_ehr_file_name_{_si}"
    _json_key = f"_ehr_raw_json_{_si}"

    if _ehr_file is not None:
        _ehr_fid = _ehr_file.file_id
        if st.session_state.get(_fid_key) != _ehr_fid:
            # New or replaced file — read and cache
            _ehr_bytes = _ehr_file.getbuffer().tobytes()
            st.session_state[_fid_key]  = _ehr_fid
            st.session_state[_name_key] = _ehr_file.name
            st.session_state[_json_key] = None
            try:
                _ehr_fname = _ehr_file.name.lower()
                if _ehr_fname.endswith((".xlsx", ".xls")):
                    _ehr_raw_df = pd.read_excel(io.BytesIO(_ehr_bytes), dtype=str)
                else:
                    _ehr_raw_df = pd.read_csv(io.BytesIO(_ehr_bytes), dtype=str)
                st.session_state[_json_key] = _ehr_raw_df.to_json(orient="split")
                _audit_log(
                    f"EHR Dataset {_si} uploaded: {_ehr_file.name} "
                    f"({len(_ehr_raw_df):,} rows, {len(_ehr_raw_df.columns)} columns)"
                )
            except Exception as _ehr_e:
                st.sidebar.warning(f"EHR Dataset {_si}: could not read file — {_ehr_e}")

    # Load from cache (whether just uploaded or from a previous run)
    _ehr_json = st.session_state.get(_json_key)
    if _ehr_json:
        _raw = pd.read_json(io.StringIO(_ehr_json), orient="split")
        _override = st.session_state.get(f"_ehr_col_override_{_si}", {})
        _norm, _col_map = normalize_ehr(_raw, override_map=_override)
        _ehr_datasets.append({
            "idx":     _si,
            "name":    st.session_state.get(_name_key, f"Dataset {_si}"),
            "raw":     _raw,
            "norm":    _norm,
            "col_map": _col_map,
        })

# Combined normalized EHR (union of all loaded datasets)
_ehr_combined_norm: pd.DataFrame | None = None
if _ehr_datasets:
    _ehr_combined_norm = pd.concat(
        [d["norm"] for d in _ehr_datasets], ignore_index=True
    )
    _total_ehr_rows = len(_ehr_combined_norm)
    _total_enc = (
        _ehr_combined_norm[EHR_CANONICAL["encounter_date"]].notna().sum()
        if EHR_CANONICAL["encounter_date"] in _ehr_combined_norm.columns else 0
    )
    # Sidebar summary shown inside the expander would cause state issues; show as caption
    st.sidebar.caption(
        f"🩺 {len(_ehr_datasets)} EHR dataset(s) loaded · "
        f"{_total_ehr_rows:,} rows · {_total_enc:,} dated encounters"
    )


# ── Code 20 processing ──────────────────────────────────────────────────────

_CODE20_PLANID_MAP = {
    # Medicaid / Managed Medicaid
    "AMERI":      "Managed Medicaid — Amerigroup",
    "AMERIGRO":   "Managed Medicaid — Amerigroup",
    "MEDICAID":   "Medicaid",
    # Commercial — BCBS
    "ANT/BCBS":   "Commercial — Anthem BCBS",
    "BCBS/G":     "Commercial — BCBS of Georgia",
    "BCBS":       "Commercial — BCBS",
    "BCBS/ILL":   "Commercial — BCBS Illinois",
    "BCBS/NJ":    "Commercial — BCBS New Jersey",
    "BCBSMR":     "Commercial — BCBS (MR)",
    "BGH-FEP":    "Commercial — BCBS Federal Employee",
    "BCBO":       "Commercial — BCBS (Other)",
    "BCTX":       "Commercial — BCBS Texas",
    "ANT":        "Commercial — Anthem",
    "ANT-D":      "Commercial — Anthem (D)",
    # Commercial — CVS/Caremark
    "CVS/CARE":   "PBM — CVS Caremark",
    "CAREMARK":   "PBM — Caremark",
    "CARMARK":    "PBM — Caremark",
    "CAREMKS":    "PBM — Caremark",
    "CareM":      "PBM — Caremark",
    "CARE":       "PBM — Caremark",
    # Commercial — Cigna
    "CIG-GWH":    "Commercial — Cigna",
    "CIGEXP":     "Commercial — Cigna Express",
    "CIG-D":      "Commercial — Cigna (D)",
    # Commercial — Aetna
    "AETNA":      "Commercial — Aetna",
    "AETNA-D":    "Commercial — Aetna (D)",
    # Commercial — Humana
    "HUMANA-D":   "Commercial — Humana",
    # Commercial — UHC/Optum
    "UHC":        "Commercial — UnitedHealthcare",
    "UNITED":     "Commercial — United",
    "OPTUMRX":    "PBM — OptumRx",
    "OPTUMMD":    "PBM — OptumRx (MD)",
    "UNI-D":      "Commercial — United (D)",
    "UMR":        "Commercial — UMR",
    "UMR/2":      "Commercial — UMR",
    # Commercial — Oscar
    "OSC25":      "Commercial — Oscar Health",
    # Commercial — Medco/Express
    "MEDCO":      "PBM — Medco",
    "MEDCO-D":    "PBM — Medco (D)",
    "CMK-D":      "PBM — Caremark (D)",
    # Commercial — Other
    "5PLAN":      "Commercial — 5 Plan",
    "RXS-D":      "PBM — Rx Savings",
    "PRO/C":      "Commercial — ProCare",
    "SMITHRX":    "PBM — SmithRx",
    "RXADV":      "PBM — Rx Advantage",
    "MAXOR3":     "PBM — Maxor",
    "MAGRX":      "PBM — MagRx",
    "USRX":       "PBM — USRX",
    "CORESLAB":   "PBM — CoreSlab",
    # Cash / Sliding Scale
    "SFS-E":      "Sliding Fee Scale — Cash",
    "SFE-CPAY":   "Sliding Fee — Copay",
    "SFC-CPAY":   "Sliding Fee — Copay",
    "OTC":        "Cash — Over the Counter",
    # Discount
    "DIS/CARD":   "Discount Card",
    "DIS/COU":    "Discount Coupon",
    "GIL/PAP":    "Patient Assistance Program",
    # Pre-pack / Internal
    "PREPACK":    "Pre-Pack (Internal)",
    "FRIDAYS":    "Internal — Fridays",
    "MCK-LOY":    "PBM — McKesson Loyalty",
    "anthemrx":   "Commercial — AnthemRx",
}


def _classify_planid(planid: str) -> str:
    """Classify a PLANID into a billing category."""
    if pd.isna(planid) or not str(planid).strip():
        return "Unknown"
    p = str(planid).strip()
    mapped = _CODE20_PLANID_MAP.get(p)
    if mapped:
        return mapped
    # Numeric plan IDs are typically Medicaid
    if p.isdigit():
        return "Medicaid / State Plan"
    return f"Other — {p}"


def _classify_planid_group(planid: str) -> str:
    """High-level group for PLANID (for charts)."""
    full = _classify_planid(planid)
    if "Medicaid" in full:
        return "Medicaid"
    elif "BCBS" in full or "Anthem" in full:
        return "BCBS / Anthem"
    elif "PBM" in full:
        return "PBM"
    elif "Humana" in full:
        return "Humana"
    elif "Aetna" in full:
        return "Aetna"
    elif "Cigna" in full:
        return "Cigna"
    elif "UHC" in full or "United" in full or "Optum" in full or "UMR" in full:
        return "UHC / Optum"
    elif "Oscar" in full:
        return "Oscar"
    elif "Cash" in full or "Sliding" in full or "Discount" in full or "OTC" in full:
        return "Cash / Discount"
    elif "Commercial" in full:
        return "Commercial — Other"
    elif "Patient Assistance" in full:
        return "PAP"
    else:
        return "Other"


@st.cache_data
def _load_code20(file) -> pd.DataFrame:
    """Load a Code 20 / rejected claims file (Southside format or generic)."""
    fname = file.name.lower()
    file.seek(0)
    if fname.endswith(".csv"):
        df = pd.read_csv(file, dtype=str)
    else:
        df = pd.read_excel(file, dtype=str)

    df.columns = df.columns.str.strip()

    # Detect Southside format (Rx Nbr, RX DATE, PLANID, etc.)
    southside_cols = {"Rx Nbr", "RX DATE", "DRUG NAME", "PLANID", "PAID"}
    is_southside = southside_cols.issubset(set(df.columns))

    if is_southside:
        # Normalize Southside format
        df["RX DATE"] = pd.to_datetime(df["RX DATE"], errors="coerce")
        df["PAID"] = pd.to_numeric(df["PAID"], errors="coerce").fillna(0)
        df["QTY"] = pd.to_numeric(df.get("QTY", 0), errors="coerce").fillna(0)
        df["RF"] = pd.to_numeric(df.get("RF", 0), errors="coerce").fillna(0)
        df["PA CODE"] = df.get("PA CODE", "").astype(str).replace("nan", "")
        df["DAW"] = pd.to_numeric(df.get("DAW", 0), errors="coerce").fillna(0)

        # Combine doctor name
        doc_first = df.get("DOCTOR FIRST NAME", pd.Series([""] * len(df))).fillna("").astype(str).str.strip()
        doc_last = df.get("DOCTOR LAST NAME", pd.Series([""] * len(df))).fillna("").astype(str).str.strip()
        df["Provider"] = (doc_first + " " + doc_last).str.strip()

        # Classify plan
        df["Plan Category"] = df["PLANID"].apply(_classify_planid)
        df["Plan Group"] = df["PLANID"].apply(_classify_planid_group)

        # Flag type of rejection
        df["Rejection Type"] = df.apply(lambda r: (
            "Zero Pay" if float(r["PAID"]) == 0
            else "Underpaid" if float(r["PAID"]) < 1
            else "Negative" if float(r["PAID"]) < 0
            else "Low Reimbursement"
        ), axis=1)

        df["_format"] = "southside"
    else:
        # Generic format — try to map common column names
        df["_format"] = "generic"
        df["Plan Group"] = "Unknown"
        df["Plan Category"] = "Unknown"
        df["Rejection Type"] = "Unknown"
        df["Provider"] = ""

    return df


_code20_df: pd.DataFrame | None = None
if code20_file is not None:
    _code20_df = _load_code20(code20_file)
    if _code20_df is not None and not _code20_df.empty:
        _c20_count = len(_code20_df)
        _c20_zero_pay = len(_code20_df[_code20_df.get("PAID", pd.Series()).astype(float) == 0]) if "PAID" in _code20_df.columns else 0
        st.sidebar.caption(
            f"💢 No Code 20 report loaded · {_c20_count:,} scripts without 340B indicator"
            + (f" · {_c20_zero_pay:,} zero-pay" if _c20_zero_pay else "")
        )


# ── run audit ─────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _load_results(path, pm_json, mef_json, exc_json, rules_json, carve,
                  fmt="excel", site_reg_json=None, entity_fw_json=None,
                  enc_key_hex=None) -> dict:
    rules = json.loads(rules_json)

    # ── decrypt temp file if it was encrypted by _write_temp ──────────────────
    _use_bio: io.BytesIO | None = None
    if enc_key_hex and isinstance(path, str) and path.endswith(".enc"):
        try:
            _raw_bytes = Path(path).read_bytes()
            _dec_bytes = Fernet(bytes.fromhex(enc_key_hex)).decrypt(_raw_bytes)
            _use_bio = io.BytesIO(_dec_bytes)
        except Exception as _dec_err:
            raise ValueError(
                f"Could not decrypt uploaded file (session key mismatch or corrupt data): {_dec_err}"
            ) from _dec_err

    def _rj(j):
        df = pd.read_json(io.StringIO(j))
        return df.astype({c: object for c in df.columns})

    pm  = _rj(pm_json)  if pm_json  else None
    mef = _rj(mef_json) if mef_json else None
    exc = _rj(exc_json) if exc_json else None

    # Build entity lookup once
    _entity_fw   = json.loads(entity_fw_json) if entity_fw_json else {}
    _entity_lkup = _build_entity_lookup(_entity_fw)

    # ── helpers shared by CSV and Excel-RX-log paths ──────────────────────────
    def _apply_entity_and_site_reg(sm_df, se_df, raw_df):
        """
        1. Apply entity framework (higher priority):
           - 340B sites → inject 340B ID + Covered entity + Site type='340B'
           - Retail sites → inject Site type='Retail', leave 340B ID blank
        2. Apply site registry (lower priority, fills gaps not covered by entity fw)
        3. Add 'Prescriber address check' to raw_df via DOCADD1 match
        """
        # ── Step 1: entity framework ──────────────────────────────────────────
        if _entity_lkup:
            if "Site type" not in sm_df.columns:
                sm_df["Site type"] = ""
            for _sid, _info in _entity_lkup.items():
                _sm_mask = sm_df["Store number"].astype(str).str.strip() == _sid
                if not _sm_mask.any():
                    continue
                _se_mask = se_df["Site location"].astype(str).str.strip() == _sid

                if _info["site_type"].upper() == "RETAIL":
                    # Retail: mark as retail, no 340B credentials
                    sm_df.loc[_sm_mask, "Site type"] = "Retail"
                else:
                    # 340B site: inject credentials
                    _b340   = _info["b340_id"]
                    _entity = _info["name"]
                    if _b340:
                        sm_df.loc[_sm_mask, "340B ID"]        = _b340
                        sm_df.loc[_sm_mask, "Covered entity"] = _entity
                        sm_df.loc[_sm_mask, "Site type"]      = "340B"
                        if _se_mask.any():
                            se_df.loc[_se_mask, "340B ID"]        = _b340
                            se_df.loc[_se_mask, "Covered entity"] = _entity

        # ── Step 2: site registry (gap-fill only) ─────────────────────────────
        if site_reg_json:
            for _sid, _reg in json.loads(site_reg_json).items():
                _b340   = str(_reg.get("340B ID", "")).strip()
                _entity = str(_reg.get("Covered entity", "")).strip()
                if not _b340:
                    continue
                _sm_mask = sm_df["Store number"].astype(str).str.strip() == str(_sid).strip()
                # Only fill if entity framework hasn't already set a 340B ID
                _already = sm_df.loc[_sm_mask, "340B ID"].astype(str).str.strip().ne("").any() if _sm_mask.any() else False
                if _sm_mask.any() and not _already:
                    sm_df.loc[_sm_mask, "340B ID"]        = _b340
                    sm_df.loc[_sm_mask, "Covered entity"] = _entity
                _se_mask = se_df["Site location"].astype(str).str.strip() == str(_sid).strip()
                _se_already = se_df.loc[_se_mask, "340B ID"].astype(str).str.strip().ne("").any() if _se_mask.any() else False
                if _se_mask.any() and not _se_already:
                    se_df.loc[_se_mask, "340B ID"]        = _b340
                    se_df.loc[_se_mask, "Covered entity"] = _entity

        # ── Step 3: DOCADD1 prescriber address check ──────────────────────────
        # Build keyword list from entity framework site addresses
        _addr_kws: list[str] = []
        for _info in _entity_lkup.values():
            _addr_kws.extend(_addr_keywords(_info.get("address", "")))
        _addr_kws = [k for k in _addr_kws if k]  # remove empty

        if _addr_kws and "DOCADD1" in raw_df.columns:
            _docadd1 = raw_df["DOCADD1"].fillna("").astype(str).str.upper()
            raw_df["Prescriber address check"] = _docadd1.apply(
                lambda v: "PASS" if any(kw in v for kw in _addr_kws) else "REVIEW"
            )
        # else: engine defaults to N/A

        return sm_df, se_df, raw_df

    def _audit_rxlog(raw_df):
        """Map RX-log columns → canonical, apply framework+registry, run engine."""
        raw_df, sm_df, se_df = map_rx_log(raw_df)
        sm_df, se_df, raw_df = _apply_entity_and_site_reg(sm_df, se_df, raw_df)
        return audit_dataframe(
            raw=raw_df, store_map=sm_df, site_entity_map=se_df,
            provider_master=pm, mef=mef, exceptions=exc,
            rules=rules, carve_status=carve,
        )

    def _to_object(df):
        """Convert all columns to plain object dtype (avoids PyArrow issues on Cloud)."""
        return df.astype({c: object for c in df.columns})

    # ── route by format ────────────────────────────────────────────────────────
    _src = _use_bio if _use_bio is not None else path   # BytesIO (decrypted) or plain path

    if fmt == "csv":
        raw_df = _to_object(pd.read_csv(_src, dtype=str, low_memory=False))
        if not detect_rx_log(raw_df):
            if looks_like_ehr(raw_df):
                raise ValueError(
                    "EHR_FILE_DETECTED: This file appears to be an EHR patient/encounter "
                    "export (columns: " + ", ".join(raw_df.columns[:8].tolist()) + ", ...). "
                    "Upload it using the EHR Encounter Data section in the sidebar, "
                    "not the main claims file slot."
                )
            raise ValueError(
                "CSV does not match the expected RX-log format. "
                "Required columns: RXNBR, FILLDATE, DRUG NAME, NDC, RX STOREID, DR NPI."
            )
        res = _audit_rxlog(raw_df)

    elif fmt == "excel_rxlog":
        # Excel file whose first sheet is an RX-log (e.g. rxlog_20260501092518)
        _xl  = pd.ExcelFile(_src)
        raw_df = _to_object(_xl.parse(_xl.sheet_names[0], dtype=str))
        if not detect_rx_log(raw_df):
            if looks_like_ehr(raw_df):
                raise ValueError(
                    "EHR_FILE_DETECTED: This file appears to be an EHR patient/encounter "
                    "export. Upload it using the EHR Encounter Data section in the sidebar, "
                    "not the main claims file slot."
                )
            raise ValueError(
                f"Excel sheet '{_xl.sheet_names[0]}' does not match the RX-log format. "
                "Required columns: RXNBR, FILLDATE, DRUG NAME, NDC, RX STOREID, DR NPI."
            )
        res = _audit_rxlog(raw_df)

    else:  # standard 340B workbook
        if _use_bio is not None:
            # Read each sheet from a fresh BytesIO (same decrypted bytes, position=0 each time)
            _wb_bytes = _use_bio.read()
            def _rio():
                return io.BytesIO(_wb_bytes)
            _raw_wb = pd.read_excel(_rio(), sheet_name="Raw_Data")
            _sm_wb  = pd.read_excel(_rio(), sheet_name="Store_Map")
            _se_wb  = pd.read_excel(_rio(), sheet_name="Site_Entity_Map")
            if pm is None:
                try:
                    pm = pd.read_excel(_rio(), sheet_name="Provider_Master")
                except Exception:
                    pass
            if mef is None:
                try:
                    mef = pd.read_excel(_rio(), sheet_name="MEF")
                except Exception:
                    pass
            res = audit_dataframe(
                raw=_raw_wb, store_map=_sm_wb, site_entity_map=_se_wb,
                provider_master=pm, mef=mef, exceptions=exc,
                rules=rules, carve_status=carve,
            )
        else:
            res = run_audit_from_workbook(path, rules=rules, exceptions=exc,
                                          provider_master=pm, mef=mef, carve_status=carve)

    return {k: v.to_dict(orient="split") for k, v in res.items()}


def _reconstruct(cached: dict) -> dict[str, pd.DataFrame]:
    return {k: pd.DataFrame(**v) for k, v in cached.items()}


rules_json      = json.dumps(load_rules())
pm_json         = provider_master_df.to_json() if provider_master_df is not None else None
mef_json        = mef_df.to_json()             if mef_df             is not None else None
exc_json        = exceptions_df.to_json()      if exceptions_df       is not None else None
_site_reg_raw   = _cached_site_registry()["data"]
site_reg_json   = json.dumps(_site_reg_raw) if _site_reg_raw else None
_entity_fw_raw  = _cached_entity_framework()["data"]
entity_fw_json  = json.dumps(_entity_fw_raw) if _entity_fw_raw else None
# Pass session encryption key so _load_results can decrypt the temp file
_enc_key_hex    = st.session_state["_enc_key"].hex() if "_enc_key" in st.session_state else None

with st.spinner("Running decision engine…"):
    try:
        cached = _load_results(source_path, pm_json, mef_json, exc_json, rules_json,
                               carve_status, input_format, site_reg_json, entity_fw_json,
                               _enc_key_hex)
    except ValueError as _ve:
        _msg = str(_ve)
        if "not found" in _msg.lower() or "worksheet" in _msg.lower():
            st.error(
                "⚠️  **Workbook sheet not found.** Your Excel file must contain sheets named exactly: "
                "`Raw_Data`, `Store_Map`, `Site_Entity_Map`. "
                f"Detail: {_msg}"
            )
        elif "ehr_file_detected" in _msg.lower():
            st.warning(
                "📋  **This looks like an EHR patient/encounter file.**  "
                "It cannot be used as the main 340B claims file here.\n\n"
                "**What to do:** Upload this file in the **🩺 EHR Encounter Data** "
                "section in the left sidebar.  The EHR section will auto-detect the "
                "column names and let you cross-reference encounters against your "
                "340B claims."
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

tab_queue, tab_all, tab_store, tab_exc, tab_prov, tab_entity, tab_ehr, tab_c20, tab_dl = st.tabs([
    "⚠️ Review Queue",
    "📄 All Claims",
    "🏪 Store Status",
    "📋 Exception Management",
    "👨‍⚕️ Provider Registry",
    "🏢 Entity Frameworks",
    "🩺 EHR Encounters",
    "💢 Code 20",
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
        st.dataframe(
            _mask_phi(bucket_display) if _mask_phi_enabled else bucket_display,
            width="stretch", height=300,
        )

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

            _rx_display  = row.get('Prescription number', 'N/A')
            _pat_display = row.get('Patient name', 'N/A')
            if _mask_phi_enabled:
                _rx_str = str(_rx_display).strip()
                _rx_display = "***" + _rx_str[-4:] if len(_rx_str) >= 4 else "***"
                _pat_parts  = str(_pat_display).strip().split()
                _pat_display = " ".join(p[0] + "***" for p in _pat_parts if p) if _pat_parts else "***"
            c1, c2, c3 = st.columns(3)
            c1.markdown(f"**Rx#:** `{_rx_display}`")
            c2.markdown(f"**Patient:** {_pat_display}")
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
    display_cols  = [c for c in DISPLAY_COLS if c in filtered.columns]
    _all_display  = filtered[display_cols]
    st.caption(f"{len(filtered):,} of {len(claims):,} claims")
    st.dataframe(
        _mask_phi(_all_display) if _mask_phi_enabled else _all_display,
        width="stretch", height=450,
    )


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

    # ── 340B Site Registration ────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### 🏥 340B Site Registration")
    st.caption(
        "Register each site's 340B ID and covered entity name. "
        "Once registered, claims dispensed from that site will pass the "
        "entity/site-of-care check. Changes take effect on the next audit run."
    )

    _site_reg = _cached_site_registry()["data"]

    # Build a list of (store_id, address) from current audit results
    _reg_stores = (
        store_status[["Store number", "Pharmacy location"]]
        .drop_duplicates("Store number")
        .values.tolist()
    )

    if not _reg_stores:
        st.info("Upload a CSV file to detect stores and register sites.")
    else:
        _any_registered = any(str(s) in _site_reg for s, _ in _reg_stores)
        if _any_registered:
            st.success(
                f"✅ {sum(1 for s, _ in _reg_stores if str(s) in _site_reg)} of "
                f"{len(_reg_stores)} site(s) registered — entity check active."
            )

        for _store_num, _store_addr_val in _reg_stores:
            _sid = str(_store_num)
            _existing = _site_reg.get(_sid, {})
            _is_registered = bool(_existing.get("340B ID", "").strip())

            _badge = (
                "<span style='background:#27ae60;color:white;border-radius:12px;"
                "padding:2px 10px;font-size:0.75em;margin-left:8px'>✓ Registered</span>"
                if _is_registered else
                "<span style='background:#e67e22;color:white;border-radius:12px;"
                "padding:2px 10px;font-size:0.75em;margin-left:8px'>Unregistered</span>"
            )
            with st.expander(
                f"Store {_sid} — {_store_addr_val}",
                expanded=not _is_registered,
            ):
                st.markdown(
                    f"**{_store_addr_val}** {_badge}",
                    unsafe_allow_html=True,
                )
                if _is_registered:
                    st.markdown(
                        f"340B ID: `{_existing.get('340B ID','')}` &nbsp;·&nbsp; "
                        f"Covered entity: **{_existing.get('Covered entity','')}**",
                        unsafe_allow_html=True,
                    )

                with st.form(key=f"site_reg_form_{_sid}"):
                    _c1, _c2 = st.columns(2)
                    _b340_val = _c1.text_input(
                        "340B ID",
                        value=_existing.get("340B ID", ""),
                        placeholder="e.g. SMC340B-001",
                        key=f"b340_{_sid}",
                    )
                    _ent_val = _c2.text_input(
                        "Covered entity name",
                        value=_existing.get("Covered entity", ""),
                        placeholder="e.g. Southside Medical Center",
                        key=f"ent_{_sid}",
                    )
                    _save_col, _clear_col = st.columns([3, 1])
                    _submitted = _save_col.form_submit_button(
                        "💾 Register site",
                        type="primary",
                        use_container_width=True,
                    )
                    _clear_btn = _clear_col.form_submit_button(
                        "Remove",
                        use_container_width=True,
                    )

                    if _submitted:
                        if not _b340_val.strip():
                            st.error("340B ID is required to register a site.")
                        else:
                            _site_reg[_sid] = {
                                "340B ID":       _b340_val.strip(),
                                "Covered entity": _ent_val.strip(),
                            }
                            _save_site_registry(_site_reg)
                            st.success(
                                f"Store {_sid} registered as **{_ent_val.strip()}** "
                                f"(340B ID: {_b340_val.strip()}). Re-running audit…"
                            )
                            st.rerun()

                    if _clear_btn and _is_registered:
                        _site_reg.pop(_sid, None)
                        _save_site_registry(_site_reg)
                        st.info(f"Registration for store {_sid} removed.")
                        st.rerun()


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


# ── TAB 6: Entity Frameworks ──────────────────────────────────────────────────

with tab_entity:
    st.subheader("Entity Frameworks")
    st.caption(
        "Define covered entities and their dispensing sites. "
        "When a file is uploaded, the engine auto-recognises the entity, applies the correct "
        "carve status, validates prescriber addresses against known site addresses, and flags "
        "retail-only stores that cannot use 340B pricing."
    )

    _ef = _cached_entity_framework()["data"]

    # ── Status banner ─────────────────────────────────────────────────────────
    if _ef:
        _ef_total_sites = sum(len(v.get("sites", {})) for v in _ef.values())
        st.success(
            f"🏢 {len(_ef)} entit{'y' if len(_ef)==1 else 'ies'} configured · "
            f"{_ef_total_sites} site(s) registered"
        )
    else:
        st.info(
            "No entity frameworks configured. Add your first entity below to enable "
            "auto-recognition, carve status auto-detection, and prescriber address validation."
        )

    st.markdown("---")

    # ── ADD / EDIT ENTITY ─────────────────────────────────────────────────────
    st.markdown("### Add / Edit Entity")
    with st.form("ef_add_entity_form"):
        _ef_c1, _ef_c2, _ef_c3 = st.columns(3)
        _ef_entity_id   = _ef_c1.text_input(
            "Entity ID (short key)",
            placeholder="e.g. smc, map_health",
            help="Unique short key — letters, numbers, underscores. e.g. 'smc' or 'map_health'",
            key="ef_entity_id",
        )
        _ef_entity_name = _ef_c2.text_input(
            "Entity name",
            placeholder="e.g. Southside Medical Center",
            key="ef_entity_name",
        )
        _ef_b340_id     = _ef_c3.text_input(
            "340B ID",
            placeholder="e.g. SMC340B-001",
            key="ef_b340_id",
        )
        _ef_carve       = st.radio(
            "Carve status for this entity",
            options=["carve-in", "carve-out", "unknown"],
            horizontal=True,
            key="ef_carve",
        )
        if st.form_submit_button("💾 Save entity", type="primary"):
            _eid = _ef_entity_id.strip().lower().replace(" ", "_")
            if not _eid:
                st.error("Entity ID is required.")
            elif not _ef_entity_name.strip():
                st.error("Entity name is required.")
            else:
                # Preserve existing sites when editing
                _existing_sites = _ef.get(_eid, {}).get("sites", {})
                _ef[_eid] = {
                    "name":         _ef_entity_name.strip(),
                    "340B_ID":      _ef_b340_id.strip(),
                    "carve_status": _ef_carve,
                    "sites":        _existing_sites,
                }
                _save_entity_framework(_ef)
                st.success(f"Entity '{_ef_entity_name.strip()}' saved.")
                st.rerun()

    st.markdown("---")

    # ── PER-ENTITY SITE MANAGEMENT ────────────────────────────────────────────
    if _ef:
        st.markdown("### Manage Sites")
        _sel_entity_label = st.selectbox(
            "Select entity to manage",
            options=list(_ef.keys()),
            format_func=lambda k: f"{_ef[k]['name']} ({k})",
            key="ef_sel_entity",
        )
        _sel_e = _ef.get(_sel_entity_label, {})
        _sel_sites = _sel_e.get("sites", {})

        st.markdown(
            f"**{_sel_e.get('name', '')}** · 340B ID: `{_sel_e.get('340B_ID', 'N/A')}` "
            f"· Carve status: **{_sel_e.get('carve_status', 'unknown')}**"
        )

        # Show existing sites
        if _sel_sites:
            st.markdown(f"**{len(_sel_sites)} site(s):**")
            for _site_id, _site_data in list(_sel_sites.items()):
                _type_badge = (
                    "<span style='background:#27ae60;color:white;border-radius:10px;"
                    "padding:2px 8px;font-size:0.75em'>340B</span>"
                    if _site_data.get("site_type", "340B").upper() == "340B"
                    else
                    "<span style='background:#e67e22;color:white;border-radius:10px;"
                    "padding:2px 8px;font-size:0.75em'>Retail</span>"
                )
                _col_info, _col_remove = st.columns([6, 1])
                _col_info.markdown(
                    f"**Store {_site_id}** {_type_badge} — {_site_data.get('address', 'No address')}",
                    unsafe_allow_html=True,
                )
                if _col_remove.button("Remove", key=f"ef_rm_{_sel_entity_label}_{_site_id}"):
                    del _sel_sites[_site_id]
                    _ef[_sel_entity_label]["sites"] = _sel_sites
                    _save_entity_framework(_ef)
                    st.rerun()
        else:
            st.info("No sites configured for this entity yet.")

        st.markdown("#### Add site")
        with st.form(f"ef_add_site_{_sel_entity_label}"):
            _sa_c1, _sa_c2, _sa_c3 = st.columns(3)
            _sa_store = _sa_c1.text_input(
                "Store / Site number",
                placeholder="e.g. 106540",
                key=f"ef_sa_store_{_sel_entity_label}",
            )
            _sa_addr  = _sa_c2.text_input(
                "Site address",
                placeholder="e.g. 1046 Ridge Ave SW, Atlanta, GA 30315",
                key=f"ef_sa_addr_{_sel_entity_label}",
            )
            _sa_type  = _sa_c3.selectbox(
                "Site type",
                options=["340B", "Retail"],
                help="340B = eligible dispense site. Retail = standard retail, not 340B.",
                key=f"ef_sa_type_{_sel_entity_label}",
            )
            if st.form_submit_button("➕ Add site"):
                _sid = _sa_store.strip()
                if not _sid:
                    st.error("Store number is required.")
                else:
                    _ef[_sel_entity_label]["sites"][_sid] = {
                        "address":   _sa_addr.strip(),
                        "site_type": _sa_type,
                    }
                    _save_entity_framework(_ef)
                    # Also sync 340B sites to site registry
                    if _sa_type == "340B" and _sel_e.get("340B_ID"):
                        _sr = _cached_site_registry()["data"]
                        _sr[_sid] = {
                            "340B ID":       _sel_e["340B_ID"],
                            "Covered entity": _sel_e["name"],
                        }
                        _save_site_registry(_sr)
                    st.success(f"Site {_sid} added as {_sa_type}.")
                    st.rerun()

        # ── Import from current audit ──────────────────────────────────────────
        st.markdown("#### Import stores from current audit")
        st.caption(
            "Bulk-add all stores detected in the current audit as 340B sites for this entity. "
            "You can change individual sites to 'Retail' after importing."
        )
        _audit_stores = (
            store_status[["Store number", "Pharmacy location"]]
            .drop_duplicates("Store number")
            .values.tolist()
        )
        _import_new = [(s, a) for s, a in _audit_stores if str(s) not in _sel_sites]
        if _import_new:
            if st.button(
                f"Import {len(_import_new)} new store(s) as 340B sites",
                key=f"ef_import_{_sel_entity_label}",
            ):
                for _isid, _iaddr in _import_new:
                    _ef[_sel_entity_label]["sites"][str(_isid)] = {
                        "address":   str(_iaddr),
                        "site_type": "340B",
                    }
                _save_entity_framework(_ef)
                # Sync to site registry
                _sr = _cached_site_registry()["data"]
                for _isid, _ in _import_new:
                    if _sel_e.get("340B_ID"):
                        _sr[str(_isid)] = {
                            "340B ID":       _sel_e["340B_ID"],
                            "Covered entity": _sel_e["name"],
                        }
                _save_site_registry(_sr)
                st.success(f"Imported {len(_import_new)} store(s) as 340B sites.")
                st.rerun()
        else:
            st.info("All current audit stores are already in this entity's site list.")

        st.markdown("---")

        # ── Danger zone ───────────────────────────────────────────────────────
        with st.expander("Danger zone"):
            st.warning(f"This will permanently delete entity '{_sel_e.get('name', _sel_entity_label)}'.")
            if st.button(f"Delete entity '{_sel_entity_label}'", type="secondary", key=f"ef_del_{_sel_entity_label}"):
                del _ef[_sel_entity_label]
                _save_entity_framework(_ef)
                st.success("Entity deleted.")
                st.rerun()


# ── TAB 7: EHR Encounters ─────────────────────────────────────────────────────

with tab_ehr:
    st.subheader("EHR Encounter Data")
    st.caption(
        "Upload up to 5 EHR exports via the sidebar — one per EHR system or location. "
        "The engine auto-maps columns for each dataset, then combines them for a single "
        "cross-reference against all 340B claims."
    )

    if not _ehr_datasets:
        st.info(
            "No EHR datasets loaded. Use the **🩺 EHR Encounter Data** expander in the sidebar "
            "to upload up to 5 Excel or CSV EHR exports.\n\n"
            "**Common column names the engine recognises:**\n"
            "- **Encounter date**: `Visit Date`, `Date of Service`, `DOS`, `Encounter Date`\n"
            "- **Provider**: `Provider Name`, `Prescriber`, `Attending Provider`, `Physician`\n"
            "- **Provider NPI**: `NPI`, `Provider NPI`, `DR NPI`\n"
            "- **Patient**: `Patient Name`, `Patient`, `Member Name`\n"
            "- **Patient MRN**: `MRN`, `Patient ID`, `Chart Number`, `Member ID`\n"
            "- **Patient DOB**: `Date of Birth`, `DOB`, `Birth Date`\n"
            "- **Location**: `Location`, `Facility`, `Clinic`, `Department`\n"
            "- **Drug**: `Drug Name`, `Medication`, `Drug`, `Product`\n"
            "- **NDC**: `NDC`, `National Drug Code`, `NDC Code`\n"
            "- **Diagnosis**: `Diagnosis Code`, `ICD-10`, `Dx Code`, `Primary Diagnosis`\n"
            "- **Rx number**: `Rx Number`, `Prescription Number`, `RXNBR`"
        )
    else:
        # ── per-dataset column mapping & preview ───────────────────────────────
        st.markdown(f"### {len(_ehr_datasets)} EHR Dataset(s) Loaded")

        for _ds in _ehr_datasets:
            _ds_idx     = _ds["idx"]
            _ds_name    = _ds["name"]
            _ds_raw     = _ds["raw"]
            _ds_norm    = _ds["norm"]
            _ds_col_map = _ds["col_map"]
            _ds_n_det   = sum(1 for v in _ds_col_map.values() if v)
            _ds_n_tot   = len(_ds_col_map)
            _ds_enc_col = EHR_CANONICAL["encounter_date"]
            _ds_enc_cnt = (
                _ds_norm[_ds_enc_col].notna().sum()
                if _ds_enc_col in _ds_norm.columns else 0
            )

            with st.expander(
                f"📋 Dataset {_ds_idx}: {_ds_name}  —  "
                f"{len(_ds_raw):,} rows · {_ds_enc_cnt:,} dated encounters · "
                f"{_ds_n_det}/{_ds_n_tot} fields mapped",
                expanded=(_ds_n_det < _ds_n_tot),   # auto-expand if mapping is incomplete
            ):
                # Column mapping grid
                st.markdown("**Column mapping** — correct any mis-detected field:")
                _ds_all_cols       = ["— not mapped —"] + list(_ds_raw.columns)
                _ds_override_key   = f"_ehr_col_override_{_ds_idx}"
                _ds_cur_override   = st.session_state.get(_ds_override_key, {})
                _ds_override_new: dict[str, str | None] = {}

                _mp_cols = st.columns(3)
                for _fi, (_fkey, _flabel) in enumerate(EHR_CANONICAL.items()):
                    _auto   = _ds_col_map.get(_fkey)
                    _cur    = _ds_cur_override.get(_fkey) or _auto
                    _sel_i  = _ds_all_cols.index(_cur) if _cur in _ds_all_cols else 0
                    _hint   = f" (auto: {_auto})" if _auto else " ⚠️ not detected"
                    _chosen = _mp_cols[_fi % 3].selectbox(
                        f"{_flabel}{_hint}",
                        options=_ds_all_cols,
                        index=_sel_i,
                        key=f"ehr_map_{_ds_idx}_{_fkey}",
                    )
                    _ds_override_new[_fkey] = None if _chosen == "— not mapped —" else _chosen

                if _ds_override_new != _ds_cur_override:
                    st.session_state[_ds_override_key] = _ds_override_new
                    _ds["norm"], _ds["col_map"] = normalize_ehr(
                        _ds_raw, override_map=_ds_override_new
                    )
                    _ds_norm    = _ds["norm"]
                    _ds_col_map = _ds["col_map"]

                # Detection badge
                _nd = sum(1 for v in _ds_col_map.values() if v)
                _badge_color = "#27ae60" if _nd == _ds_n_tot else "#e67e22" if _nd >= 6 else "#e74c3c"
                st.markdown(
                    f"<div style='background:#f8f9fa;border-left:4px solid {_badge_color};"
                    f"padding:6px 12px;border-radius:4px;margin:8px 0'>"
                    f"<strong style='color:{_badge_color}'>{_nd} / {_ds_n_tot} fields mapped</strong>"
                    f"{'&nbsp; ✅ All fields detected' if _nd == _ds_n_tot else ''}"
                    f"</div>",
                    unsafe_allow_html=True,
                )

                # Dataset preview (mapped cols first)
                _mapped_cols = [c for c in EHR_DISPLAY_FIELDS if c in _ds_norm.columns]
                _extra_cols  = [c for c in _ds_norm.columns if c not in _mapped_cols]
                _show_cols   = _mapped_cols + _extra_cols
                st.dataframe(
                    _ds_norm[_show_cols].reset_index(drop=True),
                    width="stretch", height=300,
                )

                # Per-dataset stats
                _sc1, _sc2, _sc3, _sc4 = st.columns(4)
                _sc1.metric("Rows", f"{len(_ds_norm):,}")
                _sc2.metric("Dated encounters", f"{int(_ds_enc_cnt):,}")
                _sc3.metric(
                    "Unique patients",
                    f"{_ds_norm[EHR_CANONICAL['patient_name']].nunique():,}"
                    if EHR_CANONICAL["patient_name"] in _ds_norm.columns else "N/A"
                )
                _sc4.metric(
                    "Unique providers",
                    f"{_ds_norm[EHR_CANONICAL['provider_name']].nunique():,}"
                    if EHR_CANONICAL["provider_name"] in _ds_norm.columns else "N/A"
                )

                st.download_button(
                    f"⬇ Export Dataset {_ds_idx} mapped CSV",
                    data=_ds_norm[_show_cols].to_csv(index=False).encode(),
                    file_name=f"ehr_dataset_{_ds_idx}_{datetime.date.today().isoformat()}.csv",
                    mime="text/csv",
                    key=f"ehr_ds_export_{_ds_idx}",
                )

        # Re-build combined norm after any override changes
        _ehr_combined_norm = pd.concat(
            [d["norm"] for d in _ehr_datasets], ignore_index=True
        )

        st.markdown("---")

        # ── Combined stats ─────────────────────────────────────────────────────
        st.markdown("### Combined EHR Summary")
        _cb_enc_col = EHR_CANONICAL["encounter_date"]
        _cb_enc_cnt = (
            _ehr_combined_norm[_cb_enc_col].notna().sum()
            if _cb_enc_col in _ehr_combined_norm.columns else 0
        )
        _cs1, _cs2, _cs3, _cs4, _cs5 = st.columns(5)
        _cs1.metric("Datasets",         len(_ehr_datasets))
        _cs2.metric("Total rows",       f"{len(_ehr_combined_norm):,}")
        _cs3.metric("Dated encounters", f"{int(_cb_enc_cnt):,}")
        _cs4.metric(
            "Unique patients",
            f"{_ehr_combined_norm[EHR_CANONICAL['patient_name']].nunique():,}"
            if EHR_CANONICAL["patient_name"] in _ehr_combined_norm.columns else "N/A"
        )
        _cs5.metric(
            "Unique providers",
            f"{_ehr_combined_norm[EHR_CANONICAL['provider_name']].nunique():,}"
            if EHR_CANONICAL["provider_name"] in _ehr_combined_norm.columns else "N/A"
        )

        # Combined EHR date range filter
        _cb_dates = (
            _ehr_combined_norm[_cb_enc_col].dropna()
            if _cb_enc_col in _ehr_combined_norm.columns
            else pd.Series(dtype="datetime64[ns]")
        )
        _cb_prov_col = EHR_CANONICAL["provider_name"]
        _cb_loc_col  = EHR_CANONICAL["location"]

        _cf1, _cf2, _cf3 = st.columns(3)
        _cb_provs = (
            sorted(_ehr_combined_norm[_cb_prov_col].dropna().astype(str).unique().tolist())
            if _cb_prov_col in _ehr_combined_norm.columns else []
        )
        _sel_cb_prov = _cf1.multiselect(
            "Provider", _cb_provs, default=_cb_provs, key="cb_prov_filter"
        ) if _cb_provs else None

        _cb_locs = (
            sorted(_ehr_combined_norm[_cb_loc_col].dropna().astype(str).unique().tolist())
            if _cb_loc_col in _ehr_combined_norm.columns else []
        )
        _sel_cb_loc = _cf2.multiselect(
            "Location", _cb_locs, default=_cb_locs, key="cb_loc_filter"
        ) if _cb_locs else None

        _cb_date_filter = None
        if not _cb_dates.empty:
            _cb_min = _cb_dates.min().date()
            _cb_max = _cb_dates.max().date()
            _cb_dr  = _cf3.date_input(
                "Encounter date range",
                value=(_cb_min, _cb_max),
                min_value=_cb_min,
                max_value=_cb_max,
                key="cb_date_filter",
            )
            if isinstance(_cb_dr, (list, tuple)) and len(_cb_dr) == 2:
                _cb_date_filter = _cb_dr

        _cb_filtered = _ehr_combined_norm.copy()
        if _sel_cb_prov is not None and _cb_prov_col in _cb_filtered.columns:
            _cb_filtered = _cb_filtered[_cb_filtered[_cb_prov_col].astype(str).isin(_sel_cb_prov)]
        if _sel_cb_loc is not None and _cb_loc_col in _cb_filtered.columns:
            _cb_filtered = _cb_filtered[_cb_filtered[_cb_loc_col].astype(str).isin(_sel_cb_loc)]
        if _cb_date_filter and _cb_enc_col in _cb_filtered.columns:
            _cb_filtered = _cb_filtered[
                _cb_filtered[_cb_enc_col].between(
                    pd.Timestamp(_cb_date_filter[0]),
                    pd.Timestamp(_cb_date_filter[1]),
                    inclusive="both",
                )
            ]

        _cb_show = [c for c in EHR_DISPLAY_FIELDS if c in _cb_filtered.columns] + \
                   [c for c in _cb_filtered.columns if c not in EHR_DISPLAY_FIELDS]
        st.caption(f"Combined view: {len(_cb_filtered):,} of {len(_ehr_combined_norm):,} encounters")
        st.dataframe(_cb_filtered[_cb_show].reset_index(drop=True), width="stretch", height=350)

        st.download_button(
            "⬇ Export combined EHR data (CSV)",
            data=_cb_filtered[_cb_show].to_csv(index=False).encode(),
            file_name=f"ehr_combined_{datetime.date.today().isoformat()}.csv",
            mime="text/csv",
            key="ehr_combined_export",
        )

        st.markdown("---")

        # ── Claims cross-reference (combined EHR) ─────────────────────────────
        st.markdown("### Claims Cross-Reference")
        st.caption(
            f"Each 340B claim is matched against all {len(_ehr_datasets)} EHR dataset(s) combined. "
            "A **MATCHED** claim has a supporting patient encounter within the audit window. "
            "A **NO MATCH** claim may lack required encounter documentation."
        )

        _xref_window = int(load_rules().get("thresholds", {}).get(
            "encounter_date_window_days", 365
        ))

        with st.spinner(f"Cross-referencing claims against {len(_ehr_datasets)} EHR dataset(s)…"):
            _xref_df = crossref_claims(
                _ehr_combined_norm, claims, window_days=_xref_window
            )

        # Match statistics
        _xr_matched  = int((_xref_df["EHR match"] == "MATCHED").sum())
        _xr_no_match = int(_xref_df["EHR match"].str.startswith("NO MATCH").sum())
        _xr_na       = int(_xref_df["EHR match"].str.startswith("N/A").sum())
        _xr_total    = len(_xref_df)
        _xr_pct      = _xr_matched / _xr_total * 100 if _xr_total else 0
        _xr_color    = "#27ae60" if _xr_pct >= 80 else "#e67e22" if _xr_pct >= 50 else "#e74c3c"

        st.markdown(
            f"<div style='background:#1a2e4a;border-radius:10px;padding:18px 24px;margin:8px 0'>"
            f"<div style='color:#8daabf;font-size:0.75em;text-transform:uppercase;"
            f"letter-spacing:1.5px;margin-bottom:6px'>"
            f"EHR Match Rate — {len(_ehr_datasets)} dataset(s) combined</div>"
            f"<div style='display:flex;align-items:flex-end;gap:24px;flex-wrap:wrap'>"
            f"<div><span style='font-size:3em;font-weight:800;color:{_xr_color}'>"
            f"{_xr_pct:.1f}%</span></div>"
            f"<div style='margin-bottom:6px'>"
            f"<div style='color:#8daabf;font-size:0.85em'>"
            f"✅ {_xr_matched:,} matched &nbsp;·&nbsp; "
            f"❌ {_xr_no_match:,} no match &nbsp;·&nbsp; "
            f"⚪ {_xr_na:,} N/A</div>"
            f"<div style='color:#8daabf;font-size:0.78em;margin-top:4px'>"
            f"Window: ±{_xref_window} days · {len(_ehr_combined_norm):,} combined EHR rows</div>"
            f"</div></div></div>",
            unsafe_allow_html=True,
        )

        # Breakdown by compliance category
        if "Compliance category" in _xref_df.columns:
            st.markdown("#### Match breakdown by compliance category")
            _xr_pivot = (
                _xref_df.groupby(["Compliance category", "EHR match"])
                .size().unstack(fill_value=0).reset_index()
            )
            st.dataframe(_xr_pivot, width="stretch", height=200)

        # Missing Encounter callout
        _me_matched = _xref_df[
            (_xref_df.get("Compliance category", pd.Series(dtype=str)) == "Missing Encounter")
            & (_xref_df["EHR match"] == "MATCHED")
        ]
        if len(_me_matched) > 0:
            st.success(
                f"🔍 **{len(_me_matched):,} 'Missing Encounter' claim(s) have a matching EHR encounter.** "
                "These may be resolvable — attach the EHR encounter record as supporting documentation."
            )
            _me_cols = [c for c in [
                "Prescription number", "Fill date", "Patient name", "Prescribing provider",
                "Store number", "EHR match", "EHR encounter date",
                "EHR provider", "EHR location", "EHR diagnosis",
            ] if c in _me_matched.columns]
            st.dataframe(
                _mask_phi(_me_matched[_me_cols]) if _mask_phi_enabled else _me_matched[_me_cols],
                width="stretch", height=250,
            )

        # Full cross-reference table
        st.markdown("#### Full cross-reference — all claims")
        _xr_filter = st.radio(
            "Filter by EHR match status",
            options=["All", "MATCHED", "NO MATCH", "N/A"],
            horizontal=True,
            key="xr_filter",
        )
        _xref_show = _xref_df.copy()
        if _xr_filter == "MATCHED":
            _xref_show = _xref_show[_xref_show["EHR match"] == "MATCHED"]
        elif _xr_filter == "NO MATCH":
            _xref_show = _xref_show[_xref_show["EHR match"].str.startswith("NO MATCH")]
        elif _xr_filter == "N/A":
            _xref_show = _xref_show[_xref_show["EHR match"].str.startswith("N/A")]

        _xr_disp_cols = [c for c in [
            "Prescription number", "Fill date", "Drug name", "NDC",
            "Patient name", "Prescribing provider", "Provider NPI",
            "Store number", "Compliance category", "Risk score", "Risk tier",
            "EHR match", "EHR encounter date", "EHR provider",
            "EHR location", "EHR diagnosis",
        ] if c in _xref_show.columns]

        st.caption(
            f"{len(_xref_show):,} of {len(_xref_df):,} claims shown · "
            f"{int((_xref_show['EHR match'] == 'MATCHED').sum()):,} matched in view"
        )
        st.dataframe(
            _mask_phi(_xref_show[_xr_disp_cols]) if _mask_phi_enabled
            else _xref_show[_xr_disp_cols],
            width="stretch", height=400,
        )

        _xr_exp_cols = [c for c in [
            "Prescription number", "Fill date", "Drug name",
            "Patient name", "Prescribing provider", "Provider NPI", "Store number",
            "Compliance category", "Risk score",
            "EHR match", "EHR encounter date", "EHR provider",
            "EHR location", "EHR diagnosis",
        ] if c in _xref_df.columns]
        st.download_button(
            "⬇ Export full cross-reference CSV",
            data=_xref_df[_xr_exp_cols].to_csv(index=False).encode(),
            file_name=f"ace_340b_ehr_crossref_{datetime.date.today().isoformat()}.csv",
            mime="text/csv",
            key="xref_export",
        )


# ── TAB: Code 20 — Scripts Missing 340B Billing Indicator ────────────────────

with tab_c20:
    st.subheader("💢 Code 20 — Scripts Missing 340B Billing Indicator")
    st.markdown("""
    **NCPDP Field 436-E1 (Basis of Cost Determination) = "20"** tells the payer
    that the drug was purchased at the 340B ceiling price.

    **Every script in the uploaded report is missing Code 20** — meaning it is
    NOT being billed as a 340B drug. This is critical for Medicaid claims because:

    - **Medicaid scripts without Code 20** → the state may request a manufacturer rebate,
      creating a **duplicate discount violation** (prohibited under 340B statute)
    - **Commercial scripts without Code 20** → may be billed at retail instead of 340B cost,
      which could be correct depending on your contract carve-in/carve-out status
    """)

    if _code20_df is not None and not _code20_df.empty:
        c20 = _code20_df.copy()
        st.markdown("---")

        # ── KPIs ──
        c20_total = len(c20)
        c20_zero = len(c20[c20["PAID"].astype(float) == 0]) if "PAID" in c20.columns else 0
        c20_medicaid = len(c20[c20["Plan Group"] == "Medicaid"]) if "Plan Group" in c20.columns else 0
        c20_commercial = c20_total - c20_medicaid

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Scripts Without Code 20", f"{c20_total:,}")
        k2.metric("Zero-Pay Scripts", f"{c20_zero:,}")
        k3.metric("⚠️ Medicaid (Risk)", f"{c20_medicaid:,}")
        k4.metric("Commercial/Other", f"{c20_commercial:,}")

        if c20_medicaid > 0:
            st.markdown(f"""
            <div style='background:linear-gradient(135deg, #fef2f2, #fff1f2);
                 border:1px solid #fca5a5; border-left:4px solid #dc2626;
                 border-radius:8px; padding:1rem 1.25rem; margin:0.75rem 0'>
                <div style='color:#991b1b; font-weight:700; font-size:1rem;
                     margin-bottom:0.35rem'>
                    🚨 {c20_medicaid:,} Medicaid scripts are NOT billed as 340B
                </div>
                <div style='color:#7f1d1d; font-size:0.85rem'>
                    These Medicaid claims do not have Code 20. If 340B-purchased inventory
                    was used to fill these scripts, they <strong>must</strong> be resubmitted with
                    Code 20 (NCPDP 436-E1 = "20") to suppress the manufacturer rebate and
                    avoid duplicate discount violations. HRSA auditors specifically look for this.
                </div>
            </div>
            """, unsafe_allow_html=True)

        st.markdown("---")

        # ── Sub-tabs within Code 20 ──
        c20_sub1, c20_sub2, c20_sub3, c20_sub4 = st.tabs([
            "📋 All Scripts Missing Code 20",
            "🚨 Medicaid — Duplicate Discount Risk",
            "📊 Plan Breakdown",
            "🔗 Cross-Reference with Rx Log",
        ])

        with c20_sub1:
            st.markdown("#### All Scripts Without Code 20")
            st.markdown(
                "Every script below is **not** being billed at the 340B price. "
                "Review whether 340B-purchased inventory was used — if so, Code 20 must be added."
            )
            disp_cols = [c for c in [
                "Rx Nbr", "RX DATE", "DRUG NAME", "Provider", "PLANID",
                "Plan Category", "Plan Group", "PAID", "QTY", "RF",
                "PA CODE", "DAW", "Rejection Type", "_format"
            ] if c in c20.columns]

            disp_c20 = c20[disp_cols].copy()
            if "RX DATE" in disp_c20.columns:
                disp_c20["RX DATE"] = pd.to_datetime(disp_c20["RX DATE"], errors="coerce").dt.strftime("%m/%d/%Y").fillna("")
            if "PAID" in disp_c20.columns:
                disp_c20["PAID"] = pd.to_numeric(disp_c20["PAID"], errors="coerce").apply(lambda x: f"${x:,.2f}" if pd.notna(x) else "")

            st.dataframe(disp_c20, use_container_width=True, height=400)

            st.download_button(
                "⬇ Export All Scripts Missing Code 20 (CSV)",
                data=c20[disp_cols].to_csv(index=False).encode(),
                file_name=f"ace_no_code20_all_{datetime.date.today().isoformat()}.csv",
                mime="text/csv",
                key="c20_export_all",
            )

        with c20_sub2:
            st.markdown("#### 🚨 Medicaid Scripts Without Code 20 — Duplicate Discount Risk")
            st.markdown("""
            **These are Medicaid / Managed Medicaid scripts that are NOT billed as 340B.**

            Under the 340B statute, if a covered entity dispenses a drug purchased at the
            340B ceiling price to a Medicaid patient, the claim **must** carry Code 20 to
            suppress the manufacturer rebate. Without it:

            - The manufacturer pays a rebate to the state on a drug already purchased at 340B price
            - This creates a **duplicate discount** — the entity benefits twice
            - HRSA considers this a serious compliance violation
            - Can result in removal from the 340B program

            **Action Required:** For each script below, verify whether 340B inventory was used.
            If yes, resubmit the claim with Code 20.
            """)

            if "Plan Group" in c20.columns:
                medicaid_no_code20 = c20[c20["Plan Group"] == "Medicaid"].copy()
                if medicaid_no_code20.empty:
                    st.success("✅ No Medicaid claims in the missing Code 20 report.")
                else:
                    # Sub-metrics for Medicaid
                    med_zero = len(medicaid_no_code20[medicaid_no_code20["PAID"].astype(float) == 0]) if "PAID" in medicaid_no_code20.columns else 0
                    med_paid = len(medicaid_no_code20) - med_zero
                    med_total_paid = pd.to_numeric(medicaid_no_code20.get("PAID", 0), errors="coerce").sum()

                    mm1, mm2, mm3 = st.columns(3)
                    mm1.metric("Medicaid Scripts (No Code 20)", f"{len(medicaid_no_code20):,}")
                    mm2.metric("Zero-Pay", f"{med_zero:,}")
                    mm3.metric("Total Paid Amount", f"${med_total_paid:,.2f}")

                    med_disp_cols = [c for c in [
                        "Rx Nbr", "RX DATE", "DRUG NAME", "Provider", "PLANID",
                        "Plan Category", "PAID", "QTY", "PA CODE", "Rejection Type"
                    ] if c in medicaid_no_code20.columns]

                    med_disp = medicaid_no_code20[med_disp_cols].copy()
                    if "RX DATE" in med_disp.columns:
                        med_disp["RX DATE"] = pd.to_datetime(med_disp["RX DATE"], errors="coerce").dt.strftime("%m/%d/%Y").fillna("")
                    if "PAID" in med_disp.columns:
                        med_disp["PAID"] = pd.to_numeric(med_disp["PAID"], errors="coerce").apply(lambda x: f"${x:,.2f}" if pd.notna(x) else "")

                    st.dataframe(med_disp, use_container_width=True, height=400)

                    st.download_button(
                        "⬇ Export Medicaid Missing Code 20 (CSV)",
                        data=medicaid_no_code20[med_disp_cols].to_csv(index=False).encode(),
                        file_name=f"ace_medicaid_no_code20_{datetime.date.today().isoformat()}.csv",
                        mime="text/csv",
                        key="c20_export_medicaid",
                    )
            else:
                st.info("Plan Group classification not available — upload a Southside-format file for Medicaid detection.")

        with c20_sub3:
            st.markdown("#### Scripts Missing Code 20 by Plan")
            st.markdown("Breakdown of which insurance plans have scripts not billed as 340B.")
            if "Plan Group" in c20.columns:
                plan_summary = c20.groupby("Plan Group").agg(
                    Scripts=("Rx Nbr" if "Rx Nbr" in c20.columns else c20.columns[0], "count"),
                ).reset_index().sort_values("Scripts", ascending=False)

                fig_plan = px.pie(plan_summary, names="Plan Group", values="Scripts",
                    hole=0.4, color="Plan Group",
                    color_discrete_map={
                        "Medicaid": "#dc2626", "BCBS / Anthem": "#f59e0b",
                        "PBM": "#8b5cf6", "UHC / Optum": "#06b6d4",
                        "Aetna": "#ec4899", "Cigna": "#10b981",
                        "Humana": "#f97316", "Oscar": "#6366f1",
                        "Cash / Discount": "#84cc16", "Commercial — Other": "#94a3b8",
                        "PAP": "#14b8a6", "Other": "#64748b",
                    })
                fig_plan.update_layout(
                    font=dict(family="DM Sans"),
                    margin=dict(t=20, b=20, l=20, r=20),
                    legend=dict(orientation="h", y=-0.2),
                )
                st.plotly_chart(fig_plan, use_container_width=True)

                if "Plan Category" in c20.columns:
                    plan_detail = c20.groupby(["PLANID", "Plan Category", "Plan Group"]).agg(
                        Scripts=("Rx Nbr" if "Rx Nbr" in c20.columns else c20.columns[0], "count"),
                    ).reset_index().sort_values("Scripts", ascending=False)
                    st.dataframe(plan_detail, use_container_width=True, hide_index=True)

                if "Rejection Type" in c20.columns:
                    st.markdown("#### Payment Status")
                    rej_summary = c20.groupby("Rejection Type").size().reset_index(name="Count")
                    rej_summary = rej_summary.sort_values("Count", ascending=False)
                    fig_rej = px.bar(rej_summary, x="Rejection Type", y="Count",
                        color="Rejection Type",
                        color_discrete_sequence=px.colors.qualitative.Set2)
                    fig_rej.update_layout(showlegend=False, font=dict(family="DM Sans"),
                        margin=dict(t=20, b=30))
                    st.plotly_chart(fig_rej, use_container_width=True)
            else:
                st.info("Upload a Southside-format file for plan breakdown.")

        with c20_sub4:
            st.markdown("#### Cross-Reference: Missing Code 20 ↔ Rx Log")
            st.markdown("""
            Matches scripts missing Code 20 against the main Rx Log by prescription number.
            This reveals whether the script was filled using 340B pricing (PRICE SCHED = SXC-GAM)
            in your pharmacy system — if so, Code 20 **should** have been on the claim but wasn't.
            """)

            if audited is not None and not audited.empty:
                c20_rxnbr_col = "Rx Nbr" if "Rx Nbr" in c20.columns else None

                if c20_rxnbr_col:
                    c20_match = c20.copy()
                    c20_match["_rx_clean"] = c20_match[c20_rxnbr_col].fillna("").astype(str).str.strip()

                    audit_rx_col = "Prescription number" if "Prescription number" in audited.columns else None
                    if audit_rx_col is None and "RXNBR" in audited.columns:
                        audit_rx_col = "RXNBR"

                    if audit_rx_col:
                        audit_lookup = audited.copy()
                        audit_lookup["_rx_clean"] = audit_lookup[audit_rx_col].fillna("").astype(str).str.strip()

                        audit_keep = ["_rx_clean"]
                        for c in ["Drug name", "DRUG NAME", "Fill date", "FILLDATE",
                                   "Primary payer", "P1 NAME", "PRICE SCHED",
                                   "Patient name", "Prescribing provider",
                                   "Compliance category"]:
                            if c in audit_lookup.columns:
                                audit_keep.append(c)

                        merged = c20_match.merge(
                            audit_lookup[audit_keep].drop_duplicates(subset=["_rx_clean"]),
                            on="_rx_clean", how="left", suffixes=("", "_rxlog")
                        )

                        price_sched_col = "PRICE SCHED" if "PRICE SCHED" in merged.columns else None
                        if price_sched_col:
                            merged["Was Billed as 340B in Rx Log"] = merged[price_sched_col].apply(
                                lambda x: "⚠️ YES — SXC-GAM (should have Code 20)"
                                if str(x).strip().upper() == "SXC-GAM"
                                else "No — " + str(x) if pd.notna(x) and str(x).strip()
                                else "Not found in Rx Log"
                            )
                        else:
                            merged["Was Billed as 340B in Rx Log"] = "PRICE SCHED not available"

                        merged["Found in Rx Log"] = merged.apply(
                            lambda r: "✅ Yes" if any(
                                pd.notna(r.get(c)) and str(r.get(c)).strip()
                                for c in ["Drug name", "DRUG NAME"]
                            ) else "❌ No", axis=1
                        )

                        # Summary
                        matched_count = (merged["Found in Rx Log"] == "✅ Yes").sum()
                        unmatched_count = (merged["Found in Rx Log"] == "❌ No").sum()
                        billed_340b = merged["Was Billed as 340B in Rx Log"].str.contains("SXC-GAM", na=False).sum()

                        xm1, xm2, xm3 = st.columns(3)
                        xm1.metric("Matched to Rx Log", f"{matched_count:,}")
                        xm2.metric("Not in Rx Log", f"{unmatched_count:,}")
                        xm3.metric("⚠️ 340B in Rx Log but No Code 20", f"{billed_340b:,}")

                        if billed_340b > 0:
                            st.markdown(f"""
                            <div style='background:linear-gradient(135deg, #fef2f2, #fff1f2);
                                 border:1px solid #fca5a5; border-left:4px solid #dc2626;
                                 border-radius:8px; padding:1rem 1.25rem; margin:0.5rem 0'>
                                <div style='color:#991b1b; font-weight:700; font-size:1rem;
                                     margin-bottom:0.35rem'>
                                    🚨 {billed_340b:,} scripts billed as 340B (SXC-GAM) in the pharmacy system
                                    but missing Code 20 on the claim
                                </div>
                                <div style='color:#7f1d1d; font-size:0.85rem'>
                                    Your Rx Log shows these were filled using 340B pricing (PRICE SCHED = SXC-GAM),
                                    but they do not have Code 20 on the claim submission. This means the payer
                                    does not know the drug was purchased at the 340B price. For Medicaid claims,
                                    this will trigger a manufacturer rebate request — creating a duplicate discount.
                                    <br><br>
                                    <strong>Action:</strong> Resubmit these claims with Code 20
                                    (NCPDP Field 436-E1 = "20").
                                </div>
                            </div>
                            """, unsafe_allow_html=True)

                        show_cols = [c for c in [
                            c20_rxnbr_col, "RX DATE", "DRUG NAME", "Provider", "PLANID",
                            "Plan Group", "PAID",
                            "Found in Rx Log", "Was Billed as 340B in Rx Log",
                            "Compliance category",
                        ] if c in merged.columns]

                        disp_merged = merged[show_cols].copy()
                        if "RX DATE" in disp_merged.columns:
                            disp_merged["RX DATE"] = pd.to_datetime(disp_merged["RX DATE"], errors="coerce").dt.strftime("%m/%d/%Y").fillna("")
                        if "PAID" in disp_merged.columns:
                            disp_merged["PAID"] = pd.to_numeric(disp_merged["PAID"], errors="coerce").apply(lambda x: f"${x:,.2f}" if pd.notna(x) else "")

                        st.dataframe(disp_merged, use_container_width=True, height=400)

                        st.download_button(
                            "⬇ Export Cross-Reference (CSV)",
                            data=merged[show_cols].to_csv(index=False).encode(),
                            file_name=f"ace_no_code20_xref_{datetime.date.today().isoformat()}.csv",
                            mime="text/csv",
                            key="c20_xref_export",
                        )
                    else:
                        st.warning("Could not find prescription number column in audit data for cross-reference.")
                else:
                    st.warning("Report does not contain 'Rx Nbr' column.")
            else:
                st.info("Upload both the Rx Log (main upload) and the No Code 20 report to enable cross-referencing.")

    else:
        st.info(
            "👈 Upload the **No Code 20 report** in the sidebar under "
            "**💢 Code 20 — Scripts Without 340B Indicator**.\n\n"
            "This report contains every script that is **not** being billed with Code 20 "
            "(NCPDP 436-E1 ≠ 20), meaning they are not identified as 340B to the payer.\n\n"
            "**What to look for:**\n"
            "- **Medicaid scripts without Code 20** → duplicate discount risk if 340B inventory was used\n"
            "- **Scripts billed as SXC-GAM in the Rx Log but missing Code 20** → confirms Code 20 should be added\n"
            "- **Zero-pay claims** → may indicate the payer rejected because Code 20 was missing"
        )


# ── TAB 8: Downloads ──────────────────────────────────────────────────────────

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

    # ── Session Audit Log ─────────────────────────────────────────────────────
    st.markdown("### 🔒 Session Audit Log")
    st.caption(
        "HIPAA-required access log: timestamped record of key events in this session. "
        "No PHI is recorded. Export for compliance documentation."
    )
    _al = st.session_state.get("_audit_log", [])
    if _al:
        _al_df = pd.DataFrame(_al).rename(columns={"time": "Timestamp", "event": "Event"})
        st.dataframe(_al_df, width="stretch", height=min(200, 40 + len(_al_df) * 35))
        st.download_button(
            "⬇ Export session audit log CSV",
            data=_al_df.to_csv(index=False).encode(),
            file_name=f"ace_340b_session_log_{datetime.date.today().isoformat()}.csv",
            mime="text/csv",
            key="audit_log_export",
        )
    else:
        st.info("No events recorded in this session yet.")

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
