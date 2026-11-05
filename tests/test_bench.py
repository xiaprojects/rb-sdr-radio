"""The command-line antenna tests, through the real entry point."""

import stat
import time
import wave

import numpy as np
import pytest
from conftest import BLOCK, tone_level

from rbsdrradio import synth
from rbsdrradio.__main__ import main
from rbsdrradio.bench import (
    EXIT_HARDWARE,
    EXIT_NO_SIGNAL,
    EXIT_OK,
    BenchRequest,
    run_test,
)
from rbsdrradio.dsp import AM_AUDIO_HZ, FM_AUDIO_HZ, FS_HZ, TUNER_OFFSET_HZ
from rbsdrradio.sdr import FakeDevice, SdrError


def capture(path, iq):
    synth.quantize(iq).tofile(path)
    return path


def am_capture(tmp_path, seconds=1.0, level=-30.0):
    return capture(tmp_path / "am.cu8", synth.am(seconds, level) + synth.noise(seconds, -50.0))


def fm_capture(tmp_path, seconds=1.0):
    return capture(tmp_path / "fm.cu8", synth.fm(seconds, -10.0) + synth.noise(seconds, -40.0))


def test_am_good_signal(tmp_path, capsys):
    rc = main(["test-am", "118.7", "--replay", str(am_capture(tmp_path))])
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "GOOD" in out and "S/N" in out


def test_am_nothing_on_air_says_what_to_do(tmp_path, capsys):
    path = capture(tmp_path / "quiet.cu8", synth.noise(1.0, -30.0))
    rc = main(["test-am", "118.7", "--replay", str(path)])
    out = capsys.readouterr().out
    assert rc == EXIT_NO_SIGNAL
    assert "NO usable signal" in out and "ATIS" in out


def test_am_intermittent_traffic_counts_as_heard(tmp_path, capsys):
    """Airband is mostly silence: one transmission in the run is a working antenna."""
    n = FS_HZ
    iq = synth.noise(3.0, -45.0)
    iq[n:2 * n] += synth.am(1.0, -30.0)[:n]
    rc = main(["test-am", "118.7", "--replay", str(capture(tmp_path / "x.cu8", iq))])
    assert rc == EXIT_OK and "GOOD" in capsys.readouterr().out


def test_am_weak_signal(tmp_path, capsys):
    # -40 dBFS across 1.024 MHz is -58 dBFS in the 15.6 kHz channel: 15 dB over that.
    path = capture(tmp_path / "weak.cu8", synth.carrier(1.0, -43.0) + synth.noise(1.0, -40.0))
    rc = main(["test-am", "118.7", "--replay", str(path)])
    out = capsys.readouterr().out
    assert rc == EXIT_OK and "WEAK" in out


def test_am_reports_the_dongle_error(tmp_path, capsys):
    path = capture(tmp_path / "off.cu8", synth.am(1.0, -30.0, offset_hz=1300.0)
                   + synth.noise(1.0, -50.0))
    main(["test-am", "130.0", "--replay", str(path)])
    out = capsys.readouterr().out
    assert "+1312 Hz" in out or "+1281 Hz" in out or "+1344 Hz" in out
    assert "add about -10." in out


def test_am_accepts_a_frequency_that_is_not_a_channel(tmp_path, capsys):
    rc = main(["test-am", "118.123", "--replay", str(am_capture(tmp_path))])
    assert rc == EXIT_OK
    assert "plain frequency" in capsys.readouterr().out


def test_am_saves_what_it_heard(tmp_path, capsys):
    wav = tmp_path / "out.wav"
    main(["test-am", "118.7", "--replay", str(am_capture(tmp_path)), "--wav", str(wav)])
    with wave.open(str(wav)) as w:
        assert w.getframerate() == AM_AUDIO_HZ
        audio = np.frombuffer(w.readframes(w.getnframes()), "<i2") / 32768.0
    assert tone_level(audio[2000:], AM_AUDIO_HZ, 1000.0)[1] > 25.0


