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

S/N, for AM, is the strongest carrier in the channel over the channel noise, from the
same periodogram. Channel power, the measurement before, read 2 to 3 dB on an empty
channel whose noise was a little stronger inside than beside it, which is where a
squelch has to sit (flight tests, 2026-11-10). Five bins of a carrier stand out from
noise spread over hundreds; noise alone reads 0.

AM audio, in order: detector (envelope, or synchronous as an option), division by the
carrier, the voice band-pass, optional noise reduction, then a soft limiter. On weak
signals the detector matters little: at 5 dB of carrier over noise in a 25 kHz
channel, a synchronous detector gained 0.6 dB over an envelope detector in
simulation, and narrowing the audio to 2.7 kHz gained 1.1 dB.
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
           "CarrierLock", "NoiseReducer", "DETECTORS", "VOICE_BAND_HZ",
           "to_complex64", "pcm16", "lowpass", "soft_limit"]

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

#: Audio passband. COM receivers are specified for about 350 to 2500 Hz (DO-186, ED-23).
#: Above 2.7 kHz an airband channel carries hiss and nothing that helps understanding;
#: the third-order 300-3400 Hz filter this replaced let that hiss through to 6 kHz
#: (hangar recordings, 2026-11-08).
VOICE_BAND_HZ = (300.0, 2_700.0)

#: Headroom under full scale for a fully modulated carrier. The output is linear up to
#: here, and `soft_limit` bends what lies beyond.
_AUDIO_SCALE = 0.8
#: AM stays within -1..+1 of its carrier, a little beyond if overmodulated. A carrier
#: that jumps up (a strong station keying over a weak one, the end of a fade) reads
#: several times that until the 30 ms carrier average catches up, which played as a
#: full-scale click. Bounded here, a jump is no louder than a fully modulated peak.
_RATIO_LIMIT = 1.5
_DB_FLOOR = 1e-12

ENVELOPE = "envelope"
SYNCHRONOUS = "synchronous"
DETECTORS = (ENVELOPE, SYNCHRONOUS)


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


@cache
def voice_filter() -> np.ndarray:
    """Eighth-order elliptic band-pass: 0.5 dB ripple across VOICE_BAND_HZ, 60 dB down
    by 3.0 kHz and below 250 Hz."""
    # Left writable: scipy's sosfilt refuses a read-only array, though it only reads it.
    return signal.ellip(8, 0.5, 60.0, VOICE_BAND_HZ, btype="bandpass", fs=AM_AUDIO_HZ,
                        output="sos")


def soft_limit(x: np.ndarray, knee: float = _AUDIO_SCALE) -> np.ndarray:
    """Linear up to `knee`, then bending smoothly towards full scale, never beyond it.

    A hard clip squares off whatever crosses it, the harsh edge noise peaks get on a
    weak carrier. The slope is 1 at the knee, so nothing below it changes.
    """
    mag = np.abs(x)
    over = mag > knee
    if not over.any():
        return x
    head = 1.0 - knee
    y = x.copy()
    y[over] = np.sign(x[over]) * (knee + head * np.tanh((mag[over] - knee) / head))
    return y


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
    """Noise density and the strongest carrier in the channel, from a periodogram.

    `sigma2` is the noise power of the whole stream, so a filter `h` of unity DC
    gain passes `sigma2 * sum(h ** 2)`. That makes the channel noise independent
    of the filter in use and of the tuner gain.

    Noise-only bins are exponentially distributed, so the `quantile` point sits at
    `-ln(1 - quantile)` times the mean. The FM band can be so full that the median
    bin is a station, hence a lower quantile there.

    `carrier` is the power per sample of the strongest carrier within `search_hz` of the
    channel centre, above that frame's own floor: the five bins of a Hann main lobe, so
    a carrier between two bins reads the same. Five noise bins of 4096 are a tiny part
    of a channel's noise, which is the point of it: noise alone reads some 16 dB below
    the channel noise, where a broadband measurement reads 0 dB and anything that
    raises the noise inside the channel more than beside it reads as signal. A frame's
    own floor, not the smoothed one, so an impulse raising every bin raises nothing.
    """

    _LOBE = 5

    def __init__(self, fs: int, nfft: int, hop: int, inner_hz: float, outer_hz: float,
                 quantile: float = 0.5, smooth: float = 0.3, search_hz: float | None = None):
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
        self._bias = -math.log(1.0 - quantile)
        self.set_search(inner_hz if search_hz is None else search_hz)
        self.reset()

    def set_search(self, search_hz: float) -> None:
        """Where a carrier may be: the channel filter's pass edge, so the neighbouring
        channel's carrier, 8.33 kHz away, is not taken for this one's."""
        self._near_bins = np.flatnonzero(np.abs(self._freqs) < search_hz)
        # Each near bin with its neighbours, wrapping: 0 Hz is at both ends of an FFT.
        half = self._LOBE // 2
        self._lobe_bins = (self._near_bins[:, None] + np.arange(-half, half + 1)) % self.nfft

    def reset(self) -> None:
        self.sigma2: float | None = None
        self.carrier: float | None = None
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
        # Parseval: the bins of a periodogram scaled this way sum to nfft times the power.
        excess = float(np.max(power[self._lobe_bins].sum(axis=1))) - self._LOBE * floor
        self.carrier = max(excess, 0.0) / self.nfft


