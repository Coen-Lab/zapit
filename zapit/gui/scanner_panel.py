"""The "Calibrate scanners" tab: camera exposure and ROI, scanner calibration, beam pointing and the laser.

The panel owns its widgets and the logic behind them. It talks to the model
directly for hardware actions, to the ``CameraView`` for overlays and to the
``MainWindow`` for toggle exclusivity, notifications and the status line.
"""
from __future__ import annotations

import numpy as np
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                             QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget)

from zapit.gui.bridge import Worker
from zapit.pointer import NotReadyError

LAMP_ON = "background-color: #2ecc40; color: black; padding: 3px; border-radius: 3px;"
LAMP_OFF = "background-color: #ff4136; color: black; padding: 3px; border-radius: 3px;"
RUN_TEXT = "Run calibration"
CANCEL_TEXT = "Cancel"


def lamp(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lab.setStyleSheet(LAMP_OFF)
    return lab


def set_lamp(lab: QLabel, on: bool):
    lab.setStyleSheet(LAMP_ON if on else LAMP_OFF)
    lab.setProperty("lit", bool(on))


class ScannerPanel(QWidget):
    """Widgets and behaviour of the scanner calibration tab."""

    def __init__(self, model, view, window):
        super().__init__()
        self.model = model
        self.view = view
        self.window = window
        self.calib_worker: Worker | None = None
        self._calib_points: list[np.ndarray] = []
        self._build()
        self.refresh_from_settings()
        self.update_roi_buttons()

    # ------------------------------------------------------------------ layout
    def _build(self):
        s = self.model.settings
        root = QVBoxLayout(self)

        lamps = QHBoxLayout()
        self.scanners_lamp = lamp("Scanners calibrated")
        self.sample_lamp = lamp("Sample calibrated")
        lamps.addWidget(self.scanners_lamp)
        lamps.addWidget(self.sample_lamp)
        root.addLayout(lamps)

        cam = QGroupBox("Camera")
        form = QFormLayout(cam)
        self.exposure = QDoubleSpinBox()
        self.exposure.setRange(0, 1e6)
        self.exposure.setDecimals(0)
        self.exposure.setSuffix(" us")
        self.exposure.valueChanged.connect(self._exposure_changed)
        form.addRow("Exposure", self.exposure)
        roi_row = QHBoxLayout()
        self.set_roi_btn = QPushButton("Set ROI")
        self.apply_roi_btn = QPushButton("Apply ROI")
        self.reset_roi_btn = QPushButton("Reset ROI")
        self.set_roi_btn.clicked.connect(self.start_roi)
        self.apply_roi_btn.clicked.connect(self.apply_roi)
        self.reset_roi_btn.clicked.connect(self.reset_roi)
        self.apply_roi_btn.setEnabled(False)
        for b in (self.set_roi_btn, self.apply_roi_btn, self.reset_roi_btn):
            roi_row.addWidget(b)
        form.addRow(roi_row)
        root.addWidget(cam)

        calib = QGroupBox("Scanner calibration")
        grid = QGridLayout(calib)
        self.run_btn = QPushButton(RUN_TEXT)
        self.run_btn.setCheckable(True)
        self.check_btn = QPushButton("Check calibration")
        self.check_btn.setCheckable(True)
        self.point_btn = QPushButton("Point mode")
        self.point_btn.setCheckable(True)
        self.cat_btn = QPushButton("Cat & mouse")
        self.cat_btn.setCheckable(True)
        grid.addWidget(self.run_btn, 0, 0)
        grid.addWidget(self.check_btn, 0, 1)
        grid.addWidget(self.point_btn, 1, 0)
        grid.addWidget(self.cat_btn, 1, 1)
        self.calib_result = QLabel("Not calibrated")
        self.calib_result.setWordWrap(True)
        grid.addWidget(self.calib_result, 2, 0, 1, 2)
        root.addWidget(calib)
        self.run_btn.toggled.connect(self._run_toggled)
        self.check_btn.toggled.connect(self._check_toggled)
        self.point_btn.toggled.connect(self._point_toggled)
        self.cat_btn.toggled.connect(self._cat_toggled)
        for name, b in (("run_calibration", self.run_btn), ("check_calibration", self.check_btn),
                        ("point_mode", self.point_btn), ("cat_mouse", self.cat_btn)):
            self.window.register_toggle(name, b)

        laser = QGroupBox("Laser")
        lrow = QHBoxLayout(laser)
        self.laser_btn = QPushButton("Laser: Off")
        self.laser_btn.setCheckable(True)
        self.laser_btn.toggled.connect(self._laser_toggled)
        self.power = QSlider(Qt.Orientation.Horizontal)
        self.power.setRange(0, int(round(float(s["laser"]["maxValueInGUI"]) * 10)))
        self.power.valueChanged.connect(self._power_changed)
        self.power_label = QLabel("0.0 mW")
        self.power_label.setMinimumWidth(60)
        lrow.addWidget(self.laser_btn)
        lrow.addWidget(self.power, 1)
        lrow.addWidget(self.power_label)
        root.addWidget(laser)

        params = QGroupBox("Calibration parameters")
        pform = QFormLayout(params)
        self.spacing = QDoubleSpinBox()
        self.spacing.setRange(0.1, 50)
        self.spacing.setSingleStep(0.5)
        self.spacing.setSuffix(" mm")
        self.buffer = QDoubleSpinBox()
        self.buffer.setRange(-50, 50)
        self.buffer.setSingleStep(0.5)
        self.buffer.setSuffix(" mm")
        self.threshold = QSpinBox()
        self.threshold.setRange(1, 1_000_000)
        self.threshold.setSuffix(" px")
        self.calib_exposure = QDoubleSpinBox()
        self.calib_exposure.setRange(0, 1e6)
        self.calib_exposure.setDecimals(0)
        self.calib_exposure.setSuffix(" us")
        pform.addRow("Point spacing", self.spacing)
        pform.addRow("Border buffer", self.buffer)
        pform.addRow("Size threshold", self.threshold)
        pform.addRow("Calibration exposure", self.calib_exposure)
        root.addWidget(params)
        self.spacing.valueChanged.connect(lambda v: self._setting("calibrateScanners", "pointSpacingInMM", float(v)))
        self.buffer.valueChanged.connect(lambda v: self._setting("calibrateScanners", "bufferMM", float(v)))
        self.threshold.valueChanged.connect(lambda v: self._setting("calibrateScanners", "areaThreshold", int(v)))
        self.calib_exposure.valueChanged.connect(
            lambda v: self._setting("calibrateScanners", "beam_calib_exposure", float(v)))
        root.addStretch(1)

    # ------------------------------------------------------------------ settings
    def _setting(self, section: str, key: str, value):
        if self.model.settings[section][key] != value:
            self.model.update_setting(section, key, value)

    def refresh_from_settings(self):
        """Push the settings values into the widgets without firing their callbacks."""
        s = self.model.settings
        cs = s["calibrateScanners"]
        pairs = ((self.exposure, s["camera"]["default_exposure"]), (self.spacing, cs["pointSpacingInMM"]),
                 (self.buffer, cs["bufferMM"]), (self.threshold, int(cs["areaThreshold"])),
                 (self.calib_exposure, cs["beam_calib_exposure"]),
                 (self.power, int(round(float(cs["calibration_power_mW"]) * 10))))
        for w, v in pairs:
            w.blockSignals(True)
            w.setValue(v)
            w.blockSignals(False)
        self.power_label.setText(f"{self.power.value() / 10:.1f} mW")

    def _exposure_changed(self, v: float):
        self._setting("camera", "default_exposure", float(v))
        self.model.cam.exposure = float(v)

    # ------------------------------------------------------------------ lamps
    def set_scanners_lamp(self, on: bool):
        set_lamp(self.scanners_lamp, on)
        self.check_btn.setEnabled(bool(on) and bool(self.model.calibration_data))

    def set_sample_lamp(self, on: bool):
        set_lamp(self.sample_lamp, on)

    # ------------------------------------------------------------------ ROI
    def start_roi(self):
        self.window.release_toggles()
        self.view.show_roi()
        self.apply_roi_btn.setEnabled(True)

    def apply_roi(self):
        if not self.view.roi_visible():
            return
        x, y, w, h = self.view.roi_rect_mm()
        px0 = self.model.mm_to_pixels([x, y])[0]
        mmpp = self.model.mapping.mm_per_pixel
        cx, cy, _, _ = self.model.cam.roi
        roi = (cx + px0[0], cy + px0[1], w / mmpp, h / mmpp)
        self.view.hide_roi()
        self.apply_roi_btn.setEnabled(False)
        self.view.clear_all()
        self.model.set_cam_roi(roi)
        self.update_roi_buttons()

    def reset_roi(self):
        self.view.hide_roi()
        self.apply_roi_btn.setEnabled(False)
        self.view.clear_all()
        self.model.reset_cam_roi()
        self.update_roi_buttons()

    def update_roi_buttons(self):
        self.reset_roi_btn.setEnabled(not self.model.cam.is_full_frame)

    # ------------------------------------------------------------------ scanner calibration
    def _run_toggled(self, on: bool):
        if on:
            if self.calib_worker is not None and self.calib_worker.isRunning():
                return
            self.window.release_toggles(except_name="run_calibration")
            self.view.clear_all()
            self._calib_points = []
            self.calib_result.setText("Calibrating...")
            self.run_btn.setText(CANCEL_TEXT)
            self.calib_worker = Worker(self.model.calibrate_scanners, parent=self)
            self.calib_worker.done.connect(self._calib_done, Qt.ConnectionType.QueuedConnection)
            self.calib_worker.failed.connect(self._calib_failed, Qt.ConnectionType.QueuedConnection)
            self.calib_worker.start()
        else:
            if self.calib_worker is not None and self.calib_worker.isRunning():
                self.model.abort_calibration = True
            self.run_btn.setText(RUN_TEXT)

    def on_calibration_point(self, m):
        """Live overlay of each detected spot (queued from the worker thread)."""
        self._calib_points.append(np.asarray(m.actual_mm, dtype=float))
        pts = np.vstack(self._calib_points)
        self.view.set_points("calib_all", pts)
        self.view.set_points("calib_current", pts[-1:])
        self.calib_result.setText(f"Calibrating: {len(pts)} points, last error {m.error_um:.0f} um")

    def _finish_calib(self):
        self.run_btn.blockSignals(True)
        self.run_btn.setChecked(False)
        self.run_btn.blockSignals(False)
        self.run_btn.setText(RUN_TEXT)
        self.view.clear_layer("calib_current")
        self.check_btn.setEnabled(self.model.scanners_calibrated and bool(self.model.calibration_data))

    def _calib_done(self, ok):
        self._finish_calib()
        if ok and self.model.transform is not None:
            tf = self.model.transform
            target = np.array([m.target_mm for m in self.model.calibration_data])
            actual = np.array([m.actual_mm for m in self.model.calibration_data])
            res = tf.residuals(target, actual) * 1e3
            self.calib_result.setText(
                f"Calibrated: {len(target)} points, scale {tf.scale:.3f}, rotation {tf.rotation_deg:.2f} deg, "
                f"residual mean {res.mean():.0f} um, max {res.max():.0f} um")
        elif self.model.abort_calibration:
            self.calib_result.setText("Calibration cancelled")
            self.view.clear_layer("calib_all")
        else:
            self.calib_result.setText(f"Calibration failed: only {len(self.model.calibration_data)} points detected")
            self.window.notify("Scanner calibration", self.calib_result.text(), "warning")

    def _calib_failed(self, msg: str):
        self._finish_calib()
        self.calib_result.setText(f"Calibration error: {msg}")
        self.window.notify("Scanner calibration", msg, "critical")

    def calibration_running(self) -> bool:
        return self.calib_worker is not None and self.calib_worker.isRunning()

    def wait_for_calibration(self, timeout_ms: int = 30000):
        if self.calib_worker is not None:
            self.calib_worker.wait(timeout_ms)

    # ------------------------------------------------------------------ check calibration
    def _check_toggled(self, on: bool):
        if on:
            self.window.release_toggles(except_name="check_calibration")
            try:
                self.model.check_scanner_calibration()
            except NotReadyError as e:
                self.window.notify("Check calibration", str(e), "warning")
                self.check_btn.setChecked(False)
        else:
            self.model.stop_pattern()

    # ------------------------------------------------------------------ pointing
    def _point_toggled(self, on: bool):
        if on:
            self.window.release_toggles(except_name="point_mode")
            self.laser_btn.setChecked(True)
        else:
            self.laser_btn.setChecked(False)
            self.view.clear_layer("last_point")
            self.view.clear_layer("last_detected")

    def _cat_toggled(self, on: bool):
        if on:
            self.window.release_toggles(except_name="cat_mouse")
            self.laser_btn.setChecked(True)
        else:
            self.laser_btn.setChecked(False)
            self.view.clear_layer("last_point")

    def on_view_clicked(self, x: float, y: float):
        if self.point_btn.isChecked():
            self.point_beam(x, y)

    def on_view_moved(self, x: float, y: float):
        if self.cat_btn.isChecked():
            self.view.set_points("last_point", [[x, y]])
            self.model.move_beam_mm([x, y])

    def point_beam(self, x: float, y: float):
        """Send the beam to (x, y) mm, then look for it on the camera and report the error."""
        self.view.set_points("last_point", [[x, y]])
        self.model.move_beam_mm([x, y])
        volts = self.model.mapping.mm_to_volts([x, y])[0]
        msg = f"X={x:.2f} mm ({volts[0]:.2f} V)  Y={y:.2f} mm ({volts[1]:.2f} V)"
        self.window.set_status(msg)

        def measure():
            if not self.point_btn.isChecked():
                return
            try:
                m = self.model.measure_beam_position([x, y])
            except Exception:
                m = None
            if m is None:
                self.view.clear_layer("last_detected")
                return
            self.view.set_points("last_detected", [m.actual_mm])
            self.window.set_status(f"{msg}  error {m.error_um:.0f} um")

        QTimer.singleShot(150, measure)

    # ------------------------------------------------------------------ laser
    def _laser_toggled(self, on: bool):
        self.laser_btn.setText("Laser: On" if on else "Laser: Off")
        self.model.set_laser_mw(self.power.value() / 10 if on else 0.0)

    def _power_changed(self, v: int):
        mw = v / 10
        self.power_label.setText(f"{mw:.1f} mW")
        self._setting("calibrateScanners", "calibration_power_mW", float(mw))
        if self.laser_btn.isChecked():
            self.model.set_laser_mw(mw)

    def set_laser(self, on: bool):
        self.laser_btn.setChecked(bool(on))
