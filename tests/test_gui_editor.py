"""Stim config editor: condition editing, load/save round trips and atlas lookup.

Runs headless: QT_QPA_PLATFORM=offscreen uv run pytest -q tests/test_gui_editor.py
"""
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
pytest.importorskip("pyqtgraph")

from PyQt6.QtCore import Qt  # noqa: E402

from zapit.gui.config_editor import StimConfigEditor  # noqa: E402
from zapit.settings import default_settings  # noqa: E402
from zapit.stim_config import StimConfig  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    import pyqtgraph as pg
    return pg.mkQApp()


@pytest.fixture
def editor(qapp):
    ed = StimConfigEditor(default_settings())
    yield ed
    ed.close()


def test_defaults_from_settings(editor):
    s = default_settings()
    assert editor.bilateral
    assert editor.power_spin.value() == s["experiment"]["defaultLaserPowerMW"]
    assert editor.power_spin.maximum() == s["laser"]["laserMinMax_mW"][1]
    assert editor.freq_spin.value() == s["experiment"]["defaultLaserModulationFrequencyHz"]
    assert editor.ramp_spin.value() == s["experiment"]["offRampDownDuration_ms"]
    assert editor.conditions == []
    assert editor.bottom_label.text() == "0 stimulus conditions"


def test_add_bilateral_condition(editor):
    cond = editor.add_condition_at(2.345, -3.2)
    assert len(editor.conditions) == 1
    assert cond.type == "bilateral_points"
    np.testing.assert_allclose(cond.ml, [2.35, -2.35])
    np.testing.assert_allclose(cond.ap, [-3.2, -3.2])
    assert cond.laser_power_mw == editor.power_spin.value()
    assert cond.rampdown_ms == editor.ramp_spin.value()
    assert editor.bottom_label.text() == "1 stimulus conditions"


def test_unilateral_add_and_append(editor):
    editor.bilateral = False
    first = editor.add_condition_at(1.0, 1.0)
    assert first.type == "unilateral_point"
    same = editor.append_point_at(1.5, 0.5)
    assert same is first
    assert len(editor.conditions) == 1
    assert first.type == "unilateral_points"
    assert first.n_points == 2
    np.testing.assert_allclose(first.points, [[1.0, 1.0], [1.5, 0.5]])


def test_append_ignored_in_bilateral_mode(editor):
    editor.add_condition_at(2.0, -2.0)
    assert editor.append_point_at(1.0, 1.0) is None
    assert editor.conditions[0].n_points == 2


def test_append_with_no_conditions_starts_one(editor):
    editor.bilateral = False
    cond = editor.append_point_at(1.0, 1.0)
    assert cond is editor.conditions[0] and cond.type == "unilateral_point"


def test_delete_nearest(editor):
    editor.add_condition_at(1.0, 1.0)
    editor.add_condition_at(3.0, -3.0)
    editor.add_condition_at(0.5, -6.0)
    assert editor.nearest_condition(2.8, -2.9) == 1
    assert editor.delete_nearest(-2.9, -3.1) == 1   # mirrored point of the second condition
    assert len(editor.conditions) == 2
    np.testing.assert_allclose([c.ap[0] for c in editor.conditions], [1.0, -6.0])
    assert editor.delete_nearest(0, 0) == 0
    assert editor.delete_nearest(0, 0) == 0
    assert editor.delete_nearest(0, 0) is None
    assert editor.conditions == []


def test_click_dispatch_by_modifier(editor):
    editor.bilateral = False
    none = Qt.KeyboardModifier.NoModifier
    editor._on_click(1.0, 1.0, none)
    editor._on_click(1.2, 1.2, Qt.KeyboardModifier.ShiftModifier)
    assert len(editor.conditions) == 1 and editor.conditions[0].n_points == 2
    editor._on_click(-2.0, -2.0, none)
    assert len(editor.conditions) == 2
    editor._on_click(1.1, 1.1, Qt.KeyboardModifier.ControlModifier)
    assert len(editor.conditions) == 1
    np.testing.assert_allclose(editor.conditions[0].points, [[-2.0, -2.0]])


def test_load_uniform_file(editor, data_dir):
    cfg = editor.load_path(data_dir / "uniAndBilateral_5_conditions.yml")
    assert cfg.n_conditions == 5
    assert len(editor.conditions) == 5
    assert [c.type for c in editor.conditions] == ["bilateral_points", "bilateral_points", "unilateral_point",
                                                   "unilateral_points", "bilateral_points"]
    assert editor.power_spin.isEnabled() and editor.ramp_spin.isEnabled()
    assert editor.power_spin.value() == 2.0
    assert editor.ramp_spin.value() == 250.0
    assert editor.freq_spin.value() == 40.0
    assert editor.bottom_label.text() == "uniAndBilateral_5_conditions.yml - 5 stimulus conditions"


