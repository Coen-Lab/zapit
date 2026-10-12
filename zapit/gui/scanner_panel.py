"""The "Calibrate scanners" tab: camera exposure and ROI, scanner calibration, beam pointing and the laser.

The panel owns its widgets and the logic behind them. It talks to the model
directly for hardware actions, to the ``CameraView`` for overlays and to the
``MainWindow`` for toggle exclusivity, the trial lock, notifications and the
status line. Every control that moves the beam or sets the laser is disabled
while the window reports a trial in progress (``window.trial_locked``).
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
TRIAL_NOTE = " Disabled while a trial is armed or playing."


def lamp(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lab.setStyleSheet(LAMP_OFF)
    return lab


def set_lamp(lab: QLabel, on: bool):
    lab.setStyleSheet(LAMP_ON if on else LAMP_OFF)
    lab.setProperty("lit", bool(on))


def set_checked_quietly(button: QPushButton, on: bool):
    """Change a toggle's checked state without running its handler."""
    button.blockSignals(True)
    button.setChecked(bool(on))
    button.blockSignals(False)


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
        self.update_enabled()

    # ------------------------------------------------------------------ layout
    def _build(self):
        s = self.model.settings
        root = QVBoxLayout(self)

        cam = QGroupBox("Camera")
        form = QFormLayout(cam)
        self.exposure = QDoubleSpinBox()
        self.exposure.setRange(0, 1e6)
        self.exposure.setDecimals(0)
        self.exposure.setSuffix(" us")
        self.exposure.setToolTip("Camera exposure for the live view, in microseconds, saved to the settings.")
        self.exposure.valueChanged.connect(self._exposure_changed)
        form.addRow("Exposure", self.exposure)
        roi_row = QHBoxLayout()
        self.set_roi_btn = QPushButton("Set ROI")
        self.apply_roi_btn = QPushButton("Apply ROI")
        self.reset_roi_btn = QPushButton("Reset ROI")
        self.set_roi_btn.setToolTip("Show a rectangle on the image to choose a camera region of interest.")
        self.apply_roi_btn.setToolTip("Crop the camera to the rectangle; this clears the scanner calibration. "
                                      "Needs Set ROI first.")
        self.reset_roi_btn.setToolTip("Return to the full sensor; this clears the scanner calibration. "
                                      "Needs an ROI to be applied.")
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
        self.run_btn.setToolTip("Sweep the beam over a grid with the laser at the slider's power, find it on "
                                "the camera and fit the scanner transform. Press again to cancel." + TRIAL_NOTE)
        self.check_btn = QPushButton("Check calibration")
        self.check_btn.setCheckable(True)
        self.check_btn.setToolTip("Slowly revisit the calibration points with the laser on at the slider's "
                                  "power. Needs a scanner calibration." + TRIAL_NOTE)
        self.point_btn = QPushButton("Point mode")
        self.point_btn.setCheckable(True)
        self.point_btn.setToolTip("Switches the laser on. Click on the image to send the beam there and "
                                  "measure the pointing error." + TRIAL_NOTE)
        self.cat_btn = QPushButton("Cat & mouse")
        self.cat_btn.setCheckable(True)
        self.cat_btn.setToolTip("Switches the laser on; the beam follows the mouse cursor over the image."
                                + TRIAL_NOTE)
        grid.addWidget(self.run_btn, 0, 0)
        grid.addWidget(self.check_btn, 0, 1)
        grid.addWidget(self.point_btn, 1, 0)
        grid.addWidget(self.cat_btn, 1, 1)
        self.calib_result = QLabel("Not calibrated")
        self.calib_result.setWordWrap(True)
        self.calib_result.setToolTip("The last calibration's result. A good fit leaves a residual of tens of "
                                     "microns; above 100 um it is rejected")
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
        self.laser_btn.setToolTip("Switch the laser on at the power set by the slider; it also shows when a "
                                  "check pattern is firing the laser." + TRIAL_NOTE)
        self.laser_btn.toggled.connect(self._laser_toggled)
        self.power = QSlider(Qt.Orientation.Horizontal)
        self.power.setRange(0, int(round(float(s["laser"]["maxValueInGUI"]) * 10)))
        self.power.setToolTip("Laser power in mW for pointing, the scanner calibration and every check "
                              "pattern, saved to the settings (calibrateScanners.calibration_power_mW). Its "
                              "maximum is laser.maxValueInGUI. Trials use their config's power." + TRIAL_NOTE)
        self.power.valueChanged.connect(self._power_changed)
        self.power_label = QLabel("0.0 mW")
        self.power_label.setToolTip("The slider's laser power")
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
        self.spacing.setToolTip("Distance between neighbouring points of the calibration grid; smaller "
                                "gives more points and a slower calibration. Saved to the settings.")
        self.buffer = QDoubleSpinBox()
        self.buffer.setRange(-50, 50)
        self.buffer.setSingleStep(0.5)
        self.buffer.setSuffix(" mm")
        self.buffer.setToolTip("Margin kept free of calibration points at the image edges. Saved to the "
                               "settings.")
        self.threshold = QSpinBox()
        self.threshold.setRange(1, 1_000_000)
        self.threshold.setSuffix(" px")
        self.threshold.setToolTip("Smallest bright area, in pixels, accepted as the beam spot; raise it if "
                                  "reflections are taken for the beam. Saved to the settings.")
        self.calib_exposure = QDoubleSpinBox()
        self.calib_exposure.setRange(0, 1e6)
        self.calib_exposure.setDecimals(0)
        self.calib_exposure.setSuffix(" us")
        self.calib_exposure.setToolTip("Camera exposure, in microseconds, while the calibration looks for the "
                                       "beam; short, so only the spot is bright. Saved to the settings.")
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

    # ------------------------------------------------------------------ enabled state
    def update_enabled(self):
        """Enable the controls that are usable now: calibrated, and no trial owning the outputs."""
        free = not self.window.trial_locked
        self.run_btn.setEnabled(free or self.calibration_running())
        self.check_btn.setEnabled(free and self.model.scanners_calibrated and bool(self.model.calibration_data))
        for w in (self.point_btn, self.cat_btn, self.laser_btn, self.power):
            w.setEnabled(free)

    def drop_beam_controls(self):
        """A trial took the outputs: show pointing and the laser as off without touching the hardware."""
        for b in (self.point_btn, self.cat_btn):
            set_checked_quietly(b, False)
        self.show_laser(False)
        self.view.clear_layer("last_point")
        self.view.clear_layer("last_detected")

    # ------------------------------------------------------------------ ROI
    def start_roi(self):
        self.window.release_toggles()
        self.view.show_roi()
        self.apply_roi_btn.setEnabled(True)

    def apply_roi(self):
        if not self.view.roi_visible():
            return
        roi = self.model.roi_from_mm(*self.view.roi_rect_mm())
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
            if self.calibration_running():
                # The previous run is still stopping; never leave the button checked under the wrong label.
                set_checked_quietly(self.run_btn, False)
                self.run_btn.setText(RUN_TEXT)
                self.window.set_status("The cancelled calibration is still stopping; press Run calibration "
                                       "again in a moment")
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
            if self.calibration_running():
                self.model.abort_calibration = True
                self.calib_result.setText("Cancelling...")
            self.run_btn.setText(RUN_TEXT)

    def on_calibration_point(self, m):
        """Live overlay of each detected spot (queued from the worker thread)."""
        self._calib_points.append(np.asarray(m.actual_mm, dtype=float))
        pts = np.vstack(self._calib_points)
        self.view.set_points("calib_all", pts)
        self.view.set_points("calib_current", pts[-1:])
        self.calib_result.setText(f"Calibrating: {len(pts)} points, last error {m.error_um:.0f} um")

    def _finish_calib(self):
        set_checked_quietly(self.run_btn, False)
        self.run_btn.setText(RUN_TEXT)
        self.view.clear_layer("calib_current")
        self.update_enabled()
        self.window.refresh_calibration_layers()

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
            self.window.set_status("Scanners calibrated")
        elif self.model.abort_calibration:
            self.calib_result.setText("Calibration cancelled")
            self.view.clear_layer("calib_all")
            self.window.set_status("Scanner calibration cancelled")
        else:
            self.calib_result.setText("Calibration failed: too few spots were detected, or the fit residual was "
                                      "too high (a mirrored view needs scanners.invertXscanner or "
                                      "invertYscanner). The console has the details.")
            self.view.clear_layer("calib_all")
            self.window.notify("Scanner calibration", self.calib_result.text(), "warning", modal=False)

    def _calib_failed(self, msg: str):
        self._finish_calib()
        self.calib_result.setText(f"Calibration error: {msg}")
        kind = "warning" if msg.startswith(NotReadyError.__name__) else "critical"
        self.window.notify("Scanner calibration", msg, kind, modal=False)

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
                self.window.not_ready("Check calibration", e)
                set_checked_quietly(self.check_btn, False)
                return
            self.window.pattern_started()
        else:
            self.window.stop_pattern()

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
            try:
                self.model.move_beam_mm([x, y])
            except NotReadyError as e:
                self.window.not_ready("Cat & mouse", e, explicit=False)

    def point_beam(self, x: float, y: float):
        """Send the beam to (x, y) mm, then look for it on the camera and report the error."""
        try:
            self.model.move_beam_mm([x, y])
        except NotReadyError as e:
            self.window.not_ready("Point mode", e)
            return
        self.view.set_points("last_point", [[x, y]])
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
    def show_laser(self, on: bool):
        """Show the laser state on the button without commanding the laser."""
        set_checked_quietly(self.laser_btn, on)
        self.laser_btn.setText("Laser: On" if on else "Laser: Off")

    def _laser_toggled(self, on: bool):
        if not on and self.window.pattern_running():
            # The button showed a check pattern's laser: switching it off stops the pattern.
            self.window.release_toggles()
            return
        self.laser_btn.setText("Laser: On" if on else "Laser: Off")
        try:
            self.model.set_laser_mw(self.power.value() / 10 if on else 0.0)
        except NotReadyError as e:
            self.show_laser(False)
            self.window.not_ready("Laser", e)

    def _power_changed(self, v: int):
        mw = v / 10
        self.power_label.setText(f"{mw:.1f} mW")
        self._setting("calibrateScanners", "calibration_power_mW", float(mw))
        if self.laser_btn.isChecked() and not self.window.pattern_running():
            try:
                self.model.set_laser_mw(mw)
            except NotReadyError as e:
                self.window.not_ready("Laser", e, explicit=False)

    def set_laser(self, on: bool):
        self.laser_btn.setChecked(bool(on))
