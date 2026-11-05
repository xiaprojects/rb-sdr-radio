"""SDR access: which dongle, how it is opened, and the reader thread.

Adapted from rb-vor `sdr.py`. The selection policy and the reader are the same; the
backend is not. rb-vor reaches librtlsdr through pyrtlsdr, which binds optional
symbols at import, so against a stock librtlsdr it dies with `undefined symbol:
rtlsdr_set_dithering` (rb-vor A27). Here the dozen calls this needs are bound
directly with ctypes: no wheel to install into a managed Python, and the same code
drives the stock library and the RTL-SDR Blog fork.

Bounded, never queued: when the consumer falls behind, the oldest block goes and
the gap is flagged. Late air traffic control audio is worse than a dropped syllable.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from rbsdrradio.dsp import FS_HZ, TUNER_OFFSET_HZ, to_complex64

__all__ = ["SdrError", "DeviceChoice", "DeviceInfo", "RtlDevice", "FakeDevice", "SdrReader",
           "resolve_device", "list_devices", "probe_tuner", "snap_gain", "apply_tuner_profile",
           "TUNER_NAMES", "NO_AGC_TUNERS", "TUNER_PROFILE_KEYS"]

log = logging.getLogger("rbsdrradio.sdr")


class SdrError(RuntimeError):
    """Device could not be opened, configured or read."""


@dataclass(frozen=True)
class DeviceChoice:
    """Which dongle to open, and how it was decided."""

    index: int
    serial: str = ""
    detail: str = ""
    warning: str = ""


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    manufacturer: str
    product: str
    serial: str


def resolve_device(cfg_sdr: dict, serials: Sequence[str]) -> DeviceChoice:
    """Pick the dongle from `sdr.device_serial` / `sdr.device_index`.

    `serials` is the enumeration, position == device index. Pure, so the whole
    policy is testable without a radio.

    1. `device_serial` set -> that dongle, or an error naming what IS present. A
       wrong serial never falls back to an index: this aircraft's other dongles are
       on other antennas, and tuning the wrong one fails silently.
    2. otherwise `device_index`, bounds-checked.
    3. more than one dongle and no serial -> a warning, because index order follows
       USB enumeration and can change between boots.
    """
    want = str(cfg_sdr.get("device_serial", "") or "").strip()
    n = len(serials)
    if want:
        matches = [i for i, s in enumerate(serials) if s == want]
        if not matches:
            have = ", ".join(f"{i}:{s or '<no serial>'}" for i, s in enumerate(serials))
            raise SdrError(f"no RTL-SDR with serial {want!r} (sdr.device_serial); "
                           f"{n} device(s) present: {have or 'none'}")
        if len(matches) > 1:
            raise SdrError(f"{len(matches)} RTL-SDRs share serial {want!r}: "
                           "give them distinct serials with `rtl_eeprom -d <n> -s <name>`")
        return DeviceChoice(matches[0], want, f"serial {want!r} (index {matches[0]})")

    idx = int(cfg_sdr.get("device_index", 0) or 0)
    if n == 0:
        raise SdrError("no RTL-SDR device found")
    if not 0 <= idx < n:
        raise SdrError(f"sdr.device_index {idx} but {n} RTL-SDR device(s) present "
                       f"(valid 0..{n - 1})")
    serial = serials[idx]
    warning = ""
    if n > 1:
        warning = (f"{n} RTL-SDR devices present and sdr.device_serial is unset: using "
                   f"index {idx} (serial {serial or '<no serial>'}). USB enumeration "
                   "order is not stable, so pin this dongle by serial.")
    return DeviceChoice(idx, serial, f"index {idx}" + (f" (serial {serial})" if serial else ""),
                        warning)


#: librtlsdr's `enum rtlsdr_tuner`. It identifies the chip over I2C inside `rtlsdr_open`
#: and offers no way to choose one, so no setting names the fitted tuner. An R820T2 and
#: an R860 report as R820T.
TUNER_NAMES = {0: "unknown", 1: "E4000", 2: "FC0012", 3: "FC0013",
               4: "FC2580", 5: "R820T", 6: "R828D"}

#: librtlsdr's gain-mode call is an empty stub for these, so asking for AGC changes
#: nothing: an FC0012 stays on the -4 dB step its init sequence leaves it at.
NO_AGC_TUNERS = frozenset({"FC0012"})

#: Properties of the tuner rather than of the installation (rb-vor A28).
TUNER_PROFILE_KEYS = ("swap_iq", "gain_db", "ppm")


def apply_tuner_profile(cfg_sdr: dict, tuner: str) -> tuple[dict, list[str]]:
    """Overlay `sdr.tuner_profiles[<tuner>]` on the sdr config.

    The tuner is only known once the device is open, so this runs late, with the
    base values as the fallback.
    """
    prof = (cfg_sdr.get("tuner_profiles") or {}).get(tuner or "", {}) or {}
    effective = dict(cfg_sdr)
    notes: list[str] = []
    for k in TUNER_PROFILE_KEYS:
        if k in prof and prof[k] != cfg_sdr.get(k):
            notes.append(f"{k}: {cfg_sdr.get(k)!r} -> {prof[k]!r}")
            effective[k] = prof[k]
    return effective, notes


def snap_gain(requested_db: float, supported_db: Sequence[float]) -> tuple[float, str]:
    """Snap a gain to the tuner's own ladder, and say so.

    librtlsdr rounds silently: an FC0012 tops out at 19.2 dB where an R820T reaches
    49.6, so a config asking for 32.8 would describe neither request nor hardware.
    """
    if not supported_db:
        return float(requested_db), ""
    chosen = min(supported_db, key=lambda g: (abs(g - requested_db), g))
    if abs(chosen - requested_db) < 0.05:
        return float(chosen), ""
    lo, hi = min(supported_db), max(supported_db)
    return float(chosen), (f"gain {requested_db:g} dB is not available on this tuner; "
                           f"using {chosen:g} dB (ladder {lo:g}..{hi:g} dB)")


# -- librtlsdr ------------------------------------------------------------- #

_handle = ctypes.c_void_p
_lib: ctypes.CDLL | None = None


def _load() -> ctypes.CDLL:
    """Load librtlsdr once and declare every call used. Raises `SdrError`."""
    global _lib
    if _lib is not None:
        return _lib
    path = ctypes.util.find_library("rtlsdr") or "librtlsdr.so.0"
    try:
        lib = ctypes.CDLL(path)
    except OSError as e:
        raise SdrError(f"cannot load librtlsdr ({e}): sudo apt install librtlsdr0") from e
    i, u, p, buf = ctypes.c_int, ctypes.c_uint32, _handle, ctypes.c_void_p
    sigs = {
        "rtlsdr_get_device_count": (u, ()),
        "rtlsdr_get_device_name": (ctypes.c_char_p, (u,)),
        "rtlsdr_get_device_usb_strings": (i, (u, ctypes.c_char_p, ctypes.c_char_p,
                                              ctypes.c_char_p)),
        "rtlsdr_open": (i, (ctypes.POINTER(p), u)),
        "rtlsdr_close": (i, (p,)),
        "rtlsdr_set_sample_rate": (i, (p, u)),
        "rtlsdr_set_center_freq": (i, (p, u)),
        "rtlsdr_set_freq_correction": (i, (p, i)),
        "rtlsdr_get_tuner_type": (i, (p,)),
        "rtlsdr_get_tuner_gains": (i, (p, ctypes.POINTER(i))),
        "rtlsdr_set_tuner_gain_mode": (i, (p, i)),
        "rtlsdr_set_tuner_gain": (i, (p, i)),
        "rtlsdr_reset_buffer": (i, (p,)),
        "rtlsdr_read_sync": (i, (p, buf, i, ctypes.POINTER(i))),
    }
    for name, (restype, argtypes) in sigs.items():
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = restype, argtypes
    _lib = lib
    return lib


def list_devices() -> list[DeviceInfo]:
    """Every RTL-SDR present, position == device index."""
    lib = _load()
    found = []
    for i in range(lib.rtlsdr_get_device_count()):
        fields = [ctypes.create_string_buffer(256) for _ in range(3)]
        rc = lib.rtlsdr_get_device_usb_strings(i, *fields)
        vendor, product, serial = ("", "", "") if rc else (
            f.value.decode("ascii", "replace").strip() for f in fields)
        name = (lib.rtlsdr_get_device_name(i) or b"").decode("ascii", "replace")
        found.append(DeviceInfo(i, name, vendor, product, serial))
    return found


def probe_tuner(index: int) -> str:
    """The tuner on one dongle, or "" when another program holds it.

    The chip is only identified by opening the dongle, so this claims it for a moment.
    A held dongle refuses the claim and is not disturbed.
    """
    lib = _load()
    dev = _handle()
    if lib.rtlsdr_open(ctypes.byref(dev), index) != 0:
        return ""
    try:
        return TUNER_NAMES.get(lib.rtlsdr_get_tuner_type(dev), "unknown")
    finally:
        lib.rtlsdr_close(dev)


class RtlDevice:
    """One open dongle. `close()` is what gives the USB device back."""

    def __init__(self, cfg: dict):
        self._lib = _load()
        choice = resolve_device(cfg, [d.serial for d in list_devices()])
        if choice.warning:
            log.warning("%s", choice.warning)
        self._dev = _handle()
        rc = self._lib.rtlsdr_open(ctypes.byref(self._dev), choice.index)
        if rc != 0:
            raise SdrError(f"cannot open RTL-SDR {choice.detail} (error {rc}): already in use "
                           "by another program (stratux, rb-vor, a test), or the kernel "
                           "DVB driver holds it")
        self.closed = False
        try:
            self.tuner = TUNER_NAMES.get(self._lib.rtlsdr_get_tuner_type(self._dev), "unknown")
            if self.tuner == "unknown":
                # librtlsdr falls back to direct sampling: blocks arrive, nothing is heard.
                raise SdrError(f"no supported tuner on RTL-SDR {choice.detail}: "
                               "librtlsdr did not recognise the chip")
            self._configure(cfg)
        except Exception:
            self.close()
            raise
        self.name = f"{choice.detail}, {self.tuner} tuner, gain {self.gain_label}"

    def _check(self, what: str, rc: int) -> None:
        if rc < 0:
            raise SdrError(f"{what} failed (error {rc})")

    def _gains_db(self) -> list[float]:
        count = self._lib.rtlsdr_get_tuner_gains(self._dev, None)
        if count <= 0:
            return []
        tenths = (ctypes.c_int * count)()
        self._lib.rtlsdr_get_tuner_gains(self._dev, tenths)
        return [g / 10.0 for g in tenths]

    def _configure(self, cfg: dict) -> None:
        eff, notes = apply_tuner_profile(cfg, self.tuner)
        for note in notes:
            log.info("tuner profile %s: %s", self.tuner, note)
        self.swap_iq = bool(eff.get("swap_iq", False))
        self._check("set sample rate", self._lib.rtlsdr_set_sample_rate(self._dev, FS_HZ))
        agc = eff["gain_mode"] == "agc"
        if agc and self.tuner in NO_AGC_TUNERS:
            log.warning("%s has no AGC (librtlsdr would leave it at -4 dB): using manual gain",
                        self.tuner)
            agc = False
        if agc:
            self.gain_label = "AGC"
            self._check("set AGC", self._lib.rtlsdr_set_tuner_gain_mode(self._dev, 0))
        else:
            gain, note = snap_gain(eff["gain_db"], self._gains_db())
            if note:
                log.warning("%s: %s", self.tuner, note)
            self.gain_label = f"{gain:g} dB"
            self._check("set manual gain", self._lib.rtlsdr_set_tuner_gain_mode(self._dev, 1))
            self._check("set gain",
                        self._lib.rtlsdr_set_tuner_gain(self._dev, int(round(gain * 10))))
        if eff["ppm"]:
            # -2 is librtlsdr saying the value is already set.
            rc = self._lib.rtlsdr_set_freq_correction(self._dev, int(eff["ppm"]))
            if rc != -2:
                self._check("set ppm", rc)
        self._check("reset buffer", self._lib.rtlsdr_reset_buffer(self._dev))

    def set_frequency(self, hz: int) -> None:
        self._check("tune", self._lib.rtlsdr_set_center_freq(self._dev, int(hz)))

    def read(self, n: int) -> np.ndarray:
        raw = np.empty(n * 2, dtype=np.uint8)
        got = ctypes.c_int(0)
        buf = raw.ctypes.data_as(ctypes.c_void_p)
        rc = self._lib.rtlsdr_read_sync(self._dev, buf, raw.size, ctypes.byref(got))
        if rc != 0:
            raise SdrError(f"read failed (error {rc})")
        return to_complex64(raw[:got.value & ~1])

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._lib.rtlsdr_close(self._dev)


class FakeDevice:
    """Bench and test stand-in: blocks from a callable, no hardware."""

    tuner = ""

    def __init__(self, source: Callable[[int], np.ndarray], name: str = "fake"):
        self.source = source
        self.name = name
        self.freq_hz = 0
        self.closed = False

    def set_frequency(self, hz: int) -> None:
        self.freq_hz = int(hz)

    def read(self, n: int) -> np.ndarray:
        return self.source(n)

    def close(self) -> None:
        self.closed = True


class SdrReader:
    """Threaded reader: the controller's SDR port.

    `set_frequency()` takes the wanted frequency; the tuner offset is applied here,
    so nothing else has to remember it. `opener` is injectable for tests.
    """

    def __init__(self, cfg_sdr: dict, opener: Callable[[dict], object] = RtlDevice,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg_sdr
        self.opener = opener
        self.clock = clock
        self.device = None
        self.block_samples = cfg_sdr["block_samples"]
        self._q: queue.Queue = queue.Queue(maxsize=cfg_sdr["buffer_blocks"])
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._gap = False
        self.swap_iq = bool(cfg_sdr.get("swap_iq", False))
        self.freq_hz: int | None = None
        self.last_block_at: float | None = None
        self.overruns = 0
        self.blocks_read = 0
        self.read_errors = 0

    @property
    def is_open(self) -> bool:
        return self.device is not None

    def open(self) -> None:
        if self.device is None:
            self.device = self.opener(self.cfg)
            self.swap_iq = bool(getattr(self.device, "swap_iq", self.swap_iq))
            log.info("SDR open: %s", getattr(self.device, "name", "unknown"))

    def set_frequency(self, hz: int) -> None:
        """Retune, then discard what is in flight: it was sampled on the old channel."""
        self.freq_hz = int(hz)
        try:
            self.open()
            self.device.set_frequency(self.freq_hz + TUNER_OFFSET_HZ)
        except SdrError:
            raise
        except Exception as e:                       # noqa: BLE001
            raise SdrError(f"retune to {hz} Hz failed: {e}") from e
        self._drain()
        self._gap = True
        self.last_block_at = self.clock()
        self._start()

    def _start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="rbsdrradio-sdr", daemon=True)
            self._thread.start()

    def close(self) -> None:
        """Release the dongle. Safe to call when already released."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                # Closing under a thread inside rtlsdr_read_sync can crash the process.
                log.error("SDR reader did not stop; abandoning the device")
                self._thread, self.device = None, None
                return
            self._thread = None
        if self.device is not None:
            with contextlib.suppress(Exception):
                self.device.close()
            self.device = None
            log.info("SDR released")
        self._drain()

    def blocks(self):
        """Yield the `(samples, gap)` pairs queued at the moment of the call.

        Bounded on purpose (rb-vor F5): if the reader refills the queue as fast as
        it drains, an unbounded loop would never give the event loop back, and the
        ticker and the watchdog would stop exactly when the machine is overloaded.
        """
        for _ in range(max(1, self._q.qsize())):
            try:
                yield self._q.get_nowait()
            except queue.Empty:
                return

    def _drain(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    def _run(self) -> None:
        backoff = 0.0
        while not self._stop.is_set():
            try:
                block = self.device.read(self.block_samples)
                backoff = 0.0
            except Exception as e:                   # noqa: BLE001
                self.read_errors += 1
                self._gap = True
                backoff = min(max(backoff * 2, 0.1), 2.0)
                if self.read_errors in (1, 10, 100):
                    log.warning("SDR read failed (%s)", e)
                self._stop.wait(backoff)
                continue

            self.blocks_read += 1
            self.last_block_at = self.clock()
            if self.swap_iq:
                block = np.conj(block)
            gap, self._gap = self._gap, False
            try:
                self._q.put_nowait((block, gap))
            except queue.Full:
                self.overruns += 1
                self._gap = True
                with contextlib.suppress(queue.Empty, queue.Full):
                    self._q.get_nowait()
                    self._q.put_nowait((block, True))
                if self.overruns in (1, 10, 100) or self.overruns % 1000 == 0:
                    log.warning("SDR overrun #%d: the consumer is not keeping up", self.overruns)

    @property
    def stats(self) -> dict:
        return {"blocks_read": self.blocks_read, "overruns": self.overruns,
                "read_errors": self.read_errors, "queued": self._q.qsize()}
