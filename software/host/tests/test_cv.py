import csv
import json
from pathlib import Path

from pa_host.cv import export_cv_csv, load_cv_run, save_cv_summary, summarize_cv
from pa_host.gui_server import SettingsController
from pa_host.record import CSV_COLUMNS, Sample, sample_to_row


def _write_raw(path: Path) -> None:
    samples = [
        Sample(0, 1000, 31000, 1000000, 0, True, 0, 0, -600, 1, 1),
        Sample(1, 1124, 30000, 2000000, 0, True, 0, 0, 0, 1, 1),
        Sample(2, 1248, 29000, 3000000, 0, True, 0, 0, 600, 1, -1),
        Sample(3, 1372, 28000, 4000000, 0, True, 0, 0, 0, 1, -1),
    ]
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for index, sample in enumerate(samples):
            writer.writerow(sample_to_row(sample, 100.0 + index))


def test_cv_protocol_derives_duration_and_preserves_requested_conditions() -> None:
    settings = SettingsController.validate({
        "method": "cv",
        "cv_low_v": -0.6,
        "cv_high_v": 0.6,
        "cv_scan_rate_v_s": 0.05,
        "cv_cycles": 30,
        "cv_quiet_s": 2,
        "fsr_nA": 2000,
        "offset_mode": "50pct",
    })
    assert settings["duration_s"] == 1440
    assert settings["prestep_s"] == 2
    assert settings["initial_potential_v"] == -0.6
    assert settings["cv_step_v"] == 0.001


def test_cv_raw_export_and_summary(tmp_path: Path) -> None:
    raw = tmp_path / "raw.csv"
    exported = tmp_path / "cv.csv"
    summary_path = tmp_path / "summary.json"
    _write_raw(raw)
    settings = SettingsController.validate({"method": "cv", "cv_cycles": 1})

    data = load_cv_run(raw)
    assert data["potential_v"].tolist() == [-0.6, 0.0, 0.6, 0.0]
    assert data["current_nA"].tolist() == [1.0, 2.0, 3.0, 4.0]

    export_cv_csv(raw, exported, settings)
    text = exported.read_text()
    assert "Cyclic Voltammetry" in text
    assert "Potential (V),Current (A),Current (uA),Current (nA)" in text

    summary = summarize_cv(raw, settings)
    assert summary.sample_count == 4
    assert summary.cycles_observed == 1
    assert summary.current_min_nA == 1.0
    assert summary.current_max_nA == 4.0
    save_cv_summary(summary, summary_path)
    assert json.loads(summary_path.read_text())["cycles_observed"] == 1
