# Python API

Everything hangs off one object:

```python
from zapit import Zapit
z = Zapit(simulated=False)          # reads the settings file, connects camera and DAQ, starts the camera
```

`Zapit(simulated=True)` substitutes a simulated camera and DAQ. `settings_path=` loads a specific settings file and never rewrites it.

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
z.generate_laser_calibration_curve(); z.save_laser_fit()
```

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
z.state                       # 'idle', 'stim', 'rampdown'
z.stop_opto_stim()            # ramp down a continuous stimulus (config ramp-down, or pass ms)
z.set_experiment_path("D:/data/mouse1/session3")      # enables logging
```

`send_samples` raises `NotReadyError` when a calibration or config is missing or a finite trial is still playing, and `ValueError` for a bad condition number.

## Callbacks

```python
z.on_state.connect(print)              # 'stim', 'rampdown', 'idle'
z.on_calibration_point.connect(lambda m: print(m.error_um))
z.on_stim_config.connect(lambda cfg: ...)
```

Signals: `on_state`, `on_stim_config`, `on_scanners_calibrated`, `on_sample_calibrated`, `on_calibration_point`, `on_experiment_path`, `on_settings`. They fire on the thread that did the work.

## Check patterns (continuous until `stop_pattern()`)

`zap_all_coords()`, `zap_site(n)`, `check_scanner_calibration()`, `draw_brain_outline()`, `draw_areas([i, j])`.

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

`z.close()` ramps down, zeroes the laser and scanners, and releases the camera and DAQ.
