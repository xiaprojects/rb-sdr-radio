"""The receive chain, driven by synthetic signals of known level."""

import numpy as np
import pytest
from conftest import BLOCK, blocks_of, tone_level
from scipy import signal as sps

from rbsdrradio import synth
from rbsdrradio.dsp import (
    AM_AUDIO_HZ,
    FM_AUDIO_HZ,
    FS_HZ,
    IF_HZ,
    TUNER_OFFSET_HZ,
    AmDemod,
    DecimatingFir,
    FmDemod,
    Fs4Mixer,
    SpectrumMeter,
    lowpass,
    pcm16,
    to_complex64,
)
from rbsdrradio.freqrules import SPACING_25, SPACING_833


def run(demod, iq):
    return [demod.process(b) for b in blocks_of(iq)]


def audio_of(results, skip=3):
    return np.concatenate([r.audio for r in results[skip:]])


def mean_snr(results, skip=3):
    return float(np.mean([r.snr_db for r in results[skip:]]))


# -- sample plan -------------------------------------------------------------- #

def test_rates_are_exact_ratios():
    assert FS_HZ / IF_HZ == 8 and IF_HZ / AM_AUDIO_HZ == 8
    assert FS_HZ % FM_AUDIO_HZ == 0
    assert TUNER_OFFSET_HZ == -FS_HZ / 4


