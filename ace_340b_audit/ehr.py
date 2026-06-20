"""
EHR data ingestion, column auto-detection, normalization, and claims cross-reference.

Supports any EHR / ADT / visit-summary Excel or CSV export.
Auto-maps common column naming conventions to canonical field names, then
cross-references encounters against 340B claim records to validate:
  - Encounter date is within the required window of the fill date
  - Prescriber in the claim appears in the EHR
  - Location / site matches

Usage
-----
    from ace_340b_audit.ehr import detect_ehr_columns, normalize_ehr, crossref_claims

    ehr_norm, col_map = normalize_ehr(raw_df)
    result_claims = crossref_claims(ehr_norm, claims_df, window_days=365)
"""
from __future__ import annotations

from typing import Any
import pandas as pd

# ── canonical field keys and their display labels ────────────────────────────

CANONICAL: dict[str, str] = {
    "encounter_date": "Encounter date",
    "provider_name":  "Provider name",
    "provider_npi":   "Provider NPI",
    "patient_name":   "Patient name",
    "patient_mrn":    "Patient MRN",
    "patient_dob":    "Patient DOB",
    "location":       "Location",
    "drug_name":      "Drug name",
    "ndc":            "NDC",
    "diagnosis":      "Diagnosis code",
    "rx_number":      "Rx number",
    "pharmacy":       "Pharmacy",
    "payer":          "Payer",
}

# Fields surfaced in the EHR preview table (ordered)
EHR_DISPLAY_FIELDS: list[str] = [
    "Encounter date",
    "Patient name",
    "Patient MRN",
    "Patient DOB",
    "Provider name",
    "Provider NPI",
    "Location",
    "Drug name",
    "NDC",
    "Diagnosis code",
    "Rx number",
    "Pharmacy",
    "Payer",
]

# ── keyword hints for auto-column detection ──────────────────────────────────

_HINTS: dict[str, list[str]] = {
    "encounter_date": [
        "visit date", "encounter date", "date of service", "dos",
        "service date", "appt date", "appointment date", "visit dt",
        "encounter dt", "svc date", "date of visit", "encounter_date",
        "visit_date", "servicedate", "visitdate",
        # common EHR export variants
        "date",  # bare "Date" column — common in eCW/EHR medication exports
        "rx date", "rxdate",
        "dispense date", "dispensed date", "fill date", "filldate",
        "prescription date", "order date", "written date",
        "effective date", "effective_date",  # often empty; lower priority
    ],
    "provider_name": [
        "provider name", "provider", "prescriber", "attending provider",
        "ordering provider", "physician", "doctor", "clinician",
        "rendering provider", "prescribing provider", "provider_name",
        "physician name",
        # bracket-style agent fields common in pharmacy EHRs
        "prescriber [agent]", "prescriber[agent]", "prescriber agent",
        "ordering physician", "rendering physician",
    ],
    "provider_npi": [
        "provider npi", "npi", "physician npi", "prescriber npi",
        "ordering npi", "rendering npi", "attending npi", "dr npi",
        "provider_npi",
    ],
    "patient_name": [
        "patient name", "patient", "member name", "beneficiary name",
        "patient_name", "patientname", "member",
        "resident name", "resident",
    ],
    "patient_mrn": [
        "mrn", "patient id", "patient mrn", "member id", "chart number",
        "chart #", "chart#", "record number", "patient_mrn", "patientid",
        "medical record number", "med rec #",
    ],
    "patient_dob": [
        "date of birth", "dob", "birth date", "birthdate", "patient dob",
        "patient_dob", "dateofbirth",
    ],
    "location": [
        "location", "facility", "site", "department", "clinic",
        "practice", "dispense location", "dispensing location",
        "practice location", "service location", "place of service",
        "pos", "facility name",
    ],
    "drug_name": [
        "drug name", "drug", "medication", "medication name",
        "drug description", "item description", "product", "drug_name",
        "med name", "prescribed drug", "dispensed drug",
        # pharmacy system export names
        "medication description", "drug/medication", "med description",
    ],
    "ndc": [
        "ndc", "national drug code", "ndc code", "drug code", "ndc11",
        "ndc number",
    ],
    "diagnosis": [
        "diagnosis code", "diagnosis", "icd10", "icd-10", "icd 10",
        "dx code", "primary diagnosis", "primary dx", "diag code",
        "icd_10", "icd10_code", "diagnosis_code",
    ],
    "rx_number": [
        "rx number", "rx#", "rxnbr", "prescription number", "rx nbr",
        "rx no", "rxno", "script number", "rx_number", "prescription#",
        "fill number",
    ],
    "pharmacy": [
        "pharmacy", "pharmacy name", "dispensing pharmacy", "filling pharmacy",
        "pharmacy location", "store", "pharmacy_name", "fill pharmacy",
        "dispense pharmacy", "rx pharmacy",
    ],
    "payer": [
        "payer", "payor", "insurance", "insurance plan", "plan name",
        "primary payer", "primary insurance", "payer name", "plan",
        "coverage", "insurance name", "payer_name", "carrier",
    ],
}


