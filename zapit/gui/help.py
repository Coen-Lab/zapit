"""The Help menu: quick start, keyboard and mouse, the documentation on GitHub, the settings folder, About.

The texts here describe the GUI as built; keep them in step with the controls' tooltips and the README's
"Running on a rig". Documentation links open the GitHub copy of the repo's Markdown files.
"""
from __future__ import annotations

import platform
import webbrowser

from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QMenu, QMessageBox

from zapit import __version__
from zapit import settings as settings_mod

REPO_URL = "https://github.com/Coen-Lab/zapit"
# (menu text, path in the repo, what it covers): shown as the action's tooltip.
DOCS = (
    ("User guide", "README.md", "Install, running on a rig, stimulus config files"),
    ("Settings reference", "docs/settings.md", "Every key of zapitSystemSettings.yml"),
    ("Hardware and wiring", "docs/hardware.md", "DAQ lines, camera, laser and scanners"),
    ("Bonsai and the server", "docs/bonsai.md", "The ZeroMQ commands a client sends, and their replies"),
    ("Bonsai workflow", "bonsai/README.md", "The example Bonsai workflow and its packages"),
    ("Python API", "docs/api.md", "Driving Zapit from a Python session"),
    ("Design and rig tests", "docs/plan.md", "Trial safety rules, what is not verified on hardware, rig test order"),
)

QUICK_START = """\
<p><b>Once per rig:</b> wire the DAQ (Help, Hardware and wiring), set the rig's values in the settings
file (Help, Open settings folder) and restart Zapit to use them. For a non-linear laser, run File, Laser
calibration with a photodiode on AI0 and press Save and use fit.</p>
<p><b>Each session:</b></p>
<ol>
<li><b>Calibrate scanners</b> tab: set a camera ROI if needed, then <b>Run calibration</b>. The laser fires
at the Laser slider's power while the beam sweeps a grid; the <i>Scanners calibrated</i> lamp turns green.
<b>Point mode</b> then sends the beam wherever you click, to check it.</li>
<li><b>Calibrate sample</b> tab: choose the <b>Reference AP</b>, press <b>Calibrate sample</b>, click bregma,
then the midline point at the reference AP, and accept the outline. The <i>Sample calibrated</i> lamp turns
green.</li>
<li><b>File, Load stim config</b> (Ctrl+O), or File, New stim config (Ctrl+N) to make one on the atlas.
<b>Overlay stim sites</b> and <b>Zap all coords</b> show where it will stimulate.</li>
<li><b>Set experiment path</b> to log every trial there (Bonsai can set it too).</li>
<li>Run trials from Bonsai or Python through the server ({server}). While a trial is armed or playing, the
controls that move the beam or set the laser are locked.</li>
</ol>
<p>Hover over any control for what it does.</p>
"""

KEYS_AND_MOUSE = """\
<table cellspacing="4">
<tr><td><b>Ctrl+N</b></td><td>New stim config</td></tr>
<tr><td><b>Ctrl+O</b></td><td>Load stim config</td></tr>
<tr><td><b>Ctrl+Q</b></td><td>Quit (stops the hardware)</td></tr>
<tr><td><b>Esc</b></td><td>Cancel the sample calibration clicks, keeping the previous calibration</td></tr>
</table>
<p><b>Camera image</b></p>
<table cellspacing="4">
<tr><td>Point mode on</td><td>click: send the beam there (laser on) and measure where it landed</td></tr>
<tr><td>Cat &amp; mouse on</td><td>the beam follows the cursor (laser on)</td></tr>
<tr><td>Calibrate sample</td><td>click bregma, then the midline point at the reference AP</td></tr>
<tr><td>Set ROI</td><td>drag the rectangle or its handles, then Apply ROI</td></tr>
</table>
<p><b>Stim config editor atlas</b></p>
<table cellspacing="4">
<tr><td>click</td><td>new condition (bilateral: the point and its mirror)</td></tr>
<tr><td>Shift+click</td><td>add a point to the last condition (unilateral only)</td></tr>
<tr><td>Ctrl+click</td><td>delete the condition nearest the cursor</td></tr>
</table>
"""


def docs_url(path: str) -> str:
    return f"{REPO_URL}/blob/main/{path}"


def build_help_menu(window, menu: QMenu):
    """Fill ``menu`` for ``window`` (a MainWindow); each action's tooltip says what it opens."""
    menu.setToolTipsVisible(True)
    window._add_action(menu, "Quick start...", lambda: show_text(window, "Quick start", quick_start(window)),
                       tip="The steps from start-up to running trials")
    window._add_action(menu, "Keyboard and mouse...",
                       lambda: show_text(window, "Keyboard and mouse", KEYS_AND_MOUSE),
                       tip="Shortcuts, and what a click does on the camera image and the atlas")
    docs = menu.addMenu("Documentation")
    docs.setToolTipsVisible(True)
    for text, path, what in DOCS:
        window._add_action(docs, text, lambda p=path: webbrowser.open(docs_url(p)), tip=f"{what} (opens GitHub)")
    menu.addSeparator()
    window._add_action(menu, "Open settings folder", lambda: open_settings_folder(window),
                       tip=f"Open {window.model.settings_path.parent}, which holds the settings file and the "
                           f"laser fit. Edits to the settings file take effect when Zapit restarts")
    window._add_action(menu, "Zapit on GitHub", lambda: webbrowser.open(REPO_URL), tip=REPO_URL)
    menu.addSeparator()
    window._add_action(menu, "About Zapit...", lambda: show_text(window, "About Zapit", about(window)),
                       tip="Version, files in use and the server address")


def server_text(window) -> str:
    if window.server is not None:
        return f"listening on {window.server.bind}"
    return window.server_label.text().removeprefix("Server: ")


def quick_start(window) -> str:
    return QUICK_START.format(server=server_text(window))


def about(window) -> str:
    m = window.model
    fit = settings_mod.laser_fit_path(m.settings_path)
    fit_text = str(fit) if m.laser.fit is not None and fit.exists() else \
        "none in use: a linear map between laser.laserMinMaxControlVolts and laser.laserMinMax_mW"
    rows = [("Version", __version__ + (" (simulated hardware)" if m.simulated else "")),
            ("Settings file", str(m.settings_path)),
            ("Laser fit", fit_text),
            ("Server", server_text(window)),
            ("Python, Qt", f"{platform.python_version()}, Qt {QT_VERSION_STR} (PyQt {PYQT_VERSION_STR})")]
    table = "".join(f"<tr><td><b>{k}</b></td><td>{v}</td></tr>" for k, v in rows)
    return (f"<p>Zapit: scanning opto-stimulation for head-fixed mice.</p><table cellspacing='4'>{table}</table>"
            f"<p><a href='{REPO_URL}'>{REPO_URL}</a></p>")


def open_settings_folder(window):
    folder = window.model.settings_path.parent
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
        window.set_status(f"Could not open {folder}")


def show_text(window, title: str, html: str):
    """A non-modal rich-text box, so the help stays open beside the window. Kept in ``window.help_shown``
    (title, text) when dialogs are off, as in tests."""
    window.help_shown = (title, html)
    if not window.dialogs_enabled:
        return None
    box = QMessageBox(QMessageBox.Icon.Information, title, html, QMessageBox.StandardButton.Ok, window)
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    box.setWindowModality(Qt.WindowModality.NonModal)
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    window._open_messages = [b for b in window._open_messages if b.isVisible()] + [box]
    box.show()
    return box