def test_fm_good_station(tmp_path, capsys):
    rc = main(["test-fm", "101.5", "--replay", str(fm_capture(tmp_path))])
    assert rc == EXIT_OK and "GOOD" in capsys.readouterr().out


def test_fm_saves_music_at_its_own_rate(tmp_path, capsys):
    wav = tmp_path / "fm.wav"
    main(["test-fm", "101.5", "--replay", str(fm_capture(tmp_path)), "--wav", str(wav)])
    with wave.open(str(wav)) as w:
        assert w.getframerate() == FM_AUDIO_HZ


def test_fm_dead_antenna_mentions_the_band_stop(tmp_path, capsys):
    """rb-vor's FM band-stop filter would make a healthy antenna fail this test."""
    path = capture(tmp_path / "quiet.cu8", synth.noise(1.0, -30.0))
    rc = main(["test-fm", "101.5", "--replay", str(path)])
    out = capsys.readouterr().out
    assert rc == EXIT_NO_SIGNAL and "band-stop" in out


def test_fm_outside_the_band_warns(tmp_path, capsys):
    main(["test-fm", "118.7", "--replay", str(fm_capture(tmp_path))])
    assert "outside the FM broadcast band" in capsys.readouterr().out


def test_a_replay_says_nothing_about_the_sense(tmp_path, capsys):
    """One capture is one tuning, so there is no way to see the band move."""
    iq = np.conj(synth.fm(1.0, -10.0) + synth.noise(1.0, -40.0))
    assert main(["test-fm", "101.5", "--replay", str(capture(tmp_path / "x.cu8", iq))]) == 1
    assert "swap_iq" not in capsys.readouterr().out


def test_clipping_is_reported(tmp_path, capsys):
    path = capture(tmp_path / "hot.cu8", synth.am(1.0, 3.0) + synth.noise(1.0, -30.0))
    main(["test-am", "118.7", "--replay", str(path)])
    assert "clipping" in capsys.readouterr().out


def test_empty_capture(tmp_path, capsys):
    path = tmp_path / "empty.cu8"
    path.write_bytes(b"")
    assert main(["test-am", "118.7", "--replay", str(path)]) == EXIT_HARDWARE


# -- live path, with the dongle replaced ---------------------------------------- #

def device_for(iq):
    """A FakeDevice that loops `iq` at real time."""
    iq = iq.astype(np.complex64)
    position = {"n": 0}

    def read(n):
        import time
        time.sleep(n / FS_HZ)
        start = position["n"] % (iq.size - n)
        position["n"] += n
        return iq[start:start + n]

    return FakeDevice(read)


