"""Squelch, alone and in front of the real demodulator."""

import numpy as np
import pytest
from conftest import BLOCK, blocks_of

from rbsdrradio import synth
from rbsdrradio.dsp import FS_HZ, AmDemod
from rbsdrradio.squelch import Squelch, SquelchParams

PARAMS = SquelchParams(hysteresis_db=3.0, open_blocks=2, hang_s=0.3)
BLOCK_S = BLOCK / FS_HZ
HANG_BLOCKS = round(0.3 / BLOCK_S)


def make(threshold=10.0):
    return Squelch(PARAMS, threshold, BLOCK_S)


def test_starts_shut():
    assert make().open is False


def test_one_noise_spike_does_not_open_it():
    s = make()
    assert s.update(30.0) is False
    assert s.update(0.0) is False
    assert s.update(30.0) is False


def test_two_blocks_open_it():
    s = make()
    s.update(12.0)
    assert s.update(12.0) is True


def test_hangs_through_a_dropout():
    s = make()
    s.update(20.0)
    s.update(20.0)
    for _ in range(HANG_BLOCKS - 1):
        assert s.update(0.0) is True
    assert s.update(0.0) is False


def test_a_good_block_restarts_the_hang():
    s = make()
    s.update(20.0)
    s.update(20.0)
    for _ in range(3):
        for _ in range(HANG_BLOCKS - 1):
            assert s.update(0.0) is True
        assert s.update(20.0) is True


def test_hysteresis_holds_a_signal_sitting_on_the_threshold():
    s = make(10.0)
    s.update(11.0)
    s.update(11.0)
    for _ in range(HANG_BLOCKS * 3):
        assert s.update(8.0) is True          # below open, above close


def test_zero_holds_it_open():
    s = make(0.0)
    assert s.open is True
    assert s.update(0.0) is True


def test_raising_from_zero_lets_it_close():
    s = make(0.0)
    s.set_threshold(10.0)
    for _ in range(HANG_BLOCKS + 1):
        s.update(0.0)
    assert s.open is False


def test_lowering_to_zero_opens_at_once():
    s = make(10.0)
    s.set_threshold(0.0)
    assert s.open is True


def test_shut_gate_is_silent_and_open_gate_transparent():
    s = make(0.0)
    audio = np.ones(512, dtype=np.float32)
    s.apply(audio)
    assert np.array_equal(s.apply(audio), audio)
    closed = make(10.0)
    assert not closed.apply(audio).any()


def test_gate_ramps_rather_than_clicks():
    s = make(10.0)
    s.update(20.0)
    s.update(20.0)
    audio = np.ones(512, dtype=np.float32)
    ramp = s.apply(audio)
    assert ramp[0] == 0.0 and ramp[-1] == 1.0
    assert np.max(np.abs(np.diff(ramp))) < 0.01


def _through_chain(iq, threshold=10.0):
    demod = AmDemod()
    squelch = make(threshold)
    states = []
    for block in blocks_of(iq):
        states.append(squelch.update(demod.process(block).snr_db))
    return states


def test_stays_shut_on_noise():
    assert not any(_through_chain(synth.noise(3.0, -50.0)))


def test_opens_and_closes_around_a_transmission():
    quiet, tx = synth.noise(1.0, -50.0, seed=1), synth.noise(1.0, -50.0, seed=2)
    iq = np.concatenate([quiet, tx + synth.am(1.0, -30.0), synth.noise(1.5, -50.0, seed=3)])
    states = _through_chain(iq)
    per_second = FS_HZ // BLOCK
    assert not any(states[:per_second])
    assert all(states[per_second + 3:2 * per_second])
    assert not any(states[-per_second // 2:])


def test_atis_is_never_squelched_out():
    """ATIS is a carrier that never stops. Tuned in mid-broadcast it must open within
    a block or two and stay open: this is what a remembered noise floor gets wrong."""
    states = _through_chain(synth.am(8.0, -40.0) + synth.noise(8.0, -60.0))
    assert all(states[3:])


def test_threshold_is_in_real_db_of_snr():
    """A carrier 6 dB over the noise opens a 4 dB squelch but not a 10 dB one."""
    demod = AmDemod()
    enbw = demod._noise_gain * 128_000
    level = synth.channel_noise_dbfs(-50.0, enbw) + 6.0
    iq = synth.carrier(1.0, level) + synth.noise(1.0, -50.0)
    assert any(_through_chain(iq, threshold=4.0))
    assert not any(_through_chain(iq, threshold=10.0))


@pytest.mark.parametrize("gain_db", [-15, 15])
def test_same_threshold_after_a_gain_change(gain_db):
    scale = 10 ** (gain_db / 20)
    iq = (synth.am(1.0, -40.0) + synth.noise(1.0, -55.0)) * scale
    assert any(_through_chain(iq))
    assert not any(_through_chain(synth.noise(1.0, -55.0) * scale))