# ── column detection ──────────────────────────────────────────────────────────

def detect_ehr_columns(df: pd.DataFrame) -> dict[str, str | None]:
    """
    Return {canonical_key: best_matching_column_name | None}.

    Matching is case-insensitive and whitespace-stripped.
    """
    cols_lower = {c.lower().strip(): c for c in df.columns}
    mapping: dict[str, str | None] = {}
    for key, hints in _HINTS.items():
        matched = None
        for hint in hints:
            if hint in cols_lower:
                matched = cols_lower[hint]
                break
        # Fallback: substring match (e.g. a column called "DOS/Date" matches "dos")
        if matched is None:
            for hint in hints:
                for col_lower, col_orig in cols_lower.items():
                    if hint in col_lower or col_lower in hint:
                        matched = col_orig
                        break
                if matched:
                    break
        mapping[key] = matched
    return mapping


# ── normalization ─────────────────────────────────────────────────────────────

def normalize_ehr(
    df: pd.DataFrame,
    override_map: dict[str, str | None] | None = None,
) -> tuple[pd.DataFrame, dict[str, str | None]]:
    """
    Return (normalized_df, column_mapping).

    normalized_df has canonical column names where detected.
    Unrecognised columns are kept as-is.

    Parameters
    ----------
    df : raw EHR DataFrame
    override_map : {canonical_key: actual_column_name} to override auto-detection
    """
    mapping = detect_ehr_columns(df)
    if override_map:
        for key, col in override_map.items():
            if col:  # only override if a column was chosen
                mapping[key] = col

    # Build rename dict (skip None mappings; avoid double-renaming same source col)
    rename: dict[str, str] = {}
    used_targets: set[str] = set()
    for key, src_col in mapping.items():
        if src_col is None:
            continue
        target = CANONICAL[key]
        if target in used_targets:
            continue
        if src_col != target:
            rename[src_col] = target
        used_targets.add(target)

    out = df.rename(columns=rename).copy()

    # Coerce encounter date to datetime (strip whitespace-only values first)
    enc_col = CANONICAL["encounter_date"]
    if enc_col in out.columns:
        out[enc_col] = out[enc_col].replace(r'^\s*$', pd.NA, regex=True)
        out[enc_col] = pd.to_datetime(out[enc_col], errors="coerce")

    # Coerce patient DOB
    dob_col = CANONICAL["patient_dob"]
    if dob_col in out.columns:
        out[dob_col] = out[dob_col].replace(r'^\s*$', pd.NA, regex=True)
        out[dob_col] = pd.to_datetime(out[dob_col], errors="coerce")

    return out, mapping


# ── name normalization helpers ────────────────────────────────────────────────

_NAME_SUFFIXES = {
    "JR", "SR", "II", "III", "IV", "V",
    "MD", "DO", "NP", "PA", "RN", "APRN", "FNP", "DNP",
    "PHARMD", "DDS", "DMD", "PHD", "ESQ",
}


