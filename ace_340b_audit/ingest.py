"""
ace_340b_audit/ingest.py
------------------------
Adapters for pharmacy dispensing / EHR prescription export files.

Three adapters are provided:

1. RX-log adapter (Southside / legacy proprietary format)
   Detects the strict RX-log column schema and maps it to engine-canonical
   column names.  Required: RXNBR, FILLDATE, DRUG NAME, NDC, RX STOREID, DR NPI.

2. MAP / multi-location adapter (TX-, DG-, DR-, PD- prefixed columns)
   Detects pharmacy systems that use prefix-based naming such as
   TX-Rx Number, TX-Date Filled, DG-Drug Name, DG-NDC Nbr,
   DR-Doctor NPI#, RX-Store Number, PD-Pat Combined Name, etc.
   Common in multi-entity 340B covered entities (e.g. MAP Health).

3. Generic dispense log adapter (flexible EHR/pharmacy exports)
   Handles any CSV or Excel export from an EHR or pharmacy system that
   includes patient, drug, date, and location information under various
   column name conventions (e.g. "Patient", "Medication", "Effective Date",
   "Location", "Rendering Provider", "Prescriber [Agent]", etc.).
   Missing NDC / Prescription number are synthesised so the audit engine
   can still run; those fields are flagged as incomplete.
"""

from __future__ import annotations

import pandas as pd

# ══════════════════════════════════════════════════════════════════════════════
# FORMAT 1: Southside / legacy RX-log
# ══════════════════════════════════════════════════════════════════════════════

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

_SIGNATURE_COLS = {"RXNBR", "FILLDATE", "DRUG NAME", "NDC", "RX STOREID", "DR NPI"}


# ══════════════════════════════════════════════════════════════════════════════
# FORMAT 2: MAP / multi-location (TX-, DG-, DR-, PD- prefixed)
# ══════════════════════════════════════════════════════════════════════════════

_MAP_COLUMN_MAP: dict[str, str] = {
    "TX-Rx Number":         "Prescription number",
    "TX-Date Filled":       "Fill date",
    "DG-Drug Name 27":      "Drug name",
    "DG-Drug Name":         "Drug name",
    "DG-NDC Nbr":           "NDC",
    "DG-NDC":               "NDC",
    "DR-Doctor NPI#":       "Provider NPI",
    "DR-Doctor NPI":        "Provider NPI",
    "DR-Doctor Name(25)":   "Prescribing provider",
    "DR-Doctor Name":       "Prescribing provider",
    "DR-Doctor Address":    "Provider address",
    "RX-Store Number":      "Store number",
    "RX-Store Nbr":         "Store number",
    "PD-Pat Combined Name": "Patient name",
    "PD-Patient Name":      "Patient name",
    "PD-Patient Birthdate": "Patient DOB",
    "PD-Patient DOB":       "Patient DOB",
    "PD-Patient Zip":       "Patient zip",
    "PD-Patient State":     "Patient state",
    "PD-Patient Gender":    "Patient gender",
    # Payer columns (MAP format)
    "TP-Plan Name":         "Primary payer",
    "TP-Plan ID":           "Primary payer ID",
    "TP-Primary Plan":      "Primary payer",
    # Date written
    "TX-Date Written":      "Encounter date",
    "TX-Written Date":      "Encounter date",
    # Additional MAP fields
    "TX-Refill Number":     "Refill",
    "TX-Qty Dispensed":     "Qty dispensed",
    "TX-Price":             "Rx price",
    "TX-Cost":              "Rx cost",
    "DG-DEA Class":         "DEA class",
    "DR-DEA Number":        "Prescriber DEA",
}

# Minimum columns to detect MAP format (need at least 3)
_MAP_SIGNATURE_PREFIXES = {"TX-", "DG-", "DR-", "PD-", "RX-"}


def detect_rx_log(df: pd.DataFrame) -> bool:
    """Return True if *df* has the Southside RX-log column signature."""
    return _SIGNATURE_COLS.issubset(set(df.columns))


