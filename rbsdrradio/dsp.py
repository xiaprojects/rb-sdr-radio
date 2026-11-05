"""Receive chain: sample blocks in, audio and channel measurements out.

Sample plan (exact integer ratios, nothing to resample)::

    AM  1.024 Msps -> mix -fs/4 -> /8 -> 128 kHz -> channel filter /8 -> 16 kHz
    FM  1.024 Msps -> mix -fs/4 -> /4 -> 256 kHz -> discriminator -> /8 -> 32 kHz

The tuner sits fs/4 below the channel (rb-vor amendment A1). That moves the wanted
signal off the dongle's DC spur, and at exactly fs/4 the down-mix is the +1/-j/-1/+j
sequence, so it costs no trigonometry.

Noise floor. Channel power is compared with the noise density measured *around*
the channel, from the median of a periodogram of the 128 kHz stream, not with a
level remembered from when the channel was quiet. ATIS and VOLMET are continuous
carriers: a remembered floor either learns the carrier as noise after a minute,
or, if the receiver tunes in mid-broadcast, starts there, and in both cases the
squelch shuts a healthy station off. A median over a few thousand bins is unmoved
by the handful of bins a carrier occupies, on this channel or on its neighbours.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cache

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy import signal

from rbsdrradio.freqrules import SPACING_25, SPACING_833

__all__ = ["FS_HZ", "TUNER_OFFSET_HZ", "AM_AUDIO_HZ", "FM_AUDIO_HZ", "Measurement",
           "AmDemod", "FmDemod", "DecimatingFir", "Fs4Mixer", "SpectrumMeter",
           "to_complex64", "pcm16", "lowpass"]

FS_HZ = 1_024_000
#: Tuner sits this far from the wanted frequency (see module docstring).
TUNER_OFFSET_HZ = -FS_HZ // 4

_AM_DECIM_1 = 8
_AM_DECIM_2 = 8
IF_HZ = FS_HZ // _AM_DECIM_1
AM_AUDIO_HZ = IF_HZ // _AM_DECIM_2

_FM_DECIM_1 = 4
_FM_DECIM_2 = 8
FM_IF_HZ = FS_HZ // _FM_DECIM_1
FM_AUDIO_HZ = FM_IF_HZ // _FM_DECIM_2

#: (pass, stop) of the channel filter, one-sided Hz.
#:
#: 8.33 kHz: 3.2 kHz passes the audio; the neighbouring carrier sits at 8.33 kHz
#: and its sidebands start near 5.3 kHz.
#: 25 kHz: wide on purpose. Older 25 kHz transmitters are allowed tens of ppm, so
#: a legal carrier can sit several kHz off the channel. The stop edge is the limit
#: of what a 16 kHz output rate can hold without folding.
CHANNEL_FILTER_HZ = {SPACING_833: (3_200.0, 5_000.0), SPACING_25: (7_000.0, 9_000.0)}

#: Headroom under full scale for a fully modulated carrier.
_AUDIO_SCALE = 0.8
_DB_FLOOR = 1e-12


def db(power: float) -> float:
    return 10.0 * math.log10(max(power, _DB_FLOOR))


def to_complex64(raw: np.ndarray) -> np.ndarray:
    """cu8 (unsigned, centred on 127.5: rb-vor A4) to complex64 in +/-1."""
    iq = (raw.astype(np.float32) - 127.5) / 127.5
    return iq[0::2] + 1j * iq[1::2]


def pcm16(audio: np.ndarray) -> bytes:
    """float audio in +/-1 to S16_LE."""
    return (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


@cache
def lowpass(fs: float, pass_hz: float, stop_hz: float, atten_db: float = 70.0) -> np.ndarray:
    """Unity-DC Kaiser FIR. Cached: a retune must not redesign the filters."""
    taps, beta = signal.kaiserord(atten_db, (stop_hz - pass_hz) / (fs / 2.0))
    h = signal.firwin(taps | 1, (pass_hz + stop_hz) / 2.0, window=("kaiser", beta), fs=fs)
    h = h.astype(np.float32)
    h.setflags(write=False)
    return h


class Fs4Mixer:
    """Multiply by (-j)^n, keeping the phase across blocks."""

    _LUT = np.array([1, -1j, -1, 1j], dtype=np.complex64)

    def __init__(self) -> None:
        self._phase = 0

    def reset(self) -> None:
        self._phase = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        idx = (np.arange(x.size) + self._phase) & 3
        self._phase = (self._phase + x.size) & 3
        return x * self._LUT[idx]


class DecimatingFir:
    """Streaming polyphase FIR decimator.

    Only the windows that produce an output are evaluated, which is what makes a
    numpy implementation affordable at 1 Msps. Output does not depend on how the
    input is cut into blocks.
    """

    def __init__(self, taps: np.ndarray, factor: int, dtype=np.complex64):
        self.taps = taps
        self.factor = factor
        self._rev = np.asarray(taps[::-1], dtype=np.float32)
        self._dtype = dtype
        self.reset()

    def reset(self) -> None:
        self._hist = np.zeros(self._rev.size - 1, dtype=self._dtype)
        self._skip = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:                      # a short USB read is not an error
            return np.empty(0, dtype=np.complex64)
        size = self._rev.size
        buf = np.concatenate((self._hist, x))
        windows = sliding_window_view(buf, size)[self._skip :: self.factor]
        out = windows @ self._rev
        self._skip = (self._skip - (buf.size - size + 1)) % self.factor
        self._hist = buf[-(size - 1):].copy()
        return out


class SpectrumMeter:
    """Noise density and strongest in-channel bin, from an occasional periodogram.

    `sigma2` is the noise power of the whole stream, so a filter `h` of unity DC
    gain passes `sigma2 * sum(h ** 2)`. That makes the channel noise independent
    of the filter in use and of the tuner gain.

    Noise-only bins are exponentially distributed, so the `quantile` point sits at
    `-ln(1 - quantile)` times the mean. The FM band can be so full that the median
    bin is a station, hence a lower quantile there.
    """

    def __init__(self, fs: int, nfft: int, hop: int, inner_hz: float, outer_hz: float,
                 quantile: float = 0.5, smooth: float = 0.3):
        self.fs = fs
        self.nfft = nfft
        self.hop = hop
        self.quantile = quantile
        self.smooth = smooth
        self._window = np.hanning(nfft).astype(np.float32)
        self._norm = float(np.sum(self._window ** 2))
        self._freqs = np.fft.fftfreq(nfft, 1.0 / fs)
        mag = np.abs(self._freqs)
        self._noise_bins = (mag >= inner_hz) & (mag <= outer_hz)
        self._near_bins = np.flatnonzero(mag < inner_hz)
        self._bias = -math.log(1.0 - quantile)
        self.reset()

    def reset(self) -> None:
        self.sigma2: float | None = None
        self.peak_hz = 0.0
        self._pending: list[np.ndarray] = []
        self._have = 0
        self._skip = 0

    def push(self, x: np.ndarray) -> None:
        if self._skip >= x.size:
            self._skip -= x.size
            return
        self._pending.append(x[self._skip:])
        self._have += x.size - self._skip
        self._skip = 0
        if self._have >= self.nfft:
            frame = np.concatenate(self._pending)[:self.nfft]
            leftover = self._have - self.nfft
            self._pending, self._have = [], 0
            self._skip = max(0, self.hop - leftover)
            self._measure(frame)

    def _measure(self, frame: np.ndarray) -> None:
        spectrum = np.fft.fft(frame * self._window)
        power = (spectrum.real ** 2 + spectrum.imag ** 2) / self._norm
        floor = float(np.quantile(power[self._noise_bins], self.quantile)) / self._bias
        if self.sigma2 is None:
            self.sigma2 = floor
        else:
            self.sigma2 += self.smooth * (floor - self.sigma2)
        near = self._near_bins
        best = near[int(np.argmax(power[near]))]
        # Only a bin standing clear of the noise is a carrier.
        self.peak_hz = float(self._freqs[best]) if power[best] > 100.0 * floor else 0.0


@dataclass(frozen=True)
class Measurement:
    """One block through the demodulator."""

    audio: np.ndarray
    signal_dbfs: float
    noise_dbfs: float
    snr_db: float
    carrier_hz: float = 0.0


def _no_output() -> Measurement:
    """A block too short to yield one output sample."""
    return Measurement(np.empty(0, dtype=np.float32), -120.0, -120.0, 0.0)


def _measure(z: np.ndarray, noise_gain: float, sigma2: float | None,
             filled: bool = True) -> tuple[float, float, float]:
    """`(signal dBFS, channel noise dBFS, S/N dB)` of a filtered channel stream.

    `filled` is False for the first block after a reset. The filters are starting
    from silence then, so a strong neighbour reaches the channel as a step, and the
    splatter reads as signal for that one block. It would happen on every retune.
    """
    power = float(np.mean(z.real ** 2 + z.imag ** 2))
    if sigma2 is None or not filled:
        return db(power), -120.0, 0.0
    noise = sigma2 * noise_gain
    return db(power), db(noise), max(0.0, db(power / max(noise, _DB_FLOOR) - 1.0))


class AmDemod:
    """Airband AM: envelope detector with carrier-normalised output.

    An envelope detector does not care where the carrier sits inside the filter,
    which is why an uncalibrated dongle still sounds right. Dividing by the carrier
    makes loudness follow modulation depth, not distance from the transmitter.
    """

    audio_hz = AM_AUDIO_HZ

    def __init__(self, spacing: str = SPACING_25, carrier_tau_s: float = 0.03):
        self._alpha = 1.0 - math.exp(-1.0 / (AM_AUDIO_HZ * carrier_tau_s))
        self._voice = signal.butter(3, (300.0, 3400.0), btype="bandpass",
                                    fs=AM_AUDIO_HZ, output="sos")
        self._mixer = Fs4Mixer()
        pass1 = CHANNEL_FILTER_HZ[SPACING_25][1] + 31_000.0   # keeps the noise bins
        self._front = DecimatingFir(lowpass(FS_HZ, pass1, IF_HZ - pass1), _AM_DECIM_1)
        self._meter = SpectrumMeter(IF_HZ, nfft=4096, hop=28_672, inner_hz=9_000.0,
                                    outer_hz=38_000.0)
        self.set_spacing(spacing)

    def set_spacing(self, spacing: str) -> None:
        self.spacing = spacing
        taps = lowpass(IF_HZ, *CHANNEL_FILTER_HZ[spacing])
        self._channel = DecimatingFir(taps, _AM_DECIM_2)
        self._noise_gain = float(np.sum(np.square(taps, dtype=np.float64)))
        self.reset()

    def reset(self) -> None:
        self._mixer.reset()
        self._front.reset()
        self._channel.reset()
        self._meter.reset()
        self._carrier_zi = None
        self._voice_zi = signal.sosfilt_zi(self._voice) * 0.0
        self._filled = False

    def process(self, iq: np.ndarray) -> Measurement:
        intermediate = self._front.process(self._mixer.process(iq))
        self._meter.push(intermediate)
        z = self._channel.process(intermediate)
        if z.size == 0:
            return _no_output()
        signal_dbfs, noise_dbfs, snr_db = _measure(z, self._noise_gain, self._meter.sigma2,
                                                   self._filled)
        self._filled = True

        envelope = np.abs(z)
        b, a = [self._alpha], [1.0, self._alpha - 1.0]
        if self._carrier_zi is None:
            self._carrier_zi = signal.lfilter_zi(b, a) * envelope[0]
        carrier, self._carrier_zi = signal.lfilter(b, a, envelope, zi=self._carrier_zi)
        audio = (envelope - carrier) / np.maximum(carrier, 1e-6)
        audio, self._voice_zi = signal.sosfilt(self._voice, audio, zi=self._voice_zi)
        audio = np.clip(audio * _AUDIO_SCALE, -1.0, 1.0).astype(np.float32)
        return Measurement(audio, signal_dbfs, noise_dbfs, snr_db, self._meter.peak_hz)


class FmDemod:
    """Broadcast FM, mono. Exists only to prove the antenna on a loud station."""

    audio_hz = FM_AUDIO_HZ
    _DEVIATION_HZ = 75_000.0

    def __init__(self, deemphasis_us: float = 50.0):
        self._deemph = 1.0 - math.exp(-1.0 / (FM_IF_HZ * deemphasis_us * 1e-6))
        self._mixer = Fs4Mixer()
        taps = lowpass(FS_HZ, 100_000.0, 150_000.0, atten_db=60.0)
        self._front = DecimatingFir(taps, _FM_DECIM_1)
        self._noise_gain = float(np.sum(np.square(taps, dtype=np.float64)))
        self._audio = DecimatingFir(lowpass(FM_IF_HZ, 15_000.0, 17_000.0, atten_db=60.0),
                                    _FM_DECIM_2, dtype=np.float32)
        self._meter = SpectrumMeter(FS_HZ, nfft=8192, hop=57_344, inner_hz=10_000.0,
                                    outer_hz=480_000.0, quantile=0.1)
        self.reset()

    def reset(self) -> None:
        self._mixer.reset()
        self._front.reset()
        self._audio.reset()
        self._meter.reset()
        self._last = 1.0 + 0.0j
        self._deemph_zi = np.zeros(1)
        self._filled = False

    def process(self, iq: np.ndarray) -> Measurement:
        self._meter.push(iq)
        z = self._front.process(self._mixer.process(iq))
        if z.size == 0:
            return _no_output()
        signal_dbfs, noise_dbfs, snr_db = _measure(z, self._noise_gain, self._meter.sigma2,
                                                   self._filled)
        self._filled = True

        previous = np.concatenate(([self._last], z[:-1]))
        self._last = z[-1]
        phase = np.angle(z * np.conj(previous))
        message = phase * (FM_IF_HZ / (2.0 * math.pi * self._DEVIATION_HZ))
        b, a = [self._deemph], [1.0, self._deemph - 1.0]
        message, self._deemph_zi = signal.lfilter(b, a, message, zi=self._deemph_zi)
        audio = self._audio.process(message.astype(np.float32))
        return Measurement(np.clip(audio, -1.0, 1.0).astype(np.float32),
                           signal_dbfs, noise_dbfs, snr_db)
