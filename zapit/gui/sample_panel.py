"""The "Calibrate sample" tab: bregma/reference calibration, stimulus overlays, demo patterns and export.

Sample calibration is an interactive flow driven by clicks on the camera view:
the first click places bregma and the atlas outline, the cursor then rotates and
scales the outline so the stereotaxic reference point (0, refAP) sits under it,
and the second click fixes the calibration. ``confirm`` can be replaced to skip
the Yes/No dialog (tests do this).
"""
from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import (QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QVBoxLayout, QWidget)

from zapit.pointer import NotReadyError
from zapit.transforms import rotate_and_scale

REF_AP_VALUES = list(range(5, 1, -1)) + list(range(-2, -9, -1))
MIN_REF_DISTANCE_MM = 0.5


class SamplePanel(QWidget):
    """Widgets and behaviour of the sample calibration tab."""

    def __init__(self, model, view, window):
        super().__init__()
        self.model = model
        self.view = view
        self.window = window
        self.stage = 0                      # 0 idle, 1 awaiting bregma, 2 awaiting the reference point
        self._bregma: np.ndarray | None = None
        self.confirm: Callable[[], bool] = self._confirm_dialog
        self._build()
        self.refresh_from_settings()
        self.update_enabled()

    # ------------------------------------------------------------------ layout
    def _build(self):
        root = QVBoxLayout(self)

        calib = QGroupBox("Sample calibration")
        grid = QGridLayout(calib)
        self.calibrate_btn = QPushButton("Calibrate sample")
        self.calibrate_btn.clicked.connect(self.start_calibration)
        self.ref_ap = QComboBox()
        for v in REF_AP_VALUES:
            self.ref_ap.addItem(f"{v:+d} mm", v)
        self.ref_ap.currentIndexChanged.connect(self._ref_ap_changed)
        self.instructions = QLabel("")
        self.instructions.setWordWrap(True)
        grid.addWidget(self.calibrate_btn, 0, 0)
        grid.addWidget(QLabel("Reference AP"), 0, 1)
        grid.addWidget(self.ref_ap, 0, 2)
        grid.addWidget(self.instructions, 1, 0, 1, 3)
        root.addWidget(calib)

        demo = QGroupBox("Check on the sample")
        dgrid = QGridLayout(demo)
        self.paint_border_btn = QPushButton("Paint brain border")
        self.overlay_btn = QPushButton("Overlay stim sites")
        self.zap_all_btn = QPushButton("Zap all coords")
        self.zap_site_btn = QPushButton("Zap site")
        self.paint_area_btn = QPushButton("Paint area")
        for b in (self.paint_border_btn, self.overlay_btn, self.zap_all_btn, self.zap_site_btn, self.paint_area_btn):
            b.setCheckable(True)
        self.condition = QComboBox()
        self.area = QComboBox()
        for a in self.model.atlas.areas:
            self.area.addItem(a.name, a.index)
        dgrid.addWidget(self.paint_border_btn, 0, 0)
        dgrid.addWidget(self.overlay_btn, 0, 1)
        dgrid.addWidget(self.zap_all_btn, 1, 0)
        dgrid.addWidget(self.zap_site_btn, 2, 0)
        dgrid.addWidget(self.condition, 2, 1)
        dgrid.addWidget(self.paint_area_btn, 3, 0)
        dgrid.addWidget(self.area, 3, 1)
        root.addWidget(demo)
        self.paint_border_btn.toggled.connect(self._paint_border_toggled)
        self.overlay_btn.toggled.connect(self._overlay_toggled)
        self.zap_all_btn.toggled.connect(self._zap_all_toggled)
        self.zap_site_btn.toggled.connect(self._zap_site_toggled)
        self.paint_area_btn.toggled.connect(self._paint_area_toggled)
        self.condition.currentIndexChanged.connect(self._condition_changed)
        self.area.currentIndexChanged.connect(self._area_changed)
        for name, b in (("paint_border", self.paint_border_btn), ("zap_all", self.zap_all_btn),
                        ("zap_site", self.zap_site_btn), ("paint_area", self.paint_area_btn)):
            self.window.register_toggle(name, b)

        exp = QGroupBox("Experiment")
        egrid = QGridLayout(exp)
        self.export_btn = QPushButton("Export waveforms")
        self.export_btn.clicked.connect(self.export_waveforms)
        self.set_path_btn = QPushButton("Set experiment path")
        self.clear_path_btn = QPushButton("Clear path")
        self.set_path_btn.clicked.connect(self.choose_experiment_path)
        self.clear_path_btn.clicked.connect(lambda: self.model.set_experiment_path(None))
        self.path_field = QLineEdit()
        self.path_field.setReadOnly(True)
        self.path_field.setPlaceholderText("no experiment path")
        egrid.addWidget(self.export_btn, 0, 0, 1, 2)
        row = QHBoxLayout()
        row.addWidget(self.set_path_btn)
        row.addWidget(self.clear_path_btn)
        egrid.addLayout(row, 1, 0, 1, 2)
        egrid.addWidget(self.path_field, 2, 0, 1, 2)
        root.addWidget(exp)
        root.addStretch(1)

    # ------------------------------------------------------------------ state
    def refresh_from_settings(self):
        v = int(round(float(self.model.settings["calibrateSample"]["refAP"])))
        idx = self.ref_ap.findData(v)
        if idx >= 0:
            self.ref_ap.blockSignals(True)
            self.ref_ap.setCurrentIndex(idx)
            self.ref_ap.blockSignals(False)

    def update_enabled(self):
        sample = self.model.sample_calibrated
        cfg = self.model.stim_config is not None
        self.paint_border_btn.setEnabled(sample)
        self.paint_area_btn.setEnabled(sample)
        self.area.setEnabled(sample)
        for w in (self.overlay_btn, self.zap_all_btn, self.zap_site_btn, self.condition, self.export_btn):
            w.setEnabled(sample and cfg)

    def on_sample_calibrated(self, on: bool):
        if on:
            self.view.set_curve("outline_calibrated", self.model.calibrated_brain_outline(),
                                pen=pg.mkPen("g", width=2), closed=True)
        else:
            self.view.clear_layer("outline_calibrated")
            self.view.clear_layer("stim_sites")
            self.view.clear_layer("stim_labels")
            if self.overlay_btn.isChecked():
                self.overlay_btn.setChecked(False)
        self.update_enabled()

    def on_stim_config(self, cfg):
        self.condition.blockSignals(True)
        self.condition.clear()
        if cfg is not None:
            for i, c in enumerate(cfg.conditions):
                self.condition.addItem(f"{i + 1}: {c.type} ({c.n_points} pt, {c.laser_power_mw:g} mW)", i + 1)
        self.condition.blockSignals(False)
        self.update_enabled()
        if self.overlay_btn.isChecked():
            self._draw_stim_sites()

    def on_experiment_path(self, path):
        self.path_field.setText(str(path) if path else "")

    def _ref_ap_changed(self, _idx: int):
        v = self.ref_ap.currentData()
        if v is None or v == self.model.settings["calibrateSample"]["refAP"]:
            return
        self.model.update_setting("calibrateSample", "refAP", float(v))
        if self.model.sample_calibrated:
            # The existing two-point calibration was taken against the old reference AP.
            self.model.wipe_sample_calib()
            self.window.set_status("Reference AP changed: calibrate the sample again")

    # ------------------------------------------------------------------ sample calibration flow
    def start_calibration(self):
        """Begin the two-click flow. Clicks and moves arrive via ``on_view_clicked``/``on_view_moved``."""
        self.window.release_toggles()
        if self.overlay_btn.isChecked():
            self.overlay_btn.setChecked(False)
        self.model.wipe_sample_calib()
        self.view.clear_layer("outline_calibrated")
        self.view.clear_layer("outline_temp")
        self.view.clear_layer("bregma")
        self._bregma = None
        self.stage = 1
        self.instructions.setText("Click on bregma")
        self.window.set_status("Sample calibration: click on bregma")

    def cancel_calibration(self):
        if self.stage:
            self.stage = 0
            self._bregma = None
            self.view.clear_layer("outline_temp")
            self.view.clear_layer("bregma")
            self.instructions.setText("")

    def on_view_moved(self, x: float, y: float):
        if self.stage == 1:
            self.view.set_curve("outline_temp", self.model.atlas.whole_brain + [x, y], closed=True)
        elif self.stage == 2:
            outline = self._outline_for_reference(x, y)
            if outline is not None:
                self.view.set_curve("outline_temp", outline, closed=True)

    def on_view_clicked(self, x: float, y: float):
        if self.stage == 1:
            self._bregma = np.array([x, y], dtype=float)
            self.view.set_points("bregma", [[x, y]])
            self.view.set_curve("outline_temp", self.model.atlas.whole_brain + [x, y], closed=True)
            self.stage = 2
            ap = self.model.settings["calibrateSample"]["refAP"]
            self.instructions.setText(f"Move to the midline point at AP {ap:+g} mm and click")
            self.window.set_status(self.instructions.text())
        elif self.stage == 2:
            if self._outline_for_reference(x, y) is None:
                return
            self.stage = 0
            self.instructions.setText("")
            ref = np.array([self._bregma, [x, y]], dtype=float)
            self.model.calibrate_sample(ref)
            self.view.clear_layer("outline_temp")
            self.view.clear_layer("bregma")
            if self.confirm():
                self.window.set_status("Sample calibrated")
                if self.model.stim_config is not None:
                    self.overlay_btn.setChecked(True)
            else:
                self.model.wipe_sample_calib()
                self.window.set_status("Sample calibration discarded")

    def _outline_for_reference(self, x: float, y: float) -> np.ndarray | None:
        if self._bregma is None or np.hypot(*(self._bregma - [x, y])) < MIN_REF_DISTANCE_MM:
            return None
        ref = np.array([self._bregma, [x, y]], dtype=float)
        return rotate_and_scale(self.model.atlas.whole_brain, self.model.ref_points_stereotaxic, ref)

    def _confirm_dialog(self) -> bool:
        return self.window.ask("Sample calibration", "Are you happy with the calibration?")

    # ------------------------------------------------------------------ overlays and patterns
    def _draw_stim_sites(self):
        try:
            per_condition = self.model.calibrated_points()
        except NotReadyError:
            self.view.clear_layer("stim_sites")
            return
        n = len(per_condition)
        xy, brushes, data, label_xy, labels = [], [], [], [], []
        for i, pts in enumerate(per_condition):
            colour = pg.intColor(i, hues=max(n, 1))
            for p in pts:
                xy.append(p)
                brushes.append(pg.mkBrush(colour))
                data.append(i + 1)
            label_xy.append(pts[0])
            labels.append(str(i + 1))
        self.view.set_points("stim_sites", xy, brush=brushes, data=data,
                             tip=lambda x, y, data: f"Condition {data}")
        self.view.set_labels("stim_labels", label_xy, labels)

    def _overlay_toggled(self, on: bool):
        if on:
            if self.model.stim_config is None or not self.model.sample_calibrated:
                self.overlay_btn.setChecked(False)
                return
            self._draw_stim_sites()
        else:
            self.view.clear_layer("stim_sites")
            self.view.clear_layer("stim_labels")

    def _pattern_toggle(self, name: str, button: QPushButton, on: bool, action: Callable[[], None]):
        if on:
            self.window.release_toggles(except_name=name)
            try:
                action()
            except (NotReadyError, ValueError, IndexError) as e:
                self.window.notify(button.text(), str(e), "warning")
                button.setChecked(False)
        else:
            self.model.stop_pattern()

    def _paint_border_toggled(self, on: bool):
        self._pattern_toggle("paint_border", self.paint_border_btn, on, self.model.draw_brain_outline)

    def _zap_all_toggled(self, on: bool):
        self._pattern_toggle("zap_all", self.zap_all_btn, on, self.model.zap_all_coords)

    def _zap_site_toggled(self, on: bool):
        self._pattern_toggle("zap_site", self.zap_site_btn, on, self._zap_selected_site)

    def _zap_selected_site(self):
        n = self.condition.currentData()
        if n is None:
            raise NotReadyError("No condition selected")
        self.model.zap_site(int(n))

    def _condition_changed(self, _idx: int):
        if self.zap_site_btn.isChecked():
            self.model.stop_pattern()
            self._zap_selected_site()

    def _paint_area_toggled(self, on: bool):
        self._pattern_toggle("paint_area", self.paint_area_btn, on, self._paint_selected_area)

    def _paint_selected_area(self):
        idx = self.area.currentData()
        if idx is None:
            raise NotReadyError("No area selected")
        self.model.draw_areas([int(idx)])

    def _area_changed(self, _idx: int):
        if self.paint_area_btn.isChecked():
            self.model.stop_pattern()
            self._paint_selected_area()

    # ------------------------------------------------------------------ export and paths
    def export_waveforms(self, path: str | Path | None = None):
        """Save one modulation cycle per condition as .npz and the condition metadata as .csv."""
        cfg = self.model.stim_config
        if cfg is None or not self.model.sample_calibrated:
            return None
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Export waveforms", "zapit_waveforms.npz", "NumPy (*.npz)")
            if not path:
                return None
        path = Path(path).with_suffix(".npz")
        try:
            samples = self.model.chan_samples()
        except (NotReadyError, ValueError) as e:
            self.window.notify("Export waveforms", str(e), "warning")
            return None
        np.savez(path, waveforms=samples, sample_rate=float(self.model.daq.sample_rate),
                 modulation_hz=float(cfg.modulation_hz), columns=np.array(["x_volts", "y_volts", "laser_gate", "trigger"]))
        csv_path = path.with_suffix(".csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["condition", "type", "n_points", "ML_mm", "AP_mm", "laser_power_mW", "rampdown_ms", "ephys"])
            for i, c in enumerate(cfg.conditions):
                w.writerow([i + 1, c.type, c.n_points, ";".join(f"{v:g}" for v in c.ml),
                            ";".join(f"{v:g}" for v in c.ap), c.laser_power_mw, c.rampdown_ms, int(c.ephys)])
        self.window.set_status(f"Exported {path.name} and {csv_path.name}")
        return path

    def choose_experiment_path(self):
        d = QFileDialog.getExistingDirectory(self, "Experiment directory", str(self.model.experiment_path or ""))
        if d:
            self.model.set_experiment_path(d)
