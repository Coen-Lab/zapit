# CLAUDE.md

Python rewrite of the Zapit scanning opto-stimulation system (upstream: Zapit-Optostim/zapit, MATLAB). PyQt6 GUI, ZeroMQ server for Bonsai, nidaqmx and pypylon backends with simulated twins.

## Layout

| Path | Holds |
|---|---|
| `zapit/pointer.py` | `Zapit`, the model: hardware, calibrations, config, trial API, logging. Qt-free |
| `zapit/waveforms.py` | Pure numpy waveform generation. Must stay sample-identical to upstream (regression test) |
| `zapit/transforms.py` | Stereotaxic to sample (two reference points), similarity fit, mm/pixel/volt mapping |
| `zapit/stim_config.py`, `zapit/atlas.py` | YAML configs, atlas lookup; `zapit/data/atlas.npz` from `scripts/convert_atlas.py` |
| `zapit/hardware/` | `daq.py`, `camera.py` (interfaces plus simulated), `nidaq.py`, `pylon.py` (real, untested on hardware) |
| `zapit/server.py`, `zapit/client.py` | JSON over ZeroMQ REQ/REP |
| `zapit/gui/` | `main_window.py`, `camera_view.py`, `config_editor.py`, `laser_dialog.py` |
| `bonsai/` | Example environment and workflow; `docs/bonsai.md` is the protocol |
| `tests/` | pytest, all simulated; `tests/data/` holds upstream configs and the MATLAB waveform reference |

## Commands

```
uv sync --all-extras
uv run pytest -q
uv run zapit gui --simulated
uv run zapit server --simulated
uv run zapit check-config tests/data/visualAreas_5_points.yml
```

GUI tests run offscreen: set `QT_QPA_PLATFORM=offscreen`.

## Conventions

- "Sample mm" is relative to the full-sensor centre everywhere in the model; ROI offsets are applied only in `Zapit.pixels_to_mm` and `mm_to_pixels`.
- Waveform columns are fixed: 0 galvo X, 1 galvo Y, 2 laser, 3 LED. Condition indices are 1-based at the API and wire level, 0-based inside.
- Settings keys match the upstream YAML; add a key by extending `_SCHEMA` in `zapit/settings.py` with a default and a validator, then document it in `docs/settings.md`.
- The model emits `zapit.events.Signal` callbacks; the GUI bridges them to Qt signals in `ModelBridge`. Never import Qt in the core.
- Tests use `tests/data/settings_test.yml` through `settings_path=`, which is never rewritten.
- Docs are Markdown with one paragraph per line. No em-dashes.

## Traps

- `tests/data/chan_samples_reference.npz` is the MATLAB output; any change to `waveforms.one_cycle` that alters it is a behaviour change and needs a deliberate regeneration plus a changelog entry.
- A similarity transform cannot reflect. A mirrored camera image needs `scanners.invertXscanner` or `invertYscanner`, not a camera flip alone.
- The simulated camera re-renders when the beam state changes, so calibration tests can use a zero settle time; real cameras need the default settle.
- `nidaq.py` writes a new buffer into a running continuous task for the ramp-down, as the upstream .NET code did. If a driver version refuses, stop and restart the task instead.
- `.gitignore` excludes `bonsai/.bonsai/Packages/` and `Bonsai.exe`; `local_packages/` style folders must be committed if ever added.
