# Design and rig validation

This records the design decisions, what is still unverified on hardware, and the order in which to test a rig.

## Decisions

| Question | Decision | Why |
|---|---|---|
| Where the real-time work lives | Python owns DAQ, camera, calibration and GUI. Bonsai is a client. | Retriggerable finite AO, per-trial waveform rewrites and the ramp-down are routine with nidaqmx and awkward as reactive nodes. Click-to-calibrate flows need a real GUI. |
| Bonsai transport | ZeroMQ REQ/REP with JSON | Bonsai.ZeroMQ is maintained by the Bonsai team and gives a reply per command, so Bonsai learns which condition was presented. OSC is fire and forget. |
| Hardware backends | nidaqmx for NI, pypylon for Basler, both behind interfaces with simulated twins | A Spinnaker backend is a small addition behind the same `Camera` interface. |
| Stim config editor | Full atlas editor | Configs are made without any other tool. |
| File formats | Stimulus configs, settings and the laser fit are YAML; the atlas is npz | Human-readable, diffable, no proprietary reader. Older rig files are converted once with `scripts/migrate_rig.py`; the app reads only the current formats. |
| GUI toolkit | PyQt6 and pyqtgraph | The lab's standard (PyRAT GUI). pyqtgraph handles a 25 Hz camera feed with overlays without effort. |
| Package management | uv, pyproject, optional extras `gui`, `ni`, `basler` | Core imports without hardware SDKs; tests run anywhere. |
| Coordinates | "Sample mm" is always relative to the full-sensor centre; ROI offsets are applied at the pixel boundary | One frame for every calculation removes a class of bugs when the ROI changes. |
| Server exposure | Binds `tcp://127.0.0.1:5555` unless `server.bind` says otherwise | A laser should not be controllable from the whole network by default. |
| Unedited settings | A rig refuses to stimulate while `laser.laserMinMax_mW`, `scanners.voltsPerPixel` and `camera.micronsPerPixel` all hold their shipped values | Those defaults are placeholders, not a rig. The GUI never writes them, so tweaking exposure cannot unlock a rig, and a rig that genuinely shares one default value is not locked out. |

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

## Trial safety rules

- While a trial is armed, playing or ramping down, the model refuses on-demand beam and laser writes and check patterns: an on-demand write takes the AO lines from the clocked task and would silently drop the trial. The GUI greys out those controls.
- `stop` disarms any trial whose trigger never came, finite or continuous, so a missed TTL cannot lock out the session (a ramp-down written into a task waiting for its trigger would block on NI). A finite trial that is playing finishes by itself.
- A new trial is refused while another is armed or playing; a continuous one must be stopped first, so it always gets its ramp-down. Arming during a ramp-down, or over a laser or beam set on demand (point mode, the laser button, a laser sweep), stops any task and zeroes the outputs first, because the lines hold their last value and the laser would otherwise stay at that value until the next TTL. A config cannot be loaded under a trial.
- After a continuous stimulus ramps down, the task is stopped and the laser and scanners are set to 0 V with an on-demand write, because a stopped AO task holds its last sample. The delayed stop is tied to its trial, so it never stops a trial armed during the ramp-down.
- The laser waveform is never allowed above `laser.laserMinMaxControlVolts[1]`; the ephys half-sine is scaled down if needed.
- A scanner calibration whose mean residual exceeds 100 um is rejected (usually a mirrored view needing an invert flag).
- Trials are logged only after the DAQ accepts them, one parameter log and trials file per experiment folder and config. A trial the log cannot record (the trials CSV open in Excel, which locks it) is refused before the DAQ is armed, so a client's next TTL never fires an unrecorded trial. An experiment folder that has gone (a network share that dropped) is made again with a new parameter log, or the trial is refused. A trial disarmed before its TTL, by `stop` or by closing Zapit, gets a `disarmed` row beside its `armed` row, even if the experiment path changed in between; that row is written after the outputs are safe, so a failure to write it is a warning. A trial whose power the laser maximum limited is flagged `power_capped`.
- The model state is read and written under the model lock, so a GUI poll cannot report a newly armed trial as idle and unlock the beam controls over it. The GUI's frame-timer poll (`Zapit.poll_state`) never waits for that lock: while a ramp-down holds it, the poll reports the last state, so the camera view does not freeze for the ramp.
- A `stop` that ramps down a check pattern also ends it, and so does an on-demand beam or laser write (which takes the AO lines), so the next trial is not refused as "pattern running". The laser calibration sweep stops any pattern in the GUI first, is refused while a scanner calibration runs, is disabled during a trial, and locks the main window for the sweep's whole life (not the dialog's: Esc and Close are refused mid-sweep, Cancel sweep stops it, and quitting cancels it before the hardware closes), so no other control writes the laser line mid-sweep. Its fit is used only once saved.

