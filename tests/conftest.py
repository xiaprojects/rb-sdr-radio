"""Shared fixtures. No test needs a dongle, a sound card or a network."""

import copy
import json
import time

import numpy as np
import pytest

import rbsdrradio._threadcaps  # noqa: F401  -- must precede any numpy import
from rbsdrradio.config import SHIPPED_PATH, Config
from rbsdrradio.freqrules import channel_from_name

BLOCK = 32768


@pytest.fixture
def cfg(tmp_path):
    """The shipped configuration, recording into a temporary folder."""
    raw = json.loads(SHIPPED_PATH.read_text())
    raw = copy.deepcopy(raw)
    raw["record"]["dir"] = str(tmp_path / "playback")
    raw["record"]["min_free_mb"] = 0
    return Config(raw, SHIPPED_PATH)


class Clock:
    """A clock tests move by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


def blocks_of(iq, size=BLOCK):
    """Cut an IQ stream into the blocks the reader would deliver."""
    iq = iq.astype(np.complex64)
    return [iq[i:i + size] for i in range(0, iq.size - size + 1, size)]


def channel(mhz_khz):
    return channel_from_name(mhz_khz)


def wait_until(predicate, timeout=5.0, step=0.01):
    """Poll a condition from a test. Fails loudly rather than sleeping and hoping."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def tone_level(audio, fs, tone_hz):
    """`(rms of the tone, tone-to-rest in dB)`, measured independently of the chain."""
    window = np.hanning(audio.size)
    power = np.abs(np.fft.rfft(audio * window)) ** 2
    k = int(round(tone_hz * audio.size / fs))
    tone = float(np.sum(power[max(k - 4, 0):k + 5]))
    rest = max(float(np.sum(power)) - tone, 1e-20)
    rms = np.sqrt(tone / (np.sum(window ** 2) * audio.size / 2.0))
    return float(rms), 10.0 * np.log10(tone / rest)
