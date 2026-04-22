from pathlib import Path

from ace_340b_audit.engine import run_audit_from_workbook

BASE = Path(__file__).resolve().parent
input_file = BASE / "MAP_340B_Compliance_Analytics_System_Jan2025.xlsx"
out_dir = BASE / "sample_outputs"
out_dir.mkdir(exist_ok=True)

results = run_audit_from_workbook(input_file)
for name, df in results.items():
    df.to_csv(out_dir / f"{name}.csv", index=False)

print(f"Wrote sample outputs to {out_dir}")
