from __future__ import annotations

import csv
import json
import re
import subprocess
from pathlib import Path

import numpy as np
import pytest

import pa_host.filtering as filtering
from pa_host.filtering import (
    apply_filter,
    validate_filter_config,
    write_filtered_csv,
)


GUI_APP = Path(__file__).parents[1] / "pa_host" / "gui" / "app.js"


def _trace(fs: float = 8.0, n: int = 400):
    t = np.arange(n, dtype=float) / fs
    y = 2.0 + 0.15 * np.sin(2 * np.pi * 1.2 * t)
    return t, y, np.ones(n, dtype=bool)


def test_off_is_bit_for_bit_and_does_not_change_invalid_rows() -> None:
    t, y, valid = _trace()
    valid[120] = False
    out, meta = apply_filter(t, y, valid, {"mode": "off"})
    assert np.array_equal(out, y)
    assert meta["applied"] is False


def test_boolean_strings_are_parsed_without_truthiness_bug() -> None:
    config = validate_filter_config({
        "lowpass_enabled": "false",
        "lowpass_auto": "false",
    })
    assert config["lowpass_enabled"] is False
    assert config["lowpass_auto"] is False


def test_manual_lowpass_rejects_zero_cutoff() -> None:
    with pytest.raises(ValueError, match="lowpass_cutoff_hz"):
        validate_filter_config({
            "lowpass_enabled": True,
            "lowpass_auto": False,
            "lowpass_cutoff_hz": 0,
        })


def test_manual_cutoff_is_clamped_and_reported() -> None:
    t, y, valid = _trace(fs=8.0, n=400)
    _, meta = apply_filter(t, y, valid, {
        "mode": "display", "lowpass_enabled": True,
        "lowpass_auto": False, "lowpass_cutoff_hz": 50,
    })
    assert meta["lowpass_cutoff_hz"] == 3.6
    assert "限制" in meta["note"]


def test_nan_is_kept_out_of_filter_segments() -> None:
    t, y, valid = _trace(fs=8.0, n=120)
    y[60] = np.nan
    out, meta = apply_filter(t, y, valid, {
        "mode": "display", "lowpass_enabled": True,
    })
    assert meta["applied"] is True
    assert np.isnan(out[60])
    assert np.isfinite(out[59]) and np.isfinite(out[61])


def _extract_js_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    opening = source.index("{", start)
    depth = 0
    for offset in range(opening, len(source)):
        if source[offset] == "{":
            depth += 1
        elif source[offset] == "}":
            depth -= 1
            if depth == 0:
                return source[start:offset + 1]
    raise AssertionError(f"Unterminated JavaScript function: {name}")


def _browser_filter_values(
    times: np.ndarray, values: np.ndarray, valid: np.ndarray,
    config: dict[str, object],
) -> tuple[np.ndarray, dict[str, object]]:
    source = GUI_APP.read_text(encoding="utf-8")
    functions = "\n".join(_extract_js_function(source, name) for name in (
        "filterRate", "filterLowpass", "filterValues",
    ))
    script = f"""
{functions}
const state = {{filter: {json.dumps(config)}}};
const FILTER_DEFAULTS = {{mode: 'display'}};
function fmt(value) {{ return String(value); }}
const result = filterValues(
  {json.dumps(times.tolist())},
  {json.dumps(values.tolist())},
  {json.dumps(valid.tolist())}
);
console.log(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True,
    )
    result = json.loads(completed.stdout)
    return np.asarray(result["values"], dtype=float), result["meta"]


def test_backend_lowpass_matches_browser_preview_point_for_point() -> None:
    t, y, valid = _trace(fs=8.0, n=300)
    out, meta = filtering.apply_filter(t, y, valid, {
        "mode": "analysis", "lowpass_enabled": True,
        "lowpass_auto": False, "lowpass_cutoff_hz": 0.7,
        "lowpass_order": 3,
    })
    expected, browser_meta = _browser_filter_values(t, y, valid, {
        "mode": "analysis", "lowpass_enabled": True,
        "lowpass_auto": False, "lowpass_cutoff_hz": 0.7,
        "lowpass_order": 3,
    })
    assert meta["applied"] is True
    assert browser_meta["lowpass_cutoff_hz"] == meta["lowpass_cutoff_hz"]
    assert np.allclose(out, expected, rtol=0.0, atol=1e-14)


def test_backend_and_browser_split_the_same_invalid_segments() -> None:
    t, y, valid = _trace(fs=8.0, n=120)
    t[60] = np.nan
    valid[80] = False
    config = {
        "mode": "display", "lowpass_enabled": True,
        "lowpass_auto": True, "lowpass_order": 2,
    }
    backend, _ = apply_filter(t, y, valid, config)
    browser, _ = _browser_filter_values(t, y, valid, config)

    assert np.allclose(backend, browser, rtol=0.0, atol=1e-14, equal_nan=True)


def test_filtered_csv_preserves_raw_column_and_invalid_mask(tmp_path) -> None:
    source = tmp_path / "raw.csv"
    output = tmp_path / "filtered.csv"
    with source.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time_s", "current_nA", "valid", "sat", "ovf"])
        for i in range(40):
            writer.writerow([
                float("nan") if i == 22 else i / 8,
                1.0 + (i in (20, 21)), 1,
                int(i == 20), int(i == 21),
            ])
    write_filtered_csv(source, output, {"mode": "analysis", "lowpass_enabled": True})
    rows = list(csv.DictReader(line for line in output.open() if not line.startswith("#")))
    assert rows[20]["valid"] == "0"
    assert rows[20]["sat"] == "1"
    assert rows[21]["valid"] == "0"
    assert rows[21]["ovf"] == "1"
    assert rows[22]["valid"] == "0"
    assert float(rows[20]["raw_current_nA"]) == 2.0
    assert len(rows) == 40


def test_filtered_csv_never_overwrites_source(tmp_path) -> None:
    source = tmp_path / "raw.csv"
    source.write_text("time_s,current_nA,valid,sat\n0,1,1,0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="不能覆盖原始"):
        write_filtered_csv(source, source)
    assert source.read_text(encoding="utf-8").startswith("time_s,current_nA")
