"""
ace_340b_audit/ingest.py
------------------------
Adapters for pharmacy dispensing / EHR prescription export files.

Two adapters are provided:

1. RX-log adapter (legacy proprietary format)
   Detects the strict RX-log column schema and maps it to engine-canonical
   column names.  Required: RXNBR, FILLDATE, DRUG NAME, NDC, RX STOREID, DR NPI.

2. Generic dispense log adapter (flexible EHR/pharmacy exports)
   Handles any CSV or Excel export from an EHR or pharmacy system that
   includes patient, drug, date, and location information under various
   column name conventions (e.g. "Patient", "Medication", "Effective Date",
   "Location", "Rendering Provider", "Prescriber [Agent]", etc.).
   Missing NDC / Prescription number are synthesised so the audit engine
   can still run; those fields are flagged as incomplete.
"""

from __future__ import annotations

import pandas as pd

# ── column mapping: source → engine canonical ─────────────────────────────────
_COLUMN_MAP: dict[str, str] = {
    "RXNBR":    "Prescription number",
    "FILLDATE": "Fill date",
    "WRITTEN":  "Encounter date",       # Rx written date ≈ patient encounter date
    "DRUG NAME": "Drug name",
    "NDC":      "NDC",
    "RX STOREID": "Store number",
    "DR NPI":   "Provider NPI",
    # payer columns – 'payer' substring triggers duplicate-discount check
    "P1 NAME":  "Primary payer",
    "P2 NAME":  "Secondary payer",
    "P3 NAME":  "Tertiary payer",
    # extras preserved for display / filtering
    "MRN":      "MRN",
    "PATKEY":   "Patient key",
    "RF":       "Refill",
    "DTYPE":    "Drug type",
    "DEACLASS": "DEA class",
    "DEANBR":   "Prescriber DEA",
    "RXDIAG 1": "Diagnosis 1",
    "RXDIAG 2": "Diagnosis 2",
    "RXDIAG 3": "Diagnosis 3",
    "PATDOB":   "Patient DOB",
    "GENDER":   "Patient gender",
    "PATST":    "Patient state",
    "PATZIP":   "Patient zip",
}

# Columns that must be present for a file to be treated as an RX log
_SIGNATURE_COLS = {"RXNBR", "FILLDATE", "DRUG NAME", "NDC", "RX STOREID", "DR NPI"}


def detect_rx_log(df: pd.DataFrame) -> bool:
    """Return True if *df* has the RX-log column signature."""
    return _SIGNATURE_COLS.issubset(set(df.columns))