def detect_map_format(df: pd.DataFrame) -> bool:
    """Return True if *df* uses the MAP / multi-location column naming.
    
    Detects columns with TX-, DG-, DR-, PD-, RX- prefixes commonly used
    by QS/1, Computer-Rx, PioneerRx, and similar pharmacy systems in
    multi-entity 340B environments.
    """
    cols = set(df.columns)
    # Check for known MAP columns
    known_matches = sum(1 for c in cols if c in _MAP_COLUMN_MAP)
    if known_matches >= 3:
        return True
    # Fallback: check for prefix pattern
    prefix_counts = sum(1 for c in cols if any(c.startswith(p) for p in _MAP_SIGNATURE_PREFIXES))
    return prefix_counts >= 4


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


def map_map_format(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Transform a MAP / multi-location pharmacy DataFrame into the three
    DataFrames expected by ``audit_dataframe()``.

    Handles column naming like TX-Rx Number, DG-Drug Name 27,
    DR-Doctor NPI#, PD-Pat Combined Name, RX-Store Number, etc.

    Also auto-detects any unmapped columns with recognized prefixes and
    preserves them for downstream use.
    """
    # Rename known columns to canonical names
    rename = {k: v for k, v in _MAP_COLUMN_MAP.items() if k in df.columns}
    raw = df.rename(columns=rename)

    # ── Ensure critical columns exist ────────────────────────────────────────

    # Fill date: use TX-Date Filled (already mapped) or first TX-Date* column
    if "Fill date" not in raw.columns:
        for c in df.columns:
            if c.startswith("TX-") and "date" in c.lower():
                raw["Fill date"] = df[c]
                break

    # Encounter date: if no WRITTEN/TX-Date Written, copy from Fill date
    if "Encounter date" not in raw.columns and "Fill date" in raw.columns:
        raw["Encounter date"] = raw["Fill date"]

    # NDC: ensure string format, remove decimals
    if "NDC" in raw.columns:
        raw["NDC"] = raw["NDC"].astype(str).str.replace(r"\.0$", "", regex=True)

    # Provider NPI: ensure string format, remove decimals from float conversion
    if "Provider NPI" in raw.columns:
        raw["Provider NPI"] = (
            raw["Provider NPI"]
            .fillna("")
            .astype(str)
            .str.replace(r"\.0$", "", regex=True)
            .str.strip()
        )

    # Store number: ensure string
    if "Store number" in raw.columns:
        raw["Store number"] = raw["Store number"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()

    # Patient name: already mapped from PD-Pat Combined Name
    # May be in "LAST, FIRST" or "LAST***, FIRST" format — clean up
    if "Patient name" in raw.columns:
        raw["Patient name"] = (
            raw["Patient name"]
            .fillna("")
            .astype(str)
            .str.replace(r"\*+", "", regex=True)  # remove privacy asterisks
            .str.strip()
        )

    # Prescribing provider: already mapped from DR-Doctor Name
    # May be in "LAST, FIRST" format — keep as-is for display

    # ── Parse dates ──────────────────────────────────────────────────────────
    for col in ("Fill date", "Encounter date", "Patient DOB"):
        if col in raw.columns:
            raw[col] = pd.to_datetime(raw[col], errors="coerce")

    # ── Per-store address from DR-Doctor Address ─────────────────────────────
    _store_addr: dict[str, str] = {}
    if "Store number" in raw.columns and "Provider address" in raw.columns:
        for sid, grp in raw.groupby("Store number"):
            addr = grp["Provider address"].dropna().astype(str).str.strip()
            addr = addr[addr != ""].head(1)
            _store_addr[str(sid)] = addr.iloc[0] if len(addr) > 0 else str(sid)
    elif "Store number" in raw.columns:
        for sid in raw["Store number"].unique():
            _store_addr[str(sid)] = str(sid)

    # ── Store_Map ────────────────────────────────────────────────────────────
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
        "Pharmacy location":      [_store_addr.get(s, s) for s in stores],
        "Site location":          stores,
        "Patient encounter site": stores,
        "Entity site address":    [_store_addr.get(s, s) for s in stores],
    })

    # ── Site_Entity_Map ──────────────────────────────────────────────────────
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
