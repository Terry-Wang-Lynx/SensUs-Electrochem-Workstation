# -*- coding: utf-8 -*-
"""直线模组滑块可视化控件（苹果风格）。竖直导轨，顶部 0mm，底部为下止点。"""

from PySide6.QtCore import Qt, QRectF, QRect, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QBrush, QLinearGradient
from PySide6.QtWidgets import QWidget

BLUE = QColor("#007aff")
BLUE_TOP = QColor("#0a84ff")
BLUE_BOTTOM = QColor("#006be0")
RED = QColor("#ff3b30")
TEXT = QColor("#1d1d1f")
MUTED = QColor("#8e8e93")
TRACK = QColor("#f2f2f4")
BORDER = QColor("#e5e5ea")


def _font(size: int, bold: bool = False) -> QFont:
    f = QFont()
    f.setPointSize(size)
    f.setBold(bold)
    return f


class StageWidget(QWidget):
    def __init__(self, parent=None, travel: float = 39.0):
        super().__init__(parent)
        self._travel = float(travel)
        self._position = 0.0
        self._target = None
        self.setMinimumWidth(200)
        self.setMinimumHeight(200)

    def set_travel(self, t: float):
        self._travel = float(t)
        self.update()

    def set_position(self, mm: float):
        self._position = float(mm)
        self.update()

    def set_target(self, mm):
        self._target = float(mm) if mm is not None else None
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        left = 42
        top = 22
        bottom = 22
        read_w = 110
        track_x = left + 6
        track_w = max(30, w - track_x - read_w - 10)
        track_h = h - top - bottom
        track_y = top

        def y_for(mm):
            if self._travel <= 0:
                return track_y
            frac = max(0.0, min(1.0, mm / self._travel))
            return track_y + frac * track_h

        # 导轨（浅灰内槽 + 发丝边框）
        p.setPen(Qt.NoPen)
        p.setBrush(TRACK)
        p.drawRoundedRect(QRectF(track_x, track_y, track_w, track_h), 9, 9)
        p.setPen(QPen(BORDER, 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(track_x + 0.5, track_y + 0.5, track_w - 1, track_h - 1), 9, 9)

        # 刻度（每 5mm）
        p.setFont(_font(7))
        tick_pen = QPen(QColor("#d1d1d6"))
        for i in range(0, int(self._travel) + 1, 5):
            if i > self._travel:
                break
            y = y_for(float(i))
            p.setPen(tick_pen)
            p.drawLine(QPointF(track_x + track_w - 7, y), QPointF(track_x + track_w, y))
            p.setPen(MUTED)
            p.drawText(QRect(0, int(y) - 8, left - 6, 16),
                       Qt.AlignRight | Qt.AlignVCenter, f"{i:g}")

        # 顶部 / 底部标注
        p.setFont(_font(7))
        p.setPen(MUTED)
        p.drawText(QRectF(track_x, track_y - 15, track_w, 14),
                   Qt.AlignHCenter | Qt.AlignVCenter, "顶部 0 mm")
        p.drawText(QRectF(track_x, track_y + track_h + 1, track_w, 14),
                   Qt.AlignHCenter | Qt.AlignVCenter, f"底部 {self._travel:g} mm")

        # 目标线
        if self._target is not None:
            y = y_for(self._target)
            p.setPen(QPen(RED, 2, Qt.DashLine))
            p.drawLine(QPointF(track_x - 5, y), QPointF(track_x + track_w + 5, y))
            p.setPen(Qt.NoPen)
            p.setBrush(RED)
            p.drawEllipse(QPointF(track_x + track_w + 3, y), 3.5, 3.5)

        # 滑块（蓝色胶囊 + 高光 + 中心刻线）
        y = y_for(self._position)
        block_h = 22
        block = QRectF(track_x - 8, y - block_h / 2, track_w + 16, block_h)
        grad = QLinearGradient(block.topLeft(), block.bottomLeft())
        grad.setColorAt(0, BLUE_TOP)
        grad.setColorAt(1, BLUE_BOTTOM)
        p.setBrush(QBrush(grad))
        p.setPen(QPen(QColor("#0060c8"), 1))
        p.drawRoundedRect(block, 11, 11)
        p.setPen(QPen(QColor(255, 255, 255, 60), 1))
        p.drawLine(QPointF(block.left() + 6, block.top() + 2),
                   QPointF(block.right() - 6, block.top() + 2))
        p.setPen(QPen(QColor("#ffffff"), 1))
        p.drawLine(QPointF(block.left() + 6, y), QPointF(block.right() - 6, y))

        # 右侧读数区（固定位置，避免与滑块/目标线文字重叠）
        rx = track_x + track_w + 12
        rw = max(0, w - rx - 6)
        p.setFont(_font(8))
        p.setPen(MUTED)
        p.drawText(QRectF(rx, track_y, rw, 14), Qt.AlignLeft | Qt.AlignVCenter, "当前位置")
        p.setPen(BLUE)
        p.setFont(_font(10, True))
        p.drawText(QRectF(rx, track_y + 12, rw, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   f"{self._position:.2f} mm")

        if self._target is not None:
            p.setFont(_font(8))
            p.setPen(MUTED)
            p.drawText(QRectF(rx, track_y + 34, rw, 14), Qt.AlignLeft | Qt.AlignVCenter, "目标")
            p.setPen(RED)
            p.setFont(_font(9, True))
            p.drawText(QRectF(rx, track_y + 46, rw, 16), Qt.AlignLeft | Qt.AlignVCenter,
                       f"{self._target:.2f} mm")

        p.end()
