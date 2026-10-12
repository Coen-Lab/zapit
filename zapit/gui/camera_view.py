"""Live camera view in sample millimetres with named overlay layers.

The image is a pyqtgraph ``ImageItem`` placed at its mm extent inside a
``ViewBox`` with the y axis pointing down, so row 0 is at the top and the axes
read in sample mm (origin at the full-sensor centre). Overlays are addressed by
name: scatter layers (``set_points``), polylines (``set_curve``), text labels
(``set_labels``) and one draggable ROI rectangle. Mouse clicks and moves are
re-emitted as mm coordinates.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF, Qt, pyqtSignal

pg.setConfigOptions(imageAxisOrder="row-major", antialias=False, background="k", foreground="w")

# Default styles per overlay layer: (symbol size px, pen colour, brush colour or None)
DEFAULT_STYLES = {
    "calib_current": dict(size=18, pen=pg.mkPen("r", width=3), brush=None),
    "calib_all": dict(size=14, pen=pg.mkPen("g", width=2), brush=None),
    "bregma": dict(size=12, pen=pg.mkPen("r", width=2), brush=None),
    "last_point": dict(size=16, pen=pg.mkPen("y", width=2), brush=None, symbol="+"),
    "last_detected": dict(size=12, pen=pg.mkPen("c", width=2), brush=None),
    "stim_sites": dict(size=10, pen=pg.mkPen("w", width=1), brush=None),
}


class CameraView(pg.GraphicsLayoutWidget):
    """Image in mm plus named overlays. Signals carry sample-mm (x, y)."""

    clicked = pyqtSignal(float, float)      # left click inside the image
    moved = pyqtSignal(float, float)        # cursor moved inside the image

    def __init__(self, parent=None):
        super().__init__(parent)
        self.plot = self.addPlot()
        self.vb = self.plot.getViewBox()
        self.vb.setAspectLocked(True)
        self.vb.invertY(True)
        self.vb.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()
        self.plot.setLabel("bottom", "x (mm)")
        self.plot.setLabel("left", "y (mm)")
        self.plot.showGrid(x=True, y=True, alpha=0.3)

        self.image = pg.ImageItem()
        self.image.setZValue(-10)
        self.plot.addItem(self.image)
        self.extent: tuple[float, float, float, float] | None = None

        self._scatters: dict[str, pg.ScatterPlotItem] = {}
        self._curves: dict[str, pg.PlotCurveItem] = {}
        self._labels: dict[str, list[pg.TextItem]] = {}

        self.roi = pg.RectROI([0, 0], [1, 1], pen=pg.mkPen("y", width=2), movable=True, resizable=True)
        self.roi.setZValue(20)
        self.roi.hide()
        self.plot.addItem(self.roi)

        self.scene().sigMouseMoved.connect(self._on_scene_move)
        self.scene().sigMouseClicked.connect(self._on_scene_click)

    # ------------------------------------------------------------------ image
    def set_frame(self, frame: np.ndarray, extent: tuple[float, float, float, float]):
        """Show ``frame`` ([row, col]) covering ``extent`` = (x0, y0, width, height) mm."""
        self.image.setImage(frame, autoLevels=False, levels=(0, 255))
        if extent != self.extent:
            self.extent = tuple(float(v) for v in extent)
            x0, y0, w, h = self.extent
            rect = QRectF(x0, y0, w, h)
            self.image.setRect(rect)
            self.roi.maxBounds = rect
            self.vb.setRange(rect, padding=0.02)

    def image_rect(self) -> QRectF | None:
        if self.extent is None:
            return None
        return QRectF(*self.extent)

    def contains(self, x: float, y: float) -> bool:
        r = self.image_rect()
        return r is not None and r.contains(x, y)

    # ------------------------------------------------------------------ overlays
    def set_points(self, name: str, xy, brush=None, pen=None, size=None, symbol=None, data=None, tip=None):
        """Scatter layer ``name`` at ``xy`` ((N, 2) mm). Empty ``xy`` clears it."""
        item = self._scatters.get(name)
        if item is None:
            style = dict(DEFAULT_STYLES.get(name, dict(size=10, pen=pg.mkPen("w", width=1), brush=None)))
            kwargs = dict(size=style["size"], pen=style["pen"], brush=style.get("brush"),
                          symbol=style.get("symbol", "o"), pxMode=True)
            if tip is not None:
                kwargs.update(hoverable=True, tip=tip)
            item = pg.ScatterPlotItem(**kwargs)
            item.setZValue(10)
            self.plot.addItem(item)
            self._scatters[name] = item
        pts = np.atleast_2d(np.asarray(xy, dtype=float)) if xy is not None and len(xy) else np.zeros((0, 2))
        kwargs = {}
        if brush is not None:
            kwargs["brush"] = brush
        if pen is not None:
            kwargs["pen"] = pen
        if size is not None:
            kwargs["size"] = size
        if symbol is not None:
            kwargs["symbol"] = symbol
        if data is not None:
            kwargs["data"] = list(data)
        item.setData(x=pts[:, 0], y=pts[:, 1], **kwargs)
        item.show()

    def set_curve(self, name: str, xy, pen=None, closed: bool = False):
        """Polyline layer ``name`` through ``xy`` ((N, 2) mm). Empty ``xy`` clears it."""
        item = self._curves.get(name)
        if item is None:
            item = pg.PlotCurveItem(pen=pen or pg.mkPen("c", width=2))
            item.setZValue(5)
            self.plot.addItem(item)
            self._curves[name] = item
        elif pen is not None:
            item.setPen(pen)
        pts = np.atleast_2d(np.asarray(xy, dtype=float)) if xy is not None and len(xy) else np.zeros((0, 2))
        if closed and len(pts) > 1:
            pts = np.vstack([pts, pts[:1]])
        item.setData(pts[:, 0], pts[:, 1])
        item.show()

    def set_labels(self, name: str, xy, texts, color="w"):
        """Text labels anchored at ``xy``. Replaces any existing labels in the layer."""
        self.clear_layer(name)
        items = []
        for (x, y), t in zip(np.atleast_2d(np.asarray(xy, dtype=float)), texts):
            ti = pg.TextItem(str(t), color=color, anchor=(0, 1))
            ti.setPos(float(x), float(y))
            ti.setZValue(15)
            self.plot.addItem(ti)
            items.append(ti)
        self._labels[name] = items

    def clear_layer(self, name: str):
        if name in self._scatters:
            self._scatters[name].setData(x=[], y=[])
        if name in self._curves:
            self._curves[name].setData([], [])
        for ti in self._labels.pop(name, []):
            self.plot.removeItem(ti)

    def clear_all(self):
        for name in list(self._scatters) + list(self._curves) + list(self._labels):
            self.clear_layer(name)

    def set_visible(self, name: str, visible: bool):
        for store in (self._scatters, self._curves):
            if name in store:
                store[name].setVisible(visible)
        for ti in self._labels.get(name, []):
            ti.setVisible(visible)

    def has_layer(self, name: str) -> bool:
        return name in self._scatters or name in self._curves or name in self._labels

    def layer_points(self, name: str) -> np.ndarray:
        """(N, 2) data currently shown in a scatter layer (for tests and status text)."""
        item = self._scatters.get(name)
        if item is None:
            return np.zeros((0, 2))
        return np.c_[item.data["x"], item.data["y"]]

    # ------------------------------------------------------------------ ROI
    def show_roi(self, rect_mm: tuple[float, float, float, float] | None = None, inset_mm: float = 1.0):
        """Show the draggable ROI, by default the image inset by ``inset_mm`` on each side."""
        if rect_mm is None:
            if self.extent is None:
                return
            x0, y0, w, h = self.extent
            inset = min(inset_mm, w / 4, h / 4)
            rect_mm = (x0 + inset, y0 + inset, w - 2 * inset, h - 2 * inset)
        x, y, w, h = rect_mm
        self.roi.setPos((x, y))
        self.roi.setSize((w, h))
        self.roi.show()

    def hide_roi(self):
        self.roi.hide()

    def roi_visible(self) -> bool:
        return self.roi.isVisible()

    def roi_rect_mm(self) -> tuple[float, float, float, float]:
        pos, size = self.roi.pos(), self.roi.size()
        return float(pos.x()), float(pos.y()), float(size.x()), float(size.y())

    # ------------------------------------------------------------------ mouse
    def _scene_to_mm(self, scene_pos):
        if not self.vb.sceneBoundingRect().contains(scene_pos):
            return None
        p = self.vb.mapSceneToView(scene_pos)
        x, y = float(p.x()), float(p.y())
        if not self.contains(x, y):
            return None
        return x, y

    def _on_scene_move(self, scene_pos):
        xy = self._scene_to_mm(scene_pos)
        if xy is not None:
            self.moved.emit(*xy)

    def _on_scene_click(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        xy = self._scene_to_mm(ev.scenePos())
        if xy is not None:
            self.clicked.emit(*xy)
