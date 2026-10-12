"""Offscreen tests of the main GUI window driven directly through its methods."""
import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6")
pytest.importorskip("pyqtgraph")

from PyQt6.QtCore import Qt  # noqa: E402
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
    assert not window.scanners_lamp.property("lit")
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
    assert window.sample_lamp.property("lit")
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
    assert wait_until(lambda: window.scanners_lamp.property("lit"), timeout_s=60)
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
    assert window.sample_lamp.property("lit")
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
    assert not window.sample_lamp.property("lit")


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


# ---------------------------------------------------------------------- audit regressions
def make_ready(window):
    model = window.model
    model.scanners_calibrated = True
    model.apply_unity_sample_calib()
    assert window.load_stim_config(CONFIG)
    process_events(50)
    return model


def beam_controls(window):
    sp, sa = window.scanner_panel, window.sample_panel
    return [sp.point_btn, sp.cat_btn, sp.laser_btn, sp.power, sp.run_btn, sp.check_btn, sa.zap_all_btn,
            sa.zap_site_btn, sa.paint_border_btn, sa.paint_area_btn, sa.calibrate_btn]


def test_controls_locked_during_trial(window):
    model = make_ready(window)
    model.calibration_data = [object()]          # lets Check calibration enable
    window.scanner_panel.update_enabled()
    assert all(w.isEnabled() for w in beam_controls(window))
    window.scanner_panel.point_btn.setChecked(True)
    model.send_samples(condition=2, hardware_triggered=False, logging=False)
    assert wait_until(lambda: window.trial_locked, timeout_s=5)
    assert not any(w.isEnabled() for w in beam_controls(window))
    assert not window.scanner_panel.point_btn.isChecked()
    assert window.scanner_panel.laser_btn.text() == "Laser: Off"
    assert "stim" in window.state_label.text() and "condition 2" in window.state_label.text()
    assert "laser on" in window.state_label.text()
    # A refused action is reported, never raised.
    window.scanner_panel.laser_btn.setChecked(True)
    assert window.messages[-1][0] == "warning" and "trial" in window.messages[-1][2]
    assert not window.scanner_panel.laser_btn.isChecked()
    window.scanner_panel.on_view_moved(0.0, 0.0)
    model.stop_opto_stim(0)
    assert wait_until(lambda: not window.trial_locked, timeout_s=5)
    assert all(w.isEnabled() for w in beam_controls(window))
    model.calibration_data = []


def test_finite_trial_unlocks_by_itself(window):
    model = make_ready(window)
    model.send_samples(condition=1, stim_duration_s=0.2, hardware_triggered=False, logging=False)
    assert wait_until(lambda: window.trial_locked, timeout_s=5)
    # Nobody calls stop: the state poll on the frame timer notices the finished trial.
    assert wait_until(lambda: not window.trial_locked, timeout_s=10)
    assert window.state_label.text() == "State: idle"
    assert window.scanner_panel.point_btn.isEnabled()


def test_window_keeps_ticking_through_a_server_ramp_down(window):
    model = make_ready(window)
    ticks = []
    window.frame_timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    write_ao = model.daq.write_ao

    def blocking_write(w):                   # as on NI: a write returns once its buffer region has played
        time.sleep(w.shape[0] / model.daq.sample_rate)
        write_ao(w)
    model.daq.write_ao = blocking_write
    model.send_samples(condition=1, hardware_triggered=False, logging=False)
    assert wait_until(lambda: window.trial_locked, timeout_s=5)
    ramp = threading.Thread(target=model.stop_opto_stim, args=(600,))   # the server thread holds the lock throughout
    t0 = time.monotonic()
    ramp.start()
    process_events(800)
    ramp.join()
    gaps = np.diff([t0] + ticks)
    assert gaps.max() < 0.3                  # one 600 ms freeze before poll_state
    assert wait_until(lambda: not window.trial_locked, timeout_s=5)


def test_laser_sweep_stops_a_check_pattern_and_waits_out_a_trial(window):
    model = make_ready(window)
    window.open_laser_dialog()
    dialog = window._laser_dialog
    window.sample_panel.zap_all_btn.setChecked(True)
    assert model._pattern_running and window.pattern_running()
    dialog.run_sweep()
    assert wait_until(lambda: dialog.run_btn.isEnabled(), timeout_s=10)
    assert not window.sample_panel.zap_all_btn.isChecked() and not window.pattern_running()
    assert window.scanner_panel.laser_btn.text() == "Laser: Off"
    model.send_samples(condition=1, hardware_triggered=True, logging=False)   # not refused as "pattern running"
    assert wait_until(lambda: window.trial_locked, timeout_s=5)
    assert not dialog.run_btn.isEnabled()
    model.stop_opto_stim()
    assert wait_until(lambda: dialog.run_btn.isEnabled(), timeout_s=5)
    dialog.close()


