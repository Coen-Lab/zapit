"""The Zapit model: hardware, calibrations, stimulus config and the experiment API.

One ``Zapit`` instance owns the camera and DAQ. The GUI and the ZeroMQ server are
both thin layers over it. Everything here runs headless; ``simulated=True`` uses
software stand-ins for the camera and DAQ that are faithful enough to run the
full calibration and stimulation flows.

Coordinate conventions (see zapit.transforms): "sample mm" always means mm in
the camera frame with the origin at the centre of the full sensor, x along
columns and y along rows. Pixel coordinates refer to the current ROI image.
"""
from __future__ import annotations

import csv
import datetime as _dt
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from zapit import calibration as calib
from zapit import settings as settings_mod
from zapit import waveforms as wf
from zapit.atlas import load_atlas
from zapit.events import Signal
from zapit.hardware import make_camera, make_daq
from zapit.hardware.camera import SimulatedCamera
from zapit.hardware.daq import SimulatedDAQ
from zapit.laser import LaserCalibration, LaserFit
from zapit.stim_config import StimConfig, StimConfigError
from zapit.transforms import ScannerMapping, Similarity, rotate_and_scale

STATES = ("idle", "stim", "rampdown")
LOG_STEM = "zapit_log_"
# event: "armed" (waiting for the TTL), "started" (software start) or "disarmed" (stopped before its TTL).
# power_capped: 1 when the laser maximum limited the delivered power below laser_power.
TRIALS_HEADER = ["timestamp", "stim_index", "laser_power", "hardware_triggered", "stim_duration", "laser_on",
                 "power_capped", "event"]
# A good scanner fit leaves tens of microns; a mirrored view or a wrong scale leaves millimetres.
MAX_CALIBRATION_RESIDUAL_UM = 100.0


@dataclass
class TrialResult:
    condition: int           # 1-based index of the presented condition
    laser_on: bool
    laser_power_mw: float    # time-averaged power requested (0 when laser off)
    hardware_triggered: bool
    stim_duration_s: float   # -1 for continuous
    waveforms: np.ndarray


class NotReadyError(RuntimeError):
    pass


