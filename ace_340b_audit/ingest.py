"""
ace_340b_audit/ingest.py
------------------------
Adapter for proprietary pharmacy RX-log CSV files.

Detects the RX-log column schema and maps it to the engine's canonical
column names, producing the three DataFrames (raw, store_map, site_entity_map)
expected by audit_dataframe().

Supported source: pharmacy dispensing logs with columns such as
RXNBR, FILLDATE, DRUG NAME, NDC, RX STOREID, DR NPI, DOCNAMELAST, etc.
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
        One row per unique store; ``Pharmacy location`` is populated with the
        physical site address from DOCADD1/DOCCITY/DOCST/DOCZIP.
        ``340B ID`` / ``Covered entity`` are left blank until registered via
        the Site Registry tab, at which point the engine's entity check passes.
    site_entity_map : pd.DataFrame
        Minimal map with ``Site location`` = store number so the entity join
        can run; ``340B ID`` / ``Covered entity`` are left blank, which causes
        ``Entity map`` = REVIEW until the user registers the site.
    """
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
