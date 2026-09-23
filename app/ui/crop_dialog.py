from __future__ import annotations

from pathlib import Path

from PIL import Image
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


class CropCanvas(QWidget):
    """Displays a source image with a movable, resizable normalized crop rectangle."""

    handle_radius = 8

    def __init__(self, pixmap: QPixmap, initial_rect=None, parent=None):
        super().__init__(parent)
        if pixmap.isNull():
            raise ValueError("Could not load the image for cropping.")
        self.source = pixmap
        self.scale = min(1.0, 640 / pixmap.width(), 640 / pixmap.height())
        self.setFixedSize(max(1, round(pixmap.width() * self.scale)), max(1, round(pixmap.height() * self.scale)))
        self._scaled = pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.rect_norm = self._normalise_rect(initial_rect or (0.0, 0.0, 1.0, 1.0))
        self.aspect_locked = None
        self._drag_mode = None
        self._drag_origin = None
        self._rect_origin = None
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)

    def _minimum_width(self):
        return 1.0 / max(1, self.source.width())

    def _minimum_height(self):
        return 1.0 / max(1, self.source.height())

    def _normalise_rect(self, rect):
        try:
            x, y, width, height = (float(value) for value in rect)
        except (TypeError, ValueError):
            x, y, width, height = 0.0, 0.0, 1.0, 1.0
        return self._bounded_rect(x, y, width, height)

    def _bounded_rect(self, x, y, width, height):
        min_width, min_height = self._minimum_width(), self._minimum_height()
        width = max(min_width, min(float(width), 1.0))
        height = max(min_height, min(float(height), 1.0))
        x = max(0.0, min(float(x), 1.0 - width))
        y = max(0.0, min(float(y), 1.0 - height))
        return [x, y, width, height]

    def _set_rect(self, x, y, width, height):
        self.rect_norm = self._bounded_rect(x, y, width, height)

    def _normalised_point(self, point: QPoint):
        return (
            max(0.0, min(1.0, point.x() / max(1, self.width()))),
            max(0.0, min(1.0, point.y() / max(1, self.height()))),
        )

    def _normalised_aspect(self):
        if not self.aspect_locked:
            return None
        return self.aspect_locked * self.source.height() / self.source.width()

    def to_widget_rect(self) -> QRect:
        x, y, width, height = self.rect_norm
        left, top = round(x * self.width()), round(y * self.height())
        right, bottom = round((x + width) * self.width()), round((y + height) * self.height())
        return QRect(left, top, max(1, right - left), max(1, bottom - top))

    def set_aspect_locked(self, aspect):
        self.aspect_locked = aspect
        if aspect:
            x, y, width, height = self.rect_norm
            target = self._normalised_aspect()
            if width / height > target:
                new_width = height * target
                x += (width - new_width) / 2
                width = new_width
            else:
                new_height = width / target
                y += (height - new_height) / 2
                height = new_height
            self._set_rect(x, y, width, height)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(0, 0, self._scaled)
        rect = self.to_widget_rect()
        painter.fillRect(self.rect(), QColor(0, 0, 0, 120))
        painter.drawPixmap(rect, self._scaled, rect)
        pen = QPen(QColor(108, 92, 231, 230))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect)
        painter.setBrush(QColor("#f5f8ff"))
        for point in (rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight()):
            painter.drawRect(QRect(point.x() - 3, point.y() - 3, 6, 6))

    def _hit_test(self, point: QPoint):
        rect = self.to_widget_rect()
        left = abs(point.x() - rect.left()) <= self.handle_radius
        right = abs(point.x() - rect.right()) <= self.handle_radius
        top = abs(point.y() - rect.top()) <= self.handle_radius
        bottom = abs(point.y() - rect.bottom()) <= self.handle_radius
        if left and top:
            return "resize-top-left"
        if right and top:
            return "resize-top-right"
        if left and bottom:
            return "resize-bottom-left"
        if right and bottom:
            return "resize-bottom-right"
        if left:
            return "resize-left"
        if right:
            return "resize-right"
        if top:
            return "resize-top"
        if bottom:
            return "resize-bottom"
        if rect.contains(point):
            return "move"
        return "draw"

    def _update_cursor(self, point):
        mode = self._hit_test(point)
        cursors = {
            "resize-left": Qt.SizeHorCursor,
            "resize-right": Qt.SizeHorCursor,
            "resize-top": Qt.SizeVerCursor,
            "resize-bottom": Qt.SizeVerCursor,
            "resize-top-left": Qt.SizeFDiagCursor,
            "resize-bottom-right": Qt.SizeFDiagCursor,
            "resize-top-right": Qt.SizeBDiagCursor,
            "resize-bottom-left": Qt.SizeBDiagCursor,
            "move": Qt.OpenHandCursor,
        }
        self.setCursor(cursors.get(mode, Qt.CrossCursor))

    def _rect_from_anchor(self, anchor, point):
        anchor_x, anchor_y = anchor
        point_x, point_y = point
        sign_x = 1 if point_x >= anchor_x else -1
        sign_y = 1 if point_y >= anchor_y else -1
        max_width = (1.0 - anchor_x) if sign_x > 0 else anchor_x
        max_height = (1.0 - anchor_y) if sign_y > 0 else anchor_y
        width, height = abs(point_x - anchor_x), abs(point_y - anchor_y)
        aspect = self._normalised_aspect()
        if aspect:
            if width / max(height, 0.000001) > aspect:
                height = width / aspect
            else:
                width = height * aspect
        if width > 0 and height > 0:
            factor = min(1.0, max_width / width, max_height / height)
            width *= factor
            height *= factor
        width = min(max_width, max(self._minimum_width(), width))
        height = min(max_height, max(self._minimum_height(), height))
        x = anchor_x if sign_x > 0 else anchor_x - width
        y = anchor_y if sign_y > 0 else anchor_y - height
        return self._bounded_rect(x, y, width, height)

    def _resize_aspect_locked(self, point):
        x, y, width, height = self._rect_origin
        right, bottom = x + width, y + height
        mode = self._drag_mode
        if mode in {"resize-top-left", "resize-top-right", "resize-bottom-left", "resize-bottom-right"}:
            anchor = {
                "resize-top-left": (right, bottom),
                "resize-top-right": (x, bottom),
                "resize-bottom-left": (right, y),
                "resize-bottom-right": (x, y),
            }[mode]
            self.rect_norm = self._rect_from_anchor(anchor, point)
            return
        aspect = self._normalised_aspect()
        center_x, center_y = x + width / 2, y + height / 2
        if mode in {"resize-left", "resize-right"}:
            desired_width = (right - point[0]) if mode == "resize-left" else (point[0] - x)
            max_width = min(right if mode == "resize-left" else 1.0 - x, 2 * min(center_y, 1.0 - center_y) * aspect)
            width = max(self._minimum_width(), min(max_width, desired_width))
            height = width / aspect
            self._set_rect(right - width if mode == "resize-left" else x, center_y - height / 2, width, height)
            return
        desired_height = (bottom - point[1]) if mode == "resize-top" else (point[1] - y)
        max_height = min(bottom if mode == "resize-top" else 1.0 - y, 2 * min(center_x, 1.0 - center_x) / aspect)
        height = max(self._minimum_height(), min(max_height, desired_height))
        width = height * aspect
        self._set_rect(center_x - width / 2, bottom - height if mode == "resize-top" else y, width, height)

    def _resize(self, point):
        if self.aspect_locked:
            self._resize_aspect_locked(point)
            return
        x, y, width, height = self._rect_origin
        left, top, right, bottom = x, y, x + width, y + height
        mode = self._drag_mode
        if "left" in mode:
            left = min(point[0], right - self._minimum_width())
        elif "right" in mode:
            right = max(point[0], left + self._minimum_width())
        if "top" in mode:
            top = min(point[1], bottom - self._minimum_height())
        elif "bottom" in mode:
            bottom = max(point[1], top + self._minimum_height())
        self._set_rect(left, top, right - left, bottom - top)

    def _update_drag(self, point: QPoint):
        current = self._normalised_point(point)
        if self._drag_mode == "draw":
            self.rect_norm = self._rect_from_anchor(self._drag_origin, current)
        elif self._drag_mode == "move":
            x, y, width, height = self._rect_origin
            delta_x, delta_y = current[0] - self._drag_origin[0], current[1] - self._drag_origin[1]
            self._set_rect(x + delta_x, y + delta_y, width, height)
        else:
            self._resize(current)
        self.update()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() != Qt.LeftButton:
            event.ignore()
            return
        point = event.position().toPoint()
        self._drag_mode = self._hit_test(point)
        self._drag_origin = self._normalised_point(point)
        self._rect_origin = list(self.rect_norm)
        if self._drag_mode == "draw":
            self._set_rect(self._drag_origin[0], self._drag_origin[1], self._minimum_width(), self._minimum_height())
        self.setCursor(Qt.ClosedHandCursor if self._drag_mode == "move" else self.cursor())
        self.update()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        point = event.position().toPoint()
        if self._drag_mode is not None and event.buttons() & Qt.LeftButton:
            self._update_drag(point)
            event.accept()
            return
        self._update_cursor(point)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton and self._drag_mode is not None:
            self._update_drag(event.position().toPoint())
            self._drag_mode = None
            self._drag_origin = None
            self._rect_origin = None
            self._update_cursor(event.position().toPoint())
            event.accept()
            return
        event.ignore()


