"""Zapit: scanning opto-stimulation for head-fixed mouse behaviour.

Python core (hardware, calibration, waveforms, experiment API), a PyQt6 GUI and a
ZeroMQ server so Bonsai (or any client) can drive trials.
"""
from importlib.metadata import PackageNotFoundError, version as _version

try:
    __version__ = _version("zapit")
except PackageNotFoundError:  # running from a source checkout without install
    __version__ = "2.0.0"

from zapit.pointer import Zapit  # noqa: E402

__all__ = ["Zapit", "__version__"]
