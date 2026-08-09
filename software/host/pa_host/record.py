"""记录格式 — 固件 RTT 行协议 ↔ CSV 落盘的单一真源.

用途    : 定义固件通过 SEGGER RTT 打出的行格式、解析器、以及落盘 CSV 的列。
          固件侧的打印格式必须与本文件的 LINE_RE 一致(改一处必须改两处,
          `make test` 里有一致性测试兜着)。
用法    : from pa_host.record import parse_line, Sample, CSV_COLUMNS
前置条件: Python ≥3.10,无第三方依赖(analyze.py 才需要 numpy/scipy)。
快照日期: 2026-07-27

为什么用「行文本 + 正则」而不是二进制帧:
  - 0.27 SPS,一分钟一批,吞吐量对格式效率毫无要求;
  - RTT 文本可以人眼直接看,bring-up 现场排错比二进制帧快一个数量级;
  - 帧同步/CRC 在 SWD 这种可靠链路上是多余复杂度。
  以后换 BLE 传输时,把 collect.py 换掉即可,本文件的 Sample/CSV 不变。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields

# --------------------------------------------------------------------------
# 行协议
# --------------------------------------------------------------------------
# 样本行(固件每个 FIFO 样本打一行):
#   S seq=123 ms=456789 counts=13107 fa=2500000 tag=0 auto=1 ovf=0
#
# 事件行(状态/错误,不进 CSV,只进日志):
#   E boot rev=0x01 part=0x3A
#   E cal fsr50_pa=51000 ioffset_pa=10200
#   E warn invalid_cfg
#
# 🔴 电流单位是 **fA(整数)**,不是 pA:50nA 档的 LSB = 763 fA,
#    若协议用整数 pA(1000 fA 一档)会比器件本身还粗、把亚 pA 噪声量化掉。
#    int32 的 fA 可表达 ±2.1µA,远超 2000nA 最大档,不会溢出。
SAMPLE_PREFIX = "S "
EVENT_PREFIX = "E "

LINE_RE = re.compile(
    r"^S\s+"
    r"seq=(?P<seq>\d+)\s+"
    r"ms=(?P<ms>\d+)\s+"
    r"counts=(?P<counts>\d+)\s+"
    r"fa=(?P<fa>-?\d+)\s+"
    r"tag=(?P<tag>\d+)\s+"
    r"auto=(?P<auto>[01])\s+"
    r"ovf=(?P<ovf>\d+)"
    # 🔴 sat 是 2026-08-01 追加字段,**故意做成可选**:
    #    这样 2026-07-31 那批(还没有 sat)的 RTT 日志/CSV 仍然能解析。
    #    缺失时按 0(无饱和)处理 —— 对旧数据是正确的默认(当时用 SEL4,
    #    但那时电极是开路的,没有真信号,不会饱和)。
    r"(?:\s+sat=(?P<sat>\d+))?"
    # CV 扫描追加逐点电位与圈数；IT 与历史日志没有这些字段。
    r"(?:\s+mv=(?P<mv>-?\d+)\s+cycle=(?P<cycle>\d+)\s+dir=(?P<direction>[+-]1))?\s*$"
)


@dataclass(frozen=True, slots=True)
class Sample:
    """一个 FIFO 样本.

    seq    : 固件侧单调递增序号(用于检出丢批)
    ms     : 固件本地毫秒时戳。🔴 时基来自 LFRC,±500ppm(≈±43s/天),
             **不是时间权威** —— 绝对时间靠上位机 host_unix_s 对齐。
    counts : ADC 原始 counts(保留原始值,便于事后换不同校准系数重算)
    fa     : 固件按当前校准系数算出的还原电流(**fA**);上位机会用 counts 复算校验
    ovf    : 读该批前的 FIFO OVF_COUNTER,≠0 表示这批之前丢过样
    sat    : 🔴 饱和标志位 —— bit0(1)=counts 逼近 0:还原电流吃光 offset,
             **WE 已失恒电位控制**;bit1(2)=counts 逼近满量程:氧化方向超出 FSR−offset。
             ≠0 的样本**不是测量结果**(恒电位环开环),算 σ / 标定曲线前必须剔除。
             旧数据(2026-07-31 之前,协议无此字段)默认 0。
    """

    seq: int
    ms: int
    counts: int
    fa: int
    tag: int
    auto: bool
    ovf: int
    sat: int = 0
    potential_mv: int | None = None
    cycle: int | None = None
    direction: int | None = None


CSV_COLUMNS = [
    "host_unix_s",  # 上位机收到该行的墙钟时间(时间权威)
    "seq",
    "dev_ms",  # 固件 LFRC 时戳(相对量,可信;绝对量不可信)
    "counts",
    "fa_fw",  # 固件算的电流(fA)
    "tag",
    "auto",
    "ovf",
    "sat",  # 饱和标志位(bit0=LOW/还原吃光 offset, bit1=HIGH/氧化超量程)
    "potential_mv",  # CV 实际阶梯电位；IT 为空
    "cycle",  # CV 圈数，从 1 开始；IT 为空
    "direction",  # CV 扫描方向：+1 正扫，-1 反扫；IT 为空
]

assert len(CSV_COLUMNS) == len(fields(Sample)) + 1  # host_unix_s 是多出来那列


def parse_line(line: str) -> Sample | None:
    """解析一行 RTT 输出;不是样本行则返回 None(事件行/噪声行由调用方处理)."""
    m = LINE_RE.match(line.strip())
    if m is None:
        return None
    return Sample(
        seq=int(m["seq"]),
        ms=int(m["ms"]),
        counts=int(m["counts"]),
        fa=int(m["fa"]),
        tag=int(m["tag"]),
        auto=m["auto"] == "1",
        ovf=int(m["ovf"]),
        sat=int(m["sat"]) if m["sat"] is not None else 0,
        potential_mv=int(m["mv"]) if m["mv"] is not None else None,
        cycle=int(m["cycle"]) if m["cycle"] is not None else None,
        direction=int(m["direction"]) if m["direction"] is not None else None,
    )


def sample_to_row(s: Sample, host_unix_s: float) -> list[str]:
    """转成 CSV 行(字段顺序必须与 CSV_COLUMNS 一致)."""
    return [
        f"{host_unix_s:.3f}",
        str(s.seq),
        str(s.ms),
        str(s.counts),
        str(s.fa),
        str(s.tag),
        "1" if s.auto else "0",
        str(s.ovf),
        str(s.sat),
        "" if s.potential_mv is None else str(s.potential_mv),
        "" if s.cycle is None else str(s.cycle),
        "" if s.direction is None else str(s.direction),
    ]


def format_sample_line(s: Sample) -> str:
    """反向生成行文本 —— 供测试与合成数据用,保证解析器与格式互为逆运算."""
    cv_fields = (
        "" if s.potential_mv is None else
        f" mv={s.potential_mv} cycle={s.cycle} dir={s.direction:+d}"
    )
    return (
        f"S seq={s.seq} ms={s.ms} counts={s.counts} fa={s.fa} "
        f"tag={s.tag} auto={1 if s.auto else 0} ovf={s.ovf} sat={s.sat}{cv_fields}"
    )


# --------------------------------------------------------------------------
# 数据完整性检查(在落盘阶段就能发现的问题,别留到分析阶段)
# --------------------------------------------------------------------------
@dataclass
class IntegrityReport:
    total: int = 0
    seq_gaps: int = 0  # 序号不连续的次数
    missing_samples: int = 0  # 按序号推算丢了多少个样本
    ovf_events: int = 0  # OVF_COUNTER≠0 的批次数
    bad_tag: int = 0  # tag≠0(非 Sensor1 DC)的样本数
    manual_mode: int = 0  # auto=0 的样本数(自主模式下不应出现)
    saturated: int = 0  # 🔴 sat≠0 的样本数 —— 这些不是测量结果,必须剔除后再算指标
    sat_low: int = 0  # 其中"还原吃光 offset"(WE 失恒电位控制)的数量
    sat_high: int = 0  # 其中"氧化超量程"的数量

    def summary(self) -> str:
        return (
            f"样本 {self.total} 个;序号断裂 {self.seq_gaps} 处"
            f"(推算丢样 {self.missing_samples});FIFO 溢出批 {self.ovf_events};"
            f"异常 tag {self.bad_tag};手动模式样本 {self.manual_mode};"
            f"🔴饱和样本 {self.saturated}(LOW {self.sat_low} / HIGH {self.sat_high})"
        )


def check_integrity(samples: list[Sample]) -> IntegrityReport:
    """扫一遍样本流,报告丢样/溢出/tag 异常 —— 分析前必须先看这个."""
    rep = IntegrityReport(total=len(samples))
    prev_seq: int | None = None
    for s in samples:
        if prev_seq is not None and s.seq != prev_seq + 1:
            rep.seq_gaps += 1
            if s.seq > prev_seq + 1:
                rep.missing_samples += s.seq - prev_seq - 1
        prev_seq = s.seq
        if s.ovf != 0:
            rep.ovf_events += 1
        if s.tag != 0:
            rep.bad_tag += 1
        if not s.auto:
            rep.manual_mode += 1
        if s.sat:
            rep.saturated += 1
            if s.sat & 0x01:
                rep.sat_low += 1
            if s.sat & 0x02:
                rep.sat_high += 1
    return rep
