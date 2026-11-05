"""RB-SDR-Radio: airband AM receiver for RB-Avionics.

    rb-sdr-radio                     run the daemon, driven by the RB-01 bridge
    rb-sdr-radio devices             list the dongles, for sdr.device_serial
    rb-sdr-radio self-test           the whole chain on synthetic signals
    rb-sdr-radio test-fm 101.5       antenna test on a broadcast FM station
    rb-sdr-radio test-am 118.7       antenna test on an airband channel
"""

from __future__ import annotations

# isort: off
# MUST be first: caps BLAS thread pools before numpy loads (see _threadcaps.py).
from rbsdrradio import _threadcaps  # noqa: F401

import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path
# isort: on

from rbsdrradio import __version__
from rbsdrradio.config import Config, ConfigError, load

log = logging.getLogger("rbsdrradio")


def _test_parser(sub, name: str, helptext: str, common) -> argparse.ArgumentParser:
    p = sub.add_parser(name, help=helptext, parents=[common])
    p.add_argument("mhz", type=float, help="frequency in MHz")
    p.add_argument("--seconds", type=float, default=30.0, help="length of the test (default 30)")
    p.add_argument("--volume", type=int, default=50, help="0-100, 0 is silent (default 50)")
    p.add_argument("--no-audio", action="store_true", help="measure only, leave ALSA alone")
    p.add_argument("--wav", type=Path, help="also save what is heard to this WAV file")
    p.add_argument("--gain", help="tuner gain in dB, or 'auto' (default: the config)")
    p.add_argument("--ppm", type=int, help="frequency correction (default: the config)")
    p.add_argument("--device", help="dongle index or serial (default: the config)")
    p.add_argument("--replay", type=Path,
                   help="analyse a cu8 capture instead of the SDR "
                        "(rtl_sdr -s 1024000, tuned 256 kHz below the channel)")
    return p


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS,
                        help="configuration file")
    common.add_argument("--log-level", default=argparse.SUPPRESS)

    ap = argparse.ArgumentParser(prog="rb-sdr-radio", description=__doc__, parents=[common],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"rb-sdr-radio {__version__}")
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("devices", help="list the RTL-SDR dongles", parents=[common])
    sub.add_parser("self-test", help="test the software, no hardware needed", parents=[common])
    _test_parser(sub, "test-fm", "antenna test on a broadcast FM station", common)
    t = _test_parser(sub, "test-am", "antenna test on an airband channel", common)
    t.add_argument("--squelch", type=int, default=0,
                   help="squelch in dB above the noise, 0 holds it open (default 0)")
    return ap


def _bench_config(cfg: Config, args) -> Config:
    sdr: dict = {}
    typed: set[str] = set()
    if args.gain is not None:
        if args.gain.lower() == "auto":
            sdr["gain_mode"] = "agc"
        else:
            sdr |= {"gain_mode": "manual", "gain_db": float(args.gain)}
            typed.add("gain_db")
    if args.ppm is not None:
        sdr["ppm"] = args.ppm
        typed.add("ppm")
    # What was typed beats the tuner profile, and only that: the rest of the profile,
    # swap_iq above all, is still true of this dongle.
    sdr["tuner_profiles"] = {tuner: {k: v for k, v in profile.items() if k not in typed}
                             for tuner, profile in cfg.sdr.get("tuner_profiles", {}).items()}
    if args.device is not None:
        if args.device.isdigit():
            sdr |= {"device_index": int(args.device), "device_serial": ""}
        else:
            sdr["device_serial"] = args.device
    return cfg.with_overrides(sdr=sdr)


def _devices() -> int:
    from rbsdrradio.sdr import SdrError, list_devices, probe_tuner
    try:
        found = list_devices()
    except SdrError as e:
        print(e, file=sys.stderr)
        return 3
    if not found:
        print("no RTL-SDR device found")
        return 1
    print("index  serial            tuner    device")
    for d in found:
        tuner = probe_tuner(d.index) or "in use"
        print(f"{d.index:5d}  {d.serial or '<none>':16s}  {tuner:7s}  "
              f"{d.name} ({d.manufacturer} {d.product})")
    print('\nPin the radio dongle with "device_serial" in the config. '
          "Index order can change between boots.")
    return 0


def _run_test(cfg: Config, args) -> int:
    from rbsdrradio.bench import BenchRequest, run_test
    mode = "am" if args.command == "test-am" else "fm"
    request = BenchRequest(mode, args.mhz, seconds=args.seconds, volume=args.volume,
                           squelch_db=getattr(args, "squelch", 0), audio=not args.no_audio,
                           wav=args.wav, replay=args.replay)
    return run_test(_bench_config(cfg, args), request)


def main(argv=None) -> int:
    args = _parser().parse_args(argv)

    if args.command == "self-test":
        from rbsdrradio.selftest import run_self_test
        return run_self_test()
    if args.command == "devices":
        return _devices()

    try:
        cfg = load(getattr(args, "config", None))
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2

    logging.basicConfig(level=getattr(args, "log_level", None) or cfg.log["level"],
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    if args.command in ("test-am", "test-fm"):
        return _run_test(cfg, args)

    from rbsdrradio import daemon
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(daemon.run(cfg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
