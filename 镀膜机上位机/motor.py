# -*- coding: utf-8 -*-
"""电机控制线程：持有串口，处理命令队列与周期性状态轮询。

设计要点：
- 所有串口读写都发生在同一个工作线程内，避免并发访问串口。
- 界面通过 enqueue 方法下发命令，通过信号回传遥测/日志/结果。
- 方向约定（张大头 Emm 固件）：向上 = CCW = 方向字节 1，向下 = CW = 方向字节 0。
- 细分默认 256（1.8° 电机下 51200 脉冲/圈），连接后自动从电机读取并覆盖。
"""

import queue
import threading
import time

import serial
from PySide6.QtCore import QThread, Signal

import emm_protocol as proto


class MotorController(QThread):
    # pos_mm, speed_mms, status_byte, temp_c
    sigTelemetry = Signal(float, float, int, int)
    sigLog = Signal(str)
    sigError = Signal(str)
    sigConnected = Signal(bool)
    sigResult = Signal(str, bool)          # 动作名, 是否成功
    sigVersion = Signal(str, str)          # 固件版本串, 硬件串
    sigConfig = Signal(dict)               # 电机配置（细分/步距角/堵转保护等）

    def __init__(self):
        super().__init__()
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self._serial = None
        self._connected = False
        self._address = 3

        # 机械参数（lead 来自界面输入；microstep/step_angle 连接后自动读取）
        self._lead = 2.0
        self._microstep = 256
        self._step_angle = 1.8
        self._accel = 10
        self._checksum = "fixed"

    # ---------------- 参数设置（界面调用，线程安全） ----------------
    def set_mechanical(self, lead: float, microstep: int, step_angle: float):
        with self._lock:
            self._lead = float(lead)
            self._microstep = int(microstep)
            self._step_angle = float(step_angle)

    def set_accel(self, accel: int):
        with self._lock:
            self._accel = int(accel)

    def set_checksum(self, mode: str):
        with self._lock:
            self._checksum = mode

    # ---------------- 命令入队（界面调用） ----------------
    def connect_port(self, port, baud, address):
        self._q.put(("connect", port, int(baud), int(address)))

    def disconnect(self):
        self._q.put(("disconnect",))

    def enable(self):
        self._q.put(("enable",))

    def disable(self):
        self._q.put(("disable",))

    def estop(self):
        self._q.put(("stop",))

    def move_to(self, target_mm: float, speed_mms: float):
        self._q.put(("move", float(target_mm), float(speed_mms)))

    def jog(self, up: bool, speed_mms: float):
        self._q.put(("jog", bool(up), float(speed_mms)))

    def zero(self):
        self._q.put(("zero",))

    def home(self, mode: int = proto.HOME_COLLISION):
        self._q.put(("home", int(mode)))

    def stop_home(self):
        self._q.put(("stop_home",))

    def clear_protection(self):
        self._q.put(("clear_protection",))

    def request_stop(self):
        self._stop.set()
        self._q.put(("__quit__",))

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ---------------- 线程主循环 ----------------
    def run(self):
        last_poll = 0.0
        while not self._stop.is_set():
            # 处理命令队列
            while True:
                try:
                    cmd = self._q.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(cmd)
                except Exception as e:  # 单个命令异常不影响线程
                    self.sigError.emit(f"命令异常: {e}")

            # 周期轮询遥测
            if self._connected and time.monotonic() - last_poll >= 0.1:
                last_poll = time.monotonic()
                try:
                    self._poll()
                except Exception as e:
                    self.sigError.emit(f"轮询异常: {e}")

            time.sleep(0.01)

    # ---------------- 命令处理 ----------------
    def _handle(self, cmd):
        name = cmd[0]
        if name == "__quit__":
            self._teardown()
            return
        if name == "connect":
            self._do_connect(cmd[1], cmd[2], cmd[3])
            return
        if name == "disconnect":
            self._teardown()
            return
        if not self._connected:
            self.sigError.emit("尚未连接电机")
            return

        if name == "enable":
            ok = self._transact(bytes([self._address, proto.CODE_ENABLE, 0xAB, 0x01, 0x00]), 4)
            self.sigResult.emit("使能", ok is not None)
        elif name == "disable":
            ok = self._transact(bytes([self._address, proto.CODE_ENABLE, 0xAB, 0x00, 0x00]), 4)
            self.sigResult.emit("失能", ok is not None)
        elif name == "stop":
            ok = self._transact(bytes([self._address, proto.CODE_ESTOP, 0x98, 0x00]), 4)
            self.sigResult.emit("急停", ok is not None)
        elif name == "zero":
            ok = self._transact(bytes([self._address, proto.CODE_ZERO, 0x6D]), 4)
            self.sigResult.emit("设零", ok is not None)
        elif name == "home":
            ok = self._transact(bytes([self._address, proto.CODE_HOME, cmd[1], 0x00]), 4)
            self.sigResult.emit("回零", ok is not None)
        elif name == "stop_home":
            ok = self._transact(bytes([self._address, proto.CODE_STOP_HOME, 0x48]), 4)
            self.sigResult.emit("停止回零", ok is not None)
        elif name == "clear_protection":
            ok = self._transact(bytes([self._address, proto.CODE_CLEAR_PROTECTION, 0x52]), 4)
            self.sigResult.emit("解除堵转保护", ok is not None)
        elif name == "move":
            self._do_move(cmd[1], cmd[2])
        elif name == "jog":
            self._do_jog(cmd[1], cmd[2])

    # ---------------- 连接 / 断开 ----------------
    def _do_connect(self, port, baud, address):
        self._teardown()
        try:
            ser = serial.Serial(port, baud, timeout=0.1)
            self._serial = ser
            self._address = address
            self._connected = True
            time.sleep(0.05)

            version = self._read_version()
            cfg = self._read_config()

            self.sigConnected.emit(True)
            if version:
                self.sigVersion.emit(version[0], version[1])
            if cfg:
                self.sigConfig.emit(cfg)
                with self._lock:
                    self._microstep = cfg["microstep"]
                    self._step_angle = cfg["step_angle"]

            if version:
                self.sigLog.emit(
                    f"已连接 {port}，固件 {version[0]}，硬件 {version[1]}，地址 {address}"
                )
            else:
                self.sigLog.emit(f"已打开 {port}（未读到版本，请检查接线/地址/校验方式）")

            # 竖直模组：自动使能锁轴，防止滑块因重力下滑
            self._transact(bytes([self._address, proto.CODE_ENABLE, 0xAB, 0x01, 0x00]), 4)
            self.sigLog.emit("已自动使能（锁轴，防止滑块因重力下滑）")
        except Exception as e:
            self._connected = False
            self._serial = None
            self.sigConnected.emit(False)
            self.sigError.emit(f"连接失败: {e}")

    def _teardown(self):
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        self._connected = False
        self.sigConnected.emit(False)

    # ---------------- 运动命令 ----------------
    def _do_move(self, target_mm: float, speed_mms: float):
        cur = self._read_position_mm()
        if cur is None:
            self.sigError.emit("无法读取当前位置")
            self.sigResult.emit("移动", False)
            return
        delta = target_mm - cur
        if abs(delta) < 0.0005:
            self.sigLog.emit(f"已在目标位置附近（当前 {cur:.3f} mm）")
            self.sigResult.emit("移动", True)
            return

        with self._lock:
            lead = self._lead
            microstep = self._microstep
            step_angle = self._step_angle
            accel = self._accel

        rpm = max(1, min(6000, int(round(proto.mms_to_rpm(speed_mms, lead)))))
        delta_pulses = proto.mm_to_pulses(delta, lead, microstep, step_angle)
        direction = 0 if delta_pulses >= 0 else 1  # 向下=CW(0)，向上=CCW(1)
        pulses = abs(delta_pulses)

        body = bytes([
            self._address, proto.CODE_POSITION, direction,
            (rpm >> 8) & 0xFF, rpm & 0xFF, accel,
            (pulses >> 24) & 0xFF, (pulses >> 16) & 0xFF,
            (pulses >> 8) & 0xFF, pulses & 0xFF,
            2, 0,  # 运动模式=相对当前实时位置；同步=0
        ])
        ok = self._transact(body, 4)
        self.sigResult.emit("移动", ok is not None)
        self.sigLog.emit(f"移动 Δ={delta:+.3f} mm → {pulses} 脉冲 @ {rpm} RPM")

    def _do_jog(self, up: bool, speed_mms: float):
        with self._lock:
            lead = self._lead
            accel = self._accel
        rpm = max(1, min(6000, int(round(proto.mms_to_rpm(speed_mms, lead)))))
        direction = 1 if up else 0  # 向上=CCW(1)，向下=CW(0)
        body = bytes([
            self._address, proto.CODE_JOG, direction,
            (rpm >> 8) & 0xFF, rpm & 0xFF, accel, 0,
        ])
        self._transact(body, 4)

    # ---------------- 轮询 ----------------
    def _poll(self):
        pos = self._read_position_mm()
        speed = self._read_speed_mms()
        status = self._read_status()
        temp = self._read_temp()
        if pos is not None:
            self.sigTelemetry.emit(
                pos,
                speed if speed is not None else 0.0,
                status if status is not None else 0,
                temp if temp is not None else 0,
            )

    # ---------------- 读命令 ----------------
    def _read_position_mm(self):
        with self._lock:
            lead = self._lead
        r = self._transact(bytes([self._address, proto.CODE_GET_REALTIME_POSITION]), 8)
        if r is None:
            return None
        cnt = proto.parse_signed(r[2:7])
        return proto.count_to_mm(cnt, lead)

    def _read_speed_mms(self):
        with self._lock:
            lead = self._lead
        r = self._transact(bytes([self._address, proto.CODE_GET_REALTIME_SPEED]), 6)
        if r is None:
            return None
        rpm = proto.parse_signed(r[2:5])
        return proto.rpm_to_mms(rpm, lead)

    def _read_status(self):
        r = self._transact(bytes([self._address, proto.CODE_GET_MOTOR_STATUS]), 4)
        return r[2] if r else None

    def _read_temp(self):
        r = self._transact(bytes([self._address, proto.CODE_GET_TEMP]), 5)
        if r is None:
            return None
        return (1 if r[2] == 1 else -1) * r[3]

    def _read_version(self):
        r = self._transact(bytes([self._address, proto.CODE_GET_VERSION]), 7)
        if r is None:
            return None
        fw = int.from_bytes(r[2:4], "big")
        hw_info = int.from_bytes(r[4:6], "big")
        major, minor, patch = fw // 100, (fw % 100) // 10, fw % 10
        hw_type = (hw_info >> 8) & 0x0F
        types = {0: "20", 1: "28", 2: "35", 3: "42", 4: "57", 5: "86"}
        return (f"V{major}.{minor}.{patch}", f"X{types.get(hw_type, '?')}")

    def _read_config(self):
        """读取驱动配置（动态长度响应），提取细分/步距角/堵转保护参数。"""
        r = self._transact_dynamic(bytes([self._address, proto.CODE_GET_CONFIG, 0x6C]), timeout=0.6)
        if r is None or len(r) < 6:
            return None
        data = r[4:-1]  # 去掉 addr/code/byte_count/param_count/checksum
        if len(data) < 28:
            return None
        ms = data[5]
        microstep = 256 if ms == 0 else ms  # 256 细分在配置里存 0
        step_angle = 0.9 if data[0] == 0x32 else 1.8
        return {
            "microstep": microstep,
            "step_angle": step_angle,
            "stall_protect": data[19],          # 0=关闭 1=使能 2=堵转后回零
            "stall_speed": int.from_bytes(data[20:22], "big"),
            "stall_current": int.from_bytes(data[22:24], "big"),
            "stall_time": int.from_bytes(data[24:26], "big"),
        }

    # ---------------- 串口底层 ----------------
    def _transact(self, body: bytes, resp_len: int, timeout: float = 0.3):
        ser = self._serial
        if ser is None:
            return None
        with self._lock:
            mode = self._checksum
        frame = proto.add_checksum(body, mode)
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(frame)
            ser.flush()
            time.sleep(0.003)
            resp = self._read_exact(resp_len, timeout)
        except Exception:
            return None
        if resp is None or len(resp) != resp_len:
            return None
        if resp[-1] != proto.calculate_checksum(resp[:-1], mode):
            return None
        return resp

    def _transact_dynamic(self, body: bytes, timeout: float = 0.6):
        """读写动态长度响应：addr + code + byte_count + param_count + data + checksum。"""
        ser = self._serial
        if ser is None:
            return None
        with self._lock:
            mode = self._checksum
        frame = proto.add_checksum(body, mode)
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(frame)
            ser.flush()
            time.sleep(0.003)
            head = self._read_exact(4, timeout)
            if head is None or len(head) != 4:
                return None
            remaining = head[2] - 4  # 总字节数 - 已读4字节 = 数据 + 校验
            if remaining < 1 or remaining > 128:
                return None
            rest = self._read_exact(remaining, timeout)
            if rest is None or len(rest) != remaining:
                return None
            resp = head + rest
        except Exception:
            return None
        if resp[-1] != proto.calculate_checksum(resp[:-1], mode):
            return None
        return resp

    def _read_exact(self, n: int, timeout: float):
        ser = self._serial
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while len(buf) < n and time.monotonic() < deadline:
            b = ser.read(1)
            if b:
                buf += b
        return bytes(buf) if len(buf) == n else None
