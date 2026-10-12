"""The Zapit main window: live camera view on the left, calibration tabs on the right.

``MainWindow`` wires the model's signals (via ``ModelBridge``) to the panels,
polls the camera at about 25 Hz, keeps the pattern/pointing toggles mutually
exclusive and owns the menus. An always-visible strip under the view holds the
calibration lamps, the loaded config, the server state and any settings warning.
While a trial is armed or playing (``Zapit.trial_active``) every control that
would move the beam, set the laser or start a pattern is disabled; the lock
follows the model's state signal, and the state is polled on the frame timer so
a finite trial that ends by itself releases it.

Dialogs can be switched off (``dialogs_enabled``) so the window can be driven
headless in tests; messages are then kept in ``messages`` and shown on the
status bar.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QGuiApplication, QKeySequence, QShortcut
from PyQt6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox,
                             QPushButton, QTabWidget, QVBoxLayout, QWidget)

from zapit import __version__
from zapit import settings as settings_mod
from zapit.gui.bridge import ModelBridge
from zapit.gui.camera_view import CameraView
from zapit.gui.help import build_help_menu
from zapit.gui.sample_panel import SamplePanel
from zapit.gui.scanner_panel import ScannerPanel, lamp, set_checked_quietly, set_lamp
from zapit.pointer import NotReadyError
from zapit.stim_config import StimConfigError

FRAME_INTERVAL_MS = 40
SAMPLE_TAB_LAYERS = ("outline_calibrated", "stim_sites", "stim_labels")
SCANNER_TAB_LAYERS = ("calib_all", "calib_current")
PREFERRED_SIZE = (1400, 900)
SCREEN_FRACTION = 0.9
WARNING_STYLE = "background-color: #ffdc00; color: black; padding: 3px; border-radius: 3px;"
PATTERN_TOGGLES = ("check_calibration", "paint_border", "zap_all", "zap_site", "paint_area")


def initial_window_size(available_width: int, available_height: int) -> tuple[int, int]:
    """The preferred size, shrunk to 90% of the available screen area so small laptops fit."""
    w = min(PREFERRED_SIZE[0], int(available_width * SCREEN_FRACTION))
    h = min(PREFERRED_SIZE[1], int(available_height * SCREEN_FRACTION))
    return w, h


class MainWindow(QMainWindow):
    """Top-level window over one ``Zapit`` model (and optionally a running ``ZapitServer``).

    ``server_message`` replaces the server text in the status strip when no server
    runs (disabled, or it failed to bind).
    """

    def __init__(self, model, server=None, server_message: str | None = None):
        super().__init__()
        self.model = model
        self.server = server
        self.dialogs_enabled = True
        self.messages: list[tuple[str, str, str]] = []
        self.trial_locked = False
        self._pattern_playing = False
        self._toggles: dict[str, QPushButton] = {}
        self._releasing = False
        self._last_frame_id = None
        self._config_editor = None
        self._laser_dialog = None
        self._stim_coords_window = None
        self._open_messages: list[QMessageBox] = []
        self.help_shown: tuple[str, str] | None = None   # the last Help text, kept for tests

        self.view = CameraView()
        self.tabs = QTabWidget()
        self.tabs.setMinimumWidth(380)
        self.tabs.setMaximumWidth(460)
        self.scanner_panel = ScannerPanel(model, self.view, self)
        self.sample_panel = SamplePanel(model, self.view, self)
        self.tabs.addTab(self.scanner_panel, "Calibrate scanners")
        self.tabs.addTab(self.sample_panel, "Calibrate sample")
        self.tabs.setTabToolTip(0, "First: the camera, the scanner calibration, pointing and the laser")
        self.tabs.setTabToolTip(1, "Second, after the scanners: register the skull to the atlas, check the "
                                   "stimulus sites and set where trials are logged")
        self.tabs.currentChanged.connect(self._tab_changed)

        central = QWidget()
        column = QVBoxLayout(central)
        row = QHBoxLayout()
        row.addWidget(self.view, 1)
        row.addWidget(self.tabs, 0)
        column.addLayout(row, 1)
        column.addWidget(self._build_strip(server, server_message))
        self.setCentralWidget(central)

        self.state_label = QLabel()
        self.state_label.setToolTip("idle: nothing armed. stim: a trial is armed (waiting for its TTL) or "
                                    "playing. rampdown: a continuous trial is ramping the laser down")
        self.status_label = QLabel()
        self.status_label.setToolTip("The last message: what was done or refused, and why")
        self.statusBar().addWidget(self.state_label)
        self.statusBar().addWidget(self.status_label, 1)

        self._build_menus()
        self.escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self.escape_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.escape_shortcut.activated.connect(self.sample_panel.cancel_calibration)

        self.bridge = ModelBridge(model, self)
        q = Qt.ConnectionType.QueuedConnection
        self.bridge.state.connect(self._on_state, q)
        self.bridge.stim_config.connect(self._on_stim_config, q)
        self.bridge.scanners_calibrated.connect(self._on_scanners_calibrated, q)
        self.bridge.sample_calibrated.connect(self._on_sample_calibrated, q)
        self.bridge.calibration_point.connect(self.scanner_panel.on_calibration_point, q)
        self.bridge.experiment_path.connect(self.sample_panel.on_experiment_path, q)
        self.bridge.settings.connect(self._on_settings, q)
        self.bridge.server_error.connect(lambda text: self.set_status(f"Server refused {text}"), q)
        self._server_error_slot = None
        if server is not None:
            self._server_error_slot = self.bridge.server_error.emit
            server.on_error.connect(self._server_error_slot)

        self.view.clicked.connect(self._view_clicked)
        self.view.moved.connect(self._view_moved)

        self.frame_timer = QTimer(self)
        self.frame_timer.setInterval(FRAME_INTERVAL_MS)
        self.frame_timer.timeout.connect(self._tick)
        self.frame_timer.start()

        self._on_state(model.state)
        self._on_scanners_calibrated(model.scanners_calibrated)
        self._on_sample_calibrated(model.sample_calibrated)
        self._on_stim_config(model.stim_config)
        self.sample_panel.on_experiment_path(model.experiment_path)
        self.refresh_frame()
        self._tab_changed(self.tabs.currentIndex())
        self.fit_to_screen()

    # ------------------------------------------------------------------ layout
    def _build_strip(self, server, server_message: str | None) -> QFrame:
        strip = QFrame()
        strip.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(4, 2, 4, 2)
        self.scanners_lamp = lamp("Scanners calibrated")
        self.scanners_lamp.setToolTip("Green once a scanner calibration succeeded (Calibrate scanners tab). "
                                      "Changing the camera ROI clears it")
        self.sample_lamp = lamp("Sample calibrated")
        self.sample_lamp.setToolTip("Green once bregma and the reference point were clicked and accepted "
                                    "(Calibrate sample tab). Changing the reference AP clears it")
        self.config_label = QLabel()
        self.config_label.setToolTip("The stimulus config trials are drawn from (File, Load stim config)")
        self.server_label = QLabel()
        if server is not None:
            self.server_label.setText(f"Server: {server.bind}")
            self.server_label.setToolTip("Bonsai or Python clients send trials to this address (Help, Bonsai and "
                                         "the server). Set by server.bind in the settings")
        else:
            self.server_label.setText(server_message or "Server: off")
            self.server_label.setToolTip("No client can send trials. server.enable and server.bind in the "
                                         "settings control the server")
            if server_message and "failed" in server_message:
                self.server_label.setStyleSheet(WARNING_STYLE)
        self.settings_warning = QLabel()
        self.settings_warning.setStyleSheet(WARNING_STYLE)
        self.settings_warning.setWordWrap(True)
        self.settings_warning.setToolTip("Settings: Help, Open settings folder; every key is in Help, "
                                         "Documentation, Settings reference. Laser fit: File, Laser calibration")
        self._show_settings_warning()
        layout.addWidget(self.scanners_lamp)
        layout.addWidget(self.sample_lamp)
        layout.addWidget(self.config_label)
        layout.addWidget(self.server_label)
        layout.addWidget(self.settings_warning, 1)
        return strip

    def fit_to_screen(self):
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(*PREFERRED_SIZE)
            return
        avail = screen.availableGeometry()
        self.resize(*initial_window_size(avail.width(), avail.height()))

    # ------------------------------------------------------------------ menus
    def _build_menus(self):
        file_menu = self.menuBar().addMenu("&File")
        file_menu.setToolTipsVisible(True)
        self._add_action(file_menu, "New stim config...", self.new_stim_config, "Ctrl+N",
                         tip="Build a stimulus config by clicking on a top-down atlas; saving it loads it here. "
                             "Brings an open editor forward")
        self._add_action(file_menu, "Load stim config...", self.load_stim_config_dialog, "Ctrl+O",
                         tip="Load the stimulus conditions that trials are drawn from. Refused during a trial")
        self.recent_menu = QMenu("Recent", self)
        self.recent_menu.setToolTipsVisible(True)
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        file_menu.addMenu(self.recent_menu)
        self._add_action(file_menu, "Plot stim coords...", self.plot_stim_coords,
                         tip="Show the loaded config's points on the atlas, in stereotaxic mm")
        file_menu.addSeparator()
        self._add_action(file_menu, "Laser calibration...", self.open_laser_dialog,
                         tip="Measure the laser's power curve with a photodiode on AI0 and fit it")
        file_menu.addSeparator()
        self._add_action(file_menu, "Quit", self.close, "Ctrl+Q", tip="Close Zapit and set the outputs to 0 V")
        build_help_menu(self, self.menuBar().addMenu("&Help"))

    def _add_action(self, menu: QMenu, text: str, slot, shortcut: str | None = None,
                    tip: str | None = None) -> QAction:
        act = QAction(text, self)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
        if tip:
            act.setToolTip(tip)
        act.triggered.connect(lambda _checked=False: slot())
        menu.addAction(act)
        return act

    def _fill_recent(self):
        self.recent_menu.clear()
        recent = self.model.recent_configs()
        if not recent:
            self.recent_menu.addAction("(none)").setEnabled(False)
        for p in recent:
            self._add_action(self.recent_menu, str(p), lambda p=p: self.load_stim_config(p), tip=f"Load {p.name}")

    # ------------------------------------------------------------------ stim config
    def new_stim_config(self):
        """Show the stimulus config editor; an open one is brought forward, so its unsaved work is kept."""
        old = self._config_editor
        if old is not None and old.isVisible():
            old.raise_()
            old.activateWindow()
            return
        if old is not None:
            old.deleteLater()
            self._config_editor = None
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
        """Load a config into the model, reporting failures and power-cap warnings. Never raises."""
        path = Path(path)
        try:
            cfg = self.model.load_stim_config(path)
            over = cfg.conditions_exceeding_power(self.model.laser.max_mw, self.model.blanking.blanking_time_ms)
        except NotReadyError as e:
            self.not_ready("Config not loaded", e)
            return False
        except (StimConfigError, OSError) as e:
            self.notify("Config not loaded", f"{path.name} was not loaded:\n{e}", "critical")
            return False
        except Exception as e:  # any other failure is reported, never allowed to abort the event loop
            self.notify("Config not loaded", f"{path.name} was not loaded:\n{type(e).__name__}: {e}", "critical")
            return False
        self._on_stim_config(cfg)   # we are on the GUI thread; the queued re-emit is a harmless repeat
        self.set_status(f"Loaded {cfg.name}: {cfg.n_conditions} conditions at {cfg.modulation_hz:g} Hz")
        if over:
            self.notify("Laser power", f"Conditions {[i + 1 for i in over]} need more peak power than the laser "
                        f"maximum of {self.model.laser.max_mw:.2f} mW and will be capped", "warning")
        else:
            self.notify("Config loaded", f"{cfg.name}: {cfg.n_conditions} conditions", "information")
        return True

    def plot_stim_coords(self):
        """Non-modal plot of the loaded config's points on the atlas, in stereotaxic mm."""
        cfg = self.model.stim_config
        if cfg is None:
            self.notify("Plot stim coords", "Load a stimulus config first", "warning", modal=False)
            return None
        from zapit.gui.atlas_view import StimCoordsWindow
        self._stim_coords_window = StimCoordsWindow(self.model.atlas, [c.points for c in cfg.conditions],
                                                    title=f"Stim coords - {cfg.name}", parent=self)
        self._stim_coords_window.show()
        return self._stim_coords_window

    def open_laser_dialog(self):
        """Show the laser calibration dialog; one at a time, so two sweeps can never share the laser line."""
        from zapit.gui.laser_dialog import LaserDialog
        old = self._laser_dialog
        if old is not None and old.isVisible():
            old.raise_()
            old.activateWindow()
            return
        if old is not None:
            old.deleteLater()
        self._laser_dialog = LaserDialog(self.model, self, before_sweep=self.prepare_laser_sweep,
                                         on_sweep=self._lock_for_sweep, on_saved=self._show_settings_warning,
                                         ask=self.ask, notify=self.notify)
        self._laser_dialog.set_trial_locked(self.trial_locked)
        self._laser_dialog.show()

    def _lock_for_sweep(self, running: bool):
        """A laser sweep writes the laser line for seconds: every control in the window waits for it, for the
        sweep's whole life rather than the dialog's, which is only a view onto it."""
        self.centralWidget().setEnabled(not running)
        self.set_status("Laser sweep running: the other controls are locked until it ends" if running
                        else "Laser sweep ended: controls enabled")

    def laser_sweep_running(self) -> bool:
        return self._laser_dialog is not None and self._laser_dialog.sweeping

    def prepare_laser_sweep(self) -> str | None:
        """Free the laser line for a sweep: refuse while a scanner calibration is still running or stopping
        (it sets the laser itself), otherwise stop any pattern or point mode. Returns the refusal, or None."""
        if self.scanner_panel.calibration_running():
            return "The scanner calibration is still running; run the sweep once it has finished"
        self.release_toggles()
        return None

    # ------------------------------------------------------------------ toggles and patterns
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

    def pattern_started(self):
        """A check pattern is firing the laser: the laser button says so."""
        self._pattern_playing = True
        self.scanner_panel.show_laser(True)

    def pattern_running(self) -> bool:
        return self._pattern_playing

    def stop_pattern(self):
        """Stop a check pattern (the model ignores this during a trial) and show the laser off."""
        try:
            self.model.stop_pattern()
        except NotReadyError as e:
            self.not_ready("Stop pattern", e, explicit=False)
        if self._pattern_playing:
            self._pattern_playing = False
            self.scanner_panel.show_laser(False)

    # ------------------------------------------------------------------ model -> GUI
    def _tick(self):
        self._poll_state()
        self.refresh_frame()

    def _poll_state(self):
        """Reading the state lets the model notice a finished finite trial and emit "idle"."""
        try:
            self.model.poll_state()
        except Exception as e:
            self.set_status(f"Could not read the state: {type(e).__name__}: {e}")

    def _on_state(self, state):
        text = f"State: {state}"
        if state == "stim":
            text += self._trial_summary()
        self.state_label.setText(text)
        # Zapit.trial_active from the emitted state: reading the model state here would wait for the lock,
        # which a ramp-down holds while this slot runs.
        self._apply_trial_lock(state in ("stim", "rampdown") and self.model.daq.has_clocked_task)

    def _trial_summary(self) -> str:
        """Condition and laser state of the trial that just started, from what the model holds."""
        parts = []
        if self.model.last_condition:
            parts.append(f"condition {self.model.last_condition}")
        last = self.model.daq.last_waveform
        if last is not None and last.ndim == 2 and last.shape[1] > 2:
            parts.append("laser on" if float(last[:, 2].max()) > 0 else "laser off")
        return f" ({', '.join(parts)})" if parts else ""

    def _apply_trial_lock(self, locked: bool):
        if locked == self.trial_locked:
            return
        self.trial_locked = locked
        if locked:
            # The trial owns the outputs now: show the hand controls as off without commanding hardware.
            for name, b in self._toggles.items():
                if name != "run_calibration":
                    set_checked_quietly(b, False)
            self._pattern_playing = False
            self.scanner_panel.drop_beam_controls()
            self.sample_panel.cancel_calibration()
            self.set_status("Trial in progress: beam, laser and pattern controls are disabled")
        else:
            self.set_status("Trial finished: controls enabled")
        self.scanner_panel.update_enabled()
        self.sample_panel.update_enabled()
        if self._laser_dialog is not None:
            self._laser_dialog.set_trial_locked(locked)

    def _on_stim_config(self, cfg):
        self.sample_panel.on_stim_config(cfg)
        self.config_label.setText(f"Config: {cfg.name}" if cfg is not None else "Config: none")
        self.update_title()

    def _on_scanners_calibrated(self, on):
        set_lamp(self.scanners_lamp, bool(on))
        self.scanner_panel.update_enabled()

    def _on_sample_calibrated(self, on):
        set_lamp(self.sample_lamp, bool(on))
        self.sample_panel.on_sample_calibrated(bool(on))
        self._tab_changed(self.tabs.currentIndex())

    def _on_settings(self, _settings):
        self.scanner_panel.refresh_from_settings()
        self.sample_panel.refresh_from_settings()
        self._show_settings_warning()

    def _show_settings_warning(self):
        """The strip's warning: placeholder rig values, a settings file Zapit cannot write to, hand edits that
        wait for a restart, and a laser fit in use that may map power wrongly."""
        parts = []
        if self.model.settings_unedited:
            parts.append(f"Settings file {self.model.settings_path} still holds the placeholder "
                         f"{settings_mod.rig_keys_text()}: stimulation is refused until they are set for this "
                         f"rig and Zapit is restarted")
        if self.model.settings_file_problem:
            parts.append(self.model.settings_file_problem)
        if self.model.settings_edited_outside:
            parts.append(f"The settings file was edited while Zapit runs "
                         f"({', '.join(self.model.settings_edited_outside)}): restart Zapit to use the new values")
        fit = self.model.laser.fit
        fit_problem = fit.problem() if fit is not None else None
        if fit_problem:
            parts.append(f"The laser fit in use may map power wrongly ({fit_problem}): run a new sweep (File, "
                         f"Laser calibration)")
        self.settings_warning.setText(". ".join(parts))
        self.settings_warning.setVisible(bool(parts))

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
        for layer in SCANNER_TAB_LAYERS:
            self.view.set_visible(layer, not on_sample_tab)
        if on_sample_tab:
            self.view.clear_layer("last_point")
            self.view.clear_layer("last_detected")

    def refresh_calibration_layers(self):
        """Re-apply the per-tab layer visibility after a panel redrew a layer."""
        self._tab_changed(self.tabs.currentIndex())

    # ------------------------------------------------------------------ messages
    def set_status(self, text: str):
        self.status_label.setText(text)

    def notify(self, title: str, text: str, kind: str = "information", modal: bool = True):
        """Message box (or a status-bar line when dialogs are disabled). ``modal=False`` does not block."""
        self.messages.append((kind, title, text))
        self.set_status(f"{title}: {text.splitlines()[0] if text else ''}")
        if not self.dialogs_enabled:
            return
        icon = {"warning": QMessageBox.Icon.Warning, "critical": QMessageBox.Icon.Critical}.get(
            kind, QMessageBox.Icon.Information)
        if modal:
            box = QMessageBox(icon, title, text, QMessageBox.StandardButton.Ok, self)
            box.exec()
            return
        box = QMessageBox(icon, title, text, QMessageBox.StandardButton.Ok, self)
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._open_messages = [b for b in self._open_messages if b.isVisible()] + [box]
        box.show()

    def not_ready(self, title: str, error: Exception, explicit: bool = True):
        """Report a refused action: status bar always, plus a non-modal message when the user asked for it."""
        if explicit:
            self.notify(title, str(error), "warning", modal=False)
        else:
            self.set_status(f"{title}: {error}")

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
        dialog = self._laser_dialog
        if dialog is not None and dialog.worker is not None and dialog.worker.isRunning():
            # The model closes after this: a sweep step written after that would drive the laser again.
            self.model.abort_laser_sweep = True
            dialog.worker.wait(30000)
        if self.scanner_panel.calibration_running():
            self.model.abort_calibration = True
            self.scanner_panel.wait_for_calibration()
        try:
            self.release_toggles()
        except Exception as e:
            print(f"Could not release the controls on shutdown: {e}")
        try:
            self.model.stop_pattern()
        except Exception as e:
            print(f"Could not stop the pattern on shutdown: {e}")
        if self.server is not None and self._server_error_slot is not None:
            self.server.on_error.disconnect(self._server_error_slot)
            self._server_error_slot = None
        if self.bridge is not None:
            self.bridge.disconnect_model()
            self.bridge = None

    def closeEvent(self, event):
        losses = []
        if self.laser_sweep_running():
            losses.append("A laser sweep is running and will be cancelled.")
        elif self._laser_dialog is not None and self._laser_dialog.new_fit is not None:
            losses.append("The new laser fit has not been saved and is not in use; it will be discarded.")
        editor = self._config_editor
        if editor is not None and editor.isVisible() and editor.unsaved:
            losses.append("The stim config editor has unsaved conditions; they will be discarded.")
        question = " ".join(losses + ["Close Zapit and stop the hardware?"])
        if self.dialogs_enabled and not self.ask("Quit Zapit", question):
            event.ignore()
            return
        if editor is not None:
            editor.close_discarding()   # asked above; a second question could leave it open with the app shut down
        self.shutdown()
        event.accept()
