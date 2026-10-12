# Python API

Everything hangs off one object:

```python
from zapit import Zapit
z = Zapit(simulated=False)          # reads the settings file, connects camera and DAQ, starts the camera
```

`calibrate_scanners()` returns False when fewer than four spots are found, when it is cancelled with `z.abort_calibration = True`, or when the fit's mean residual is above 100 um (usually a mirrored view that needs `scanners.invertXscanner` or `invertYscanner`).

`Zapit(simulated=True)` substitutes a simulated camera and DAQ. `settings_path=` loads a specific settings file and never writes to it.

## Calibration

```python
z.scanner_calibration_points()      # (N, 2) grid in sample mm
ok = z.calibrate_scanners()         # sweeps, detects, fits; True on success
z.transform.scale, z.transform.rotation_deg
z.measure_pointing_accuracy()       # list of SpotMeasurement with .error_um
z.wipe_scanner_calib()

z.calibrate_sample([[bx, by], [rx, ry]])   # bregma and the point at refAP, in sample mm
z.apply_unity_sample_calib()               # for tests: stereotaxic == sample
z.calibrated_brain_outline()               # atlas outline on the sample
z.is_ready_to_stim
```

Beam and laser:

```python
z.move_beam_mm([x, y]); z.move_beam_pixels([col, row]); z.zero_scanners()
z.set_laser_mw(2.0); z.set_laser_control_volts(0.0)
fit = z.generate_laser_calibration_curve(); z.save_laser_fit(fit)
```

`generate_laser_calibration_curve` sweeps 201 points over `laser.laserMinMaxControlVolts` (`min_max_volts=` and `points=` change them) and returns the fit without using it; `save_laser_fit(fit)` saves it beside the settings file and puts it in use. The sweep raises `zapit.laser.LaserFitRefused` (a `ValueError` whose `fit` holds the refused fit and its readings) when the reading does not rise steadily with the control voltage (a photodiode out of the beam), `NotReadyError` when a client arms a trial mid-sweep (the trial keeps the lines), and returns None when cancelled with `z.abort_laser_sweep = True`.

## Stimulus configs

```python
cfg = z.load_stim_config("my_config.yml")   # raises StimConfigError when refused
cfg.n_conditions; cfg.conditions[0].points; cfg.conditions[0].area_names
z.calibrated_points()         # per condition, (P, 2) sample mm
z.chan_samples()              # (n_samples, 4, n_conditions) one modulation cycle each
```

## Trials

```python
r = z.send_samples()                                   # random condition, laser on, waits for the TTL
r = z.send_samples(condition=3, laser_on=None,         # condition 3, random laser state
                   stim_duration_s=1.5, start_delay_s=0.2,
                   laser_power_mw=1.0, hardware_triggered=False)
r.condition, r.laser_on, r.laser_power_mw, r.waveforms
z.state                       # 'idle', 'stim', 'rampdown'; 'idle' again once a finite trial has played
z.poll_state()                # the same, but never waits for the model lock (for GUI timers)
z.trial_active                # True while a trial is armed, playing or ramping down
z.stop_opto_stim()            # ramp down a continuous stimulus (config ramp-down, or pass ms);
                              # disarms a trial (finite or continuous) that was never triggered
z.set_experiment_path("D:/data/mouse1/session3")      # enables logging; creates the folder if needed
```

`send_samples` raises `NotReadyError` when a calibration or config is missing, a trial is armed or playing (stop a continuous one first; arming during its ramp-down zeroes the outputs, then arms), a check pattern is running, logging is on and the trials CSV or parameter log cannot be written, or the experiment folder has gone and cannot be made again (nothing is armed then; a folder that can be made again is, with a new parameter log), or (on hardware) the settings file still holds the placeholder `laser.laserMinMax_mW`, `scanners.voltsPerPixel` and `camera.micronsPerPixel`; `ValueError` for a bad condition number. While `trial_active`, beam moves, laser writes, check patterns, scanner calibration and `load_stim_config` raise `NotReadyError`. After a ramp-down the laser and scanners are set to 0 V, and a laser or beam set on demand (point mode, a laser sweep) is set to 0 V before a trial arms, because the lines hold their last value until the trigger.

Logging writes one `zapit_log_<time>.yml` (the config, its absolute path, version, hostname) and one `zapit_trials_<time>.csv` per experiment folder and config; loading another config starts a new pair. A trial is logged only once the DAQ has accepted it, with a millisecond timestamp of when it was armed. The trials CSV columns are `timestamp, stim_index, laser_power` (time-averaged mW requested), `hardware_triggered, stim_duration` (-1 continuous), `laser_on, power_capped` (1 when the laser maximum limited the delivered power) and `event`: `armed` (waiting for the TTL), `started` (software start) or `disarmed` (a row added when `stop` or `close` cancelled that trial before its TTL, so it never played).

## Callbacks

```python
z.on_state.connect(print)              # 'stim', 'rampdown', 'idle'
z.on_calibration_point.connect(lambda m: print(m.error_um))
z.on_stim_config.connect(lambda cfg: ...)
```

Signals: `on_state`, `on_stim_config`, `on_scanners_calibrated`, `on_sample_calibrated`, `on_calibration_point`, `on_experiment_path`, `on_settings`. They fire on the thread that did the work.

## Check patterns (continuous until `stop_pattern()`)

`zap_all_coords()`, `zap_site(n)`, `check_scanner_calibration()`, `draw_brain_outline()`, `draw_areas([i, j])`. `stop_pattern()` never touches a trial armed from the API or the server.

## Server and client

```python
from zapit.server import ZapitServer
s = ZapitServer(z, "tcp://*:5555"); s.start()

from zapit.client import ZapitClient
c = ZapitClient("tcp://localhost:5555")
c.status(); c.send_samples(stim_duration_s=1); c.stop()
```

The wire protocol is in `docs/bonsai.md`.

## Shutdown

`z.close()` stops any task without a ramp, sets the laser and scanners to 0 V, and releases the camera and DAQ.
