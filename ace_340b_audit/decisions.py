"""
ACE 340B Decision Engine
========================
Categorises each claim into one of six compliance outcomes and generates
a specific, step-by-step corrective action plan.

Categories (in severity order):
  1. Duplicate Discount     – highest financial/regulatory risk
  2. Ineligible Prescriber  – eligibility breach
  3. Wrong Site             – site/entity mapping failure
  4. Missing Encounter      – encounter documentation gap
  5. Data Mismatch          – data-quality / EHR-pharmacy discrepancy
  6. Compliant              – no action required

Carve status controls the Duplicate Discount action plan:
  "carve-in"   – Medicaid claims are IN the 340B programme; must use billing code 20
  "carve-out"  – Medicaid claims are EXCLUDED from 340B; must reprocess as retail
  "unknown"    – remind user to select their election before acting
"""
from __future__ import annotations

# ── category constants ────────────────────────────────────────────────────────

COMPLIANT             = "Compliant"
MISSING_ENCOUNTER     = "Missing Encounter"
INELIGIBLE_PRESCRIBER = "Ineligible Prescriber"
WRONG_SITE            = "Wrong Site"
DATA_MISMATCH         = "Data Mismatch"
DUPLICATE_DISCOUNT    = "Duplicate Discount"

CATEGORIES = [
    DUPLICATE_DISCOUNT,
    INELIGIBLE_PRESCRIBER,
    WRONG_SITE,
    MISSING_ENCOUNTER,
    DATA_MISMATCH,
    COMPLIANT,
]

# Higher = needs attention first
SEVERITY: dict[str, int] = {
    DUPLICATE_DISCOUNT:    5,
    INELIGIBLE_PRESCRIBER: 4,
    WRONG_SITE:            3,
    MISSING_ENCOUNTER:     2,
    DATA_MISMATCH:         1,
    COMPLIANT:             0,
}

CATEGORY_COLORS: dict[str, str] = {
    DUPLICATE_DISCOUNT:    "#d32f2f",   # red
    INELIGIBLE_PRESCRIBER: "#e65100",   # deep orange
    WRONG_SITE:            "#f57c00",   # orange
    MISSING_ENCOUNTER:     "#fbc02d",   # amber
    DATA_MISMATCH:         "#1565c0",   # blue
    COMPLIANT:             "#2e7d32",   # green
}

CATEGORY_DESCRIPTIONS: dict[str, str] = {
    COMPLIANT:
        "Claim meets all 340B programme eligibility requirements. No action required.",
    MISSING_ENCOUNTER:
        "No qualifying patient encounter was found within the required date window. "
        "The 340B programme requires a documented patient encounter at an eligible site "
        "with an eligible prescriber prior to dispense.",
    INELIGIBLE_PRESCRIBER:
        "The prescribing provider is not confirmed as eligible for this covered entity. "
        "HRSA guidance requires the prescriber to be employed by, under contract with, "
        "or in a documented referral relationship with the covered entity.",
    WRONG_SITE:
        "The dispensing pharmacy or patient encounter site is not mapped to a valid 340B "
        "covered entity site. The pharmacy must be the covered entity's in-house pharmacy "
        "or a registered contract pharmacy under this 340B ID.",
    DATA_MISMATCH:
        "Required claim data is missing, formatted incorrectly, or inconsistent between "
        "the EHR and the pharmacy system. The claim cannot be verified for 340B eligibility "
        "until the data is reconciled.",
    DUPLICATE_DISCOUNT:
        "A potential duplicate discount has been detected. "
        "Carve-In entities: listed on HRSA's Medicaid Exclusion File (MEF) and use 340B "
        "drugs for Medicaid FFS patients — claims must carry NDC Qualifier '20' so the state "
        "knows NOT to request a manufacturer rebate. "
        "Carve-Out entities: NOT on the MEF and purchase drugs at WAC for Medicaid FFS patients — "
        "340B pricing must never be applied to those claims. "
        "[42 U.S.C. § 256b(a)(5)(A)(i)]",
}


