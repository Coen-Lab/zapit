# Settings

The settings file is `zapitSystemSettings.yml`. It is looked for in this order: the path in the `ZAPIT_SETTINGS` environment variable, `%USERPROFILE%\ZapitSettings\`, `C:\ZapitSettings\`. If none exists a default is written to the first location. The file is validated on every load: unknown keys are dropped, missing keys filled from defaults and invalid values replaced, with a backup in a `backups/` folder beside it. A file passed with `--settings` is never rewritten. The GUI writes changes back as you make them.

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
| `camera.connectionIndex` | 0 | Which camera when several are attached |
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
| `server.bind` | tcp://*:5555 | Bind address |
| `cache.ROI` | null | Last camera ROI, reapplied at start |
| `cache.previouslyLoadedFiles` | null | Recent stimulus configs for the File menu |

Files from the MATLAB version load unchanged. Their `NI.wrapper`, `general.openPythonBridgeOnStartup` and `tcpServer` entries are dropped and `experiment.defaultLaserFrequencyHz` is renamed.
