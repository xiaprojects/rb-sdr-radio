"""Dual watch: one tuner, two channels, taking turns.

Silent until either channel carries a signal, then locked on that channel. One tuner
hears one place at a time, so this is watching in turns, not receiving both. The
reasoning and the measurements are in `docs/dual-watch-feasibility.md`, section 3.

The rules, which are the point of the module:

* The primary always wins. It is never left while locked.
* From the secondary it looks back at the primary every `lookback_s` once the squelch
  has shut, during the hold. A transmission is not interrupted: each look is a hop
  away and back, heard as a pop (Stefano's call, 2026-11-10, after the first flight
  tests). Only one that lasts `uninterrupted_s` is looked past every `lookback_s`, so
  a secondary that never stops, an ATIS, still cannot shut the primary out, which it
  did 94 % of the time in the study's model.
* A hold after a transmission, because the reply usually follows on the same channel.
* A confirming look before any lock. Ignition noise is impulsive and a carrier is not.
* After any lock, the primary is looked at first.

Pure and clock-injected like the controller: it decides, the controller moves the
tuner. Every method returns the next `Step`, so the whole table runs without a radio.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from rbsdrradio.freqrules import Channel

__all__ = ["DualWatch", "Mode", "Step"]


class Mode(StrEnum):
    SCAN = "SCAN"
    LOCKED = "LOCKED"
    LOOKBACK = "LOOKBACK"


@dataclass(frozen=True)
class Step:
    """Where the tuner goes next: one `dwell`, or continuous audio."""

    channel: Channel
    dwell: bool
    #: Back on the channel it was locked on: the lock itself has not changed.
    resume: bool = False


class DualWatch:
    def __init__(self, cfg_dual: dict, primary: Channel, secondary: Channel,
                 threshold_db: float, clock: Callable[[], float]):
        self.cfg = cfg_dual
        self.primary = primary
        self.secondary = secondary
        self.threshold_db = threshold_db
        self.clock = clock
        self.mode = Mode.SCAN
        self.locked: Channel | None = None
        self.looking: Channel | None = None
        self._confirming = False
        self._shut_since: float | None = None
        self._open_since: float | None = None
        self._next_look = 0.0

    @property
    def streaming(self) -> bool:
        return self.mode is Mode.LOCKED

    def start(self) -> Step:
        return self._scan()

    def on_dwell(self, snr_db: float) -> Step:
        """The S/N of the look that was asked for."""
        channel = self.looking
        hit = snr_db >= self.threshold_db
        if hit and self._confirming:
            return self._lock(channel)
        self._confirming = hit
        if hit:
            return Step(channel, dwell=True)
        if self.mode is Mode.LOOKBACK:
            return self._return_to_lock()
        return self._look_at(self.secondary if channel == self.primary else self.primary)

    def on_block(self, gate_open: bool) -> Step | None:
        """One block of continuous audio, with the squelch as it now stands."""
        now = self.clock()
        if gate_open:
            self._shut_since = None
            if self._open_since is None:
                self._open_since = now
        else:
            self._open_since = None
            if self._shut_since is None:
                self._shut_since = now
        if self._shut_since is not None and now - self._shut_since >= self.cfg["hold_s"]:
            return self._scan()
        if self.locked == self.secondary and now >= self._next_look and (
                not gate_open or now - self._open_since >= self.cfg["uninterrupted_s"]):
            self.mode = Mode.LOOKBACK
            return self._look_at(self.primary)
        return None

    def _scan(self) -> Step:
        self.mode = Mode.SCAN
        self.locked = None
        return self._look_at(self.primary)

    def _look_at(self, channel: Channel) -> Step:
        self.looking, self._confirming = channel, False
        return Step(channel, dwell=True)

    def _lock(self, channel: Channel) -> Step:
        self.mode, self.locked, self.looking = Mode.LOCKED, channel, None
        self._confirming, self._shut_since, self._open_since = False, None, None
        self._next_look = self.clock() + self.cfg["lookback_s"]
        return Step(channel, dwell=False)

    def _return_to_lock(self) -> Step:
        self.mode, self.looking = Mode.LOCKED, None
        self._next_look = self.clock() + self.cfg["lookback_s"]
        return Step(self.locked, dwell=False, resume=True)