# ── categorisation ────────────────────────────────────────────────────────────

def categorize_claim(row: object) -> str:
    """
    Assign the highest-severity compliance category to a single claim row.
    Priority order: Duplicate Discount > Ineligible Prescriber > Wrong Site
                    > Missing Encounter > Data Mismatch > Compliant
    """
    def rv(col: str) -> str:
        try:
            return str(row[col]).strip().upper()   # type: ignore[index]
        except (KeyError, TypeError):
            return ""

    def intf(col: str) -> int:
        try:
            v = row[col]   # type: ignore[index]
            return int(v) if str(v) not in ("", "nan", "None", "<NA>") else 0
        except (KeyError, TypeError, ValueError):
            return 0

    if rv("Duplicate check") == "REVIEW":
        return DUPLICATE_DISCOUNT

    if rv("Prescriber check") == "REVIEW":
        return INELIGIBLE_PRESCRIBER

    if rv("Store map") == "REVIEW" or rv("Entity map") == "REVIEW":
        return WRONG_SITE

    if rv("Encounter date check") == "REVIEW":
        return MISSING_ENCOUNTER

    if intf("Missing fields") > 0 or rv("NPI check") == "REVIEW" or rv("NDC check") == "REVIEW":
        return DATA_MISMATCH

    return COMPLIANT


# ── corrective action plans ───────────────────────────────────────────────────

def generate_action_plan(row: object, carve_status: str = "unknown") -> str:
    """
    Generate a full, claim-specific corrective action plan.

    Parameters
    ----------
    row          : a dict-like object (pd.Series or dict) for one claim
    carve_status : "carve-in" | "carve-out" | "unknown"
    """
    def g(col: str, default: str = "N/A") -> str:
        try:
            v = row[col]   # type: ignore[index]
            s = str(v).strip()
            return s if s not in ("", "nan", "None", "<NA>", "NaT") else default
        except (KeyError, TypeError):
            return default

    category     = g("Compliance category", COMPLIANT)
    rx           = g("Prescription number")
    patient      = g("Patient name")
    prescriber   = g("Prescribing provider")
    npi          = g("Provider NPI")
    drug         = g("Drug name")
    ndc          = g("NDC")
    store        = g("Store number")
    pharmacy     = g("Pharmacy location")
    site         = g("Site location")
    enc_site     = g("Patient encounter site")
    b340_id      = g("340B ID")
    entity       = g("Covered entity")
    fill_date    = g("Fill date")
    dup_reason   = g("Duplicate reason", "")
    missing_list = g("Missing fields list", "")
    npi_check    = g("NPI check")
    ndc_check    = g("NDC check")
    store_map    = g("Store map")
    entity_map   = g("Entity map")

    mef_check  = g("MEF check",  "N/A")
    mef_detail = g("MEF detail", "No MEF file uploaded")
    mef_inconsistency = str(g("MEF inconsistency", "False")).lower() in ("true", "1", "yes")

    dispatch = {
        COMPLIANT:             _action_compliant,
        MISSING_ENCOUNTER:     _action_missing_encounter,
        INELIGIBLE_PRESCRIBER: _action_ineligible_prescriber,
        WRONG_SITE:            _action_wrong_site,
        DATA_MISMATCH:         _action_data_mismatch,
        DUPLICATE_DISCOUNT:    _action_duplicate_discount,
    }
    fn = dispatch.get(category, lambda **_: "No action plan available.")
    return fn(
        rx=rx, patient=patient, prescriber=prescriber, npi=npi,
        drug=drug, ndc=ndc, store=store, pharmacy=pharmacy,
        site=site, enc_site=enc_site, b340_id=b340_id, entity=entity,
        fill_date=fill_date, dup_reason=dup_reason, missing_list=missing_list,
        npi_check=npi_check, ndc_check=ndc_check,
        store_map=store_map, entity_map=entity_map,
        carve_status=carve_status,
        mef_check=mef_check, mef_detail=mef_detail,
        mef_inconsistency=mef_inconsistency,
    )


