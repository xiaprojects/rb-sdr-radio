"""Squelch: an audio gate driven by channel S/N.

The threshold is in dB above the measured noise floor, so it means the same thing
at any tuner gain and can be drawn on the same scale as `sdrradiosnr`. 0 holds the
gate open, the "squelch test" every COM radio has.

Everything counts blocks of sample time, not wall-clock time, so the behaviour is
identical live, replayed and under test.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["Squelch", "SquelchParams"]


@dataclass(frozen=True)
class SquelchParams:
    hysteresis_db: float
    open_blocks: int
    hang_s: float


class Squelch:
    def __init__(self, params: SquelchParams, threshold_db: float, block_s: float):
        self.params = params
        self.threshold_db = threshold_db
        self._hang_blocks = max(1, round(params.hang_s / block_s))
        self.reset()

    def reset(self) -> None:
        self.open = self.forced
        self._above = 0
        self._below = 0
        self._gain = 1.0 if self.open else 0.0

    @property
    def forced(self) -> bool:
        return self.threshold_db <= 0.0

    def set_threshold(self, threshold_db: float) -> None:
        self.threshold_db = threshold_db
        if self.forced:
            self.open = True

    def update(self, snr_db: float) -> bool:
        if self.forced:
            self.open = True
        elif self.open:
            close_at = self.threshold_db - self.params.hysteresis_db
            self._below = self._below + 1 if snr_db < close_at else 0
            if self._below >= self._hang_blocks:
                self.open, self._above = False, 0
        else:
            # Two blocks, not one: ignition noise is impulsive, a carrier is not.
            self._above = self._above + 1 if snr_db >= self.threshold_db else 0
            if self._above >= self.params.open_blocks:
                self.open, self._below = True, 0
        return self.open

    def apply(self, audio: np.ndarray) -> np.ndarray:
        """Gate a block, ramping across it so the gate itself never clicks."""
        target = 1.0 if self.open else 0.0
        if target == self._gain:
            return audio if target else np.zeros_like(audio)
        ramp = np.linspace(self._gain, target, audio.size, dtype=np.float32)
        self._gain = target
        return audio * ramp
