"""Synthetic IQ: known signals through the real chain, with no radio attached.

Used by `self-test` and by the test suite. Levels are stated in dBFS of the signal
itself and of the noise in the full sampled bandwidth, so an expected channel S/N
can be computed independently of the receiver under test.
"""

from __future__ import annotations

import math

import numpy as np

from rbsdrradio.dsp import FS_HZ, TUNER_OFFSET_HZ

__all__ = ["noise", "carrier", "am", "fm", "quantize", "channel_noise_dbfs", "from_dbfs"]

#: Where the wanted channel lands with the tuner at TUNER_OFFSET_HZ.
CHANNEL_AT_HZ = -TUNER_OFFSET_HZ


def from_dbfs(dbfs: float) -> float:
    return 10.0 ** (dbfs / 20.0)


def channel_noise_dbfs(noise_dbfs: float, bandwidth_hz: float) -> float:
    """Noise in `bandwidth_hz` given noise of `noise_dbfs` across the whole of FS_HZ."""
    return noise_dbfs + 10.0 * math.log10(bandwidth_hz / FS_HZ)


def _time(seconds: float) -> np.ndarray:
    return np.arange(int(round(seconds * FS_HZ))) / FS_HZ


def noise(seconds: float, dbfs: float, seed: int = 0) -> np.ndarray:
    n = int(round(seconds * FS_HZ))
    rng = np.random.default_rng(seed)
    scale = from_dbfs(dbfs) / math.sqrt(2.0)
    return (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * scale


def carrier(seconds: float, dbfs: float, offset_hz: float = 0.0) -> np.ndarray:
    """Unmodulated carrier `offset_hz` above the tuned channel."""
    return from_dbfs(dbfs) * np.exp(2j * np.pi * (CHANNEL_AT_HZ + offset_hz) * _time(seconds))


def am(seconds: float, dbfs: float, tone_hz: float = 1000.0, depth: float = 0.8,
       offset_hz: float = 0.0) -> np.ndarray:
    """AM carrier with a tone. `dbfs` is the carrier level."""
    t = _time(seconds)
    return carrier(seconds, dbfs, offset_hz) * (1.0 + depth * np.sin(2.0 * np.pi * tone_hz * t))


def fm(seconds: float, dbfs: float, tone_hz: float = 1000.0,
       deviation_hz: float = 50_000.0) -> np.ndarray:
    """Broadcast-style FM carrying a tone."""
    t = _time(seconds)
    beta = deviation_hz / tone_hz
    phase = 2.0 * np.pi * CHANNEL_AT_HZ * t + beta * np.sin(2.0 * np.pi * tone_hz * t)
    return from_dbfs(dbfs) * np.exp(1j * phase)


def quantize(iq: np.ndarray) -> np.ndarray:
    """What the dongle's 8-bit ADC would deliver: interleaved cu8."""
    raw = np.empty(iq.size * 2, dtype=np.float32)
    raw[0::2] = iq.real
    raw[1::2] = iq.imag
    return np.clip(np.round(raw * 127.5 + 127.5), 0, 255).astype(np.uint8)
