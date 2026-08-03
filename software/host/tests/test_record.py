#!/usr/bin/env python3
"""record.py 行协议单测 —— 协议是固件与上位机的唯一契约,必须有往返测试。

用途    : 保证 format_sample_line ↔ parse_line 互为逆运算、CSV 列宽对齐、
          饱和标志正确统计,以及**旧格式(无 sat 字段)向后兼容**。
用法    : python3 tests/test_record.py   (纯标准库,不需要 numpy)
快照日期: 2026-08-01
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pa_host.record import (  # noqa: E402
    CSV_COLUMNS,
    Sample,
    check_integrity,
    format_sample_line,
    parse_line,
    sample_to_row,
)
from pa_host.collect import (  # noqa: E402
    parse_it_aborted,
    parse_it_done,
    parse_it_start,
    parse_potential_fault,
)

_n = 0


def chk(cond, msg):
    global _n
    if not cond:
        print(f"❌ {msg}")
        sys.exit(1)
    _n += 1
    print(f"  ✓ {msg}")


def main() -> int:
    print("=== 协议往返(含 sat)===")
    s = Sample(seq=7, ms=1234, counts=24904, fa=-1500, tag=0, auto=True, ovf=0, sat=0)
    chk(parse_line(format_sample_line(s)) == s, "format→parse 互为逆运算")
    s2 = Sample(seq=8, ms=5678, counts=300, fa=18700000, tag=0, auto=True, ovf=0, sat=1)
    chk(parse_line(format_sample_line(s2)) == s2, "sat=1(LOW 饱和)往返")
    s3 = Sample(seq=9, ms=9999, counts=65000, fa=-30000000, tag=0, auto=True, ovf=2,
                sat=2)
    chk(parse_line(format_sample_line(s3)) == s3, "sat=2(HIGH 饱和)+ ovf 往返")

    print("\n=== 🔴 向后兼容:旧格式(无 sat 字段)必须仍能解析 ===")
    # 2026-07-31 那批 RTT 日志就是这个格式;sat 是 08-01 追加的可选字段
    old = "S seq=42 ms=1000 counts=11522 fa=1209000 tag=0 auto=1 ovf=0"
    p = parse_line(old)
    chk(p is not None, "旧格式行能解析")
    chk(p.sat == 0, "缺失 sat 时默认 0")
    chk(p.seq == 42 and p.counts == 11522, "其余字段不受影响")

    print("\n=== 非样本行必须被忽略(RTT 里混着 LOG 与 JLink 横幅)===")
    for junk in ("", "   ", "[00:00:01.000] <inf> main: hello",
                 "SEGGER J-Link V8.80 - Real time terminal output",
                 "S seq=1 ms=2 counts=3"):  # 字段不全
        chk(parse_line(junk) is None, f"忽略:{junk[:38]!r}")

    print("\n=== CSV 列数一致 ===")
    chk(len(sample_to_row(s, 1.0)) == len(CSV_COLUMNS),
        f"行宽 {len(CSV_COLUMNS)} 列对齐")
    chk(CSV_COLUMNS[-1] == "sat", "sat 在最后一列(追加不破坏旧列序)")

    print("\n=== 完整性检查统计饱和 ===")
    r = check_integrity([s, s2, s3])
    chk(r.saturated == 2, f"饱和样本计数 = {r.saturated}")
    chk(r.sat_low == 1 and r.sat_high == 1, "LOW / HIGH 分开统计")
    chk("饱和" in r.summary(), "summary 里出现饱和字样")

    print("\n=== 丢样检出 ===")
    a = Sample(seq=1, ms=0, counts=100, fa=0, tag=0, auto=True, ovf=0)
    b = Sample(seq=5, ms=4000, counts=100, fa=0, tag=0, auto=True, ovf=0)
    r2 = check_integrity([a, b])
    chk(r2.seq_gaps == 1 and r2.missing_samples == 3, "seq 1→5 = 断裂 1 处、丢 3 个")

    print("\n=== IT 完成标记 ===")
    chk(parse_it_done("IT_DONE native=968 expected=968 elapsed_ms=120338")
        == (968, 968, 120338), "完成标记可立即结束采集")
    chk(parse_it_done("S seq=1 ms=2 counts=3") is None,
        "普通数据行不会误触发完成")
    chk(parse_it_start("IT_START run=3 target_mv=200") == (3, 200),
        "无需复位即可识别新一轮测量开始")
    chk(parse_it_aborted("IT_ABORTED reason=restart native=741 elapsed_ms=92001")
        == ("restart", 741, 92001), "识别运行中重新开始标记")
    chk(parse_it_aborted("IT_ABORTED reason=stop native=12 elapsed_ms=1510")
        == ("stop", 12, 1510), "识别硬件停止标记")

    print("\n=== 电位审计故障标记 ===")
    fault = ("POTENTIAL_FAULT sample=80 target_mv=200 expected_daca=1067 "
             "expected_dacb=533 actual_daca=1067 actual_dacb=540")
    chk(parse_potential_fault(fault) == fault, "电位寄存器跳变可被收数端识别")
    chk(parse_potential_fault("POTENTIAL_AUDIT sample=80 target_mv=200") is None,
        "正常电位审计不会误报")

    print(f"\n✅ 全部通过:{_n} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
