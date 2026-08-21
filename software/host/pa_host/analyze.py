"""B 段 — 离线分析:σ / 3σ / PSD / Allan 偏差 / 基线漂移 / ER·NFR.

用途    : 把落盘的 CSV 变成 08-测试与表征 要求的那几个验收指标,口径与
          `docs/ver3.1/08-测试与表征/AFE-测试指标与验收口径.md` 严格对齐
          (该文的指标体系与本板通用:同一套 σ/漂移/分辨率口径)。
用法    : python -m pa_host.analyze <run.csv> [--fsr-pa 50000] [--plot out.png]
          也可作为库:from pa_host.analyze import analyze_current
前置条件: numpy;PSD 用 scipy.signal.welch(缺 scipy 时自动退化为周期图);
          --plot 需要 matplotlib。
快照日期: 2026-07-27

🔴 三个口径纪律(照抄 08 文档,别在报告里混用):
  1. LSB(量化步长)不是分辨率。50nA 档 LSB=0.763pA,而能不能分辨由噪声+漂移定。
  2. 检测下限用「暗噪声 3σ」定义,不是某次走运读数。
  3. 分辨率不是一个数,是随平均时间变的曲线 —— 短期噪声限、长期漂移接管,
     所以必须给 Allan 偏差曲线而不是单一 σ。
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .textio import is_legacy_encoding, read_csv_lines

try:
    from scipy import signal as _scipy_signal
except ImportError:  # pragma: no cover - 环境退化路径
    _scipy_signal = None


# --------------------------------------------------------------------------
# 结果结构
# --------------------------------------------------------------------------
@dataclass
class NoiseStats:
    n: int
    mean_pa: float
    sigma_pa: float  # 去趋势后的 rms 抖动
    three_sigma_pa: float
    pp_pa: float  # 峰峰
    sigma_raw_pa: float  # 未去趋势(含漂移)的 σ,用于对比看漂移吃了多少


@dataclass
class DriftStats:
    slope_pa_per_h: float
    intercept_pa: float
    duration_h: float
    r2: float


@dataclass
class ResolutionStats:
    fsr_pa: float
    lsb_pa: float
    er_bits: float  # log2(FSR/σ)
    nfr_bits: float  # log2(FSR/峰峰)
    min_visible_step_pa: float  # ≈6.6σ,单次采样肉眼可分门槛
    sigma_for_1pa_change: float  # 1pA 变化相当于几个 σ√2


@dataclass
class AllanPoint:
    tau_s: float
    dev_pa: float
    n_clusters: int


@dataclass
class AnalysisResult:
    fs_hz: float
    noise: NoiseStats
    drift: DriftStats
    resolution: ResolutionStats
    allan: list[AllanPoint] = field(default_factory=list)
    psd_f: np.ndarray | None = None
    psd_pa2_per_hz: np.ndarray | None = None
    warnings: list[str] = field(default_factory=list)

    def report(self) -> str:
        lines = [
            "==== AFE 电流通道分析 ====",
            f"采样率        : {self.fs_hz:.4f} Hz "
            f"(样本间隔 {1 / self.fs_hz:.3f} s)" if self.fs_hz > 0 else "采样率        : n/a",
            f"样本数        : {self.noise.n}  时长 {self.drift.duration_h:.3f} h",
            "",
            "--- 噪声(去趋势后,短期)---",
            f"均值          : {self.noise.mean_pa:.3f} pA",
            f"σ             : {self.noise.sigma_pa:.4f} pA",
            f"3σ(检测下限口径): {self.noise.three_sigma_pa:.4f} pA",
            f"峰峰          : {self.noise.pp_pa:.4f} pA",
            f"σ(未去趋势)  : {self.noise.sigma_raw_pa:.4f} pA "
            f"(与上面 σ 的差 = 漂移贡献)",
            "",
            "--- 基线漂移(长期)---",
            f"漂移率        : {self.drift.slope_pa_per_h:+.4f} pA/h  (R²={self.drift.r2:.4f})",
            "",
            "--- 分辨率(over 明确 FSR)---",
            f"FSR           : {self.resolution.fsr_pa / 1000:.1f} nA",
            f"LSB(量化步长,非能力): {self.resolution.lsb_pa:.4f} pA",
            f"ER(有效分辨率): {self.resolution.er_bits:.2f} 位",
            f"NFR(无噪声位) : {self.resolution.nfr_bits:.2f} 位",
            f"单次可见最小台阶(≈6.6σ): {self.resolution.min_visible_step_pa:.3f} pA",
            f"1pA 变化置信  : {self.resolution.sigma_for_1pa_change:.1f}σ(单次差值)",
        ]
        if self.allan:
            best = min(self.allan, key=lambda p: p.dev_pa)
            lines += [
                "",
                "--- Allan 偏差(分辨率 vs 平均时间)---",
                f"最优积分时间  : τ={best.tau_s:.1f} s → {best.dev_pa:.4f} pA",
                f"曲线点数      : {len(self.allan)}"
                f"(τ {self.allan[0].tau_s:.1f}s … {self.allan[-1].tau_s:.1f}s)",
            ]
        if self.warnings:
            lines += ["", "--- 警告 ---"] + [f"⚠️  {w}" for w in self.warnings]
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 核心算法
# --------------------------------------------------------------------------
def _linear_detrend(t: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, float, float, float]:
    """一次多项式去趋势;返回 (残差, 斜率, 截距, R²)."""
    if len(y) < 3:
        return y - np.mean(y), 0.0, float(np.mean(y)), 0.0
    slope, intercept = np.polyfit(t, y, 1)
    fit = slope * t + intercept
    resid = y - fit
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    ss_res = float(np.sum(resid**2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return resid, float(slope), float(intercept), r2


def overlapping_allan_dev(y: np.ndarray, fs_hz: float,
                          max_points: int = 24) -> list[AllanPoint]:
    """重叠式 Allan 偏差(对相位/频率型数据的标准做法之一).

    这里 y 是「测量量本身」(电流),对应 ADEV 的 *frequency-type* 数据:
        σ_y²(τ) = 1/(2(N-2m+1)) · Σ (ȳ_{i+m} − ȳ_i)²
    其中 ȳ 是长度 m 的相邻平均。τ = m/fs。
    """
    n = len(y)
    if n < 8 or fs_hz <= 0:
        return []
    m_max = n // 4  # 至少留 4 段,否则统计意义太差
    if m_max < 1:
        return []
    # 对数均匀取 m
    ms = np.unique(
        np.round(np.geomspace(1, m_max, num=min(max_points, m_max))).astype(int)
    )
    out: list[AllanPoint] = []
    for m in ms:
        k = n // m
        if k < 3:
            continue
        # 相邻 m 点平均(重叠版用累积和实现)
        csum = np.concatenate(([0.0], np.cumsum(y, dtype=float)))
        avg = (csum[m:] - csum[:-m]) / m  # 长度 n-m+1
        diffs = avg[m:] - avg[:-m]
        if len(diffs) < 2:
            continue
        var = float(np.sum(diffs**2)) / (2.0 * len(diffs))
        out.append(
            AllanPoint(tau_s=float(m) / fs_hz, dev_pa=math.sqrt(var),
                       n_clusters=len(diffs))
        )
    return out


def estimate_psd(y: np.ndarray, fs_hz: float) -> tuple[np.ndarray, np.ndarray]:
    """功率谱密度(pA²/Hz)。有 scipy 用 Welch,否则退化为单段周期图."""
    y = y - float(np.mean(y))
    if _scipy_signal is not None:
        nperseg = min(len(y), 256)
        if nperseg < 8:
            nperseg = len(y)
        f, pxx = _scipy_signal.welch(y, fs=fs_hz, nperseg=nperseg)
        return f, pxx
    # 退化路径:朴素周期图
    n = len(y)
    win = np.hanning(n)
    yf = np.fft.rfft(y * win)
    f = np.fft.rfftfreq(n, d=1.0 / fs_hz)
    scale = 1.0 / (fs_hz * float(np.sum(win**2)))
    pxx = (np.abs(yf) ** 2) * scale * 2.0
    return f, pxx


def analyze_current(pa: np.ndarray, t_s: np.ndarray, fsr_pa: float,
                    dark: bool = True) -> AnalysisResult:
    """主入口:给电流序列(pA)与时间(s),出全套指标.

    dark=True 表示这是暗噪声/零信号记录,才可以把 3σ 当检测下限口径用。
    """
    pa = np.asarray(pa, dtype=float)
    t_s = np.asarray(t_s, dtype=float)
    if len(pa) != len(t_s):
        raise ValueError("pa 与 t_s 长度不一致")
    if len(pa) < 3:
        raise ValueError("样本太少(<3),无法给出统计量")

    warnings: list[str] = []
    dt = np.diff(t_s)
    if len(dt) == 0 or np.median(dt) <= 0:
        raise ValueError("时间轴非递增")
    fs_hz = 1.0 / float(np.median(dt))
    jitter = float(np.std(dt) / np.median(dt)) if np.median(dt) > 0 else 0.0
    if jitter > 0.05:
        warnings.append(
            f"采样间隔抖动 {jitter * 100:.1f}% > 5%:PSD/Allan 的横轴会失真"
            "(LFRC 时基或丢样导致,先查 record.check_integrity)"
        )

    resid, slope_per_s, intercept, r2 = _linear_detrend(t_s, pa)
    duration_h = float(t_s[-1] - t_s[0]) / 3600.0

    noise = NoiseStats(
        n=len(pa),
        mean_pa=float(np.mean(pa)),
        sigma_pa=float(np.std(resid, ddof=1)),
        three_sigma_pa=3.0 * float(np.std(resid, ddof=1)),
        pp_pa=float(np.max(resid) - np.min(resid)),
        sigma_raw_pa=float(np.std(pa, ddof=1)),
    )
    drift = DriftStats(
        slope_pa_per_h=slope_per_s * 3600.0,
        intercept_pa=intercept,
        duration_h=duration_h,
        r2=r2,
    )

    lsb_pa = fsr_pa / 65536.0
    sigma = noise.sigma_pa if noise.sigma_pa > 0 else float("nan")
    resolution = ResolutionStats(
        fsr_pa=fsr_pa,
        lsb_pa=lsb_pa,
        er_bits=math.log2(fsr_pa / sigma) if sigma > 0 else float("nan"),
        nfr_bits=math.log2(fsr_pa / noise.pp_pa) if noise.pp_pa > 0 else float("nan"),
        min_visible_step_pa=6.6 * sigma,
        # 差值噪声 = σ√2;1pA 相当于几个 σ√2
        sigma_for_1pa_change=1.0 / (sigma * math.sqrt(2.0)) if sigma > 0 else float("nan"),
    )

    if not dark:
        warnings.append("dark=False:3σ 不可当检测下限对外引用(需零信号记录)")
    if duration_h < 2.0:
        warnings.append(
            f"记录仅 {duration_h:.2f} h < 2 h:漂移率与 Allan 长 τ 段不可信"
            "(08 文档要求 ≥2h 长记录,warm-up ~1h 不计入)"
        )
    if noise.sigma_pa < lsb_pa:
        warnings.append(
            f"σ({noise.sigma_pa:.4f} pA)< 1 LSB({lsb_pa:.4f} pA):"
            "读数被量化主导,σ 不是真实噪声"
        )

    psd_f, psd = estimate_psd(resid, fs_hz)
    return AnalysisResult(
        fs_hz=fs_hz,
        noise=noise,
        drift=drift,
        resolution=resolution,
        allan=overlapping_allan_dev(resid, fs_hz),
        psd_f=psd_f,
        psd_pa2_per_hz=psd,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# CSV 读取 + CLI
# --------------------------------------------------------------------------
def load_csv(path: Path, use_host_clock: bool = True,
             fsr_pa: float | None = None,
             offset_pa: float | None = None,
             seq_period_ms: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """读 collect.py 落的 CSV,返回 (电流 pA, 时间 s).

    时间轴三选一(优先级:seq_period_ms > use_host_clock > dev_ms):

    * **seq_period_ms**(★AUTO 模式下最正确★)—— 用 `seq × 周期` 重建**均匀**栅格。
      🔴 为什么需要它:`dev_ms` 是固件**读出**样本的时刻(轮询网格),不是 ADC
      **转换**的时刻。本板 INTB 悬空只能轮询,2000ms 轮询去接 3757ms 产出,
      时间戳被量化到轮询栅格上 → 相对周期约 53% 抖动,PSD/Allan 横轴会失真。
      而 AUTO 模式下 ADC 按 SENS_PERIOD 用自己的时钟定时转换,**真实栅格是均匀的**,
      读出抖动与物理无关。⇒ 用 seq 重建才是对的。
      ⚠️ 前提:该段内 seq 连续(有丢样时 seq 会跳,此时重建仍正确 —— 跳过的格子
      本来就没有样本;但 check_integrity 报的丢样要先解释清楚)。
    * use_host_clock=True —— host_unix_s(绝对时间权威;含 USB/RTT 传输抖动)
    * 否则 —— 固件 dev_ms

    给了 fsr_pa/offset_pa 则用 counts 重算电流(便于事后套用新校准系数),
    否则直接用固件算好的 pa_fw。
    """
    ts: list[float] = []
    vals: list[float] = []
    # ✅ 用户数据:collect.py 落在用户目录里的 run CSV。数据列全是数字,但 '#' 注释行
    #    里带样品名/批次名(中文),旧版本在中文 Windows 上按 cp936 落盘 ⇒ 严格 utf-8
    #    读会 UnicodeDecodeError,一整段实测数据就分析不出来了。
    lines, encoding = read_csv_lines(path)
    if is_legacy_encoding(encoding):
        # 🔴 旧编码不静默。analyze 是"命令行 + 库"两用,没有 GUI 字段可挂提示,
        #    唯一的用户可见通道就是 stderr;静默容错等于让人永远不知道自己手里
        #    的数据是旧编码写的。写 stderr 不污染 stdout(可能在被管道消费)。
        print(
            f"[analyze] ⚠️ {path} 是旧版本以 {encoding} 编码写下的，"
            "已按该编码正确读入；建议重新导出一份",
            file=sys.stderr,
        )
    # 跳过 '#' 注释行(synth.py 会写 SYNTHETIC 标记;真实记录也可能带表头注释),
    # 否则 DictReader 会把注释当成表头。
    rows = (ln for ln in lines if not ln.lstrip().startswith("#"))
    for row in csv.DictReader(rows):
        ts.append(
            float(row["seq"]) * seq_period_ms / 1000.0 if seq_period_ms
            else float(row["host_unix_s"]) if use_host_clock
            else float(row["dev_ms"]) / 1000.0
        )
        if fsr_pa is not None and offset_pa is not None:
            counts = float(row["counts"])
            vals.append(offset_pa - counts * fsr_pa / 65536.0)
        else:
            vals.append(float(row["fa_fw"]) / 1000.0)
    t = np.asarray(ts, dtype=float)
    return np.asarray(vals, dtype=float), t - t[0]


def plot(result: AnalysisResult, pa: np.ndarray, t_s: np.ndarray,
         out_path: Path) -> None:
    """出三联图:时序 / PSD / Allan。需要 matplotlib."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 1, figsize=(9, 10))

    axes[0].plot(t_s / 3600.0, pa, lw=0.8)
    fit = result.drift.slope_pa_per_h * (t_s / 3600.0) + result.drift.intercept_pa
    axes[0].plot(t_s / 3600.0, fit, "r--", lw=1,
                 label=f"drift {result.drift.slope_pa_per_h:+.3f} pA/h")
    axes[0].set_xlabel("time (h)")
    axes[0].set_ylabel("current (pA)")
    axes[0].set_title(
        f"time series  σ={result.noise.sigma_pa:.3f} pA  "
        f"3σ={result.noise.three_sigma_pa:.3f} pA"
    )
    axes[0].legend()
    axes[0].grid(alpha=0.3)

    if result.psd_f is not None and result.psd_pa2_per_hz is not None:
        mask = result.psd_f > 0
        axes[1].loglog(result.psd_f[mask],
                       np.sqrt(result.psd_pa2_per_hz[mask]), lw=0.9)
    axes[1].set_xlabel("frequency (Hz)")
    axes[1].set_ylabel(r"ASD (pA/$\sqrt{Hz}$)")
    axes[1].set_title("noise spectral density")
    axes[1].grid(alpha=0.3, which="both")

    if result.allan:
        taus = [p.tau_s for p in result.allan]
        devs = [p.dev_pa for p in result.allan]
        axes[2].loglog(taus, devs, "o-", ms=3, lw=0.9)
        best = min(result.allan, key=lambda p: p.dev_pa)
        axes[2].axvline(best.tau_s, color="r", ls="--", lw=1,
                        label=f"best τ={best.tau_s:.1f}s → {best.dev_pa:.3f} pA")
        axes[2].legend()
    axes[2].set_xlabel(r"averaging time $\tau$ (s)")
    axes[2].set_ylabel("Allan deviation (pA)")
    axes[2].set_title("resolution vs averaging time")
    axes[2].grid(alpha=0.3, which="both")

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="AFE 电流记录离线分析(σ/3σ/PSD/Allan/漂移/ER·NFR)"
    )
    ap.add_argument("csv", type=Path, help="collect.py 落的 CSV")
    ap.add_argument("--fsr-pa", type=float, default=50000.0,
                    help="满量程电流 pA(默认 50nA 档 = 50000)")
    ap.add_argument("--offset-pa", type=float, default=None,
                    help="给了就用 counts+该 offset 重算电流,而不是用固件的 pa_fw")
    ap.add_argument("--dev-clock", action="store_true",
                    help="用固件 LFRC 时戳而非上位机墙钟(默认后者)")
    ap.add_argument("--seq-period-ms", type=float, default=None,
                    help="★AUTO 模式推荐★ 用 seq×该周期重建均匀时间轴"
                         "(本工作点 SENS_PERIOD=0x5 → 3757)。"
                         "dev_ms 是读出时刻不是转换时刻,轮询会造成大抖动")
    ap.add_argument("--signal", action="store_true",
                    help="这是带信号的记录,不是暗噪声(3σ 不作检测下限)")
    ap.add_argument("--plot", type=Path, default=None, help="出图到该 png")
    args = ap.parse_args(argv)

    if not args.csv.exists():
        print(f"找不到文件:{args.csv}", file=sys.stderr)
        return 2

    recalc_fsr = args.fsr_pa if args.offset_pa is not None else None
    pa, t_s = load_csv(args.csv, use_host_clock=not args.dev_clock,
                     seq_period_ms=args.seq_period_ms,
                       fsr_pa=recalc_fsr, offset_pa=args.offset_pa)
    result = analyze_current(pa, t_s, fsr_pa=args.fsr_pa, dark=not args.signal)
    print(result.report())

    if args.plot is not None:
        plot(result, pa, t_s, args.plot)
        print(f"\n图已写入 {args.plot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
