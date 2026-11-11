"""Dual watch: the table of `docs/dual-watch-feasibility.md`, section 3.2, on a fake clock."""

import pytest

from rbsdrradio.dualwatch import DualWatch, Mode, Step
from rbsdrradio.freqrules import channel_from_name

CFG = {"hold_s": 2.0, "lookback_s": 1.0, "uninterrupted_s": 30.0}
PRIMARY, SECONDARY = channel_from_name(118_700), channel_from_name(121_500)
QUIET, SIGNAL = 0.0, 20.0
BLOCK_S, HOP_S = 0.032, 0.05


@pytest.fixture
def watch(clock):
    w = DualWatch(CFG, PRIMARY, SECONDARY, 10, clock)
    assert w.start() == Step(PRIMARY, dwell=True)
    return w


def lock_on(watch, channel):
    """Scan until a signal on `channel` is locked."""
    while watch.looking != channel:
        watch.on_dwell(QUIET)
    assert watch.on_dwell(SIGNAL) == Step(channel, dwell=True)
    assert watch.on_dwell(SIGNAL) == Step(channel, dwell=False)
    assert watch.locked == channel and watch.streaming


def test_scan_starts_on_the_primary_and_alternates(watch):
    assert watch.on_dwell(QUIET) == Step(SECONDARY, dwell=True)
    assert watch.on_dwell(QUIET) == Step(PRIMARY, dwell=True)
    assert watch.mode is Mode.SCAN and watch.locked is None


def test_a_signal_is_looked_at_twice_before_locking(watch):
    assert watch.on_dwell(SIGNAL) == Step(PRIMARY, dwell=True)
    assert watch.locked is None
    assert watch.on_dwell(SIGNAL) == Step(PRIMARY, dwell=False)
    assert watch.locked == PRIMARY


def test_one_reading_is_not_enough(watch):
    """Ignition noise is impulsive: a hit that does not repeat moves on."""
    watch.on_dwell(SIGNAL)
    assert watch.on_dwell(QUIET) == Step(SECONDARY, dwell=True)
    assert watch.on_dwell(SIGNAL) == Step(SECONDARY, dwell=True)
    assert watch.locked is None


def test_exactly_the_squelch_counts_as_a_signal(watch):
    watch.on_dwell(10.0)
    assert watch.on_dwell(10.0).dwell is False


def test_below_the_squelch_is_quiet(watch):
    assert watch.on_dwell(9.9) == Step(SECONDARY, dwell=True)


def test_a_new_squelch_applies_at_once(watch):
    watch.threshold_db = 25
    assert watch.on_dwell(20.0) == Step(SECONDARY, dwell=True)


def test_the_primary_is_never_left_while_locked(watch, clock):
    lock_on(watch, PRIMARY)
    for _ in range(1000):
        clock.advance(BLOCK_S)
        assert watch.on_block(True) is None


def test_hold_keeps_the_lock_for_the_reply(watch, clock):
    lock_on(watch, PRIMARY)
    assert watch.on_block(False) is None
    clock.advance(1.9)
    assert watch.on_block(False) is None
    clock.advance(0.1)
    assert watch.on_block(False) == Step(PRIMARY, dwell=True)
    assert watch.mode is Mode.SCAN and watch.locked is None


def test_a_reply_inside_the_hold_starts_it_again(watch, clock):
    lock_on(watch, PRIMARY)
    assert watch.on_block(False) is None
    clock.advance(1.9)
    assert watch.on_block(True) is None
    clock.advance(1.9)
    assert watch.on_block(False) is None
    clock.advance(1.9)
    assert watch.on_block(False) is None
    assert watch.locked == PRIMARY


def test_after_a_lock_on_the_secondary_the_primary_is_looked_at_first(watch, clock):
    lock_on(watch, SECONDARY)
    watch.on_block(False)
    clock.advance(0.5)
    assert watch.on_block(False) is None
    clock.advance(0.5)
    assert watch.on_block(False).channel == PRIMARY         # the look back
    watch.on_dwell(QUIET)
    clock.advance(1.0)
    assert watch.on_block(False) == Step(PRIMARY, dwell=True)
    assert watch.mode is Mode.SCAN