def player(tmp_path, out):
    path = tmp_path / "player"
    path.write_text(f'#!/bin/sh\nexec cat > "{out}"\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_live_am_plays_and_releases(cfg, tmp_path):
    heard = tmp_path / "heard"
    device = device_for(synth.am(1.0, -30.0) + synth.noise(1.0, -50.0))
    lines = []
    bench_cfg = cfg.with_overrides(audio={"player": player(tmp_path, heard)})
    rc = run_test(bench_cfg, BenchRequest("am", 118.03, seconds=1.2, volume=100),
                  opener=lambda c: device, out=lines.append)
    assert rc == EXIT_OK
    assert device.freq_hz == 118_025_000 + TUNER_OFFSET_HZ
    assert device.closed
    audio = np.frombuffer(heard.read_bytes(), "<i2") / 32768.0
    assert tone_level(audio[1000:], AM_AUDIO_HZ, 1000.0)[1] > 20.0
    assert any("dBFS" in line and "S/N" in line for line in lines[:-5])


def test_live_fm_plays_at_its_own_rate(cfg, tmp_path):
    heard = tmp_path / "heard"
    device = device_for(synth.fm(1.0, -10.0) + synth.noise(1.0, -40.0))
    bench_cfg = cfg.with_overrides(audio={"player": player(tmp_path, heard)})
    rc = run_test(bench_cfg, BenchRequest("fm", 101.5, seconds=1.0, volume=100),
                  opener=lambda c: device, out=lambda s: None)
    assert rc == EXIT_OK
    audio = np.frombuffer(heard.read_bytes(), "<i2") / 32768.0
    assert tone_level(audio[1000:], FM_AUDIO_HZ, 1000.0)[1] > 20.0


def test_no_audio_leaves_alsa_alone(cfg):
    device = device_for(synth.am(1.0, -30.0) + synth.noise(1.0, -50.0))
    broken = cfg.with_overrides(audio={"player": "/nonexistent/aplay"})
    rc = run_test(broken, BenchRequest("am", 118.7, seconds=0.6, audio=False),
                  opener=lambda c: device, out=lambda s: None)
    assert rc == EXIT_OK


def test_no_sound_card_still_measures(cfg):
    device = device_for(synth.am(1.0, -30.0) + synth.noise(1.0, -50.0))
    lines = []
    broken = cfg.with_overrides(audio={"player": "/nonexistent/aplay"})
    rc = run_test(broken, BenchRequest("am", 118.7, seconds=0.6), opener=lambda c: device,
                  out=lines.append)
    assert rc == EXIT_OK
    assert any("no audio" in line for line in lines)


def test_busy_dongle_points_at_the_daemon(cfg):
    def opener(c):
        raise SdrError("cannot open RTL-SDR index 0 (error -6)")

    lines = []
    rc = run_test(cfg, BenchRequest("am", 118.7, seconds=1.0), opener=opener, out=lines.append)
    assert rc == EXIT_HARDWARE
    assert any("systemctl stop rb-sdr-radio" in line for line in lines)


def test_squelch_option_gates_the_output(cfg, tmp_path):
    heard = tmp_path / "heard"
    device = device_for(synth.noise(1.0, -40.0))
    bench_cfg = cfg.with_overrides(audio={"player": player(tmp_path, heard)})
    run_test(bench_cfg, BenchRequest("am", 118.7, seconds=0.5, volume=100, squelch_db=10),
             opener=lambda c: device, out=lambda s: None)
    assert not np.frombuffer(heard.read_bytes(), "<i2").any()


# -- command line ---------------------------------------------------------------- #

class BandDevice(FakeDevice):
    """A band fixed in RF, so retuning moves it. DC and a spur belong to the dongle and stay."""

    def __init__(self, stations, inverted=False, swap_iq=False):
        super().__init__(self._band, "band")
        self.stations = stations                  # {hertz: dBFS}
        self.inverted = inverted
        self.swap_iq = swap_iq
        self._at = 0

    def _band(self, n):
        time.sleep(n / FS_HZ)
        t = (np.arange(n) + self._at) / FS_HZ
        self._at += n
        iq = 0.05 + synth.from_dbfs(-30.0) * np.exp(2j * np.pi * 60_000.0 * t)
        for hertz, dbfs in self.stations.items():
            phase = 2.0 * np.pi * (hertz - self.freq_hz) * t + 40.0 * np.sin(2e3 * np.pi * t)
            iq = iq + synth.from_dbfs(dbfs) * np.exp(1j * phase)
        iq = iq + synth.noise(n / FS_HZ, -35.0, seed=self._at)
        return (np.conj(iq) if self.inverted else iq).astype(np.complex64)


def live_fm(cfg, device):
    lines = []
    rc = run_test(cfg, BenchRequest("fm", 106.7, seconds=0.4, audio=False),
                  opener=lambda c: device, out=lines.append)
    return rc, "\n".join(lines)


def test_a_station_on_the_mirror_is_not_an_inverted_dongle(cfg):
    """Found on a real R820T: 106.7 had faded, 106.2 is its mirror, and the old test,
    comparing the energy on each side, said to set swap_iq on a dongle that did not need it.
    """
    rc, out = live_fm(cfg, BandDevice({106_188_000: -15.0}))
    assert rc == EXIT_NO_SIGNAL
    assert "normal" in out and "swap_iq" not in out


def test_stations_on_the_fm_grid_do_not_hide_the_movement(cfg):
    """A step of 200 kHz put stations on each other's places, and strong ones then read
    as nothing having moved: found on a real R820T.
    """
    band = dict.fromkeys(range(105_900_000, 107_100_000, 200_000), -20.0)
    _, out = live_fm(cfg, BandDevice(band))
    assert "sense       normal" in out


def test_an_inverted_dongle_is_called_out(cfg):
    """Inversion looks like a dead antenna. The test must say which it is."""
    rc, out = live_fm(cfg, BandDevice({106_700_000: -15.0}, inverted=True))
    assert rc == EXIT_NO_SIGNAL
    assert '"swap_iq": true' in out


def test_a_needless_swap_iq_is_called_out_too(cfg):
    _, out = live_fm(cfg, BandDevice({106_700_000: -15.0}, swap_iq=True))
    assert 'Remove "swap_iq"' in out


def test_an_inverted_dongle_with_swap_iq_set_reads_normal(cfg):
    rc, out = live_fm(cfg, BandDevice({106_700_000: -15.0}, inverted=True, swap_iq=True))
    assert rc == EXIT_OK
    assert "normal" in out and "swap_iq" not in out


def test_an_empty_band_gives_no_verdict_on_the_sense(cfg):
    """Spurs do not move, so they must not be taken for a signal that did."""
    _, out = live_fm(cfg, BandDevice({}))
    assert "not measured" in out and "swap_iq" not in out


def test_overrides_reach_the_config():
    from argparse import Namespace

    from rbsdrradio.__main__ import _bench_config
    from rbsdrradio.config import load
    base = load()
    args = Namespace(gain="19.2", ppm=9, device="radio")
    c = _bench_config(base, args)
    assert c.sdr["gain_db"] == 19.2 and c.sdr["ppm"] == 9 and c.sdr["device_serial"] == "radio"
    assert "gain_db" not in c.sdr["tuner_profiles"]["R820T"]   # the operator's gain must win
    assert base.sdr["ppm"] == 0                   # and the loaded config is untouched
    by_index = _bench_config(base, Namespace(gain="auto", ppm=None, device="1"))
    assert by_index.sdr["device_index"] == 1 and by_index.sdr["gain_mode"] == "agc"


def test_a_typed_value_replaces_only_that_key_of_the_tuner_profile():
    """An inverted dongle must stay swapped under --gain, or the test hears only the mirror."""
    from argparse import Namespace

    from rbsdrradio.__main__ import _bench_config
    from rbsdrradio.config import load
    from rbsdrradio.sdr import apply_tuner_profile
    base = load().with_overrides(sdr={"tuner_profiles": {
        "FC0012": {"gain_db": 19.2, "ppm": 7, "swap_iq": True}}})

    gain = _bench_config(base, Namespace(gain="7.1", ppm=None, device=None)).sdr
    effective = apply_tuner_profile(gain, "FC0012")[0]
    assert (effective["gain_db"], effective["ppm"], effective["swap_iq"]) == (7.1, 7, True)

    ppm = _bench_config(base, Namespace(gain=None, ppm=3, device=None)).sdr
    effective = apply_tuner_profile(ppm, "FC0012")[0]
    assert (effective["gain_db"], effective["ppm"], effective["swap_iq"]) == (19.2, 3, True)

    # On a tuner with no AGC the profile's gain is what gets used, so `auto` keeps it.
    auto = _bench_config(base, Namespace(gain="auto", ppm=None, device=None)).sdr
    assert apply_tuner_profile(auto, "FC0012")[0]["gain_db"] == 19.2


def test_bad_config_stops_the_daemon(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("{}")
    assert main(["--config", str(path)]) == 2
    assert "configuration error" in capsys.readouterr().err


def test_self_test_passes(capsys):
    assert main(["self-test"]) == 0
    assert "PASSED" in capsys.readouterr().out


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0


def test_block_constant_is_the_shipped_one(cfg):
    assert cfg.sdr["block_samples"] == BLOCK
