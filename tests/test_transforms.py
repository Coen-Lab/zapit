import numpy as np

from zapit.transforms import ScannerMapping, Similarity, rotate_and_scale


def test_rotate_and_scale_identity():
    ref = np.array([[0, 0], [0, 3.0]])
    pts = np.array([[1.0, 2.0], [-3.0, 0.5]])
    np.testing.assert_allclose(rotate_and_scale(pts, ref, ref), pts)


def test_rotate_and_scale_rotation_scale_translation():
    ref = np.array([[0, 0], [0, 2.0]])
    # bregma at (1, 1); the AP reference point rotated 90 deg and scaled 1.5
    sample = np.array([[1, 1], [1 - 3.0, 1]])
    out = rotate_and_scale(np.array([[0, 2.0]]), ref, sample)
    np.testing.assert_allclose(out, [[-2.0, 1.0]], atol=1e-12)
    out = rotate_and_scale(np.array([[1.0, 0.0]]), ref, sample)   # ML axis rotates onto +y
    np.testing.assert_allclose(out, [[1.0, 2.5]], atol=1e-12)


def test_similarity_fit_recovers_transform():
    rng = np.random.default_rng(1)
    src = rng.uniform(-10, 10, (30, 2))
    th, s, t = 0.2, 1.1, np.array([0.5, -0.7])
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    dst = (s * R @ src.T).T + t
    tf = Similarity.fit(src, dst)
    assert np.isclose(tf.scale, s)
    assert np.isclose(tf.rotation_deg, np.degrees(th))
    np.testing.assert_allclose(tf.forward(src), dst, atol=1e-9)
    np.testing.assert_allclose(tf.inverse(dst), src, atol=1e-9)
    assert tf.residuals(src, dst).max() < 1e-9
    tf2 = Similarity.from_dict(tf.to_dict())
    np.testing.assert_allclose(tf2.matrix, tf.matrix)


def test_mapping_round_trip():
    m = ScannerMapping(11.7, 0.0015, invert_x=True)
    xy = np.array([[1.0, -2.0], [0.3, 0.4]])
    np.testing.assert_allclose(m.volts_to_mm(m.mm_to_volts(xy)), xy)
    m.transform = Similarity.fit(np.array([[0, 0], [1, 0], [0, 1.0]]), np.array([[0.1, 0], [1.1, 0], [0.1, 1.0]]))
    np.testing.assert_allclose(m.volts_to_mm(m.mm_to_volts(xy)), xy)
    px = m.mm_to_pixels(xy, (200, 100))
    np.testing.assert_allclose(m.pixels_to_mm(px, (200, 100)), xy)