## Not verified on hardware

Everything was built and tested against the simulated backends. These need a rig:

1. `zapit/hardware/nidaq.py`: task creation, regeneration mode, the start trigger terminal, writing a new buffer to a running continuous task for the ramp-down (sequential writes should block until each buffer region has played), the DO task sharing `ao/SampleClock`, reusing a task by resetting `samp_quant_samp_per_chan`, and reading `total_samp_per_chan_generated` on a task waiting for its trigger (`has_started`).
2. `zapit/hardware/pylon.py`: node names differ between Basler models (handled by validity checks with fallbacks); ROI increments; `GrabStrategy_LatestImageOnly` with the frame-rate limit.
3. Spot detection thresholds on a real image at the calibration exposure.
4. Camera orientation flags versus scanner inversion flags: a mirrored image needs `invertXscanner` or `invertYscanner`, because the similarity transform is non-reflective.
5. The Bonsai workflow: validated structurally and against the package metadata, not yet opened in the Bonsai editor.
6. `scripts/migrate_rig.py` on a real older `laserFit.mat` (tested on a synthetic one).
7. That the AO lines hold their last value when the on-demand task closes and a clocked task is created and waits for its trigger (NI's idle output behaviour), which is why a laser set on demand is zeroed before a trial arms; and that the zero write and the task change put no glitch on the laser line.
8. The laser sweep's refusal thresholds (`zapit.laser.MAX_RMSE_FRACTION`, `MAX_DIP_FRACTION`) and its choice between a formula and the monotone curve (`FORMULA_NOISE_FACTOR`, `SMOOTH_POINTS`) and the flat-top note (`FLAT_TOP_FRACTION`) against a real photodiode curve; tested on simulated linear, quadratic, sigmoid, EOM, threshold, saturating and noise-only curves at 0.05% to 2% noise, and on sweeps of 21 points.

## How to test on a rig, in order

1. Migrate the rig's files (`uv run python scripts/migrate_rig.py`) or edit the default settings file, then `uv run zapit gui`; confirm the camera feed and that the exposure spinbox changes it.
2. Laser switch on at a low power with Point mode, click on the image, confirm the beam moves the expected way. If it moves the wrong way along an axis, flip the matching `invertXscanner` or `invertYscanner`.
3. Run the scanner calibration, check the residual printed to the console (tens of microns is normal) and the Check calibration pattern.
4. Calibrate the sample, Overlay stim sites, Zap all coords.
5. From Python: `ZapitClient().send_samples(condition=1, hardware_triggered=False)` then `stop()`; watch the laser and the masking LED on a scope, and confirm both read 0 V after the ramp.
6. Hardware trigger: `send_samples(stim_duration_s=1)` then pulse PFI0. Then arm again, do not pulse, call `stop()` and confirm the next `send_samples` is accepted.
7. Laser left on: in the GUI turn Point mode on (laser on) and click the image, then arm `send_samples(stim_duration_s=1)` from Python without pulsing. The scope must show the laser line at 0 V while the trial waits, and the trial must play when PFI0 is pulsed.
8. Bonsai: open `bonsai/.bonsai/Zapit.bonsai`, fix any node the editor flags, press Space to arm and T to fire.

## Possible next steps

- Spinnaker camera backend (`zapit/hardware/spinnaker.py`, same interface as `pylon.py`).
- A second laser on AO3 with the LED on the DO line (`Zapit.use_clocked_do` already exists).
- A condition schedule node in Bonsai for blocked designs.
- A `uv tool install` recipe for rigs via rig-setup.
