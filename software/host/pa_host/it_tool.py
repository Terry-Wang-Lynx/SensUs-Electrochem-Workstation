"""Command-line tool for the 10 Hz electrochemical i-t workflow.

Examples
--------
Summarize one 120 s run using its final 20 s::

    python -m pa_host.it_tool summarize-run run.csv --summary run.summary.json

Fit a calibration from a CSV containing concentration_um and current_nA::

    python -m pa_host.it_tool calibrate points.csv --model ldopa.json \
        --plot calibration.png

Predict an unknown from the final-20-second current of a run::

    python -m pa_host.it_tool predict --model ldopa.json --run unknown.csv

The ``measure`` subcommand is intentionally a thin wrapper around collect.py:
it can connect to an existing RTT socket or start the project's recommended
J-Link RTT bridge.  Raw acquisition stays independent from fitting, so a
plotting or model error cannot discard hardware data.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
from pathlib import Path

from .it import (
    CalibrationModel,
    fit_calibration,
    load_calibration_points,
    load_model,
    save_model,
    save_summary,
    resample_run_10hz,
    summarize_run,
)
from .runtime import hidden_subprocess_kwargs, module_command


def _plot_run(path: str | Path, output: str | Path, window_s: float) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from .it import load_run_csv

    t, current, valid = load_run_csv(path)
    try:
        summary = summarize_run(path, window_s=window_s)
    except ValueError as exc:
        summary = None
        summary_error = str(exc)
    else:
        summary_error = ""
    fig, ax = plt.subplots(figsize=(9, 5.2), dpi=180)
    ax.plot(t[valid], current[valid], color="#176B87", lw=1.0, label="Valid samples")
    if (~valid).any():
        ax.scatter(t[~valid], current[~valid], color="#C33C54", marker="x",
                   s=22, label="Invalid/saturated")
    duration_s = float(t[-1])
    ax.axvspan(max(0.0, duration_s - window_s), duration_s,
               color="#D97706", alpha=0.10, label=f"Fit window ({window_s:g} s)")
    if summary is not None and summary.steady_current_nA is not None:
        ax.axhline(summary.steady_current_nA, color="#D97706", ls="--", lw=1.2,
                   label=f"Final-window mean: {summary.steady_current_nA:.4f} nA")
    else:
        reason = summary_error or "; ".join(summary.warnings if summary is not None else ())
        ax.text(0.02, 0.96, f"No valid final-window fit: {reason}",
                transform=ax.transAxes, va="top", color="#C33C54", fontsize=8)
    ax.set(xlabel="Time after potential step (s)", ylabel="Signed current (nA)",
           title="10 Hz i-t measurement")
    ax.grid(True, alpha=0.22)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(output)


def _plot_calibration(points, model: CalibrationModel, output: str | Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    x = np.asarray([p.concentration_um for p in points], dtype=float)
    y = np.asarray([p.current_nA for p in points], dtype=float)
    xx = np.linspace(float(x.min()), float(x.max()), 300)
    fig, ax = plt.subplots(figsize=(8, 5.2), dpi=180)
    ax.scatter(x, y, color="#176B87", s=44, label="Calibration points")
    ax.plot(xx, model.current_from_concentration(xx), color="#D97706", ls="--",
            label=f"Degree {model.degree} fit; R²={model.r2:.4f}")
    ax.set(xlabel="Concentration (µmol/L)", ylabel="Final-20-second current (nA)",
           title="Electrochemical calibration")
    ax.grid(True, alpha=0.22)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output)


def _cmd_measure(args: argparse.Namespace) -> int:
    # Keep this wrapper transparent: collect.py owns the line protocol and raw CSV.
    cmd = module_command(
        "pa_host.collect", "--out", args.out, "--duration", args.duration,
        "--idle-timeout", args.idle_timeout, "--progress-every", "100"
    )
    cmd += ["--cv"] if args.cv else ["--it-10hz"]
    if getattr(args, "serial", None):
        cmd += ["--serial", args.serial]
    elif args.start_jlink:
        cmd += ["--start-jlink", "--elf", str(args.elf)]
        if args.probe_serial:
            cmd += ["--probe-serial", args.probe_serial]
        if args.reset_before_read:
            cmd += ["--reset-before-read"]
        else:
            # collect.py keeps a reset-on-connect default for its standalone
            # CLI.  The workstation's ARMED gate deliberately preserves the
            # running firmware and separates stale RTT bytes with a unique
            # request id, so the wrapper must forward False explicitly.
            cmd += ["--no-reset-before-read"]
    else:
        cmd += ["--socket", args.socket or "127.0.0.1:19021"]
    if getattr(args, "cmd_file", None):
        cmd += ["--cmd-file", str(args.cmd_file)]
    if getattr(args, "cell_v", None):
        cmd += ["--cell-v", str(args.cell_v)]
    if getattr(args, "audit", None):
        cmd += ["--audit", str(args.audit)]
    if args.raw_log:
        cmd += ["--raw-log", str(args.raw_log)]
    if args.trigger:
        cmd += ["--trigger", args.trigger]

    child: subprocess.Popen[bytes] | None = None
    pending_sigterm = False

    def _forward_sigterm(signum, _frame) -> None:
        nonlocal pending_sigterm
        pending_sigterm = True
        if child is None:
            return
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    previous_sigterm = signal.signal(signal.SIGTERM, _forward_sigterm)
    try:
        child = subprocess.Popen(cmd, **hidden_subprocess_kwargs())
        # SIGTERM may arrive after Popen starts the process but before it returns
        # and assigns ``child``. Forward that pending request once assignment is safe.
        if pending_sigterm:
            try:
                child.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
        exit_code = child.wait()
        # The collector exits 0 after a graceful signal so its CSV is complete.
        # Preserve a distinct wrapper code so callers do not treat a partial,
        # manually stopped acquisition as a naturally completed run.
        return 3 if pending_sigterm else exit_code
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="10Hz i-t 标定与浓度预测工具")
    sub = ap.add_subparsers(dest="command", required=True)

    measure = sub.add_parser("measure", help="从 RTT/USB CDC 收取原始数据")
    source = measure.add_mutually_exclusive_group()
    source.add_argument("--socket", default=None,
                         help="连接已启动的 RTT socket,如 127.0.0.1:19021")
    source.add_argument("--start-jlink", action="store_true",
                        help="用系统 JLinkExe 或随包 OpenOCD 自动启动 RTT")
    source.add_argument("--serial", default=None,
                        help="V5.1 DATA CDC,如 /dev/cu.usbmodemXXXX")
    measure.add_argument("--elf", type=Path,
                         default=Path("/tmp/pabuild/firmware/zephyr/zephyr.elf"))
    measure.add_argument("--probe-serial")
    measure.add_argument("--reset-before-read", action="store_true",
                         help="用 J-Link 启动 RTT 后复位目标，开始一轮新测量")
    measure.add_argument("--trigger", default="START",
                         help="连接 RTT 后触发一轮测量；默认 START")
    measure.add_argument("--out", type=Path, required=True)
    measure.add_argument("--raw-log", type=Path)
    measure.add_argument("--audit", type=Path,
                         help="配置变更审计 jsonl(转给 collect.py)")
    measure.add_argument("--cell-v", type=Path,
                         help="电极电压连采 CSV(默认 <out stem>-cellv.csv)")
    measure.add_argument("--cmd-file", type=Path,
                         help="命令文件:采集器会把新增行经自己的 RTT socket 转发给固件")
    measure.add_argument("--duration", type=float, default=205.0)
    measure.add_argument("--idle-timeout", type=float, default=25.0)
    measure.add_argument("--cv", action="store_true",
                         help="接收固件 CV 标记和逐点电位元数据")
    measure.set_defaults(func=_cmd_measure, start_jlink=False)

    summary = sub.add_parser("summarize-run", help="取最后 20s 有效数据计算稳态电流")
    summary.add_argument("run", type=Path)
    summary.add_argument("--window", type=float, default=20.0)
    summary.add_argument("--summary", type=Path)
    summary.add_argument("--plot", type=Path)
    summary.set_defaults(func=_cmd_summary)

    resample = sub.add_parser("resample", help="把硬件原生时间戳重采样为固定 10Hz/1200 点")
    resample.add_argument("run", type=Path)
    resample.add_argument("--out", type=Path, required=True)
    resample.add_argument("--duration", type=float, default=180.0)
    resample.add_argument("--rate", type=float, default=10.0)
    resample.set_defaults(func=_cmd_resample)

    calibrate = sub.add_parser("calibrate", help="用浓度+电流 CSV 拟合标定曲线")
    calibrate.add_argument("data", type=Path)
    calibrate.add_argument("--model", type=Path, required=True)
    calibrate.add_argument("--degree", type=int, default=1)
    calibrate.add_argument("--plot", type=Path)
    calibrate.set_defaults(func=_cmd_calibrate)

    predict = sub.add_parser("predict", help="用标定曲线预测浓度")
    predict.add_argument("--model", type=Path, required=True)
    source = predict.add_mutually_exclusive_group(required=True)
    source.add_argument("--current-na", type=float)
    source.add_argument("--run", type=Path)
    predict.add_argument("--window", type=float, default=20.0)
    predict.set_defaults(func=_cmd_predict)

    args = ap.parse_args(argv)
    return int(args.func(args))


def _cmd_summary(args: argparse.Namespace) -> int:
    result = summarize_run(args.run, window_s=args.window)
    print(json.dumps(result.__dict__, indent=2, ensure_ascii=False))
    if args.summary:
        save_summary(result, args.summary)
    if args.plot:
        _plot_run(args.run, args.plot, args.window)
    return 0


def _cmd_resample(args: argparse.Namespace) -> int:
    output = resample_run_10hz(args.run, args.out, args.duration, args.rate)
    print(json.dumps({"source": str(args.run), "output": str(output),
                      "duration_s": args.duration, "rate_hz": args.rate,
                      "points": int(round(args.duration * args.rate))},
                     ensure_ascii=False, indent=2))
    return 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    points = load_calibration_points(args.data)
    model = fit_calibration(points, degree=args.degree)
    save_model(model, args.model)
    print(json.dumps(model.to_json(), indent=2, ensure_ascii=False))
    if args.plot:
        _plot_calibration(points, model, args.plot)
    return 0


def _cmd_predict(args: argparse.Namespace) -> int:
    model = load_model(args.model)
    if args.current_na is not None:
        current = args.current_na
        source = "current"
    else:
        result = summarize_run(args.run, window_s=args.window)
        current = result.steady_current_nA
        source = str(args.run)
        if current is None:
            print(json.dumps({"source": source,
                              "error": "final fitting window has fewer than three valid samples",
                              "warnings": list(result.warnings)},
                             ensure_ascii=False, indent=2), file=sys.stderr)
            return 2
    concentration = model.predict_concentration(current)
    print(json.dumps({"source": source, "current_nA": current,
                      "predicted_concentration_um": concentration,
                      "model_r2": model.r2}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
