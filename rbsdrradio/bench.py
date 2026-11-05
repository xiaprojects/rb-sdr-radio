"""`test-fm` and `test-am`: prove the antenna and the receive chain from a shell.

Both tune, play what they hear through ALSA and print one line a second, then give
a verdict with what to try next. Neither needs RB-01, the bridge or the daemon.

`test-fm` is the easy one: a broadcast station is strong and always on air, so it
separates "the antenna and dongle work" from "there was nothing to hear". `test-am`
is the real path, on an airband channel, where only an always-on station such as
ATIS gives a meaningful answer.

The verdict also checks the spectral sense. The receiver tunes fs/4 below the
channel, so an inverted device leaves the wanted signal on the far side of the band,
where it is simply not heard: exactly what a dead antenna looks like. It is measured
here rather than assumed (rb-vor A28, `swap_iq`), by retuning and watching which way
the band moves. Comparing the energy on each side of the tuner cannot answer it:
another station 512 kHz below the channel looks the same, and that check told a
healthy R820T to set `swap_iq` because 106.7 MHz had faded and 106.2 MHz had not.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from rbsdrradio.audio import AlsaSink, AudioError, volume_gain
from rbsdrradio.dsp import FS_HZ, AmDemod, FmDemod, pcm16, to_complex64
from rbsdrradio.freqrules import SPACING_25, parse_channel
from rbsdrradio.recorder import repair_wav, wav_header
from rbsdrradio.sdr import RtlDevice, SdrError, SdrReader
from rbsdrradio.squelch import Squelch, SquelchParams

__all__ = ["BenchRequest", "Bench", "run_test", "EXIT_OK", "EXIT_NO_SIGNAL", "EXIT_HARDWARE"]

log = logging.getLogger("rbsdrradio.bench")

EXIT_OK = 0
EXIT_NO_SIGNAL = 1
EXIT_HARDWARE = 3

_GOOD_SNR_DB = 20.0
_USABLE_SNR_DB = 10.0
_CLIP_LEVEL = 0.99
_CLIP_WARN = 0.001
_FM_BAND_MHZ = (87.5, 108.0)
_SETTLE_S = 0.25
#: Neither this nor twice this is a multiple of the FM or airband channel spacing, so a
#: station never lands where another one was.
_SENSE_STEP_HZ = 170_000
_SENSE_BLOCKS = 12
_SENSE_TIMEOUT_S = 2.0
_SENSE_FFT = 1024
#: The correlation that one direction must reach, and must beat the other one by. On a
#: real R820T: 0.8 or more on FM stations, 0.46 on airband, under 0.1 on an empty channel.
_SENSE_MIN_CORRELATION = 0.3


@dataclass
class BenchRequest:
    mode: str                             # "am" or "fm"
    mhz: float
    seconds: float = 30.0
    volume: int = 50
    squelch_db: int = 0
    audio: bool = True
    wav: Path | None = None
    replay: Path | None = None


@dataclass
class Readings:
    signal: list[float] = field(default_factory=list)
    noise: list[float] = field(default_factory=list)
    snr: list[float] = field(default_factory=list)
    offsets: list[float] = field(default_factory=list)
    open_blocks: int = 0
    samples: int = 0
    clipped: int = 0


class Bench:
    """One test run: blocks in, audio and readings out. Knows nothing of the source."""

    def __init__(self, request: BenchRequest, cfg, sink: AlsaSink | None,
                 out: Callable[[str], None] = print):
        self.request = request
        self.out = out
        self.sink = sink
        self.spacing = SPACING_25
        self.freq_hz = int(round(request.mhz * 1e6))
        if request.mode == "am":
            channel, problem = parse_channel(request.mhz)
            if channel is None:
                out(f"  note: {problem}; tuning it as a plain frequency")
            else:
                self.freq_hz, self.spacing = channel.freq_hz, channel.spacing
            self.demod = AmDemod(self.spacing)
        else:
            if not _FM_BAND_MHZ[0] <= request.mhz <= _FM_BAND_MHZ[1]:
                out(f"  note: {request.mhz:g} MHz is outside the FM broadcast band")
            self.demod = FmDemod()
        block_s = cfg.sdr["block_samples"] / FS_HZ
        self.squelch = Squelch(SquelchParams(**cfg.squelch), request.squelch_db, block_s)
        self.gain = volume_gain(request.volume)
        self.readings = Readings()
        self._wav = None
        self._wav_bytes = 0
        if request.wav is not None:
            self._wav = open(request.wav, "wb")          # noqa: SIM115
            self._wav.write(wav_header(self.demod.audio_hz, 0))

    def feed(self, iq: np.ndarray) -> None:
        r = self.readings
        m = self.demod.process(iq)
        r.signal.append(m.signal_dbfs)
        r.noise.append(m.noise_dbfs)
        r.snr.append(m.snr_db)
        if m.carrier_hz:
            r.offsets.append(m.carrier_hz)
        r.samples += iq.size
        r.clipped += int(np.count_nonzero((np.abs(iq.real) >= _CLIP_LEVEL)
                                          | (np.abs(iq.imag) >= _CLIP_LEVEL)))
        if self.squelch.update(m.snr_db):
            r.open_blocks += 1
        gated = self.squelch.apply(m.audio)
        if self.sink is not None:
            try:
                self.sink.write(pcm16(gated * self.gain))
            except AudioError as e:
                self.out(f"  audio stopped: {e}")
                self.sink = None
        if self._wav is not None:
            pcm = pcm16(gated)
            self._wav.write(pcm)
            self._wav_bytes += len(pcm)

    def status_line(self, elapsed: float) -> str:
        r = self.readings
        recent = max(1, FS_HZ * len(r.signal) // max(1, r.samples))     # one second's blocks
        snr = float(np.mean(r.snr[-recent:]))
        bar = "#" * min(20, int(snr / 2.0))
        gate = " open" if self.squelch.open and not self.squelch.forced else ""
        return (f"  {elapsed:4.0f} s  signal {np.mean(r.signal[-recent:]):6.1f} dBFS  "
                f"noise {np.mean(r.noise[-recent:]):6.1f} dBFS  S/N {snr:5.1f} dB  "
                f"[{bar:<20}]{gate}")

    def _report_sense(self, sense: str, swapped: bool) -> None:
        if sense == "normal":
            self.out("  sense       normal: the band moved the right way when retuned")
        elif sense == "inverted":
            self.out("  ! the band moved the wrong way when retuned: this dongle looks inverted.")
            self.out('    Remove "swap_iq" from its tuner profile.' if swapped else
                     '    Set "swap_iq": true in its tuner profile.')
        else:
            self.out("  sense       not measured: nothing steady on air to compare")

    def report(self, sense: str | None = None, swapped: bool = False) -> int:
        """Print the verdict. `sense` is None on a replay, which cannot retune."""
        r = self.readings
        if self._wav is not None:
            path = Path(self._wav.name)
            self._wav.close()
            self._wav = None
            repair_wav(path, self.demod.audio_hz)
            self.out(f"  audio saved to {path}")
        if not r.snr:
            self.out("  no samples were received")
            return EXIT_HARDWARE

        snr = np.asarray(r.snr)
        # Airband traffic comes and goes, so its figure of merit is the loudest moment;
        # a broadcast station is always there, so the typical one.
        am = self.request.mode == "am"
        figure = float(np.percentile(snr, 95) if am else np.median(snr))
        clip = r.clipped / max(1, r.samples * 2)
        self.out("")
        self.out(f"  signal      {np.median(r.signal):.1f} dBFS (peak {np.max(r.signal):.1f})")
        self.out(f"  noise       {np.median(r.noise):.1f} dBFS in the channel")
        self.out(f"  S/N         {figure:.1f} dB ({'peak' if am else 'median'})")
        self.out(f"  ADC clip    {clip * 100:.3f} % of samples")
        if am and r.offsets:
            hz = float(np.median(r.offsets))
            ppm = -hz / (self.freq_hz / 1e6)
            self.out(f"  carrier     {hz:+.0f} Hz from the channel: add about {ppm:+.1f} "
                     "to sdr.ppm, if this station is on frequency")
        if self.squelch.threshold_db > 0:
            self.out(f"  squelch     open {r.open_blocks * 100 / len(snr):.0f} % of the time")
        if clip >= _CLIP_WARN:
            self.out("  ! the ADC is clipping: lower the gain with --gain")

        if sense is not None:
            self._report_sense(sense, swapped)
        if figure >= _GOOD_SNR_DB:
            self.out("  RESULT: GOOD signal")
            return EXIT_OK
        if figure >= _USABLE_SNR_DB:
            self.out("  RESULT: WEAK signal, usable")
            return EXIT_OK
        self.out("  RESULT: NO usable signal")
        self.out("    check the antenna connection and the coax, and that this is the right")
        self.out("    dongle (`devices`). An FM band-stop filter in line blocks test-fm by")
        self.out("    design." if not am else
                 "    design. On airband, choose a channel that is on air now, such as ATIS.")
        return EXIT_NO_SIGNAL


def _read_capture(path: Path, block_samples: int):
    """Blocks from a cu8 file, as `rtl_sdr -s 1024000 -f <channel - 256 kHz>` writes."""
    with open(path, "rb") as f:
        while len(raw := f.read(block_samples * 2)) == block_samples * 2:
            yield to_complex64(np.frombuffer(raw, dtype=np.uint8))


def _power_db(blocks: list[np.ndarray]) -> np.ndarray:
    """Averaged spectrum in dB, lowest frequency first."""
    iq = np.concatenate(blocks)
    frames = iq[:iq.size // _SENSE_FFT * _SENSE_FFT].reshape(-1, _SENSE_FFT)
    power = np.abs(np.fft.fft(frames * np.hanning(_SENSE_FFT), axis=1)) ** 2
    return 10.0 * np.log10(np.fft.fftshift(np.mean(power, axis=0)) + 1e-12)


def sense_from_retune(at_channel_db: np.ndarray, stepped_db: np.ndarray) -> str:
    """`"normal"` or `"inverted"`, from the band at two tunings, or `""` if it cannot tell.

    A station stays where it is, so with the tuner `_SENSE_STEP_HZ` higher it sits that
    much lower in a normal device's band and that much higher in an inverted one's.
    What the dongle makes itself, DC, spurs and the shape of its filter, stays in the
    same bins and correlates equally in both directions, so only a signal that really
    moved can tell them apart.
    """
    bins = round(_SENSE_STEP_HZ * _SENSE_FFT / FS_HZ)
    down = float(np.corrcoef(at_channel_db[bins:], stepped_db[:-bins])[0, 1])
    up = float(np.corrcoef(at_channel_db[:-bins], stepped_db[bins:])[0, 1])
    if max(down, up) < _SENSE_MIN_CORRELATION or abs(down - up) < _SENSE_MIN_CORRELATION:
        return ""
    return "normal" if down > up else "inverted"


def _band_at(reader: SdrReader, freq_hz: int) -> np.ndarray | None:
    reader.set_frequency(freq_hz)
    blocks: list[np.ndarray] = []
    start = time.monotonic()
    while len(blocks) < _SENSE_BLOCKS and (now := time.monotonic()) - start < _SENSE_TIMEOUT_S:
        for block, _gap in reader.blocks():
            if now - start >= _SETTLE_S:
                blocks.append(block)
        time.sleep(0.005)
    return _power_db(blocks) if blocks else None


def _measure_sense(reader: SdrReader, freq_hz: int) -> str:
    """Retune and watch the band move. Ends back on the channel."""
    stepped = _band_at(reader, freq_hz + _SENSE_STEP_HZ)
    at_channel = _band_at(reader, freq_hz)
    if stepped is None or at_channel is None:
        return ""
    return sense_from_retune(at_channel, stepped)


def run_test(cfg, request: BenchRequest, opener=RtlDevice,
             out: Callable[[str], None] = print) -> int:
    """Run one antenna test. Returns the process exit code."""
    name = "AM" if request.mode == "am" else "FM"
    out(f"  {name} test on {request.mhz:.3f} MHz, {request.seconds:g} s. Ctrl-C ends it early.")
    if request.replay is not None:
        bench = Bench(request, cfg, None, out)
        for block in _read_capture(request.replay, cfg.sdr["block_samples"]):
            bench.feed(block)
        return bench.report()

    sink = None
    if request.audio and request.volume > 0:
        sink = AlsaSink(cfg.audio, AmDemod.audio_hz if request.mode == "am" else FmDemod.audio_hz)
    bench = Bench(request, cfg, sink, out)
    reader = SdrReader(cfg.sdr, opener)
    try:
        reader.set_frequency(bench.freq_hz)
    except SdrError as e:
        out(f"  cannot use the SDR: {e}")
        out("  If the daemon is running it holds the dongle: "
            "sudo systemctl stop rb-sdr-radio")
        return EXIT_HARDWARE
    out(f"  SDR {getattr(reader.device, 'name', 'open')}")
    try:
        if sink is not None:
            try:
                sink.open()
            except AudioError as e:
                out(f"  no audio: {e}")
                bench.sink = None
        start = time.monotonic()
        last_line = start
        sense = ""
        with contextlib.suppress(KeyboardInterrupt):
            while (now := time.monotonic()) - start < request.seconds:
                got = False
                for block, _gap in reader.blocks():
                    got = True
                    if now - start >= _SETTLE_S:
                        bench.feed(block)
                if now - last_line >= 1.0 and bench.readings.signal:
                    out(bench.status_line(now - start))
                    last_line = now
                if not got:
                    time.sleep(0.005)
            with contextlib.suppress(SdrError):
                sense = _measure_sense(reader, bench.freq_hz)
        if reader.overruns:
            out(f"  ! {reader.overruns} block(s) lost: this machine could not keep up")
        return bench.report(sense, reader.swap_iq)
    finally:
        reader.close()
        if sink is not None:
            sink.close()
