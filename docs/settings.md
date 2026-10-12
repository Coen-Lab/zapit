# Settings

The settings file is `zapitSystemSettings.yml`. It is looked for in the folder named by the `ZAPIT_SETTINGS` environment variable, then in `%USERPROFILE%\ZapitSettings\`. If none exists a default is written to the first of those. The defaults are placeholders: on real hardware Zapit refuses to stimulate (trials and check patterns) until at least one rig value in the file differs from them, so edit it first, then restart. The `cache` section does not count as an edit. The file is validated on every load: unknown keys are dropped, missing keys filled from defaults and invalid values replaced, with a backup in a `backups/` folder beside it. A file passed with `--settings` is never rewritten. The GUI writes changes back as you make them.

The laser fit lives beside it as `laserFit.yml`. Without one, power is mapped linearly between `laserMinMaxControlVolts` and `laserMinMax_mW`, and Zapit says so at start-up.

To bring over a rig's files from Zapit 1.x (settings, laser fit, recent configs, stimulus configs that store the modulation frequency per condition), run `uv run python scripts/migrate_rig.py` once; `--help` lists the options. Zapit itself reads only the current formats and stops with a message naming the script if it finds an unconverted `laserFit.mat`.

| Key | Default | Meaning |
|---|---|---|
| `general.maxSettingsBackUpFiles` | 50 | Backups kept in `backups/` |
| `NI.device_ID` | Dev1 | NI device name from NI MAX |
| `NI.samplesPerSecond` | 100000 | AO sample rate |
| `NI.triggerChannel` | PFI0 | Start trigger terminal |
| `scanners.voltsPerPixel` | 0.0022 | Nominal command volts per camera pixel of beam movement, before calibration |
| `scanners.invertXscanner` | false | Flip the X command |
| `scanners.invertYscanner` | false | Flip the Y command |
| `laser.name` | obis | Label only |
| `laser.laserMinMaxControlVolts` | [0, 5] | Control voltage range |
| `laser.laserMinMax_mW` | [0, 75] | Power at the sample at those voltages |
| `laser.maxValueInGUI` | 30 | Upper limit of the GUI power slider |
| `camera.backend` | basler | `basler` only, for now |
| `camera.connectionIndex` | 0 | Which camera when several are attached, counting from 0 |
| `camera.default_exposure` | 400 | Microseconds |
| `camera.micronsPerPixel` | 19.3 | Measured image scale |
| `camera.flipImageUD` | false | Flip frames vertically |
| `camera.flipImageLR` | false | Flip frames horizontally |
| `calibrateScanners.areaThreshold` | 500 | Largest blob (pixels) accepted as the beam |
| `calibrateScanners.calibration_power_mW` | 10 | Laser power during calibration and check patterns |
| `calibrateScanners.beam_calib_exposure` | 200 | Exposure during calibration, low so only the beam is bright |
| `calibrateScanners.bufferMM` | 2 | Grid margin from the image edge |
| `calibrateScanners.pointSpacingInMM` | 2 | Grid spacing |
| `calibrateSample.refAP` | 3 | AP (mm from bregma) of the second reference point |
| `experiment.defaultLaserModulationFrequencyHz` | 40 | Default for new configs |
| `experiment.defaultLaserPowerMW` | 5 | Default for new configs |
| `experiment.offRampDownDuration_ms` | 250 | Default for new configs |
| `experiment.blankingTime_ms` | 0.3 | Laser off time around each beam move |
| `experiment.blankOnsetShift_ms` | -0.12 | Positive starts the blank earlier |
| `experiment.blankOffsetShift_ms` | 0.25 | Positive ends the blank later |
| `server.enable` | true | Start the ZeroMQ server with the GUI |
| `server.bind` | tcp://127.0.0.1:5555 | Bind address. Local connections only by default; use `tcp://*:5555` to accept other machines |
| `cache.ROI` | null | Last camera ROI, reapplied at start |
| `cache.previouslyLoadedFiles` | null | Recent stimulus configs for the File menu, as a list of `{path, time}` |
