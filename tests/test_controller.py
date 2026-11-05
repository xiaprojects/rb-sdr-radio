"""The state machine. No asyncio, no radio, no sound card, no waiting."""

import json

import numpy as np
import pytest
from conftest import BLOCK, blocks_of

from rbsdrradio import synth
from rbsdrradio.audio import AudioError
from rbsdrradio.controller import (
    FAULT_AUDIO,
    FAULT_FREQUENCY,
    FAULT_RECORD,
    FAULT_SDR,
    Controller,
    State,
)
from rbsdrradio.dsp import TUNER_OFFSET_HZ, AmDemod
from rbsdrradio.freqrules import SPACING_25, SPACING_833
from rbsdrradio.publisher import FIELDS
from rbsdrradio.sdr import SdrError

CARRIER = blocks_of(synth.am(0.5, -30.0) + synth.noise(0.5, -60.0))
QUIET = blocks_of(synth.noise(0.5, -60.0, seed=4))


class FakeSdr:
    def __init__(self, clock):
        self.clock = clock
        self.is_open = False
        self.tuned = []
        self.closes = 0
        self.fail = None
        self.streaming = True
        self.last_block_at = None

    def set_frequency(self, hz):
        if self.fail:
            raise self.fail
        self.tuned.append(hz)
        self.is_open = True
        self.last_block_at = self.clock()

    def close(self):
        self.closes += self.is_open
        self.is_open = False


class FakeAudio:
    def __init__(self):
        self.is_open = False
        self.writes = []
        self.opens = 0
        self.closes = 0
        self.fail_open = None
        self.fail_poll = None
        self.fail_write = None

    def open(self):
        if self.fail_open:
            raise self.fail_open
        self.is_open = True
        self.opens += 1

    def close(self):
        self.closes += self.is_open
        self.is_open = False

    def poll(self):
        if self.fail_poll:
            self.is_open = False
            raise self.fail_poll

    def write(self, pcm):
        if self.fail_write:
            self.is_open = False
            raise self.fail_write
        self.writes.append(pcm)


class FakeRecorder:
    def __init__(self):
        self.fed = []
        self.ended = 0
        self.fault = False
        self.active = False

    def feed(self, pcm, gate_open, channel_khz):
        self.fed.append((pcm, gate_open, channel_khz))
        self.active = True

    def end_segment(self):
        self.ended += self.active
        self.active = False


class FakePublisher:
    def __init__(self):
        self.seen = []

    def publish(self, payload):
        self.seen.append(dict(payload))

    @property
    def last(self):
        return self.seen[-1]


class Rig:
    def __init__(self, cfg, clock):
        self.clock = clock
        self.sdr = FakeSdr(clock)
        self.audio = FakeAudio()
        self.rec = FakeRecorder()
        self.pub = FakePublisher()
        self.c = Controller(cfg, self.sdr, self.audio, self.rec, self.pub, clock=clock)

    def run(self, seconds, step=0.1):
        for _ in range(int(round(seconds / step))):
            self.clock.advance(step)
            if self.sdr.is_open and self.sdr.streaming:
                self.sdr.last_block_at = self.clock()
            self.c.tick()

    def past_debounce(self):
        """Debounce done, tuner moved, settle time not yet over."""
        self.clock.advance(0.31)
        self.c.tick()

    def tune(self, mhz=118.7):
        self.c.set_frequency(mhz)
        self.run(0.6)
        assert self.c.state is State.LISTENING

    def receive(self, blocks=CARRIER):
        for block in blocks:
            self.sdr.last_block_at = self.clock()
            self.c.push(block)


@pytest.fixture
def rig(cfg, clock):
    return Rig(cfg, clock)


# -- engage and disengage ------------------------------------------------------- #

def test_starts_idle_holding_nothing(rig):
    rig.c.tick()
    assert rig.c.state is State.IDLE
    assert rig.pub.last["sdrradiostatus"] == 0
    assert not rig.sdr.is_open and not rig.audio.is_open


def test_a_frequency_engages_after_the_debounce(rig):
    rig.c.set_frequency(118.7)
    rig.run(0.2)
    assert rig.sdr.tuned == []
    rig.clock.advance(0.11)
    rig.c.tick()
    assert rig.sdr.tuned == [118_700_000]
    assert rig.c.state is State.TUNING


def test_settles_into_listening(rig):
    rig.tune()
    assert rig.pub.last["sdrradiostatus"] == 3
    assert rig.pub.last["sdrradiofreqactive"] == 118_700
    assert rig.pub.last["sdrradiosdr"] == 1


