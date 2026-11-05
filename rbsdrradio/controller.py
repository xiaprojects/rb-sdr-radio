"""State machine: commands in, SDR, sound device and recording managed.

Synchronous and clock-injected, like rb-vor's: every input is an explicit event and
every timer reads an injected clock, so the whole table is testable without asyncio,
a network or a radio. `daemon.py` only translates I/O into these calls.

Resource rules, which are the point of the module:

* The SDR is held only while a valid frequency is selected. Frequency 0, an invalid
  frequency or a lost bridge release it, and the next valid frequency reopens it.
* The sound device is held only while a channel is tuned **and** volume is above 0.
  Volume 0 releases it at once, without touching the SDR: recording and the channel
  status carry on.
* A failure of one output never stops the other. No sound device still records, a
  full card still plays, and neither is allowed to take the daemon down. Each shows
  as a bit of `sdrradiofault` and is retried.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import StrEnum

import numpy as np

from rbsdrradio.audio import AudioError, volume_gain
from rbsdrradio.dsp import FS_HZ, AmDemod, pcm16
from rbsdrradio.freqrules import Channel, parse_channel
from rbsdrradio.sdr import SdrError
from rbsdrradio.squelch import Squelch, SquelchParams

__all__ = ["State", "Controller", "FAULT_FREQUENCY", "FAULT_SDR", "FAULT_AUDIO", "FAULT_RECORD"]

log = logging.getLogger("rbsdrradio.controller")


class State(StrEnum):
    IDLE = "IDLE"
    INVALID_FREQ = "INVALID_FREQ"
    FAULT = "FAULT"
    TUNING = "TUNING"
    LISTENING = "LISTENING"
    RECEIVING = "RECEIVING"


#: Published `sdrradiostatus`.
STATUS_CODE = {State.IDLE: 0, State.INVALID_FREQ: 0, State.FAULT: 1, State.TUNING: 2,
               State.LISTENING: 3, State.RECEIVING: 4}

#: Published `sdrradiofault`, a bit mask: faults are independent of each other.
FAULT_FREQUENCY = 1
FAULT_SDR = 2
FAULT_AUDIO = 4
FAULT_RECORD = 8

_TUNED = frozenset({State.TUNING, State.LISTENING, State.RECEIVING})
_DECODING = frozenset({State.LISTENING, State.RECEIVING})

#: A freshly started player has to stay up this long before its fault clears: aplay
#: reports a busy device by exiting a moment after starting.
_AUDIO_PROVEN_S = 1.0


def _clamp(value: float, low: int, high: int) -> int:
    return max(low, min(high, int(round(value))))


class Controller:
    def __init__(self, cfg, sdr, audio, recorder, publisher,
                 clock: Callable[[], float] = time.monotonic,
                 notify: Callable[[], None] | None = None):
        self.cfg = cfg
        self.sdr = sdr
        self.audio = audio
        self.recorder = recorder
        self.publisher = publisher
        self.clock = clock
        self.notify = notify

        self.state = State.IDLE
        self.reason = "startup"
        self.channel: Channel | None = None
        self.volume: int = cfg.radio["volume"]
        self.squelch_db: int = cfg.radio["squelch_db"]
        self.record_enabled: bool = cfg.record["enabled"]

        self.demod = AmDemod()
        self.squelch = Squelch(SquelchParams(**cfg.squelch), self.squelch_db,
                               cfg.sdr["block_samples"] / FS_HZ)
        self.signal_dbfs = 0
        self.snr_db = 0

        self._pending: tuple[Channel, float] | None = None
        self._settle_at = 0.0
        self._sdr_retry_at = 0.0
        self._bridge_lost_at: float | None = None
        self._audio_fault = False
        self._audio_retry_at = 0.0
        self._audio_opened_at = 0.0

    # -- commands ----------------------------------------------------------- #

    def apply(self, values: dict) -> None:
        """One bridge frame. Absent keys mean no change; repeats are no-ops."""
        if "freq" in values:
            self.set_frequency(values["freq"])
        if values.get("volume") is not None:
            self.set_volume(values["volume"])
        if values.get("squelch") is not None:
            self.set_squelch(values["squelch"])
        if values.get("record") is not None:
            self.set_record(values["record"])

    def set_frequency(self, mhz: float | None) -> None:
        """Debounced, like rb-vor: a tuning knob must not retune once per detent."""
        if mhz is None or mhz == 0:
            self._pending = None
            self._release(State.IDLE, "no frequency selected")
            return
        channel, problem = parse_channel(mhz)
        if channel is None:
            self._pending = None
            self._release(State.INVALID_FREQ, problem)
            return
        if channel == self.channel:
            # A bridge frame carries every key, so this repeats constantly. Restarting
            # the retune here would reset the demodulator for ever (rb-vor A16).
            self._pending = None
            return
        if self._pending is None or self._pending[0] != channel:
            self._pending = (channel, self.clock())

    def set_volume(self, volume: float) -> None:
        volume = _clamp(volume, 0, 100)
        if volume != self.volume:
            self.volume = volume
            self._audio_retry_at = 0.0
            self._sync_audio()
            self._report()

    def set_squelch(self, squelch_db: float) -> None:
        squelch_db = _clamp(squelch_db, 0, 100)
        if squelch_db != self.squelch_db:
            self.squelch_db = squelch_db
            self.squelch.set_threshold(squelch_db)
            self._report()

    def set_record(self, enabled: float) -> None:
        enabled = bool(enabled)
        if enabled != self.record_enabled:
            self.record_enabled = enabled
            if not enabled:
                self.recorder.end_segment()
            self._report()

    def on_bridge_connected(self) -> None:
        self._bridge_lost_at = None

    def on_bridge_disconnected(self) -> None:
        if self._bridge_lost_at is None:
            self._bridge_lost_at = self.clock()

    def shutdown(self) -> None:
        self._pending = None
        self._release(State.IDLE, "shutting down")

    # -- samples ------------------------------------------------------------ #

    def push(self, iq: np.ndarray, gap: bool = False) -> None:
        """Feed one block. Anything arriving outside a decoding state is dropped,
        which is what keeps the old channel out of the new one after a retune."""
        if self.state not in _DECODING:
            return
        m = self.demod.process(iq)
        self.signal_dbfs = min(-1, int(round(m.signal_dbfs)))      # 0 is "nothing measured"
        self.snr_db = int(round(m.snr_db))
        gate_open = self.squelch.update(m.snr_db)
        self._enter(State.RECEIVING if gate_open else State.LISTENING, "signal"
                    if gate_open else "quiet")
        gated = self.squelch.apply(m.audio)
        self._play(gated)
        self._record(gated, gate_open)
        self._report()

    def _play(self, gated: np.ndarray) -> None:
        if not self.audio.is_open:
            return
        try:
            self.audio.write(pcm16(gated * volume_gain(self.volume)))
        except AudioError as e:
            self._audio_failed(e)

    def _record(self, gated: np.ndarray, gate_open: bool) -> None:
        # Never at the listener's volume: a recording must not depend on the knob.
        # Not with the squelch held open either: that is hiss, and it would push real
        # traffic out of the quota.
        if self.record_enabled and not self.squelch.forced:
            self.recorder.feed(pcm16(gated), gate_open, self.channel.name_khz)
        else:
            self.recorder.end_segment()

    # -- timers ------------------------------------------------------------- #

    def tick(self) -> None:
        """Periodic housekeeping: debounce, settle, watchdogs, retries, reporting."""
        now = self.clock()
        if self.notify is not None:
            self.notify()

        if (self._bridge_lost_at is not None
                and now - self._bridge_lost_at >= self.cfg.bridge["ws_grace_s"]):
            self._pending = None
            self._release(State.IDLE, "bridge unreachable")
        elif self._pending is not None:
            channel, since = self._pending
            if (now - since) * 1000.0 >= self.cfg.radio["debounce_ms"]:
                self._pending = None
                self._retune(channel)
        elif self.state is State.TUNING and now >= self._settle_at:
            self._enter(State.LISTENING, "settled")
        elif self.state is State.FAULT and now >= self._sdr_retry_at:
            self._enter(State.TUNING, "retrying SDR")
            self._tune()
        elif self.state in _DECODING and self._sdr_stalled(now):
            self._sdr_failed(f"no samples for {self.cfg.sdr['stall_s']:g} s")

        self._sync_audio()
        self._report()

    def _sdr_stalled(self, now: float) -> bool:
        last = self.sdr.last_block_at
        return last is not None and now - last >= self.cfg.sdr["stall_s"]

    # -- transitions -------------------------------------------------------- #

    def _enter(self, state: State, reason: str) -> None:
        if state != self.state:
            log.info("%s -> %s: %s", self.state, state, reason)
            self.state = state
            self.reason = reason

    def _release(self, state: State, reason: str) -> None:
        """Stop receiving and give everything back. Safe to repeat."""
        self.recorder.end_segment()
        self.channel = None
        self._enter(state, reason)
        self._flush_measurements()
        self.sdr.close()
        self._sync_audio()
        self._report()

    def _flush_measurements(self) -> None:
        # The old channel's reading must not be reported against the new one.
        self.signal_dbfs = 0
        self.snr_db = 0
        self.squelch.reset()

    def _retune(self, channel: Channel) -> None:
        """Status goes out before the tuner moves, so a consumer never shows a new
        frequency with the previous channel's signal or squelch."""
        self.recorder.end_segment()
        self._enter(State.TUNING, f"tuning {channel.label}")
        self.channel = channel
        self.demod.set_spacing(channel.spacing)
        self._flush_measurements()
        self._report()
        self._tune()

    def _tune(self) -> None:
        try:
            self.sdr.set_frequency(self.channel.freq_hz)
        except SdrError as e:
            self._sdr_failed(str(e))
            return
        self._settle_at = self.clock() + self.cfg.sdr["settle_ms"] / 1000.0
        self._sync_audio()

    def _sdr_failed(self, reason: str) -> None:
        log.warning("SDR unavailable: %s", reason)
        self.recorder.end_segment()
        self.sdr.close()
        self._enter(State.FAULT, reason)
        self._flush_measurements()
        self._sdr_retry_at = self.clock() + self.cfg.sdr["retry_s"]
        self._sync_audio()
        self._report()

    # -- sound device ------------------------------------------------------- #

    def _sync_audio(self) -> None:
        """Make the sound device match the rules at the top of this module."""
        now = self.clock()
        if self.volume <= 0 or self.state not in _TUNED:
            self.audio.close()
            self._audio_fault = False
            return
        if self.audio.is_open:
            try:
                self.audio.poll()
            except AudioError as e:
                self._audio_failed(e)
                return
            if now - self._audio_opened_at >= _AUDIO_PROVEN_S:
                self._audio_fault = False
        elif now >= self._audio_retry_at:
            try:
                self.audio.open()
                self._audio_opened_at = now
            except AudioError as e:
                self._audio_failed(e)

    def _audio_failed(self, error: AudioError) -> None:
        if not self._audio_fault:
            log.warning("%s", error)
        self._audio_fault = True
        self._audio_retry_at = self.clock() + self.cfg.audio["retry_s"]

    # -- status ------------------------------------------------------------- #

    @property
    def faults(self) -> int:
        mask = 0
        if self.state is State.INVALID_FREQ:
            mask |= FAULT_FREQUENCY
        if self.state is State.FAULT:
            mask |= FAULT_SDR
        if self._audio_fault:
            mask |= FAULT_AUDIO
        if self.recorder.fault and self.record_enabled:
            mask |= FAULT_RECORD
        return mask

    def payload(self) -> dict[str, int]:
        """The wire format. Integers only: see `publisher.py`."""
        return {
            "sdrradiostatus": STATUS_CODE[self.state],
            "sdrradiofault": self.faults,
            "sdrradiofreqactive": self.channel.name_khz if self.channel else 0,
            "sdrradiovolumeactive": self.volume,
            "sdrradiosquelchactive": self.squelch_db,
            "sdrradiorecordactive": int(self.record_enabled),
            "sdrradiosquelchopen": int(self.state is State.RECEIVING),
            "sdrradiosignal": self.signal_dbfs,
            "sdrradiosnr": self.snr_db,
            "sdrradiosdr": int(self.sdr.is_open),
            "sdrradioaudio": int(self.audio.is_open),
            "sdrradiorecording": int(self.recorder.active and not self.recorder.fault),
        }

    def _report(self) -> None:
        self.publisher.publish(self.payload())