class CropDialog(QDialog):
    def __init__(self, image_path: str, initial_rect=None, parent=None, remaining_count=0, lock_916=False):
        super().__init__(parent)
        self.setWindowTitle("Crop Panel")
        self.setWindowIcon(parent.windowIcon() if parent is not None else QApplication.instance().windowIcon())
        self.result_rect = None
        self.apply_to_remaining = False
        self.skip_remaining = False
        pixmap = QPixmap(image_path)
        self.canvas = CropCanvas(pixmap, initial_rect, self)
        layout = QVBoxLayout(self)
        layout.addWidget(self.canvas)
        layout.addWidget(QLabel("Drag inside the crop to move it. Drag an edge or corner to resize it."))
        self.lock_aspect = QCheckBox("Lock to 9:16")
        self.lock_aspect.toggled.connect(lambda checked: self.canvas.set_aspect_locked(9 / 16 if checked else None))
        self.lock_aspect.setChecked(lock_916)
        layout.addWidget(self.lock_aspect)
        row = QHBoxLayout()
        reset = QPushButton("Reset")
        reset.clicked.connect(self.reset_crop)
        skip = QPushButton("Skip (use uncropped)")
        skip.clicked.connect(self.reject)
        skip_all = QPushButton("Skip All")
        skip_all.clicked.connect(self.skip_all)
        apply_button = QPushButton("Apply")
        apply_button.setObjectName("primaryButton")
        apply_button.clicked.connect(self.accept)
        row.addWidget(reset)
        row.addStretch()
        row.addWidget(skip)
        row.addWidget(skip_all)
        row.addWidget(apply_button)
        layout.addLayout(row)
        if remaining_count > 0:
            self.apply_all = QCheckBox(f"Apply this same crop to the remaining {remaining_count} image(s)")
            layout.addWidget(self.apply_all)
        else:
            self.apply_all = None

    def reset_crop(self):
        self.canvas._set_rect(0.0, 0.0, 1.0, 1.0)
        self.canvas.update()

    def accept(self):
        self.result_rect = tuple(self.canvas.rect_norm)
        if self.apply_all is not None:
            self.apply_to_remaining = self.apply_all.isChecked()
        super().accept()

    def skip_all(self):
        self.skip_remaining = True
        self.reject()


