"""Command line entry point: ``zapit gui``, ``zapit server``, ``zapit pattern``, ``zapit check-config``."""
from __future__ import annotations

import argparse
import sys
import time


def _add_common(p: argparse.ArgumentParser):
    p.add_argument("--simulated", action="store_true", help="no hardware: simulated camera and DAQ")
    p.add_argument("--settings", default=None, help="explicit settings YAML (never rewritten)")
    p.add_argument("--no-server", action="store_true", help="do not start the ZeroMQ server")
    p.add_argument("--bind", default=None, help="server bind address, e.g. tcp://*:5555")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="zapit", description="Zapit scanning opto-stimulation")
    sub = ap.add_subparsers(dest="command", required=True)

    g = sub.add_parser("gui", help="start the GUI (default)")
    _add_common(g)

    s = sub.add_parser("server", help="headless: hardware plus the ZeroMQ server, no GUI")
    _add_common(s)
    s.add_argument("--config", default=None, help="stimulus config to load at start")

    pt = sub.add_parser("pattern", help="write a stimulus config with a grid/cross/line of points")
    pt.add_argument("output", help="YAML file to write (appended to if it exists)")
    pt.add_argument("ml", type=float)
    pt.add_argument("ap", type=float)
    pt.add_argument("--pattern", default="grid", choices=["grid", "cross", "mlline", "apline"])
    pt.add_argument("--size", type=float, nargs="+", default=[2.0], help="pattern size in mm (one or two values)")
    pt.add_argument("--n", type=int, nargs="+", default=[3], help="points per axis (one or two values)")
    pt.add_argument("--power", type=float, default=None, help="laser power mW (default from settings)")
    pt.add_argument("--bilateral", action="store_true")
    pt.add_argument("--overwrite", action="store_true")
    pt.add_argument("--settings", default=None)

    ck = sub.add_parser("check-config", help="validate a stimulus config and print its conditions")
    ck.add_argument("path")
    ck.add_argument("--settings", default=None)

    args = ap.parse_args(argv)
    return {"gui": _gui, "server": _server, "pattern": _pattern, "check-config": _check}[args.command](args)


def _gui(args):
    from zapit.gui import run_gui
    return run_gui(simulated=args.simulated, settings_path=args.settings,
                   server=not args.no_server, bind=args.bind)


def _server(args):
    from zapit.pointer import Zapit
    from zapit.server import ZapitServer
    model = Zapit(simulated=args.simulated, settings_path=args.settings)
    if args.config:
        model.load_stim_config(args.config)
    server = ZapitServer(model, args.bind)
    server.start()
    print("Running headless. Ctrl-C to quit.")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        model.close()
    return 0


def _pattern(args):
    from pathlib import Path
    from zapit import settings as settings_mod
    from zapit.stim_config import StimConfig, pattern_conditions
    st = settings_mod.load_settings(args.settings) if args.settings else settings_mod.load_settings()
    power = st["experiment"]["defaultLaserPowerMW"] if args.power is None else args.power
    conds = pattern_conditions([args.ml, args.ap], args.pattern, args.size if len(args.size) > 1 else args.size[0],
                               args.n if len(args.n) > 1 else args.n[0], power,
                               st["experiment"]["offRampDownDuration_ms"], args.bilateral)
    out = Path(args.output).with_suffix(".yml")
    if out.exists() and not args.overwrite:
        cfg = StimConfig.load(out)
        cfg.conditions.extend(conds)
    else:
        cfg = StimConfig(conds, st["experiment"]["defaultLaserModulationFrequencyHz"])
    cfg.save(out)
    print(f"Wrote {out} with {cfg.n_conditions} conditions")
    return 0


def _check(args):
    from zapit import settings as settings_mod
    from zapit import waveforms as wf
    from zapit.stim_config import StimConfig
    st = settings_mod.load_settings(args.settings) if args.settings else settings_mod.load_settings()
    cfg = StimConfig.load(args.path)
    e = st["experiment"]
    blank = wf.BlankingParams(e["blankingTime_ms"], e["blankOnsetShift_ms"], e["blankOffsetShift_ms"])
    bad = wf.unpresentable_conditions(cfg.points_per_condition(), st["NI"]["samplesPerSecond"], cfg.modulation_hz, blank)
    over = cfg.conditions_exceeding_power(st["laser"]["laserMinMax_mW"][1], e["blankingTime_ms"])
    print(f"{cfg.name}: {cfg.n_conditions} conditions at {cfg.modulation_hz} Hz")
    for i, c in enumerate(cfg.conditions):
        names = ", ".join(n or "outside brain" for n in c.area_names)
        flags = (" UNPRESENTABLE" if i in bad else "") + (" POWER-CAPPED" if i in over else "")
        print(f"  {i + 1:2d} {c.type:18s} {c.laser_power_mw:5.2f} mW  {c.n_points} pt  {names}{flags}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
