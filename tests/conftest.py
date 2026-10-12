from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data"


@pytest.fixture
def data_dir() -> Path:
    return DATA


@pytest.fixture
def model():
    """Simulated Zapit with the test settings file, camera running."""
    from zapit.pointer import Zapit
    m = Zapit(simulated=True, settings_path=DATA / "settings_test.yml")
    yield m
    m.close()


@pytest.fixture
def ready_model(model, data_dir):
    """Simulated model with a config loaded and unity calibrations, ready to stimulate."""
    model.scanners_calibrated = True   # unity scanner mapping: waveforms then match the reference data
    model.apply_unity_sample_calib()
    model.load_stim_config(data_dir / "uniAndBilateral_5_conditions.yml")
    return model
