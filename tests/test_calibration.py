"""End-to-end scanner calibration against the simulated camera and DAQ."""
import numpy as np

from zapit.calibration import calibration_grid, find_beam_spot


def test_grid_is_centred():
    g = calibration_grid((1000, 600), 0.01, spacing_mm=2.0, buffer_mm=1.0)
    assert g.shape[1] == 2
    np.testing.assert_allclose(g.mean(axis=0), [0, 0], atol=1e-9)
    assert np.all(np.abs(g[:, 0]) <= 5 - 1 + 1e-9)
    assert np.all(np.abs(g[:, 1]) <= 3 - 1 + 1e-9)


def test_find_beam_spot_rejects_multiple_blobs():
    f = np.zeros((100, 100))
    f[20, 20] = 255
    f[80, 80] = 255
    assert find_beam_spot(f, None, 500) is None
    f[80, 80] = 0
    c, area, crop = find_beam_spot(f, None, 500)
    np.testing.assert_allclose(c, [20, 20])
    assert area == 1


def test_scanner_calibration_recovers_hidden_transform(model):
    points = []
    model.on_calibration_point.connect(points.append)
    model.settings["calibrateScanners"]["pointSpacingInMM"] = 2.0
    model.settings["calibrateScanners"]["bufferMM"] = 2.0
    assert model.calibrate_scanners(settle_s=0.0)
    assert model.scanners_calibrated
    assert len(points) >= 20
    true = model.sim_true_transform
    assert abs(model.transform.scale - true.scale) < 0.005
    assert abs(model.transform.rotation_deg - true.rotation_deg) < 0.2
    np.testing.assert_allclose(model.transform.matrix[:2, 2], true.matrix[:2, 2], atol=0.03)


def test_pointing_accuracy_after_calibration(model):
    assert model.calibrate_scanners(settle_s=0.0)
    results = model.measure_pointing_accuracy(np.array([[0, 0], [3.0, -2.0], [-4.0, 1.5]]), settle_s=0.0)
    assert len(results) == 3
    assert max(r.error_um for r in results) < 30


def test_calibration_with_roi(model):
    model.set_cam_roi((300, 200, 1200, 800))
    assert not model.scanners_calibrated
    assert model.calibrate_scanners(settle_s=0.0)
    r = model.measure_pointing_accuracy(np.array([[1.0, 1.0]]), settle_s=0.0)
    assert r and r[0].error_um < 30
    model.reset_cam_roi()
    assert model.cam.is_full_frame
