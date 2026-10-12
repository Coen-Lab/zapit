"""Hardware backends. Each device has an abstract interface and a simulated implementation.

Real backends import their SDK lazily so the package imports without NI-DAQmx or
pylon installed.
"""
from zapit.hardware.camera import Camera, SimulatedCamera
from zapit.hardware.daq import DAQ, SimulatedDAQ

__all__ = ["Camera", "SimulatedCamera", "DAQ", "SimulatedDAQ", "make_daq", "make_camera"]


def make_daq(settings: dict, simulated: bool) -> DAQ:
    if simulated:
        return SimulatedDAQ(settings)
    from zapit.hardware.nidaq import NIDAQ
    return NIDAQ(settings)


def make_camera(settings: dict, simulated: bool) -> Camera:
    if simulated:
        return SimulatedCamera(settings)
    backend = str(settings["camera"].get("backend", "basler")).lower()
    if backend == "basler":
        from zapit.hardware.pylon import BaslerCamera
        return BaslerCamera(settings)
    raise ValueError(f"Unknown camera backend {backend!r} (supported: basler, or simulated mode)")
