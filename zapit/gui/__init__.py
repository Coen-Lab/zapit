"""PyQt6 GUI for Zapit. ``run_gui`` is the entry point used by ``zapit gui``."""
from __future__ import annotations

import sys
import traceback


def start_server(model, enabled: bool = True, bind: str | None = None):
    """Start the ZeroMQ server when ``enabled`` and ``server.enable`` allow it.

    Returns ``(server or None, status text)``; the text says why no server runs.
    """
    if not enabled:
        return None, "Server: off (--no-server)"
    if not model.settings["server"]["enable"]:
        return None, "Server: off (server.enable is false in the settings)"
    from zapit.server import ZapitServer
    addr = bind or model.settings["server"]["bind"]
    srv = None
    try:
        srv = ZapitServer(model, addr)
        srv.start()
    except Exception as e:  # the GUI is still useful without the server
        print(f"Zapit server could not start on {addr}: {e}")
        if srv is not None:
            try:
                srv.stop()
            except Exception as e2:
                print(f"Could not clean up the server: {e2}")
        return None, f"Server: failed to bind {addr}: {e}"
    return srv, f"Server: {srv.bind}"


def make_excepthook(window, previous=None):
    """An excepthook that prints the traceback and shows the error in the window's status bar.

    PyQt6 aborts the process on an exception escaping a slot unless a custom hook
    is installed; this one keeps the event loop running.
    """
    def hook(exc_type, exc, tb):
        traceback.print_exception(exc_type, exc, tb)
        if issubclass(exc_type, KeyboardInterrupt) and previous is not None:
            previous(exc_type, exc, tb)
            return
        try:
            window.set_status(f"Error: {exc_type.__name__}: {exc}")
        except Exception:
            pass
    return hook


def run_gui(simulated: bool = False, settings_path=None, server: bool = True, bind: str | None = None) -> int:
    """Create the model, optionally the ZeroMQ server, show the main window and run the event loop."""
    from PyQt6.QtWidgets import QApplication

    from zapit.gui.main_window import MainWindow
    from zapit.pointer import Zapit

    app = QApplication.instance() or QApplication(sys.argv)
    model = Zapit(simulated=simulated, settings_path=settings_path)
    zmq_server = None
    previous_hook = sys.excepthook
    try:
        zmq_server, server_message = start_server(model, server, bind)
        window = MainWindow(model, server=zmq_server, server_message=server_message)
        sys.excepthook = make_excepthook(window, previous_hook)
        window.show()
        return app.exec()
    finally:
        sys.excepthook = previous_hook
        if zmq_server is not None:
            try:
                zmq_server.stop()
            except Exception as e:
                print(f"Could not stop the server: {e}")
        model.close()


__all__ = ["make_excepthook", "run_gui", "start_server"]
