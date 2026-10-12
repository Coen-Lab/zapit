import pytest

from zapit.atlas import load_atlas
from zapit.stim_config import StimConfig, StimConfigError, malformed_key_report, pattern_conditions


def test_load_and_roundtrip(data_dir, tmp_path):
    cfg = StimConfig.load(data_dir / "uniAndBilateral_5_conditions.yml")
    assert cfg.n_conditions == 5
    assert cfg.modulation_hz == 40
    assert cfg.conditions[2].n_points == 1
    assert cfg.conditions[3].n_points == 2
    out = cfg.save(tmp_path / "copy.yml")
    again = StimConfig.load(out)
    for a, b in zip(cfg.conditions, again.conditions):
        assert a.ml.tolist() == b.ml.tolist() and a.ap.tolist() == b.ap.tolist()
        assert a.laser_power_mw == b.laser_power_mw and a.type == b.type


def test_malformed_keys(data_dir):
    with pytest.raises(StimConfigError, match="duplicate"):
        StimConfig.load(data_dir / "duplicate_keys.yml")
    with pytest.raises(StimConfigError, match="sequential"):
        StimConfig.load(data_dir / "gap_in_keys.yml")
    assert malformed_key_report("stimLocations01:\nstimLocations02:\n") == ""


def test_unpresentable_refused(model, data_dir):
    with pytest.raises(StimConfigError, match="too many points"):
        model.load_stim_config(data_dir / "unpresentable.yml")
    assert model.stim_config is None


def test_power_check(data_dir):
    cfg = StimConfig.load(data_dir / "power_exceeds.yml")
    assert cfg.conditions_exceeding_power(max_mw=57.0, blanking_time_ms=0.3)
    ok = StimConfig.load(data_dir / "power_ok.yml")
    assert ok.conditions_exceeding_power(max_mw=57.0, blanking_time_ms=0.3) == []


def test_peak_power_scaling(data_dir):
    cfg = StimConfig.load(data_dir / "uniAndBilateral_5_conditions.yml")
    single = cfg.peak_power_mw(2, 0.3)       # one point: 50 % duty -> double
    assert single == pytest.approx(2 * cfg.conditions[2].laser_power_mw)
    pair = cfg.peak_power_mw(0, 0.3)
    assert pair > single                     # blanking steals time from each of two points


def test_area_names(data_dir):
    cfg = StimConfig.load(data_dir / "visualAreas_5_points.yml")
    names = [n for c in cfg.conditions for n in c.area_names]
    assert any("visual" in n.lower() for n in names)
    atlas = load_atlas()
    assert atlas.area_at(0.0, 20.0) is None
    assert len(atlas.areas) == 43 and len(atlas.whole_brain) > 1000


def test_pattern_generator():
    conds = pattern_conditions([0, 0], "grid", 2.0, 3, 5.0, 250.0)
    assert len(conds) == 9
    cross = pattern_conditions([1, -2], "cross", 2.0, 3, 5.0, 250.0, bilateral=True)
    assert len(cross) == 5 and all(c.n_points == 2 for c in cross)
    with pytest.raises(ValueError):
        pattern_conditions([0, 0], "blob")
