# CLAUDE.md

Zapit scanning opto-stimulation system in Python (derived from Zapit-Optostim/zapit). PyQt6 GUI, ZeroMQ server for Bonsai, nidaqmx and pypylon backends with simulated twins.

## Layout

| Path | Holds |
|---|---|
| `zapit/pointer.py` | `Zapit`, the model: hardware, calibrations, config, trial API, logging. Qt-free |
| `zapit/waveforms.py` | Pure numpy waveform generation. Locked sample for sample to the reference data (regression test) |
| `zapit/transforms.py` | Stereotaxic to sample (two reference points), similarity fit, mm/pixel/volt mapping |
| `zapit/stim_config.py`, `zapit/atlas.py` | YAML configs, atlas lookup; `zapit/data/atlas.npz` |
| `zapit/hardware/` | `daq.py`, `camera.py` (interfaces plus simulated), `nidaq.py`, `pylon.py` (real, untested on hardware) |
| `zapit/server.py`, `zapit/client.py` | JSON over ZeroMQ REQ/REP |
| `zapit/gui/` | `main_window.py`, `camera_view.py`, `config_editor.py`, `laser_dialog.py` |
| `scripts/migrate_rig.py` | One-off conversion of a Zapit 1.x rig's files. The only code that knows the old formats |
| `bonsai/` | Example environment and workflow; `docs/bonsai.md` is the protocol |
| `tests/` | pytest, all simulated; `tests/data/` holds configs, the waveform reference and `legacy_rig/` for the migration test |

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
- Add a settings key by extending `_SCHEMA` in `zapit/settings.py` with a default and a validator, then document it in `docs/settings.md`.
- The app reads only current file formats. Conversions from older formats go in `scripts/migrate_rig.py`, never in the app.
- The model emits `zapit.events.Signal` callbacks; the GUI bridges them to Qt signals in `ModelBridge`. Never import Qt in the core.
- While `Zapit.trial_active`, on-demand AO writes and patterns are refused: on NI hardware an unclocked write clears the clocked task. Keep that guard on any new beam or laser method.
- Tests use `tests/data/settings_test.yml` through `settings_path=`, which is never rewritten.
- Docs are Markdown with one paragraph per line. No em-dashes.

## Traps

- `tests/data/chan_samples_reference.npz` is the locked waveform reference; any change to `waveforms.one_cycle` that alters it is a behaviour change and needs a deliberate regeneration plus a changelog entry.
- A similarity transform cannot reflect. A mirrored camera image needs `scanners.invertXscanner` or `invertYscanner`, not a camera flip alone; the simulated scanner physics ignore those flags, so the simulation shows the same failure as a rig.
- The simulated camera re-renders when the beam state changes, so calibration tests can use a zero settle time; real cameras need the default settle.
- `nidaq.py` writes a new buffer into a running continuous task for the ramp-down. If a driver version refuses, stop and restart the task instead.
- The simulated DAQ keeps state only; it does not reproduce NI's rule that an unclocked write drops the clocked task. The model guard covers it, so test ownership rules against the model, not the DAQ.
- `.gitignore` excludes `bonsai/.bonsai/Packages/` and `Bonsai.exe`; `local_packages/` style folders must be committed if ever added.
