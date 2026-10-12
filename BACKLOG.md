# Backlog

## Audit 2026-10-12: not recommended (steelman holds)

- Bonsai `Request` node has no receive timeout: a Bonsai.ZeroMQ 0.2.0 limitation, documented in `bonsai/README.md`.
- Trials file is `.csv` rather than CSV content in a `.yml`: deliberate, in the changelog.
- `stim_duration_ms` keeps the `- blanking_time_ms / 2` term for multi-point conditions: the power maths stay identical to the measured behaviour of v1.
- The config editor rounds points to 0.01 mm on add: 10 um, below the beam size.
- A `NI.triggerChannel` change at runtime does not rebuild an existing task: settings are not editable at runtime.
- No GitHub-issue or support-report menu items, and no undo in the stim config editor: dropped deliberately; v1 had no undo either.
- A read-only settings file stops start-up: fails loudly, which is right for a file the app must be able to write.
- Shared `zmq.Context.instance()` between server and client in one process, and one directory glob per logged trial: no effect at the rates involved.

## Audit 2026-10-12 (second, on the fix round): not recommended (steelman holds)

- The model lock is held while the ramp-down buffers are written: needed to keep the ramp ordered, and it lasts about one ramp length.
- The fixed 0.2 s delayed stop after a ramp-down: on NI the buffer writes block, so the timer starts near the end of the ramp; truncation needs a modulation rate below 5 Hz, and the outputs still end at 0 V.
- The log file is written under the model lock, and the GUI infers a trial's laser state from the loaded waveform: milliseconds, and correct.
- The window seemed to outgrow small screens during sample calibration: an offscreen-font artefact; with real fonts the minimum width is 622 px.
- Invalid settings values are replaced by defaults with a console line and a backup: v1 behaviour; a whole 1.x file is now refused instead.

## Audit 2026-10-12 (third, on 8c25947): not recommended (steelman holds)

- A 1.x settings file stops start-up with a traceback: its last line is the full message naming the command, as for an old `laserFit.mat`.
- `laser_power` in the trials CSV is the requested power even when capped: `power_capped` marks those rows, and it matches `mw_to_control`'s capping with and without a fit.
- `on_state` is emitted while the model lock is held: a queued Qt signal from other threads, and a re-entrant lock on the GUI thread, so no deadlock.
- `set_experiment_path` creates folders on a server request: the server binds localhost by default, and a client that can fire the laser can do more than make a folder.
- A hand-edited current settings file holding a `tcpServer` section is refused as 1.x: the message names the converter, which handles it.
- `roi_from_mm` does not snap to the camera's ROI increments: pylon snaps, and `roi_offset_mm` reads back the snapped ROI.
- The migration searches this checkout's `SETTINGS` folder: that is 1.x's `getInstallPath` for a rig updated in place, and `--from` covers any other.
- On NI, `has_started` may read 0 microseconds after a software start, so a stop at that instant disarms without a ramp: nothing meaningful has played.
- No trials-CSV row when a continuous trial stops or a TTL arrives: 1.x logged only the arming, and Bonsai's reply log has the timing.

## Audit 2026-10-12 (fourth, on 4b19317): not recommended (steelman holds)

- Pylon keeps the previous ROI's last frame until the next grab, where the simulated camera clears it: one frame (40 ms), and the next user action comes seconds later.
- A `stop` that ramps down a check pattern counts the ramp's buffers at the stimulus sample rate, not the pattern's: only the number of ramp buffers changes, and the outputs still end at 0 V.
- A config loaded from the GUI is not under the model lock while a server trial arms: a millisecond window, and loading is refused under an armed trial.
- `tests/data/legacy_rig/laserFit.mat` and the word MATLAB in `scripts/migrate_rig.py`: the converter's test needs a real 1.x file; nothing runs MATLAB.

## Audit 2026-10-12 (fifth, on 812292a): not recommended (steelman holds)

- A DAQ refusal after the pre-arm log check leaves a parameter log with no trials CSV: it records only the config, and the next trial in that folder reuses it.
- Bonsai's `WithLatestFrom` sends a TTL at the next onset after a refused arm, on the last good reply: it reaches an idle DAQ, and the refusal is in Bonsai's reply log.
- `_pattern_running` stays set if `write_unclocked` raises: only on a hardware fault, and Stop pattern clears it.
- The laser sweep cancels a sample calibration in progress: right before the laser is driven over the sample.
- A hand-trimmed 1.x settings file with none of the `old_layout_markers` keys: 1.x fills in missing keys, and `general.openPythonBridgeOnStartup` is in its defaults from their first commit (f9e6572).
- Backups numbered `.bak2` rather than `.2.bak`: cosmetic.
- Timing thresholds in the freeze and sweep tests: twice the margin of the effect they detect.
- Setting the same experiment path again starts a new log pair: one pair per `set_experiment_path`, by design.
- (Reopened and fixed in the sixth audit: quitting during a laser sweep. The probe used a 4 s sweep; a rig's 20 s sweep outlasted the dialog's 10 s wait and Qt aborted.)

## Audit 2026-10-12 (sixth, on f24ea27): not recommended (steelman holds)

- Zeroing before an arm also centres the scanners: a parked beam moves anyway when the trial plays, and the extra on-demand task comes only on the first arm after hand use.
- One filesystem check of the experiment folder per logged arm: it replaced an `is_dir()` that already touched the share each time.
- A remade folder holds a second log pair for the session: the first went with the share, timestamps tell them apart, and the console now says so.
- The lone `laserFit.mat` conversion works only in place: the app prints the in-place command, the only case that arises.
- `_unwritable` says "the trials log" for an `OSError` without a filename: that is a full disk, and its error text says so.
- `prepare_laser_sweep` returns a refusal text or None: clear, and tested.
- A long refusal is clipped in the status bar: it never widens the window, and at 1400 px the remedy is visible.

## Audit 2026-10-12 (seventh, on 671f22a): not recommended (steelman holds)

- The menus stay live during a laser sweep: none of them writes the AO lines, and Quit cancels the sweep first.
- The sweep lock lifts only from the worker's done or failed slot: `Worker` reports every exception through `failed`.
- `open_laser_dialog` deletes a hidden old dialog: a dialog cannot be hidden while it sweeps (Esc and Close are refused).
- Quitting asks about an unsaved fit in the main window, not through the dialog's `may_leave`: one question, then the app exits with the dialog.
- Tooltips repeat facts kept elsewhere (25 mV steps, file names, settings keys): the CLAUDE.md rule keeps them in step; tying the strings to constants would make them harder to read.
- The Help boxes are 400 px wide `QMessageBox`es: readable with real fonts, and they stay open beside the window.
- Documentation links open `main` on GitHub, not the installed version: the repo is public and the docs change with the code.
- The 30 s wait for a sweep on quit: a sweep step lasts about 0.1 s, so the wait ends within one step.
