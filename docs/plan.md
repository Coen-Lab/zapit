# Port plan: MATLAB Zapit to Python and Bonsai

Written 12 October 2026, the night the port was made. This records what was decided, why, what was built, and what still has to be checked on a rig.

## Goal

Replace the MATLAB application (MATLAB plus Image Processing, Image Acquisition, Curve Fitting and Instrument Control toolboxes plus the .NET DAQmx wrapper) with a Python application that behaves the same at the hardware, keeps file compatibility, has a proper standalone GUI, and lets Bonsai run the experiment.

## Decisions

| Question | Decision | Why |
|---|---|---|
| Where the real-time work lives | Python owns DAQ, camera, calibration and GUI. Bonsai is a client. | Retriggerable finite AO, per-trial waveform rewrites and the ramp-down are routine with nidaqmx and awkward as reactive nodes. Click-to-calibrate flows need a real GUI. |
| Bonsai transport | ZeroMQ REQ/REP with JSON | Bonsai.ZeroMQ is maintained by the Bonsai team and gives a reply per command, so Bonsai learns which condition was presented. OSC is fire and forget. The legacy 15-byte TCP protocol was dropped. |
| Hardware backends | nidaqmx for NI, pypylon for Basler, both behind interfaces with simulated twins | Matches the upstream rig design. A Spinnaker backend is a small addition behind the same `Camera` interface. |
| Stim config editor | Full atlas editor ported | Needed to make configs without MATLAB. |
| File formats | Stimulus YAML unchanged. Settings YAML keys unchanged; dead keys dropped on load with a backup. Laser fit saved as YAML instead of .mat. Atlas converted from .mat to npz. | Existing rigs keep their files. |
| GUI toolkit | PyQt6 and pyqtgraph | The lab's standard (PyRAT GUI). pyqtgraph handles a 25 Hz camera feed with overlays without effort. |
| Package management | uv, pyproject, optional extras `gui`, `ni`, `basler` | Core imports without hardware SDKs; tests run anywhere. |
| Coordinates | "Sample mm" is always relative to the full-sensor centre; ROI offsets are applied at the pixel boundary | Upstream mixed ROI-relative and sensor-relative frames and relied on the fitted transform to absorb the difference. One frame removes a class of bugs when the ROI changes. |

## Architecture

```
Bonsai (task)  --JSON over ZeroMQ-->  zapit.server.ZapitServer  -->  zapit.Zapit (model)
Harp DO  --TTL-->  NI PFI0                                             |-- hardware.DAQ (nidaqmx | simulated)
                                                                       |-- hardware.Camera (pypylon | simulated)
PyQt6 GUI  --direct calls + callbacks-->  zapit.Zapit                  |-- laser.LaserCalibration
                                                                       |-- transforms.ScannerMapping (+ Similarity)
                                                                       |-- stim_config.StimConfig, atlas.Atlas
                                                                       `-- waveforms (pure numpy)
```

The model is free of Qt. It emits plain callbacks (`zapit.events.Signal`) which the GUI bridges onto Qt signals. The server runs in a thread and calls the model under its lock, so Bonsai and the GUI can act at the same time.

## What was ported, one to one

- Waveform generation (`stimConfig.chanSamples`, `filterForEphys`, the finite-duration expansion and ramp-down in `sendSamples` and `stopOptoStim`). Verified sample for sample against the MATLAB output.
- Scanner calibration: grid generation (centred, buffered), background-subtracted spot detection with the area threshold, similarity fit, pointing accuracy check.
- Sample calibration: two reference points, `rotateAndScaleCoords`.
- Laser calibration: cubic and sigmoid fits, choose by RMSE, anchor to measured min and max mW, cap with a warning.
- Stimulus config loading rules: malformed keys refused, unpresentable conditions refused, laser power ceiling warned, deprecated per-condition modulation frequency migrated.
- Experiment API: random condition that never repeats, random laser state, hardware or software trigger, fixed duration with delay, power override, logging to a parameter YAML and a trial CSV.
- Check patterns: zap all coords, zap site, paint brain border, paint area, check scanner calibration.
- Settings: defaults, validation, rename of old keys, backup and rewrite.

## What changed deliberately

- No MATLAB, no .NET. No update checker, no support-report generator, no TCP byte protocol, no shared-memory Python bridge (the Python client talks ZeroMQ).
- Trial log is a `.csv` with an extra `laser_on` column rather than CSV content in a `.yml` file.
- The `laserFit.mat` file became `laserFit.yml` next to the settings file.
- Settings sections `tcpServer`, `NI.wrapper` and `general.openPythonBridgeOnStartup` are gone; a `server` section (`enable`, `bind`) and `camera.backend` were added.
- A settings file passed explicitly on the command line is never rewritten.

## Not verified on hardware

Everything was built and tested against the simulated backends. These need a rig:

1. `zapit/hardware/nidaq.py`: task creation, regeneration mode, the start trigger terminal string (`/Dev1/PFI0`), writing a new buffer to a running continuous task for the ramp-down (the upstream .NET code did this; nidaqmx should behave the same), the DO task sharing `ao/SampleClock`, and reusing a finite task by resetting `samp_quant_samp_per_chan`.
2. `zapit/hardware/pylon.py`: node names differ between Basler models (`ExposureTime` versus `ExposureTimeAbs`, `Gain` versus `GainRaw`); ROI increments; `GrabStrategy_LatestImageOnly` behaviour with the frame-rate limit.
3. Spot detection thresholds on a real image at the calibration exposure.
4. Camera orientation flags versus scanner inversion flags: a mirrored image needs `invertXscanner` or `invertYscanner`, because the similarity transform is non-reflective (same as upstream).
5. The Bonsai workflow: node names for Bonsai.ZeroMQ `Request` and the Harp.Behavior digital output must be confirmed in the Bonsai editor; the file was validated structurally only.

## How to test on a rig, in order

1. `uv run zapit gui`, confirm the camera feed and that the exposure spinbox changes it.
2. Laser switch on at a low power with Point mode, click on the image, confirm the beam moves the expected way. If it moves the wrong way along an axis, flip the matching `invertXscanner` or `invertYscanner`.
3. Run the scanner calibration, check the residual printed to the console (tens of microns is normal) and the Check calibration pattern.
4. Calibrate the sample, Overlay stim sites, Zap all coords.
5. From Python: `ZapitClient().send_samples(condition=1, hardware_triggered=False)` then `stop()`; watch the laser and the masking LED on a scope.
6. Hardware trigger: `send_samples(stim_duration_s=1)` then pulse PFI0.
7. Bonsai: open `bonsai/.bonsai/Zapit.bonsai`, fix any node the editor flags, press Space.

## Possible next steps

- Spinnaker camera backend (`zapit/hardware/spinnaker.py`, same interface as `pylon.py`).
- A second laser on AO3 with the LED on the DO line (`Zapit.use_clocked_do` already exists).
- Per-trial power and duration from Bonsai are supported; a condition schedule node in Bonsai would make blocked designs trivial.
- Build a signed installer or a `uv tool install` recipe for rigs via rig-setup.
