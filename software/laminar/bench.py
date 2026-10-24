"""Load robotics/bench.yaml. YAML is in mm / mL / rad; helpers convert to code units (m, rad)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import yaml

from .telemetry import REPO_ROOT

DEFAULT_BENCH = REPO_ROOT / "robotics" / "bench.yaml"


def load_bench(path: str | Path | None = None) -> dict:
    with Path(path or DEFAULT_BENCH).open() as f:
        return yaml.safe_load(f)


def pose(bench: dict, name: str) -> np.ndarray:
    """Taught joint pose by name, rad."""
    try:
        return np.array(bench["poses"][name], dtype=float)
    except KeyError:
        raise KeyError(f"no taught pose '{name}' in bench.yaml") from None


def workspace_m(bench: dict) -> tuple[np.ndarray, np.ndarray]:
    ws = bench["workspace_mm"]
    return np.array(ws["min"]) / 1000.0, np.array(ws["max"]) / 1000.0


def rack_slots(bench: dict) -> list[str]:
    return sorted(k[len("rack_"):-len("_pre")] for k in bench["poses"] if k.startswith("rack_") and k.endswith("_pre"))