def count_looks(watch, clock, seconds, gate_open=True):
    looks = 0
    for _ in range(int(round(seconds / BLOCK_S))):
        clock.advance(BLOCK_S)
        step = watch.on_block(gate_open)
        if step is not None:
            assert step == Step(PRIMARY, dwell=True)
            assert watch.mode is Mode.LOOKBACK
            looks += 1
            assert watch.on_dwell(QUIET) == Step(SECONDARY, dwell=False, resume=True)
    return looks


def test_a_transmission_on_the_secondary_is_not_interrupted(watch, clock):
    """Each look is a hop away and back, heard as a pop in the middle of a call."""
    lock_on(watch, SECONDARY)
    assert count_looks(watch, clock, CFG["uninterrupted_s"] - 0.1) == 0
    assert watch.locked == SECONDARY and watch.streaming


def test_looks_back_every_second_once_a_transmission_outlasts_uninterrupted(watch, clock):
    lock_on(watch, SECONDARY)
    assert count_looks(watch, clock, CFG["uninterrupted_s"] - 0.1) == 0
    assert count_looks(watch, clock, 10.0) == 10


def test_a_new_transmission_starts_uninterrupted_again(watch, clock):
    lock_on(watch, SECONDARY)
    count_looks(watch, clock, CFG["uninterrupted_s"] + 2.0)
    clock.advance(BLOCK_S)
    watch.on_block(False)                  # unkeyed, and keyed again inside the hold
    assert count_looks(watch, clock, CFG["uninterrupted_s"] - 0.1) == 0


def test_looks_back_during_the_hold_as_well(watch, clock):
    lock_on(watch, SECONDARY)
    clock.advance(1.0)
    assert watch.on_block(False) == Step(PRIMARY, dwell=True)


def test_a_quiet_look_back_returns_to_the_same_lock(watch, clock):
    lock_on(watch, SECONDARY)
    clock.advance(1.0)
    watch.on_block(False)
    assert watch.on_dwell(QUIET) == Step(SECONDARY, dwell=False, resume=True)
    assert watch.locked == SECONDARY and watch.streaming


def test_a_look_back_needs_confirming_too(watch, clock):
    lock_on(watch, SECONDARY)
    clock.advance(1.0)
    watch.on_block(False)
    assert watch.on_dwell(SIGNAL) == Step(PRIMARY, dwell=True)
    assert watch.locked == SECONDARY
    assert watch.on_dwell(QUIET) == Step(SECONDARY, dwell=False, resume=True)


def test_the_primary_takes_the_lock_from_the_secondary(watch, clock):
    lock_on(watch, SECONDARY)
    clock.advance(1.0)
    watch.on_block(False)
    watch.on_dwell(SIGNAL)
    assert watch.on_dwell(SIGNAL) == Step(PRIMARY, dwell=False)
    assert watch.locked == PRIMARY
    for _ in range(100):
        clock.advance(BLOCK_S)
        assert watch.on_block(True) is None


def test_an_atis_as_secondary_does_not_shut_the_primary_out(watch, clock):
    """The study's worst case: a secondary that never stops. Without the look back the
    primary loses 94 % of its transmissions. It starts once the ATIS has outlasted
    `uninterrupted_s`, and from then on the primary is found within a look."""
    lock_on(watch, SECONDARY)
    keyed_at = CFG["uninterrupted_s"] + 12.34       # the primary starts, seconds in
    start = clock.now
    found_at = None
    while found_at is None and clock.now - start < keyed_at + 8.0:
        clock.advance(BLOCK_S)
        step = watch.on_block(True)        # the ATIS: the squelch never shuts
        while step is not None and step.dwell:
            clock.advance(HOP_S)
            on_air = clock.now - start >= keyed_at
            step = watch.on_dwell(SIGNAL if on_air else QUIET)
        if watch.locked == PRIMARY:
            found_at = clock.now - start
    assert found_at is not None
    assert found_at - keyed_at <= CFG["lookback_s"] + 3 * HOP_S