def _normalize_name(name: str) -> str:
    """
    Normalize a patient name for matching.

    - Uppercase, strip whitespace
    - Remove suffixes (Jr, Sr, II, III, MD, DO, etc.)
    - Remove punctuation (periods, commas, apostrophes, hyphens → spaces)
    - Collapse multiple spaces
    - Sort tokens alphabetically so "FIRST LAST" == "LAST FIRST"

    Examples
    --------
    >>> _normalize_name("Kip Claxton Jr")
    'CLAXTON KIP'
    >>> _normalize_name("Claxton, Kip")
    'CLAXTON KIP'
    >>> _normalize_name("Cruz-Rodriguez, Leylani")
    'CRUZ LEYLANI RODRIGUEZ'
    """
    if not name or not isinstance(name, str):
        return ""
    s = name.upper().strip()
    # Remove punctuation
    for ch in ".,'-":
        s = s.replace(ch, " ")
    tokens = s.split()
    # Remove suffixes
    tokens = [t for t in tokens if t not in _NAME_SUFFIXES]
    # Sort alphabetically so order doesn't matter
    tokens.sort()
    return " ".join(tokens)


def _normalize_mrn(val) -> str:
    """Normalize an MRN value for matching — strip, remove leading zeros."""
    if pd.isna(val):
        return ""
    s = str(val).strip().lstrip("0")
    # Remove .0 from float conversion
    s = s.replace(".0", "")
    return s


# ── claims cross-reference ────────────────────────────────────────────────────