def test_spinning_a_knob_tunes_once(rig):
    for mhz in (118.7, 118.725, 118.75, 118.775, 118.8):
        rig.c.set_frequency(mhz)
        rig.run(0.1)
    rig.run(0.5)
    assert rig.sdr.tuned == [118_800_000]


def test_repeating_the_frequency_does_nothing_at_any_point(rig):
    """RB-01 sends the whole map, our own echoes included, on every change."""
    rig.c.set_frequency(118.7)
    rig.run(0.4)
    for _ in range(30):
        rig.c.set_frequency(118.7)
        rig.run(0.1)
    assert rig.sdr.tuned == [118_700_000]
    assert rig.c.state is State.LISTENING


def test_returning_to_the_tuned_channel_cancels_the_change(rig):
    rig.tune(118.7)
    rig.c.set_frequency(121.5)
    rig.c.set_frequency(118.7)
    rig.run(0.6)
    assert rig.sdr.tuned == [118_700_000]


def test_8_33_name_tunes_the_true_frequency_and_narrow_filter(rig):
    rig.tune(118.03)
    assert rig.sdr.tuned == [118_025_000]
    assert rig.c.demod.spacing == SPACING_833
    assert rig.pub.last["sdrradiofreqactive"] == 118_030


def test_25_khz_channel_gets_the_wide_filter(rig):
    rig.tune(118.025)
    assert rig.sdr.tuned == [118_025_000]
    assert rig.c.demod.spacing == SPACING_25


@pytest.mark.parametrize("off", [0, 0.0, None])
def test_frequency_zero_releases_the_sdr(rig, off):
    rig.tune()
    rig.c.set_frequency(off)
    assert rig.c.state is State.IDLE
    assert not rig.sdr.is_open and rig.sdr.closes == 1
    assert rig.pub.last["sdrradiostatus"] == 0
    assert rig.pub.last["sdrradiofreqactive"] == 0


def test_next_frequency_reopens_after_a_release(rig):
    rig.tune(118.7)
    rig.c.set_frequency(0)
    rig.tune(121.5)
    assert rig.sdr.tuned == [118_700_000, 121_500_000]
    assert rig.sdr.is_open


def test_invalid_frequency_releases_and_says_why(rig):
    rig.tune()
    rig.c.set_frequency(137.5)
    assert rig.c.state is State.INVALID_FREQ
    assert not rig.sdr.is_open
    assert rig.pub.last["sdrradiostatus"] == 0
    assert rig.pub.last["sdrradiofault"] == FAULT_FREQUENCY
    assert rig.pub.last["sdrradiofreqactive"] == 0


def test_invalid_frequency_while_idle_never_opens_the_sdr(rig):
    rig.c.set_frequency(118.02)
    rig.run(1.0)
    assert rig.sdr.tuned == []


def test_a_valid_frequency_clears_the_invalid_flag(rig):
    rig.c.set_frequency(137.5)
    rig.tune(118.7)
    assert rig.pub.last["sdrradiofault"] == 0


def test_cancelling_before_the_debounce_never_tunes(rig):
    rig.c.set_frequency(118.7)
    rig.c.set_frequency(0)
    rig.run(1.0)
    assert rig.sdr.tuned == []


# -- the bridge goes away ------------------------------------------------------- #

def test_lost_bridge_releases_after_the_grace_period(rig, cfg):
    rig.tune()
    rig.c.on_bridge_disconnected()
    rig.run(cfg.bridge["ws_grace_s"] - 0.5)
    assert rig.sdr.is_open
    rig.run(1.0)
    assert not rig.sdr.is_open and not rig.audio.is_open
    assert rig.c.state is State.IDLE


def test_a_blip_inside_the_grace_period_changes_nothing(rig):
    rig.tune()
    rig.c.on_bridge_disconnected()
    rig.run(1.0)
    rig.c.on_bridge_connected()
    rig.run(10.0)
    assert rig.sdr.is_open and rig.c.state is State.LISTENING


def test_reconnecting_restores_the_channel(rig, cfg):
    """The GET on connect replays `sdrradiofreq`, which must count as a new command."""
    rig.tune()
    rig.c.on_bridge_disconnected()
    rig.run(cfg.bridge["ws_grace_s"] + 1.0)
    rig.c.on_bridge_connected()
    rig.c.apply({"freq": 118.7, "volume": 50.0})
    rig.run(0.6)
    assert rig.c.state is State.LISTENING and rig.sdr.is_open


# -- volume and the sound device ------------------------------------------------ #

