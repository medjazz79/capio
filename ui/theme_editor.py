"""DaVinci-style primary color wheels for personal theme editing."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor, QImage, QPainter, QPainterPath, QPen, QRadialGradient,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from ui.theme import tone_curve_slopes


_WHEEL_IMAGES: dict[int, QImage] = {}


def _wheel_image(side: int) -> QImage:
    cached = _WHEEL_IMAGES.get(side)
    if cached is not None:
        return cached

    image = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    center = side / 2
    outer_radius = center - 2
    inner_radius = outer_radius * 0.78
    for y in range(side):
        dy = y + 0.5 - center
        for x in range(side):
            dx = x + 0.5 - center
            distance = math.hypot(dx, dy)
            if inner_radius <= distance <= outer_radius:
                angle = math.degrees(math.atan2(dy, dx))
                hue = round((240 - angle) % 360)
                image.setPixelColor(x, y, QColor.fromHsv(hue, 255, 255))
    _WHEEL_IMAGES[side] = image
    return image


class ColorBalanceWheel(QWidget):
    """Circular hue control where puck direction selects tint and radius its strength."""

    balanceChanged = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(144, 144)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._balance_x = 0.0
        self._balance_y = 0.0

    def sizeHint(self) -> QSize:
        return QSize(220, 220)

    @property
    def balance(self) -> tuple[float, float]:
        return self._balance_x, self._balance_y

    def setBalance(self, x: float, y: float, emit: bool = False) -> None:
        x, y = self._clamp_balance(x, y)
        if math.isclose(x, self._balance_x) and math.isclose(
            y, self._balance_y
        ):
            return
        self._balance_x, self._balance_y = x, y
        self.update()
        if emit:
            self.balanceChanged.emit(x, y)

    @staticmethod
    def _clamp_balance(x: float, y: float) -> tuple[float, float]:
        radius = math.hypot(x, y)
        if radius > 1:
            return x / radius, y / radius
        return max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y))

    def paintEvent(self, _event) -> None:
        side = min(self.width(), self.height())
        center = QPointF(self.width() / 2, self.height() / 2)
        radius = side / 2 - 2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        image = _wheel_image(side)
        painter.drawImage(
            int(center.x() - side / 2),
            int(center.y() - side / 2),
            image,
        )

        inner_radius = radius * 0.78 - 1
        color_angle = math.degrees(math.atan2(
            self._balance_y, self._balance_x))
        tint_hue = round((240 - color_angle) % 360)
        tint = QColor.fromHsv(tint_hue, 190, 255)
        center_fill = QColor("#20242B")
        center_fill.setAlpha(235)
        painter.setBrush(center_fill)
        painter.setPen(QPen(QColor("#515761"), 1))
        painter.drawEllipse(center, inner_radius, inner_radius)

        if self._balance_x or self._balance_y:
            glow = QRadialGradient(center, inner_radius)
            tinted = QColor(tint)
            tinted.setAlpha(round(
                75 * math.hypot(self._balance_x, self._balance_y)))
            glow.setColorAt(0, tinted)
            glow.setColorAt(1, QColor(0, 0, 0, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(center, inner_radius, inner_radius)

        crosshair = QPen(QColor("#777D86"), 1)
        painter.setPen(crosshair)
        painter.drawLine(
            QPointF(center.x() - inner_radius, center.y()),
            QPointF(center.x() + inner_radius, center.y()),
        )
        painter.drawLine(
            QPointF(center.x(), center.y() - inner_radius),
            QPointF(center.x(), center.y() + inner_radius),
        )

        puck_radius = inner_radius * 0.88
        puck = QPointF(
            center.x() + self._balance_x * puck_radius,
            center.y() + self._balance_y * puck_radius,
        )
        painter.setPen(QPen(QColor("#15171B"), 2))
        painter.setBrush(QColor("#E6E8EB"))
        painter.drawEllipse(puck, 5, 5)
        painter.setPen(QPen(QColor("#FFFFFF"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(puck, 5, 5)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._select_balance(event.position())

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._select_balance(event.position())

    def _select_balance(self, point: QPointF) -> None:
        side = min(self.width(), self.height())
        radius = max(1.0, ((side / 2 - 2) * 0.78 - 1) * 0.88)
        center_x, center_y = self.width() / 2, self.height() / 2
        x = (point.x() - center_x) / radius
        y = (point.y() - center_y) / radius
        x, y = self._clamp_balance(x, y)
        if math.isclose(x, self._balance_x, abs_tol=0.005) and math.isclose(
            y, self._balance_y, abs_tol=0.005
        ):
            return
        self._balance_x, self._balance_y = x, y
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.update()
        self.balanceChanged.emit(x, y)

    def keyPressEvent(self, event) -> None:
        directions = {
            Qt.Key.Key_Left: (-1, 0),
            Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, -1),
            Qt.Key.Key_Down: (0, 1),
        }
        direction = directions.get(event.key())
        if direction is None:
            super().keyPressEvent(event)
            return
        step = 0.002 if event.modifiers() & Qt.KeyboardModifier.ControlModifier else (
            0.05 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            else 0.01
        )
        self.setBalance(
            self._balance_x + direction[0] * step,
            self._balance_y + direction[1] * step,
            emit=True,
        )
        event.accept()

    def reset(self) -> None:
        self.setBalance(0, 0, emit=True)


class ToneCurveEditor(QWidget):
    """Editable master RGB tone curve with normalized input/output points."""

    pointsChanged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(190)
        self.setMinimumWidth(280)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._points: list[tuple[float, float]] = [(0.0, 0.0), (1.0, 1.0)]
        self._active_point: int | None = None
        self._curve_color = QColor("#DCE6FC")
        self._maximum_points = 16

    @property
    def points(self) -> tuple[tuple[float, float], ...]:
        return tuple(self._points)

    def setCurveColor(self, color: str | QColor) -> None:
        self._curve_color = QColor(color)
        self.update()

    def setPoints(
        self,
        points: tuple[tuple[float, float], ...] | list[tuple[float, float]],
        *,
        emit: bool = False,
    ) -> None:
        normalized = sorted(
            (
                (max(0.0, min(1.0, float(x))),
                 max(0.0, min(1.0, float(y))))
                for x, y in points
                if math.isfinite(float(x)) and math.isfinite(float(y))
            ),
            key=lambda point: point[0],
        )
        if len(normalized) > self._maximum_points:
            normalized = (
                normalized[:self._maximum_points - 1]
                + [normalized[-1]]
            )
        if len(normalized) < 2:
            normalized = [(0.0, 0.0), (1.0, 1.0)]
        first_y, last_y = normalized[0][1], normalized[-1][1]
        normalized_points = [(0.0, first_y)]
        for x, y in normalized[1:-1]:
            if x > normalized_points[-1][0] + 0.01 and x < 0.99:
                normalized_points.append((x, y))
        normalized_points.append((1.0, last_y))
        normalized = normalized_points
        if normalized == self._points:
            return
        self._points = normalized
        self.update()
        if emit:
            self.pointsChanged.emit(self.points)

    def reset(self) -> None:
        self.setPoints([(0.0, 0.0), (1.0, 1.0)], emit=True)

    def _plot_rect(self):
        return self.rect().adjusted(12, 10, -12, -10)

    def _to_widget(self, x: float, y: float) -> QPointF:
        plot = self._plot_rect()
        return QPointF(
            plot.left() + x * plot.width(),
            plot.bottom() - y * plot.height(),
        )

    def _from_widget(self, point: QPointF) -> tuple[float, float]:
        plot = self._plot_rect()
        return (
            max(0.0, min(1.0, (point.x() - plot.left()) / plot.width())),
            max(0.0, min(1.0, (plot.bottom() - point.y()) / plot.height())),
        )

    def _point_index_at(self, point: QPointF) -> int | None:
        for index, (x, y) in enumerate(self._points):
            if (self._to_widget(x, y) - point).manhattanLength() <= 18:
                return index
        return None

    def paintEvent(self, _event) -> None:
        plot = self._plot_rect()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(plot, QColor("#171A20"))

        grid_pen = QPen(QColor("#343A44"), 1)
        painter.setPen(grid_pen)
        for step in range(1, 4):
            fraction = step / 4
            x = plot.left() + fraction * plot.width()
            y = plot.top() + fraction * plot.height()
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        painter.setPen(QPen(QColor("#777F8A"), 1, Qt.PenStyle.DashLine))
        painter.drawLine(self._to_widget(0, 0), self._to_widget(1, 1))

        slopes = tone_curve_slopes(self.points)
        curve_path = QPainterPath(self._to_widget(*self._points[0]))
        for index, (left, right) in enumerate(
            zip(self._points, self._points[1:])
        ):
            width = right[0] - left[0]
            control_one = self._to_widget(
                left[0] + width / 3,
                max(0.0, min(1.0, left[1] + slopes[index] * width / 3)),
            )
            control_two = self._to_widget(
                right[0] - width / 3,
                max(
                    0.0,
                    min(1.0, right[1] - slopes[index + 1] * width / 3),
                ),
            )
            curve_path.cubicTo(
                control_one, control_two, self._to_widget(*right))
        painter.setPen(QPen(self._curve_color, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(curve_path)

        for x, y in self._points:
            point = self._to_widget(x, y)
            painter.setPen(QPen(QColor("#15171B"), 1))
            painter.setBrush(self._curve_color)
            painter.drawEllipse(point, 4, 4)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            index = self._point_index_at(event.position())
            if index is not None and index not in (0, len(self._points) - 1):
                points = list(self._points)
                points.pop(index)
                self.setPoints(points, emit=True)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        index = self._point_index_at(event.position())
        if index is None:
            x, y = self._from_widget(event.position())
            index = next(
                (i for i, (point_x, _) in enumerate(self._points)
                 if point_x > x),
                len(self._points) - 1,
            )
            left_x = self._points[index - 1][0]
            right_x = self._points[index][0]
            if (
                len(self._points) >= self._maximum_points
                or right_x - left_x <= 0.02
            ):
                return
            x = max(left_x + 0.01, min(right_x - 0.01, x))
            points = list(self._points)
            points.insert(index, (x, y))
            self._points = points
            self.pointsChanged.emit(self.points)
            self.update()
        self._active_point = index
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    def mouseMoveEvent(self, event) -> None:
        if (
            self._active_point is None
            or not event.buttons() & Qt.MouseButton.LeftButton
        ):
            return
        x, y = self._from_widget(event.position())
        index = self._active_point
        if index == 0:
            x = 0.0
        elif index == len(self._points) - 1:
            x = 1.0
        else:
            x = max(
                self._points[index - 1][0] + 0.01,
                min(self._points[index + 1][0] - 0.01, x),
            )
        points = list(self._points)
        points[index] = (x, y)
        self.setPoints(points, emit=True)
        self._active_point = index

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._active_point = None
