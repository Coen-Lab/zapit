"""Minimal observer used by the core so it stays free of Qt. The GUI bridges these to Qt signals."""
from __future__ import annotations

from collections.abc import Callable


class Signal:
    def __init__(self, name: str = ""):
        self.name = name
        self._slots: list[Callable] = []

    def connect(self, slot: Callable) -> Callable:
        if slot not in self._slots:
            self._slots.append(slot)
        return slot

    def disconnect(self, slot: Callable) -> None:
        if slot in self._slots:
            self._slots.remove(slot)

    def emit(self, *args) -> None:
        for slot in list(self._slots):
            try:
                slot(*args)
            except Exception as e:  # a broken listener must not break the hardware path
                print(f"Listener on {self.name or 'signal'} raised {e!r}")