class CarrierLock:
    """The phase of the channel's carrier, for synchronous detection.

    Feed-forward, so numpy does a block at a time with no loop per sample: an FFT of
    the latest `nfft` samples finds the carrier to a few hertz, an oscillator moves it
    to DC, and a 5 ms average of the result gives its phase. AM sidebands come in
    mirror pairs and average to a real number, so modulation changes the length of
    that average, never its angle.

    With several carriers in the channel (an offset-carrier ground station, two
    aircraft at once) it keeps the one it has while that stays within 6 dB of the
    strongest, rather than hopping between them block by block.
    """

    _CARRIER_RATIO = 100.0           # 20 dB over the median bin, as in SpectrumMeter
    _KEEP_RATIO = 0.25               # 6 dB
    _KEEP_BINS = 2

    def __init__(self, fs: int, search_hz: float, tau_s: float = 0.005, nfft: int = 512):
        self.fs = fs
        self.nfft = nfft
        self._alpha = 1.0 - math.exp(-1.0 / (fs * tau_s))
        self._window = np.hanning(nfft).astype(np.float32)
        self._freqs = np.fft.fftfreq(nfft, 1.0 / fs)
        self._search = np.flatnonzero(np.abs(self._freqs) <= search_hz)
        self.reset()

    def reset(self) -> None:
        self.freq_hz = 0.0
        self.locked = False
        self._phase = 0.0
        self._recent = np.zeros(self.nfft, dtype=np.complex64)
        self._zi: np.ndarray | None = None

    def _find(self) -> None:
        spectrum = np.fft.fft(self._recent * self._window)
        power = spectrum.real ** 2 + spectrum.imag ** 2
        best = self._search[int(np.argmax(power[self._search]))]
        if power[best] <= self._CARRIER_RATIO * float(np.median(power)):
            self.locked = False                  # keep the frequency; it may come back
            return
        if self.locked:
            here = int(round(self.freq_hz * self.nfft / self.fs))
            near = np.arange(here - self._KEEP_BINS, here + self._KEEP_BINS + 1) % self.nfft
            mine = near[int(np.argmax(power[near]))]
            if power[mine] >= self._KEEP_RATIO * power[best]:
                best = mine
        # Parabola through the log of three bins: a few hertz, where a bin is 31 Hz.
        lo, mid, hi = np.log(power[[(best - 1) % self.nfft, best, (best + 1) % self.nfft]])
        bend = lo - 2.0 * mid + hi
        shift = 0.5 * (lo - hi) / bend if bend < 0.0 else 0.0
        self.freq_hz = float(self._freqs[best]) + shift * self.fs / self.nfft
        self.locked = True

    def process(self, z: np.ndarray) -> np.ndarray:
        """The part of `z` in phase with the carrier: its amplitude, modulation included,
        plus half the noise. Noise in quadrature, the other half, is left out."""
        n = z.size
        if n >= self.nfft:
            self._recent = z[-self.nfft:]
        else:
            self._recent = np.concatenate((self._recent[n:], z))
        self._find()
        step = 2.0 * math.pi * self.freq_hz / self.fs
        phase = self._phase + step * np.arange(n)
        self._phase = (self._phase + step * n) % (2.0 * math.pi)
        base = z * np.exp(-1j * phase)
        b, a = [self._alpha], [1.0, self._alpha - 1.0]
        if self._zi is None:
            self._zi = signal.lfilter_zi(b, a) * base[0]
        carrier, self._zi = signal.lfilter(b, a, base, zi=self._zi)
        return np.real(base * np.conj(carrier)) / np.maximum(np.abs(carrier), 1e-12)