def test_laser_sweep_holds_the_window_and_waits_for_a_scanner_calibration(window):
    model = make_ready(window)
    window.open_laser_dialog()
    dialog = window._laser_dialog
    # Window modal: once Zap all could start mid-sweep and leave the GUI showing a pattern nothing played.
    assert dialog.windowModality() == Qt.WindowModality.WindowModal
    model.scanners_calibrated = False
    window.scanner_panel.run_btn.setChecked(True)
    assert window.scanner_panel.calibration_running()
    dialog.run_sweep()
    assert dialog.worker is None and "scanner calibration" in dialog.info.text()
    window.scanner_panel.run_btn.setChecked(False)
    assert wait_until(lambda: not window.scanner_panel.calibration_running(), timeout_s=10)
    dialog.run_sweep()
    assert dialog.worker is not None
    assert wait_until(lambda: dialog.run_btn.isEnabled(), timeout_s=10)
    dialog.close()


def test_load_garbage_config_keeps_running(window, tmp_path, monkeypatch):
    garbage = tmp_path / "garbage.yml"
    garbage.write_bytes(bytes(range(256)) * 4)
    assert not window.load_stim_config(garbage)
    assert window.messages[-1][0] == "critical" and "garbage.yml" in window.messages[-1][2]
    scalar = tmp_path / "scalar.yml"
    scalar.write_text("42\n")
    assert not window.load_stim_config(scalar)

    def boom(_path):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(window.model, "load_stim_config", boom)
    assert not window.load_stim_config(CONFIG)
    assert "RuntimeError: unexpected" in window.messages[-1][2]
    process_events(50)
    assert window.isVisible() and window.frame_timer.isActive()


def test_sample_calibration_applied_only_on_yes(window):
    panel, model = window.sample_panel, window.model
    model.apply_unity_sample_calib()
    old = model.ref_points_sample.copy()
    seen = {}

    def confirm_no():
        seen["calibrated"] = model.sample_calibrated
        seen["ref"] = model.ref_points_sample.copy()
        seen["preview"] = len(window.view._curves["outline_temp"].xData)
        return False
    panel.confirm = confirm_no
    panel.start_calibration()
    assert model.sample_calibrated and np.allclose(model.ref_points_sample, old)
    panel.on_view_clicked(1.0, 2.0)
    panel.on_view_clicked(1.0, -1.0)
    assert seen["calibrated"] and np.allclose(seen["ref"], old) and seen["preview"] > 10
    assert model.sample_calibrated and np.allclose(model.ref_points_sample, old)
    assert len(window.view._curves["outline_temp"].xData) == 0

    panel.confirm = lambda: True
    panel.start_calibration()
    panel.on_view_clicked(1.0, 2.0)
    panel.on_view_clicked(1.0, -1.0)
    assert np.allclose(model.ref_points_sample, [[1.0, 2.0], [1.0, -1.0]])


def test_sample_calibration_escape_and_cancel_keep_old(window):
    panel, model = window.sample_panel, window.model
    model.apply_unity_sample_calib()
    old = model.ref_points_sample.copy()
    panel.confirm = lambda: pytest.fail("must not ask after a cancel")
    panel.start_calibration()
    assert panel.cancel_btn.isEnabled()
    panel.on_view_clicked(1.0, 2.0)
    window.escape_shortcut.activated.emit()
    assert panel.stage == 0 and not panel.cancel_btn.isEnabled()
    assert model.sample_calibrated and np.allclose(model.ref_points_sample, old)
    panel.start_calibration()
    panel.on_view_clicked(1.0, 2.0)
    panel.cancel_btn.click()
    assert panel.stage == 0
    panel.on_view_clicked(1.0, -1.0)                 # clicks after a cancel go nowhere
    assert model.sample_calibrated and np.allclose(model.ref_points_sample, old)


def test_lamps_and_config_visible_on_both_tabs(window):
    window.load_stim_config(CONFIG)
    for i in range(window.tabs.count()):
        window.tabs.setCurrentIndex(i)
        process_events(20)
        assert window.scanners_lamp.isVisible() and window.sample_lamp.isVisible()
        assert window.config_label.isVisible()
        assert window.config_label.text() == f"Config: {window.model.stim_config.name}"


def test_calibration_circles_hidden_on_sample_tab(window):
    window.view.set_points("calib_all", [[0.0, 0.0], [1.0, 1.0]])
    window.refresh_calibration_layers()
    item = window.view._scatters["calib_all"]
    assert item.isVisible()
    window.tabs.setCurrentWidget(window.sample_panel)
    assert not item.isVisible()
    window.tabs.setCurrentWidget(window.scanner_panel)
    assert item.isVisible()


def test_window_fits_small_laptop(window):
    from zapit.gui.main_window import initial_window_size
    assert initial_window_size(2560, 1400) == (1400, 900)
    w, h = initial_window_size(1366, 728)            # 1366x768 minus a taskbar
    assert w <= 1366 * 0.9 and h <= 728 * 0.9
    hint = window.minimumSizeHint()
    assert hint.width() <= w and hint.height() <= h


