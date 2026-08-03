"""合成数据生成器 — 回板前把上位机整条链路跑通,也当分析栈的回归夹具.

用途    : 造出「和真板输出格式完全一致」的 CSV / RTT 行文本,让 collect→analyze
          整条链在没有硬件时就能端到端验证;同时可注入已知 σ、已知漂移率、
          丢样、FIFO 溢出、工频干扰,用来验证分析栈能不能把它们识别出来。
用法    : python -m pa_host.synth out.csv --hours 3 --sigma 0.4 --drift 1.5
          python -m pa_host.synth - --lines 20        # 打 RTT 行到 stdout
前置条件: numpy。
快照日期: 2026-07-27

🔴 纪律:合成数据只用于验证软件,**任何对外指标都不得引用合成数据**。
   生成的 CSV 第一行会写一条 `# SYNTHETIC` 注释以防混入真实记录。
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

from .record import CSV_COLUMNS, Sample, format_sample_line, sample_to_row

SYNTH_MARKER = "# SYNTHETIC — 合成数据,禁止用于对外指标"

# 器件真值(与固件工作点一致)
FSR_PA = 50000.0
OFFSET_PA = 10000.0
PERIOD_S = 3.757  # SENS_PERIOD=0x5


def _counts_from_reduction(pa: float) -> int:
    counts = round((OFFSET_PA - pa) * 65536.0 / FSR_PA)
    return int(min(max(counts, 0), 65535))


def generate(
    n: int,
    sigma_pa: float = 0.4,
    drift_pa_per_h: float = 0.0,
    baseline_pa: float = 2500.0,
    mains_hz: float | None = None,
    mains_pa: float = 0.0,
    drop_at: int | None = None,
    drop_count: int = 0,
    lfrc_ppm: float = 500.0,
    seed: int = 42,
) -> tuple[list[Sample], list[float]]:
    """造 n 个样本;返回 (样本表, 上位机墙钟时间戳).

    lfrc_ppm 模拟 LFRC 时基误差:固件 dev_ms 会相对墙钟系统性走偏,
    用来验证「dev_ms 不是时间权威、必须用 host_unix_s」这条设计前提。
    """
    rng = np.random.default_rng(seed)
    t = np.arange(n) * PERIOD_S
    y = baseline_pa + rng.normal(0.0, sigma_pa, n) + drift_pa_per_h * (t / 3600.0)
    if mains_hz is not None and mains_pa > 0:
        y = y + mains_pa * np.sin(2 * np.pi * mains_hz * t)

    dev_scale = 1.0 + lfrc_ppm * 1e-6  # 固件时钟偏快
    host_t0 = 1785000000.0  # 固定基准,保证可复现(不用 time.time())

    samples: list[Sample] = []
    host_times: list[float] = []
    seq = 1
    for i in range(n):
        if drop_at is not None and drop_at <= i < drop_at + drop_count:
            seq += 1  # 序号照涨,样本不落 → 制造可检出的丢样
            continue
        ovf = drop_count if (drop_at is not None and i == drop_at + drop_count) else 0
        pa = float(y[i])
        samples.append(
            Sample(
                seq=seq,
                ms=int(round(t[i] * 1000.0 * dev_scale)),
                counts=_counts_from_reduction(pa),
                fa=int(round(pa * 1000.0)),
                tag=0,
                auto=True,
                ovf=ovf,
            )
        )
        host_times.append(host_t0 + float(t[i]))
        seq += 1
    return samples, host_times


def write_csv(path: Path, samples: list[Sample], host_times: list[float]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        fh.write(SYNTH_MARKER + "\n")
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for s, ht in zip(samples, host_times, strict=True):
            w.writerow(sample_to_row(s, ht))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成合成 AFE 记录(验证软件用)")
    ap.add_argument("out", type=str, help="输出 CSV 路径;写 '-' 则打 RTT 行到 stdout")
    ap.add_argument("--hours", type=float, default=3.0)
    ap.add_argument("--lines", type=int, default=None, help="直接给样本数(覆盖 --hours)")
    ap.add_argument("--sigma", type=float, default=0.4, help="噪声 σ(pA)")
    ap.add_argument("--drift", type=float, default=0.0, help="基线漂移(pA/h)")
    ap.add_argument("--baseline", type=float, default=2500.0, help="还原电流基线(pA)")
    ap.add_argument("--mains-hz", type=float, default=None, help="注入工频(Hz)")
    ap.add_argument("--mains-pa", type=float, default=0.0, help="工频幅度(pA)")
    ap.add_argument("--drop-at", type=int, default=None, help="从第几个样本开始丢")
    ap.add_argument("--drop-count", type=int, default=0, help="丢几个")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    n = args.lines if args.lines is not None else int(args.hours * 3600 / PERIOD_S)
    samples, host_times = generate(
        n=n,
        sigma_pa=args.sigma,
        drift_pa_per_h=args.drift,
        baseline_pa=args.baseline,
        mains_hz=args.mains_hz,
        mains_pa=args.mains_pa,
        drop_at=args.drop_at,
        drop_count=args.drop_count,
        seed=args.seed,
    )

    if args.out == "-":
        for s in samples:
            print(format_sample_line(s))
        return 0

    out = Path(args.out)
    write_csv(out, samples, host_times)
    print(f"写入 {out}:{len(samples)} 样本,{n * PERIOD_S / 3600:.2f} h")
    print(f"注入真值:σ={args.sigma} pA,漂移={args.drift} pA/h,基线={args.baseline} pA")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