# ── per-category action plan builders ────────────────────────────────────────

def _action_compliant(*, rx, **_) -> str:
    return (
        f"✅  COMPLIANT — No action required.\n\n"
        f"Rx# {rx} meets all 340B programme eligibility requirements. "
        f"The prescription, prescriber, site, and entity mapping are verified."
    )


def _action_missing_encounter(
    *, rx, patient, prescriber, site, fill_date, entity, **_
) -> str:
    return f"""\
🔍  MISSING ENCOUNTER — Documentation Required

Rx# {rx}  |  Patient: {patient}  |  Fill date: {fill_date}
Prescriber: {prescriber}  |  Site: {site}  |  Entity: {entity}

The audit could not confirm a qualifying patient encounter within the
required window for this claim.

─── Required Actions ───────────────────────────────────────────────────
1. Open {entity}'s EHR (e.g., ECW, Epic, Cerner) and search for a
   patient encounter by {prescriber} within 365 days before {fill_date}.

2. If a valid encounter IS found:
   a. Confirm {prescriber} was practicing at or under contract with
      {site} at the time of the encounter.
   b. Update the encounter date in your pharmacy / TPA system.
   c. Reprocess Rx# {rx} as a 340B claim.

3. If NO qualifying encounter is found:
   → Reverse the 340B discount on Rx# {rx}.
   → Reprocess as a standard retail (non-340B) claim at full cost.
   → Document the determination in your audit log.

─── What Counts as a Qualifying Encounter? ────────────────────────────
• In-person visit, telehealth appointment, or consult at a registered
  entity site.
• A qualifying referral documented in the EHR where the original
  prescribing encounter occurred at an eligible site.
• Emergency department encounters at a qualifying hospital-based site
  (confirm with your entity type and HRSA classification).

─── Regulatory Reference ───────────────────────────────────────────────
HRSA 340B Program Integrity: Covered entities must maintain records
demonstrating an encounter with a patient of the entity."""


def _action_ineligible_prescriber(
    *, rx, prescriber, npi, entity, b340_id, **_
) -> str:
    return f"""\
⚕️  INELIGIBLE PRESCRIBER — Eligibility Verification Required

Rx# {rx}  |  Prescriber: {prescriber}  |  NPI: {npi}
Entity: {entity}  |  340B ID: {b340_id}

{prescriber} (NPI: {npi}) is not currently in the eligible prescriber
master for this covered entity.

─── Required Actions ───────────────────────────────────────────────────
1. Check {entity}'s credentialing / HR records to determine whether
   {prescriber} qualifies under any of the following:
     a. Employed provider (on payroll of the entity)
     b. Contracted or affiliated provider (signed contract on file)
     c. Locum tenens / moonlighting provider (contract on file)
     d. Referred prescriber — patient seen at an eligible entity site
        and referred to {prescriber} for ongoing care

2. If a valid relationship EXISTS:
   → Add {prescriber} (NPI: {npi}) to the provider master table.
   → Reprocess Rx# {rx} as a 340B claim.
   → Conduct a retrospective look-back to identify other affected claims.

3. If NO qualifying relationship can be established:
   → Reverse the 340B discount on Rx# {rx}.
   → Reprocess as a standard retail (non-340B) claim.
   → Flag for prescriber roster audit to prevent future occurrences.
   → Consider a system-level NPI block for this prescriber in your
      pharmacy's 340B eligibility filter.

─── Locating Referral Documentation ───────────────────────────────────
Search the EHR for a referral note, consultation note, or care plan
signed by an eligible entity prescriber that authorises care from
{prescriber}. The referral must be documented before the fill date.

─── Regulatory Reference ───────────────────────────────────────────────
HRSA Program Integrity Manual §5: Prescribers must be employed by or
under contract with the covered entity, or the patient must be referred
by an eligible entity prescriber with proper documentation."""


