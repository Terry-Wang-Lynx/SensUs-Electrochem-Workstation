import ast
import csv
import json
from pathlib import Path

import pytest

from pa_host.cv import export_cv_csv, load_cv_run, save_cv_summary, summarize_cv
from pa_host.gui_server import SettingsController
from pa_host.record import CSV_COLUMNS, Sample, sample_to_row

CV_SOURCE = Path(__file__).parents[1] / "pa_host" / "cv.py"


def _write_raw(path: Path) -> None:
    samples = [
        Sample(0, 1000, 31000, 1000000, 0, True, 0, 0, 0, -600, 1, 1),
        Sample(1, 1124, 30000, 2000000, 0, True, 0, 0, 0, 0, 1, 1),
        Sample(2, 1248, 29000, 3000000, 0, True, 0, 0, 0, 600, 1, -1),
        Sample(3, 1372, 28000, 4000000, 0, True, 0, 0, 0, 0, 1, -1),
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
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


def _text_io_without_encoding(module_path: Path) -> list[int]:
    """列出模块里所有"文本模式打开却没写 encoding="的行号(二进制模式跳过)。"""

    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))

    def keyword(call: ast.Call, name: str) -> ast.keyword | None:
        return next((item for item in call.keywords if item.arg == name), None)

    def string_literal(node: ast.expr) -> str | None:
        return (
            node.value
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            else None
        )

    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = (
            function.attr if isinstance(function, ast.Attribute)
            else function.id if isinstance(function, ast.Name) else None
        )
        if name not in {"open", "read_text", "write_text"}:
            continue
        mode: str | None = None
        if name == "open":
            positional = node.args[1:2] if isinstance(function, ast.Name) else node.args[:1]
            if positional:
                mode = string_literal(positional[0])
            explicit = keyword(node, "mode")
            if explicit is not None:
                mode = string_literal(explicit.value)
        if mode and "b" in mode:
            continue
        if keyword(node, "encoding") is None:
            offenders.append(node.lineno)
    return offenders


def test_gbk_polluted_cv_raw_is_loaded_with_a_warning(tmp_path: Path) -> None:
    """🔴 中文 Windows 用 cp936 落盘的历史 CV 文件必须还能打开,只是带警告。

    撤掉 ``load_cv_run`` 的编码回退后,这里会在第一行注释的中文上抛
    UnicodeDecodeError,整条历史曲线/汇总链路直接断掉。
    """

    clean = tmp_path / "clean.csv"
    _write_raw(clean)
    polluted = tmp_path / "gbk-raw.csv"
    polluted.write_bytes(
        ("# 备注: 低通截止频率已限制为奈奎斯特频率的 90%\n"
         + clean.read_text(encoding="utf-8")).encode("gb18030")
    )
    with pytest.raises(UnicodeDecodeError):
        polluted.read_bytes().decode("utf-8")

    data = load_cv_run(polluted)

    assert data["source_encoding"] == "gb18030"
    assert data["potential_v"].tolist() == [-0.6, 0.0, 0.6, 0.0]
    assert data["current_nA"].tolist() == [1.0, 2.0, 3.0, 4.0]

    settings = SettingsController.validate({"method": "cv", "cv_cycles": 1})
    summary = summarize_cv(polluted, settings)
    assert summary.sample_count == 4
    assert any("回退解码" in warning for warning in summary.warnings)

    # 导出必须落成干净 UTF-8,污染不再传染下去。
    exported = tmp_path / "cv.csv"
    export_cv_csv(polluted, exported, settings)
    assert "Cyclic Voltammetry" in exported.read_bytes().decode("utf-8")


def test_clean_cv_raw_reports_utf8_and_no_encoding_warning(tmp_path: Path) -> None:
    """反面样本:干净文件不能被这条容错逻辑误报成"编码有问题"。"""

    raw = tmp_path / "raw.csv"
    _write_raw(raw)
    assert load_cv_run(raw)["source_encoding"] == "utf-8"
    settings = SettingsController.validate({"method": "cv", "cv_cycles": 1})
    assert not any(
        "回退解码" in warning for warning in summarize_cv(raw, settings).warnings
    )


def test_every_text_file_open_in_cv_declares_utf8() -> None:
    """机器门禁:``cv.py`` 里任何文本 I/O 都必须显式带 encoding(与 locale 无关)。"""

    assert _text_io_without_encoding(CV_SOURCE) == []