def _append_trial_row(trials: Path, row: list, event: str):
    """One timestamped row of a trials CSV, with the header if the file is new."""
    new = not trials.exists()
    with open(trials, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(TRIALS_HEADER)
        w.writerow([_dt.datetime.now().isoformat(sep=" ", timespec="milliseconds"), *row, event])


class Zapit:
    def __init__(self, simulated: bool = False, settings_path: str | Path | None = None,
                 start_camera: bool = True):
        self.simulated = bool(simulated)
        self.settings_path = Path(settings_path) if settings_path else settings_mod.find_settings_file()
        self.settings = settings_mod.load_settings(self.settings_path, write_back=settings_path is None)
        self._settings_read_only = settings_path is not None
        # A rig must not stimulate on the placeholder laser range and scales; simulation may.
        self.settings_unedited = not simulated and settings_mod.is_unedited_default(self.settings)

        # Observable state
        self.on_state = Signal("state")
        self.on_stim_config = Signal("stim_config")
        self.on_scanners_calibrated = Signal("scanners_calibrated")
        self.on_sample_calibrated = Signal("sample_calibrated")
        self.on_calibration_point = Signal("calibration_point")
        self.on_experiment_path = Signal("experiment_path")
        self.on_settings = Signal("settings")

        self._state = "idle"
        self._trial_id = 0          # bumped by every send_samples, so a late ramp-down stop cannot hit a newer trial
        self._pattern_running = False
        self.stim_config: StimConfig | None = None
        self.experiment_path: Path | None = None
        self._log_files: tuple[Path, Path] | None = None   # (parameter log, trials csv) of this session
        # (trial id, trials csv, row) of the last logged trial, so its disarm row follows it to that file
        self._logged_trial: tuple[int, Path, list] | None = None
        self.last_condition: int = 0
        self.transform: Similarity | None = None
        self.scanners_calibrated = False
        self.sample_calibrated = False
        self.ref_points_sample = np.zeros((2, 2))
        self.calibration_data: list[calib.SpotMeasurement] = []
        self.abort_calibration = False
        self.use_clocked_do = False
        self.atlas = load_atlas()
        self.lock = threading.RLock()

        self.laser = LaserCalibration(self.settings["laser"]["laserMinMaxControlVolts"],
                                      self.settings["laser"]["laserMinMax_mW"])
        self.load_laser_fit()
        self.mapping = ScannerMapping(self.settings["camera"]["micronsPerPixel"],
                                      self.settings["scanners"]["voltsPerPixel"],
                                      self.settings["scanners"]["invertXscanner"],
                                      self.settings["scanners"]["invertYscanner"])

        # Hardware
        self.daq = make_daq(self.settings, self.simulated)
        self.cam = make_camera(self.settings, self.simulated)
        if self.simulated:
            self._wire_simulation()
        self.cam.exposure = self.settings["camera"]["default_exposure"]
        if self.settings["cache"]["ROI"]:
            try:
                self.cam.roi = tuple(int(round(v)) for v in self.settings["cache"]["ROI"])
            except Exception as e:
                print(f"Could not apply cached ROI: {e}")
        if start_camera:
            self.cam.start()

        self._last_xy_volts = np.zeros(2)
        self._last_laser_volts = 0.0
        self.zero_scanners()

    # ================================================================== simulation wiring
    def _wire_simulation(self):
        """Hidden distortion between commanded and true beam position: what calibration must recover."""
        cam: SimulatedCamera = self.cam
        daq: SimulatedDAQ = self.daq
        self.sim_true_transform = Similarity(np.array([[1.04 * np.cos(0.05), -1.04 * np.sin(0.05), 0.35],
                                                       [1.04 * np.sin(0.05), 1.04 * np.cos(0.05), -0.2],
                                                       [0, 0, 1]]))
        # The physical scanner: +X volts moves the beam along +sensor columns. The invert settings are the
        # user's correction for a mirrored view, so they belong to the model, never to the simulated physics.
        physical = ScannerMapping(self.mapping.microns_per_pixel, self.mapping.volts_per_pixel)

        def beam_pixel():
            cmd_mm = physical.volts_to_mm(daq.beam_volts)[0]
            true_mm = self.sim_true_transform.forward(cmd_mm)[0]
            px = true_mm / self.mapping.mm_per_pixel + np.asarray(cam.sensor_size) / 2
            # The spot saturates the sensor at modest powers, as a real beam does.
            amp = min(1.0, daq.laser_volts / 0.1)
            return float(px[0]), float(px[1]), amp

        cam.beam_pixel = beam_pixel
        daq.ai_source = lambda ch: 0.2 * daq.laser_volts ** 2 + 2 * daq.laser_volts + np.random.randn() * 0.02

    # ================================================================== state and settings
    @property
    def state(self) -> str:
        # A finite trial ends by itself; report that rather than a stale "stim". Under the lock, so a
        # poll can never overwrite the "stim" of a trial armed between its check and its write.
        with self.lock:
            if self._state == "stim" and self.daq.ao_mode() == "finite" and self.daq.is_ao_done():
                self.state = "idle"
            return self._state

    def poll_state(self) -> str:
        """The state for a frequent GUI poll: never waits for the lock (a ramp-down holds it for the ramp's
        length), so while the model is busy it reports the last state and leaves the finite-done check."""
        if not self.lock.acquire(blocking=False):
            return self._state
        try:
            return self.state
        finally:
            self.lock.release()

    @property
    def trial_active(self) -> bool:
        """A trial is armed, playing or ramping down: the AO lines belong to it."""
        return self.state in ("stim", "rampdown") and self.daq.ao_mode() is not None

    def _refuse_during_trial(self, what: str):
        if self.trial_active:
            raise NotReadyError(f"Cannot {what} while a trial is armed or playing; stop it first")

    def _require_rig_settings(self):
        if self.settings_unedited:
            raise NotReadyError(f"The settings file {self.settings_path} still holds the shipped placeholder "
                                f"{settings_mod.rig_keys_text()}. Set them for this rig first (docs/settings.md), "
                                f"or migrate an existing rig with scripts/migrate_rig.py, then restart")

    @state.setter
    def state(self, value: str):
        if value not in STATES:
            raise ValueError(value)
        if value != self._state:
            self._state = value
            self.on_state.emit(value)

    def save_settings(self):
        if self._settings_read_only:
            return
        settings_mod.save_settings(self.settings, self.settings_path)
        self.on_settings.emit(self.settings)

    def update_setting(self, section: str, key: str, value):
        self.settings[section][key] = value
        self.save_settings()

    @property
    def image_size(self) -> tuple[int, int]:
        return self.cam.image_size

    @property
    def blanking(self) -> wf.BlankingParams:
        e = self.settings["experiment"]
        return wf.BlankingParams(e["blankingTime_ms"], e["blankOnsetShift_ms"], e["blankOffsetShift_ms"])

    @property
    def ref_points_stereotaxic(self) -> np.ndarray:
        return np.array([[0.0, 0.0], [0.0, float(self.settings["calibrateSample"]["refAP"])]])

    def close(self):
        print("Shutting down Zapit")
        try:
            self._force_idle()
        except Exception as e:
            print(f"Could not zero the outputs on shutdown: {e}")
        self.cam.close()
        self.daq.close()

    def _force_idle(self):
        """Drop any clocked task and put laser 0 V, scanners at 0 V. No ramp. A trial still waiting for its
        trigger gets its ``disarmed`` row, as from ``stop``."""
        with self.lock:
            untriggered = self._state == "stim" and self.daq.has_clocked_task and not self.daq.has_started()
            self._pattern_running = False
            self.daq.clear_do()
            self.daq.clear_ao()
            self._last_xy_volts = np.zeros(2)
            self._last_laser_volts = 0.0
            self.daq.write_unclocked([0.0, 0.0, 0.0, 0.0])
            self.state = "idle"
            if untriggered:
                self._log_disarmed()
            self._trial_id += 1   # a pending ramp-down stop must not act after this

    # ================================================================== coordinates
    @property
    def _flip_signs(self) -> np.ndarray:
        """-1 on each axis the camera flips: the displayed image, and so sample mm, mirror the sensor there."""
        return np.array([-1.0 if self.cam.flip_lr else 1.0, -1.0 if self.cam.flip_ud else 1.0])

    @property
    def roi_offset_mm(self) -> np.ndarray:
        """Sample-mm position of the current ROI centre relative to the sensor centre."""
        x, y, w, h = self.cam.roi
        sw, sh = self.cam.sensor_size
        return (np.array([x + w / 2, y + h / 2]) - np.array([sw, sh]) / 2) * self.mapping.mm_per_pixel * self._flip_signs

    def roi_from_mm(self, x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
        """Sensor ROI (x, y, width, height) in pixels covering the sample-mm rectangle with corner (x, y)."""
        sw, sh = self.cam.sensor_size
        m = self.mapping.mm_per_pixel
        x0 = sw / 2 - (x + w) / m if self.cam.flip_lr else sw / 2 + x / m
        y0 = sh / 2 - (y + h) / m if self.cam.flip_ud else sh / 2 + y / m
        return x0, y0, w / m, h / m

    def pixels_to_mm(self, xy_px) -> np.ndarray:
        """ROI pixel (col, row) -> sample mm."""
        return self.mapping.pixels_to_mm(xy_px, self.image_size) + self.roi_offset_mm

    def mm_to_pixels(self, xy_mm) -> np.ndarray:
        return self.mapping.mm_to_pixels(np.atleast_2d(xy_mm) - self.roi_offset_mm, self.image_size)

    def image_extent_mm(self) -> tuple[float, float, float, float]:
        """(x0, y0, width, height) of the current image in sample mm, for plotting."""
        w, h = self.image_size
        x0, y0 = self.pixels_to_mm([0, 0])[0]
        return float(x0), float(y0), w * self.mapping.mm_per_pixel, h * self.mapping.mm_per_pixel

    # ================================================================== beam and laser
    def set_laser_control_volts(self, volts: float):
        with self.lock:
            # An on-demand write takes the AO lines from a clocked task, which would silently drop a trial.
            self._refuse_during_trial("set the laser")
            self._last_laser_volts = float(volts)
            self.daq.write_unclocked([*self._last_xy_volts, self._last_laser_volts, 0.0])
            self._pattern_running = False   # the write took the AO lines from any check pattern

    def set_laser_mw(self, mw: float):
        self.set_laser_control_volts(self.laser.mw_to_control(mw))

    def move_beam_volts(self, xy):
        xy = np.asarray(xy, dtype=float).ravel()
        with self.lock:
            self._refuse_during_trial("move the beam")
            self._last_xy_volts = xy.copy()
            self.daq.write_unclocked([*xy, self._last_laser_volts, 0.0])
            self._pattern_running = False   # the write took the AO lines from any check pattern

    def move_beam_mm(self, xy_mm):
        self.move_beam_volts(self.mapping.mm_to_volts(xy_mm)[0])

    def move_beam_pixels(self, xy_px):
        self.move_beam_mm(self.pixels_to_mm(xy_px)[0])

    def zero_scanners(self):
        self.move_beam_volts([0.0, 0.0])

    def set_cam_roi(self, roi):
        self.cam.roi = tuple(int(round(v)) for v in roi)
        self.wipe_scanner_calib()
        self.settings["cache"]["ROI"] = [int(v) for v in self.cam.roi]
        self.save_settings()

    def reset_cam_roi(self):
        self.cam.reset_roi()
        self.wipe_scanner_calib()
        self.settings["cache"]["ROI"] = None
        self.save_settings()

    # ================================================================== frames
    def last_frame(self) -> np.ndarray | None:
        return self.cam.get_last_frame()

    def average_frames(self, n: int = 5, timeout_s: float = 5.0) -> np.ndarray | None:
        """Mean of the next ``n`` distinct frames (float array)."""
        frames = []
        seen = self.cam.frames_acquired
        t0 = time.monotonic()
        while len(frames) < n and time.monotonic() - t0 < timeout_s:
            f = self.cam.get_last_frame()
            cnt = self.cam.frames_acquired
            if f is not None and (cnt != seen or not frames):
                frames.append(np.asarray(f, dtype=np.float64))
                seen = cnt
            else:
                time.sleep(0.005)
        if not frames:
            return None
        return np.mean(frames, axis=0)

    # ================================================================== scanner calibration
    def wipe_scanner_calib(self):
        self.transform = None
        self.mapping.transform = None
        self.calibration_data = []
        if self.scanners_calibrated:
            self.scanners_calibrated = False
            self.on_scanners_calibrated.emit(False)

    def scanner_calibration_points(self, spacing_mm: float | None = None, buffer_mm: float | None = None) -> np.ndarray:
        cs = self.settings["calibrateScanners"]
        return calib.calibration_grid(self.image_size, self.mapping.mm_per_pixel,
                                      cs["pointSpacingInMM"] if spacing_mm is None else spacing_mm,
                                      cs["bufferMM"] if buffer_mm is None else buffer_mm,
                                      self.roi_offset_mm)

    def measure_beam_position(self, target_mm, background: np.ndarray | None = None) -> calib.SpotMeasurement | None:
        """Find the beam in the current frame and compare to where it was sent."""
        frame = self.cam.get_last_frame()
        if frame is None:
            return None
        found = calib.find_beam_spot(frame, background, self.settings["calibrateScanners"]["areaThreshold"],
                                     crop_half_px=int(round(375 / self.mapping.microns_per_pixel)))
        if found is None:
            return None
        centroid_px, area, crop = found
        actual_mm = self.pixels_to_mm(centroid_px)[0]
        return calib.SpotMeasurement(np.asarray(target_mm, dtype=float), actual_mm, centroid_px, area, crop)

    def calibrate_scanners(self, settle_s: float | None = None) -> bool:
        """Sweep the beam over a grid, detect it on the camera and fit the scanner transform.

        Emits ``on_calibration_point`` after each detected point so a GUI can draw
        progress. Set ``abort_calibration`` to stop early. Returns True on success.
        """
        # Cleared on entry, before any wait, so a cancel pressed straight after starting is kept.
        self.abort_calibration = False
        settle = (0.05 if self.simulated else 0.1) if settle_s is None else settle_s
        cs = self.settings["calibrateScanners"]
        self.set_laser_mw(0)
        time.sleep(settle)
        self.cam.exposure = cs["beam_calib_exposure"]
        self.wipe_scanner_calib()
        grid = self.scanner_calibration_points()
        if len(grid) < 4:
            print(f"Scanner calibration needs at least 4 grid points but the grid has {len(grid)}: "
                  f"reduce calibrateScanners.pointSpacingInMM or bufferMM, or enlarge the ROI")
            self.set_laser_mw(0)
            self.cam.exposure = self.settings["camera"]["default_exposure"]
            return False
        nominal_mapping = ScannerMapping(self.mapping.microns_per_pixel, self.mapping.volts_per_pixel,
                                         self.mapping.invert_x, self.mapping.invert_y)
        try:
            self.move_beam_volts(nominal_mapping.mm_to_volts(grid[0])[0])
            time.sleep(settle)
            background = self.average_frames(5)
            self.set_laser_mw(cs["calibration_power_mW"])
            time.sleep(settle)
            for target in grid:
                if self.abort_calibration:
                    break
                self.move_beam_volts(nominal_mapping.mm_to_volts(target)[0])
                time.sleep(settle)
                m = self.measure_beam_position(target, background)
                if m is not None:
                    self.calibration_data.append(m)
                    self.on_calibration_point.emit(m)
            if self.abort_calibration:
                print("Scanner calibration cancelled")
                self.wipe_scanner_calib()
                return False
            if len(self.calibration_data) < 4:
                print(f"Scanner calibration failed: only {len(self.calibration_data)} of {len(grid)} points detected")
                self.calibration_data = []
                return False
            tf = self.fit_scanner_transform()
            target = np.array([m.target_mm for m in self.calibration_data])
            actual = np.array([m.actual_mm for m in self.calibration_data])
            mean_um = float(tf.residuals(target, actual).mean() * 1e3)
            if mean_um > MAX_CALIBRATION_RESIDUAL_UM:
                print(f"Scanner calibration rejected: mean residual {mean_um:.0f} um is above "
                      f"{MAX_CALIBRATION_RESIDUAL_UM:.0f} um. If the image is mirrored relative to the beam "
                      f"(a camera flip), set scanners.invertXscanner or invertYscanner to match, then recalibrate")
                self.wipe_scanner_calib()
                return False
            return True
        finally:
            self.cam.exposure = self.settings["camera"]["default_exposure"]
            self.set_laser_mw(0)
            self.zero_scanners()

    def fit_scanner_transform(self, target=None, actual=None) -> Similarity:
        if target is None or actual is None:
            target = np.array([m.target_mm for m in self.calibration_data])
            actual = np.array([m.actual_mm for m in self.calibration_data])
        tf = Similarity.fit(target, actual)
        self.transform = tf
        self.mapping.transform = tf
        if not self.scanners_calibrated:
            self.scanners_calibrated = True
            self.on_scanners_calibrated.emit(True)
        res = tf.residuals(target, actual) * 1e3
        print(f"Scanner calibration: {len(target)} points, scale {tf.scale:.4f}, rotation {tf.rotation_deg:.2f} deg, "
              f"residual {res.mean():.0f} um (max {res.max():.0f} um)")
        return tf

    def measure_pointing_accuracy(self, points_mm: np.ndarray | None = None, settle_s: float | None = None) -> list[calib.SpotMeasurement]:
        """Point the calibrated beam at each position and measure where it lands."""
        self.abort_calibration = False
        settle = (0.05 if self.simulated else 0.1) if settle_s is None else settle_s
        pts = self.scanner_calibration_points() if points_mm is None else np.atleast_2d(points_mm)
        cs = self.settings["calibrateScanners"]
        out = []
        self.set_laser_mw(0)
        self.cam.exposure = cs["beam_calib_exposure"]
        self.move_beam_mm(pts[0])
        time.sleep(settle)
        background = self.average_frames(5)
        self.set_laser_mw(cs["calibration_power_mW"])
        time.sleep(settle)
        try:
            for p in pts:
                if self.abort_calibration:
                    break
                self.move_beam_mm(p)
                time.sleep(settle)
                m = self.measure_beam_position(p, background)
                if m is not None:
                    out.append(m)
        finally:
            self.set_laser_mw(0)
            self.cam.exposure = self.settings["camera"]["default_exposure"]
        return out

    # ================================================================== sample calibration
    def calibrate_sample(self, ref_points_sample):
        """Set the two reference points (bregma, then the point at refAP) in sample mm."""
        pts = np.asarray(ref_points_sample, dtype=float)
        if pts.shape != (2, 2):
            raise ValueError("Need two [x, y] points: bregma and the reference point")
        self.ref_points_sample = pts
        self.sample_calibrated = True
        self.on_sample_calibrated.emit(True)

    def apply_unity_sample_calib(self):
        """Pretend the sample frame equals the stereotaxic frame (tests and dev)."""
        self.calibrate_sample(self.ref_points_stereotaxic)

    def wipe_sample_calib(self):
        self.ref_points_sample = np.zeros((2, 2))
        if self.sample_calibrated:
            self.sample_calibrated = False
            self.on_sample_calibrated.emit(False)

    def stereotaxic_to_sample(self, points_ml_ap) -> np.ndarray:
        if not self.sample_calibrated:
            raise NotReadyError("Sample is not calibrated")
        return rotate_and_scale(points_ml_ap, self.ref_points_stereotaxic, self.ref_points_sample)

    def calibrated_brain_outline(self) -> np.ndarray:
        return self.stereotaxic_to_sample(self.atlas.whole_brain)

    # ================================================================== stimulus config
    @property
    def is_ready_to_stim(self) -> bool:
        return self.scanners_calibrated and self.sample_calibrated and self.stim_config is not None

    def load_stim_config(self, source: str | Path | StimConfig) -> StimConfig:
        # The ramp-down and the log of a trial in progress belong to the config it was armed from.
        self._refuse_during_trial("load a stimulus config")
        cfg = source if isinstance(source, StimConfig) else StimConfig.load(source)
        bad = wf.unpresentable_conditions(cfg.points_per_condition(), self.daq.sample_rate, cfg.modulation_hz, self.blanking)
        if bad:
            raise StimConfigError(
                f"Conditions {[i + 1 for i in bad]} have too many points for the blanking settings at "
                f"{cfg.modulation_hz} Hz; reduce points, blanking time or modulation frequency")
        over = cfg.conditions_exceeding_power(self.laser.max_mw, self.blanking.blanking_time_ms)
        if over:
            print(f"WARNING: conditions {[i + 1 for i in over]} need more peak laser power than the laser "
                  f"maximum of {self.laser.max_mw:.2f} mW and will be capped")
        self.stim_config = cfg
        self.last_condition = 0
        self._log_files = None      # trials under a new config go to a new log pair
        self.on_stim_config.emit(cfg)
        try:
            self._remember_config(cfg)
        except OSError as e:
            print(f"Could not update the recent configs list: {e}")
        return cfg

    def _remember_config(self, cfg: StimConfig):
        if not cfg.path:
            return
        path = str(Path(cfg.path).resolve())
        recent = [r for r in (self.settings["cache"]["previouslyLoadedFiles"] or []) if r.get("path") != path]
        recent.insert(0, {"path": path, "time": _dt.datetime.now().isoformat(timespec="seconds")})
        self.settings["cache"]["previouslyLoadedFiles"] = recent[:10]
        self.save_settings()

    def recent_configs(self) -> list[Path]:
        return [Path(r["path"]) for r in (self.settings["cache"]["previouslyLoadedFiles"] or [])
                if Path(r["path"]).exists()]

    def clear_stim_config(self):
        self.stim_config = None
        self.on_stim_config.emit(None)

    def calibrated_points(self) -> list[np.ndarray]:
        """Per condition, (P, 2) sample-mm positions of its points."""
        if self.stim_config is None:
            raise NotReadyError("No stimulus config loaded")
        return [self.stereotaxic_to_sample(c.points) for c in self.stim_config.conditions]

    def calibrated_points_volts(self) -> list[np.ndarray]:
        return [self.mapping.mm_to_volts(p) for p in self.calibrated_points()]

    def chan_samples(self) -> np.ndarray:
        """(n_samples, 4, n_conditions): one modulation cycle per condition. Laser column is a 0/1 gate."""
        cfg = self.stim_config
        if cfg is None:
            raise NotReadyError("No stimulus config loaded")
        rate = self.daq.sample_rate
        err = abs(wf.actual_modulation_hz(rate, cfg.modulation_hz) - cfg.modulation_hz)
        if err > 0.5:
            print(f"WARNING: {cfg.modulation_hz} Hz cannot be produced exactly at {rate:g} samples/s; "
                  f"actual rate {wf.actual_modulation_hz(rate, cfg.modulation_hz):.2f} Hz")
        cycles = [wf.one_cycle(v, rate, cfg.modulation_hz, self.blanking, c.ephys)
                  for v, c in zip(self.calibrated_points_volts(), cfg.conditions)]
        return np.stack(cycles, axis=2)

    # ================================================================== experiment API
    def set_experiment_path(self, path: str | Path | None):
        """Set the folder trials are logged to, creating it if needed (raises OSError if it cannot be)."""
        if path:
            Path(path).mkdir(parents=True, exist_ok=True)
        self.experiment_path = Path(path) if path else None
        self._log_files = None
        self.on_experiment_path.emit(self.experiment_path)

    def send_samples(self, condition: int | None = None, laser_on: bool | None = True,
                     stim_duration_s: float = -1, start_delay_s: float = 0.0,
                     laser_power_mw: float | None = None, hardware_triggered: bool = True,
                     logging: bool = True, verbose: bool = False) -> TrialResult:
        """Load one trial's waveforms into the DAQ and start (or arm) them.

        condition: 1-based index; None picks a random condition (never the same twice running).
        laser_on: None picks randomly. False moves the scanners with the laser off.
        stim_duration_s: >0 plays for that long then ramps down; otherwise continuous until stop_opto_stim.
        start_delay_s: leading delay, only with a finite duration.
        laser_power_mw: time-averaged power override; None uses the config value.
        hardware_triggered: wait for a TTL on the trigger line instead of starting now.
        Raises NotReadyError if not calibrated / loaded, a check pattern is running, or a trial is
        armed or playing (a continuous one must be stopped first, so it always gets its ramp-down).
        """
        with self.lock:
            cfg = self.stim_config
            self._require_rig_settings()
            if not self.is_ready_to_stim:
                raise NotReadyError("Not ready: scanners and sample must be calibrated and a config loaded")
            if self._pattern_running:
                raise NotReadyError("A check pattern is running; stop it first")
            if self.daq.is_finite_playing():
                raise NotReadyError("A finite-duration stimulus is armed or playing; it must finish or be stopped")
            if self.state == "stim" and self.daq.ao_mode() == "continuous":
                raise NotReadyError("A continuous stimulus is armed or playing; stop it first")
            n = cfg.n_conditions
            if condition is not None and condition != -1:
                if not (isinstance(condition, (int, np.integer)) and 1 <= condition <= n):
                    raise ValueError(f"Condition must be 1..{n}, got {condition!r}")
            else:
                condition = int(np.random.randint(1, n + 1))
                while n > 1 and condition == self.last_condition:
                    condition = int(np.random.randint(1, n + 1))
            if laser_on is None or laser_on == -1:
                laser_on = bool(np.random.randint(0, 2))
            laser_on = bool(laser_on)
            cond = cfg.conditions[condition - 1]
            if laser_power_mw is not None and laser_power_mw == -1:
                laser_power_mw = None
            avg_mw = 0.0 if not laser_on else (cond.laser_power_mw if laser_power_mw is None else float(laser_power_mw))
            if verbose:
                print(f"Stimulating condition {condition} (laser {'on' if laser_on else 'off'})")

            cycle = self.chan_samples()[:, :, condition - 1].copy()
            peak_mw = cfg.peak_power_mw(condition - 1, self.blanking.blanking_time_ms, avg_mw if laser_on else 0.0)
            capped = laser_on and peak_mw > self.laser.max_mw
            # Ephys waveforms peak above 1, which assumes power is linear in control voltage.
            cycle[:, 2] *= self.laser.mw_to_control(peak_mw) if laser_on else 0.0
            if cycle[:, 2].max() > self.laser.max_cv:
                print(f"WARNING: condition {condition} would drive the laser to {cycle[:, 2].max():.2f} V, above its "
                      f"{self.laser.max_cv:.2f} V maximum; scaling the laser waveform down to the maximum")
                cycle[:, 2] *= self.laser.max_cv / cycle[:, 2].max()
                capped = True
            finite = stim_duration_s is not None and stim_duration_s > 0
            waves = wf.finite_trial(cycle, self.daq.sample_rate, stim_duration_s, cond.rampdown_ms, start_delay_s) if finite else cycle
            if not laser_on:
                waves[:, 2] = 0
            # A trial the log cannot record is refused before the DAQ is armed: an armed trial behind an error
            # reply would still be fired by the client's next TTL, with a condition nobody recorded.
            trials = self._trials_file() if logging and self.experiment_path and self.experiment_path.is_dir() else None
            if self.state == "rampdown":
                # The ramp has all but finished; zero the outputs so the new trial never arms over a held laser value.
                self._stop_and_zero()
            task = ("stim_htrig" if hardware_triggered else "stim_strig") + ("_finite" if finite else "")
            self.daq.prepare_ao(waves, finite=finite, hardware_triggered=hardware_triggered, task_name=task)
            if self.use_clocked_do:
                self.daq.prepare_do(waves[:, 3] > 0, finite=finite, task_name=task + "_do")
            else:
                self.daq.clear_do()
            self.daq.start()
            self.last_condition = int(condition)
            self._trial_id += 1
            self.state = "stim"
            # Logged once the DAQ has accepted the trial, so a refused trial leaves no row.
            self._logged_trial = None
            if trials is not None:
                row = [condition, f"{avg_mw:.2f}", int(hardware_triggered), f"{stim_duration_s:.2f}",
                       int(laser_on), int(capped)]
                try:
                    _append_trial_row(trials, row, "armed" if hardware_triggered else "started")
                except OSError as e:
                    # Writable a moment ago; the trial still must not stay armed unrecorded.
                    self._stop_and_zero()
                    raise NotReadyError(f"Trial not armed: the trials log {trials} cannot be written ({e})") from e
                self._logged_trial = (self._trial_id, trials, row)
        return TrialResult(condition, laser_on, avg_mw, bool(hardware_triggered),
                           float(stim_duration_s) if finite else -1.0, waves)

    def stop_opto_stim(self, rampdown_ms: float | None = None):
        """Ramp the laser down over ``rampdown_ms`` (config default) and stop a continuous stimulus.

        A trial that is armed but not yet triggered, finite or continuous, is disarmed.
        A finite trial that is playing is left to finish by itself (it carries its own ramp-down).
        """
        with self.lock:
            mode = self.daq.ao_mode()
            if mode is None:
                self.state = "idle"
                return
            if mode == "finite" and self.daq.is_ao_done():
                self.daq.stop()
                self.state = "idle"
                return
            if not self.daq.has_started():
                # Nothing has played, and a ramp-down written into a task waiting for its trigger
                # would wait for buffer space that never frees.
                self._stop_and_zero()
                self._log_disarmed()
                print("Disarmed a trial that had not been triggered")
                return
            if mode == "finite":
                return
            if rampdown_ms is None:
                i = max(self.last_condition - 1, 0)
                rampdown_ms = self.stim_config.conditions[i].rampdown_ms if self.stim_config else 0
            last = self.daq.last_waveform
            if last is None or self.daq.is_ao_done():
                self._stop_and_zero()
                return
            self.state = "rampdown"
            for buf in wf.rampdown_buffers(last, self.daq.sample_rate, rampdown_ms):
                self.daq.write_ao(buf)
            threading.Timer(0.2, self._delayed_stop, args=(self._trial_id,)).start()

    def _delayed_stop(self, trial_id: int):
        with self.lock:
            if trial_id != self._trial_id:
                return  # a newer trial was armed during the ramp-down; it owns the DAQ now
            self._stop_and_zero()

    def _stop_and_zero(self):
        """Stop the clocked task and set laser 0 V, scanners 0 V: a stopped AO task holds its last sample."""
        self.daq.stop()
        self.daq.clear_do()
        self._pattern_running = False   # a stop also ends a check pattern it ramped down
        self._last_xy_volts = np.zeros(2)
        self._last_laser_volts = 0.0
        self.daq.write_unclocked([0.0, 0.0, 0.0, 0.0])
        self.state = "idle"

    def _trials_file(self) -> Path:
        """The trials CSV for this experiment path and config, its parameter log written first, and proven
        writable. One parameter log and one trials CSV per (experiment path, config) for this session.
        Raises NotReadyError naming the file when either cannot be written (Excel locks a CSV it has open)."""
        try:
            if self._log_files is None or self._log_files[0].parent != self.experiment_path:
                log = self.stim_config.log_parameters(self.experiment_path, {"hostname": socket.gethostname()})
                print(f"Wrote stimulus parameter log {log}")
                self._log_files = (log, log.with_name(log.name.replace("_log_", "_trials_")).with_suffix(".csv"))
            trials = self._log_files[1]
            if trials.exists():   # a new one goes in the folder the parameter log was just written to
                with open(trials, "a", encoding="utf-8"):
                    pass
        except OSError as e:
            raise NotReadyError(f"Trial not armed: the trials log in {self.experiment_path} cannot be written "
                                f"({e}). Close it if another program has it open") from e
        return trials

    def _log_disarmed(self):
        """A ``disarmed`` row for the current trial if it was logged: beside its armed row, wherever the
        experiment path points now, and once only. Called with the outputs already safe, so a write
        failure is reported rather than raised."""
        if self._logged_trial is None or self._logged_trial[0] != self._trial_id:
            return
        _, trials, row = self._logged_trial
        self._logged_trial = None
        try:
            _append_trial_row(trials, row, "disarmed")
        except OSError as e:
            print(f"WARNING: the disarmed row could not be written to {trials}: {e}")

    # ================================================================== laser calibration
    def generate_laser_calibration_curve(self, min_max_volts=None, step: float = 0.025, n_reads: int | None = None) -> LaserFit:
        """Sweep the control voltage, read the photodiode on AI0 and fit the response."""
        lo, hi = min_max_volts or self.settings["laser"]["laserMinMaxControlVolts"]
        vals = np.arange(lo, hi + 1e-9, step)
        n_reads = (1 if self.simulated else 4) if n_reads is None else n_reads
        sensor = np.zeros_like(vals)
        try:
            for i, v in enumerate(vals):
                self.set_laser_control_volts(v)
                reads = []
                for _ in range(n_reads):
                    reads.append(self.daq.read_analog(0))
                    if not self.simulated:
                        time.sleep(0.025)
                sensor[i] = np.mean(reads)
        finally:
            self.set_laser_control_volts(0)
        fit = LaserFit.fit(vals, sensor)
        self.laser.fit = fit
        print(f"Laser fit: {fit.fit_type}, RMSE {fit.rmse:.4g}")
        return fit

    def save_laser_fit(self):
        if self.laser.fit is None:
            raise RuntimeError("No laser fit to save")
        self.laser.fit.save(settings_mod.laser_fit_path(self.settings_path))

    def load_laser_fit(self):
        p = settings_mod.laser_fit_path(self.settings_path)
        if p.exists():
            self.laser.fit = LaserFit.load(p)
            print(f"Loaded laser fit from {p}")
            return
        unconverted = p.with_suffix(".mat")
        if unconverted.exists():
            raise RuntimeError(f"{unconverted} is a laser fit in the old format and is not read. Convert it with "
                               f"`uv run python scripts/migrate_rig.py`, or delete it and refit the laser")
        print(f"No laser fit at {p}: using a linear map between laser.laserMinMaxControlVolts and laserMinMax_mW")

    # ================================================================== check / demo patterns
    def _play_pattern(self, waves: np.ndarray, rate: float, task_name: str):
        self._require_rig_settings()
        with self.lock:
            self._refuse_during_trial("start a check pattern")
            self.daq.clear_do()
            self.move_beam_volts(waves[0, :2])
            self.daq.prepare_ao(waves, rate=rate, finite=False, hardware_triggered=False, task_name=task_name)
            self.daq.start()
            self._pattern_running = True

    def stop_pattern(self):
        """Stop a check pattern. Never touches a trial armed or playing from the API or server."""
        with self.lock:
            if self.trial_active:
                return
            self._pattern_running = False
            self.daq.clear_ao()
            self.set_laser_mw(0)
            self.state = "idle"

    def zap_all_coords(self):
        """Hop the beam between every stimulus point (laser at the calibration power)."""
        pts = np.vstack(self.calibrated_points_volts())
        control = self.laser.mw_to_control(self.settings["calibrateScanners"]["calibration_power_mW"])
        self._play_pattern(wf.point_tour(pts, control), 5000, "zap_all")

    def zap_site(self, condition: int):
        pts = self.calibrated_points_volts()[condition - 1]
        control = self.laser.mw_to_control(self.settings["calibrateScanners"]["calibration_power_mW"])
        self._play_pattern(wf.point_tour(pts, control), 5000, "zap_site")

    def check_scanner_calibration(self):
        """Visit the detected calibration points through the fitted transform, slowly."""
        if not self.calibration_data:
            raise NotReadyError("No scanner calibration data")
        pts = self.mapping.mm_to_volts(np.array([m.actual_mm for m in self.calibration_data]))
        control = self.laser.mw_to_control(self.settings["calibrateScanners"]["calibration_power_mW"])
        waves = np.zeros((len(pts), 4))
        waves[:, :2] = pts
        waves[:, 2] = control
        self._play_pattern(waves, 10, "check_calib")

    def draw_outlines(self, outlines_stereotaxic: list[np.ndarray]):
        """Run the beam around stereotaxic outlines (brain border or areas) on the sample."""
        volts = [self.mapping.mm_to_volts(self.stereotaxic_to_sample(o)) for o in outlines_stereotaxic]
        control = self.laser.mw_to_control(self.settings["calibrateScanners"]["calibration_power_mW"])
        waves = wf.outline_tour(volts, control)
        rate = min(10 ** round(np.log10(len(waves) * 50), 1), 125_000)
        self._play_pattern(waves, rate, "draw_outline")

    def draw_brain_outline(self):
        self.draw_outlines([self.atlas.whole_brain])

    def draw_areas(self, area_indices: list[int]):
        outlines = [o for i in area_indices for o in self.atlas.areas[i].outlines]
        self.draw_outlines(outlines)
