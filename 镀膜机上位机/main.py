# -*- coding: utf-8 -*-
"""直线模组上位机（苹果风格 · 单应用）。

精简 / 高级合并为一个界面：默认只显示核心控制，右上角小开关
「高级」可展开高级参数（导程 / 加速度 / 波特率 / 地址 / 校验 / 精度计算等）。

连接、参数读取、使能锁轴、堵转检测全部自动完成。
"""

import sys
import time

import serial.tools.list_ports
from PySide6.QtCore import Qt, Signal, Slot, QRectF
from PySide6.QtGui import QPainter, QColor, QPen, QBrush
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QComboBox, QPushButton, QDoubleSpinBox, QSpinBox,
    QPlainTextEdit, QSizePolicy, QScrollArea, QFrame,
)

import emm_protocol as proto
from motor import MotorController
from stage_widget import StageWidget

LEAD = 2.0
BAUD = 115200
ADDRESS = 3
DEFAULT_BOTTOM = 39.0

QSS = """
QMainWindow, QWidget#root { background: #f5f5f7; }
QLabel { color: #1d1d1f; }
QLabel#title { font-size: 16px; font-weight: 700; }
QLabel#subtitle { color: #6e6e73; font-size: 10px; }
QLabel#cardTitle { color: #6e6e73; font-size: 10px; font-weight: 600; }
QLabel#muted { color: #6e6e73; font-size: 10px; }
QLabel#big { font-size: 22px; font-weight: 700; color: #007aff; }
QLabel#ok { color: #34c759; font-weight: 600; font-size: 11px; }
QLabel#bad { color: #ff3b30; font-weight: 700; font-size: 15px; }

QFrame#card { background: #ffffff; border: 1px solid #ececf1; border-radius: 10px; }

QPushButton { background: #f2f2f4; border: none; border-radius: 7px;
              padding: 6px 10px; font-size: 11px; color: #1d1d1f; }
QPushButton:hover { background: #e8e8ec; }
QPushButton:pressed { background: #dfdfe4; }
QPushButton:disabled { background: #f4f4f6; color: #c0c0c6; }
QPushButton[kind="primary"] { background: #007aff; color: #ffffff; font-weight: 600; }
QPushButton[kind="primary"]:hover { background: #1a86ff; }
QPushButton[kind="primary"]:pressed { background: #006be0; }
QPushButton[kind="primary"]:disabled { background: #a6ccff; color: #f2f7ff; }
QPushButton[kind="danger"] { background: #ff3b30; color: #ffffff; font-weight: 600; }
QPushButton[kind="danger"]:hover { background: #ff554b; }
QPushButton[kind="danger"]:pressed { background: #e02b20; }
QPushButton[kind="danger"]:disabled { background: #ffb3ae; color: #ffffff; }
QPushButton[kind="jog"] { background: #f2f2f4; font-size: 14px; font-weight: 600;
                          padding: 9px; border-radius: 9px; }

QComboBox, QDoubleSpinBox, QSpinBox { background: #ffffff; border: 1px solid #d7d7dd;
    border-radius: 6px; padding: 4px 6px; font-size: 11px; }
QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus { border-color: #007aff; }
QComboBox::drop-down { border: none; width: 16px; }
QDoubleSpinBox#spinSpeed { font-size: 16px; font-weight: 600; padding: 8px 10px; }

QPlainTextEdit#log { background: #ffffff; border: none; font-size: 10px; color: #6e6e73; }
QScrollArea { background: transparent; border: none; }
"""


class ToggleSwitch(QWidget):
    """iOS 风格开关。"""
    toggled = Signal(bool)

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self._checked = checked
        self.setFixedSize(40, 24)
        self.setCursor(Qt.PointingHandCursor)

    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, on: bool):
        if on != self._checked:
            self._checked = on
            self.update()

    def mousePressEvent(self, event):
        self._checked = not self._checked
        self.toggled.emit(self._checked)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        track = QRectF(0, 1, 40, 22)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#34c759") if self._checked else QColor("#e5e5ea"))
        p.drawRoundedRect(track, 11, 11)
        knob_x = 21.0 if self._checked else 3.0
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(knob_x, 3, 18, 18))
        p.end()


