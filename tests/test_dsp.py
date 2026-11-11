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
    CarrierLock,
    DecimatingFir,
    FmDemod,
    Fs4Mixer,
    NoiseReducer,
    SpectrumMeter,
    lowpass,
    pcm16,
    soft_limit,
    to_complex64,
)
from rbsdrradio.freqrules import SPACING_25, SPACING_833

SYNC = "synchronous"


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


def in_channel_noise(seconds, dbfs, seed=7):
    """Noise only within 12 kHz of the channel: the tuner's shape, hash, a neighbour's
    splatter. Raises the channel's noise and not the bins the floor is taken from."""
    taps = sps.firwin(255, 12_000.0, fs=FS_HZ)
    t = np.arange(round(seconds * FS_HZ)) / FS_HZ
    shift = np.exp(2j * np.pi * synth.CHANNEL_AT_HZ * t)
    return sps.lfilter(taps, 1.0, synth.noise(seconds, dbfs, seed) / shift) * shift


def test_noise_raised_inside_the_channel_alone_still_reads_zero():
    """What held a 3 dB squelch open on the first flight tests: the channel power read
    2 to 3 dB over the floor beside it with no station there. A carrier reading does not
    see noise, however it is spread."""
    iq = synth.noise(1.0, -50.0) + in_channel_noise(1.0, -47.0)
    assert max(r.snr_db for r in run(AmDemod(), iq)[2:]) < 0.5


def test_the_neighbouring_833_carrier_is_not_this_channel():
    iq = synth.noise(1.0, -50.0) + synth.carrier(1.0, -30.0, offset_hz=8333.0)
    assert max(r.snr_db for r in run(AmDemod(SPACING_833), iq)[2:]) < 0.5


@pytest.mark.parametrize("spacing, offset_hz", [(SPACING_25, 6_000.0), (SPACING_833, 2_500.0)])
def test_an_off_frequency_carrier_reads_its_level(spacing, offset_hz):
    demod = AmDemod(spacing)
    enbw = float(np.sum(np.square(demod._channel.taps, dtype=np.float64))) * IF_HZ
    level = synth.channel_noise_dbfs(-50.0, enbw) + 6.0
    iq = synth.noise(1.0, -50.0) + synth.carrier(1.0, level, offset_hz=offset_hz)
    assert mean_snr(run(demod, iq)) == pytest.approx(6.0, abs=0.5)


def test_modulation_does_not_move_the_reading():
    """The squelch follows the carrier, not the words: it must not dip between them."""
    def snr(depth):
        iq = synth.noise(1.0, -50.0) + synth.am(1.0, -50.0, depth=depth, tone_hz=700.0)
        return mean_snr(run(AmDemod(), iq))
    assert snr(0.9) == pytest.approx(snr(0.0), abs=0.5)


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


def test_hiss_above_the_voice_band_is_removed():
    """Hangar recordings, 2026-11-08: the old filter let hiss through to 6 kHz."""
    def level(tone):
        results = run(AmDemod(), synth.am(1.0, -30.0, tone_hz=tone) + synth.noise(1.0, -70.0))
        return tone_level(audio_of(results), AM_AUDIO_HZ, tone)[0]
    assert level(3200.0) < 0.01 * level(1000.0)


def test_soft_limit_leaves_the_linear_range_alone_and_never_passes_full_scale():
    x = np.linspace(-10.0, 10.0, 20001)
    y = soft_limit(x)
    inside = np.abs(x) <= 0.8
    assert np.array_equal(y[inside], x[inside])
    assert np.max(np.abs(y)) <= 1.0
    assert np.all(np.diff(y) >= 0)                       # no fold-over
    assert np.max(np.abs(np.diff(y))) <= np.max(np.diff(x)) * 1.0001  # slope never above 1
    usual = np.abs(x) <= 2.0                             # where real peaks land
    assert np.all(np.diff(y[usual]) > 0) and np.max(np.abs(y[usual])) < 1.0


