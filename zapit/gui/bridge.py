"""Glue between the Qt-free model and the GUI thread.

``ModelBridge`` re-emits every ``zapit.events.Signal`` of a ``Zapit`` model as a
Qt signal so callbacks fired from worker threads are queued onto the GUI thread
before any widget is touched. ``Worker`` runs one blocking model call in a
``QThread`` and reports the result (or the error) back as a signal.
"""
from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QObject, QThread, pyqtSignal


class ModelBridge(QObject):
    """Qt signals mirroring the model's plain-callback signals, one argument each."""

    state = pyqtSignal(object)
    stim_config = pyqtSignal(object)
    scanners_calibrated = pyqtSignal(object)
    sample_calibrated = pyqtSignal(object)
    calibration_point = pyqtSignal(object)
    experiment_path = pyqtSignal(object)
    settings = pyqtSignal(object)
    server_error = pyqtSignal(str)   # fed by ZapitServer.on_error, connected by the window

    NAMES = ("state", "stim_config", "scanners_calibrated", "sample_calibrated",
             "calibration_point", "experiment_path", "settings")

    def __init__(self, model, parent: QObject | None = None):
        super().__init__(parent)
        self.model = model
        self._slots: dict[str, Callable] = {}
        for name in self.NAMES:
            qt_signal = getattr(self, name)
            slot = _make_slot(qt_signal)
            self._slots[name] = slot
            getattr(model, f"on_{name}").connect(slot)

    def disconnect_model(self):
        """Detach from the model (call before the window is destroyed)."""
        for name, slot in self._slots.items():
            getattr(self.model, f"on_{name}").disconnect(slot)
        self._slots.clear()


def _make_slot(qt_signal) -> Callable:
    def slot(value=None):
        qt_signal.emit(value)
    return slot


class Worker(QThread):
    """Run ``fn(*args, **kwargs)`` off the GUI thread.

    ``done`` carries the return value; ``failed`` carries the error text. Keep a
    reference to the worker while it runs, and never touch widgets from ``fn``.
    """

    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn: Callable, *args, parent: QObject | None = None, **kwargs):
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.result = None
        self.error: str | None = None
        self.exception: Exception | None = None   # the raised exception, for a slot that needs more than text

    def run(self):
        try:
            self.result = self._fn(*self._args, **self._kwargs)
        except Exception as e:  # reported to the GUI, never swallowed silently
            self.exception = e
            self.error = f"{type(e).__name__}: {e}"
            self.failed.emit(self.error)
            return
        self.done.emit(self.result)
