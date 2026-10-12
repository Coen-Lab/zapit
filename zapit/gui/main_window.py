"""The Zapit main window: live camera view on the left, calibration tabs on the right.

``MainWindow`` wires the model's signals (via ``ModelBridge``) to the panels,
polls the camera at about 25 Hz, keeps the pattern/pointing toggles mutually
exclusive and owns the menus. Dialogs can be switched off (``dialogs_enabled``)
so the window can be driven headless in tests; messages are then kept in
``messages`` and shown on the status bar.
"""
from __future__ import annotations

import webbrowser
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox, QPushButton,
                             QTabWidget, QWidget)

from zapit import __version__
from zapit.gui.bridge import ModelBridge
from zapit.gui.camera_view import CameraView
from zapit.gui.sample_panel import SamplePanel
from zapit.gui.scanner_panel import ScannerPanel
from zapit.stim_config import StimConfigError

DOCS_URL = "https://github.com/Coen-Lab/zapit"
FRAME_INTERVAL_MS = 40
SAMPLE_TAB_LAYERS = ("outline_calibrated", "stim_sites", "stim_labels")


class MainWindow(QMainWindow):
    """Top-level window over one ``Zapit`` model (and optionally a running ``ZapitServer``)."""

    def __init__(self, model, server=None):
        super().__init__()
        self.model = model
        self.server = server
        self.dialogs_enabled = True
        self.messages: list[tuple[str, str, str]] = []
        self._toggles: dict[str, QPushButton] = {}
        self._releasing = False
        self._last_frame_id = None
        self._config_editor = None
        self._laser_dialog = None
        self.resize(1400, 900)

        self.view = CameraView()
        self.tabs = QTabWidget()
        self.tabs.setMinimumWidth(380)
        self.tabs.setMaximumWidth(460)
        self.scanner_panel = ScannerPanel(model, self.view, self)
        self.sample_panel = SamplePanel(model, self.view, self)
        self.tabs.addTab(self.scanner_panel, "Calibrate scanners")
        self.tabs.addTab(self.sample_panel, "Calibrate sample")
        self.tabs.currentChanged.connect(self._tab_changed)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.tabs, 0)
        self.setCentralWidget(central)

        self.state_label = QLabel()
        self.server_label = QLabel()
        self.status_label = QLabel()
        self.statusBar().addWidget(self.state_label)
        self.statusBar().addWidget(self.server_label)
        self.statusBar().addWidget(self.status_label, 1)
        self.server_label.setText(f"Server: {server.bind}" if server is not None else "Server: off")

        self._build_menus()
        self.bridge = ModelBridge(model, self)
        q = Qt.ConnectionType.QueuedConnection
        self.bridge.state.connect(self._on_state, q)
        self.bridge.stim_config.connect(self._on_stim_config, q)
        self.bridge.scanners_calibrated.connect(self._on_scanners_calibrated, q)
        self.bridge.sample_calibrated.connect(self._on_sample_calibrated, q)
        self.bridge.calibration_point.connect(self.scanner_panel.on_calibration_point, q)
        self.bridge.experiment_path.connect(self.sample_panel.on_experiment_path, q)
        self.bridge.settings.connect(self._on_settings, q)

        self.view.clicked.connect(self._view_clicked)
        self.view.moved.connect(self._view_moved)

        self.frame_timer = QTimer(self)
        self.frame_timer.setInterval(FRAME_INTERVAL_MS)
        self.frame_timer.timeout.connect(self.refresh_frame)
        self.frame_timer.start()

        self._on_state(model.state)
        self._on_scanners_calibrated(model.scanners_calibrated)
        self._on_sample_calibrated(model.sample_calibrated)
        self.sample_panel.on_stim_config(model.stim_config)
        self.sample_panel.on_experiment_path(model.experiment_path)
        self.update_title()
        self.refresh_frame()
        self._tab_changed(self.tabs.currentIndex())

    # ------------------------------------------------------------------ menus
    def _build_menus(self):
        file_menu = self.menuBar().addMenu("&File")
        self._add_action(file_menu, "New stim config...", self.new_stim_config)
        self._add_action(file_menu, "Load stim config...", self.load_stim_config_dialog)
        self.recent_menu = QMenu("Recent", self)
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        file_menu.addMenu(self.recent_menu)
        file_menu.addSeparator()
        self._add_action(file_menu, "Laser calibration...", self.open_laser_dialog)
        file_menu.addSeparator()
        self._add_action(file_menu, "Quit", self.close)
        help_menu = self.menuBar().addMenu("&Help")
        self._add_action(help_menu, "Zapit on GitHub", lambda: webbrowser.open(DOCS_URL))

    def _add_action(self, menu: QMenu, text: str, slot) -> QAction:
        act = QAction(text, self)
        act.triggered.connect(lambda _checked=False: slot())
        menu.addAction(act)
        return act

    def _fill_recent(self):
        self.recent_menu.clear()
        recent = self.model.recent_configs()
        if not recent:
            self.recent_menu.addAction("(none)").setEnabled(False)
        for p in recent:
            self._add_action(self.recent_menu, str(p), lambda p=p: self.load_stim_config(p))

    # ------------------------------------------------------------------ stim config
    def new_stim_config(self):
        try:
            from zapit.gui.config_editor import StimConfigEditor
        except Exception as e:  # the editor is a separate module that may be missing or broken
            self.notify("New stim config", f"The stimulus config editor could not be loaded:\n{e}", "critical")
            return
        self._config_editor = StimConfigEditor(self.model.settings, parent=self, on_saved=self.load_stim_config,
                                               path=None)
        self._config_editor.show()

    def load_stim_config_dialog(self):
        start = str(self.model.stim_config.path.parent) if self.model.stim_config and self.model.stim_config.path else ""
        path, _ = QFileDialog.getOpenFileName(self, "Load stimulus config", start, "YAML (*.yml *.yaml)")
        if path:
            self.load_stim_config(path)

    def load_stim_config(self, path) -> bool:
        """Load a config into the model, reporting failures and power-cap warnings."""
        path = Path(path)
        try:
            cfg = self.model.load_stim_config(path)
        except (StimConfigError, OSError) as e:
            self.notify("Config not loaded", f"{path.name} was not loaded:\n{e}", "critical")
            return False
        self._on_stim_config(cfg)   # we are on the GUI thread; the queued re-emit is a harmless repeat
        over = cfg.conditions_exceeding_power(self.model.laser.max_mw, self.model.blanking.blanking_time_ms)
        self.set_status(f"Loaded {cfg.name}: {cfg.n_conditions} conditions at {cfg.modulation_hz:g} Hz")
        if over:
            self.notify("Laser power", f"Conditions {[i + 1 for i in over]} need more peak power than the laser "
                        f"maximum of {self.model.laser.max_mw:.2f} mW and will be capped", "warning")
        else:
            self.notify("Config loaded", f"{cfg.name}: {cfg.n_conditions} conditions", "information")
        return True

    def open_laser_dialog(self):
        from zapit.gui.laser_dialog import LaserDialog
        self._laser_dialog = LaserDialog(self.model, self)
        self._laser_dialog.show()

    # ------------------------------------------------------------------ toggles
    def register_toggle(self, name: str, button: QPushButton):
        """Buttons registered here are mutually exclusive: checking one unchecks the others."""
        self._toggles[name] = button

    def release_toggles(self, except_name: str | None = None):
        """Uncheck every registered toggle (except one), running their off handlers."""
        if self._releasing:
            return
        self._releasing = True
        try:
            for name, b in self._toggles.items():
                if name != except_name and b.isChecked():
                    b.setChecked(False)
            if except_name != "calibrate_sample":
                self.sample_panel.cancel_calibration()
        finally:
            self._releasing = False

    # ------------------------------------------------------------------ model -> GUI
    def _on_state(self, state):
        self.state_label.setText(f"State: {state}")

    def _on_stim_config(self, cfg):
        self.sample_panel.on_stim_config(cfg)
        self.update_title()

    def _on_scanners_calibrated(self, on):
        self.scanner_panel.set_scanners_lamp(bool(on))

    def _on_sample_calibrated(self, on):
        self.scanner_panel.set_sample_lamp(bool(on))
        self.sample_panel.on_sample_calibrated(bool(on))
        self._tab_changed(self.tabs.currentIndex())

    def _on_settings(self, _settings):
        self.scanner_panel.refresh_from_settings()
        self.sample_panel.refresh_from_settings()

    def update_title(self):
        name = self.model.stim_config.name if self.model.stim_config else "no config"
        title = f"Zapit {__version__} - {name}"
        if self.model.simulated:
            title += " [SIMULATED]"
        self.setWindowTitle(title)

    # ------------------------------------------------------------------ view and frames
    def refresh_frame(self):
        frame = self.model.last_frame()
        if frame is None:
            return
        frame_id = (id(frame), self.model.cam.frames_acquired)
        if frame_id == self._last_frame_id:
            return
        self._last_frame_id = frame_id
        self.view.set_frame(frame, self.model.image_extent_mm())

    def _view_clicked(self, x: float, y: float):
        if self.sample_panel.stage:
            self.sample_panel.on_view_clicked(x, y)
        else:
            self.scanner_panel.on_view_clicked(x, y)

    def _view_moved(self, x: float, y: float):
        if self.sample_panel.stage:
            self.sample_panel.on_view_moved(x, y)
        else:
            self.scanner_panel.on_view_moved(x, y)

    def _tab_changed(self, index: int):
        on_sample_tab = self.tabs.widget(index) is self.sample_panel
        for layer in SAMPLE_TAB_LAYERS:
            self.view.set_visible(layer, on_sample_tab)
        if on_sample_tab:
            self.view.clear_layer("last_point")
            self.view.clear_layer("last_detected")

    # ------------------------------------------------------------------ messages
    def set_status(self, text: str):
        self.status_label.setText(text)

    def notify(self, title: str, text: str, kind: str = "information"):
        """Message box (or a status-bar line when dialogs are disabled)."""
        self.messages.append((kind, title, text))
        self.set_status(f"{title}: {text.splitlines()[0]}")
        if not self.dialogs_enabled:
            return
        fn = {"warning": QMessageBox.warning, "critical": QMessageBox.critical}.get(kind, QMessageBox.information)
        fn(self, title, text)

    def ask(self, title: str, text: str) -> bool:
        """Yes/No question; True when dialogs are disabled."""
        if not self.dialogs_enabled:
            return True
        reply = QMessageBox.question(self, title, text,
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        return reply == QMessageBox.StandardButton.Yes

    # ------------------------------------------------------------------ shutdown
    def shutdown(self):
        """Stop timers, workers and any running pattern. Safe to call more than once."""
        self.frame_timer.stop()
        if self.scanner_panel.calibration_running():
            self.model.abort_calibration = True
            self.scanner_panel.wait_for_calibration()
        self.release_toggles()
        try:
            self.model.stop_pattern()
        except Exception as e:
            print(f"Could not stop the pattern on shutdown: {e}")
        self.bridge.disconnect_model()

    def closeEvent(self, event):
        if self.dialogs_enabled and not self.ask("Quit Zapit", "Close Zapit and stop the hardware?"):
            event.ignore()
            return
        self.shutdown()
        event.accept()