def test_load_varying_attributes_disables_spinbox(editor, data_dir, tmp_path, monkeypatch):
    cfg = StimConfig.load(data_dir / "uniAndBilateral_5_conditions.yml")
    cfg.conditions[2].laser_power_mw = 7.5
    varied = cfg.save(tmp_path / "varied.yml")
    warnings = []
    monkeypatch.setattr(StimConfigEditor, "_warn", lambda self, title, text: warnings.append(text))
    editor.load_path(varied)
    assert not editor.power_spin.isEnabled()
    assert editor.ramp_spin.isEnabled()
    assert len(warnings) == 1 and "laser power" in warnings[0] and "ramp-down" not in warnings[0]
    assert editor.power_spin.value() == 2.0
    # New conditions take the shown value; loaded ones keep their own on save.
    editor.add_condition_at(1.0, 1.0)
    out = editor.save_to(tmp_path / "out.yml")
    back = StimConfig.load(out)
    assert [c.laser_power_mw for c in back.conditions] == [2.0, 2.0, 7.5, 2.0, 2.0, 2.0]
    editor.clear()
    assert editor.power_spin.isEnabled()


def test_spinbox_edits_apply_to_all_conditions(editor):
    editor.add_condition_at(1.0, 1.0)
    editor.add_condition_at(2.0, 2.0)
    editor.power_spin.setValue(9.0)
    editor.ramp_spin.setValue(100.0)
    assert all(c.laser_power_mw == 9.0 for c in editor.conditions)
    assert all(c.rampdown_ms == 100.0 for c in editor.conditions)


def test_save_round_trip(editor, tmp_path):
    saved = []
    editor.on_saved = saved.append
    editor.add_condition_at(2.5, -3.5)
    editor.bilateral = False
    editor.add_condition_at(-1.0, 2.0)
    editor.append_point_at(-1.5, 2.5)
    editor.freq_spin.setValue(20.0)
    path = editor.save_to(tmp_path / "edited.yml")
    assert saved == [path] and editor.path == path
    assert editor.bottom_label.text() == "edited.yml - 2 stimulus conditions"
    back = StimConfig.load(path)
    assert back.modulation_hz == 20.0
    assert back.n_conditions == 2
    for a, b in zip(back.conditions, editor.to_stim_config().conditions):
        assert a.type == b.type
        np.testing.assert_allclose(a.points, b.points)
        assert a.laser_power_mw == b.laser_power_mw and a.rampdown_ms == b.rampdown_ms


def test_add_pattern(editor):
    new = editor.add_pattern((2.0, -3.0), "grid", 1.0, 3, bilateral=False)
    assert len(new) == 9 and len(editor.conditions) == 9
    assert all(c.type == "unilateral_point" for c in new)
    editor.add_pattern((2.0, -3.0), "mlline", 1.0, 3, bilateral=True)
    assert len(editor.conditions) == 12
    assert editor.conditions[-1].n_points == 2


def test_area_name_lookup(editor):
    assert "visual" in editor.area_name_at(2.5, -3.5).lower()
    assert editor.area_name_at(5.5, 4.0) == ""


def test_mouse_move_updates_label_and_highlight(editor):
    editor._on_mouse_moved(2.5, -3.5)
    text = editor.cursor_label.text()
    assert text.startswith("ML=2.50 mm, AP=-3.50 mm, ") and "visual" in text.lower()
    assert len(editor.view._highlight_items) == 2          # bilateral: both hemispheres
    editor.bilateral = False
    editor._on_mouse_moved(2.6, -3.5)
    assert len(editor.view._highlight_items) == 1
    editor._on_mouse_moved(5.4, 3.9)
    assert editor.cursor_label.text() == "ML=5.40 mm, AP=3.90 mm"
    assert editor.view._highlight_items == []
    editor._on_mouse_left()
    assert editor.view._highlight_key == (None, 0)


def test_save_warns_on_points_outside_brain(editor, tmp_path, monkeypatch):
    questions = []
    answer = {"value": False}

    def confirm(self, title, text):
        questions.append(text)
        return answer["value"]
    monkeypatch.setattr(StimConfigEditor, "_confirm", confirm)
    editor.bilateral = False
    editor.add_condition_at(2.5, -3.5)               # visual cortex
    editor.add_condition_at(5.5, 4.0)                # outside the brain
    assert editor.outside_brain_conditions() == [2]
    editor._path = tmp_path / "outside.yml"
    editor.save()
    assert len(questions) == 1 and "Condition 2 " in questions[0]
    assert not (tmp_path / "outside.yml").exists()   # default No: nothing written
    answer["value"] = True
    editor.save()
    assert (tmp_path / "outside.yml").exists()
    editor.delete_nearest(5.5, 4.0)
    questions.clear()
    editor.save()
    assert questions == []                           # all inside: no question


def test_editor_load_failure_is_reported(editor, tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(StimConfigEditor, "_warn", lambda self, title, text: warnings.append(text))
    garbage = tmp_path / "garbage.yml"
    garbage.write_bytes(bytes(range(256)) * 4)
    assert editor.try_load(garbage) is None
    assert len(warnings) == 1 and "garbage.yml" in warnings[0]
    assert editor.conditions == [] and editor.path is None
