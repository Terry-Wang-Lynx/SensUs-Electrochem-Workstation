# -*- coding: utf-8 -*-
"""张大头 (ZDT) 第二代闭环步进电机 · Emm 固件串口协议常量与换算。

协议依据：ZDT_X42S / ZDT_Y42 用户手册 + 开源库 emm_stepper
          (https://github.com/xhfb/emm_stepper)

帧格式：地址(1 字节) + 功能码(1 字节) + 数据(N 字节) + 校验(1 字节)
- 地址：1~255，广播地址 0
- 波特率：默认 115200，8N1
- 校验：默认固定 0x6B；也支持 XOR / CRC8
- 电机位置用「编码器计数」表示，65536 计数 = 1 圈 = 360°
- 位置模式按「脉冲」计，默认 16 细分、1.8° 电机下 3200 脉冲 = 1 圈
- 速度模式按「RPM」计（整数，0~6000）
"""

FIXED_CHECKSUM = 0x6B

# ---- 功能码 ----
CODE_ENABLE = 0xF3                # 使能/失能（锁轴/松轴）
CODE_JOG = 0xF6                   # 速度模式（连续运转）
CODE_POSITION = 0xFD              # 位置模式（脉冲数）
CODE_ESTOP = 0xFE                 # 立即停止
CODE_CLEAR_PROTECTION = 0x0E      # 解除堵转/过热/过流保护
CODE_ZERO = 0x0A                  # 将当前位置角度清零
CODE_HOME = 0x9A                  # 触发回零
CODE_STOP_HOME = 0x9C             # 强制中断并退出回零
CODE_GET_VERSION = 0x1F           # 读固件/硬件版本
CODE_GET_CONFIG = 0x42            # 读取驱动配置参数（动态长度响应）
CODE_GET_REALTIME_SPEED = 0x35    # 读实时转速
CODE_GET_REALTIME_POSITION = 0x36  # 读实时位置
CODE_GET_TEMP = 0x39              # 读驱动温度
CODE_GET_MOTOR_STATUS = 0x3A      # 读电机状态标志

# ---- 电机状态字节位（0x3A 返回） ----
STATUS_ENABLED = 0x01             # 使能（锁轴）
STATUS_REACHED = 0x02             # 位置到达
STATUS_STALL = 0x04               # 堵转标志
STATUS_STALL_PROTECTED = 0x08     # 堵转保护
STATUS_LEFT_LIMIT = 0x10          # 左限位
STATUS_RIGHT_LIMIT = 0x20         # 右限位
STATUS_POWER_OFF = 0x80           # 掉电标志

# ---- 回零模式（0x9A 的参数） ----
HOME_NEAREST = 0        # 单圈就近回零
HOME_DIRECTION = 1      # 单圈方向回零
HOME_COLLISION = 2      # 无限位碰撞回零
HOME_LIMIT = 3          # 限位回零
HOME_ABS_ZERO = 4       # 回到绝对坐标零点
HOME_LAST_POWER_OFF = 5  # 回到上次掉电位置

# CRC8 查表（多项式与官方库一致）
CRC8_TABLE = bytes([
    0x00, 0x5E, 0xBC, 0xE2, 0x61, 0x3F, 0xDD, 0x83,
    0xC2, 0x9C, 0x7E, 0x20, 0xA3, 0xFD, 0x1F, 0x41,
    0x9D, 0xC3, 0x21, 0x7F, 0xFC, 0xA2, 0x40, 0x1E,
    0x5F, 0x01, 0xE3, 0xBD, 0x3E, 0x60, 0x82, 0xDC,
    0x23, 0x7D, 0x9F, 0xC1, 0x42, 0x1C, 0xFE, 0xA0,
    0xE1, 0xBF, 0x5D, 0x03, 0x80, 0xDE, 0x3C, 0x62,
    0xBE, 0xE0, 0x02, 0x5C, 0xDF, 0x81, 0x63, 0x3D,
    0x7C, 0x22, 0xC0, 0x9E, 0x1D, 0x43, 0xA1, 0xFF,
    0x46, 0x18, 0xFA, 0xA4, 0x27, 0x79, 0x9B, 0xC5,
    0x84, 0xDA, 0x38, 0x66, 0xE5, 0xBB, 0x59, 0x07,
    0xDB, 0x85, 0x67, 0x39, 0xBA, 0xE4, 0x06, 0x58,
    0x19, 0x47, 0xA5, 0xFB, 0x78, 0x26, 0xC4, 0x9A,
    0x65, 0x3B, 0xD9, 0x87, 0x04, 0x5A, 0xB8, 0xE6,
    0xA7, 0xF9, 0x1B, 0x45, 0xC6, 0x98, 0x7A, 0x24,
    0xF8, 0xA6, 0x44, 0x1A, 0x99, 0xC7, 0x25, 0x7B,
    0x3A, 0x64, 0x86, 0xD8, 0x5B, 0x05, 0xE7, 0xB9,
    0x8C, 0xD2, 0x30, 0x6E, 0xED, 0xB3, 0x51, 0x0F,
    0x4E, 0x10, 0xF2, 0xAC, 0x2F, 0x71, 0x93, 0xCD,
    0x11, 0x4F, 0xAD, 0xF3, 0x70, 0x2E, 0xCC, 0x92,
    0xD3, 0x8D, 0x6F, 0x31, 0xB2, 0xEC, 0x0E, 0x50,
    0xAF, 0xF1, 0x13, 0x4D, 0xCE, 0x90, 0x72, 0x2C,
    0x6D, 0x33, 0xD1, 0x8F, 0x0C, 0x52, 0xB0, 0xEE,
    0x32, 0x6C, 0x8E, 0xD0, 0x53, 0x0D, 0xEF, 0xB1,
    0xF0, 0xAE, 0x4C, 0x12, 0x91, 0xCF, 0x2D, 0x73,
    0xCA, 0x94, 0x76, 0x28, 0xAB, 0xF5, 0x17, 0x49,
    0x08, 0x56, 0xB4, 0xEA, 0x69, 0x37, 0xD5, 0x8B,
    0x57, 0x09, 0xEB, 0xB5, 0x36, 0x68, 0x8A, 0xD4,
    0x95, 0xCB, 0x29, 0x77, 0xF4, 0xAA, 0x48, 0x16,
    0xE9, 0xB7, 0x55, 0x0B, 0x88, 0xD6, 0x34, 0x6A,
    0x2B, 0x75, 0x97, 0xC9, 0x4A, 0x14, 0xF6, 0xA8,
    0x74, 0x2A, 0xC8, 0x96, 0x15, 0x4B, 0xA9, 0xF7,
    0xB6, 0xE8, 0x0A, 0x54, 0xD7, 0x89, 0x6B, 0x35,
])