def test_run_calibration_pressed_while_cancel_finishes(window, monkeypatch):
    import threading
    release = threading.Event()

    def slow_calibration():
        release.wait(10)
        return False
    monkeypatch.setattr(window.model, "calibrate_scanners", slow_calibration)
    panel = window.scanner_panel
    panel.run_btn.setChecked(True)
    assert panel.calibration_running() and panel.run_btn.text() == "Cancel"
    panel.run_btn.setChecked(False)                  # cancel; the worker is still finishing
    panel.run_btn.setChecked(True)                   # pressed again too soon
    assert not panel.run_btn.isChecked() and panel.run_btn.text() == "Run calibration"
    assert "still stopping" in window.status_label.text()
    release.set()
    assert wait_until(lambda: not panel.calibration_running(), timeout_s=10)
    process_events(50)
    assert not panel.run_btn.isChecked() and panel.run_btn.text() == "Run calibration"


def test_laser_button_follows_check_pattern(window):
    make_ready(window)
    sp, sa = window.scanner_panel, window.sample_panel
    sa.zap_all_btn.setChecked(True)
    assert window.model.daq.ao_mode() is not None
    assert sp.laser_btn.isChecked() and sp.laser_btn.text() == "Laser: On"
    sa.zap_all_btn.setChecked(False)
    assert window.model.daq.ao_mode() is None
    assert not sp.laser_btn.isChecked() and sp.laser_btn.text() == "Laser: Off"
    # Switching the laser off during a pattern stops the pattern.
    sa.paint_border_btn.setChecked(True)
    assert sp.laser_btn.isChecked()
    sp.laser_btn.setChecked(False)
    assert not sa.paint_border_btn.isChecked() and window.model.daq.ao_mode() is None


def test_plot_stim_coords(window):
    assert window.plot_stim_coords() is None
    window.load_stim_config(CONFIG)
    plot = window.plot_stim_coords()
    assert plot is not None and not plot.isModal()
    assert len(plot.view._condition_items) == window.model.stim_config.n_conditions
    plot.close()


def test_shortcuts_and_tooltips(window):
    from PyQt6.QtGui import QAction
    from PyQt6.QtWidgets import QAbstractSpinBox, QPushButton
    shortcuts = {a.text(): a.shortcut().toString() for a in window.findChildren(QAction)}
    assert shortcuts["Load stim config..."] == "Ctrl+O"
    assert shortcuts["New stim config..."] == "Ctrl+N"
    assert shortcuts["Quit"] == "Ctrl+Q"
    missing = [w.text() if isinstance(w, QPushButton) else type(w).__name__
               for w in window.findChildren(QPushButton) + window.findChildren(QAbstractSpinBox)
               if not w.toolTip()]
    assert missing == []


def test_excepthook_reports_to_status_bar(window):
    from zapit.gui import make_excepthook
    hook = make_excepthook(window)
    try:
        raise ValueError("slot blew up")
    except ValueError as e:
        hook(type(e), e, e.__traceback__)
    assert "ValueError: slot blew up" in window.status_label.text()


def test_server_respects_enable_and_reports_bind_failure(window):
    from zapit.gui import start_server
    model = window.model
    srv, msg = start_server(model, enabled=False)
    assert srv is None and "--no-server" in msg
    model.settings["server"]["enable"] = False
    srv, msg = start_server(model, enabled=True)
    assert srv is None and "server.enable" in msg
    model.settings["server"]["enable"] = True
    srv, msg = start_server(model, enabled=True, bind="tcp://no-such-host-zapit:notaport")
    assert srv is None and msg.startswith("Server: failed to bind tcp://no-such-host-zapit:notaport")
    win = MainWindow(model, server_message=msg)
    win.dialogs_enabled = False
    try:
        assert win.server_label.text() == msg
    finally:
        win.shutdown()
        win.close()


def test_refused_server_request_shows_in_status_bar(window):
    from zapit.client import ZapitClient
    from zapit.server import ZapitServer
    srv = ZapitServer(window.model, "tcp://127.0.0.1:5599")
    srv.start()
    win = MainWindow(window.model, server=srv)
    win.dialogs_enabled = False
    try:
        client = ZapitClient("tcp://127.0.0.1:5599")
        with pytest.raises(Exception):
            client.send_samples(condition=1)          # not calibrated: refused
        assert wait_until(lambda: "Server refused send_samples" in win.status_label.text(), 5)
        client.close()
    finally:
        win.shutdown()
        win.close()
        srv.stop()


def test_unedited_settings_warning(app):
    model = Zapit(simulated=True, settings_path=DATA / "settings_test.yml")
    model.settings_unedited = True
    win = MainWindow(model)
    win.dialogs_enabled = False
    win.show()
    try:
        process_events(20)
        assert win.settings_warning.isVisible()
        assert str(model.settings_path) in win.settings_warning.text()
    finally:
        win.shutdown()
        win.close()
        model.close()
