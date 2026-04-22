from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .decisions import (
    CATEGORIES,
    COMPLIANT,
    DUPLICATE_DISCOUNT,
    SEVERITY,
    categorize_claim,
    generate_action_plan,
)
from .rules import DEFAULT_RULES, load_rules

REQUIRED_RAW_COLUMNS = [
    "Prescription number",
    "Fill date",
    "Drug name",
    "NDC",
    "Prescribing provider",
    "Provider NPI",
    "Store number",
]

DERIVED_COLUMNS = [
    "Pharmacy location",
    "Site location",
    "Patient encounter site",
    "340B ID",
    "Covered entity",
    "Entity site address",
]


# ── helpers ──────────────────────────────────────────────────────────────────

def _clean_string_series(series: pd.Series) -> pd.Series:
    return (
        series.astype("string")
        .fillna("")
        .str.strip()
        .replace({"<NA>": "", "nan": "", "None": ""})
    )


def _normalize_store_number(series: pd.Series) -> pd.Series:
    return _clean_string_series(series).str.replace(r"\.0$", "", regex=True)


def _normalize_npi(series: pd.Series) -> pd.Series:
    return _clean_string_series(series).str.replace(r"\D", "", regex=True)


def _normalize_ndc(series: pd.Series) -> pd.Series:
    return _clean_string_series(series).str.replace(r"\D", "", regex=True)


def _read_sheet(path: str | Path, sheet_name: str) -> pd.DataFrame:
    return pd.read_excel(path, sheet_name=sheet_name)


