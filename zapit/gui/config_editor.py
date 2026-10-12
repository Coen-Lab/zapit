"""Stimulus-config editor window: click on a top-down atlas to build conditions.

``StimConfigEditor`` reproduces the upstream MATLAB editor. A list of
``zapit.stim_config.Condition`` objects is the single source of truth; the plot
is redrawn from it after every change. Mouse handling on the plot:

* click: new condition at the cursor (bilateral adds the mirrored point too)
* shift+click (unilateral): append the point to the last condition
* ctrl+click: delete the condition whose nearest point is closest to the cursor

Laser power and ramp-down are per-condition attributes edited through one
spinbox each. When a loaded file has values that differ across conditions the
spinbox is disabled and the loaded values are kept; new conditions take the
value shown. Modulation frequency is one value for the whole file.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QSignalBlocker, Qt
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
                             QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                             QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
                             QRadioButton, QSpinBox, QVBoxLayout, QWidget)

from zapit.atlas import load_atlas
from zapit.gui.atlas_view import DELETE_COLOUR, ENLARGED_SIZE, AtlasView, condition_style
from zapit.stim_config import Condition, StimConfig, pattern_conditions

PATTERNS = ("grid", "cross", "mlline", "apline")


class PatternDialog(QDialog):
    """Parameters for ``pattern_conditions``: centre, pattern, size, point count, bilateral."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add pattern")
        form = QFormLayout(self)
        self.ml = QDoubleSpinBox(decimals=2, minimum=-6.0, maximum=6.0, singleStep=0.1)
        self.ap = QDoubleSpinBox(decimals=2, minimum=-8.0, maximum=5.0, singleStep=0.1)
        self.pattern = QComboBox()
        self.pattern.addItems(PATTERNS)
        self.size = QDoubleSpinBox(decimals=2, minimum=0.1, maximum=10.0, singleStep=0.5, value=2.0)
        self.n_points = QSpinBox(minimum=2, maximum=20, value=3)
        self.bilateral = QCheckBox("Mirror each point into the other hemisphere")
        form.addRow("Centre ML (mm)", self.ml)
        form.addRow("Centre AP (mm)", self.ap)
        form.addRow("Pattern", self.pattern)
        form.addRow("Size (mm)", self.size)
        form.addRow("Points per axis", self.n_points)
        form.addRow(self.bilateral)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> dict:
        return {"centre": (self.ml.value(), self.ap.value()), "pattern": self.pattern.currentText(),
                "size": self.size.value(), "n_points": self.n_points.value(),
                "bilateral": self.bilateral.isChecked()}