def test_sound_device_held_while_tuned(rig):
    rig.tune()
    assert rig.audio.is_open
    assert rig.pub.last["sdrradioaudio"] == 1


def test_volume_zero_releases_the_sound_device_at_once(rig):
    rig.tune()
    rig.c.set_volume(0)                    # no tick: synchronous with the command
    assert not rig.audio.is_open and rig.audio.closes == 1
    assert rig.pub.last["sdrradioaudio"] == 0


def test_volume_zero_keeps_the_sdr(rig):
    rig.tune()
    rig.c.set_volume(0)
    rig.run(1.0)
    assert rig.sdr.is_open and rig.c.state is State.LISTENING


def test_volume_zero_keeps_channel_status_and_recording(rig):
    rig.tune()
    rig.c.set_volume(0)
    rig.receive()
    assert rig.pub.last["sdrradiosquelchopen"] == 1
    assert rig.rec.fed and rig.pub.last["sdrradiosnr"] > 10


def test_turning_it_back_up_reopens_at_once(rig):
    rig.tune()
    rig.c.set_volume(0)
    rig.c.set_volume(30)
    assert rig.audio.is_open and rig.audio.opens == 2


def test_no_frequency_means_no_sound_device_whatever_the_volume(rig):
    rig.c.set_volume(80)
    rig.run(1.0)
    assert not rig.audio.is_open
    rig.tune()
    rig.c.set_frequency(0)
    assert not rig.audio.is_open


def test_volume_scales_the_samples(rig):
    rig.tune()

    def loudness(volume):
        rig.c.set_volume(volume)
        rig.audio.writes.clear()
        rig.receive()
        return max(np.abs(np.frombuffer(b"".join(rig.audio.writes), "<i2")))

    assert loudness(100) > 3.0 * loudness(50) > 0


def test_recording_ignores_the_volume_knob(rig):
    rig.tune()

    def recorded(volume):
        rig.c.set_volume(volume)
        rig.rec.fed.clear()
        rig.receive()
        return max(np.abs(np.frombuffer(b"".join(p[0] for p in rig.rec.fed), "<i2")))

    assert recorded(1) == pytest.approx(recorded(100), rel=0.05)


@pytest.mark.parametrize("value,expected", [(150, 100), (-5, 0), (49.6, 50)])
def test_volume_is_clamped_and_echoed(rig, value, expected):
    rig.c.set_volume(value)
    rig.c.tick()
    assert rig.pub.last["sdrradiovolumeactive"] == expected


# -- squelch --------------------------------------------------------------------- #

def test_squelch_shuts_out_noise(rig):
    rig.tune()
    rig.audio.writes.clear()
    rig.receive(QUIET)
    assert not any(np.frombuffer(b"".join(rig.audio.writes), "<i2"))
    assert rig.pub.last["sdrradiostatus"] == 3
    assert rig.pub.last["sdrradiosquelchopen"] == 0


def test_carrier_opens_it_and_reports_receiving(rig):
    rig.tune()
    rig.receive()
    assert rig.c.state is State.RECEIVING
    assert rig.pub.last["sdrradiostatus"] == 4
    assert rig.pub.last["sdrradiosquelchopen"] == 1
    assert any(np.frombuffer(b"".join(rig.audio.writes), "<i2"))


def test_channel_status_reports_real_levels(rig):
    rig.tune()
    rig.receive()
    assert rig.pub.last["sdrradiosignal"] == pytest.approx(-30, abs=1)
    assert rig.pub.last["sdrradiosnr"] > 20


def test_squelch_zero_holds_it_open_on_noise(rig):
    rig.c.set_squelch(0)
    rig.tune()
    rig.audio.writes.clear()
    rig.receive(QUIET)
    assert any(np.frombuffer(b"".join(rig.audio.writes), "<i2"))


def test_squelch_change_takes_effect_on_the_next_block(rig):
    rig.tune()
    rig.c.set_squelch(90)
    rig.receive()
    assert rig.c.state is State.LISTENING
    assert rig.pub.last["sdrradiosquelchactive"] == 90


def test_a_retune_never_reports_the_old_channel(rig):
    """Status for the new channel must not carry the old channel's reading."""
    rig.tune(118.7)
    rig.receive()
    assert rig.pub.last["sdrradiosnr"] > 10
    rig.c.set_frequency(121.5)
    rig.past_debounce()
    assert rig.c.state is State.TUNING
    assert rig.pub.last["sdrradiosnr"] == 0 and rig.pub.last["sdrradiosignal"] == 0
    assert rig.pub.last["sdrradiosquelchopen"] == 0


