# ACE 340B AI Audit MVP

This MVP uses your MAP sample workbook structure to create a working 340B scrubbing engine and dashboard.

## What it does
- Reads `Raw_Data`, `Store_Map`, and `Site_Entity_Map` from an Excel workbook.
- Validates required claim fields.
- Checks provider NPI format and NDC format.
- Applies store/site/entity mapping logic.
- Assigns `PASS` or `REVIEW`, a risk score, a risk tier, and a primary issue.
- Produces a Streamlit dashboard with KPIs, issue charts, store review-rate charts, and downloadable review files.

## Included files
- `app.py` - Streamlit dashboard
- `ace_340b_audit/engine.py` - rule engine and scoring logic
- `run_sample.py` - generates CSV outputs from the bundled MAP workbook
- `sample_outputs/` - generated CSV files based on the sample workbook

## How to run
```bash
cd ace_340b_mvp
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Expected workbook tabs
- `Raw_Data`
- `Store_Map`
- `Site_Entity_Map`

## Current scoring logic
- Start at 100 points
- -5 for each missing required field
- -15 for invalid or missing provider NPI
- -10 for invalid or missing NDC
- -10 for incomplete store mapping
- -20 for incomplete or mismatched entity mapping

## Next upgrades I recommend
1. Add prescriber master validation against an ACE HRSA-eligible provider table.
2. Add encounter-date validation from ECW.
3. Add duplicate discount checks for Medicaid and MTF logic.
4. Add user-editable rules and exception management.
5. Add API endpoints for scheduled daily ingestion.