def _card(title: str = None):
    """返回 (卡片 Frame, 内部布局)，可选标题。"""
    card = QFrame()
    card.setObjectName("card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(12, 10, 12, 10)
    lay.setSpacing(8)
    if title:
        t = QLabel(title)
        t.setObjectName("cardTitle")
        lay.addWidget(t)
    return card, lay


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("直线模组")
        self.resize(400, 620)

        self.motor = MotorController()
        self.bottom_mm = DEFAULT_BOTTOM
        self._current_mm = 0.0
        self._microstep = 256
        self._step_angle = 1.8
        self._stall_protect = None

        self._build_ui()
        self._connect_signals()

        self.motor.set_mechanical(LEAD, 256, 1.8)
        self._refresh_ports()
        self._on_connected(False)
        self.motor.start()

    # ---------------- UI ----------------
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        # 头部
        header = QHBoxLayout()
        head_txt = QVBoxLayout()
        t = QLabel("直线模组")
        t.setObjectName("title")
        st = QLabel("张大头 Emm 固件 · 竖直滑块")
        st.setObjectName("subtitle")
        head_txt.addWidget(t)
        head_txt.addWidget(st)
        header.addLayout(head_txt)
        header.addStretch(1)
        adv_label = QLabel("高级")
        adv_label.setObjectName("muted")
        self.switch_adv = ToggleSwitch(False)
        header.addWidget(adv_label)
        header.addWidget(self.switch_adv)
        outer.addLayout(header)

        # 滚动区
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("root")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # ---- 连接卡片 ----
        conn_card, conn_lay = _card("连接")
        row = QHBoxLayout()
        self.combo_port = QComboBox()
        self.combo_port.setMinimumWidth(110)
        self.btn_refresh = QPushButton("刷新")
        self.btn_connect = QPushButton("连接")
        self.btn_connect.setProperty("kind", "primary")
        self.lbl_conn = QLabel("○ 未连接")
        self.lbl_conn.setObjectName("muted")
        row.addWidget(self.combo_port, 1)
        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_connect)
        row.addWidget(self.lbl_conn)
        conn_lay.addLayout(row)

        # 高级连接参数（默认隐藏）
        adv_conn = QHBoxLayout()
        adv_conn.addWidget(QLabel("波特率"))
        self.spin_baud = QSpinBox()
        self.spin_baud.setRange(1200, 1000000)
        self.spin_baud.setValue(BAUD)
        self.spin_baud.setSingleStep(100)
        adv_conn.addWidget(self.spin_baud)
        adv_conn.addSpacing(10)
        adv_conn.addWidget(QLabel("地址"))
        self.spin_addr = QSpinBox()
        self.spin_addr.setRange(1, 255)
        self.spin_addr.setValue(ADDRESS)
        adv_conn.addWidget(self.spin_addr)
        adv_conn.addSpacing(10)
        adv_conn.addWidget(QLabel("校验"))
        self.combo_csum = QComboBox()
        self.combo_csum.addItems(["固定 0x6B", "异或", "CRC8"])
        adv_conn.addWidget(self.combo_csum)
        adv_conn.addStretch(1)
        self._adv_conn = self._wrap(adv_conn)
        conn_lay.addWidget(self._adv_conn)
        lay.addWidget(conn_card)

        # ---- 滑块卡片 ----
        stage_card, stage_lay = _card()
        self.stage = StageWidget(travel=DEFAULT_BOTTOM)
        self.stage.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.stage.setMinimumHeight(220)
        stage_lay.addWidget(self.stage)
        lay.addWidget(stage_card, 1)

        # ---- 状态卡片 ----
        status_card, status_lay = _card("状态")
        srow = QHBoxLayout()
        pos_col = QVBoxLayout()
        pos_cap = QLabel("当前位置")
        pos_cap.setObjectName("muted")
        self.lbl_pos = QLabel("0.00 mm")
        self.lbl_pos.setObjectName("big")
        pos_col.addWidget(pos_cap)
        pos_col.addWidget(self.lbl_pos)
        srow.addLayout(pos_col)
        srow.addStretch(1)
        spd_col = QVBoxLayout()
        spd_cap = QLabel("速度")
        spd_cap.setObjectName("muted")
        self.lbl_spd = QLabel("0.0 mm/s")
        spd_col.addWidget(spd_cap)
        spd_col.addWidget(self.lbl_spd)
        srow.addLayout(spd_col)
        srow.addStretch(1)
        stl_col = QVBoxLayout()
        stl_cap = QLabel("堵转保护")
        stl_cap.setObjectName("muted")
        self.lbl_stall = QLabel("正常")
        self.lbl_stall.setObjectName("ok")
        stl_col.addWidget(stl_cap)
        stl_col.addWidget(self.lbl_stall)
        srow.addLayout(stl_col)
        status_lay.addLayout(srow)
        lay.addWidget(status_card)

        # ---- 控制卡片 ----
        ctrl_card, ctrl_lay = _card("控制")
        # 速度
        spd_row = QHBoxLayout()
        spd_row.addWidget(QLabel("速度"))
        self.spin_speed = QDoubleSpinBox()
        self.spin_speed.setObjectName("spinSpeed")
        self.spin_speed.setRange(0.1, 500.0)
        self.spin_speed.setDecimals(1)
        self.spin_speed.setValue(10.0)
        self.spin_speed.setSuffix(" mm/s")
        self.spin_speed.setMinimumWidth(170)
        spd_row.addWidget(self.spin_speed)
        spd_row.addStretch(1)
        ctrl_lay.addLayout(spd_row)

        grid = QGridLayout()
        grid.setSpacing(10)
        self.btn_up = QPushButton("▲ 上移")
        self.btn_up.setProperty("kind", "jog")
        self.btn_down = QPushButton("▼ 下移")
        self.btn_down.setProperty("kind", "jog")
        self.btn_home = QPushButton("归零（上零点）")
        self.btn_set_bottom = QPushButton("设为下止点")
        self.btn_go_top = QPushButton("到上零点")
        self.btn_go_top.setProperty("kind", "primary")
        self.btn_go_bottom = QPushButton("到下止点")
        self.btn_go_bottom.setProperty("kind", "primary")
        grid.addWidget(self.btn_up, 0, 0)
        grid.addWidget(self.btn_down, 0, 1)
        grid.addWidget(self.btn_home, 1, 0)
        grid.addWidget(self.btn_set_bottom, 1, 1)
        grid.addWidget(self.btn_go_top, 2, 0)
        grid.addWidget(self.btn_go_bottom, 2, 1)

        self.btn_estop = QPushButton("■ 急停")
        self.btn_estop.setProperty("kind", "danger")
        self.btn_estop.setMinimumHeight(34)
        self.btn_clear = QPushButton("解除堵转保护")
        grid.addWidget(self.btn_estop, 3, 0, 1, 2)
        grid.addWidget(self.btn_clear, 4, 0, 1, 2)
        ctrl_lay.addLayout(grid)

        # 高级运动（默认隐藏）
        adv_mot = QGridLayout()
        adv_mot.setSpacing(8)
        adv_mot.addWidget(QLabel("加速度档"), 0, 0)
        self.spin_accel = QSpinBox()
        self.spin_accel.setRange(0, 255)
        self.spin_accel.setValue(100)
        adv_mot.addWidget(self.spin_accel, 0, 1)
        self.btn_enable = QPushButton("使能")
        self.btn_enable.setCheckable(True)
        self.btn_enable.setChecked(True)
        self.btn_enable.setProperty("kind", "primary")
        self.btn_zero = QPushButton("设零")
        self.btn_stop_home = QPushButton("停止回零")
        adv_mot.addWidget(self.btn_enable, 1, 0)
        adv_mot.addWidget(self.btn_zero, 1, 1)
        adv_mot.addWidget(self.btn_stop_home, 2, 0, 1, 2)
        self._adv_motion = self._wrap(adv_mot)
        ctrl_lay.addWidget(self._adv_motion)
        lay.addWidget(ctrl_card)

        # ---- 机械参数卡片（默认隐藏）----
        param_card, param_lay = _card("机械参数与精度")
        pgrid = QGridLayout()
        pgrid.setSpacing(8)
        pgrid.addWidget(QLabel("丝杆导程"), 0, 0)
        self.spin_lead = QDoubleSpinBox()
        self.spin_lead.setRange(0.5, 100.0)
        self.spin_lead.setDecimals(2)
        self.spin_lead.setValue(LEAD)
        self.spin_lead.setSuffix(" mm")
        pgrid.addWidget(self.spin_lead, 0, 1)
        pgrid.addWidget(QLabel("细分"), 1, 0)
        self.lbl_microstep = QLabel("256")
        pgrid.addWidget(self.lbl_microstep, 1, 1)
        pgrid.addWidget(QLabel("步距角"), 2, 0)
        self.lbl_step = QLabel("1.8°")
        pgrid.addWidget(self.lbl_step, 2, 1)
        pgrid.addWidget(QLabel("堵转保护电流"), 3, 0)
        self.lbl_stall_cur = QLabel("—")
        pgrid.addWidget(self.lbl_stall_cur, 3, 1)

        self.lbl_precision = QLabel("")
        self.lbl_precision.setObjectName("muted")
        self.lbl_precision.setWordWrap(True)
        param_lay.addLayout(pgrid)
        param_lay.addWidget(self.lbl_precision)
        self._adv_param = param_card
        lay.addWidget(self._adv_param)

        # ---- 日志卡片（仅高级模式显示）----
        log_card, log_lay = _card()
        self._adv_log = log_card
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(54)
        self.log.setMaximumHeight(70)
        log_lay.addWidget(self.log)
        lay.addWidget(log_card)

        self.scroll.setWidget(body)
        outer.addWidget(self.scroll, 1)

        self.motion_widgets = [
            self.spin_speed, self.btn_up, self.btn_down, self.btn_home,
            self.btn_set_bottom, self.btn_go_top, self.btn_go_bottom,
            self.btn_estop, self.btn_clear, self.spin_baud, self.spin_addr,
            self.combo_csum, self.spin_accel, self.btn_enable, self.btn_zero,
            self.btn_stop_home, self.spin_lead,
        ]

        # 高级内容默认隐藏，仅在打开右上角开关后显示
        for w in (self._adv_conn, self._adv_motion, self._adv_param, self._adv_log):
            w.setVisible(False)

    def _wrap(self, layout) -> QWidget:
        w = QWidget()
        w.setObjectName("root")
        w.setLayout(layout)
        return w

    def _connect_signals(self):
        m = self.motor
        m.sigTelemetry.connect(self._on_telemetry)
        m.sigLog.connect(self._log)
        m.sigError.connect(self._on_error)
        m.sigConnected.connect(self._on_connected)
        m.sigResult.connect(self._on_result)
        m.sigConfig.connect(self._on_config)

        self.switch_adv.toggled.connect(self._on_toggle_adv)

        self.btn_refresh.clicked.connect(self._refresh_ports)
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_estop.clicked.connect(m.estop)
        self.btn_clear.clicked.connect(m.clear_protection)
        self.btn_home.clicked.connect(lambda: m.home(proto.HOME_COLLISION))
        self.btn_stop_home.clicked.connect(m.stop_home)
        self.btn_set_bottom.clicked.connect(self._on_set_bottom)
        self.btn_go_top.clicked.connect(lambda: self._go(0.0))
        self.btn_go_bottom.clicked.connect(lambda: self._go(self.bottom_mm))
        self.btn_zero.clicked.connect(m.zero)
        self.btn_enable.toggled.connect(
            lambda on: m.enable() if on else m.disable())

        self.spin_lead.valueChanged.connect(self._recalc_precision)
        self.spin_baud.valueChanged.connect(self._apply_advanced)
        self.spin_addr.valueChanged.connect(self._apply_advanced)
        self.combo_csum.currentIndexChanged.connect(self._apply_advanced)
        self.spin_accel.valueChanged.connect(self._apply_advanced)

        self.btn_up.pressed.connect(lambda: m.jog(True, self.spin_speed.value()))
        self.btn_up.released.connect(m.estop)
        self.btn_down.pressed.connect(lambda: m.jog(False, self.spin_speed.value()))
        self.btn_down.released.connect(m.estop)

    # ---------------- 槽 ----------------
    @Slot(bool)
    def _on_toggle_adv(self, on: bool):
        for w in (self._adv_conn, self._adv_motion, self._adv_param, self._adv_log):
            w.setVisible(on)
        self._log(f"高级选项已{'显示' if on else '隐藏'}")

    @Slot()
    def _refresh_ports(self):
        current = self.combo_port.currentText()
        self.combo_port.clear()
        port_infos = list(serial.tools.list_ports.comports())
        ports = [p.device for p in port_infos]
        self.combo_port.addItems(ports)
        if current in ports:
            self.combo_port.setCurrentText(current)
        else:
            for p in port_infos:
                dev = (p.device or "").lower()
                desc = (p.description or "").lower()
                hwid = (p.hwid or "").lower()
                if (
                    getattr(p, "vid", None) == 0x1A86
                    or "usbserial" in dev
                    or "wchusbserial" in dev
                    or "ch340" in desc
                    or "ch341" in desc
                    or "usb-serial" in desc
                    or "usb serial" in desc
                    or "1a86" in hwid
                ):
                    self.combo_port.setCurrentText(p.device)
                    break

    @Slot()
    def _on_connect(self):
        if self.motor.is_connected:
            self.motor.disconnect()
            return
        port = self.combo_port.currentText()
        if not port:
            self._log("⚠ 请先选择串口")
            return
        self.motor.connect_port(port, self.spin_baud.value(),
                                self.spin_addr.value())

    @Slot(bool)
    def _on_connected(self, ok: bool):
        self.btn_connect.setText("断开" if ok else "连接")
        self.btn_connect.setProperty("kind", "primary" if not ok else "danger")
        self.btn_connect.style().unpolish(self.btn_connect)
        self.btn_connect.style().polish(self.btn_connect)
        self.lbl_conn.setText("● 已连接" if ok else "○ 未连接")
        self.lbl_conn.setObjectName("ok" if ok else "muted")
        self.lbl_conn.style().unpolish(self.lbl_conn)
        self.lbl_conn.style().polish(self.lbl_conn)
        for w in self.motion_widgets:
            w.setEnabled(ok)

    @Slot(float, float, int, int)
    def _on_telemetry(self, pos: float, speed: float, status: int, temp: int):
        self._current_mm = pos
        self.stage.set_position(pos)
        self.lbl_pos.setText(f"{pos:.2f} mm")
        self.lbl_spd.setText(f"{speed:.2f} mm/s")
        enabled = bool(status & proto.STATUS_ENABLED)
        self.btn_enable.blockSignals(True)
        self.btn_enable.setChecked(enabled)
        self.btn_enable.setText("使能中" if enabled else "已失能")
        self.btn_enable.blockSignals(False)
        stall = bool(status & proto.STATUS_STALL) or bool(status & proto.STATUS_STALL_PROTECTED)
        self.lbl_stall.setText("保护触发！" if stall else "正常")
        self.lbl_stall.setObjectName("bad" if stall else "ok")
        self.lbl_stall.style().unpolish(self.lbl_stall)
        self.lbl_stall.style().polish(self.lbl_stall)

    @Slot(dict)
    def _on_config(self, cfg: dict):
        self._microstep = cfg.get("microstep", 256)
        self._step_angle = cfg.get("step_angle", 1.8)
        self._stall_protect = cfg.get("stall_protect")
        self.lbl_microstep.setText(str(self._microstep))
        self.lbl_step.setText(f"{self._step_angle:.1f}°")
        if self._stall_protect is not None:
            self.lbl_stall_cur.setText(str(self._stall_protect))
        self._recalc_precision()

    @Slot()
    def _recalc_precision(self):
        lead = self.spin_lead.value()
        micro = self._microstep or 256
        step_angle = self._step_angle or 1.8
        try:
            ppr = proto.pulses_per_rev(micro, step_angle)
            pos_res = proto.position_resolution_mm(lead, micro, step_angle)
            enc_res = proto.encoder_resolution_mm(lead)
            spd_res = proto.speed_resolution_mms(lead)
            vmax = proto.max_speed_mms(lead)
            self.lbl_precision.setText(
                f"脉冲/圈：{ppr:,}　位置精度：{pos_res*1000:.4f} μm/pulse　"
                f"编码分辨率：{enc_res*1000:.4f} μm\n"
                f"速度精度：{spd_res:.4f} mm/s　最大线速度：{vmax:.1f} mm/s")
            # 同步到控制线程
            self.motor.set_mechanical(lead, micro, step_angle)
        except Exception:
            pass

    def _apply_advanced(self):
        modes = ["fixed", "xor", "crc8"]
        self.motor.set_checksum(modes[self.combo_csum.currentIndex()])
        self.motor.set_accel(self.spin_accel.value())

    @Slot()
    def _on_set_bottom(self):
        if self._current_mm < 0.5:
            self._log("⚠ 当前位置太靠近零点，请先移到下方再设为下止点")
            return
        self.bottom_mm = self._current_mm
        self.stage.set_travel(self.bottom_mm)
        self._log(f"下止点已设为 {self.bottom_mm:.2f} mm")

    def _go(self, target_mm: float):
        self.stage.set_target(target_mm)
        self.motor.move_to(target_mm, self.spin_speed.value())

    @Slot(str, bool)
    def _on_result(self, name: str, ok: bool):
        self._log(f"[{name}] {'成功' if ok else '失败'}")

    @Slot(str)
    def _on_error(self, msg: str):
        self._log(f"⚠ {msg}")

    def _log(self, text: str):
        self.log.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {text}")

    def closeEvent(self, event):
        self.motor.request_stop()
        self.motor.wait(1500)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