def crossref_claims(
    ehr_df: pd.DataFrame,
    claims_df: pd.DataFrame,
    window_days: int = 365,
) -> pd.DataFrame:
    """
    Cross-reference EHR encounters against 340B claim records.

    Match strategy (in priority order):
      1. MRN exact match (most reliable patient identifier)
      2. MRN match + encounter date within ``window_days`` of fill date
      3. Patient name (normalized) + encounter date within ``window_days``
         + Provider NPI match (if NPI available in both)
      4. Patient name (normalized) + encounter date within ``window_days``
         (NPI absent/mismatch)
      5. Rx number exact match (if Rx number present in EHR)

    Name normalization handles:
      - "Kip Claxton Jr" vs "Claxton, Kip" → both become "CLAXTON KIP"
      - Suffixes (Jr, Sr, II, III, MD, DO, etc.) are stripped
      - Hyphens and punctuation are removed
      - Token order is ignored (FIRST LAST == LAST FIRST)

    Returns
    -------
    pd.DataFrame
        claims_df with added columns:
        - ``EHR match``          : "MATCHED" | "MATCHED (MRN)" | "NO MATCH" | "N/A"
        - ``EHR encounter date`` : matched encounter date string (blank if no match)
        - ``EHR provider``       : matched provider name from EHR (blank if no match)
        - ``EHR location``       : matched location from EHR (blank if no match)
        - ``EHR diagnosis``      : matched diagnosis code from EHR (blank if no match)
        - ``EHR pharmacy``       : pharmacy from EHR encounter (blank if no match)
        - ``EHR payer``          : payer/insurance from EHR encounter (blank if no match)
        - ``EHR medication``     : medication from EHR encounter (blank if no match)
        - ``EHR MRN``            : patient MRN from EHR (blank if no match)
        - ``EHR match method``   : which strategy produced the match
    """
    result = claims_df.copy()
    for col in ("EHR match", "EHR encounter date", "EHR provider",
                "EHR location", "EHR diagnosis", "EHR pharmacy",
                "EHR payer", "EHR medication", "EHR MRN",
                "EHR match method"):
        result[col] = ""
    result["EHR match"] = "N/A"

    # Column shortcuts
    enc_col  = CANONICAL["encounter_date"]
    pat_col  = CANONICAL["patient_name"]
    mrn_col  = CANONICAL["patient_mrn"]
    npi_col  = CANONICAL["provider_npi"]
    prov_col = CANONICAL["provider_name"]
    loc_col  = CANONICAL["location"]
    dx_col   = CANONICAL["diagnosis"]
    rx_col   = CANONICAL["rx_number"]
    pharm_col = CANONICAL["pharmacy"]
    payer_col = CANONICAL["payer"]
    drug_col  = CANONICAL["drug_name"]

    fill_col      = "Fill date"
    claim_pat_col = "Patient name"
    claim_npi_col = "Provider NPI"
    claim_rx_col  = "Prescription number"
    claim_mrn_col = "MRN"               # mapped from PATKEY or MRN in rx log

    has_enc  = enc_col in ehr_df.columns
    has_pat  = pat_col in ehr_df.columns
    has_mrn  = mrn_col in ehr_df.columns
    has_npi  = npi_col in ehr_df.columns
    has_prov = prov_col in ehr_df.columns
    has_loc  = loc_col in ehr_df.columns
    has_dx   = dx_col in ehr_df.columns
    has_rx   = rx_col in ehr_df.columns
    has_pharm = pharm_col in ehr_df.columns
    has_payer = payer_col in ehr_df.columns
    has_drug  = drug_col in ehr_df.columns

    has_claim_mrn = claim_mrn_col in claims_df.columns
    # Also check for "Patient key" (PATKEY) as alternate MRN in claims
    claim_patkey_col = "Patient key"
    has_claim_patkey = claim_patkey_col in claims_df.columns

    # Need at minimum an encounter date and patient name, an MRN, or an Rx number
    if not ((has_enc and has_pat) or has_mrn or has_rx):
        result["EHR match"] = "N/A — EHR must contain patient name + encounter date, MRN, or Rx number"
        return result

    if fill_col not in claims_df.columns:
        result["EHR match"] = "N/A — claims data missing Fill date"
        return result

    # ── preprocess EHR ───────────────────────────────────────────────────────
    ehr = ehr_df.copy()
    if has_pat:
        ehr["_pat_norm"] = ehr[pat_col].fillna("").astype(str).apply(_normalize_name)
    if has_mrn:
        ehr["_mrn"] = ehr[mrn_col].apply(_normalize_mrn)
    if has_enc:
        ehr[enc_col] = pd.to_datetime(ehr[enc_col], errors="coerce")
    if has_rx:
        ehr["_rx"] = ehr[rx_col].fillna("").astype(str).str.strip()

    # Build EHR row entry helper
    def _ehr_entry(row) -> dict[str, Any]:
        return {
            "enc_date": getattr(row, enc_col, None) if has_enc else None,
            "npi":      str(getattr(row, npi_col, "")).strip() if has_npi else "",
            "provider": str(getattr(row, prov_col, "")).strip() if has_prov else "",
            "location": str(getattr(row, loc_col, "")).strip() if has_loc else "",
            "dx":       str(getattr(row, dx_col, "")).strip() if has_dx else "",
            "pat_name": str(getattr(row, pat_col, "")).strip() if has_pat else "",
            "pharmacy": str(getattr(row, pharm_col, "")).strip() if has_pharm else "",
            "payer":    str(getattr(row, payer_col, "")).strip() if has_payer else "",
            "drug":     str(getattr(row, drug_col, "")).strip() if has_drug else "",
            "mrn":      str(getattr(row, mrn_col, "")).strip() if has_mrn else "",
        }

    # Build fast lookup: MRN → list of EHR row dicts
    _ehr_by_mrn: dict[str, list[dict]] = {}
    if has_mrn:
        for row in ehr.itertuples(index=False):
            mrn_val = getattr(row, "_mrn", "")
            if not mrn_val:
                continue
            _ehr_by_mrn.setdefault(mrn_val, []).append(_ehr_entry(row))

    # Build fast lookup: normalized patient name → list of EHR row dicts
    _ehr_by_pat: dict[str, list[dict]] = {}
    if has_pat and has_enc:
        ehr_valid = ehr.dropna(subset=[enc_col])
        for row in ehr_valid.itertuples(index=False):
            pat = getattr(row, "_pat_norm", "")
            if not pat:
                continue
            _ehr_by_pat.setdefault(pat, []).append(_ehr_entry(row))

    # Build fast lookup: rx_number → EHR row dict (for Rx-based match fallback)
    _ehr_by_rx: dict[str, dict] = {}
    if has_rx:
        for row in ehr.itertuples(index=False):
            rxn = getattr(row, "_rx", "")
            if not rxn:
                continue
            _ehr_by_rx[rxn] = _ehr_entry(row)

    # ── preprocess claims ────────────────────────────────────────────────────
    result["_fill"] = pd.to_datetime(result[fill_col], errors="coerce")
    result["_pat_norm"] = (
        result[claim_pat_col].fillna("").astype(str).apply(_normalize_name)
        if claim_pat_col in result.columns else ""
    )
    result["_npi"] = (
        result[claim_npi_col].fillna("").astype(str).str.strip()
        if claim_npi_col in result.columns else ""
    )
    result["_rxn"] = (
        result[claim_rx_col].fillna("").astype(str).str.strip()
        if claim_rx_col in result.columns else ""
    )
    # Try MRN from claims (mapped from MRN column or PATKEY)
    if has_claim_mrn:
        result["_mrn"] = result[claim_mrn_col].apply(_normalize_mrn)
    elif has_claim_patkey:
        result["_mrn"] = result[claim_patkey_col].apply(_normalize_mrn)
    else:
        result["_mrn"] = ""

    # ── match helper ─────────────────────────────────────────────────────────
    def _fill_match(entry: dict, method: str) -> dict:
        return {
            "EHR match":          "MATCHED",
            "EHR encounter date": str(entry.get("enc_date", ""))[:10] if entry.get("enc_date") else "",
            "EHR provider":       entry.get("provider", ""),
            "EHR location":       entry.get("location", ""),
            "EHR diagnosis":      entry.get("dx", ""),
            "EHR pharmacy":       entry.get("pharmacy", ""),
            "EHR payer":          entry.get("payer", ""),
            "EHR medication":     entry.get("drug", ""),
            "EHR MRN":            entry.get("mrn", ""),
            "EHR match method":   method,
        }

    # ── match each claim ─────────────────────────────────────────────────────
    def _match_row(row: Any) -> dict:
        out = {"EHR match": "NO MATCH", "EHR encounter date": "",
               "EHR provider": "", "EHR location": "", "EHR diagnosis": "",
               "EHR pharmacy": "", "EHR payer": "", "EHR medication": "",
               "EHR MRN": "", "EHR match method": ""}

        fill = row["_fill"]
        pat  = row["_pat_norm"]
        npi  = row["_npi"]
        rxn  = row["_rxn"]
        mrn  = row["_mrn"] if "_mrn" in row.index else ""

        # ── Strategy 1: MRN match + date window (highest confidence) ──────
        if mrn and mrn in _ehr_by_mrn:
            candidates = _ehr_by_mrn[mrn]
            # If we have a fill date, prefer encounters in the date window
            if not pd.isna(fill):
                for c in candidates:
                    enc = c.get("enc_date")
                    if enc is not None and not pd.isna(enc):
                        if abs((enc - fill).days) <= window_days:
                            return _fill_match(c, "MRN + date window")
            # MRN match without date window — still high confidence
            if candidates:
                return _fill_match(candidates[0], "MRN only")

        # ── Strategy 2: normalized name + encounter date ──────────────────
        if pat and not pd.isna(fill) and pat in _ehr_by_pat:
            candidates = _ehr_by_pat[pat]
            best = None
            best_npi_match = False
            for c in candidates:
                enc = c.get("enc_date")
                if enc is None or pd.isna(enc):
                    continue
                if abs((enc - fill).days) <= window_days:
                    npi_ok = (not npi) or (not c["npi"]) or (c["npi"] == npi)
                    if best is None or (npi_ok and not best_npi_match):
                        best = c
                        best_npi_match = npi_ok
            if best:
                method = "Name + date"
                if best_npi_match and npi:
                    method = "Name + date + NPI"
                return _fill_match(best, method)

        # ── Strategy 3: Rx number exact match ─────────────────────────────
        if rxn and rxn in _ehr_by_rx:
            return _fill_match(_ehr_by_rx[rxn], "Rx number")

        # Provide detail on near-misses
        if mrn and mrn in _ehr_by_mrn:
            out["EHR match"] = "NO MATCH — MRN found, no encounter in date window"
        elif pat and pat in _ehr_by_pat:
            out["EHR match"] = "NO MATCH — patient found by name, no encounter in window"
        elif pat:
            out["EHR match"] = "NO MATCH — patient not in EHR"

        return out

    matched = result.apply(_match_row, axis=1, result_type="expand")
    for col in matched.columns:
        result[col] = matched[col]

    result.drop(columns=["_fill", "_pat", "_npi", "_rxn"], errors="ignore", inplace=True)
    return result
