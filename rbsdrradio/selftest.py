"""`self-test`: the whole receive chain on synthetic signals. No hardware needed.

Run it first on a new install. If this passes and a live test hears nothing, the
fault is in the antenna, the dongle or the tuning, not in this software.
"""

from __future__ import annotations

import shutil
import tempfile
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

from rbsdrradio import sdr, synth
from rbsdrradio.config import load
from rbsdrradio.dsp import AM_AUDIO_HZ, FM_AUDIO_HZ, AmDemod, FmDemod, pcm16, to_complex64
from rbsdrradio.freqrules import SPACING_25, SPACING_833, parse_channel
from rbsdrradio.recorder import Recorder
from rbsdrradio.squelch import Squelch, SquelchParams

__all__ = ["run_self_test"]

_BLOCK = 32768
_NOISE_DBFS = -45.0


def _blocks(iq: np.ndarray):
    """Through the 8-bit ADC model, so quantisation is part of what is tested."""
    raw = synth.quantize(iq)
    for i in range(0, raw.size - 2 * _BLOCK + 1, 2 * _BLOCK):
        yield to_complex64(raw[i:i + 2 * _BLOCK])


def _tone_snr_db(audio: np.ndarray, fs: int, tone_hz: float) -> float:
    power = np.abs(np.fft.rfft(audio * np.hanning(audio.size))) ** 2
    k = int(round(tone_hz * audio.size / fs))
    tone = float(np.sum(power[k - 3:k + 4]))
    return 10.0 * float(np.log10(tone / max(float(np.sum(power)) - tone, 1e-20)))


def _check_am() -> tuple[bool, str]:
    demod = AmDemod(SPACING_25)
    results = [demod.process(b) for b in _blocks(synth.am(1.0, -20.0) + synth.noise(1.0, -45.0))]
    audio = np.concatenate([r.audio for r in results[4:]])
    snr = _tone_snr_db(audio, AM_AUDIO_HZ, 1000.0)
    return snr > 25.0, f"1 kHz tone recovered at {snr:.0f} dB"


def _check_squelch() -> tuple[bool, str]:
    params = SquelchParams(hysteresis_db=3.0, open_blocks=2, hang_s=0.1)
    demod = AmDemod(SPACING_25)
    squelch = Squelch(params, 10.0, _BLOCK / synth.FS_HZ)
    opened_on_noise = any(squelch.update(demod.process(b).snr_db)
                          for b in _blocks(synth.noise(1.0, _NOISE_DBFS)))
    opened = any(squelch.update(demod.process(b).snr_db)
                 for b in _blocks(synth.am(1.0, -20.0) + synth.noise(1.0, _NOISE_DBFS)))
    return opened and not opened_on_noise, "shut on noise, open on a carrier"


def _check_adjacent() -> tuple[bool, str]:
    demod = AmDemod(SPACING_833)
    neighbour = synth.carrier(1.0, -20.0, offset_hz=25_000 / 3) + synth.noise(1.0, _NOISE_DBFS)
    snr = max(demod.process(b).snr_db for b in list(_blocks(neighbour))[2:])
    return snr < 3.0, f"8.33 kHz neighbour at -20 dBFS reads {snr:.1f} dB"


def _check_fm() -> tuple[bool, str]:
    demod = FmDemod()
    results = [demod.process(b) for b in _blocks(synth.fm(1.0, -10.0) + synth.noise(1.0, -40.0))]
    audio = np.concatenate([r.audio for r in results[4:]])
    snr = _tone_snr_db(audio, FM_AUDIO_HZ, 1000.0)
    return snr > 30.0, f"1 kHz tone recovered at {snr:.0f} dB"


def _check_recorder() -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = {"dir": tmp, "prefix": "radio", "min_segment_s": 0.1, "max_segment_s": 10,
               "hold_s": 0.0, "max_total_mb": 1, "min_free_mb": 0}
        recorder = Recorder(cfg, AM_AUDIO_HZ)
        recorder.start()
        pcm = pcm16(np.zeros(AM_AUDIO_HZ, dtype=np.float32))
        recorder.feed(pcm, True, 118_700)
        recorder.close()
        files = list(Path(tmp).glob("radio_*_118700000.wav"))
        if len(files) != 1:
            return False, f"expected one file, found {len(files)}"
        with wave.open(str(files[0])) as w:
            ok = (w.getnframes(), w.getframerate()) == (AM_AUDIO_HZ, AM_AUDIO_HZ)
    return ok, "wrote a valid WAV"


def _check_channels() -> tuple[bool, str]:
    channel, _ = parse_channel(118.030)
    ok = channel is not None and channel.freq_hz == 118_025_000
    return ok, "118.030 tunes 118.0250 MHz (8.33 kHz)"


def _check_config() -> tuple[bool, str]:
    return True, f"valid: {load().source}"


def _note_librtlsdr() -> str:
    try:
        return f"librtlsdr loaded, {len(sdr.list_devices())} dongle(s) present"
    except sdr.SdrError as e:
        return str(e)


def run_self_test(out: Callable[[str], None] = print) -> int:
    checks = (("configuration", _check_config), ("channel plan", _check_channels),
              ("AM demodulator", _check_am), ("squelch", _check_squelch),
              ("adjacent channel", _check_adjacent), ("FM demodulator", _check_fm),
              ("recorder", _check_recorder))
    failed = 0
    for name, check in checks:
        try:
            ok, detail = check()
        except Exception as e:                       # noqa: BLE001
            ok, detail = False, repr(e)
        failed += not ok
        out(f"  {'ok  ' if ok else 'FAIL'}  {name:18s} {detail}")
    # Environment, not software: reported, never failed, so this works on a CI box.
    out(f"  note  {'SDR':18s} {_note_librtlsdr()}")
    player = shutil.which("aplay")
    out(f"  note  {'audio':18s} {player or 'aplay not found: sudo apt install alsa-utils'}")
    out("  self-test " + ("PASSED" if not failed else f"FAILED ({failed})"))
    return 1 if failed else 0