def test_status_goes_out_before_the_tuner_moves(rig):
    order = []
    rig.tune(118.7)
    original = rig.sdr.set_frequency
    rig.sdr.set_frequency = lambda hz: (order.append(("tune", rig.pub.last["sdrradiostatus"])),
                                        original(hz))
    rig.c.set_frequency(121.5)
    rig.past_debounce()
    assert order == [("tune", 2)]


def test_samples_during_a_retune_are_dropped(rig):
    rig.tune(118.7)
    rig.c.set_frequency(121.5)
    rig.past_debounce()
    rig.audio.writes.clear()
    rig.rec.fed.clear()
    rig.receive()
    assert rig.audio.writes == [] and rig.rec.fed == []


def test_samples_while_idle_are_dropped(rig):
    rig.receive()
    assert rig.audio.writes == []


# -- recording ------------------------------------------------------------------- #

def test_records_by_channel_name_while_open(rig):
    rig.tune(118.03)
    rig.receive()
    assert rig.rec.fed[-1][2] == 118_030
    assert rig.rec.fed[-1][1] is True
    assert rig.pub.last["sdrradiorecording"] == 1


def test_record_off_stops_at_once(rig):
    rig.tune()
    rig.receive()
    rig.c.set_record(0)
    assert rig.rec.ended == 1
    fed = len(rig.rec.fed)
    rig.receive()
    assert len(rig.rec.fed) == fed
    assert rig.pub.last["sdrradiorecordactive"] == 0


def test_record_on_from_the_config(cfg, clock):
    off = Rig(cfg.with_overrides(record={"enabled": False}), clock)
    off.tune()
    off.receive()
    assert off.rec.fed == []
    off.c.set_record(1)
    off.receive()
    assert off.rec.fed


def test_nothing_recorded_with_the_squelch_held_open(rig):
    """It would be hiss, and it would push real traffic out of the quota."""
    rig.c.set_squelch(0)
    rig.tune()
    rig.receive()
    assert rig.rec.fed == []


def test_release_ends_the_recording(rig):
    rig.tune()
    rig.receive()
    rig.c.set_frequency(0)
    assert rig.rec.ended == 1


def test_recorder_fault_is_flagged_and_audio_carries_on(rig):
    rig.tune()
    rig.rec.fault = True
    rig.receive()
    assert rig.pub.last["sdrradiofault"] == FAULT_RECORD
    assert rig.pub.last["sdrradiorecording"] == 0
    assert any(np.frombuffer(b"".join(rig.audio.writes), "<i2"))


def test_recorder_fault_is_not_shown_when_recording_is_off(rig):
    rig.tune()
    rig.rec.fault = True
    rig.c.set_record(0)
    assert rig.pub.last["sdrradiofault"] == 0


# -- faults ---------------------------------------------------------------------- #

def test_sdr_failure_is_reported_not_fatal(rig):
    rig.sdr.fail = SdrError("no RTL-SDR device found")
    rig.c.set_frequency(118.7)
    rig.run(0.5)
    assert rig.c.state is State.FAULT
    assert rig.pub.last["sdrradiostatus"] == 1
    assert rig.pub.last["sdrradiofault"] == FAULT_SDR
    assert rig.pub.last["sdrradiofreqactive"] == 118_700


def test_sdr_is_retried_until_it_comes_back(rig, cfg):
    rig.sdr.fail = SdrError("busy")
    rig.c.set_frequency(118.7)
    rig.run(0.5)
    rig.sdr.fail = None
    rig.run(cfg.sdr["retry_s"] + 0.5)
    assert rig.c.state is State.LISTENING
    assert rig.pub.last["sdrradiofault"] == 0


def test_no_sound_device_held_during_an_sdr_fault(rig):
    rig.tune()
    rig.c._sdr_failed("gone")
    assert not rig.audio.is_open


def test_unplugged_dongle_is_noticed(rig, cfg):
    rig.tune()
    rig.sdr.streaming = False
    rig.sdr.last_block_at = rig.clock()
    rig.run(cfg.sdr["stall_s"] + 0.2)
    assert rig.c.state is State.FAULT
    assert not rig.sdr.is_open


def test_frequency_zero_during_a_fault_stops_the_retries(rig, cfg):
    rig.sdr.fail = SdrError("busy")
    rig.c.set_frequency(118.7)
    rig.run(0.5)
    rig.c.set_frequency(0)
    rig.run(cfg.sdr["retry_s"] * 2)
    assert rig.c.state is State.IDLE
    assert rig.pub.last["sdrradiofault"] == 0


