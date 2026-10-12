"""Laser power calibration dialog: sweep the control voltage, plot the photodiode response and save the fit.

The sweep (``Zapit.generate_laser_calibration_curve``) blocks for seconds on real
hardware (about 20 s over 0-5 V), so it runs in a ``Worker`` thread; the plot updates
when it finishes. A new fit is shown, not used: only "Save and use fit" puts it in use,
and leaving the dialog with an unsaved one asks first.
"""
from __future__ import annotations

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from zapit.gui.bridge import Worker
from zapit.laser import LaserFitRefused
from zapit.pointer import NotReadyError

RUN_TEXT = "Run sweep"
CANCEL_TEXT = "Cancel sweep"
FIT_PEN = pg.mkPen("r", width=2)
REFUSED_PEN = pg.mkPen("r", width=2, style=Qt.PenStyle.DashLine)


class LaserDialog(QDialog):
    """Run and inspect the laser control-voltage to sensor-reading curve."""

    def __init__(self, model, parent=None, before_sweep=None, on_sweep=None, ask=None, notify=None):
        """``before_sweep`` runs on the GUI thread before each sweep and returns an error text to refuse it,
        or None; the main window's ``prepare_laser_sweep`` stops a check pattern or point mode first.
        ``on_sweep(running)`` is called when a sweep starts and when it has ended: the main window locks its
        controls for the sweep's whole life, because any of them would take the laser line and corrupt the
        fit. While a sweep runs the dialog cannot be closed (Esc included); Cancel sweep stops it.
        ``ask(title, text) -> bool`` and ``notify(title, text, kind)`` default to message boxes."""
        super().__init__(parent)
        self.model = model
        self.worker: Worker | None = None
        self.before_sweep = before_sweep
        self.on_sweep = on_sweep
        self.ask = ask or self._ask
        self.notify = notify or self._notify
        self.trial_locked = False
        self.sweeping = False
        self.new_fit = None        # from the last sweep, not saved and not in use
        self.setWindowTitle("Laser calibration")
        self.resize(700, 500)

        layout = QVBoxLayout(self)
        self.plot = pg.PlotWidget()
        self.plot.setLabel("bottom", "control voltage (V)")
        self.plot.setLabel("left", "sensor reading (V)")
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.addLegend()
        self.measured = self.plot.plot([], [], pen=None, symbol="o", symbolSize=6,
                                       symbolBrush="w", name="measured")
        self.fitted = self.plot.plot([], [], pen=FIT_PEN, name="fit")
        layout.addWidget(self.plot, 1)

        self.info = QLabel()
        self.info.setWordWrap(True)
        layout.addWidget(self.info)

        row = QHBoxLayout()
        self.run_btn = QPushButton(RUN_TEXT)
        self.save_btn = QPushButton("Save and use fit")
        self.close_btn = QPushButton("Close")
        self.run_btn.setToolTip("Step the laser control voltage over laser.laserMinMaxControlVolts in 25 mV "
                                "steps, read the photodiode on AI0 at each and fit the curve (about 20 s). The "
                                "rest of the window is locked meanwhile; press again to cancel. Disabled while a "
                                "trial is armed or playing.")
        self.save_btn.setToolTip("Save the new fit as laserFit.yml beside the settings file and use it for the "
                                 "power of every later trial. Until then trials keep the fit they had.")
        self.close_btn.setToolTip("Close the dialog; refused while a sweep runs, and asks before discarding an "
                                  "unsaved fit.")
        self.plot.setToolTip("White: the photodiode reading at each control voltage. Red: the fit (dashed: a "
                             "refused one). Power is read off the red curve, so it must rise steadily.")
        self.run_btn.clicked.connect(self._run_clicked)
        self.save_btn.clicked.connect(self.save_fit)
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.run_btn)
        row.addWidget(self.save_btn)
        row.addStretch(1)
        row.addWidget(self.close_btn)
        layout.addLayout(row)
        self.show_fit(self.model.laser.fit)

    def show_fit(self, fit, new: bool = False):
        self.save_btn.setEnabled(self.new_fit is not None)
        self.fitted.setPen(FIT_PEN)
        if fit is None:
            self.measured.setData([], [])
            self.fitted.setData([], [])
            self.info.setText("No laser fit in use: the laser is assumed linear between laser.laserMinMax_mW "
                              "at laser.laserMinMaxControlVolts")
            return
        self.measured.setData(fit.control_values, fit.sensor_values)
        self.fitted.setData(fit.control_values, fit.fitted_sensor_values)
        what = (f"New fit: {fit.fit_type}, RMSE {fit.rmse:.4g}, {len(fit.control_values)} points. Not in use "
                f"until you press Save and use fit") if new else \
            (f"Fit in use: {fit.fit_type}, RMSE {fit.rmse:.4g}, {len(fit.control_values)} points, "
             f"made {fit.date_made or 'unknown'}")
        problem = None if new else fit.problem()
        if problem:   # a fit saved before the check existed, or brought over from Zapit 1.x
            what += f". WARNING: {problem}; run a new sweep"
        self.info.setText(what)

    def set_trial_locked(self, locked: bool):
        """A trial owns the outputs: no sweep until it ends."""
        self.trial_locked = bool(locked)
        self._update_run_btn()

    def _update_run_btn(self):
        self.run_btn.setText(CANCEL_TEXT if self.sweeping else RUN_TEXT)
        self.run_btn.setEnabled(self.sweeping or not self.trial_locked)

    # ------------------------------------------------------------------ sweep
    def _run_clicked(self):
        if self.sweeping:
            self.cancel_sweep()
        else:
            self.run_sweep()

    def run_sweep(self):
        if self.trial_locked or self.sweeping:
            return
        refused = self.before_sweep() if self.before_sweep is not None else None
        if refused:
            self.info.setText(refused)
            return
        self.sweeping = True
        if self.on_sweep is not None:
            self.on_sweep(True)
        self._update_run_btn()
        self.info.setText("Sweeping the laser control voltage... The other controls are locked until it ends.")
        self.worker = Worker(self.model.generate_laser_calibration_curve, parent=self)
        self.worker.done.connect(self._sweep_done, Qt.ConnectionType.QueuedConnection)
        self.worker.failed.connect(self._sweep_failed, Qt.ConnectionType.QueuedConnection)
        self.worker.start()

    def cancel_sweep(self):
        """Stop the sweep after its current step; the laser is set to 0 V and the fit in use is unchanged."""
        if not self.sweeping:
            return
        self.model.abort_laser_sweep = True
        self.run_btn.setEnabled(False)
        self.info.setText("Cancelling the sweep...")

    def _end_sweep(self):
        self.sweeping = False
        self._update_run_btn()
        if self.on_sweep is not None:
            self.on_sweep(False)

    def _sweep_done(self, fit):
        self._end_sweep()
        if fit is None:
            self.show_fit(self.model.laser.fit)
            self.info.setText("Sweep cancelled; the fit in use is unchanged")
            return
        self.new_fit = fit
        self.show_fit(fit, new=True)

    def _sweep_failed(self, msg: str):
        self._end_sweep()
        exc = self.worker.exception if self.worker is not None else None
        if isinstance(exc, LaserFitRefused):
            # Show what was measured, so the user can tell a photodiode out of the beam from a laser problem.
            self.measured.setData(exc.fit.control_values, exc.fit.sensor_values)
            self.fitted.setData(exc.fit.control_values, exc.fit.fitted_sensor_values)
            self.fitted.setPen(REFUSED_PEN)
            self.info.setText(f"Sweep refused (readings plotted, the refused fit dashed): {exc}")
        else:
            self.info.setText(f"Sweep failed: {msg}")
        expected = isinstance(exc, (LaserFitRefused, NotReadyError))
        self.notify("Laser calibration", str(exc) if expected else msg, "warning" if expected else "critical")

    def save_fit(self):
        if self.new_fit is None:
            return
        try:
            self.model.save_laser_fit(self.new_fit)
        except OSError as e:
            self.notify("Laser calibration", f"The fit was not saved and is not in use: {e}", "warning")
            return
        self.new_fit = None
        self.show_fit(self.model.laser.fit)
        self.info.setText(self.info.text() + "  (saved)")

    # ------------------------------------------------------------------ leaving
    def may_leave(self) -> bool:
        """False while a sweep runs; with an unsaved new fit, True only if the user discards it."""
        if self.sweeping:
            self.info.setText("A sweep is running: press Cancel sweep first, or wait for it to end")
            return False
        if self.new_fit is not None:
            if not self.ask("Laser calibration", "The new laser fit has not been saved and is not in use. "
                                                 "Close and discard it?"):
                return False
            self.new_fit = None
            self.show_fit(self.model.laser.fit)
        return True

    def reject(self):
        # Esc lands here, not in closeEvent: once it hid the dialog mid-sweep and freed the main window.
        if self.may_leave():
            super().reject()

    def closeEvent(self, event):
        if not self.may_leave():
            event.ignore()
            return
        super().closeEvent(event)

    def _ask(self, title: str, text: str) -> bool:
        reply = QMessageBox.question(self, title, text,
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        return reply == QMessageBox.StandardButton.Yes

    def _notify(self, title: str, text: str, kind: str = "information"):
        icon = {"warning": QMessageBox.Icon.Warning, "critical": QMessageBox.Icon.Critical}.get(
            kind, QMessageBox.Icon.Information)
        QMessageBox(icon, title, text, QMessageBox.StandardButton.Ok, self).exec()
