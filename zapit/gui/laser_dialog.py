"""Laser power calibration dialog: sweep the control voltage, plot the photodiode response and save the fit.

The sweep (``Zapit.generate_laser_calibration_curve``) blocks for seconds on real
hardware, so it runs in a ``Worker`` thread; the plot updates when it finishes.
"""
from __future__ import annotations

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from zapit.gui.bridge import Worker


class LaserDialog(QDialog):
    """Run and inspect the laser control-voltage to sensor-reading curve."""

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.worker: Worker | None = None
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
        self.fitted = self.plot.plot([], [], pen=pg.mkPen("r", width=2), name="fit")
        layout.addWidget(self.plot, 1)

        self.info = QLabel("No laser fit loaded")
        layout.addWidget(self.info)

        row = QHBoxLayout()
        self.run_btn = QPushButton("Run sweep")
        self.save_btn = QPushButton("Save fit")
        self.close_btn = QPushButton("Close")
        self.run_btn.clicked.connect(self.run_sweep)
        self.save_btn.clicked.connect(self.save_fit)
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.run_btn)
        row.addWidget(self.save_btn)
        row.addStretch(1)
        row.addWidget(self.close_btn)
        layout.addLayout(row)
        self.show_fit(self.model.laser.fit)

    def show_fit(self, fit):
        if fit is None:
            self.measured.setData([], [])
            self.fitted.setData([], [])
            self.save_btn.setEnabled(False)
            self.info.setText("No laser fit loaded: the laser is assumed linear between its min and max")
            return
        self.measured.setData(fit.control_values, fit.sensor_values)
        self.fitted.setData(fit.control_values, fit.fitted_sensor_values)
        self.save_btn.setEnabled(True)
        self.info.setText(f"Fit: {fit.fit_type}, RMSE {fit.rmse:.4g}, {len(fit.control_values)} points, "
                          f"made {fit.date_made or 'unknown'}")

    def run_sweep(self):
        if self.worker is not None and self.worker.isRunning():
            return
        self.run_btn.setEnabled(False)
        self.info.setText("Sweeping the laser control voltage...")
        self.worker = Worker(self.model.generate_laser_calibration_curve, parent=self)
        self.worker.done.connect(self._sweep_done, Qt.ConnectionType.QueuedConnection)
        self.worker.failed.connect(self._sweep_failed, Qt.ConnectionType.QueuedConnection)
        self.worker.start()

    def _sweep_done(self, fit):
        self.run_btn.setEnabled(True)
        self.show_fit(fit)

    def _sweep_failed(self, msg: str):
        self.run_btn.setEnabled(True)
        self.info.setText(f"Sweep failed: {msg}")
        QMessageBox.critical(self, "Laser calibration", msg)

    def save_fit(self):
        try:
            self.model.save_laser_fit()
        except (RuntimeError, OSError) as e:
            QMessageBox.warning(self, "Laser calibration", str(e))
            return
        self.info.setText(self.info.text() + "  (saved)")

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.worker.wait(10000)
        super().closeEvent(event)
