"""User-editable rules configuration for ACE 340B audit."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_RULES: dict[str, Any] = {
    "scoring": {
        "missing_field_penalty": 5,
        "invalid_npi_penalty": 15,
        "invalid_ndc_penalty": 10,
        "store_map_penalty": 10,
        "entity_map_penalty": 20,
        "prescriber_not_in_master_penalty": 15,
        "encounter_date_out_of_window_penalty": 20,
        "duplicate_discount_penalty": 25,
    },
    "thresholds": {
        "high_risk_max": 69,
        "medium_risk_max": 89,
        "encounter_date_window_days": 365,
    },
    "medicaid_indicators": ["MEDICAID", "MCD", "MA", "AHCCCS", "MEDI-CAL"],
    "mtf_indicators": ["MTF", "TRICARE", "DOD", "MILITARY", "VA ", "CHAMPVA"],
}

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "rules_config.json"


def load_rules() -> dict[str, Any]:
    """Load rules from disk, falling back to defaults for missing keys."""
    if _CONFIG_PATH.exists():
        try:
            with open(_CONFIG_PATH) as f:
                loaded = json.load(f)
            merged: dict[str, Any] = {}
            for section, defaults in DEFAULT_RULES.items():
                if section in loaded and isinstance(defaults, dict):
                    merged[section] = {**defaults, **loaded[section]}
                elif section in loaded:
                    merged[section] = loaded[section]
                else:
                    merged[section] = defaults.copy() if isinstance(defaults, dict) else defaults
            return merged
        except Exception:
            pass
    return {k: (v.copy() if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in DEFAULT_RULES.items()}


def save_rules(rules: dict[str, Any]) -> None:
    """Persist rules to disk."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_PATH, "w") as f:
        json.dump(rules, f, indent=2)
