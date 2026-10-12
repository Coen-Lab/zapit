"""Top-down atlas plot used by the stimulus-config editor.

``AtlasView`` is a pyqtgraph PlotWidget that draws the whole-brain outline and
every dorsal area outline in stereotaxic mm (ML on x, positive right; AP on y,
positive anterior), marks bregma, and owns the three dynamic layers the editor
drives: a translucent highlight of the area under the cursor, a marker that
follows the cursor showing what the next click will add, and one scatter item
per stimulus condition. It knows nothing about conditions beyond their points
and style; the editor decides what to draw and when.

Mouse events are forwarded as Qt signals in mm so the editor can be driven and
tested without a real pointer.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPolygonF
from PyQt6.QtWidgets import QGraphicsPolygonItem, QVBoxLayout, QWidget

from zapit.atlas import Area, Atlas

# Matplotlib's tab10 palette; a condition's colour is PALETTE[index % 10].
PALETTE = ("#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
           "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf")
SYMBOLS = ("o", "s", "d", "t")
MARKER_SIZE = 14
ENLARGED_SIZE = 20
DELETE_COLOUR = "#ff0000"
X_RANGE = (-5.5, 5.5)
Y_RANGE = (-8.0, 4.0)


def condition_style(index: int) -> tuple[str, str]:
    """(colour, symbol) for the condition at ``index``; cycles through the palette."""
    return PALETTE[index % len(PALETTE)], SYMBOLS[index % len(SYMBOLS)]


class AtlasView(pg.PlotWidget):
    """Top-down brain plot with cursor, highlight and condition layers.

    Signals carry stereotaxic mm: ``mouse_moved(ml, ap)``,
    ``mouse_clicked(ml, ap, modifiers)`` (left button only) and ``mouse_left()``
    when the cursor leaves the plot area.
    """

    mouse_moved = pyqtSignal(float, float)
    mouse_clicked = pyqtSignal(float, float, object)
    mouse_left = pyqtSignal()

    def __init__(self, atlas: Atlas, parent=None):
        super().__init__(parent, background="w")
        self.atlas = atlas
        plot = self.getPlotItem()
        plot.setAspectLocked(True)
        plot.setMouseEnabled(x=False, y=False)
        plot.hideButtons()
        plot.setMenuEnabled(False)
        plot.showGrid(x=True, y=True, alpha=0.3)
        plot.setLabel("bottom", "ML [mm from bregma]")
        plot.setLabel("left", "AP [mm from bregma]")
        plot.setXRange(*X_RANGE, padding=0)
        plot.setYRange(*Y_RANGE, padding=0)

        self._draw_outlines()
        self._draw_bregma()

        self._highlight_items: list[QGraphicsPolygonItem] = []
        self._highlight_key: tuple | None = None
        self._condition_items: list[pg.ScatterPlotItem] = []
        self._enlarged: int | None = None

        self._cursor = pg.ScatterPlotItem(size=MARKER_SIZE, pxMode=True)
        self._cursor.setZValue(10)
        self.addItem(self._cursor)

        self.scene().sigMouseMoved.connect(self._on_scene_move)
        self.scene().sigMouseClicked.connect(self._on_scene_click)

    # ------------------------------------------------------------------ static layers
    def _draw_outlines(self) -> None:
        """Whole brain in black, every area outline in light grey, as two curve items."""
        wb = self.atlas.whole_brain
        self.addItem(pg.PlotCurveItem(wb[:, 0], wb[:, 1], pen=pg.mkPen("k", width=1.5)))
        segments = [ol for area in self.atlas.areas for ol in area.outlines]
        if not segments:
            return
        pts = np.vstack(segments)
        connect = np.ones(len(pts), dtype=bool)
        ends = np.cumsum([len(s) for s in segments]) - 1
        connect[ends] = False
        self.addItem(pg.PlotCurveItem(pts[:, 0], pts[:, 1], connect=connect,
                                      pen=pg.mkPen((170, 170, 170), width=1)))

    def _draw_bregma(self) -> None:
        pen = pg.mkPen("b", width=2)
        self.addItem(pg.PlotCurveItem([-0.25, 0.25], [0.0, 0.0], pen=pen))
        self.addItem(pg.PlotCurveItem([0.0, 0.0], [-0.25, 0.25], pen=pen))
        tick_pen = pg.mkPen("r", width=3)
        self._ml_tick = pg.PlotCurveItem([0.0, 0.0], [Y_RANGE[0], Y_RANGE[0] + 0.5], pen=tick_pen)
        self._ap_tick = pg.PlotCurveItem([X_RANGE[0], X_RANGE[0] + 0.5], [0.0, 0.0], pen=tick_pen)
        for item in (self._ml_tick, self._ap_tick):
            item.setVisible(False)
            self.addItem(item)

    # ------------------------------------------------------------------ dynamic layers
    def set_ticks(self, ml: float | None, ap: float | None = None) -> None:
        """Move the red edge ticks to the cursor, or hide them when ``ml`` is None."""
        visible = ml is not None
        if visible:
            self._ml_tick.setData([ml, ml], [Y_RANGE[0], Y_RANGE[0] + 0.5])
            self._ap_tick.setData([X_RANGE[0], X_RANGE[0] + 0.5], [ap, ap])
        self._ml_tick.setVisible(visible)
        self._ap_tick.setVisible(visible)

    def set_highlight(self, area: Area | None, side: int = 0) -> None:
        """Fill ``area`` translucently; ``side`` 0 both hemispheres, -1 left, +1 right.

        Rebuilt only when the (area, side) key changes, so moving within an area
        costs no graphics work.
        """
        key = (area.index if area else None, side)
        if key == self._highlight_key:
            return
        for item in self._highlight_items:
            self.removeItem(item)
        self._highlight_items = []
        if area is not None:
            colour = QColor(DELETE_COLOUR)
            fill = QColor(DELETE_COLOUR)
            fill.setAlpha(30)
            for outline in area.outlines:
                if side and len(area.outlines) > 1 and np.sign(outline[:, 0].mean()) != side:
                    continue
                poly = QPolygonF([QPointF(float(x), float(y)) for x, y in outline])
                item = QGraphicsPolygonItem(poly)
                item.setPen(pg.mkPen(colour, width=1))
                item.setBrush(pg.mkBrush(fill))
                item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                item.setZValue(1)
                self.addItem(item)
                self._highlight_items.append(item)
        self._highlight_key = key

    def set_cursor(self, points, colour: str, symbol: str, size: int = MARKER_SIZE) -> None:
        """Show the follow-the-cursor marker(s) at ``points`` (N, 2) mm, or clear with None."""
        if points is None or len(points) == 0:
            self._cursor.setData([], [])
            return
        pts = np.atleast_2d(np.asarray(points, dtype=float))
        self._cursor.setData(pts[:, 0], pts[:, 1], symbol=symbol, size=size,
                             pen=pg.mkPen(colour, width=2), brush=pg.mkBrush(None))

    def set_conditions(self, point_sets: list[np.ndarray]) -> None:
        """Redraw every condition from its (P, 2) points; style follows list position."""
        for item in self._condition_items:
            self.removeItem(item)
        self._condition_items = []
        self._enlarged = None
        for i, pts in enumerate(point_sets):
            colour, symbol = condition_style(i)
            item = pg.ScatterPlotItem(pts[:, 0], pts[:, 1], symbol=symbol, size=MARKER_SIZE,
                                      pen=pg.mkPen(colour, width=2), brush=pg.mkBrush(None),
                                      pxMode=True)
            item.setZValue(5)
            self.addItem(item)
            self._condition_items.append(item)

    def set_enlarged(self, index: int | None) -> None:
        """Enlarge the markers of one condition (the delete target), or none."""
        if index == self._enlarged:
            return
        if self._enlarged is not None and self._enlarged < len(self._condition_items):
            self._condition_items[self._enlarged].setSize(MARKER_SIZE)
        if index is not None and index < len(self._condition_items):
            self._condition_items[index].setSize(ENLARGED_SIZE)
        self._enlarged = index

    # ------------------------------------------------------------------ mouse
    def _scene_to_mm(self, scene_pos) -> tuple[float, float] | None:
        vb = self.getPlotItem().vb
        if not vb.sceneBoundingRect().contains(scene_pos):
            return None
        p = vb.mapSceneToView(scene_pos)
        return float(p.x()), float(p.y())

    def _on_scene_move(self, scene_pos) -> None:
        mm = self._scene_to_mm(scene_pos)
        if mm is None:
            self.mouse_left.emit()
        else:
            self.mouse_moved.emit(*mm)

    def _on_scene_click(self, ev) -> None:
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        mm = self._scene_to_mm(ev.scenePos())
        if mm is not None:
            self.mouse_clicked.emit(mm[0], mm[1], ev.modifiers())


class StimCoordsWindow(QWidget):
    """Non-modal plot of a config's points in stereotaxic mm on the brain outline, one colour per condition."""

    def __init__(self, atlas: Atlas, point_sets: list[np.ndarray], title: str = "Stim coords", parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(title)
        self.resize(520, 600)
        layout = QVBoxLayout(self)
        self.view = AtlasView(atlas)
        layout.addWidget(self.view)
        self.view.set_conditions([np.atleast_2d(np.asarray(p, dtype=float)) for p in point_sets])
        self.labels: list[pg.TextItem] = []
        for i, pts in enumerate(point_sets):
            colour, _ = condition_style(i)
            p = np.atleast_2d(np.asarray(pts, dtype=float))[0]
            label = pg.TextItem(str(i + 1), color=colour, anchor=(0, 1))
            label.setPos(float(p[0]), float(p[1]))
            self.view.addItem(label)
            self.labels.append(label)
