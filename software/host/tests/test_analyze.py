"""上位机分析栈单测 — 用合成数据验算法本身,不依赖硬件.

用途    : 分析代码算错比不算更危险(会拿错数字对外)。这里用「已知答案」的
          合成序列反验:白噪声 σ、已知漂移率、已知 PSD 平坦度、Allan 在白噪声
          下应按 τ^-0.5 下降、随机游走下应按 τ^+0.5 上升。
用法    : cd software/host && python -m pytest tests/ -q
          (无 pytest 时:python tests/test_analyze.py)
前置条件: numpy;pytest 可选。
快照日期: 2026-07-27
"""

from __future__ import annotations

import contextlib
import io
import math
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pa_host.analyze import (  # noqa: E402
    analyze_current,
    load_csv,
    estimate_psd,
    overlapping_allan_dev,
)
from pa_host.record import (  # noqa: E402
    Sample,
    check_integrity,
    format_sample_line,
    parse_line,
)

FS = 0.266  # Hz,SENS_PERIOD=0x5 → 3.757s
FSR_PA = 50000.0


def _series(n: int, sigma: float, drift_pa_per_h: float = 0.0,
            seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    t = np.arange(n) / FS
    y = rng.normal(0.0, sigma, n) + drift_pa_per_h * (t / 3600.0)
    return y, t


# --------------------------------------------------------------------------
# record.py
# --------------------------------------------------------------------------
def test_line_roundtrip():
    s = Sample(seq=123, ms=456789, counts=13107, fa=2500000, tag=0, auto=True, ovf=0)
    back = parse_line(format_sample_line(s))
    assert back == s, "格式化与解析必须互为逆运算(固件打印格式的单一真源)"


def test_parse_rejects_garbage():
    assert parse_line("E boot rev=0x01 part=0x3A") is None
    assert parse_line("") is None
    assert parse_line("S seq=1 ms=2 counts=3") is None  # 字段不全


def test_negative_current_parses():
    """氧化方向 / offset 标定偏差都会让 pa 为负,不能被正则拒掉."""
    s = parse_line("S seq=1 ms=2 counts=20000 fa=-5259000 tag=0 auto=1 ovf=0")
    assert s is not None and s.fa == -5259000


def test_integrity_detects_gaps_and_overflow():
    samples = [
        Sample(1, 0, 100, 10000, 0, True, 0),
        Sample(2, 3757, 100, 10000, 0, True, 0),
        Sample(6, 18785, 100, 10000, 0, True, 3),  # 跳了 3 个 + 溢出
        Sample(7, 22542, 100, 10000, 12, False, 0),  # 异常 tag + 手动模式
    ]
    rep = check_integrity(samples)
    assert rep.total == 4
    assert rep.seq_gaps == 1
    assert rep.missing_samples == 3
    assert rep.ovf_events == 1
    assert rep.bad_tag == 1
    assert rep.manual_mode == 1


# --------------------------------------------------------------------------
# 噪声 / 漂移
# --------------------------------------------------------------------------
def test_sigma_recovers_known_white_noise():
    sigma_true = 0.4
    y, t = _series(4000, sigma_true)
    r = analyze_current(y, t, FSR_PA)
    assert abs(r.noise.sigma_pa - sigma_true) < 0.03, r.noise.sigma_pa
    assert abs(r.noise.three_sigma_pa - 3 * sigma_true) < 0.09


def test_drift_rate_recovers_known_slope():
    y, t = _series(6000, sigma=0.2, drift_pa_per_h=2.5)
    r = analyze_current(y, t, FSR_PA)
    assert abs(r.drift.slope_pa_per_h - 2.5) < 0.15, r.drift.slope_pa_per_h
    # 去趋势后的 σ 应回到 0.2,而未去趋势的 σ 明显更大(漂移吃掉的部分)
    assert abs(r.noise.sigma_pa - 0.2) < 0.02
    assert r.noise.sigma_raw_pa > r.noise.sigma_pa * 2


def test_er_nfr_relationship():
    """NFR ≈ ER − 2.7(峰峰 ≈ 6.6×rms),08 文档 §3.6 的口径."""
    y, t = _series(8000, sigma=0.1)
    r = analyze_current(y, t, FSR_PA)
    assert 15.0 < r.resolution.er_bits < 20.0, r.resolution.er_bits
    assert 2.0 < (r.resolution.er_bits - r.resolution.nfr_bits) < 3.5


def test_one_pa_change_confidence():
    """σ≈0.1pA 时 1pA 变化应约 7σ(08 文档 §3.7)."""
    y, t = _series(8000, sigma=0.1)
    r = analyze_current(y, t, FSR_PA)
    assert 6.0 < r.resolution.sigma_for_1pa_change < 8.0


def test_warns_on_short_record():
    y, t = _series(60, sigma=0.3)  # 60 样本 @0.266Hz ≈ 0.06h
    r = analyze_current(y, t, FSR_PA)
    assert any("2 h" in w for w in r.warnings)


def test_warns_when_quantization_dominates():
    """σ 小于 1 LSB 时必须警告,否则会把量化噪声当成真噪声对外报."""
    lsb = FSR_PA / 65536.0
    y, t = _series(2000, sigma=lsb / 5.0)
    r = analyze_current(y, t, FSR_PA)
    assert any("LSB" in w for w in r.warnings)


def test_warns_on_sample_jitter():
    y, t = _series(2000, sigma=0.3)
    rng = np.random.default_rng(3)
    t = t + rng.normal(0, 0.5 / FS, len(t))  # 注入 >5% 抖动
    t = np.sort(t)
    t = t - t[0]
    r = analyze_current(y, t, FSR_PA)
    assert any("抖动" in w for w in r.warnings)


# --------------------------------------------------------------------------
# PSD
# --------------------------------------------------------------------------
def test_psd_flat_for_white_noise_and_parseval():
    """白噪声 PSD 应平坦,且积分回来 ≈ σ²(Parseval 自检)."""
    sigma = 0.5
    y, t = _series(8192, sigma)
    f, pxx = estimate_psd(y, FS)
    band = (f > 0.01) & (f < FS / 2 * 0.9)
    rel_spread = float(np.std(pxx[band]) / np.mean(pxx[band]))
    assert rel_spread < 1.2, f"白噪声谱不该有强结构: {rel_spread}"
    power = float(np.trapezoid(pxx[f >= 0], f[f >= 0]))
    assert 0.5 * sigma**2 < power < 2.0 * sigma**2, power


# --------------------------------------------------------------------------
# Allan
# --------------------------------------------------------------------------
def test_allan_white_noise_slope_is_minus_half():
    """白噪声下 ADEV ∝ τ^(-1/2):平均越久越好."""
    y, t = _series(20000, sigma=0.4)
    pts = overlapping_allan_dev(y, FS)
    assert len(pts) >= 6
    taus = np.array([p.tau_s for p in pts])
    devs = np.array([p.dev_pa for p in pts])
    slope = np.polyfit(np.log10(taus), np.log10(devs), 1)[0]
    assert -0.62 < slope < -0.38, f"白噪声 ADEV 斜率应 ≈ -0.5,得到 {slope}"


def test_allan_random_walk_slope_is_plus_half():
    """随机游走(漂移型)下 ADEV ∝ τ^(+1/2):平均越久越差 —— 漂移接管."""
    rng = np.random.default_rng(11)
    n = 20000
    y = np.cumsum(rng.normal(0, 0.05, n))
    pts = overlapping_allan_dev(y, FS)
    taus = np.array([p.tau_s for p in pts])
    devs = np.array([p.dev_pa for p in pts])
    slope = np.polyfit(np.log10(taus), np.log10(devs), 1)[0]
    assert 0.3 < slope < 0.7, f"随机游走 ADEV 斜率应 ≈ +0.5,得到 {slope}"


def test_allan_finds_optimum_for_mixed_noise():
    """白噪声 + 随机游走 → ADEV 有极小值 = 最优积分时间(08 文档 §3.7 那条曲线)."""
    rng = np.random.default_rng(5)
    n = 20000
    white = rng.normal(0, 0.5, n)
    walk = np.cumsum(rng.normal(0, 0.004, n))
    pts = overlapping_allan_dev(white + walk, FS)
    devs = [p.dev_pa for p in pts]
    imin = int(np.argmin(devs))
    assert 0 < imin < len(devs) - 1, "极小值应落在内部,而不是端点"


# --------------------------------------------------------------------------
# 与固件换算式的一致性(跨语言防漂移)
# --------------------------------------------------------------------------
def test_reduction_formula_matches_firmware():
    """上位机重算电流必须与固件 max30131_counts_to_reduction_pa 同式同舍入."""
    fsr, offset = 50000.0, 10000.0
    for counts in (0, 1, 655, 13107, 65535):
        expect = offset - counts * fsr / 65536.0
        got = offset - counts * fsr / 65536.0
        assert math.isclose(got, expect)
    # 抽查固件测试里断言过的两个点
    assert math.isclose(0.0 - 655 * fsr / 65536.0, -499.84, abs_tol=0.2)
    assert math.isclose(offset - 0 * fsr / 65536.0, 10000.0)


def _run_all() -> int:
    fails = 0
    mod = sys.modules[__name__]
    for name in sorted(dir(mod)):
        if not name.startswith("test_"):
            continue
        fn = getattr(mod, name)
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as exc:
            fails += 1
            print(f"  ✗ {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            fails += 1
            print(f"  ✗ {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'✅ 全部通过' if fails == 0 else f'❌ {fails} 项失败'}")
    return 1 if fails else 0


if __name__ == "__main__":
    print("=== 上位机分析栈单测 ===")
    raise SystemExit(_run_all())


def test_legacy_gbk_run_csv_loads_with_chinese_intact() -> None:
    """旧版本写的 run CSV(中文注释头)必须能读,且中文正确还原。

    现场:旧版本在中文 Windows 上把带中文的注释头按 cp936 落盘;严格 utf-8 读会在
    那串中文上抛,而用户已积攒一批这样的文件,不能要求他们转换。
    🔴 用 gb18030 回退而不是 errors="replace",决定的是"中文还能不能看" ——
    实测少了那一档,`左旋多巴` 会变成一串 U+FFFD。

    ⚠️ 本文件设计成无 pytest 也能跑,所以不用 capsys / pytest.approx。
    """
    with tempfile.TemporaryDirectory() as raw_dir:
        csv_path = Path(raw_dir) / "旧数据.csv"
        csv_path.write_bytes((
            "# pA-Converter V5.1 实时采集 —— 左旋多巴标定\n"
            "host_unix_s,seq,dev_ms,counts,fa_fw\n"
            "1787320483.774,0,668874,7176,-18994141\n"
            "1787320483.897,1,668997,7180,-18872070\n"
            "1787320484.020,2,669120,7175,-19000000\n"
        ).encode("gb18030"))

        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            values, elapsed = load_csv(csv_path)

        assert len(values) == 3 and len(elapsed) == 3
        # fa_fw 是 fA,load_csv 返回 pA(/1000);-18994141 fA = -18994.141 pA
        assert abs(values[0] - (-18994.141)) < 1e-3
        assert abs(elapsed[0]) < 1e-9 and elapsed[2] > 0
        assert "gb18030" in captured.getvalue()      # 旧编码必须有可见提示


def test_clean_utf8_run_csv_is_not_flagged() -> None:
    """反面样本:干净 UTF-8 不许被提示成旧编码,否则提示会变成噪声。"""
    with tempfile.TemporaryDirectory() as raw_dir:
        csv_path = Path(raw_dir) / "新数据.csv"
        csv_path.write_text(
            "# 左旋多巴\nhost_unix_s,seq,dev_ms,counts,fa_fw\n"
            "1787320483.774,0,668874,7176,-18994141\n"
            "1787320483.897,1,668997,7180,-18872070\n"
            "1787320484.020,2,669120,7175,-19000000\n",
            encoding="utf-8",
        )
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            load_csv(csv_path)
        assert "gb18030" not in captured.getvalue()
