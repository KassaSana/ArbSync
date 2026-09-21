from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RESULTS_PATH = Path("artifacts/benchmarks/results.json")


def load_results() -> dict[str, Any]:
    if not RESULTS_PATH.exists():
        return {}
    loaded: dict[str, Any] = json.loads(RESULTS_PATH.read_text())
    return loaded


def save_result(name: str, payload: dict[str, Any]) -> None:
    results = load_results()
    results[name] = payload
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(results, indent=2, sort_keys=True))
