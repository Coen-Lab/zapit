"""Stimulus waveform generation for the four analog output lines.

Channel order on the DAQ (one column each): 0 galvo X, 1 galvo Y, 2 laser
control, 3 masking LED (5 V TTL). One modulation cycle is ``round(rate / freq)``
samples. Within a cycle the beam visits each point of the condition in turn; the
scanner command is boxcar-smoothed over the blanking window and the laser and
LED are gated off while the mirrors move.

This is a faithful port of the upstream MATLAB ``stimConfig.chanSamples`` so
waveforms are identical sample for sample (see tests/test_waveforms.py).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

LED_VOLTS = 5.0
MIN_ON_SAMPLES = 2   # laser-on samples a point must keep after blanking


@dataclass(frozen=True)
class BlankingParams:
    blanking_time_ms: float
    onset_shift_ms: float
    offset_shift_ms: float

    def samples(self, rate: float) -> tuple[int, int, int]:
        dt = 1.0 / rate
        return (
            int(round(self.blanking_time_ms * 1e-3 / dt)),
            int(round(self.onset_shift_ms * 1e-3 / dt)),
            int(round(self.offset_shift_ms * 1e-3 / dt)),
        )

    def total_samples(self, rate: float) -> int:
        return sum(self.samples(rate))


def samples_per_cycle(rate: float, modulation_hz: float) -> int:
    return int(round(rate / modulation_hz))


def actual_modulation_hz(rate: float, modulation_hz: float) -> float:
    return rate / samples_per_cycle(rate, modulation_hz)


def unpresentable_conditions(points_per_condition: list[int], rate: float, modulation_hz: float,
                             blanking: BlankingParams) -> list[int]:
    """Indices of conditions whose points cannot all get laser-on time in one cycle."""
    n = samples_per_cycle(rate, modulation_hz)
    total_blank = blanking.total_samples(rate)
    bad = []
    for i, p in enumerate(points_per_condition):
        effective = max(int(p), 2)   # single points are spoofed to two
        if effective * (total_blank + MIN_ON_SAMPLES) > n:
            bad.append(i)
    return bad


def _smooth_scanner(seq: np.ndarray, n: int, blank: int) -> np.ndarray:
    """Boxcar-smooth a stepped scanner sequence the way the upstream code does."""
    if blank <= 0:
        return seq[:n]
    kernel = np.ones(blank) / blank
    sm = np.convolve(np.roll(seq, 2 * blank), kernel, mode="valid")
    sm = np.concatenate([sm, np.full(blank - 1, sm[-1])])
    sm = np.roll(sm, -blank)
    return sm[:n]


def one_cycle(points_volts: np.ndarray, rate: float, modulation_hz: float,
              blanking: BlankingParams, ephys: bool = False) -> np.ndarray:
    """Waveforms for one modulation cycle of one condition.

    points_volts: (P, 2) scanner volts for the P points of this condition.
    Returns (n_samples, 4). Column 2 (laser) is a 0/1 gate to be multiplied by
    the control voltage; column 3 is the 5 V LED signal.
    """
    pts = np.atleast_2d(np.asarray(points_volts, dtype=float))
    single_point = pts.shape[0] == 1
    if single_point:
        pts = np.repeat(pts, 2, axis=0)
    p = pts.shape[0]
    n = samples_per_cycle(rate, modulation_hz)
    blank, onset, offset = blanking.samples(rate)

    reps = int(np.ceil(n / p))
    x = np.repeat(pts[:, 0], reps)   # hold each point for `reps` samples, point by point
    y = np.repeat(pts[:, 1], reps)

    w = np.zeros((n, 4))
    w[:, 0] = _smooth_scanner(x, n, blank)
    w[:, 1] = _smooth_scanner(y, n, blank)
    w[:, 2] = 1.0
    w[:, 3] = LED_VOLTS

    edges = np.ceil(np.linspace(1, n, p + 1)).astype(int) - 1   # 0-based slot starts
    mask = np.ones(n)
    total = blank + onset + offset
    for k in range(total):
        idx = edges[:-1] + k
        mask[idx[idx < n]] = 0
    w[:, 2:4] *= mask[:, None]

    if single_point:
        # The second half of the cycle revisits the same point: laser off there.
        to_zero = edges[1::2] + 1
        gap = float(np.median(np.diff(edges)))
        for s in to_zero:
            e = min(int(np.floor(s + gap)), n - 1)
            w[s:e + 1, 2] = 0

    if ephys:
        w[:, 2] = ephys_waveform(w[:, 2])

    w[:, 2:4] = np.roll(w[:, 2:4], -onset, axis=0)
    return w


def ephys_waveform(gate: np.ndarray) -> np.ndarray:
    """Replace square laser-on epochs with half-sinusoids of equal area (reduces artefacts).

    The returned waveform peaks above 1, so the caller must scale laser power by
    its peak. Port of upstream ``filterForEphys`` ('sine' mode).
    """
    gate = np.asarray(gate, dtype=float)
    n_cycles = int(np.sum(np.diff(gate) == 1))
    one_cycle_only = n_cycles == 1
    if one_cycle_only:
        n_cycles = 2
    w = np.sin(np.linspace(np.pi * 0.5, (2 * n_cycles + 0.5) * np.pi, len(gate)))
    w = w + w.min()
    if one_cycle_only:
        w[int(round(len(w) / 2)) - 1:] = 0
    area_in, area_out = gate.sum(), w.sum()
    return w * (area_in / area_out) if area_out else w


def finite_trial(cycle: np.ndarray, rate: float, duration_s: float, rampdown_ms: float,
                 start_delay_s: float = 0.0) -> np.ndarray:
    """Expand one cycle into a finite trial: repeats, laser ramp-down, optional leading delay."""
    cycle = np.asarray(cycle, dtype=float)
    period = cycle.shape[0] / rate
    n_cycles = max(int(round(duration_s / period)), 1)
    out = np.tile(cycle, (n_cycles, 1))
    if rampdown_ms < 1:
        out[-1, 2:4] = 0
    else:
        n_ramp = int(np.ceil(rampdown_ms * 1e-3 / period))
        ramp = np.tile(cycle, (n_ramp, 1))
        ramp[:, 2] *= np.linspace(1, 0, ramp.shape[0])
        ramp[-1, 3] = 0
        out = np.vstack([out, ramp])
    if start_delay_s > 0:
        n_delay = int(round(rate * start_delay_s))
        lead = np.tile(out[:1], (n_delay, 1))
        lead[:, 2:4] = 0
        out = np.vstack([lead, out])
    return out


def rampdown_buffers(last: np.ndarray, rate: float, rampdown_ms: float) -> list[np.ndarray]:
    """Successive buffers that fade the laser out of a continuous waveform, ending all-zero."""
    last = np.asarray(last, dtype=float)
    ms_per_buffer = last.shape[0] / rate * 1e3
    rampdown_ms = max(rampdown_ms, ms_per_buffer)
    n = int(np.ceil(rampdown_ms / ms_per_buffer))
    out = []
    if rampdown_ms > 0 and n > 1:
        amps = np.linspace(1, 0, n + 1)[1:-1]
        for i, a in enumerate(amps):
            buf = last.copy()
            buf[:, 2] *= a
            if i == len(amps) - 1:
                buf[:, 3] = 0
            out.append(buf)
    out.append(np.zeros_like(last))
    return out


def point_tour(points_volts: np.ndarray, laser_control: float, steps_between: int = 10) -> np.ndarray:
    """Beam hops between points with the laser on at each point and off during the ramps.

    Used by the GUI's "zap all coords" check. (N, 4) output.
    """
    pts = np.atleast_2d(np.asarray(points_volts, dtype=float))
    if len(pts) == 1:
        w = np.zeros((steps_between, 4))
        w[:, :2] = pts[0]
        w[0, 2] = laser_control
        return w
    rows = []
    for i in range(len(pts)):
        nxt = pts[(i + 1) % len(pts)]
        rows.append([pts[i, 0], pts[i, 1], laser_control, 0.0])
        ramp = np.zeros((steps_between, 4))
        ramp[:, 0] = np.linspace(pts[i, 0], nxt[0], steps_between)
        ramp[:, 1] = np.linspace(pts[i, 1], nxt[1], steps_between)
        rows.extend(ramp.tolist())
    return np.asarray(rows)


def outline_tour(outlines_volts: list[np.ndarray], laser_control: float, steps_between: int = 200,
                 decimate: int = 2) -> np.ndarray:
    """Run the beam around one or more closed outlines, laser off while jumping between them."""
    segs = []
    for i, o in enumerate(outlines_volts):
        o = np.atleast_2d(np.asarray(o, dtype=float))[::decimate]
        seg = np.zeros((len(o), 4))
        seg[:, :2] = o
        seg[:, 2] = laser_control
        nxt = np.atleast_2d(outlines_volts[(i + 1) % len(outlines_volts)])[0]
        ramp = np.zeros((steps_between, 4))
        ramp[:, 0] = np.linspace(o[-1, 0], nxt[0], steps_between)
        ramp[:, 1] = np.linspace(o[-1, 1], nxt[1], steps_between)
        segs.extend([seg, ramp])
    return np.vstack(segs)