class StimConfigEditor(QMainWindow):
    """Editor window. ``settings`` is the Zapit settings dict; ``on_saved`` receives the saved path."""

    def __init__(self, settings: dict, parent=None, on_saved: Callable[[Path], None] | None = None,
                 path: Path | None = None):
        super().__init__(parent)
        self.setWindowTitle("Stim config editor")
        self.resize(620, 780)
        self.settings = settings
        self.on_saved = on_saved
        self.atlas = load_atlas()
        self._conditions: list[Condition] = []
        self._path: Path | None = None
        self._cursor: tuple[float, float] | None = None
        self._build_ui()
        self._update_label()
        if path is not None:
            self.load_path(path)

    # ------------------------------------------------------------------ construction
    def _build_ui(self) -> None:
        exp, laser = self.settings["experiment"], self.settings["laser"]
        central = QWidget()
        self.setCentralWidget(central)
        column = QVBoxLayout(central)

        self.cursor_label = QLabel("Stimulus config editor")
        self.cursor_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.cursor_label)

        self.view = AtlasView(self.atlas)
        column.addWidget(self.view, stretch=1)
        self.view.mouse_moved.connect(self._on_mouse_moved)
        self.view.mouse_left.connect(self._on_mouse_left)
        self.view.mouse_clicked.connect(self._on_click)

        panel = QHBoxLayout()
        column.addLayout(panel)
        buttons = QVBoxLayout()
        panel.addLayout(buttons)
        self.new_button = QPushButton("New")
        self.load_button = QPushButton("Load")
        self.save_button = QPushButton("Save")
        self.save_as_button = QPushButton("Save As")
        self.pattern_button = QPushButton("Add pattern...")
        for b in (self.new_button, self.load_button, self.save_button, self.save_as_button, self.pattern_button):
            buttons.addWidget(b)
        self.new_button.setToolTip("Wipe any existing points and start over.")
        self.load_button.setToolTip("Load an existing stim config file from disk.")
        self.save_button.setToolTip("Save the current points to a stim config file.")
        self.pattern_button.setToolTip("Append a grid, cross or line of single-point conditions.")
        self.new_button.clicked.connect(self.new_config)
        self.load_button.clicked.connect(self.load_dialog)
        self.save_button.clicked.connect(self.save)
        self.save_as_button.clicked.connect(self.save_as)
        self.pattern_button.clicked.connect(self.pattern_dialog)

        form = QFormLayout()
        panel.addLayout(form, stretch=1)
        self.unilateral_radio = QRadioButton("Unilateral")
        self.bilateral_radio = QRadioButton("Bilateral")
        self.bilateral_radio.setToolTip("Adds two symmetric points, one per hemisphere. "
                                        "Ctrl and click to delete a condition.")
        self.bilateral_radio.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.unilateral_radio)
        self._mode_group.addButton(self.bilateral_radio)
        self._mode_group.buttonClicked.connect(lambda _b: self._refresh_cursor())
        mode = QHBoxLayout()
        mode.addWidget(self.unilateral_radio)
        mode.addWidget(self.bilateral_radio)
        form.addRow("Point type", mode)

        self.power_spin = QDoubleSpinBox(decimals=2, minimum=0.0, maximum=float(laser["laserMinMax_mW"][1]),
                                         singleStep=0.5, value=float(exp["defaultLaserPowerMW"]))
        self.power_spin.setToolTip("Laser power in mW, applied to every condition while enabled.")
        self.freq_spin = QDoubleSpinBox(decimals=1, minimum=1.0, maximum=1000.0, singleStep=1.0,
                                        value=float(exp["defaultLaserModulationFrequencyHz"]))
        self.freq_spin.setToolTip("Frequency at which the laser switches between points or on and off.")
        self.ramp_spin = QDoubleSpinBox(decimals=0, minimum=0.0, maximum=1000.0, singleStep=10.0,
                                        value=float(exp["offRampDownDuration_ms"]))
        self.ramp_spin.setToolTip("Duration of the laser power ramp-down at the end of a trial.")
        form.addRow("Laser power (mW)", self.power_spin)
        form.addRow("Stim. freq. (Hz)", self.freq_spin)
        form.addRow("Rampdown (ms)", self.ramp_spin)
        self.power_spin.valueChanged.connect(self._apply_power)
        self.ramp_spin.valueChanged.connect(self._apply_ramp)

        self.bottom_label = QLabel()
        column.addWidget(self.bottom_label)

    # ------------------------------------------------------------------ state
    @property
    def conditions(self) -> list[Condition]:
        return self._conditions

    @property
    def bilateral(self) -> bool:
        return self.bilateral_radio.isChecked()

    @bilateral.setter
    def bilateral(self, value: bool) -> None:
        (self.bilateral_radio if value else self.unilateral_radio).setChecked(True)
        self._refresh_cursor()

    @property
    def path(self) -> Path | None:
        return self._path

    def to_stim_config(self) -> StimConfig:
        return StimConfig(list(self._conditions), float(self.freq_spin.value()), self._path)

    def area_name_at(self, ml: float, ap: float) -> str:
        area = self.atlas.area_at(ml, ap)
        return area.name if area else ""

    def nearest_condition(self, ml: float, ap: float) -> int | None:
        """Index of the condition with the point closest to (ml, ap), or None when empty."""
        if not self._conditions:
            return None
        d = [np.min(np.hypot(c.ml - ml, c.ap - ap)) for c in self._conditions]
        return int(np.argmin(d))

    # ------------------------------------------------------------------ edits
    def add_condition_at(self, ml: float, ap: float) -> Condition:
        """New condition at the cursor: one point, or the point and its mirror in bilateral mode."""
        ml, ap = round(float(ml), 2), round(float(ap), 2)
        pts = [[ml, ap], [-ml, ap]] if self.bilateral else [[ml, ap]]
        cond = Condition.from_points(pts, self.power_spin.value(), self.ramp_spin.value(),
                                     bilateral=self.bilateral)
        self._conditions.append(cond)
        self._redraw()
        return cond

    def append_point_at(self, ml: float, ap: float) -> Condition | None:
        """Append a point to the last condition (unilateral mode only); it becomes unilateral_points."""
        if self.bilateral:
            return None
        if not self._conditions:
            return self.add_condition_at(ml, ap)
        cond = self._conditions[-1]
        cond.ml = np.append(cond.ml, round(float(ml), 2))
        cond.ap = np.append(cond.ap, round(float(ap), 2))
        cond.type = "unilateral_points"
        self._redraw()
        return cond

    def delete_nearest(self, ml: float, ap: float) -> int | None:
        """Remove the condition nearest the cursor; returns its former index."""
        idx = self.nearest_condition(ml, ap)
        if idx is not None:
            del self._conditions[idx]
            self._redraw()
        return idx

    def add_pattern(self, centre, pattern: str = "grid", size=2.0, n_points=3, bilateral: bool = False) -> list[Condition]:
        new = pattern_conditions(centre, pattern, size, n_points, self.power_spin.value(),
                                 self.ramp_spin.value(), bilateral)
        self._conditions.extend(new)
        self._redraw()
        return new

    def clear(self) -> None:
        self._conditions = []
        self._path = None
        self.power_spin.setEnabled(True)
        self.ramp_spin.setEnabled(True)
        self._redraw()

    def _apply_power(self, value: float) -> None:
        if self.power_spin.isEnabled():
            for c in self._conditions:
                c.laser_power_mw = float(value)

    def _apply_ramp(self, value: float) -> None:
        if self.ramp_spin.isEnabled():
            for c in self._conditions:
                c.rampdown_ms = float(value)

    # ------------------------------------------------------------------ files
    def load_path(self, path: str | Path) -> StimConfig:
        """Load a config, draw it and set the controls from it (see module docstring)."""
        cfg = StimConfig.load(path)
        self._conditions = list(cfg.conditions)
        self._path = Path(path)
        powers = {c.laser_power_mw for c in cfg.conditions}
        ramps = {c.rampdown_ms for c in cfg.conditions}
        with QSignalBlocker(self.power_spin), QSignalBlocker(self.ramp_spin):
            self.freq_spin.setValue(cfg.modulation_hz)
            self.power_spin.setValue(cfg.conditions[0].laser_power_mw)
            self.ramp_spin.setValue(cfg.conditions[0].rampdown_ms)
            self.power_spin.setEnabled(len(powers) == 1)
            self.ramp_spin.setEnabled(len(ramps) == 1)
        self._redraw()
        if len(powers) > 1 or len(ramps) > 1:
            lines = ["This config has values that differ between conditions:"]
            if len(powers) > 1:
                lines.append("  - laser power")
            if len(ramps) > 1:
                lines.append("  - ramp-down duration")
            lines.append("")
            lines.append("Those controls are disabled because the editor has only one control for each. "
                         "Existing conditions keep their own values when you save. New conditions use "
                         "the value shown (the first condition's).")
            self._warn("Per-condition values", "\n".join(lines))
        return cfg

    def save_to(self, path: str | Path) -> Path:
        cfg = self.to_stim_config()
        path = cfg.save(path)
        self._path = path
        self._update_label()
        if self.on_saved is not None:
            self.on_saved(path)
        return path

    def new_config(self) -> None:
        if self._conditions and not self._confirm("Confirm", "Wipe points and start over?"):
            return
        self.clear()

    def load_dialog(self) -> None:
        start = str(self._path.parent) if self._path else ""
        name, _ = QFileDialog.getOpenFileName(self, "Load stim config", start, "YAML (*.yml *.yaml)")
        if name:
            self.load_path(name)

    def save(self) -> None:
        if not self._conditions:
            self._warn("Nothing to save", "Add at least one condition first.")
            return
        if self._path is None:
            self.save_as()
        else:
            self.save_to(self._path)

    def save_as(self) -> None:
        if not self._conditions:
            self._warn("Nothing to save", "Add at least one condition first.")
            return
        start = str(self._path) if self._path else ""
        name, _ = QFileDialog.getSaveFileName(self, "Save stim config", start, "YAML (*.yml)")
        if name:
            if not name.lower().endswith((".yml", ".yaml")):
                name += ".yml"
            self.save_to(name)

    def pattern_dialog(self) -> None:
        dlg = PatternDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.add_pattern(**dlg.values())

    def _confirm(self, title: str, text: str) -> bool:
        yes, no = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
        return QMessageBox.question(self, title, text, yes | no, no) == yes

    def _warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    # ------------------------------------------------------------------ drawing
    def _redraw(self) -> None:
        self.view.set_conditions([c.points for c in self._conditions])
        self._update_label()
        self._refresh_cursor()

    def _update_label(self) -> None:
        prefix = f"{self._path.name} - " if self._path else ""
        self.bottom_label.setText(f"{prefix}{len(self._conditions)} stimulus conditions")

    def _modifiers(self) -> tuple[bool, bool]:
        mods = QApplication.keyboardModifiers()
        return (bool(mods & Qt.KeyboardModifier.ControlModifier),
                bool(mods & Qt.KeyboardModifier.ShiftModifier))

    def _refresh_cursor(self) -> None:
        """Marker colour, symbol and count show what the next click does."""
        if self._cursor is None:
            self.view.set_cursor(None, "", "")
            self.view.set_enlarged(None)
            return
        ml, ap = self._cursor
        ctrl, shift = self._modifiers()
        pts = [[-ml, ap], [ml, ap]] if self.bilateral else [[ml, ap]]
        if ctrl and self._conditions:
            idx = self.nearest_condition(ml, ap)
            self.view.set_enlarged(idx)
            if self.bilateral and self._conditions[idx].n_points != 2:
                pts = [[ml, ap]]
            self.view.set_cursor(pts, DELETE_COLOUR, "x", ENLARGED_SIZE)
            return
        self.view.set_enlarged(None)
        if shift and not self.bilateral and self._conditions:
            colour, symbol = condition_style(len(self._conditions) - 1)
        else:
            colour, symbol = condition_style(len(self._conditions))
        self.view.set_cursor(pts, colour, symbol)

    # ------------------------------------------------------------------ events
    def _on_mouse_moved(self, ml: float, ap: float) -> None:
        self._cursor = (ml, ap)
        area = self.atlas.area_at(ml, ap)
        name = f", {area.name}" if area else ""
        self.cursor_label.setText(f"ML={ml:0.2f} mm, AP={ap:0.2f} mm{name}")
        self.view.set_ticks(ml if area else None, ap)
        side = 0 if self.bilateral else int(np.sign(ml)) or 1
        self.view.set_highlight(area, side)
        self._refresh_cursor()

    def _on_mouse_left(self) -> None:
        self._cursor = None
        self.view.set_ticks(None)
        self.view.set_highlight(None)
        self._refresh_cursor()

    def _on_click(self, ml: float, ap: float, mods) -> None:
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if ctrl:
            self.delete_nearest(ml, ap)
        elif shift:
            self.append_point_at(ml, ap)
        else:
            self.add_condition_at(ml, ap)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Control, Qt.Key.Key_Shift):
            self._refresh_cursor()
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Control, Qt.Key.Key_Shift):
            self._refresh_cursor()
        super().keyReleaseEvent(event)