def test_new_frequency_during_a_fault_is_honoured(rig):
    rig.sdr.fail = SdrError("busy")
    rig.c.set_frequency(118.7)
    rig.run(0.5)
    rig.sdr.fail = None
    rig.c.set_frequency(121.5)
    rig.run(0.6)
    assert rig.sdr.tuned == [121_500_000]


def test_sound_device_failure_keeps_receiving_and_recording(rig):
    rig.audio.fail_open = AudioError("device busy")
    rig.tune()
    rig.receive()
    assert rig.c.state is State.RECEIVING
    assert rig.pub.last["sdrradiofault"] == FAULT_AUDIO
    assert rig.pub.last["sdrradioaudio"] == 0
    assert rig.rec.fed


def test_sound_device_is_retried_with_a_pause(rig, cfg):
    rig.audio.fail_open = AudioError("device busy")
    rig.tune()
    rig.audio.fail_open = None
    assert rig.audio.opens == 0
    rig.run(cfg.audio["retry_s"] + 0.2)
    assert rig.audio.is_open
    rig.run(1.5)
    assert rig.pub.last["sdrradiofault"] == 0


def test_player_dying_later_is_noticed(rig):
    rig.tune()
    rig.audio.fail_poll = AudioError("unplugged")
    rig.run(0.2)
    assert rig.pub.last["sdrradiofault"] == FAULT_AUDIO
    assert rig.pub.last["sdrradiosdr"] == 1


def test_write_failure_in_the_sample_path_is_contained(rig):
    rig.tune()
    rig.audio.fail_write = AudioError("gone")
    rig.receive()
    assert rig.c.state is State.RECEIVING
    assert rig.pub.last["sdrradiofault"] == FAULT_AUDIO


def test_volume_zero_clears_a_sound_device_fault(rig):
    """The operator has stopped asking for it, so there is nothing to report."""
    rig.audio.fail_open = AudioError("busy")
    rig.tune()
    rig.c.set_volume(0)
    assert rig.pub.last["sdrradiofault"] == 0


def test_faults_combine_in_the_mask(rig):
    rig.audio.fail_open = AudioError("busy")
    rig.tune()
    rig.rec.fault = True
    rig.c.tick()
    assert rig.pub.last["sdrradiofault"] == FAULT_AUDIO | FAULT_RECORD


# -- wire format ----------------------------------------------------------------- #

@pytest.mark.parametrize("prepare", ["idle", "tuned", "receiving", "invalid", "fault"])
def test_every_payload_is_integers_rb01_can_store(rig, prepare):
    """One non-number and RB-01 throws the whole POST away, silently."""
    if prepare in ("tuned", "receiving"):
        rig.tune()
    if prepare == "receiving":
        rig.receive()
    if prepare == "invalid":
        rig.c.set_frequency(200.0)
    if prepare == "fault":
        rig.sdr.fail = SdrError("x")
        rig.c.set_frequency(118.7)
        rig.run(0.5)
    rig.c.tick()
    payload = rig.pub.last
    assert set(payload) == set(FIELDS)
    for key, value in payload.items():
        assert type(value) is int, key
        assert abs(value) < 2 ** 24, key           # exact in float32
    json.dumps(payload)


def test_the_tuner_offset_is_not_this_modules_business(rig):
    """The controller speaks wanted frequencies; SdrReader applies the offset."""
    rig.tune()
    assert rig.sdr.tuned == [118_700_000]
    assert TUNER_OFFSET_HZ != 0


def test_apply_ignores_null_for_settings(rig):
    rig.c.apply({"volume": None, "squelch": None, "record": None})
    assert rig.c.volume == 50 and rig.c.squelch_db == 10


def test_apply_routes_every_command(rig):
    rig.c.apply({"freq": 118.7, "volume": 10, "squelch": 20, "record": 0})
    rig.run(0.6)
    assert (rig.c.volume, rig.c.squelch_db, rig.c.record_enabled) == (10, 20, False)
    assert rig.c.channel.name_khz == 118_700


def test_shutdown_releases_everything(rig):
    rig.tune()
    rig.receive()
    rig.c.shutdown()
    assert not rig.sdr.is_open and not rig.audio.is_open
    assert rig.rec.ended == 1
    assert rig.pub.last["sdrradiostatus"] == 0


def test_demod_block_rate_matches_the_squelch(rig, cfg):
    assert rig.c.squelch._hang_blocks == round(cfg.squelch["hang_s"] / (BLOCK / 1_024_000))
    assert isinstance(rig.c.demod, AmDemod)
