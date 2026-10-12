"""PyQt6 GUI for Zapit. ``run_gui`` is the entry point used by ``zapit gui``."""
from __future__ import annotations

import sys


def run_gui(simulated: bool = False, settings_path=None, server: bool = True, bind: str | None = None) -> int:
    """Create the model, optionally the ZeroMQ server, show the main window and run the event loop."""
    from PyQt6.QtWidgets import QApplication

    from zapit.gui.main_window import MainWindow
    from zapit.pointer import Zapit

    app = QApplication.instance() or QApplication(sys.argv)
    model = Zapit(simulated=simulated, settings_path=settings_path)
    zmq_server = None
    if server:
        from zapit.server import ZapitServer
        zmq_server = ZapitServer(model, bind)
        try:
            zmq_server.start()
        except Exception as e:  # the GUI is still useful without the server
            print(f"Zapit server could not start: {e}")
            zmq_server = None
    window = MainWindow(model, server=zmq_server)
    window.show()
    try:
        return app.exec()
    finally:
        if zmq_server is not None:
            zmq_server.stop()
        model.close()


__all__ = ["run_gui"]