def _action_wrong_site(
    *, rx, store, pharmacy, site, enc_site, entity, b340_id,
    store_map, entity_map, **_
) -> str:
    lines = [
        "🏥  WRONG SITE — Site/Entity Mapping Failure",
        "",
        f"Rx# {rx}  |  Store: {store}  |  Pharmacy: {pharmacy}",
        f"Mapped site: {site}  |  Encounter site: {enc_site}",
        f"Entity: {entity}  |  340B ID: {b340_id}",
        "",
    ]

    if store_map == "REVIEW":
        lines += [
            f"ISSUE: Store {store} ({pharmacy}) is not fully mapped to a covered",
            f"entity site. One or more required mapping fields are missing.",
            "",
            "─── Actions for Store/Pharmacy Mapping ────────────────────────────",
            f"1. Confirm that {pharmacy} (Store {store}) is registered with HRSA",
            f"   as either:",
            f"   a. An in-house pharmacy of {entity}, OR",
            f"   b. A contract pharmacy under 340B ID {b340_id}.",
            f"   Verify at: https://340bopais.hrsa.gov",
            "",
            f"2. If the pharmacy IS registered:",
            f"   → Update the Store_Map table: populate Pharmacy location,",
            f"     Site location, Patient encounter site, 340B ID, Covered",
            f"     entity, and Entity site address.",
            f"   → Reprocess Rx# {rx} as a 340B claim.",
            "",
            f"3. If the pharmacy is NOT registered:",
            f"   → Reverse the 340B discount on Rx# {rx}.",
            f"   → Reprocess as standard retail (non-340B).",
            f"   → Contact your TPA to initiate contract pharmacy registration",
            f"     if this pharmacy should be included in your programme.",
        ]

    if entity_map == "REVIEW":
        if store_map == "REVIEW":
            lines.append("")
        lines += [
            f"ISSUE: Patient encounter site '{enc_site}' does not match any",
            f"active site on record for {entity}.",
            "",
            "─── Actions for Entity/Site Mapping ───────────────────────────────",
            f"4. Confirm the patient was registered or seen at '{enc_site}',",
            f"   which must be a qualifying location under 340B ID {b340_id}.",
            "",
            f"5. If the patient WAS seen at a valid entity site but the site name",
            f"   in the data is wrong (e.g., abbreviation or alias mismatch):",
            f"   → Update the Site_Entity_Map to add the alias.",
            f"   → Reprocess Rx# {rx}.",
            "",
            f"6. If the patient was NOT seen at a qualifying entity site:",
            f"   → Reverse the 340B discount on Rx# {rx}.",
            f"   → Reprocess as standard retail (non-340B).",
        ]

    lines += [
        "",
        "─── Regulatory Reference ───────────────────────────────────────────────",
        "HRSA requires all 340B claims to be dispensed from a registered in-house",
        "or contract pharmacy and tied to a patient encounter at a qualifying site.",
    ]
    return "\n".join(lines)