@pytest.mark.parametrize("step_db", [12, 20, 30])
def test_a_carrier_that_jumps_up_does_not_click(step_db):
    """A strong station keying over a weak one read many times full modulation until the
    carrier average caught up, and played as a full-scale click (130.000, 2026-11-08)."""
    c = synth.am(1.0, -50.0, depth=0.3)
    step = np.ones(c.size)
    step[c.size // 2:] = 10 ** (step_db / 20)
    audio = audio_of(run(AmDemod(), c * step + synth.noise(1.0, -75.0, seed=3)), skip=0)
    mid = audio.size // 2
    assert np.max(np.abs(audio[mid:mid + 1600])) < 0.8


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


def test_unknown_detector_is_refused():
    with pytest.raises(ValueError, match="detector"):
        AmDemod(detector="coherent")


# -- synchronous detector ------------------------------------------------------- #

def tone_snr_db(audio, tone_hz=1000.0):
    power = np.abs(np.fft.rfft(audio * np.hanning(audio.size))) ** 2
    k = int(round(tone_hz * audio.size / AM_AUDIO_HZ))
    tone = float(np.sum(power[k - 3:k + 4]))
    return 10.0 * np.log10(tone / (float(np.sum(power)) - tone))


def test_synchronous_detector_recovers_the_tone_at_the_same_loudness():
    results = run(AmDemod(detector=SYNC), synth.am(1.0, -30.0, tone_hz=1000.0, depth=0.8)
                  + synth.noise(1.0, -60.0))
    rms, purity = tone_level(audio_of(results), AM_AUDIO_HZ, 1000.0)
    assert purity > 30.0
    assert rms == pytest.approx(0.8 * 0.8 / np.sqrt(2.0), rel=0.1)


@pytest.mark.parametrize("spacing,offset_hz", [
    (SPACING_25, 2500.0), (SPACING_25, -5000.0), (SPACING_833, 1000.0), (SPACING_833, -300.0)])
def test_synchronous_detector_follows_an_off_frequency_carrier(spacing, offset_hz):
    demod = AmDemod(spacing, detector=SYNC)
    results = run(demod, synth.am(1.0, -30.0, offset_hz=offset_hz) + synth.noise(1.0, -60.0))
    assert tone_level(audio_of(results), AM_AUDIO_HZ, 1000.0)[1] > 25.0
    assert demod._lock.freq_hz == pytest.approx(offset_hz, abs=5.0)


def test_synchronous_detector_does_better_on_the_weakest_signals():
    """No threshold: at 0 dB of S/N in a 25 kHz channel it measured 1.7 dB better."""
    enbw = float(np.sum(np.square(AmDemod()._channel.taps, dtype=np.float64))) * IF_HZ
    level = synth.channel_noise_dbfs(-50.0, enbw)
    iq = synth.am(2.0, level, depth=0.5) + synth.noise(2.0, -50.0, seed=2)
    envelope = tone_snr_db(audio_of(run(AmDemod(), iq), skip=4))
    synchronous = tone_snr_db(audio_of(run(AmDemod(detector=SYNC), iq), skip=4))
    assert synchronous > envelope + 1.0


def test_synchronous_detector_reads_the_same_snr():
    """The squelch must not care which detector is chosen."""
    iq = synth.am(1.0, -40.0) + synth.noise(1.0, -50.0)
    assert mean_snr(run(AmDemod(detector=SYNC), iq)) == pytest.approx(
        mean_snr(run(AmDemod(), iq)), abs=0.01)


def tones(seconds, *pairs):
    t = np.arange(int(seconds * AM_AUDIO_HZ)) / AM_AUDIO_HZ
    return sum(a * np.exp(2j * np.pi * f * t) for f, a in pairs)


def test_carrier_lock_finds_the_frequency_to_a_few_hertz():
    lock = CarrierLock(AM_AUDIO_HZ, search_hz=7000.0)
    lock.process(tones(0.032, (1234.5, 0.01)) + 1e-5)
    assert lock.locked and lock.freq_hz == pytest.approx(1234.5, abs=3.0)


def test_carrier_lock_keeps_its_carrier_against_one_up_to_6_db_stronger():
    """An offset-carrier ground station puts two in the channel. Hopping between them at
    every block would garble both."""
    lock = CarrierLock(AM_AUDIO_HZ, search_hz=7000.0)
    lock.process(tones(0.032, (0.0, 1.0), (5000.0, 0.5)))
    assert lock.freq_hz == pytest.approx(0.0, abs=3.0)
    lock.process(tones(0.032, (0.0, 1.0), (5000.0, 1.8)))               # +5 dB
    assert lock.freq_hz == pytest.approx(0.0, abs=3.0)
    lock.process(tones(0.032, (0.0, 1.0), (5000.0, 3.0)))               # +9.5 dB
    assert lock.freq_hz == pytest.approx(5000.0, abs=3.0)


def test_carrier_lock_keeps_the_frequency_when_the_carrier_goes():
    lock = CarrierLock(AM_AUDIO_HZ, search_hz=7000.0)
    lock.process(tones(0.032, (800.0, 0.01)))
    rng = np.random.default_rng(4)
    lock.process(1e-3 * (rng.standard_normal(512) + 1j * rng.standard_normal(512)))
    assert not lock.locked and lock.freq_hz == pytest.approx(800.0, abs=3.0)


def test_carrier_lock_ignores_a_carrier_outside_the_channel():
    lock = CarrierLock(AM_AUDIO_HZ, search_hz=3200.0)
    rng = np.random.default_rng(5)
    noise = 1e-3 * (rng.standard_normal(512) + 1j * rng.standard_normal(512))
    lock.process(tones(0.032, (5000.0, 1.0)) + noise)
    assert not lock.locked


# -- noise reduction ------------------------------------------------------------ #

@pytest.mark.parametrize("chunks", [[512] * 8, [1000, 7, 3089], [127] * 32 + [32], [4096]])
def test_noise_reducer_output_does_not_depend_on_block_boundaries(chunks):
    rng = np.random.default_rng(6)
    x = rng.standard_normal(4096)
    whole = NoiseReducer(12.0)
    noise = np.full(NoiseReducer.NFFT // 2 + 1, whole.white_gain)
    want = whole.process(x, noise)
    cut, parts, start = NoiseReducer(12.0), [], 0
    for size in chunks:
        parts.append(cut.process(x[start:start + size], noise))
        start += size
    assert all(p.size == size for p, size in zip(parts, chunks, strict=True))
    assert np.allclose(np.concatenate(parts), want)


def test_noise_reducer_with_no_noise_figure_is_a_pure_delay():
    rng = np.random.default_rng(7)
    x = rng.standard_normal(4096)
    out = NoiseReducer(12.0).process(x, None)
    n = NoiseReducer.NFFT
    assert np.allclose(out[:n], 0.0)
    assert np.allclose(out[n:], x[:-n])


@pytest.mark.parametrize("reduction_db", [6.0, 12.0])
def test_noise_reducer_brings_noise_down_by_its_setting(reduction_db):
    rng = np.random.default_rng(8)
    x = rng.standard_normal(32_000)
    nr = NoiseReducer(reduction_db)
    out = nr.process(x, np.full(NoiseReducer.NFFT // 2 + 1, nr.white_gain))
    got_db = 10.0 * np.log10(np.mean(x[4096:] ** 2) / np.mean(out[4096:] ** 2))
    assert reduction_db * 0.75 <= got_db <= reduction_db + 1.0


def test_noise_reduction_keeps_a_clear_tone():
    iq = synth.am(1.0, -30.0, tone_hz=1000.0, depth=0.8) + synth.noise(1.0, -60.0)
    plain = tone_level(audio_of(run(AmDemod(), iq)), AM_AUDIO_HZ, 1000.0)
    reduced = tone_level(audio_of(run(AmDemod(noise_reduction_db=12), iq)), AM_AUDIO_HZ, 1000.0)
    assert reduced[0] == pytest.approx(plain[0], rel=0.05)
    assert reduced[1] >= plain[1] - 1.0


@pytest.mark.parametrize("detector", ["envelope", SYNC])
def test_noise_reduction_quietens_the_hiss_on_a_weak_carrier(detector):
    """An unmodulated carrier 8 dB over the noise: what is heard is hiss alone."""
    enbw = float(np.sum(np.square(AmDemod()._channel.taps, dtype=np.float64))) * IF_HZ
    iq = synth.carrier(1.0, synth.channel_noise_dbfs(-50.0, enbw) + 8.0) + synth.noise(1.0, -50.0)
    plain = audio_of(run(AmDemod(detector=detector), iq), skip=6)
    reduced = audio_of(run(AmDemod(detector=detector, noise_reduction_db=12), iq), skip=6)
    assert 10.0 * np.log10(np.mean(plain ** 2) / np.mean(reduced ** 2)) > 8.0


@pytest.mark.parametrize("detector,cnr_db,within_db", [
    ("envelope", 10.0, 1.0), ("envelope", 3.0, 1.5), (SYNC, 10.0, 0.5), (SYNC, 0.0, 1.0)])
def test_noise_reduction_knows_the_noise_it_will_meet(detector, cnr_db, within_db):
    """The noise figure comes from the meter beside the channel, not from the audio, so it
    must agree with what the detector actually delivers."""
    demod = AmDemod(SPACING_25, detector=detector, noise_reduction_db=1e-6)
    seen = []
    process = demod._nr.process

    def spy(x, noise):
        if noise is not None:
            seen.append((x.copy(), noise))
        return process(x, noise)

    demod._nr.process = spy
    enbw = float(np.sum(np.square(demod._channel.taps, dtype=np.float64))) * IF_HZ
    iq = (synth.carrier(2.0, synth.channel_noise_dbfs(-40.0, enbw) + cnr_db, offset_hz=700.0)
          + synth.noise(2.0, -40.0, seed=5))
    run(demod, iq)
    audio = np.concatenate([x for x, _ in seen[4:]])
    model = np.mean([noise for _, noise in seen[4:]], axis=0)
    window = np.sqrt(np.hanning(NoiseReducer.NFFT + 1)[:-1])
    frames = np.lib.stride_tricks.sliding_window_view(audio, NoiseReducer.NFFT)[::128]
    measured = np.mean(np.abs(np.fft.rfft(frames * window, axis=1)) ** 2, axis=0)
    f = np.fft.rfftfreq(NoiseReducer.NFFT, 1.0 / AM_AUDIO_HZ)
    band = (f > 500.0) & (f < 2500.0)
    error_db = 10.0 * np.log10(np.mean(model[band]) / np.mean(measured[band]))
    assert abs(error_db) < within_db


def test_noise_reduction_keeps_every_block_the_same_length():
    """The recorder and the dual watch count audio samples as time."""
    demod = AmDemod(noise_reduction_db=12)
    iq = (synth.am(0.2, -30.0) + synth.noise(0.2, -60.0)).astype(np.complex64)
    for size in (32_768, 8192, 1024, 32_768):
        assert demod.process(iq[:size]).audio.size == size // (FS_HZ // AM_AUDIO_HZ)


def test_reset_clears_the_noise_reducer_and_the_lock():
    demod = AmDemod(detector=SYNC, noise_reduction_db=12)
    run(demod, synth.am(0.5, -20.0, offset_hz=2000.0) + synth.noise(0.5, -60.0))
    demod.reset()
    assert not demod._lock.locked and demod._lock.freq_hz == 0.0
    assert not np.any(demod._nr._output) and demod._nr._clean is None


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


# -- dual watch: one short look ---------------------------------------------------- #

DWELL, FILL = 16_384, 3_072


def test_a_dwell_sees_a_carrier():
    iq = (synth.am(0.05, -30.0) + synth.noise(0.05, -60.0)).astype(np.complex64)
    assert AmDemod(meter_nfft=2048).dwell(iq[:DWELL], FILL) > 20.0


def test_a_dwell_on_an_empty_channel_reads_nothing():
    demod = AmDemod(meter_nfft=2048)
    iq = synth.noise(0.05, -60.0, seed=3).astype(np.complex64)
    assert demod.dwell(iq[:DWELL], FILL) < 3.0


def test_each_dwell_starts_clean():
    """The channel before must not leak into the next reading: it is a different one."""
    demod = AmDemod(meter_nfft=2048)
    loud = (synth.am(0.05, -20.0) + synth.noise(0.05, -60.0)).astype(np.complex64)
    quiet = synth.noise(0.05, -60.0, seed=3).astype(np.complex64)
    demod.dwell(loud[:DWELL], FILL)
    assert demod.dwell(quiet[:DWELL], FILL) < 3.0


def test_a_short_read_is_only_a_quiet_look():
    demod = AmDemod(meter_nfft=2048)
    iq = (synth.am(0.05, -30.0) + synth.noise(0.05, -60.0)).astype(np.complex64)
    assert demod.dwell(iq[:DWELL // 2], FILL) == 0.0


def test_resume_keeps_the_noise_floor_and_drops_the_samples():
    demod = AmDemod()
    iq = (synth.am(0.1, -30.0) + synth.noise(0.1, -60.0)).astype(np.complex64)
    demod.process(iq[:32_768])
    sigma2 = demod._meter.sigma2
    assert sigma2 is not None
    demod.resume()
    assert demod._meter.sigma2 == sigma2
    assert not demod._filled