def lock_crop_rect(source_path: str, rect_norm, aspect=9 / 16):
    """Keep a normalized crop centered while fitting it to an image-space aspect ratio."""
    with Image.open(source_path) as image:
        source_width, source_height = image.size
    x, y, width, height = (float(value) for value in rect_norm)
    center_x, center_y = x + width / 2, y + height / 2
    target_width = max(1 / source_width, min(1.0, height * aspect * source_height / source_width))
    target_height = target_width * source_width / (aspect * source_height)
    if target_height > 1.0:
        target_height = 1.0
        target_width = target_height * aspect * source_height / source_width
    left = max(0.0, min(center_x - target_width / 2, 1.0 - target_width))
    top = max(0.0, min(center_y - target_height / 2, 1.0 - target_height))
    return left, top, target_width, target_height


def apply_crop(source_path: str, rect_norm, dest_path: str) -> str:
    """Crop a source image by a normalized rectangle and save a PNG cache image."""
    destination = Path(dest_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source_path) as image:
        width, height = image.size
        x, y, crop_width, crop_height = (float(value) for value in rect_norm)
        left = max(0, min(width - 1, round(x * width)))
        top = max(0, min(height - 1, round(y * height)))
        right = max(left + 1, min(width, round((x + crop_width) * width)))
        bottom = max(top + 1, min(height, round((y + crop_height) * height)))
        image.crop((left, top, right, bottom)).save(destination, "PNG")
    return str(destination)