def test_one_block_is_a_whole_number_of_audio_samples():
    assert BLOCK % (FS_HZ // AM_AUDIO_HZ) == 0
    assert BLOCK % (FS_HZ // FM_AUDIO_HZ) == 0


# -- building blocks ---------------------------------------------------------- #

def test_mixer_moves_fs4_to_dc_across_block_boundaries():
    n = np.arange(4096)
    tone = np.exp(1j * np.pi * n / 2.0).astype(np.complex64)
    mixer = Fs4Mixer()
    out = np.concatenate([mixer.process(tone[:1001]), mixer.process(tone[1001:2002]),
                          mixer.process(tone[2002:])])
    assert np.allclose(out, 1.0, atol=1e-5)


@pytest.mark.parametrize("chunks", [[32768], [1000, 7, 31761], [4096] * 8, [3] * 1000 + [29768]])
def test_decimator_output_does_not_depend_on_block_boundaries(chunks):
    rng = np.random.default_rng(1)
    x = (rng.standard_normal(32768) + 1j * rng.standard_normal(32768)).astype(np.complex64)
    taps = lowpass(FS_HZ, 40_000.0, 88_000.0)
    whole = DecimatingFir(taps, 8).process(x)
    fir = DecimatingFir(taps, 8)
    parts, start = [], 0
    for size in chunks:
        parts.append(fir.process(x[start:start + size]))
        start += size
    assert np.allclose(np.concatenate(parts), whole, atol=1e-4)


def test_decimator_matches_a_plain_convolution():
    rng = np.random.default_rng(2)
    x = rng.standard_normal(8192).astype(np.complex64)
    taps = lowpass(FS_HZ, 40_000.0, 88_000.0)
    direct = sps.lfilter(taps, [1.0], x)[::8]
    assert np.allclose(DecimatingFir(taps, 8).process(x), direct, atol=1e-3)


def test_cu8_is_centred_not_offset():
    """rb-vor A4: librtlsdr is offset binary around 127.5."""
    raw = np.full(1024, 127, dtype=np.uint8)
    raw[::2] = 128
    iq = to_complex64(raw)
    assert abs(iq.real.mean()) < 0.01 and abs(iq.imag.mean()) < 0.01


def test_pcm16_clips_rather_than_wraps():
    samples = np.frombuffer(pcm16(np.array([0.0, 2.0, -2.0], dtype=np.float32)), "<i2")
    assert list(samples) == [0, 32767, -32767]


# -- level measurement -------------------------------------------------------- #

@pytest.mark.parametrize("spacing", [SPACING_25, SPACING_833])
@pytest.mark.parametrize("want_db", [6, 10, 20, 30])
def test_snr_reads_true(spacing, want_db):
    """The expected figure comes from the noise density, not from the receiver."""
    demod = AmDemod(spacing)
    noise_dbfs = -50.0
    enbw = float(np.sum(np.square(demod._channel.taps, dtype=np.float64))) * IF_HZ
    level = synth.channel_noise_dbfs(noise_dbfs, enbw) + want_db
    results = run(demod, synth.noise(1.0, noise_dbfs) + synth.carrier(1.0, level))
    assert mean_snr(results) == pytest.approx(want_db, abs=0.5)


@pytest.mark.parametrize("gain_db", [-20, 0, 20])
def test_snr_does_not_depend_on_gain(gain_db):
    """The point of a relative squelch: a retuned gain must not move it."""
    scale = 10 ** (gain_db / 20)
    iq = (synth.noise(1.0, -55.0) + synth.carrier(1.0, -55.0)) * scale
    assert mean_snr(run(AmDemod(), iq)) == pytest.approx(mean_snr(run(AmDemod(), iq / scale)),
                                                         abs=0.5)


def test_noise_alone_reads_zero():
    assert mean_snr(run(AmDemod(), synth.noise(1.0, -50.0))) < 0.5


def test_noise_floor_ignores_a_carrier_on_the_channel():
    """A strong carrier from the first block on must not raise the floor (ATIS)."""
    quiet = run(AmDemod(), synth.noise(1.0, -50.0))
    loud = run(AmDemod(), synth.noise(1.0, -50.0) + synth.am(1.0, -20.0))
    assert loud[-1].noise_dbfs == pytest.approx(quiet[-1].noise_dbfs, abs=0.5)


def test_noise_floor_ignores_every_neighbouring_channel_being_busy():
    """Median of a periodogram: carriers fill a few bins out of thousands."""
    iq = synth.noise(1.0, -50.0)
    for offset in (-25_000, 25_000, -50_000, 50_000, -8333, 8333, 16667):
        iq = iq + synth.am(1.0, -25.0, offset_hz=offset, tone_hz=700.0)
    quiet = run(AmDemod(SPACING_833), synth.noise(1.0, -50.0))
    busy = run(AmDemod(SPACING_833), iq)
    assert busy[-1].noise_dbfs == pytest.approx(quiet[-1].noise_dbfs, abs=1.0)


def test_spectrum_meter_accumulates_short_blocks():
    meter = SpectrumMeter(IF_HZ, nfft=4096, hop=0, inner_hz=9000.0, outer_hz=38000.0)
    noise = synth.noise(0.1, -40.0)[:8192].astype(np.complex64)
    for i in range(0, 8192, 512):
        meter.push(noise[i:i + 512])
    assert meter.sigma2 == pytest.approx(10 ** (-40 / 10), rel=0.2)


def test_first_block_after_reset_measures_nothing():
    """Found by the self-test: the filters start from silence, so a strong neighbour
    arrives as a step and its splatter read 6 dB of signal on every retune."""
    demod = AmDemod(SPACING_833)
    iq = synth.carrier(0.2, -20.0, offset_hz=25_000 / 3) + synth.noise(0.2, -50.0)
    results = run(demod, iq)
    assert max(r.snr_db for r in results) < 1.0


# -- AM ------------------------------------------------------------------------ #

def test_am_recovers_the_tone():
    results = run(AmDemod(), synth.am(1.0, -30.0, tone_hz=1000.0, depth=0.8)
                  + synth.noise(1.0, -60.0))
    rms, purity = tone_level(audio_of(results), AM_AUDIO_HZ, 1000.0)
    assert purity > 30.0
    assert rms == pytest.approx(0.8 * 0.8 / np.sqrt(2.0), rel=0.1)   # depth x scale


@pytest.mark.parametrize("level_db", [-50.0, -30.0, -10.0])
def test_loudness_follows_modulation_not_distance(level_db):
    """Near and far stations must sound the same."""
    results = run(AmDemod(), synth.am(1.0, level_db) + synth.noise(1.0, -70.0))
    rms, _ = tone_level(audio_of(results), AM_AUDIO_HZ, 1000.0)
    assert rms == pytest.approx(0.8 * 0.8 / np.sqrt(2.0), rel=0.15)


def test_voice_band_is_kept_and_rumble_removed():
    def level(tone):
        results = run(AmDemod(), synth.am(1.0, -30.0, tone_hz=tone) + synth.noise(1.0, -70.0))
        return tone_level(audio_of(results), AM_AUDIO_HZ, tone)[0]
    assert level(100.0) < 0.25 * level(1000.0)
    assert level(2500.0) > 0.7 * level(1000.0)


@pytest.mark.parametrize("offset_hz", [-2500.0, 2500.0])
def test_off_frequency_carrier_still_demodulates(offset_hz):
    """An uncalibrated dongle: the envelope detector does not care."""
    results = run(AmDemod(SPACING_25), synth.am(1.0, -30.0, offset_hz=offset_hz)
                  + synth.noise(1.0, -60.0))
    assert tone_level(audio_of(results), AM_AUDIO_HZ, 1000.0)[1] > 25.0
    assert results[-1].carrier_hz == pytest.approx(offset_hz, abs=40.0)


@pytest.mark.parametrize("spacing,offset_hz", [
    (SPACING_833, 25_000 / 3), (SPACING_833, -25_000 / 3), (SPACING_833, 50_000 / 3),
    (SPACING_25, 25_000.0), (SPACING_25, -25_000.0), (SPACING_25, 50_000.0)])
def test_adjacent_channel_stays_out(spacing, offset_hz):
    """A loud neighbour is 30 dB over the wanted channel's squelch and must read zero."""
    results = run(AmDemod(spacing), synth.am(1.0, -20.0, offset_hz=offset_hz)
                  + synth.noise(1.0, -50.0))
    assert mean_snr(results) < 1.0


def test_mirror_image_of_the_channel_stays_out():
    """The DC spur and everything on the far side of the tuner are 512 kHz away."""
    t = np.arange(FS_HZ) / FS_HZ
    mirror = 0.1 * np.exp(-2j * np.pi * (-TUNER_OFFSET_HZ) * t)
    assert mean_snr(run(AmDemod(), mirror + synth.noise(1.0, -50.0))) < 1.0


def test_dc_spur_stays_out():
    """Why the tuner is offset at all: the dongle's own DC sits 256 kHz away."""
    iq = 0.05 + synth.noise(1.0, -50.0)
    assert mean_snr(run(AmDemod(), iq)) < 1.0


def test_zero_if_artefacts_leave_the_channel_reading_alone():
    """An FC0012 is zero-IF: it has a DC spur and an I/Q error that a low-IF R820T lacks."""
    def zero_if(iq):
        iq = iq + 0.1
        gain, phase = 10.0 ** (1.0 / 20.0), np.deg2rad(3.0)
        return iq.real + 1j * gain * (iq.imag * np.cos(phase) + iq.real * np.sin(phase))

    noise = synth.noise(1.0, -35.0)
    clean = synth.am(1.0, -30.0) + noise
    assert mean_snr(run(AmDemod(), zero_if(clean))) == pytest.approx(
        mean_snr(run(AmDemod(), clean)), abs=0.5)
    assert mean_snr(run(AmDemod(), zero_if(noise))) < 1.0


def test_survives_the_8_bit_adc():
    iq = synth.am(1.0, -20.0) + synth.noise(1.0, -45.0)
    raw = synth.quantize(iq)
    blocks = [to_complex64(raw[i:i + 2 * BLOCK]) for i in range(0, raw.size - 2 * BLOCK + 1,
                                                                2 * BLOCK)]
    demod = AmDemod()
    results = [demod.process(b) for b in blocks]
    assert tone_level(audio_of(results), AM_AUDIO_HZ, 1000.0)[1] > 25.0


def test_reset_forgets_the_previous_channel():
    demod = AmDemod()
    run(demod, synth.am(0.5, -20.0))
    demod.reset()
    results = run(demod, synth.noise(0.5, -50.0))
    assert mean_snr(results, skip=1) < 0.5


def test_empty_block_does_not_crash():
    demod = AmDemod()
    demod.process(np.empty(0, dtype=np.complex64))
    demod.process(synth.noise(0.05, -50.0).astype(np.complex64)[:BLOCK])


def test_set_spacing_changes_the_filter():
    narrow = AmDemod(SPACING_833)
    wide = AmDemod(SPACING_25)
    assert narrow._noise_gain < wide._noise_gain
    wide.set_spacing(SPACING_833)
    assert wide._noise_gain == narrow._noise_gain


# -- FM ------------------------------------------------------------------------ #

def test_fm_recovers_the_tone():
    results = run(FmDemod(), synth.fm(1.0, -10.0, tone_hz=1000.0, deviation_hz=50_000.0)
                  + synth.noise(1.0, -40.0))
    rms, purity = tone_level(audio_of(results), FM_AUDIO_HZ, 1000.0)
    assert purity > 40.0
    # 50 kHz of a 75 kHz scale, less the 50 us de-emphasis at 1 kHz (0.05 dB)
    assert rms == pytest.approx((50 / 75) / np.sqrt(2.0), rel=0.05)


def test_fm_deemphasis_rolls_off_treble():
    def level(tone):
        results = run(FmDemod(), synth.fm(1.0, -10.0, tone_hz=tone) + synth.noise(1.0, -50.0))
        return tone_level(audio_of(results), FM_AUDIO_HZ, tone)[0]
    corner = 1.0 / (2.0 * np.pi * 50e-6)                # 3.18 kHz
    want = 1.0 / np.sqrt(1.0 + (10_000 / corner) ** 2)
    assert level(10_000.0) / level(300.0) == pytest.approx(want, rel=0.1)


def test_fm_pilot_tone_is_filtered_out():
    t = np.arange(FS_HZ) / FS_HZ
    pilot = 7_500.0 / 19_000.0 * np.sin(2.0 * np.pi * 19_000.0 * t)
    iq = 0.3 * np.exp(1j * (2.0 * np.pi * 256_000.0 * t + pilot))
    audio = audio_of(run(FmDemod(), iq))
    assert np.max(np.abs(audio[256:])) < 0.01


def test_fm_snr_survives_a_crowded_band():
    """Stations either side: only a low quantile of the spectrum is still noise."""
    iq = synth.fm(1.0, -10.0) + synth.noise(1.0, -55.0)
    t = np.arange(FS_HZ) / FS_HZ
    for offset in (-300_000.0, -200_000.0, -100_000.0, 150_000.0, 250_000.0):
        iq = iq + 0.1 * np.exp(1j * (2.0 * np.pi * offset * t
                                     + 25.0 * np.sin(2.0 * np.pi * 2000.0 * t)))
    assert run(FmDemod(), iq)[-1].snr_db > 25.0
