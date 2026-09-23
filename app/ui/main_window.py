from __future__ import annotations
import copy, logging, os, sys, tempfile, threading
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import QEvent, QObject, Qt, QRectF, QSize, QElapsedTimer, QThread, QUrl, Signal, QTimer, QSettings
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent, QWheelEvent
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap, QUndoCommand, QUndoStack
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QApplication, QColorDialog, QDialog, QFileDialog, QFontComboBox, QFormLayout, QFrame, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QComboBox, QProgressBar, QPlainTextEdit, QScrollArea, QSlider, QSplitter, QTextEdit, QVBoxLayout, QWidget, QSizePolicy, QDoubleSpinBox, QSpinBox, QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView, QToolButton, QStackedWidget, QStyledItemDelegate)
from ..core import IMAGE_EXTENSIONS, assign_effects, assign_timings, group_captions, load_project, missing_sources, parse_elevenlabs_with_segments, safe_rename, save_project, set_effect
from ..logging_setup import configure_logging
from ..models import CaptionSettings, EFFECTS, Panel, Project
from ..renderer import RenderError, background_music_path as resolve_background_music_path, panel_render_duration, render
from .crop_dialog import CropDialog, apply_crop, lock_crop_rect

def panel_ids(widget):
    return [widget.item(index).data(Qt.UserRole) for index in range(widget.count())]

POPUP_VIEW_STYLE = """
QAbstractItemView { background: #0e1015; color: #f5f8ff; selection-background-color: #6c5ce7; selection-color: #ffffff; alternate-background-color: #101216; border: 1px solid #1c2028; outline: 0; padding: 4px; }
QAbstractItemView::item { color: #f5f8ff; min-height: 28px; padding: 4px 9px; }
QAbstractItemView::item:selected { background: #6c5ce7; color: #ffffff; }
QAbstractItemView::item:hover { background: #24203f; color: #ffffff; }
QAbstractItemView::item:disabled { background: #0a0b0f; color: #b6c2d3; }
"""

def configure_dark_palette(app):
    palette=QPalette(); colors={QPalette.ColorRole.Text:"#e8edf5",QPalette.ColorRole.WindowText:"#e8edf5",QPalette.ColorRole.ButtonText:"#e8edf5",QPalette.ColorRole.Base:"#0a0b0f",QPalette.ColorRole.AlternateBase:"#101216",QPalette.ColorRole.Window:"#08090c",QPalette.ColorRole.Button:"#14171e",QPalette.ColorRole.Highlight:"#6c5ce7",QPalette.ColorRole.HighlightedText:"#ffffff",QPalette.ColorRole.ToolTipBase:"#0e1015",QPalette.ColorRole.ToolTipText:"#f5f8ff",QPalette.ColorRole.PlaceholderText:"#aebbd0"}
    for role,color in colors.items(): palette.setColor(role,QColor(color))
    for role in (QPalette.ColorRole.Text,QPalette.ColorRole.WindowText,QPalette.ColorRole.ButtonText,QPalette.ColorRole.ToolTipText,QPalette.ColorRole.PlaceholderText): palette.setColor(QPalette.ColorGroup.Disabled,role,QColor("#b6c2d3"))
    app.setPalette(palette)