def _action_data_mismatch(
    *, rx, drug, ndc, npi, prescriber, missing_list, npi_check, ndc_check, **_
) -> str:
    lines = [
        "📋  DATA MISMATCH — EHR / Pharmacy Data Correction Required",
        "",
        f"Rx# {rx}  |  Drug: {drug}  |  NDC: {ndc}",
        f"Prescriber: {prescriber}  |  NPI: {npi}",
        "",
    ]

    if missing_list:
        lines += [
            f"MISSING FIELDS: {missing_list}",
            "",
            "─── Actions for Missing Data ───────────────────────────────────────",
            f"1. Pull the original prescription record from both the pharmacy",
            f"   system and the EHR for Rx# {rx}.",
            f"2. Populate the following required fields: {missing_list}.",
            f"3. Verify the data matches across both systems before reprocessing.",
            "",
        ]

    if ndc_check == "REVIEW":
        lines += [
            f"INVALID NDC: '{ndc}' is not a valid 10- or 11-digit NDC.",
            "",
            "─── Actions for NDC Issue ──────────────────────────────────────────",
            f"4. Locate the drug label or pharmacy dispensing record for Rx# {rx}.",
            f"   a. Cross-check the NDC against the manufacturer label.",
            f"   b. If a generic was dispensed, confirm the dispensed NDC is",
            f"      recorded (not the prescribed brand NDC).",
            f"   c. Strip any formatting (dashes, spaces) — NDC must be numeric.",
            f"   d. Correct in the pharmacy system and resubmit.",
            "",
        ]

    if npi_check == "REVIEW":
        lines += [
            f"INVALID NPI: '{npi}' is not a valid 10-digit NPI.",
            "",
            "─── Actions for NPI Issue ──────────────────────────────────────────",
            f"5. Verify the NPI for {prescriber} at:",
            f"   https://npiregistry.cms.hhs.gov",
            f"   a. If the NPI was entered with dashes or spaces, correct to",
            f"      10-digit numeric format.",
            f"   b. If the NPI is incorrect, update in both the pharmacy system",
            f"      and the EHR prescriber record.",
            "",
        ]

    lines += [
        "─── General Resolution Steps ───────────────────────────────────────",
        f"6. After correcting all data fields, re-run Rx# {rx} through the",
        f"   340B audit engine to confirm it clears.",
        f"7. If data cannot be reconciled with EHR records:",
        f"   → Reverse the 340B discount on Rx# {rx}.",
        f"   → Reprocess as standard retail (non-340B).",
        "",
        "─── Root Cause Note ────────────────────────────────────────────────",
        "Recurring data mismatches often indicate a broken interface between",
        "the EHR and the pharmacy system. Review your HL7/FHIR or ADT feed",
        "configuration with your IT/vendor team to prevent future gaps.",
    ]
    return "\n".join(lines)


