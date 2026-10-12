# Hardware

The optical design, parts list and alignment procedure are in the Zapit manual at https://zapit.swcmicroscopy.com. This page covers what the software expects.

## DAQ

An NI X-series or similar multifunction DAQ with four analog outputs, one analog input, one PFI line and (optionally) one digital line. The device name is `NI.device_ID` in the settings, the sample rate `NI.samplesPerSecond` (100 kHz by default; anything the device supports that divides the modulation frequency cleanly).

| Line | Signal | Notes |
|---|---|---|
| AO0 | Galvo X command | `scanners.voltsPerPixel` sets the scale; `invertXscanner` flips it |
| AO1 | Galvo Y command | `invertYscanner` flips it |
| AO2 | Laser analog modulation | 0 to `laser.laserMinMaxControlVolts[1]` V; mW mapping from the settings or a measured fit |
| AO3 | Masking LED | 5 V TTL while the laser is gated on, so the animal cannot use the laser flash as a cue |
| PFI0 | Start trigger input | Rising edge starts an armed trial. Configurable as `NI.triggerChannel`, written either as `PFI0` or with the device, `/Dev1/PFI0` |
| AI0 | Photodiode | Only for the laser power calibration sweep. Read differentially (AI0 against AI8) over +/-10 V |
| port0/line0 | LED gate as a clocked digital line | Optional, `Zapit.use_clocked_do = True`; frees AO3 for a second laser |

All outputs are in the +/-10 V range; the code refuses waveforms outside it.

Timing: the waveform for one trial is loaded before the trigger arrives. After the trigger the DAQ plays it with hardware timing, so there is no software jitter at onset. Software-triggered trials start within a few milliseconds of the call.

## Trigger source

Any TTL works. The Bonsai example uses a Harp Behavior board digital output wired to PFI0 with a common ground. Keep the pulse to a few milliseconds; the trigger is edge-sensitive, so a long pulse does no harm but a second pulse while a finite trial is playing is ignored.

## Camera

Basler cameras through pypylon. Set `camera.backend: basler` (the default) and `camera.connectionIndex` when more than one is attached (the GUI console lists them at start). `connectionIndex` is 0-based: 0 is the first camera in that list, and an index past the end is an error. Older cameras that name their features `ExposureTimeAbs`, `GainRaw` and `AcquisitionFrameRateAbs` are supported; on those the gain is set to the lowest raw value, since raw gain units differ between models. `micronsPerPixel` must be measured for your lens (image a ruler). The default exposure is in microseconds. ROI is applied on the camera, not in software, so the frame rate stays high when cropped.

Adding another camera: subclass `zapit.hardware.camera.Camera`, implement the abstract members, and add a branch in `zapit.hardware.make_camera`.

## Laser

Any laser with an analog modulation input. `laser.laserMinMaxControlVolts` is the voltage range to drive it over, `laser.laserMinMax_mW` the measured power at the sample at those two voltages. Lasers that are linear in between need nothing else. For a non-linear laser or an EOM, run the laser calibration (File, Laser calibration) with a photodiode on AI0: Zapit sweeps the control voltage in 25 mV steps, fits a cubic and a sigmoid and shows the better one. A sweep whose reading does not rise steadily with the voltage is refused. Save and use fit stores it as `laserFit.yml` next to the settings file and puts it in use; until then trials keep the fit they had. A sweep takes about 20 s over 0 to 5 V, the rest of the window is locked while it runs, and Cancel sweep stops it.

## Scanners

Saturn or similar fast galvos. The blanking time (`experiment.blankingTime_ms`, 0.3 ms default) is how long the laser is gated off around each beam move; slower scanners need more. The onset and offset shifts fine-tune the gate against scanner lag; measure with a photodiode if you change scanners.
