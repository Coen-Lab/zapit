"""Offscreen tests of the main GUI window driven directly through its methods."""
import os
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6")
pytest.importorskip("pyqtgraph")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from zapit.gui.main_window import MainWindow  # noqa: E402
from zapit.pointer import Zapit  # noqa: E402

DATA = Path(__file__).parent / "data"
CONFIG = DATA / "uniAndBilateral_5_conditions.yml"


def process_events(ms: int = 50):
    app = QApplication.instance()
    t0 = time.monotonic()
    while time.monotonic() - t0 < ms / 1000:
        app.processEvents()
        time.sleep(0.005)


def wait_until(cond, timeout_s: float = 30.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        QApplication.instance().processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    model = Zapit(simulated=True, settings_path=DATA / "settings_test.yml")
    win = MainWindow(model)
    win.dialogs_enabled = False
    win.show()
    process_events(100)
    yield win
    win.shutdown()
    win.close()
    process_events(50)
    model.close()


def test_window_builds_and_shows_frames(window):
    assert "Zapit" in window.windowTitle() and "[SIMULATED]" in window.windowTitle()
    assert "no config" in window.windowTitle()
    assert window.view.extent is not None
    x0, y0, w, h = window.view.extent
    ex = window.model.image_extent_mm()
    assert np.allclose((x0, y0, w, h), ex)
    assert window.view.image.image is not None and window.view.image.image.ndim == 2
    assert not window.scanner_panel.scanners_lamp.property("lit")
    assert not window.sample_panel.overlay_btn.isEnabled()


def test_load_config_and_overlay(window):
    assert window.load_stim_config(CONFIG)
    assert window.model.stim_config is not None
    assert CONFIG.name in window.windowTitle()
    assert window.sample_panel.condition.count() == 5
    assert not window.sample_panel.overlay_btn.isEnabled()      # sample not yet calibrated

    window.model.apply_unity_sample_calib()
    window.model.scanners_calibrated = True
    process_events(100)
    assert window.sample_panel.overlay_btn.isEnabled()
    assert window.scanner_panel.sample_lamp.property("lit")
    assert window.view.layer_points("stim_sites").shape == (0, 2)

    window.sample_panel.overlay_btn.setChecked(True)
    process_events(50)
    pts = window.view.layer_points("stim_sites")
    assert len(pts) == sum(c.n_points for c in window.model.stim_config.conditions)
    expected = np.vstack(window.model.calibrated_points())
    assert np.allclose(pts, expected)
    assert [window.sample_panel.condition.itemData(i) for i in range(5)] == [1, 2, 3, 4, 5]

    window.sample_panel.overlay_btn.setChecked(False)
    assert window.view.layer_points("stim_sites").shape == (0, 2)


def test_load_bad_config_reports(window):
    assert not window.load_stim_config(DATA / "duplicate_keys.yml")
    assert window.messages and window.messages[-1][0] == "critical"
    assert window.model.stim_config is None


def test_scanner_calibration_worker(window):
    panel = window.scanner_panel
    panel.run_btn.setChecked(True)
    process_events(20)
    assert panel.run_btn.text() == "Cancel"
    assert panel.calibration_running()
    assert wait_until(lambda: panel.scanners_lamp.property("lit"), timeout_s=60)
    assert wait_until(lambda: not panel.calibration_running() and not panel.run_btn.isChecked(), timeout_s=30)
    process_events(100)
    assert window.model.scanners_calibrated and window.model.transform is not None
    assert len(window.view.layer_points("calib_all")) == len(window.model.calibration_data) >= 4
    assert panel.run_btn.text() == "Run calibration"
    assert "residual" in panel.calib_result.text()
    assert panel.check_btn.isEnabled()
    panel.check_btn.setChecked(True)
    assert window.model.daq.ao_mode() is not None
    panel.check_btn.setChecked(False)
    assert window.model.daq.ao_mode() is None


def test_sample_calibration_click_flow(window):
    panel = window.sample_panel
    model = window.model
    panel.confirm = lambda: True
    panel.start_calibration()
    assert panel.stage == 1
    panel.on_view_moved(0.5, 0.5)
    assert window.view.has_layer("outline_temp")
    panel.on_view_clicked(1.0, 2.0)
    assert panel.stage == 2
    assert np.allclose(window.view.layer_points("bregma"), [[1.0, 2.0]])
    panel.on_view_clicked(1.1, 2.1)              # too close to bregma: ignored
    assert panel.stage == 2 and not model.sample_calibrated
    panel.on_view_moved(1.0, -1.0)
    panel.on_view_clicked(1.0, -1.0)
    assert panel.stage == 0
    assert model.sample_calibrated
    assert np.allclose(model.ref_points_sample, [[1.0, 2.0], [1.0, -1.0]])
    process_events(100)
    assert window.scanner_panel.sample_lamp.property("lit")
    assert panel.paint_border_btn.isEnabled()
    # Bregma maps onto the first click and the reference point onto the second.
    mapped = model.stereotaxic_to_sample(model.ref_points_stereotaxic)
    assert np.allclose(mapped, [[1.0, 2.0], [1.0, -1.0]])


def test_sample_calibration_rejected(window):
    panel = window.sample_panel
    panel.confirm = lambda: False
    panel.start_calibration()
    panel.on_view_clicked(0.0, 0.0)
    panel.on_view_clicked(0.0, 3.0)
    process_events(100)
    assert not window.model.sample_calibrated
    assert not window.scanner_panel.sample_lamp.property("lit")


def test_toggles_are_exclusive(window):
    sp = window.scanner_panel
    sp.point_btn.setChecked(True)
    assert sp.laser_btn.isChecked()
    sp.cat_btn.setChecked(True)
    assert not sp.point_btn.isChecked() and sp.cat_btn.isChecked()
    sp.on_view_moved(0.0, 0.0)
    sp.cat_btn.setChecked(False)
    assert not sp.laser_btn.isChecked()
    sp.point_btn.setChecked(True)
    sp.on_view_clicked(1.0, 1.0)
    assert np.allclose(window.view.layer_points("last_point"), [[1.0, 1.0]])
    sp.point_btn.setChecked(False)


def test_export_waveforms(window, tmp_path):
    window.load_stim_config(CONFIG)
    window.model.apply_unity_sample_calib()
    window.model.scanners_calibrated = True
    out = window.sample_panel.export_waveforms(tmp_path / "waves.npz")
    assert out is not None and out.exists() and out.with_suffix(".csv").exists()
    with np.load(out) as z:
        assert z["waveforms"].shape[1:] == (4, 5)
    assert len(out.with_suffix(".csv").read_text().strip().splitlines()) == 6