def _action_duplicate_discount(
    *, rx, patient, drug, fill_date, dup_reason, carve_status, ndc,
    mef_check="N/A", mef_detail="", mef_inconsistency=False, b340_id="N/A", **_
) -> str:
    is_medicaid = any(
        kw in dup_reason.upper()
        for kw in ("MEDICAID", "STATE PLAN", "MTF", "TRICARE", "DOD")
    )
    is_exact_dup = "DUPLICATE RX" in dup_reason.upper() or not dup_reason

    lines = []

    # ── exact duplicate (no Medicaid / federal plan signal) ───────────────────
    if is_exact_dup and not is_medicaid:
        lines += [
            "⚠️  DUPLICATE CLAIM — Same Rx Appearing Multiple Times",
            "",
            f"Rx# {rx}  |  Patient: {patient}  |  Drug: {drug}",
            f"NDC: {ndc}  |  Fill date: {fill_date}",
            "",
            "This prescription appears more than once in the claims file with the",
            "same Patient, NDC, and Fill Date. Only one dispense can be a valid 340B claim.",
            "",
            "─── Required Actions ───────────────────────────────────────────────",
            f"1. Identify which record is the original, valid 340B dispense for Rx# {rx}.",
            f"2. Reverse all duplicate instances — retain only the confirmed original.",
            f"3. Investigate the root cause:",
            f"   a. Was the batch import file loaded more than once into the TPA?",
            f"   b. Was there a double-entry at the point of dispensing?",
            f"   c. Is this a legitimate refill that should carry a new Rx number?",
            f"4. Implement a deduplication rule in your TPA import workflow",
            f"   (deduplicate on Rx# + NDC + Fill Date) to prevent recurrence.",
        ]
        return "\n".join(lines)

    # ── Medicaid FFS / State Plan / MTF / TRICARE duplicate discount ──────────
    dup_signal = dup_reason if dup_reason else "Same Patient + NDC + Date"
    if "MEDICAID" in dup_reason.upper() or "STATE PLAN" in dup_reason.upper():
        header = "🚨  DUPLICATE DISCOUNT — MEDICAID FFS / STATE PLAN DETECTED"
    else:
        header = "🚨  DUPLICATE DISCOUNT — FEDERAL PLAN DETECTED (MTF / TRICARE / DOD)"

    lines += [
        header,
        "",
        f"Rx# {rx}  |  Patient: {patient}  |  Drug: {drug}  |  Fill: {fill_date}",
        f"Signal: {dup_signal}",
        "",
    ]

    carve_lower = carve_status.strip().lower()

    # ─────────────────────────────────────────────────────────────────────────
    # CARVE-IN
    # Entity uses 340B drugs for Medicaid FFS. Must be on HRSA MEF so states
    # don't request manufacturer rebates. Must bill with NDC Qualifier '20'.
    # ─────────────────────────────────────────────────────────────────────────
    if carve_lower == "carve-in":
        # ── MEF status banner ──────────────────────────────────────────────
        if mef_check == "ON_MEF":
            mef_banner = [
                f"MEF STATUS: ✅  VERIFIED — {mef_detail}",
                f"340B ID {b340_id} is listed on the HRSA Medicaid Exclusion File.",
                "The state Medicaid agency is notified NOT to request manufacturer",
                "rebates for drugs dispensed by your entity. However, the individual",
                "claim must still carry NDC Qualifier '20' when submitted to Medicaid.",
                "",
            ]
        elif mef_check == "NOT_ON_MEF":
            mef_banner = [
                f"MEF STATUS: ❌  NOT FOUND — {mef_detail}",
                f"340B ID {b340_id} was NOT found in the uploaded MEF file.",
                "⚠️  Without MEF registration, the state WILL request manufacturer",
                "rebates on your dispensed drugs — creating a duplicate discount",
                "even if you dispense 340B-priced drugs to Medicaid patients.",
                "You cannot legally use 340B pricing for Medicaid FFS patients until",
                "your Medicaid Provider NPI is listed on the MEF.",
                "",
            ]
        else:
            mef_banner = [
                "MEF STATUS: ⚠️  NOT VERIFIED — No MEF file uploaded.",
                "Upload your MEF file (sidebar) to verify whether your 340B ID is",
                "registered before proceeding with corrective actions.",
                "",
            ]

        lines += [
            "━━━  YOUR SITE IS: CARVE-IN  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "Your entity uses 340B discounted drugs for Medicaid FFS patients and",
            "must be listed on HRSA's Medicaid Exclusion File (MEF) with your",
            "Medicaid Provider NPI. The MEF tells state Medicaid agencies NOT to",
            "request manufacturer rebates for drugs you dispense under 340B.",
            "",
            "A duplicate discount occurs when the claim is not identified as 340B",
            "to the state — causing the state to still request a rebate while you",
            "also hold the 340B discounted price.",
            "",
        ] + mef_banner + [
            "─── Required Actions ───────────────────────────────────────────────",
        ]

        if mef_check == "NOT_ON_MEF":
            lines += [
                f"1. ⚠️  STOP — Your 340B ID is NOT on the MEF for this state.",
                f"   → You cannot dispense 340B-priced drugs to Medicaid FFS patients",
                f"     in this state until MEF registration is complete.",
                f"   → Reverse the 340B discount on Rx# {rx}.",
                f"   → Repurchase NDC {ndc} at WAC / standard contract price.",
                f"   → Reprocess Rx# {rx} as a standard retail (non-340B) claim.",
                f"",
                f"2. Register on HRSA's Medicaid Exclusion File:",
                f"   ▶ https://340bopais.hrsa.gov",
                f"   → Submit your Medicaid Provider NPI for each state where you",
                f"     intend to use 340B pricing for Medicaid FFS patients.",
                f"   → Once registered, future Medicaid FFS claims can use 340B",
                f"     pricing and must be submitted with NDC Qualifier '20'.",
            ]
        else:
            lines += [
                f"1. Reverse the original Medicaid claim submission for Rx# {rx}.",
                f"2. Resubmit with NDC Qualifier Code '20' (340B indicator):",
                f"     NCPDP Field 436-E1 (Basis of Cost Determination) → '20'",
                f"     This signals to the state Medicaid programme that a 340B",
                f"     discounted drug was dispensed — no rebate will be requested.",
                f"3. Confirm with your TPA / PBM that Rx# {rx} is flagged as 340B",
                f"   in their system after resubmission.",
                f"4. Verify MEF status if not yet confirmed:",
                f"   ▶ https://340bopais.hrsa.gov",
            ]

        lines += [
            "",
            f"5. Retain supporting documentation:",
            f"   a. 340B purchase invoice — NDC {ndc} purchased at 340B ceiling price.",
            f"   b. Pharmacy dispense record matching this NDC / fill date.",
            f"   c. Patient's Medicaid FFS eligibility record.",
            f"   d. MEF registration confirmation for this state.",
            "",
            f"6. Check state-specific Medicaid 340B billing requirements — some",
            f"   states require additional modifiers or have their own 340B",
            f"   programme enrolment separate from HRSA's MEF.",
            "",
            "─── Why the MEF Is the Mechanism ──────────────────────────────────",
            "Your MEF listing is the HRSA-maintained signal to states. Without it,",
            "the state sees your claim as a normal (non-340B) dispense and requests",
            "a manufacturer rebate. NDC Qualifier '20' on the individual claim is",
            "the billing-level flag that reinforces the MEF suppression.",
            "",
            "─── Regulatory References ──────────────────────────────────────────",
            "• 42 U.S.C. § 256b(a)(5)(A)(i) — Prohibition on duplicate discounts.",
            "• HRSA Medicaid Exclusion File (OPA policy notice).",
            "• CMS Informational Bulletin (July 2020) — NDC Qualifier '20'.",
        ]

    # ─────────────────────────────────────────────────────────────────────────
    # CARVE-OUT
    # Entity does NOT use 340B for Medicaid FFS. Purchases at WAC. NOT on MEF.
    # Manufacturer still receives state Medicaid rebate.
    # ─────────────────────────────────────────────────────────────────────────
    elif carve_lower == "carve-out":
        # ── MEF consistency check ──────────────────────────────────────────
        if mef_check == "ON_MEF":
            mef_banner = [
                f"⚠️  MEF INCONSISTENCY DETECTED — {mef_detail}",
                f"340B ID {b340_id} IS listed on the HRSA MEF, but your site is",
                "selected as CARVE-OUT. This is contradictory:",
                "  • Carve-out = NOT on MEF (manufacturer receives Medicaid rebate)",
                "  • Being ON the MEF signals carve-in intent to the state",
                "→ Contact your 340B TPA and legal counsel IMMEDIATELY to reconcile",
                "  your carve status election and MEF registration.",
                "",
            ]
        elif mef_check == "NOT_ON_MEF":
            mef_banner = [
                f"MEF STATUS: ✅  CONSISTENT — {mef_detail}",
                f"340B ID {b340_id} is NOT on the MEF, consistent with your carve-out",
                "election. The state Medicaid programme will request manufacturer",
                "rebates as expected — no duplicate discount from MEF perspective.",
                "However, a 340B-priced drug must not have been dispensed to this",
                "Medicaid FFS patient (see actions below).",
                "",
            ]
        else:
            mef_banner = [
                "MEF STATUS: ⚠️  NOT VERIFIED — No MEF file uploaded.",
                "Upload your MEF file (sidebar) to confirm your entity is NOT listed,",
                "which is required for a valid carve-out election.",
                "",
            ]

        lines += [
            "━━━  YOUR SITE IS: CARVE-OUT  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "Your entity does NOT use 340B discounted drugs for Medicaid FFS",
            "patients. You purchase those drugs at WAC / standard contract price",
            "and are NOT listed on HRSA's Medicaid Exclusion File (MEF). The",
            "state Medicaid programme continues to receive manufacturer rebates",
            "on drugs dispensed to your Medicaid patients.",
            "",
            "A duplicate discount occurs if a 340B-priced drug was dispensed to a",
            "Medicaid FFS patient — your entity received the 340B discount AND the",
            "manufacturer still owes the state a rebate for the same drug.",
            "",
        ] + mef_banner + [
            "─── Required Actions ───────────────────────────────────────────────",
            f"1. IMMEDIATELY reverse the 340B discount on Rx# {rx}.",
            f"2. Repurchase NDC {ndc} at WAC (Wholesale Acquisition Cost) or your",
            f"   standard contract price. Do NOT use 340B-priced inventory for",
            f"   this patient's Medicaid claim.",
            f"3. Reprocess Rx# {rx} as a standard retail (non-340B) claim.",
            f"4. Verify your entity is NOT listed on the MEF:",
            f"   ▶ https://340bopais.hrsa.gov",
            f"   If your entity IS on the MEF, your carve-out election is",
            f"   inconsistent — contact your TPA and legal counsel immediately.",
            f"5. Add this patient's Medicaid ID to the pharmacy system's 340B",
            f"   exclusion list so future fills are blocked from 340B pricing.",
            f"6. Notify your TPA / PBM of the reversal and document in audit log.",
            f"7. Conduct a look-back for this patient to identify any other",
            f"   Medicaid FFS fills incorrectly processed as 340B.",
            "",
            "─── Why This Matters ───────────────────────────────────────────────",
            "As a carve-out entity, you are not on the MEF, so the state Medicaid",
            "programme will request a manufacturer rebate. If you also applied 340B",
            "pricing, the manufacturer bears both costs — a statutory violation.",
            "Liability: repayment of manufacturer rebates + potential termination.",
            "",
            "─── Regulatory References ──────────────────────────────────────────",
            "• 42 U.S.C. § 256b(a)(5)(A)(i) — Prohibition on duplicate discounts.",
            "• HRSA OPA Policy Notice — Carve-out entities and Medicaid FFS.",
        ]

    # ─────────────────────────────────────────────────────────────────────────
    # UNKNOWN — prompt user to set carve status
    # ─────────────────────────────────────────────────────────────────────────
    else:
        lines += [
            "━━━  CARVE STATUS NOT SET — Select Before Acting  ━━━━━━━━━━━━━━",
            "⚠️  Select your entity's Carve-In / Carve-Out election at the top of",
            "the dashboard to receive specific corrective action instructions.",
            "",
            "─── Definition Reminder ────────────────────────────────────────────",
            "CARVE-IN:",
            "  Your entity uses 340B discounted drugs for Medicaid FFS patients.",
            "  You must be listed on HRSA's Medicaid Exclusion File (MEF) so the",
            "  state knows NOT to request manufacturer rebates.",
            "  Action required: verify MEF listing → resubmit with NDC Qualifier '20'.",
            "",
            "CARVE-OUT:",
            "  Your entity does NOT use 340B drugs for Medicaid FFS patients.",
            "  You purchase at WAC for those patients and are NOT on the MEF.",
            "  The manufacturer still receives the state Medicaid rebate.",
            "  Action required: reverse 340B discount → repurchase at WAC →",
            "  reprocess as standard retail.",
            "",
            "─── Regulatory Reference ───────────────────────────────────────────",
            "• 42 U.S.C. § 256b(a)(5)(A)(i) — Prohibition on duplicate discounts.",
            "• HRSA Medicaid Exclusion File (MEF) — https://340bopais.hrsa.gov",
        ]

    return "\n".join(lines)
