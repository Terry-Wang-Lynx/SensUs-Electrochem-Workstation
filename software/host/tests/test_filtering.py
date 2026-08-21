from __future__ import annotations

import ast
import codecs
import csv
import io
import json
import os
import re
import subprocess
import sys
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
HOST_ROOT = Path(__file__).parents[1]
FILTERING_SOURCE = HOST_ROOT / "pa_host" / "filtering.py"

# 让 apply_filter 走进"截止频率被钳到奈奎斯特 90%"分支,meta["note"] 因此带中文
# ——这正是把 GBK 写进 CSV 注释行的那个字符串来源。
CLAMPING_FILTER = {
    "mode": "analysis", "lowpass_enabled": True,
    "lowpass_auto": False, "lowpass_cutoff_hz": 50,
}
CLAMP_NOTE_FRAGMENT = "低通截止频率已限制"


def _write_ascii_run_csv(path: Path, rows: int = 40) -> None:
    """一份纯 ASCII 的 8 Hz 输入,保证被测的只有输出侧编码。"""

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["time_s", "current_nA", "valid", "sat", "ovf"])
    for index in range(rows):
        writer.writerow([index / 8, 1.0 + 0.01 * index, 1, 0, 0])
    path.write_text(buffer.getvalue(), encoding="utf-8")


def _read_filter_comment(path: Path) -> str:
    """按 UTF-8 严格解码并取回 ``# filter:`` 注释行(解码失败即缺陷复现)。"""

    text = path.read_bytes().decode("utf-8")
    for line in text.splitlines():
        if line.startswith("# filter:"):
            return line
    raise AssertionError(f"滤波输出缺少 # filter: 注释行：{path}")


def _text_io_without_encoding(module_path: Path) -> list[int]:
    """列出模块里所有"文本模式打开却没写 encoding="的行号。

    二进制模式(``"rb"/"wb"/"ab"`` …)不需要 encoding,跳过。
    """

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


def _trace(fs: float = 8.0, n: int = 400):
    t = np.arange(n, dtype=float) / fs
    y = 2.0 + 0.15 * np.sin(2 * np.pi * 1.2 * t)
    return t, y, np.ones(n, dtype=bool)


def test_default_filter_matches_the_workstation_baseline() -> None:
    assert validate_filter_config() == {
        "mode": "analysis",
        "lowpass_enabled": True,
        "lowpass_cutoff_hz": 0.3,
        "lowpass_auto": False,
        "lowpass_order": 4,
    }


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


def test_filter_comment_line_is_utf8_even_under_a_gbk_locale(tmp_path) -> None:
    """🔴 中文 Windows(cp936)现场缺陷的等价复现。

    在 macOS 上 locale 默认就是 UTF-8,漏写 ``encoding=`` 永远测不出来,所以这里
    用 ``LC_ALL=zh_CN.GBK`` + 关掉 Python UTF-8 模式的子进程去写文件,再在父进程
    里按 UTF-8 严格解码字节。撤掉 ``write_filtered_csv`` 里的 ``encoding="utf-8"``
    时,这条断言会直接抛 UnicodeDecodeError。
    """

    source = tmp_path / "raw.csv"
    output = tmp_path / "filtered.csv"
    _write_ascii_run_csv(source)

    script = (
        "import json, locale, sys\n"
        f"sys.path.insert(0, {str(HOST_ROOT)!r})\n"
        "from pa_host.filtering import write_filtered_csv\n"
        f"meta = write_filtered_csv({str(source)!r}, {str(output)!r},"
        f" {CLAMPING_FILTER!r})\n"
        "print(json.dumps({'locale_encoding': locale.getencoding(),"
        " 'note': meta['note']}))\n"
    )
    environment = {
        **os.environ,
        "PYTHONUTF8": "0",
        "LC_ALL": "zh_CN.GBK",
        "LANG": "zh_CN.GBK",
    }
    environment.pop("PYTHONIOENCODING", None)
    completed = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", script],
        capture_output=True, text=True, env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    reported = json.loads(completed.stdout.strip().splitlines()[-1])
    if codecs.lookup(reported["locale_encoding"]).name == "utf-8":
        pytest.skip(
            "本机没有 GBK locale，无法复现中文 Windows 的 cp936 默认编码"
        )

    assert CLAMP_NOTE_FRAGMENT in reported["note"]
    comment = _read_filter_comment(output)
    assert CLAMP_NOTE_FRAGMENT in comment
    # 注释行现在是 JSON,不再是 dict 的 repr,下游排错时可以直接解析。
    assert CLAMP_NOTE_FRAGMENT in json.loads(comment[len("# filter:"):])["note"]


def test_gbk_polluted_source_is_filtered_with_a_warning_instead_of_dying(
    tmp_path,
) -> None:
    """已经被 GBK 污染的文件还在现场磁盘上:读取端必须容错并留下警告。"""

    utf8_source = tmp_path / "clean.csv"
    _write_ascii_run_csv(utf8_source)
    source = tmp_path / "gbk.csv"
    output = tmp_path / "filtered.csv"
    polluted_comment = (
        "# filter: {'applied': True, 'note': '低通截止频率已限制为奈奎斯特"
        "频率的 90%（3.6 Hz）'}\n"
    )
    source.write_bytes(
        (polluted_comment + utf8_source.read_text(encoding="utf-8")).encode("gb18030")
    )
    with pytest.raises(UnicodeDecodeError):
        source.read_bytes().decode("utf-8")

    meta = write_filtered_csv(source, output, CLAMPING_FILTER)

    assert meta["source_encoding"] == "gb18030"
    assert "回退解码" in meta["note"]
    assert CLAMP_NOTE_FRAGMENT in meta["note"]
    rows = list(csv.DictReader(
        line for line in output.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ))
    assert len(rows) == 40
    assert float(rows[0]["raw_current_nA"]) == 1.0
    # 输出必须重新落成干净的 UTF-8,污染不再传染下去。
    assert "回退解码" in _read_filter_comment(output)


def test_every_text_file_open_in_filtering_declares_utf8() -> None:
    """机器门禁:``filtering.py`` 里任何文本 I/O 都必须显式带 encoding。

    这条不依赖 locale,在任何平台上都能挡住"下次又忘了写 encoding"。
    """

    assert _text_io_without_encoding(FILTERING_SOURCE) == []