def asset_root() -> Path:
    if getattr(sys,"frozen",False): return Path(getattr(sys,"_MEIPASS",Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[2]

def app_logo() -> QIcon:
    return QIcon(str(asset_root() / "assets" / "icons" / "manhwa_studio.svg"))

def apply_elevation(widget, blur=28, y_offset=6, alpha=160):
    shadow=QGraphicsDropShadowEffect(widget); shadow.setBlurRadius(blur); shadow.setOffset(0,y_offset); shadow.setColor(QColor(0,0,0,alpha)); widget.setGraphicsEffect(shadow)

def apply_preview_glow(widget):
    glow=QGraphicsDropShadowEffect(widget); glow.setBlurRadius(40); glow.setOffset(0,0); glow.setColor(QColor(108,92,231,90)); widget.setGraphicsEffect(glow)

class SettingsWheelGuard(QObject):
    def __init__(self, scroll_area): super().__init__(scroll_area); self.scroll_area=scroll_area
    def eventFilter(self, watched, event):
        if event.type() != QEvent.Type.Wheel or watched.hasFocus(): return False
        delta=event.angleDelta().y() or event.pixelDelta().y()
        if delta:
            bar=self.scroll_area.verticalScrollBar(); step=max(bar.singleStep()*3,24)
            bar.setValue(bar.value()-int(delta/120*step))
        event.accept(); return True

class Worker(QThread):
    done=Signal(object); failed=Signal(str); progress=Signal(int)
    def __init__(self, fn): super().__init__(); self.fn=fn
    def run(self):
        try: self.done.emit(self.fn(self.progress.emit))
        except Exception as e: self.failed.emit(str(e))

class ThumbnailWorker(QThread):
    thumbnail = Signal(int, str, QPixmap)
    def __init__(self, panels, generation): super().__init__(); self.generation = generation; self.panels = [(p.id, p.render_path) for p in panels]
    def run(self):
        for panel_id, path in self.panels:
            pix = QPixmap(path)
            if not pix.isNull(): self.thumbnail.emit(self.generation, panel_id, pix.scaled(120,170,Qt.KeepAspectRatio,Qt.SmoothTransformation))

class MotionPreview(QLabel):
    """Lightweight in-app preview using the same subtle motion rules as rendering."""
    def __init__(self):
        super().__init__("Select panel"); self.setAlignment(Qt.AlignCenter)
        self.source = QPixmap(); self.effect = "zoom_in"; self.anchor = "center"; self.duration = 3.0; self.progress = 0.0; self.elapsed = QElapsedTimer()
        self.clock = QTimer(self); self.clock.setInterval(16); self.clock.timeout.connect(self.advance)
    def set_panel(self, pixmap, effect, anchor, duration):
        self.clock.stop(); self.source,self.effect,self.anchor,self.duration = pixmap,effect,anchor,max(.2,duration); self.progress=0.0; self.update()
    def play(self):
        if self.source.isNull(): return
        self.progress=0.0; self.elapsed.start(); self.clock.start(); self.update()
    def advance(self):
        self.progress = self.elapsed.elapsed() / (self.duration * 1000)
        if self.progress >= 1: self.progress=1; self.clock.stop()
        self.update()
    def paintEvent(self, event):
        painter=QPainter(self); painter.fillRect(self.rect(), self.palette().window())
        if self.source.isNull(): return super().paintEvent(event)
        w,h=self.width(),self.height(); cover=max(w/self.source.width(), h/self.source.height()); eased=self.progress*self.progress*(3-2*self.progress)
        zoom=1 + .08*self.progress if self.effect == "zoom_in" else 1.15; dw,dh=self.source.width()*cover*zoom,self.source.height()*cover*zoom
        x,y=(w-dw)/2,(h-dh)/2
        if self.effect == "slide_left": x=-(dw-w)*(1-eased)
        elif self.effect == "slide_right": x=-(dw-w)*eased
        elif self.effect == "slide_up": y=-(dh-h)*(1-eased)
        elif self.effect == "slide_down": y=-(dh-h)*eased
        elif self.effect == "zoom_in":
            if self.anchor == "top": y=0
            elif self.anchor == "bottom": y=h-dh
            elif self.anchor == "left": x=0
            elif self.anchor == "right": x=w-dw
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(QRectF(x,y,dw,dh), self.source, QRectF(0,0,self.source.width(),self.source.height()))

class AspectPreviewHost(QFrame):
    """Centers one preview widget while maintaining a 9:16 viewport."""
    def __init__(self, preview):
        super().__init__(); self.preview = preview; self.preview.setParent(self); self.setObjectName("previewHost")
        self.preview.setMinimumSize(1, 1); self.preview.setMaximumSize(16_777_215, 16_777_215); self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    def resizeEvent(self, event):
        available_w, available_h = max(1, self.width() - 12), max(1, self.height() - 12)
        width = min(available_w, available_h * 9 / 16); height = width * 16 / 9
        self.preview.setGeometry(round((self.width() - width) / 2), round((self.height() - height) / 2), round(width), round(height))
        super().resizeEvent(event)

class PanelGridHost(QFrame):
    """Keeps the organizer list active while showing its empty-state message."""
    def __init__(self, panels):
        super().__init__(); self.setObjectName("panelGridHost"); self.panels=panels; self.panels.setParent(self)
        self.empty_state=QLabel("Drop panel images here or click Add Panels to get started.",self); self.empty_state.setObjectName("panelEmptyState"); self.empty_state.setAlignment(Qt.AlignCenter); self.empty_state.setWordWrap(True); self.empty_state.setAttribute(Qt.WA_TransparentForMouseEvents,True); self.empty_state.show()
    def set_empty_state(self, empty): self.empty_state.setVisible(empty)
    def resizeEvent(self,event):
        self.panels.setGeometry(self.rect()); self.empty_state.setGeometry(self.rect().adjusted(28,28,-28,-28)); super().resizeEvent(event)

class TimelinePreview(QLabel):
    """Low-resolution live preview of the complete project timeline."""
    def __init__(self):
        super().__init__("Import panels to preview the timeline"); self.setAlignment(Qt.AlignCenter)
        self.project = None; self.time = 0.0; self.timeline = []; self.pixmaps = {}
    def set_project(self, project):
        if project is not self.project: self.pixmaps.clear()
        self.project = project; self.timeline = []; cursor = 0.0
        for index, panel in enumerate(project.panels):
            duration = panel_render_duration(project, index)
            self.timeline.append((cursor, cursor + duration, panel)); cursor += duration
        self.time = min(self.time, cursor); self.update()
    def duration(self): return self.timeline[-1][1] if self.timeline else 0.0
    def set_time(self, value): self.time = max(0.0, min(value, self.duration())); self.update()
    def active_panel(self):
        for start, end, panel in self.timeline:
            if start <= self.time < end or (self.time == self.duration() and end == self.duration()):
                return start, end, panel
        return None
    def pixmap_for(self, path):
        if path not in self.pixmaps:
            pixmap = QPixmap(path)
            if not pixmap.isNull() and pixmap.height() > 432:
                pixmap = pixmap.scaledToHeight(432, Qt.SmoothTransformation)
            self.pixmaps[path] = pixmap
        return self.pixmaps[path]
    def color(self, bgr):
        value = (bgr or "FFFFFF").replace("#", "").zfill(6)[-6:]
        return QColor(int(value[4:6], 16), int(value[2:4], 16), int(value[0:2], 16))
    def caption_at(self):
        if not self.project: return None
        offset = self.project.caption_settings.timing_offset
        for caption in self.project.captions:
            start,end=max(0.0,caption.start + offset),max(0.0,caption.end + offset)
            if end > start and start <= self.time <= end: return caption
        return None
    def draw_caption_word(self, painter, font, text, x, baseline, color, outline, outline_color, shadow, shadow_color):
        path = QPainterPath(); path.addText(x, baseline, font, text)
        if shadow:
            shadow_path = QPainterPath(); shadow_path.addText(x + shadow, baseline + shadow, font, text)
            if outline: painter.strokePath(shadow_path, QPen(shadow_color, outline * 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.fillPath(shadow_path, shadow_color)
        if outline: painter.strokePath(path, QPen(outline_color, outline * 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.fillPath(path, color)
    def draw_caption(self, painter):
        caption = self.caption_at()
        if not caption: return
        settings = self.project.caption_settings; scale = self.height() / 1920
        font = QFont(settings.font); font.setPixelSize(max(9, round(settings.font_size * scale)))
        font.setWeight({"Regular": QFont.Weight.Normal, "Medium": QFont.Weight.Medium, "Bold": QFont.Weight.Bold, "Extra Bold": QFont.Weight.ExtraBold}.get(settings.font_weight, QFont.Weight.Bold))
        metrics = QFontMetrics(font)
        words = [word.text.upper() if settings.all_caps else word.text for word in caption.words]
        widths = [metrics.horizontalAdvance(word) for word in words]; gap = metrics.horizontalAdvance(" ")
        total_width = sum(widths) + gap * max(0, len(words) - 1); margin = round(settings.horizontal_margin * scale)
        if settings.horizontal_alignment == "left": x = margin
        elif settings.horizontal_alignment == "right": x = self.width() - total_width - margin
        else: x = (self.width() - total_width) / 2
        baseline = max(metrics.ascent(), min(self.height() - metrics.descent(), round(settings.vertical_position * scale)))
        outline = max(0, settings.outline * scale); shadow = max(0, settings.shadow * scale)
        normal, highlight = self.color(settings.normal_color), self.color(settings.highlight_color)
        outline_color, shadow_color = self.color(settings.outline_color), self.color(settings.shadow_color)
        offset = settings.timing_offset
        for word, width, source in zip(words, widths, caption.words):
            active = settings.highlight_mode == "line" or source.start + offset <= self.time <= source.end + offset
            self.draw_caption_word(painter, font, word, x, baseline, highlight if active else normal, outline, outline_color, shadow, shadow_color)
            x += width + gap
    def paintEvent(self, event):
        painter = QPainter(self); painter.fillRect(self.rect(), QColor("black")); active = self.active_panel()
        if not active:
            painter.end(); return super().paintEvent(event)
        start, end, panel = active; pixmap = self.pixmap_for(panel.render_path)
        if pixmap.isNull(): painter.end(); return super().paintEvent(event)
        progress = (self.time - start) / max(.001, end - start); eased = progress * progress * (3 - 2 * progress)
        width, height = self.width(), self.height(); cover = max(width / pixmap.width(), height / pixmap.height())
        zoom = 1 + .08 * progress if panel.effect == "zoom_in" else 1.15; draw_w, draw_h = pixmap.width() * cover * zoom, pixmap.height() * cover * zoom
        x, y = (width - draw_w) / 2, (height - draw_h) / 2
        if panel.effect == "slide_left": x = -(draw_w - width) * (1 - eased)
        elif panel.effect == "slide_right": x = -(draw_w - width) * eased
        elif panel.effect == "slide_up": y = -(draw_h - height) * (1 - eased)
        elif panel.effect == "slide_down": y = -(draw_h - height) * eased
        elif panel.effect == "zoom_in":
            if panel.zoom_anchor == "top": y = 0
            elif panel.zoom_anchor == "bottom": y = height - draw_h
            elif panel.zoom_anchor == "left": x = 0
            elif panel.zoom_anchor == "right": x = width - draw_w
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(QRectF(x, y, draw_w, draw_h), pixmap, QRectF(0, 0, pixmap.width(), pixmap.height())); self.draw_caption(painter)

class PanelListWidget(QListWidget):
    """Icon list with dependable internal drops and wheel scrolling while dragging."""
    order_dropped = Signal(list, list)
    def __init__(self):
        super().__init__(); self.dragging = False; self._dragged_ids = []; self._dragged_items = []; self._drop_slot = None; self._drop_committed = False; self._order_before_drag = []
        self.setDragEnabled(True); self.setAcceptDrops(True); self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True); self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction); self.setDragDropOverwriteMode(False); self.setAutoScroll(True); self.setAutoScrollMargin(30)
    def resizeEvent(self,event):
        super().resizeEvent(event); self.sync_item_widths()
    def sync_item_widths(self):
        if self.viewMode() != QListWidget.ListMode: return
        width=self.viewport().width()
        for row in range(self.count()):
            item=self.item(row); hint=item.sizeHint()
            if hint.width() != width: item.setSizeHint(QSize(width,hint.height()))
    def startDrag(self, actions):
        self._dragged_ids = [item.data(Qt.UserRole) for item in self.selectedItems()]
        self._order_before_drag = panel_ids(self); self._dragged_items = []; self._drop_slot = None; self._drop_committed = False
        self.dragging = True; super().startDrag(actions)
        # Dropping outside the organizer cancels the temporary open slot.
        if not self._drop_committed: self.restore_original_order()
        self.dragging = False
    def dragEnterEvent(self, event: QDragEnterEvent):
        self.dragging = True; event.acceptProposedAction()
    def dragMoveEvent(self, event):
        self.begin_drop_slot(); self.move_drop_slot(event.position().toPoint())
        # Qt's own edge auto-scroll can vary by platform; keep a fallback so
        # a dragged item can always reach panels above/below the viewport.
        y = event.position().y(); bar = self.verticalScrollBar()
        if y < self.autoScrollMargin(): bar.setValue(bar.value() - max(bar.singleStep(), 1))
        elif y > self.viewport().height() - self.autoScrollMargin(): bar.setValue(bar.value() + max(bar.singleStep(), 1))
        event.acceptProposedAction()
    def dragLeaveEvent(self, event: QDragLeaveEvent):
        # Keep drag state while moving between child widgets; startDrag() will
        # restore the original order if the drag is ultimately cancelled.
        event.accept()
    def dropEvent(self, event: QDropEvent):
        before = getattr(self, "_order_before_drag", panel_ids(self))
        self.begin_drop_slot(); self.move_drop_slot(event.position().toPoint())
        slot_row = self.row(self._drop_slot); self.takeItem(slot_row)
        for offset, (_, item) in enumerate(self._dragged_items):
            self.insertItem(slot_row + offset, item); item.setSelected(True)
        if self._dragged_items: self.setCurrentItem(self._dragged_items[0][1])
        after = panel_ids(self); self._drop_committed = True
        event.acceptProposedAction(); self.dragging = False
        if before != after: self.order_dropped.emit(before, after)
    def begin_drop_slot(self):
        if self._drop_slot is not None: return
        selected = [(item.data(Qt.UserRole), item) for item in self.selectedItems() if item.data(Qt.UserRole) in self._dragged_ids]
        if not selected: return
        rows = sorted((self.row(item) for _, item in selected), reverse=True)
        self.blockSignals(True)
        for row in rows: self.takeItem(row)
        self._dragged_items = selected
        slot = QListWidgetItem("↓  DROP HERE  ↓"); slot.setData(Qt.UserRole, None)
        slot.setSizeHint(selected[0][1].sizeHint()); self.addItem(slot); self._drop_slot = slot
        self.blockSignals(False)
    def move_drop_slot(self, position):
        if self._drop_slot is None: return
        target_item = self.itemAt(position); target = self.row(target_item) if target_item else self.count()
        if target_item is self._drop_slot: return
        if target_item and self.dropIndicatorPosition() == QAbstractItemView.BelowItem: target += 1
        current_slot = self.row(self._drop_slot); slot = self.takeItem(current_slot)
        if target > current_slot: target -= 1
        self.insertItem(max(0, target), slot)
    def restore_original_order(self):
        if self._drop_slot is None: return
        items = {self.item(index).data(Qt.UserRole): self.item(index) for index in range(self.count()) if self.item(index) is not self._drop_slot}
        items.update(dict(self._dragged_items))
        self.blockSignals(True)
        while self.count(): self.takeItem(0)
        for panel_id in self._order_before_drag: self.addItem(items[panel_id])
        self.blockSignals(False); self._drop_slot = None
    def wheelEvent(self, event: QWheelEvent):
        if self.dragging:
            delta = event.angleDelta().y() or event.pixelDelta().y()
            if delta:
                bar = self.verticalScrollBar(); step = max(bar.singleStep(), 3)
                bar.setValue(bar.value() - int(delta / 120 * step)); event.accept(); return
        super().wheelEvent(event)

class PlayingHighlightDelegate(QStyledItemDelegate):
    def __init__(self,parent=None): super().__init__(parent); self.playing_id=None
    def paint(self,painter,option,index):
        super().paint(painter,option,index)
        if self.playing_id is None or index.data(Qt.UserRole) != self.playing_id: return
        painter.save(); pen=QPen(QColor(108,92,231,230)); pen.setWidth(3); painter.setPen(pen); painter.setBrush(Qt.NoBrush); painter.setRenderHint(QPainter.Antialiasing); painter.drawRoundedRect(option.rect.adjusted(3,3,-3,-3),8,8); painter.restore()

class ReorderCommand(QUndoCommand):
    def __init__(self, win, before, after): super().__init__("Reorder panels"); self.win,self.before,self.after=win,before,after
    def undo(self): self.win.project.panels=self.before[:]; self.win.refresh()
    def redo(self): self.win.project.panels=self.after[:]; self.win.refresh()

class PanelStateCommand(QUndoCommand):
    def __init__(self, win, before, after, name): super().__init__(name); self.win,self.before,self.after=win,copy.deepcopy(before),copy.deepcopy(after)
    def undo(self): self.win.project.panels=copy.deepcopy(self.before); self.win.refresh()
    def redo(self): self.win.project.panels=copy.deepcopy(self.after); self.win.refresh()

class CaptionSettingsCommand(QUndoCommand):
    def __init__(self, win, before_settings, before_captions, after_settings, after_captions, merge_key):
        super().__init__("Adjust caption style")
        self.win = win; self.before_settings = copy.deepcopy(before_settings); self.before_captions = copy.deepcopy(before_captions)
        self.after_settings = copy.deepcopy(after_settings); self.after_captions = copy.deepcopy(after_captions); self.merge_key = merge_key
    def id(self): return 4101
    def mergeWith(self, other):
        if not isinstance(other, CaptionSettingsCommand) or self.merge_key != other.merge_key: return False
        self.after_settings, self.after_captions = copy.deepcopy(other.after_settings), copy.deepcopy(other.after_captions)
        return True
    def apply(self, settings, captions):
        self.win.project.caption_settings = copy.deepcopy(settings); self.win.project.captions = copy.deepcopy(captions)
        self.win.sync_caption_controls(); self.win.timeline_preview.update()
    def undo(self): self.apply(self.before_settings, self.before_captions)
    def redo(self): self.apply(self.after_settings, self.after_captions)

class MainWindow(QMainWindow):
    TEXT_INPUT_TYPES = (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)

    def __init__(self):
        super().__init__(); QApplication.instance().setWindowIcon(app_logo()); self.setWindowIcon(app_logo()); self.project=Project(); self.project_path=""; self.undo=QUndoStack(self); self.settings=QSettings("ManhwaAutomation", "ManhwaEditor"); self.projects_directory(); self.project.name=self.next_project_path().stem; self._caption_pending={}; self._caption_change_session=0; self._updating_caption_controls=False; self._thumbnail_generation=0; self._thumbnail_workers=[]; self.caption_debounce=QTimer(self); self.caption_debounce.setSingleShot(True); self.caption_debounce.setInterval(120); self.caption_debounce.timeout.connect(self.commit_caption_changes); self.resize(1300,850); self.setMinimumSize(940,650); self._build(); QApplication.instance().installEventFilter(self); self.set_timeline_project(); self.update_window_title(); self.setAcceptDrops(True)
        self.autosave=QTimer(self); self.autosave.setInterval(60_000); self.autosave.timeout.connect(self.autosave_project); self.autosave.start()
    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Space and not event.isAutoRepeat():
            focus=QApplication.focusWidget()
            is_typing=isinstance(focus,self.TEXT_INPUT_TYPES) or (isinstance(focus,QComboBox) and focus.isEditable())
            if not is_typing:
                self.toggle_timeline()
                return True
        return super().eventFilter(obj,event)
    def _build(self):
        bar=self.menuBar(); file=bar.addMenu("Project")
        for label,method,shortcut in [("New",self.new,"Ctrl+N"),("Open",self.open,"Ctrl+O"),("Save",self.save,"Ctrl+S"),("Save As",self.save_as,"Ctrl+Shift+S"),("Caption Settings",self.caption_settings,"")]:
            action=QAction(label,self); action.triggered.connect(method)
            if shortcut: action.setShortcut(shortcut)
            file.addAction(action)
        edit=bar.addMenu("Edit"); undo_action=self.undo.createUndoAction(self,"Undo"); redo_action=self.undo.createRedoAction(self,"Redo"); undo_action.setShortcut("Ctrl+Z"); redo_action.setShortcut("Ctrl+Y"); edit.addAction(undo_action); edit.addAction(redo_action)
        root=QWidget(); root.setObjectName("appRoot"); self.setCentralWidget(root); layout=QVBoxLayout(root); layout.setContentsMargins(18,14,18,16); layout.setSpacing(12)
        self.main_splitter=QSplitter(Qt.Horizontal); self.main_splitter.setChildrenCollapsible(False); self.main_splitter.setMinimumHeight(0); layout.addWidget(self.main_splitter,1)
        organizer_card=self._build_panel_organizer(); preview_card=self._build_preview_card(); settings_card=self._build_settings_card(); preview_card.setProperty("previewStage",True)
        self.main_splitter.addWidget(organizer_card); self.main_splitter.addWidget(preview_card); self.main_splitter.addWidget(settings_card)
        self.main_splitter.setStretchFactor(0,30); self.main_splitter.setStretchFactor(1,23); self.main_splitter.setStretchFactor(2,47); self.main_splitter.setSizes([310,240,450])
        apply_elevation(organizer_card); apply_preview_glow(preview_card); apply_elevation(settings_card)
        footer=QFrame(); footer.setObjectName("footerCard"); imports=QGridLayout(footer); imports.setContentsMargins(8,8,8,8); imports.setHorizontalSpacing(8); imports.setVerticalSpacing(6)
        footer_actions=[("Import Voice",self.import_audio,"Choose the narration audio used for timing and timeline playback."),("Import ElevenLabs JSON",self.import_transcript,"Import a timestamped ElevenLabs transcript for timings and captions."),("Recalculate Timing",self.recalculate_timing,"Recalculate panel timings from the imported transcript."),("Full Preview (240p)",lambda:self.start_render(True),"Render a low-resolution preview. Requires imported panels."),("Final Render",lambda:self.start_render(False),"Render the final video. Requires panels and a matched transcript.")]
        for column,(text,method,tooltip) in enumerate(footer_actions):
            button=QPushButton(text); button.clicked.connect(method); button.setToolTip(tooltip); button.setObjectName("primaryButton" if text == "Final Render" else ""); imports.addWidget(button,0,column)
        self.cancel=QPushButton("Cancel Render"); self.cancel.clicked.connect(self.cancel_render); self.cancel.setToolTip("Cancel the active render."); self.cancel.setObjectName("dangerButton"); self.cancel.setEnabled(False); imports.addWidget(self.cancel,0,5); layout.addWidget(footer)
        status_card=QFrame(); status_card.setObjectName("statusCard"); status_layout=QHBoxLayout(status_card); self.status=QLabel("Ready - import panels, voice, and transcript."); status_layout.addWidget(self.status); self.progress=QProgressBar(); self.progress.setFixedWidth(260); status_layout.addStretch(); status_layout.addWidget(self.progress); layout.addWidget(status_card)
        self.update_panel_counter(); self.apply_popup_styles(); self.apply_settings_wheel_guard()
    def _build_panel_organizer(self):
        card=QFrame(); card.setObjectName("panelCard"); layout=QVBoxLayout(card); layout.setContentsMargins(14,14,14,14); layout.setSpacing(8)
        header=QHBoxLayout(); header.addWidget(self.section_label("PANEL ORGANIZER")); header.addStretch(); layout.addLayout(header)
        button_grid=QGridLayout(); button_grid.setHorizontalSpacing(6); button_grid.setVerticalSpacing(6)
        actions=[("Add Panels",self.import_panels,"Import image panels into the organizer.","primaryButton"),("Remove",self.remove_selected,"Remove the selected panels from this project.","dangerButton"),("Duplicate",self.duplicate_selected,"Duplicate the selected panel after its current position.",""),("Reverse Order",self.reverse_order,"Reverse the order of all panels.","warningButton"),("Reset Order",self.reset_order,"Sort panels back into their original filename order.","warningButton"),("Apply/Rename",self.rename_panels,"Rename panel files to match their current sequence.","")]
        for index,(text,method,tooltip,name) in enumerate(actions):
            button=QPushButton(text); button.clicked.connect(method); button.setToolTip(tooltip); button.setObjectName(name); button_grid.addWidget(button,index//2,index%2)
        layout.addLayout(button_grid)
        self.panel_counter=QLabel("Panels 0 / Segments -"); self.panel_counter.setObjectName("matchCounter"); counter_row=QHBoxLayout(); counter_row.addStretch(); counter_row.addWidget(self.panel_counter); layout.addLayout(counter_row)
        body=QHBoxLayout(); body.setSpacing(9)
        sidebar=QFrame(); sidebar.setObjectName("orderSidebar"); sidebar_layout=QVBoxLayout(sidebar); sidebar_layout.setContentsMargins(8,10,8,10); sidebar_layout.addWidget(self.section_label("PANEL ORDER"))
        self.panel_sidebar=PanelListWidget(); self.panel_sidebar.setViewMode(QListWidget.ListMode); self.panel_sidebar.setIconSize(QSize(28,40)); self.panel_sidebar.setSelectionMode(QListWidget.ExtendedSelection); self.panel_sidebar.setVerticalScrollMode(QListWidget.ScrollPerPixel); self.panel_sidebar.setMinimumWidth(152); self.panel_sidebar.setMaximumWidth(180); self.panel_sidebar.order_dropped.connect(self.record_reorder); self.panel_sidebar.itemSelectionChanged.connect(self.select_from_sidebar); sidebar_layout.addWidget(self.panel_sidebar,1); body.addWidget(sidebar,0)
        self.panels=PanelListWidget(); self.panels.setViewMode(QListWidget.IconMode); self.panels.setIconSize(QSize(120,170)); self.panels.setGridSize(QSize(150,208)); self.panels.setSpacing(6); self.panels.setUniformItemSizes(True); self.panels.setWordWrap(True); self.panels.setWrapping(True); self.panels.setTextElideMode(Qt.ElideMiddle); self.panels.setResizeMode(QListWidget.Adjust); self.panels.setVerticalScrollMode(QListWidget.ScrollPerPixel); self.panels.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.panels.setSelectionMode(QListWidget.ExtendedSelection); self.panels.setContextMenuPolicy(Qt.CustomContextMenu); self.panels.customContextMenuRequested.connect(self.show_panel_context_menu); self.panels.order_dropped.connect(self.record_reorder); self.panels.itemSelectionChanged.connect(self.show_selected); self.playing_delegate=PlayingHighlightDelegate(self.panels); self.panels.setItemDelegate(self.playing_delegate)
        self.panel_grid_host=PanelGridHost(self.panels); body.addWidget(self.panel_grid_host,1); layout.addLayout(body,1)
        return card
    def _build_preview_card(self):
        card=QFrame(); card.setObjectName("panelCard"); layout=QVBoxLayout(card); layout.setContentsMargins(12,14,12,14); layout.setSpacing(8)
        header=QHBoxLayout(); header.addWidget(self.section_label("PREVIEW")); header.addStretch()
        self.timeline_toggle=QToolButton(); self.timeline_toggle.setObjectName("previewToggle"); self.timeline_toggle.setText("Timeline"); self.timeline_toggle.setCheckable(True); self.timeline_toggle.setAutoExclusive(True); self.timeline_toggle.setToolTip("Show the full timeline preview.")
        self.panel_toggle=QToolButton(); self.panel_toggle.setObjectName("previewToggle"); self.panel_toggle.setText("Panel"); self.panel_toggle.setCheckable(True); self.panel_toggle.setAutoExclusive(True); self.panel_toggle.setToolTip("Show the selected panel preview.")
        self.timeline_toggle.clicked.connect(lambda:self.set_preview_mode(0)); self.panel_toggle.clicked.connect(lambda:self.set_preview_mode(1)); header.addWidget(self.timeline_toggle); header.addWidget(self.panel_toggle); layout.addLayout(header)
        self.preview_stack=QStackedWidget(); self.preview_stack.addWidget(self._build_timeline_card()); self.preview_stack.addWidget(self._build_panel_preview_page()); layout.addWidget(self.preview_stack,1)
        self.set_preview_mode(1 if self.settings.value("preview_mode","timeline") == "panel" else 0)
        return card
    def _build_timeline_card(self):
        page=QFrame(); page.setObjectName("previewModePage"); layout=QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(8)
        self.timeline_preview=TimelinePreview(); self.timeline_preview.setObjectName("timelinePreview"); self.timeline=self.timeline_preview; layout.addWidget(AspectPreviewHost(self.timeline_preview),1)
        self.timeline_play=QPushButton("Play"); self.timeline_play.clicked.connect(self.toggle_timeline); self.timeline_time=QLabel("0:00 / 0:00"); self.timeline_time.setAlignment(Qt.AlignCenter); self.timeline_slider=QSlider(Qt.Horizontal); self.timeline_slider.setRange(0,0); self.timeline_slider.sliderMoved.connect(self.scrub_timeline); layout.addWidget(self.timeline_play); layout.addWidget(self.timeline_time); layout.addWidget(self.timeline_slider)
        self.timeline_audio=QAudioOutput(self); self.timeline_player=QMediaPlayer(self); self.timeline_player.setAudioOutput(self.timeline_audio); self.timeline_bgm_audio=QAudioOutput(self); self.timeline_bgm_player=QMediaPlayer(self); self.timeline_bgm_player.setAudioOutput(self.timeline_bgm_audio); self.timeline_bgm_player.setLoops(QMediaPlayer.Loops.Infinite); self.timeline_clock=QElapsedTimer(); self.timeline_timer=QTimer(self); self.timeline_timer.setInterval(16); self.timeline_timer.timeout.connect(self.update_timeline); self.timeline_playing=False; self.timeline_time_value=0.0; self.timeline_has_audio=False; self.timeline_has_bgm=False
        return page
    def _build_panel_preview_page(self):
        page=QFrame(); page.setObjectName("previewModePage"); layout=QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(8)
        self.preview=MotionPreview(); self.preview.setObjectName("previewFrame"); layout.addWidget(AspectPreviewHost(self.preview),1); self.play_preview=QPushButton("Play"); self.play_preview.clicked.connect(self.preview.play); layout.addWidget(self.play_preview)
        return page
    def _build_settings_card(self):
        card=QFrame(); card.setObjectName("panelCard"); outer=QVBoxLayout(card); outer.setContentsMargins(12,14,12,14); outer.setSpacing(8); outer.addWidget(self.section_label("SETTINGS & DETAILS"))
        tabs=QFrame(); tabs.setObjectName("settingsTabRow"); tab_layout=QHBoxLayout(tabs); tab_layout.setContentsMargins(0,0,0,0); tab_layout.setSpacing(4)
        self.settings_tab_buttons=[]
        for index,(label,text) in enumerate((("Panel","Panel"),("Timing Plan","Timing\nPlan"),("Background Music","Background\nMusic"),("Captions","Captions"))):
            tab=QToolButton(tabs); tab.setObjectName("settingsTab"); tab.setText(text); tab.setToolTip(label); tab.setAccessibleName(label); tab.setCheckable(True); tab.setAutoExclusive(True); tab.setToolButtonStyle(Qt.ToolButtonTextOnly); tab.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed); tab.setFixedHeight(40); tab.clicked.connect(lambda _, index=index:self.set_settings_tab(index)); tab_layout.addWidget(tab,1); self.settings_tab_buttons.append(tab)
        outer.addWidget(tabs)
        self.settings_stack=QStackedWidget(); self.settings_stack.setObjectName("settingsStack")

        self.settings_scroll,panel_content,panel_layout=self.build_settings_page("settingsScroll","settingsStage")
        self.detail=QLabel("Panel details"); self.detail.setObjectName("detailCard"); panel_layout.addWidget(self.detail); panel_layout.addWidget(self.section_label("NARRATION FOLLOWED")); self.following_text=QLabel("Import a timestamped ElevenLabs transcript to see the dialogue for this panel."); self.following_text.setObjectName("narrationCard"); self.following_text.setWordWrap(True); self.following_text.setMinimumHeight(58); panel_layout.addWidget(self.following_text)
        form=QFormLayout(); form.setLabelAlignment(Qt.AlignLeft); self.effect=QComboBox(); self.effect.addItems([effect.replace("_"," ").title() for effect in EFFECTS]); self.effect.currentIndexChanged.connect(self.change_effect); self.anchor=QComboBox(); self.anchor.addItems(["center","top","bottom","left","right","custom"]); self.anchor.currentTextChanged.connect(self.change_anchor); self.start=QDoubleSpinBox(); self.end=QDoubleSpinBox()
        for spin in (self.start,self.end): spin.setDecimals(3); spin.setMaximum(36000); spin.setSingleStep(.1); spin.editingFinished.connect(self.change_timing)
        form.addRow("Effect",self.effect); form.addRow("Zoom anchor",self.anchor); form.addRow("Start",self.start); form.addRow("End",self.end); panel_layout.addLayout(form); panel_layout.addStretch()

        self.timing_plan_scroll,timing_content,timing_layout=self.build_settings_page("settingsTabScroll","settingsTabStage")
        timing_layout.addWidget(self.section_label("EDIT DECISION PLAN")); self.effect_table=QTableWidget(0,5); self.effect_table.setHorizontalHeaderLabels(["Panel","Start","End","Effect","Source"]); self.effect_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); self.effect_table.verticalHeader().setVisible(False); self.effect_table.setEditTriggers(QTableWidget.NoEditTriggers); self.effect_table.setMinimumHeight(140); self.effect_table.setMaximumHeight(16_777_215); timing_layout.addWidget(self.effect_table,1)

        self.background_music_scroll,music_content,music_layout=self.build_settings_page("settingsTabScroll","settingsTabStage")
        self.background_music_section=self.build_background_music_section(); music_layout.addWidget(self.background_music_section); music_layout.addStretch()

        self.captions_scroll,captions_content,captions_layout=self.build_settings_page("settingsTabScroll","settingsTabStage")
        captions_layout.addWidget(self.build_caption_style_section()); captions_layout.addStretch()

        self.settings_tab_scrolls=[self.settings_scroll,self.timing_plan_scroll,self.background_music_scroll,self.captions_scroll]
        for page in self.settings_tab_scrolls: self.settings_stack.addWidget(page)
        outer.addWidget(self.settings_stack,1); self.set_settings_tab(0)
        return card
    def build_settings_page(self, scroll_name, content_name):
        scroll=QScrollArea(); scroll.setObjectName(scroll_name); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        content=QWidget(); content.setObjectName(content_name); layout=QVBoxLayout(content); layout.setContentsMargins(10,10,10,10); layout.setSpacing(10); scroll.setWidget(content)
        return scroll,content,layout
    def set_settings_tab(self,index):
        if not hasattr(self,"settings_stack") or not 0 <= index < self.settings_stack.count(): return
        self.settings_stack.setCurrentIndex(index)
        for button_index,button in enumerate(self.settings_tab_buttons): button.setChecked(button_index == index)
        if hasattr(self,"settings_wheel_guard"): self.settings_wheel_guard.scroll_area=self.settings_tab_scrolls[index]
    def set_preview_mode(self,index):
        self.preview_stack.setCurrentIndex(index); self.timeline_toggle.setChecked(index == 0); self.panel_toggle.setChecked(index == 1); self.settings.setValue("preview_mode","timeline" if index == 0 else "panel")
    def apply_popup_styles(self):
        for combo in self.findChildren(QComboBox):
            view=combo.view(); palette=view.palette()
            for role,color in ((QPalette.ColorRole.Base,"#0e1015"),(QPalette.ColorRole.AlternateBase,"#101216"),(QPalette.ColorRole.Text,"#f5f8ff"),(QPalette.ColorRole.WindowText,"#f5f8ff"),(QPalette.ColorRole.Highlight,"#6c5ce7"),(QPalette.ColorRole.HighlightedText,"#ffffff")):
                palette.setColor(role,QColor(color))
            palette.setColor(QPalette.ColorGroup.Disabled,QPalette.ColorRole.Text,QColor("#b6c2d3")); view.setPalette(palette); view.setStyleSheet(POPUP_VIEW_STYLE)
    def apply_settings_wheel_guard(self):
        self.settings_wheel_guard=SettingsWheelGuard(self.settings_scroll)
        for widget_type in (QComboBox,QSpinBox,QDoubleSpinBox,QSlider):
            for control in self.settings_stack.findChildren(widget_type):
                control.setFocusPolicy(Qt.StrongFocus); control.installEventFilter(self.settings_wheel_guard)
    def section_label(self,text):
        label=QLabel(text); label.setObjectName("sectionTitle"); return label
    def build_background_music_section(self):
        section=QFrame(); section.setObjectName("backgroundMusic"); layout=QVBoxLayout(section); layout.setContentsMargins(8,8,8,8); layout.setSpacing(6); layout.addWidget(self.section_label("BACKGROUND MUSIC"))
        self.background_music_source=QLabel(); self.background_music_source.setObjectName("backgroundMusicSource"); self.background_music_source.setWordWrap(True); layout.addWidget(self.background_music_source)
        buttons=QHBoxLayout(); choose=QPushButton("Choose audio"); choose.clicked.connect(self.choose_background_music); use_default=QPushButton("Use default"); use_default.clicked.connect(self.use_default_background_music); buttons.addWidget(choose); buttons.addWidget(use_default); layout.addLayout(buttons)
        volume_row=QHBoxLayout(); volume_row.addWidget(QLabel("Music volume")); self.background_music_volume_slider=QSlider(Qt.Horizontal); self.background_music_volume_slider.setRange(0,100); self.background_music_volume=QSpinBox(); self.background_music_volume.setRange(0,100); self.background_music_volume.setSuffix("%"); self.background_music_volume.setFixedWidth(76); self.background_music_volume_slider.valueChanged.connect(self.background_music_volume.setValue); self.background_music_volume.valueChanged.connect(self.background_music_volume_slider.setValue); self.background_music_volume_slider.valueChanged.connect(self.change_background_music_volume); volume_row.addWidget(self.background_music_volume_slider,1); volume_row.addWidget(self.background_music_volume); layout.addLayout(volume_row); self.sync_background_music_controls(); return section
    def sync_background_music_controls(self):
        if not hasattr(self,"background_music_source"): return
        path=resolve_background_music_path(self.project); configured=self.project.background_music_path
        if configured: text=f"Custom: {Path(configured).name}" if path else f"Custom file missing: {Path(configured).name}"
        elif path: text=f"Default: {path.name}"
        else: text="Default: add assets/background_music.mp3"
        self.background_music_source.setText(text); volume=round(max(0.0,min(1.0,self.project.background_music_volume))*100)
        self.background_music_volume_slider.blockSignals(True); self.background_music_volume.blockSignals(True); self.background_music_volume_slider.setValue(volume); self.background_music_volume.setValue(volume); self.background_music_volume_slider.blockSignals(False); self.background_music_volume.blockSignals(False)
    def choose_background_music(self):
        path=QFileDialog.getOpenFileName(self,"Choose background music","","Audio (*.mp3 *.wav *.m4a *.aac *.ogg *.flac)")[0]
        if not path: return
        self.project.background_music_path=path; self.set_timeline_project(); self.sync_background_music_controls(); self.status.setText(f"Background music: {Path(path).name}")
    def use_default_background_music(self):
        self.project.background_music_path=""; self.set_timeline_project(); self.sync_background_music_controls(); self.status.setText("Using default background music")
    def change_background_music_volume(self, value):
        self.project.background_music_volume=value/100; self.timeline_bgm_audio.setVolume(self.project.background_music_volume); self.sync_background_music_controls()
    def build_caption_style_section(self):
        self.caption_style_section=QFrame(); self.caption_style_section.setObjectName("captionStyle")
        outer=QVBoxLayout(self.caption_style_section); outer.setContentsMargins(8,8,8,8); outer.setSpacing(8)
        header=QHBoxLayout(); header.addWidget(self.section_label("CAPTION STYLE")); header.addStretch(); reset=QPushButton("Reset to defaults"); reset.clicked.connect(self.reset_caption_defaults); header.addWidget(reset); outer.addLayout(header)
        self.caption_style_body=QWidget(); body=QVBoxLayout(self.caption_style_body); body.setContentsMargins(0,0,0,0); body.setSpacing(8)
        position_form=QFormLayout(); position_form.setLabelAlignment(Qt.AlignLeft); position_form.setHorizontalSpacing(8); position_form.setVerticalSpacing(6)
        self.caption_position_slider,self.caption_position=self.caption_slider_spin(0,1919,1450,"vertical_position"); position_form.addRow("Vertical",self.caption_position_row(self.caption_position_slider,self.caption_position))
        presets=QHBoxLayout()
        for text,value in [("Top",220),("Middle",960),("Lower third",1450),("Bottom",1780)]:
            button=QPushButton(text); button.clicked.connect(lambda _, value=value:self.queue_caption_setting("vertical_position",value)); presets.addWidget(button)
        position_form.addRow("Presets",self.layout_widget(presets))
        self.caption_alignment=QComboBox(); self.caption_alignment.addItems(["left","center","right"]); self.caption_alignment.currentTextChanged.connect(lambda value:self.queue_caption_setting("horizontal_alignment",value)); position_form.addRow("Alignment",self.caption_alignment)
        body.addLayout(position_form)
        self.typography_section,typography_body,typography_layout=self.build_caption_collapsible_section("TYPOGRAPHY")
        typography_form=QFormLayout(); typography_form.setLabelAlignment(Qt.AlignLeft); typography_form.setHorizontalSpacing(8); typography_form.setVerticalSpacing(6)
        self.caption_font=QFontComboBox(); self.caption_font.currentFontChanged.connect(lambda font:self.queue_caption_setting("font",font.family())); typography_form.addRow("Font",self.caption_font)
        self.caption_font_size=QSpinBox(); self.caption_font_size.setRange(16,240); self.caption_font_size.setSuffix(" px"); self.caption_font_size.valueChanged.connect(lambda value:self.queue_caption_setting("font_size",value)); typography_form.addRow("Font size",self.caption_font_size)
        self.caption_weight=QComboBox(); self.caption_weight.addItems(["Regular","Medium","Bold","Extra Bold"]); self.caption_weight.currentTextChanged.connect(lambda value:self.queue_caption_setting("font_weight",value)); typography_form.addRow("Text weight",self.caption_weight)
        self.caption_normal_color=QPushButton(); self.caption_normal_color.clicked.connect(lambda:self.pick_caption_color("normal_color","Normal text color")); typography_form.addRow("Normal color",self.caption_normal_color)
        self.caption_highlight_color=QPushButton(); self.caption_highlight_color.clicked.connect(lambda:self.pick_caption_color("highlight_color","Highlight color")); typography_form.addRow("Highlight color",self.caption_highlight_color)
        self.caption_all_caps=QCheckBox("ALL CAPS"); self.caption_all_caps.toggled.connect(lambda value:self.queue_caption_setting("all_caps",value,True)); typography_form.addRow("Casing",self.caption_all_caps)
        typography_layout.addLayout(typography_form); body.addWidget(self.typography_section)
        self.effects_positioning_section,effects_body,effects_layout=self.build_caption_collapsible_section("EFFECTS & POSITIONING")
        effects_form=QFormLayout(); effects_form.setLabelAlignment(Qt.AlignLeft); effects_form.setHorizontalSpacing(8); effects_form.setVerticalSpacing(6)
        self.caption_margin=QSpinBox(); self.caption_margin.setRange(0,500); self.caption_margin.setSuffix(" px"); self.caption_margin.valueChanged.connect(lambda value:self.queue_caption_setting("horizontal_margin",value)); effects_form.addRow("Horizontal margin",self.caption_margin)
        self.caption_outline_slider,self.caption_outline=self.caption_slider_spin(0,20,5,"outline"); self.caption_outline_slider.setProperty("accentSlider",True); effects_form.addRow("Outline thickness",self.caption_position_row(self.caption_outline_slider,self.caption_outline))
        self.caption_outline_color=QPushButton(); self.caption_outline_color.clicked.connect(lambda:self.pick_caption_color("outline_color","Outline color")); effects_form.addRow("Outline color",self.caption_outline_color)
        self.caption_shadow_slider,self.caption_shadow=self.caption_slider_spin(0,20,2,"shadow"); self.caption_shadow_slider.setProperty("accentSlider",True); effects_form.addRow("Shadow depth",self.caption_position_row(self.caption_shadow_slider,self.caption_shadow))
        self.caption_shadow_color=QPushButton(); self.caption_shadow_color.clicked.connect(lambda:self.pick_caption_color("shadow_color","Shadow color")); effects_form.addRow("Shadow color",self.caption_shadow_color)
        effects_layout.addLayout(effects_form); body.addWidget(self.effects_positioning_section)
        timing_form=QFormLayout(); timing_form.setLabelAlignment(Qt.AlignLeft); timing_form.setHorizontalSpacing(8); timing_form.setVerticalSpacing(6)
        self.caption_offset=QDoubleSpinBox(); self.caption_offset.setRange(-1.0,1.0); self.caption_offset.setDecimals(2); self.caption_offset.setSingleStep(.01); self.caption_offset.setSuffix(" sec"); self.caption_offset.valueChanged.connect(lambda value:self.queue_caption_setting("timing_offset",value)); timing_form.addRow("Timing offset",self.caption_offset)
        self.caption_grouping=QComboBox(); self.caption_grouping.addItems(["auto","2","3","4"]); self.caption_grouping.currentTextChanged.connect(lambda value:self.queue_caption_setting("grouping",value,True)); timing_form.addRow("Words per caption",self.caption_grouping)
        self.caption_highlight_mode=QComboBox(); self.caption_highlight_mode.addItem("Current word","word"); self.caption_highlight_mode.addItem("Whole caption line","line"); self.caption_highlight_mode.currentIndexChanged.connect(lambda _:self.queue_caption_setting("highlight_mode",self.caption_highlight_mode.currentData())); timing_form.addRow("Highlight mode",self.caption_highlight_mode)
        body.addLayout(timing_form); outer.addWidget(self.caption_style_body); self.caption_style_section.setProperty("state","expanded"); self.sync_caption_controls(); return self.caption_style_section
    def build_caption_collapsible_section(self,title):
        section=QFrame(); section.setObjectName("captionSubsection"); section.setProperty("collapsible",True); outer=QVBoxLayout(section); outer.setContentsMargins(8,6,8,8); outer.setSpacing(6)
        toggle=QToolButton(); toggle.setObjectName("captionSectionToggle"); toggle.setText(title); toggle.setCheckable(True); toggle.setChecked(True); toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon); toggle.setArrowType(Qt.DownArrow); toggle.setProperty("collapsible",True); body=QWidget(); body_layout=QVBoxLayout(body); body_layout.setContentsMargins(0,0,0,0); body_layout.setSpacing(6)
        toggle.toggled.connect(lambda expanded, section=section, body=body, toggle=toggle:self.set_caption_collapsible_expanded(section,body,toggle,expanded)); outer.addWidget(toggle); outer.addWidget(body); self.set_caption_collapsible_expanded(section,body,toggle,True)
        return section,body,body_layout
    def set_caption_collapsible_expanded(self,section,body,toggle,expanded):
        body.setVisible(expanded); toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow); section.setProperty("state","expanded" if expanded else "collapsed"); section.style().unpolish(section); section.style().polish(section)
    def layout_widget(self, layout):
        widget=QWidget(); widget.setLayout(layout); return widget
    def caption_slider_spin(self, minimum, maximum, value, field):
        slider=QSlider(Qt.Horizontal); slider.setRange(minimum,maximum); slider.setValue(value)
        spin=QSpinBox(); spin.setRange(minimum,maximum); spin.setValue(value); spin.setFixedWidth(76)
        slider.valueChanged.connect(spin.setValue); spin.valueChanged.connect(slider.setValue)
        slider.valueChanged.connect(lambda current:self.queue_caption_setting(field,current))
        return slider,spin
    def caption_position_row(self, slider, spin):
        row=QHBoxLayout(); row.setContentsMargins(0,0,0,0); row.addWidget(slider,1); row.addWidget(spin); return self.layout_widget(row)
    def set_caption_style_expanded(self, expanded):
        self.caption_style_body.setVisible(expanded); self.caption_style_section.setProperty("state","expanded" if expanded else "collapsed"); self.caption_style_section.style().unpolish(self.caption_style_section); self.caption_style_section.style().polish(self.caption_style_section)
    def caption_color(self, value):
        value=str(value or "000000").upper().replace("#","").zfill(6)[-6:]
        try: return QColor(int(value[4:6],16),int(value[2:4],16),int(value[:2],16))
        except ValueError: return QColor("black")
    def caption_color_value(self, color):
        return f"{color.blue():02X}{color.green():02X}{color.red():02X}"
    def update_caption_color_button(self, button, value):
        color=self.caption_color(value); swatch=QPixmap(18,18); swatch.fill(color); button.setIcon(QIcon(swatch)); button.setText(color.name().upper())
    def pick_caption_color(self, field, title):
        color=QColorDialog.getColor(self.caption_color(getattr(self.project.caption_settings,field)),self,title)
        if color.isValid(): self.queue_caption_setting(field,self.caption_color_value(color))
    def sync_caption_controls(self):
        if not hasattr(self,"caption_style_section"): return
        self._updating_caption_controls=True; s=self.project.caption_settings
        controls=[self.caption_position_slider,self.caption_position,self.caption_alignment,self.caption_margin,self.caption_font,self.caption_font_size,self.caption_weight,self.caption_outline_slider,self.caption_outline,self.caption_shadow_slider,self.caption_shadow,self.caption_offset,self.caption_grouping,self.caption_highlight_mode,self.caption_all_caps]
        for control in controls: control.blockSignals(True)
        self.caption_position_slider.setValue(s.vertical_position); self.caption_position.setValue(s.vertical_position); self.caption_alignment.setCurrentText(s.horizontal_alignment); self.caption_margin.setValue(s.horizontal_margin); self.caption_font.setCurrentFont(QFont(s.font)); self.caption_font_size.setValue(s.font_size); self.caption_weight.setCurrentText(s.font_weight); self.caption_outline_slider.setValue(s.outline); self.caption_outline.setValue(s.outline); self.caption_shadow_slider.setValue(s.shadow); self.caption_shadow.setValue(s.shadow); self.caption_offset.setValue(s.timing_offset); self.caption_grouping.setCurrentText(str(s.grouping)); self.caption_highlight_mode.setCurrentIndex(max(0,self.caption_highlight_mode.findData(s.highlight_mode))); self.caption_all_caps.setChecked(s.all_caps)
        for control in controls: control.blockSignals(False)
        self.update_caption_color_button(self.caption_normal_color,s.normal_color); self.update_caption_color_button(self.caption_highlight_color,s.highlight_color); self.update_caption_color_button(self.caption_outline_color,s.outline_color); self.update_caption_color_button(self.caption_shadow_color,s.shadow_color); self._updating_caption_controls=False
    def rebuild_captions(self):
        s=self.project.caption_settings
        self.project.captions=group_captions(self.project.words,s.grouping,s.all_caps) if self.project.words else []
    def queue_caption_setting(self, field, value, rebuild=False):
        if self._updating_caption_controls: return
        settings=self.project.caption_settings
        if getattr(settings,field) == value: return
        before_settings,before_captions=copy.deepcopy(settings),copy.deepcopy(self.project.captions)
        setattr(settings,field,value)
        if rebuild: self.rebuild_captions()
        self.timeline_preview.update()
        pending=self._caption_pending.get(field)
        if pending is None: self._caption_pending[field]=[before_settings,before_captions,copy.deepcopy(settings),copy.deepcopy(self.project.captions)]
        else: pending[2],pending[3]=copy.deepcopy(settings),copy.deepcopy(self.project.captions)
        self.caption_debounce.start()
    def commit_caption_changes(self):
        pending,self._caption_pending=self._caption_pending,{}
        if not pending: return
        session=self._caption_change_session; self._caption_change_session+=1
        for field,(before_settings,before_captions,after_settings,after_captions) in pending.items():
            self.undo.push(CaptionSettingsCommand(self,before_settings,before_captions,after_settings,after_captions,f"{field}:{session}"))
    def reset_caption_defaults(self):
        self.caption_debounce.stop(); self.commit_caption_changes(); before_settings,before_captions=copy.deepcopy(self.project.caption_settings),copy.deepcopy(self.project.captions); self.project.caption_settings=CaptionSettings(); self.rebuild_captions(); after_settings,after_captions=copy.deepcopy(self.project.caption_settings),copy.deepcopy(self.project.captions)
        if before_settings == after_settings and before_captions == after_captions: return
        self.undo.push(CaptionSettingsCommand(self,before_settings,before_captions,after_settings,after_captions,"reset"))
    def dragEnterEvent(self,e):
        if e.mimeData().hasUrls(): e.acceptProposedAction()
    def dropEvent(self,e):
        self.add_paths([u.toLocalFile() for u in e.mimeData().urls()])
    def import_panels(self): self.add_paths(QFileDialog.getOpenFileNames(self,"Import panels","","Images (*.jpg *.jpeg *.png *.webp)")[0])
    def crop_cache_dir(self) -> Path:
        base = Path(self.project_path).parent / f"{Path(self.project_path).stem}_crops" if self.project_path else Path(tempfile.gettempdir()) / "manhwa_studio_crops"
        base.mkdir(parents=True, exist_ok=True)
        return base
    def add_paths(self, paths):
        valid=[p for p in paths if Path(p).suffix.lower() in IMAGE_EXTENSIONS]
        if not valid: return
        cache_dir = self.crop_cache_dir()
        new_panels = []
        pending_rect = None
        pending_lock_916 = False
        lock_916 = False
        skip_remaining = False
        for index, path in enumerate(valid):
            panel_id = str(uuid4())
            crop_path, crop_rect = "", None
            try:
                if skip_remaining:
                    pass
                elif pending_rect is not None:
                    crop_rect = lock_crop_rect(path, pending_rect) if pending_lock_916 else pending_rect
                    crop_path = apply_crop(path, crop_rect, str(cache_dir / f"{panel_id}.png"))
                else:
                    dialog = CropDialog(path, parent=self, remaining_count=len(valid) - index - 1, lock_916=lock_916)
                    if dialog.exec() == QDialog.Accepted and dialog.result_rect:
                        crop_rect = dialog.result_rect
                        crop_path = apply_crop(path, crop_rect, str(cache_dir / f"{panel_id}.png"))
                        lock_916 = dialog.lock_aspect.isChecked()
                        if dialog.apply_to_remaining:
                            pending_rect = crop_rect
                            pending_lock_916 = lock_916
                    else:
                        lock_916 = dialog.lock_aspect.isChecked()
                        skip_remaining = dialog.skip_remaining
            except Exception as error:
                self.error(str(error))
                return
            new_panels.append(Panel(path=path, original_file=Path(path).name, id=panel_id, crop_path=crop_path, crop_rect=crop_rect))
        before=self.project.panels[:]
        self.project.panels.extend(new_panels)
        assign_effects(self.project.panels)
        self.undo.push(PanelStateCommand(self,before,self.project.panels,"Add panels"))
        self.status.setText(f"Imported {len(new_panels)} panels")
    def refresh(self):
        self.project.renumber(); self.playing_delegate.playing_id=None; self._thumbnail_generation += 1; self.timeline_preview.pixmaps.clear(); self.panels.viewport().update(); self.panels.blockSignals(True); self.panel_sidebar.blockSignals(True); self.panels.clear(); self.panel_sidebar.clear()
        for p in self.project.panels:
            item=QListWidgetItem(f"{p.sequence:03d}\n{Path(p.path).name}")
            item.setSizeHint(QSize(150,208))
            item.setData(Qt.UserRole,p.id); self.panels.addItem(item)
            sidebar_item=QListWidgetItem(f"{p.sequence:03d}   {Path(p.path).name}"); sidebar_item.setSizeHint(QSize(154,42)); sidebar_item.setData(Qt.UserRole,p.id); self.panel_sidebar.addItem(sidebar_item)
        self.panels.blockSignals(False); self.panel_sidebar.blockSignals(False)
        self.panel_sidebar.sync_item_widths()
        self.effect_table.setRowCount(len(self.project.panels))
        for row,p in enumerate(self.project.panels):
            for column,value in enumerate((f"{p.sequence:03d}",f"{p.start_time:.3f}",f"{p.end_time:.3f}",p.effect.replace("_"," ").title(),p.effect_source)):
                self.effect_table.setItem(row,column,QTableWidgetItem(value))
        self.thumbnail_worker=ThumbnailWorker(self.project.panels,self._thumbnail_generation); self._thumbnail_workers.append(self.thumbnail_worker); self.thumbnail_worker.thumbnail.connect(self.set_thumbnail); self.thumbnail_worker.finished.connect(lambda worker=self.thumbnail_worker:self.retire_thumbnail_worker(worker)); self.thumbnail_worker.start()
        self.update_panel_counter(); self.set_timeline_project(preserve_time=True); self.sync_caption_controls(); self.sync_background_music_controls()
    def retire_thumbnail_worker(self, worker):
        if worker in self._thumbnail_workers: self._thumbnail_workers.remove(worker)
    def set_thumbnail(self,generation,panel_id,pixmap):
        if generation != self._thumbnail_generation: return
        for row in range(self.panels.count()):
            item=self.panels.item(row)
            if item.data(Qt.UserRole)==panel_id: item.setIcon(pixmap); break
        for row in range(self.panel_sidebar.count()):
            item=self.panel_sidebar.item(row)
            if item.data(Qt.UserRole)==panel_id:
                item.setIcon(pixmap.scaled(28,40,Qt.KeepAspectRatio,Qt.SmoothTransformation)); break
    def show_panel_context_menu(self, pos):
        if len(self.panels.selectedItems()) != 1: return
        menu = QMenu(self)
        menu.addAction("Crop Panel...", self.crop_selected_panel)
        menu.exec(self.panels.viewport().mapToGlobal(pos))
    def crop_selected_panel(self):
        index = self.selected_index()
        if index < 0: return
        panel = self.project.panels[index]
        try:
            dialog = CropDialog(panel.path, initial_rect=panel.crop_rect, parent=self)
        except Exception as error:
            self.error(str(error))
            return
        if dialog.exec() != QDialog.Accepted or not dialog.result_rect: return
        crop_path = str(self.crop_cache_dir() / f"{panel.id}.png")
        try:
            apply_crop(panel.path, dialog.result_rect, crop_path)
        except Exception as error:
            self.error(str(error))
            return
        before = copy.deepcopy(self.project.panels)
        panel.crop_rect = dialog.result_rect
        panel.crop_path = crop_path
        self.undo.push(PanelStateCommand(self,before,self.project.panels,"Crop panel"))
        self.panels.setCurrentRow(index)
        self.show_selected()
        self.status.setText(f"Cropped panel {panel.sequence:03d}")
    def selected_index(self):
        item=self.panels.currentItem(); return self.panels.row(item) if item else -1
    def show_selected(self):
        i=self.selected_index()
        if i<0:return
        self.set_settings_tab(0)
        p=self.project.panels[i]; self.panel_sidebar.blockSignals(True); self.panel_sidebar.setCurrentRow(i); self.panel_sidebar.blockSignals(False); pix=QPixmap(p.render_path); self.preview.set_panel(pix,p.effect,p.zoom_anchor,p.duration); self.detail.setText(f"Panel {p.sequence:03d}\nStart: {p.start_time:.3f}  End: {p.end_time:.3f}\nDuration: {p.duration:.3f} sec"); self.following_text.setText(self.panel_narration(p))
        self.effect.blockSignals(True); self.effect.setCurrentIndex(EFFECTS.index(p.effect)); self.effect.blockSignals(False); self.anchor.blockSignals(True); self.anchor.setCurrentText(p.zoom_anchor); self.anchor.blockSignals(False); self.start.setValue(p.start_time); self.end.setValue(p.end_time); self.sync_timeline_to_panel(p)
    def sync_timeline_to_panel(self,panel):
        if self.timeline_playing: self.pause_timeline()
        active=self.timeline_preview.active_panel()
        if active and active[2].id == panel.id: return
        for start,end,current in self.timeline_preview.timeline:
            if current.id == panel.id:
                self.set_timeline_time(start,seek_audio=True)
                break
    def select_from_sidebar(self):
        item = self.panel_sidebar.currentItem()
        if not item: return
        panel_id = item.data(Qt.UserRole)
        for row in range(self.panels.count()):
            if self.panels.item(row).data(Qt.UserRole) == panel_id:
                self.panels.setCurrentRow(row); return
    def panel_narration(self, panel):
        segment_index = panel.sequence - 1
        if 0 <= segment_index < len(self.project.segments):
            return self.project.segments[segment_index].text
        if self.project.segments:
            return "No narration segment is assigned to this extra panel. Add or remove panels until the counter matches."
        if not self.project.words:
            return "Import a timestamped ElevenLabs transcript to see the dialogue for this panel."
        words = [word.text for word in self.project.words if word.end > panel.start_time and word.start < panel.end_time]
        return " ".join(words) if words else "No spoken words fall within this panel’s current timing."
    def record_reorder(self, before_ids, after_ids):
        by_id = {panel.id: panel for panel in self.project.panels}
        before = [by_id[panel_id] for panel_id in before_ids]
        after = [by_id[panel_id] for panel_id in after_ids]
        self.undo.push(ReorderCommand(self,before,after))
    def remove_selected(self):
        before=self.project.panels[:]; ids={x.data(Qt.UserRole) for x in self.panels.selectedItems()}; self.project.panels=[p for p in self.project.panels if p.id not in ids]; self.undo.push(PanelStateCommand(self,before,self.project.panels,"Remove panels"))
    def duplicate_selected(self):
        n=self.selected_index()
        if n < 0:return
        before=self.project.panels[:]; p=copy.deepcopy(self.project.panels[n]); p.id=Panel(path=p.path,original_file=p.original_file).id; self.project.panels.insert(n+1,p); self.undo.push(PanelStateCommand(self,before,self.project.panels,"Duplicate panel"))
    def reset_order(self):
        before=self.project.panels[:]; self.project.panels.sort(key=lambda p:p.original_file.lower()); assign_effects(self.project.panels); self.undo.push(PanelStateCommand(self,before,self.project.panels,"Reset panel order"))
    def reverse_order(self):
        before_ids=[panel.id for panel in self.project.panels]
        if len(before_ids) > 1: self.record_reorder(before_ids,list(reversed(before_ids)))
    def rename_panels(self):
        try: safe_rename(self.project.panels); self.refresh(); self.status.setText("Panel files renamed safely.")
        except Exception as e: self.error(str(e))
    def change_effect(self,i):
        n=self.selected_index()
        if n>=0:
            before=self.project.panels[:]; set_effect(self.project.panels,n,EFFECTS[i]); self.undo.push(PanelStateCommand(self,before,self.project.panels,"Change effect")); self.show_selected()
    def change_anchor(self,value):
        n=self.selected_index()
        if n>=0:
            before=self.project.panels[:]; self.project.panels[n].zoom_anchor=value; self.undo.push(PanelStateCommand(self,before,self.project.panels,"Change zoom anchor")); self.show_selected()
    def change_timing(self):
        n=self.selected_index()
        if n>=0 and self.end.value() > self.start.value():
            before=self.project.panels[:]; self.project.panels[n].start_time,self.project.panels[n].end_time=self.start.value(),self.end.value(); self.undo.push(PanelStateCommand(self,before,self.project.panels,"Change panel timing"))
    def import_audio(self):
        p=QFileDialog.getOpenFileName(self,"Import voice","","Audio (*.mp3 *.wav *.m4a)")[0]
        if p:self.project.audio_path=p; self.set_timeline_project(); self.status.setText(f"Voice: {Path(p).name}")
    def import_transcript(self):
        p=QFileDialog.getOpenFileName(self,"Import ElevenLabs JSON","","JSON (*.json)")[0]
        if not p:return
        try:
            _,self.project.words,self.project.segments=parse_elevenlabs_with_segments(p); self.project.transcript_path=p; s=self.project.caption_settings; self.project.captions=group_captions(self.project.words,s.grouping,s.all_caps); assign_timings(self.project.panels,self.project.words,self.project.segments); self.refresh(); self.status.setText(f"Timestamps detected: {len(self.project.segments)} narration segments; {len(self.project.words)} words; {len(self.project.captions)} captions."); self.show_selected()
        except ValueError as e:self.error(str(e))
    def recalculate_timing(self):
        if not self.project.words: return self.error("Import a timestamped ElevenLabs JSON first.")
        before=self.project.panels[:]; assign_timings(self.project.panels,self.project.words,self.project.segments); self.undo.push(PanelStateCommand(self,before,self.project.panels,"Recalculate timing"))
    def projects_directory(self):
        directory=Path.cwd()/"projects"; directory.mkdir(parents=True,exist_ok=True); return directory
    def next_project_path(self):
        directory=self.projects_directory(); indexes=[]
        for path in directory.glob("Project_*.json"):
            suffix=path.stem[len("Project_"):]
            if suffix.isdigit(): indexes.append(int(suffix))
        return directory/f"Project_{max(indexes,default=0)+1:03d}.json"
    def new(self): self.project=Project(name=self.next_project_path().stem); self.project_path=""; self.undo.clear(); self.refresh(); self.update_window_title()
    def open(self):
        p=QFileDialog.getOpenFileName(self,"Open project",str(self.projects_directory()),"Manhwa project (*.json)")[0]
        if p:
            try:
                self.project=load_project(p); self.project_path=p; self.refresh(); self.update_window_title(); missing=missing_sources(self.project); self.status.setText(f"Opened {Path(p).name}" + (f" — {len(missing)} missing source file(s)" if missing else "")); self.remember_project(p)
            except Exception as e:self.error(str(e))
    def save(self):
        if not self.project_path:return self.save_as()
        save_project(self.project,self.project_path); self.remember_project(self.project_path); self.update_window_title(); self.status.setText("Project saved")
    def save_as(self):
        p=QFileDialog.getSaveFileName(self,"Save project",str(self.next_project_path()),"Manhwa project (*.json)")[0]
        if p:self.project_path=p; self.project.name=Path(p).stem; self.save()
    def autosave_project(self):
        if not self.project_path:return
        try: save_project(self.project,self.project_path+".autosave.json")
        except Exception: logging.exception("Autosave failed")
    def remember_project(self,path):
        paths=self.settings.value("recent_projects",[],type=list); paths=[path]+[x for x in paths if x!=path]; self.settings.setValue("recent_projects",paths[:10])
    def update_window_title(self):
        name = Path(self.project_path).stem if self.project_path else self.project.name
        self.setWindowTitle(f"Manhwa Studio — {name}")
    def update_panel_counter(self):
        panel_count, segment_count = len(self.project.panels), len(self.project.segments)
        if not segment_count:
            text, state = f"Panels {panel_count} / Segments —", "neutral"
        elif panel_count == segment_count:
            text, state = f"Panels {panel_count} / Segments {segment_count} (MATCH)", "valid"
        else:
            text, state = f"Panels {panel_count} / Segments {segment_count} (MISMATCH)", "invalid"
        self.panel_counter.setText(text); self.panel_counter.setProperty("state", state); self.panel_counter.style().unpolish(self.panel_counter); self.panel_counter.style().polish(self.panel_counter)
        self.panel_grid_host.set_empty_state(panel_count == 0)
    def timeline_label(self, seconds):
        return f"{int(seconds // 60)}:{int(seconds % 60):02d}"
    def highlight_playing_panel(self):
        active=self.timeline_preview.active_panel(); panel_id=active[2].id if active else None
        if panel_id == self.playing_delegate.playing_id: return
        self.playing_delegate.playing_id=panel_id; self.panels.viewport().update()
        if panel_id is None: return
        for row in range(self.panels.count()):
            item=self.panels.item(row)
            if item.data(Qt.UserRole) == panel_id:
                self.panels.scrollToItem(item,QListWidget.PositionAtCenter)
                break
    def set_timeline_project(self,preserve_time=False):
        previous_time=self.timeline_time_value if preserve_time else 0.0
        self.pause_timeline(); self.timeline_preview.set_project(self.project); duration = self.timeline_preview.duration()
        self.timeline_slider.setRange(0, round(duration * 1000))
        self.timeline_has_audio = bool(self.project.audio_path and Path(self.project.audio_path).exists())
        background_music=resolve_background_music_path(self.project); self.timeline_has_bgm=bool(background_music)
        self.timeline_player.setSource(QUrl.fromLocalFile(self.project.audio_path) if self.timeline_has_audio else QUrl())
        self.timeline_bgm_player.setSource(QUrl.fromLocalFile(str(background_music)) if self.timeline_has_bgm else QUrl()); self.timeline_bgm_audio.setVolume(max(0.0,min(1.0,self.project.background_music_volume)))
        self.set_timeline_time(min(previous_time,duration),seek_audio=preserve_time)
    def set_timeline_time(self, seconds, seek_audio=False):
        duration = self.timeline_preview.duration(); self.timeline_time_value = max(0.0, min(seconds, duration)); self.timeline_preview.set_time(self.timeline_time_value); self.highlight_playing_panel()
        self.timeline_slider.setValue(round(self.timeline_time_value * 1000)); self.timeline_time.setText(f"{self.timeline_label(self.timeline_time_value)} / {self.timeline_label(duration)}")
        if seek_audio and self.timeline_has_audio: self.timeline_player.setPosition(round(self.timeline_time_value * 1000))
        if seek_audio and self.timeline_has_bgm: self.timeline_bgm_player.setPosition(round(self.timeline_time_value * 1000))
    def toggle_timeline(self):
        if not self.timeline_preview.duration(): return
        if self.timeline_playing:
            self.pause_timeline(); return
        if self.timeline_time_value >= self.timeline_preview.duration(): self.set_timeline_time(0, True)
        if self.timeline_has_audio:
            self.timeline_player.setPosition(round(self.timeline_time_value * 1000)); self.timeline_player.play()
        if self.timeline_has_bgm:
            self.timeline_bgm_player.setPosition(round(self.timeline_time_value * 1000)); self.timeline_bgm_player.play()
        if not self.timeline_has_audio and not self.timeline_has_bgm: self.timeline_clock.start()
        self.timeline_playing = True; self.timeline_play.setText("Pause"); self.timeline_timer.start()
    def pause_timeline(self):
        if hasattr(self, "timeline_player") and self.timeline_has_audio: self.timeline_player.pause()
        if hasattr(self, "timeline_bgm_player") and self.timeline_has_bgm: self.timeline_bgm_player.pause()
        if hasattr(self, "timeline_timer"): self.timeline_timer.stop()
        if hasattr(self, "timeline_play"): self.timeline_play.setText("Play")
        self.timeline_playing = False
    def scrub_timeline(self, value):
        self.set_timeline_time(value / 1000, True)
        if self.timeline_playing and not self.timeline_has_audio and not self.timeline_has_bgm: self.timeline_clock.start()
    def update_timeline(self):
        if not self.timeline_playing: return
        seconds = self.timeline_player.position() / 1000 if self.timeline_has_audio else self.timeline_bgm_player.position() / 1000 if self.timeline_has_bgm else self.timeline_time_value + self.timeline_clock.elapsed() / 1000
        if not self.timeline_has_audio and not self.timeline_has_bgm: self.timeline_clock.restart()
        if seconds >= self.timeline_preview.duration(): self.set_timeline_time(self.timeline_preview.duration(), True); self.pause_timeline(); return
        self.set_timeline_time(seconds)
    def next_render_path(self, preview):
        output_dir=Path.cwd()/"output"; output_dir.mkdir(parents=True,exist_ok=True); stem="preview_240p" if preview else "final"; index=1
        while (output_dir/f"{stem}_{index:03d}.mp4").exists(): index+=1
        return str(output_dir/f"{stem}_{index:03d}.mp4")
    def start_render(self,preview):
        default_path=self.next_render_path(preview)
        p=QFileDialog.getSaveFileName(self,"Export video",default_path,"MP4 (*.mp4)")[0]
        if not p:return
        self.cancel_event=threading.Event(); self.worker=Worker(lambda report:render(copy.deepcopy(self.project),p,preview,report,self.cancel_event)); self.worker.progress.connect(self.progress.setValue); self.worker.done.connect(lambda _:self.render_finished(p)); self.worker.failed.connect(self.error); self.worker.start(); self.cancel.setEnabled(True); self.status.setText("Rendering…")
    def cancel_render(self):
        if hasattr(self,"cancel_event"): self.cancel_event.set(); self.status.setText("Cancelling render…")
    def render_finished(self,path): self.cancel.setEnabled(False); self.status.setText(f"Rendered: {path}")
    def caption_settings(self):
        self.set_settings_tab(3); self.caption_style_section.setFocus()
        QTimer.singleShot(0,lambda:self.captions_scroll.ensureWidgetVisible(self.caption_style_section))
    def error(self,message): logging.error(message); self.cancel.setEnabled(False); QMessageBox.critical(self,"Manhwa Automation",message); self.status.setText("Operation failed — see message.")

def run():
    configure_logging(); app=QApplication(sys.argv); app.setStyle("Fusion"); app.setWindowIcon(app_logo()); configure_dark_palette(app); style_path=Path(__file__).with_name("styles.qss"); icons=(asset_root()/"assets"/"icons").as_posix(); app.setStyleSheet(style_path.read_text(encoding="utf-8").replace("{ICONS}",icons))
    w=MainWindow(); w.showMaximized(); sys.exit(app.exec())