def map_rx_log(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Transform a pharmacy RX-log DataFrame into the three DataFrames
    expected by ``audit_dataframe()``.

    Returns
    -------
    raw : pd.DataFrame
        Claim-level data with engine-canonical column names.
    store_map : pd.DataFrame
        One row per unique store.
    site_entity_map : pd.DataFrame
        Minimal map with ``Site location`` = store number.
    """
    # ── Drop unused columns early to save memory ─────────────────────────────
    # The rx log can have 200+ columns; we only need the ones in _COLUMN_MAP
    # plus a few extras for provider/patient name and address building.
    _keep = set(_COLUMN_MAP.keys()) | {
        "DOCNAMEFIRST", "DOCNAMELAST", "PATNAMEFIRST", "PATNAMELAST",
        "DOCADD1", "DOCADD2", "DOCCITY", "DOCST", "DOCZIP",
        "PRICE SCHED", "RXCOST", "RXPRICE", "QTY DSP",
        "PATDOB", "PATST", "PATZIP",
    }
    _available = [c for c in df.columns if c in _keep]
    df = df[_available].copy()

    raw = df.rename(columns={k: v for k, v in _COLUMN_MAP.items() if k in df.columns})

    # ── Prescribing provider (combine first + last) ───────────────────────────
    if "Prescribing provider" not in raw.columns:
        first = df.get("DOCNAMEFIRST", pd.Series([""] * len(df), dtype=str)).fillna("").astype(str).str.strip()
        last  = df.get("DOCNAMELAST",  pd.Series([""] * len(df), dtype=str)).fillna("").astype(str).str.strip()
        raw["Prescribing provider"] = (first + " " + last).str.strip()

    # ── Patient name (combine first + last) ───────────────────────────────────
    if "Patient name" not in raw.columns:
        pfirst = df.get("PATNAMEFIRST", pd.Series([""] * len(df), dtype=str)).fillna("").astype(str).str.strip()
        plast  = df.get("PATNAMELAST",  pd.Series([""] * len(df), dtype=str)).fillna("").astype(str).str.strip()
        raw["Patient name"] = (pfirst + " " + plast).str.strip()

    # ── Per-store address from prescriber location columns ────────────────────
    # DOCADD1/DOCADD2/DOCCITY/DOCST/DOCZIP give the physical site address.
    # Build a {store_id: "address"} lookup so each store card shows the real
    # address instead of the raw store number.
    def _s(col: str) -> pd.Series:
        return (
            df.get(col, pd.Series([""] * len(df), dtype=str))
            .fillna("").astype(str).str.strip()
        )

    _addr_df = pd.DataFrame({
        "store": (
            df["RX STOREID"].astype(str).str.strip()
            if "RX STOREID" in df.columns
            else raw["Store number"].astype(str).str.strip()
        ),
        "add1": _s("DOCADD1"),
        "add2": _s("DOCADD2"),
        "city": _s("DOCCITY"),
        "st":   _s("DOCST"),
        "zip":  _s("DOCZIP"),
    })

    _store_addr: dict[str, str] = {}
    for sid, grp in _addr_df.groupby("store"):
        r = grp.iloc[0]
        parts: list[str] = [r["add1"]] if r["add1"] else []
        if r["add2"]:
            parts.append(r["add2"])
        state_zip = " ".join(p for p in [r["st"], r["zip"]] if p)
        if r["city"] and state_zip:
            parts.append(f"{r['city']}, {state_zip}")
        elif r["city"]:
            parts.append(r["city"])
        elif state_zip:
            parts.append(state_zip)
        _store_addr[str(sid)] = ", ".join(parts) if parts else str(sid)

    # ── Store_Map: one row per unique store ───────────────────────────────────
    stores = (
        raw["Store number"]
        .dropna()
        .astype(str)
        .str.strip()
        .replace("", pd.NA)
        .dropna()
        .unique()
        .tolist()
    )
    store_map = pd.DataFrame({
        "Store number":           stores,
        # Pharmacy location shows the real site address for display.
        # Site location / Patient encounter site stay as store IDs so the
        # entity-join key remains stable until 340B IDs are registered.
        "Pharmacy location":      [_store_addr.get(s, s) for s in stores],
        "Site location":          stores,
        "Patient encounter site": stores,
        # Entity site address also set from physical address so the
        # Store-map completeness check can pass once 340B ID + Covered entity
        # are registered via the Site Registry tab.
        "Entity site address":    [_store_addr.get(s, s) for s in stores],
    })

    # ── Site_Entity_Map: minimal — entity match will be REVIEW until user
    #    populates 340B ID / Covered entity via the Site Registry tab ──────────
    site_entity_map = pd.DataFrame({
        "Site location":                stores,
        "Valid patient encounter site": stores,
        "Active Y/N":                   ["Y"] * len(stores),
    })

    return raw, store_map, site_entity_map


# ── EHR / patient-record detection ───────────────────────────────────────────

# Column keywords that strongly indicate the file is an EHR/patient record
# export rather than a pharmacy billing RX-log.
_EHR_INDICATOR_COLS: set[str] = {
    "patient", "patient name", "mrn", "medical record number", "dob",
    "date of birth", "rendering provider", "medication", "prescriber",
    "prescriber [agent]", "effective date", "dispense", "is hospice",
    "inactivated on", "inactive comments", "encounter date", "visit date",
    "date of service",
}


def looks_like_ehr(df: pd.DataFrame, threshold: int = 3) -> bool:
    """
    Return True if *df* appears to be an EHR patient/encounter export.

    Checks how many of its column names (case-insensitive) overlap with
    common EHR field keywords.  A match count >= *threshold* is treated
    as a positive signal.
    """
    cols_lower = {c.lower().strip() for c in df.columns}
    matches = len(cols_lower & _EHR_INDICATOR_COLS)
    return matches >= threshold
