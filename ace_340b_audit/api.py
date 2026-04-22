"""FastAPI REST endpoints for scheduled daily ingestion of 340B claims."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from .engine import run_audit_from_workbook
from .report import generate_html_report
from .rules import load_rules, save_rules

app = FastAPI(
    title="ACE 340B Audit API",
    version="1.0.0",
    description="REST endpoints for 340B compliance auditing and scheduled daily ingestion.",
    docs_url="/",
)


# ── health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Liveness check."""
    return {"status": "ok", "service": "ACE 340B Audit API"}


# ── audit ─────────────────────────────────────────────────────────────────────

@app.post("/audit", tags=["audit"])
async def audit_workbook(
    workbook: UploadFile = File(..., description="340B workbook (.xlsx)"),
    exceptions: UploadFile | None = File(None, description="Approved exceptions CSV (optional)"),
    provider_master: UploadFile | None = File(None, description="HRSA provider master CSV (optional)"),
    mef: UploadFile | None = File(None, description="HRSA Medicaid Exclusion File CSV (optional)"),
    carve_status: str = "unknown",
) -> JSONResponse:
    """
    Run a full 340B audit on an uploaded workbook.

    Returns a JSON summary with pass/review counts, issue breakdown, and
    risk-tier distribution. Full claim-level detail can be fetched via
    the /audit/claims endpoint (POST with same files).
    """
    if not (workbook.filename or "").endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Only .xlsx workbooks are accepted.")

    rules = load_rules()

    # Write uploaded files to temp paths
    wb_path = _save_upload(await workbook.read(), suffix=".xlsx")
    exc_df: pd.DataFrame | None = None
    pm_df: pd.DataFrame | None = None

    try:
        if exceptions:
            exc_bytes = await exceptions.read()
            exc_df = pd.read_csv(_save_upload(exc_bytes, suffix=".csv"))

        if provider_master:
            pm_bytes = await provider_master.read()
            pm_df = pd.read_csv(_save_upload(pm_bytes, suffix=".csv"))

        mef_df: pd.DataFrame | None = None
        if mef:
            mef_bytes = await mef.read()
            mef_df = pd.read_csv(_save_upload(mef_bytes, suffix=".csv"))

        results = run_audit_from_workbook(
            wb_path,
            rules=rules,
            exceptions=exc_df,
            provider_master=pm_df,
            mef=mef_df,
            carve_status=carve_status,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        Path(wb_path).unlink(missing_ok=True)

    summary = {row.metric: row.value for row in results["summary"].itertuples(index=False)}
    issue_breakdown = results["issue_summary"].to_dict(orient="records")
    store_breakdown = results["store_status"].to_dict(orient="records")

    return JSONResponse({
        "status": "ok",
        "summary": summary,
        "issue_breakdown": issue_breakdown,
        "store_breakdown": store_breakdown,
    })


@app.post("/audit/claims", tags=["audit"])
async def audit_claims_detail(
    workbook: UploadFile = File(..., description="340B workbook (.xlsx)"),
    exceptions: UploadFile | None = File(None, description="Approved exceptions CSV (optional)"),
    provider_master: UploadFile | None = File(None, description="HRSA provider master CSV (optional)"),
    status_filter: str = "REVIEW",
    carve_status: str = "unknown",
) -> JSONResponse:
    """
    Return claim-level audit detail.  Use status_filter=REVIEW (default),
    PASS, EXCEPTION, or ALL.
    """
    if not (workbook.filename or "").endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Only .xlsx workbooks are accepted.")

    rules = load_rules()
    wb_path = _save_upload(await workbook.read(), suffix=".xlsx")
    exc_df = None
    pm_df = None

    try:
        if exceptions:
            exc_df = pd.read_csv(_save_upload(await exceptions.read(), suffix=".csv"))
        if provider_master:
            pm_df = pd.read_csv(_save_upload(await provider_master.read(), suffix=".csv"))

        results = run_audit_from_workbook(
            wb_path,
            rules=rules,
            exceptions=exc_df,
            provider_master=pm_df,
            carve_status=carve_status,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        Path(wb_path).unlink(missing_ok=True)

    claims = results["claims"]
    if status_filter.upper() != "ALL":
        claims = claims[claims["Overall status"] == status_filter.upper()]

    return JSONResponse({
        "status": "ok",
        "total": len(claims),
        "claims": _df_to_json_safe(claims),
    })


@app.post("/audit/report", tags=["audit"], response_class=HTMLResponse)
async def audit_report(
    workbook: UploadFile = File(..., description="340B workbook (.xlsx)"),
    exceptions: UploadFile | None = File(None, description="Approved exceptions CSV (optional)"),
    provider_master: UploadFile | None = File(None, description="HRSA provider master CSV (optional)"),
    mef: UploadFile | None = File(None, description="HRSA Medicaid Exclusion File CSV (optional)"),
    carve_status: str = "unknown",
) -> HTMLResponse:
    """
    Run a full 340B audit and return a complete standalone HTML report.

    The report includes: executive summary, compliance category breakdown,
    per-claim corrective action plans for all REVIEW claims (sorted by severity),
    MEF verification summary (if MEF provided), store performance table,
    and regulatory references.

    Open the returned HTML directly in a browser or print to PDF.
    """
    if not (workbook.filename or "").endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Only .xlsx workbooks are accepted.")

    rules   = load_rules()
    wb_path = _save_upload(await workbook.read(), suffix=".xlsx")
    exc_df: pd.DataFrame | None = None
    pm_df:  pd.DataFrame | None = None
    mef_df: pd.DataFrame | None = None

    try:
        if exceptions:
            exc_df = pd.read_csv(_save_upload(await exceptions.read(), suffix=".csv"))
        if provider_master:
            pm_df  = pd.read_csv(_save_upload(await provider_master.read(), suffix=".csv"))
        if mef:
            mef_df = pd.read_csv(_save_upload(await mef.read(), suffix=".csv"))

        results = run_audit_from_workbook(
            wb_path,
            rules=rules,
            exceptions=exc_df,
            provider_master=pm_df,
            mef=mef_df,
            carve_status=carve_status,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        Path(wb_path).unlink(missing_ok=True)

    html = generate_html_report(
        results,
        carve_status=carve_status,
        workbook_name=workbook.filename or "uploaded_workbook.xlsx",
        mef_loaded=(mef_df is not None),
    )
    return HTMLResponse(content=html, status_code=200)


# ── rules ─────────────────────────────────────────────────────────────────────

@app.get("/rules", tags=["rules"])
def get_rules() -> dict[str, Any]:
    """Return the current scoring and threshold rules."""
    return load_rules()


@app.post("/rules", tags=["rules"])
def update_rules(rules: dict[str, Any]) -> dict[str, str]:
    """
    Persist updated rules.  Only provided keys are updated; all others
    retain their current values.
    """
    try:
        current = load_rules()
        for section, vals in rules.items():
            if section in current and isinstance(vals, dict):
                current[section].update(vals)
            else:
                current[section] = vals
        save_rules(current)
        return {"status": "saved"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/rules/reset", tags=["rules"])
def reset_rules() -> dict[str, str]:
    """Reset rules to factory defaults."""
    from .rules import DEFAULT_RULES
    try:
        save_rules(DEFAULT_RULES)
        return {"status": "reset to defaults"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── helpers ───────────────────────────────────────────────────────────────────

def _save_upload(data: bytes, suffix: str = "") -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        return f.name


def _df_to_json_safe(df: pd.DataFrame) -> list[dict]:
    """Convert DataFrame to JSON-serialisable list, handling NaT/NaN."""
    return df.where(df.notna(), other=None).to_dict(orient="records")