class NoiseReducer:
    """Spectral noise reduction for a noise spectrum known in advance.

    Short-time spectra (512 samples, 75 % overlap, square-root Hann in and out) are
    each scaled by a Wiener gain from the decision-directed a-priori S/N of Ephraim
    and Malah, never more than `reduction_db` below unity. The usual difficulty,
    guessing the noise from the audio, does not arise: the caller passes it with each
    block, from the density the spectrum meter measures beside the channel. Learning it
    from quiet moments would learn ATIS as noise, as it would for the squelch.

    Delays by NFFT samples, 32 ms at 16 kHz, and returns as many samples as it is
    given, however the input is cut.
    """

    NFFT = 512
    HOP = 128
    _SMOOTHING = 0.96

    def __init__(self, reduction_db: float):
        self.reduction_db = reduction_db
        self._floor = 10.0 ** (-reduction_db / 20.0)
        self._window = np.sqrt(np.hanning(self.NFFT + 1)[:-1])      # periodic
        #: E|X|^2 in each bin for white noise of unit variance.
        self.white_gain = float(np.sum(self._window ** 2))
        self.reset()

    def reset(self) -> None:
        self._input = np.zeros(self.NFFT - self.HOP)
        self._tail = np.zeros(self.NFFT - self.HOP)
        self._output = np.zeros(self.HOP)
        self._clean: np.ndarray | None = None

    def process(self, x: np.ndarray, noise: np.ndarray | None) -> np.ndarray:
        """`noise` is E|X|^2 per bin for this block, or None to pass the audio through
        (still delayed, so the timing never jumps)."""
        size, hop = self.NFFT, self.HOP
        buf = np.concatenate((self._input, x))
        count = (buf.size - (size - hop)) // hop
        if count:
            frames = sliding_window_view(buf, size)[::hop][:count]
            spectra = np.fft.rfft(frames * self._window, axis=1)
            gains = self._gains(spectra.real ** 2 + spectra.imag ** 2, noise)
            frames = np.fft.irfft(spectra * gains, n=size, axis=1) * (self._window / 2.0)
            acc = np.zeros(count * hop + size - hop)
            acc[:size - hop] = self._tail
            for i in range(count):
                acc[i * hop:i * hop + size] += frames[i]
            done = count * hop
            self._output = np.concatenate((self._output, acc[:done]))
            self._tail = acc[done:]
            self._input = buf[done:]
        else:
            self._input = buf
        out, self._output = self._output[:x.size], self._output[x.size:]
        return out

    def _gains(self, power: np.ndarray, noise: np.ndarray | None) -> np.ndarray:
        if noise is None:
            self._clean = None
            return np.ones_like(power)
        noise = np.maximum(noise, 1e-30)
        gains = np.empty_like(power)
        clean = self._clean
        for i, p in enumerate(power):
            prior = np.maximum(p / noise - 1.0, 0.0)
            if clean is not None:
                prior = self._SMOOTHING * clean / noise + (1.0 - self._SMOOTHING) * prior
            gains[i] = np.maximum(prior / (1.0 + prior), self._floor)
            clean = gains[i] ** 2 * p
        self._clean = clean
        return gains


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
    """Airband AM, with carrier-normalised output.

    The envelope detector does not care where the carrier sits inside the filter,
    which is why an uncalibrated dongle still sounds right. The synchronous detector
    finds the carrier first (`CarrierLock`) and keeps only what is in phase with it;
    it has no threshold, so it degrades more gently on the weakest signals. Dividing
    by the carrier makes loudness follow modulation depth, not distance from the
    transmitter.
    """

    audio_hz = AM_AUDIO_HZ

    def __init__(self, spacing: str = SPACING_25, carrier_tau_s: float = 0.03,
                 meter_nfft: int = 4096, detector: str = ENVELOPE,
                 noise_reduction_db: float = 0.0):
        if detector not in DETECTORS:
            raise ValueError(f"detector must be one of {DETECTORS}, not {detector!r}")
        self.detector = detector
        self._alpha = 1.0 - math.exp(-1.0 / (AM_AUDIO_HZ * carrier_tau_s))
        self._voice = voice_filter()
        self._mixer = Fs4Mixer()
        pass1 = CHANNEL_FILTER_HZ[SPACING_25][1] + 31_000.0   # keeps the noise bins
        self._front = DecimatingFir(lowpass(FS_HZ, pass1, IF_HZ - pass1), _AM_DECIM_1)
        # A frame every block, for a carrier reading per block. The floor keeps the
        # time constant it had at one frame in 32768 samples.
        self._meter = SpectrumMeter(IF_HZ, nfft=meter_nfft, hop=0,
                                    inner_hz=9_000.0, outer_hz=38_000.0,
                                    smooth=1.0 - 0.7 ** (meter_nfft / 32_768))
        self._lock: CarrierLock | None = None
        self._nr = NoiseReducer(noise_reduction_db) if noise_reduction_db > 0 else None
        if self._nr is not None:
            bins = np.fft.rfftfreq(NoiseReducer.NFFT, 1.0 / AM_AUDIO_HZ)
            _, response = signal.sosfreqz(self._voice, worN=bins, fs=AM_AUDIO_HZ)
            # See _audio_noise: the density per hertz times fs and the window's gain.
            self._noise_shape = (self._nr.white_gain * np.abs(response) ** 2
                                 * AM_AUDIO_HZ / (2.0 * IF_HZ))
        self.set_spacing(spacing)

    def set_spacing(self, spacing: str) -> None:
        self.spacing = spacing
        taps = lowpass(IF_HZ, *CHANNEL_FILTER_HZ[spacing])
        self._channel = DecimatingFir(taps, _AM_DECIM_2)
        self._noise_gain = float(np.sum(np.square(taps, dtype=np.float64)))
        self._meter.set_search(CHANNEL_FILTER_HZ[spacing][0])
        if self.detector == SYNCHRONOUS:
            self._lock = CarrierLock(AM_AUDIO_HZ, search_hz=CHANNEL_FILTER_HZ[spacing][0])
        self.reset()

    def reset(self) -> None:
        self._mixer.reset()
        self._front.reset()
        self._channel.reset()
        self._meter.reset()
        if self._lock is not None:
            self._lock.reset()
        if self._nr is not None:
            self._nr.reset()
        self._carrier_zi = None
        self._voice_zi = signal.sosfilt_zi(self._voice) * 0.0
        self._filled = False

    def resume(self) -> None:
        """Back on a channel after looking away: the samples jumped, the noise did not."""
        sigma2 = self._meter.sigma2
        self.reset()
        self._meter.sigma2 = sigma2

    def dwell(self, iq: np.ndarray, fill_samples: int) -> float:
        """S/N of one short look at a channel just hopped to. The head fills the filters,
        and the meter needs all of it: `meter_nfft` 2048 is 16 ms of the 128 kHz stream."""
        self.reset()
        self.process(iq[:fill_samples])
        return self.process(iq[fill_samples:]).snr_db

    def process(self, iq: np.ndarray) -> Measurement:
        intermediate = self._front.process(self._mixer.process(iq))
        self._meter.push(intermediate)
        z = self._channel.process(intermediate)
        if z.size == 0:
            return _no_output()
        signal_dbfs, noise_dbfs, _ = _measure(z, self._noise_gain, self._meter.sigma2,
                                              self._filled)
        snr_db = self._carrier_snr_db(noise_dbfs) if self._filled else 0.0
        filled, self._filled = self._filled, True

        detected = np.abs(z) if self._lock is None else self._lock.process(z)
        b, a = [self._alpha], [1.0, self._alpha - 1.0]
        if self._carrier_zi is None:
            self._carrier_zi = signal.lfilter_zi(b, a) * detected[0]
        carrier, self._carrier_zi = signal.lfilter(b, a, detected, zi=self._carrier_zi)
        carrier = np.maximum(carrier, 1e-6)
        audio = np.clip((detected - carrier) / carrier, -_RATIO_LIMIT, _RATIO_LIMIT)
        audio, self._voice_zi = signal.sosfilt(self._voice, audio, zi=self._voice_zi)
        if self._nr is not None:
            audio = self._nr.process(audio, self._audio_noise(carrier, filled))
        audio = soft_limit(audio * _AUDIO_SCALE).astype(np.float32)
        return Measurement(audio, signal_dbfs, noise_dbfs, snr_db, self._meter.peak_hz)

    def _carrier_snr_db(self, noise_dbfs: float) -> float:
        """Carrier over the channel noise, the S/N every AM reading uses.

        A carrier is what an AM transmitter sends for as long as it is keyed, voice or
        not, so this tells a station from noise far better than the power in the
        channel does (see SpectrumMeter). The scale stays the same: a carrier 5 dB over
        the channel noise reads 5 dB, a little under what the channel power read with
        modulation on it.
        """
        carrier = self._meter.carrier
        if carrier is None or noise_dbfs <= -120.0:
            return 0.0
        return max(0.0, db(carrier) - noise_dbfs)

    def _audio_noise(self, carrier: np.ndarray, filled: bool) -> np.ndarray | None:
        """E|X|^2 of the noise in each NoiseReducer bin, before the audio is scaled.

        The meter's `sigma2` is the noise per sample of the 128 kHz stream: a density of
        sigma2 / IF_HZ per hertz, whatever the channel width. The detector keeps the half
        in phase with the carrier and divides by the carrier, which leaves a density of
        sigma2 / (2 IF_HZ carrier^2), coloured by the voice filter. In simulation it is
        within 0.5 dB of the real noise down to 0 dB of S/N with the synchronous
        detector, and within 1 dB down to 3 dB with the envelope detector. Below that it
        overstates the noise (2.5 dB at -3 dB), where there is little but noise.
        """
        sigma2 = self._meter.sigma2
        if sigma2 is None or not filled:
            return None
        return self._noise_shape * (sigma2 / float(np.mean(np.square(carrier))))


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