def calculate_checksum(data: bytes, mode: str = "fixed") -> int:
    """按校验模式计算校验字节。mode: fixed / xor / crc8。"""
    if mode == "xor":
        c = 0
        for b in data:
            c ^= b
        return c & 0xFF
    if mode == "crc8":
        if not data:
            return 0
        c = data[0]
        for i in range(1, len(data)):
            c = CRC8_TABLE[c ^ data[i]]
        return c
    return FIXED_CHECKSUM


def add_checksum(body: bytes, mode: str = "fixed") -> bytes:
    """在命令体末尾追加校验字节，得到完整帧。"""
    return bytes(body) + bytes([calculate_checksum(bytes(body), mode)])


def parse_signed(data: bytes) -> int:
    """解析带符号数据：data[0] 为符号(0=正,1=负)，其余为无符号大端值。"""
    sign = -1 if data[0] == 1 else 1
    return sign * int.from_bytes(data[1:], "big")


# ---------------- 机械换算 ----------------

def full_steps_per_rev(step_angle: float) -> int:
    """整步数/圈：1.8° → 200，0.9° → 400。"""
    return 400 if abs(step_angle - 0.9) < 1e-6 else 200


def pulses_per_rev(microstep: int, step_angle: float = 1.8) -> int:
    """每圈脉冲数 = 整步数 × 细分。默认 200×16 = 3200。"""
    return full_steps_per_rev(step_angle) * int(microstep)


def count_to_mm(count: int, lead: float) -> float:
    """编码器计数 → 直线位移(mm)。65536 计数 = 1 圈 = 导程。"""
    return count * lead / 65536.0


def mm_to_count(mm: float, lead: float) -> int:
    """直线位移(mm) → 编码器计数。"""
    return round(mm * 65536.0 / lead)


def mm_to_deg(mm: float, lead: float) -> float:
    """直线位移(mm) → 角度(度)。"""
    return mm / lead * 360.0


def rpm_to_mms(rpm: float, lead: float) -> float:
    """转速 RPM → 线速度 mm/s。"""
    return rpm * lead / 60.0


def mms_to_rpm(mms: float, lead: float) -> float:
    """线速度 mm/s → 转速 RPM。"""
    return mms * 60.0 / lead


def mm_to_pulses(mm: float, lead: float, microstep: int, step_angle: float = 1.8) -> int:
    """直线位移(mm) → 脉冲数（有符号，向下为正）。"""
    return round(mm * pulses_per_rev(microstep, step_angle) / lead)


def position_resolution_mm(lead: float, microstep: int, step_angle: float = 1.8) -> float:
    """位置精度：单个脉冲对应的位移(mm)。"""
    return lead / pulses_per_rev(microstep, step_angle)


def encoder_resolution_mm(lead: float) -> float:
    """编码器反馈分辨率(mm)。"""
    return lead / 65536.0


def speed_resolution_mms(lead: float) -> float:
    """速度精度：1 RPM 对应的线速度(mm/s)。"""
    return lead / 60.0


def max_speed_mms(lead: float, max_rpm: int = 6000) -> float:
    """给定导程下的最大线速度(mm/s)。"""
    return max_rpm * lead / 60.0