def _safe_col(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return df[col]
    return pd.Series([""] * len(df), index=df.index, dtype="string")


def _truthy(series: pd.Series) -> pd.Series:
    return _clean_string_series(series).str.upper().isin(["Y", "YES", "TRUE", "1"])


def _missing_fields_list(df: pd.DataFrame, check_cols: list[str]) -> pd.Series:
    """Return a comma-separated string of missing field names per row."""
    parts = []
    for col in check_cols:
        mask = _clean_string_series(_safe_col(df, col)).eq("")
        parts.append(mask.map({True: col, False: ""}))
    combined = pd.concat(parts, axis=1).apply(
        lambda row: ", ".join(v for v in row if v), axis=1
    )
    return combined


# ── check functions ───────────────────────────────────────────────────────────

def _check_prescriber_master(df: pd.DataFrame, provider_master: pd.DataFrame) -> pd.Series:
    npi_col = next(
        (c for c in ("NPI", "Provider NPI", "provider_npi", "npi") if c in provider_master.columns),
        None,
    )
    if npi_col is None:
        return pd.Series(["N/A"] * len(df), index=df.index, dtype="string")

    valid_npis: set[str] = set(_normalize_npi(provider_master[npi_col])) - {""}
    result = np.where(
        df["Provider NPI"].eq(""),
        "REVIEW",
        np.where(df["Provider NPI"].isin(valid_npis), "PASS", "REVIEW"),
    )
    return pd.Series(result, index=df.index, dtype="string")


def _check_encounter_date(df: pd.DataFrame, window_days: int = 365) -> pd.Series:
    if "Encounter date" not in df.columns:
        return pd.Series(["N/A"] * len(df), index=df.index, dtype="string")

    enc = pd.to_datetime(df["Encounter date"], errors="coerce")
    fill = pd.to_datetime(df["Fill date"], errors="coerce")
    both_valid = enc.notna() & fill.notna()
    within_window = (fill - enc).abs() <= pd.Timedelta(days=window_days)

    result = np.where(~both_valid, "REVIEW", np.where(within_window, "PASS", "REVIEW"))
    return pd.Series(result, index=df.index, dtype="string")


def _check_duplicate_discounts(
    df: pd.DataFrame,
    medicaid_indicators: list[str],
    mtf_indicators: list[str],
) -> tuple[pd.Series, pd.Series]:
    """
    Returns (check_series, reason_series).
    reason_series contains a human-readable explanation of why the flag fired.
    """
    dup_keys = [c for c in ("Patient name", "NDC", "Fill date") if c in df.columns]
    dup_mask = df.duplicated(subset=dup_keys, keep=False) if dup_keys else pd.Series(False, index=df.index)

    med_indicators_upper = [i.upper() for i in medicaid_indicators]
    mtf_indicators_upper = [i.upper() for i in mtf_indicators]
    all_indicators_upper = med_indicators_upper + mtf_indicators_upper

    payer_cols = [
        c for c in df.columns
        if any(p in c.upper() for p in ("PAYER", "INSURANCE", "PLAN", "CARRIER", "BENEFIT"))
    ]

    med_mask = pd.Series(False, index=df.index)
    mtf_mask = pd.Series(False, index=df.index)
    for col in payer_cols:
        col_upper = _clean_string_series(df[col]).str.upper()
        for ind in med_indicators_upper:
            med_mask |= col_upper.str.contains(ind, na=False, regex=False)
        for ind in mtf_indicators_upper:
            mtf_mask |= col_upper.str.contains(ind, na=False, regex=False)

    payer_mask = med_mask | mtf_mask
    flag_mask = dup_mask | payer_mask

    # Build reason strings
    reasons: list[str] = []
    for i in df.index:
        parts = []
        if dup_mask.loc[i]:
            parts.append("Duplicate Rx")
        if med_mask.loc[i]:
            parts.append("Medicaid/State Plan")
        if mtf_mask.loc[i]:
            parts.append("MTF/TRICARE/DOD")
        reasons.append(", ".join(parts) if parts else "")

    check = pd.Series(
        np.where(flag_mask, "REVIEW", "PASS"),
        index=df.index,
        dtype="string",
    )
    reason = pd.Series(reasons, index=df.index, dtype="string")
    return check, reason


def _check_mef(df: pd.DataFrame, mef: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """
    Cross-reference each claim's 340B ID against the uploaded Medicaid Exclusion File (MEF).

    The MEF is maintained by HRSA. Covered entities that elect carve-in list their
    Medicaid Provider NPI on the MEF so state Medicaid agencies know NOT to request
    manufacturer rebates for drugs dispensed under 340B.

    Returns
    -------
    check  : "ON_MEF" | "NOT_ON_MEF" | "N/A"
    detail : human-readable string explaining the lookup result
    """
    # ── locate the 340B ID column in the MEF file ─────────────────────────────
    b340_col = next(
        (c for c in mef.columns
         if any(k in c.upper() for k in ("340B ID", "340B_ID", "340BID",
                                          "340B PROGRAM ID", "340B PARTICIPANT"))),
        None,
    )
    if b340_col is None:
        na = pd.Series(["N/A"] * len(df), index=df.index, dtype="string")
        msg = pd.Series(
            ["MEF file uploaded but no '340B ID' column found — check column headers"] * len(df),
            index=df.index, dtype="string",
        )
        return na, msg

    # ── locate optional status / active column ────────────────────────────────
    status_col = next(
        (c for c in mef.columns
         if c.strip().upper() in ("STATUS", "ACTIVE", "ACTIVE Y/N", "ACTIVE?", "IS ACTIVE")),
        None,
    )

    # ── locate optional state column ──────────────────────────────────────────
    state_col = next(
        (c for c in mef.columns
         if c.strip().upper() in ("STATE", "STATE CODE", "MEDICAID STATE")),
        None,
    )

    # ── build lookup set from MEF (active entries only) ───────────────────────
    mef_clean = mef.copy()
    mef_clean["_key"] = _clean_string_series(mef_clean[b340_col]).str.upper()

    if status_col:
        active_mask = _truthy(_safe_col(mef_clean, status_col))
        active_mef = mef_clean[active_mask].copy()
    else:
        active_mef = mef_clean.copy()

    # Build a dict: 340B_ID → list of states (or ["ALL"] if no state column)
    mef_dict: dict[str, list[str]] = {}
    for _, row in active_mef.iterrows():
        key = str(row["_key"])
        if not key:
            continue
        state = str(row[state_col]).strip().upper() if state_col else "ALL"
        mef_dict.setdefault(key, []).append(state)

    on_mef_set = set(mef_dict.keys()) - {""}

    # ── look up each claim ────────────────────────────────────────────────────
    claim_b340 = _clean_string_series(_safe_col(df, "340B ID")).str.upper()

    check_vals: list[str] = []
    detail_vals: list[str] = []
    for b340 in claim_b340:
        if not b340:
            check_vals.append("N/A")
            detail_vals.append("No 340B ID on claim")
        elif b340 in on_mef_set:
            states = mef_dict[b340]
            state_str = ", ".join(s for s in states if s != "ALL") or "all states"
            check_vals.append("ON_MEF")
            detail_vals.append(
                f"340B ID {b340} found on MEF"
                + (f" — active for: {state_str}" if state_str != "all states" else " — active")
            )
        else:
            check_vals.append("NOT_ON_MEF")
            detail_vals.append(f"340B ID {b340} not found in uploaded MEF")

    return (
        pd.Series(check_vals,  index=df.index, dtype="string"),
        pd.Series(detail_vals, index=df.index, dtype="string"),
    )


def _apply_exceptions(df: pd.DataFrame, exceptions: pd.DataFrame) -> pd.DataFrame:
    if "Prescription number" not in exceptions.columns:
        df["Exception flag"] = False
        df["Exception reason"] = ""
        return df

    exc_rx_clean = _clean_string_series(exceptions["Prescription number"])
    valid_exc = exc_rx_clean.ne("")
    exc_map: dict[str, str] = {}
    if "Exception reason" in exceptions.columns:
        exc_map = dict(zip(exc_rx_clean[valid_exc], _clean_string_series(exceptions["Exception reason"])[valid_exc]))

    exc_set = set(exc_rx_clean[valid_exc])
    claim_rx_clean = _clean_string_series(df["Prescription number"])
    exc_mask = claim_rx_clean.isin(exc_set)

    df["Exception flag"] = exc_mask
    df["Exception reason"] = claim_rx_clean.map(exc_map).fillna("")
    df.loc[exc_mask & (df["Overall status"] == "REVIEW"), "Overall status"] = "EXCEPTION"
    return df


# ── public API ────────────────────────────────────────────────────────────────

def run_audit_from_workbook(
    path: str | Path,
    rules: dict[str, Any] | None = None,
    exceptions: pd.DataFrame | None = None,
    provider_master: pd.DataFrame | None = None,
    mef: pd.DataFrame | None = None,
    carve_status: str = "unknown",
) -> dict[str, pd.DataFrame]:
    raw = _read_sheet(path, "Raw_Data")
    store_map = _read_sheet(path, "Store_Map")
    site_entity_map = _read_sheet(path, "Site_Entity_Map")

    if provider_master is None:
        try:
            provider_master = _read_sheet(path, "Provider_Master")
        except Exception:
            pass

    # Try loading MEF from workbook sheet if not passed explicitly
    if mef is None:
        try:
            mef = _read_sheet(path, "MEF")
        except Exception:
            pass

    return audit_dataframe(
        raw=raw,
        store_map=store_map,
        site_entity_map=site_entity_map,
        provider_master=provider_master,
        mef=mef,
        exceptions=exceptions,
        rules=rules,
        carve_status=carve_status,
    )


def audit_dataframe(
    raw: pd.DataFrame,
    store_map: pd.DataFrame,
    site_entity_map: pd.DataFrame,
    *,
    provider_master: pd.DataFrame | None = None,
    mef: pd.DataFrame | None = None,
    exceptions: pd.DataFrame | None = None,
    rules: dict[str, Any] | None = None,
    carve_status: str = "unknown",
) -> dict[str, pd.DataFrame]:

    if rules is None:
        rules = load_rules()

    scoring    = {**DEFAULT_RULES["scoring"],    **rules.get("scoring", {})}
    thresholds = {**DEFAULT_RULES["thresholds"], **rules.get("thresholds", {})}
    medicaid_indicators: list[str] = rules.get("medicaid_indicators", DEFAULT_RULES["medicaid_indicators"])
    mtf_indicators:      list[str] = rules.get("mtf_indicators",      DEFAULT_RULES["mtf_indicators"])

    df = raw.copy()
    for col in REQUIRED_RAW_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df["Store number"] = _normalize_store_number(df["Store number"])
    df["Provider NPI"] = _normalize_npi(df["Provider NPI"])
    df["NDC"]          = _normalize_ndc(df["NDC"])
    if "Fill date" in df.columns:
        df["Fill date"] = pd.to_datetime(df["Fill date"], errors="coerce")

    # ── store map join ────────────────────────────────────────────────────────
    store = store_map.copy()
    if "Store number" not in store.columns:
        raise ValueError("Store_Map sheet must contain 'Store number'.")
    store["Store number"] = _normalize_store_number(store["Store number"])
    store = store.drop_duplicates(subset=["Store number"], keep="first")
    store_lookup_cols = [c for c in [
        "Store number", "Pharmacy location", "Site location",
        "Patient encounter site", "340B ID", "Covered entity",
        "Entity site address", "Store map status",
    ] if c in store.columns]
    df = df.merge(store[store_lookup_cols], on="Store number", how="left")

    # ── entity map join ───────────────────────────────────────────────────────
    entity = site_entity_map.copy()
    if "Site location" not in entity.columns:
        entity["Site location"] = ""
    entity["Site location"] = _clean_string_series(entity["Site location"])
    if "Valid patient encounter site" not in entity.columns:
        entity["Valid patient encounter site"] = ""
    active = _truthy(_safe_col(entity, "Active Y/N"))
    active_entity = entity[active].copy()
    active_entity["entity_key"] = (
        _clean_string_series(_safe_col(active_entity, "Site location")).str.upper() + "|" +
        _clean_string_series(_safe_col(active_entity, "Valid patient encounter site")).str.upper()
    )
    entity_cols = ["entity_key", "340B ID", "Covered entity", "Entity site address",
                   "Site location", "Valid patient encounter site"]
    active_entity = active_entity[[c for c in entity_cols if c in active_entity.columns]].drop_duplicates("entity_key")

    df["site_key"] = (
        _clean_string_series(_safe_col(df, "Site location")).str.upper() + "|" +
        _clean_string_series(_safe_col(df, "Patient encounter site")).str.upper()
    )
    entity_join = active_entity.rename(columns={
        "340B ID":               "Entity 340B ID",
        "Covered entity":        "Entity covered entity",
        "Entity site address":   "Entity site address master",
        "Site location":         "Entity site location master",
        "Valid patient encounter site": "Entity encounter master",
    })
    df = df.merge(entity_join, left_on="site_key", right_on="entity_key", how="left")

    for col in REQUIRED_RAW_COLUMNS + DERIVED_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    for col in REQUIRED_RAW_COLUMNS + DERIVED_COLUMNS:
        df[col] = _safe_col(df, col)

    # ── missing fields ────────────────────────────────────────────────────────
    required_check_cols = REQUIRED_RAW_COLUMNS + DERIVED_COLUMNS
    missing_counts = pd.Series(0, index=df.index)
    for col in required_check_cols:
        missing_counts += _clean_string_series(df[col]).eq("")
    df["Missing fields"] = missing_counts
    df["Missing fields list"] = _missing_fields_list(df, required_check_cols)

    # ── format checks ─────────────────────────────────────────────────────────
    df["NPI check"] = np.where(df["Provider NPI"].str.fullmatch(r"\d{10}"), "PASS", "REVIEW")
    df["NDC check"] = np.where(df["NDC"].str.fullmatch(r"\d{10,11}"),        "PASS", "REVIEW")

    # ── store / entity mapping checks ─────────────────────────────────────────
    store_complete = (
        _clean_string_series(_safe_col(df, "Pharmacy location")).ne("")
        & _clean_string_series(_safe_col(df, "Site location")).ne("")
        & _clean_string_series(_safe_col(df, "Patient encounter site")).ne("")
        & _clean_string_series(_safe_col(df, "340B ID")).ne("")
        & _clean_string_series(_safe_col(df, "Covered entity")).ne("")
        & _clean_string_series(_safe_col(df, "Entity site address")).ne("")
    )
    df["Store map"] = np.where(store_complete, "PASS", "REVIEW")

    entity_match = (
        _clean_string_series(_safe_col(df, "Entity 340B ID")).ne("")
        & _clean_string_series(_safe_col(df, "Entity covered entity")).ne("")
    )
    entity_aligned = (
        _clean_string_series(_safe_col(df, "340B ID")).eq(_clean_string_series(_safe_col(df, "Entity 340B ID")))
        & _clean_string_series(_safe_col(df, "Covered entity")).eq(_clean_string_series(_safe_col(df, "Entity covered entity")))
    )
    df["Entity map"] = np.where(entity_match & entity_aligned, "PASS", "REVIEW")

    # ── new checks ────────────────────────────────────────────────────────────
    df["Prescriber check"] = (
        _check_prescriber_master(df, provider_master)
        if provider_master is not None
        else pd.Series(["N/A"] * len(df), index=df.index, dtype="string")
    )

    df["Encounter date check"] = _check_encounter_date(df, int(thresholds["encounter_date_window_days"]))

    dup_check, dup_reason = _check_duplicate_discounts(df, medicaid_indicators, mtf_indicators)
    df["Duplicate check"]  = dup_check
    df["Duplicate reason"] = dup_reason

    # ── MEF (Medicaid Exclusion File) cross-reference ─────────────────────────
    if mef is not None:
        mef_check, mef_detail = _check_mef(df, mef)
    else:
        mef_check  = pd.Series(["N/A"] * len(df), index=df.index, dtype="string")
        mef_detail = pd.Series(
            ["No MEF file uploaded — upload via sidebar to verify carve-in/carve-out status"] * len(df),
            index=df.index, dtype="string",
        )
    df["MEF check"]  = mef_check
    df["MEF detail"] = mef_detail

    # ── overall status ────────────────────────────────────────────────────────
    df["Overall status"] = np.where(
        (df["Missing fields"] > 0)
        | (df["NPI check"]            == "REVIEW")
        | (df["NDC check"]            == "REVIEW")
        | (df["Store map"]            == "REVIEW")
        | (df["Entity map"]           == "REVIEW")
        | (df["Prescriber check"]     == "REVIEW")
        | (df["Encounter date check"] == "REVIEW")
        | (df["Duplicate check"]      == "REVIEW"),
        "REVIEW",
        "PASS",
    )

    # ── risk scoring (rule-driven) ────────────────────────────────────────────
    df["Risk score"] = (
        100
        - (df["Missing fields"].astype(int) * int(scoring["missing_field_penalty"]))
        - np.where(df["NPI check"]            == "REVIEW", int(scoring["invalid_npi_penalty"]),                       0)
        - np.where(df["NDC check"]            == "REVIEW", int(scoring["invalid_ndc_penalty"]),                       0)
        - np.where(df["Store map"]            == "REVIEW", int(scoring["store_map_penalty"]),                         0)
        - np.where(df["Entity map"]           == "REVIEW", int(scoring["entity_map_penalty"]),                        0)
        - np.where(df["Prescriber check"]     == "REVIEW", int(scoring["prescriber_not_in_master_penalty"]),          0)
        - np.where(df["Encounter date check"] == "REVIEW", int(scoring["encounter_date_out_of_window_penalty"]),      0)
        - np.where(df["Duplicate check"]      == "REVIEW", int(scoring["duplicate_discount_penalty"]),                0)
    ).clip(lower=0)

    high_max = int(thresholds["high_risk_max"])
    med_max  = int(thresholds["medium_risk_max"])
    df["Risk tier"] = pd.cut(
        df["Risk score"],
        bins=[-1, high_max, med_max, 100],
        labels=["High", "Medium", "Low"],
    ).astype("string")

    # ── decision engine: category + action plan ───────────────────────────────
    df["Compliance category"] = df.apply(categorize_claim, axis=1)
    df["Severity"]            = df["Compliance category"].map(SEVERITY).fillna(0).astype(int)
    df["Action plan"]         = df.apply(
        lambda row: generate_action_plan(row, carve_status=carve_status), axis=1
    )

    # ── legacy issue columns (kept for backwards compat) ─────────────────────
    conditions = [
        df["Missing fields"] > 0,
        df["NPI check"]            == "REVIEW",
        df["NDC check"]            == "REVIEW",
        df["Store map"]            == "REVIEW",
        df["Entity map"]           == "REVIEW",
        df["Prescriber check"]     == "REVIEW",
        df["Encounter date check"] == "REVIEW",
        df["Duplicate check"]      == "REVIEW",
    ]
    choices = [
        "Missing required fields",
        "Invalid provider NPI",
        "Invalid NDC",
        "Store mapping incomplete",
        "Entity mapping incomplete",
        "Prescriber not in master",
        "Encounter date out of window",
        "Duplicate discount flag",
    ]
    df["Primary issue"] = np.select(conditions, choices, default="Clean")
    df["Issue bucket"]  = np.where(df["Overall status"] == "PASS", "Compliant", df["Primary issue"])

    # ── apply exceptions ──────────────────────────────────────────────────────
    if exceptions is not None and not exceptions.empty:
        df = _apply_exceptions(df, exceptions)
    else:
        df["Exception flag"]   = False
        df["Exception reason"] = ""

    # Override category / action for excepted claims
    exc_mask = df["Exception flag"]
    df.loc[exc_mask, "Compliance category"] = "EXCEPTION"
    df.loc[exc_mask, "Action plan"] = df.loc[exc_mask].apply(
        lambda row: (
            f"✅  EXCEPTION — Manually reviewed and approved.\n\n"
            f"Rx# {row.get('Prescription number', 'N/A')}  |  "
            f"Reason: {row.get('Exception reason', 'N/A')}\n\n"
            f"This claim has been reviewed and approved via the exceptions list. "
            f"No further action required."
        ),
        axis=1,
    )

    # ── MEF inconsistency flag ────────────────────────────────────────────────
    # Carve-out + ON_MEF is a compliance inconsistency worth surfacing
    mef_inconsistent = (
        (carve_status.strip().lower() == "carve-out")
        & (df["MEF check"] == "ON_MEF")
    )
    df["MEF inconsistency"] = mef_inconsistent

    # ── carve status column ───────────────────────────────────────────────────
    df["Carve status"] = carve_status

    # ── summary tables ────────────────────────────────────────────────────────
    cat_counts = df["Compliance category"].value_counts()

    summary = pd.DataFrame({
        "metric": [
            "Total claims imported",
            "Compliant claims",
            "REVIEW claims",
            "EXCEPTION claims",
            "Pass rate",
            "High risk claims",
            "Medium risk claims",
            "Low risk claims",
            "Average risk score",
            # Decision categories
            "Duplicate Discount",
            "Ineligible Prescriber",
            "Wrong Site",
            "Missing Encounter",
            "Data Mismatch",
            # Sub-checks
            "Prescriber check failures",
            "Encounter date failures",
            "Duplicate discount flags",
            "Exception overrides",
            # MEF
            "Claims with 340B ID on MEF",
            "Claims with 340B ID NOT on MEF",
            "MEF inconsistency flags",
        ],
        "value": [
            int(len(df)),
            int((df["Overall status"] == "PASS").sum()),
            int((df["Overall status"] == "REVIEW").sum()),
            int((df["Overall status"] == "EXCEPTION").sum()),
            float((df["Overall status"] == "PASS").mean()) if len(df) else 0.0,
            int((df["Risk tier"] == "High").sum()),
            int((df["Risk tier"] == "Medium").sum()),
            int((df["Risk tier"] == "Low").sum()),
            float(df["Risk score"].mean()) if len(df) else 0.0,
            int(cat_counts.get("Duplicate Discount",    0)),
            int(cat_counts.get("Ineligible Prescriber", 0)),
            int(cat_counts.get("Wrong Site",            0)),
            int(cat_counts.get("Missing Encounter",     0)),
            int(cat_counts.get("Data Mismatch",         0)),
            int((df["Prescriber check"]     == "REVIEW").sum()),
            int((df["Encounter date check"] == "REVIEW").sum()),
            int((df["Duplicate check"]      == "REVIEW").sum()),
            int(df["Exception flag"].sum()),
            int((df["MEF check"] == "ON_MEF").sum()),
            int((df["MEF check"] == "NOT_ON_MEF").sum()),
            int(df["MEF inconsistency"].sum()),
        ],
    })

    issue_summary = (
        df.groupby(["Compliance category"], dropna=False)
        .size()
        .reset_index(name="claims")
        .sort_values(["claims", "Compliance category"], ascending=[False, True])
    )

    store_status = (
        df.groupby(["Store number", "Pharmacy location", "Site location"], dropna=False)
        .agg(
            scripts=("Prescription number", "count"),
            review_claims=("Overall status", lambda s: int((s == "REVIEW").sum())),
            avg_risk_score=("Risk score", "mean"),
        )
        .reset_index()
        .sort_values("scripts", ascending=False)
    )
    store_status["review_rate"] = np.where(
        store_status["scripts"] > 0,
        store_status["review_claims"] / store_status["scripts"],
        0,
    )

    reviewed_claims = (
        df[df["Overall status"] == "REVIEW"]
        .copy()
        .sort_values(["Severity", "Risk score"], ascending=[False, True])
    )

    df = df.drop(columns=[c for c in ["site_key", "entity_key"] if c in df.columns], errors="ignore")

    return {
        "claims":          df,
        "summary":         summary,
        "issue_summary":   issue_summary,
        "store_status":    store_status,
        "reviewed_claims": reviewed_claims,
    }
