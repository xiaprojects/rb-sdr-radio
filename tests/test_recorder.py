"""Recording, including what a power cut and missing storage do to it."""

import os
import re
import stat
import wave
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import wait_until

from rbsdrradio.recorder import INCOMPLETE_DIR, Recorder, repair_wav, segment_name, wav_header

RATE = 16000
SECOND = b"\x10\x00" * RATE


def config(tmp_path, **over):
    return {"enabled": True, "dir": str(tmp_path), "prefix": "radio", "min_segment_s": 0.5,
            "max_segment_s": 300, "hold_s": 2.0, "max_total_mb": 128, "min_free_mb": 0, **over}


def recorder(tmp_path, clock=None, **over):
    kwargs = {"clock": clock} if clock else {}
    r = Recorder(config(tmp_path, **over), RATE, **kwargs)
    r.start()
    return r


def wavs(tmp_path):
    return sorted(p.name for p in Path(tmp_path).glob("*.wav"))


def seconds_of(path):
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def test_header_is_what_python_wave_reads(tmp_path):
    path = tmp_path / "x.wav"
    path.write_bytes(wav_header(RATE, 4) + b"\x01\x00\x02\x00")
    with wave.open(str(path)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, RATE)
        assert w.readframes(2) == b"\x01\x00\x02\x00"


def test_file_name_is_what_radio_js_parses():
    """Mirrors the RB-01 parsing line for line, on the example in its source."""
    name = segment_name("radio", datetime(2024, 6, 7, 5, 12, 25), 130_000)
    assert name == "radio_20240607_051225_130000000.wav"
    path = "/playback/" + name
    assert len(path.split("_")) == 4
    digits = path.split("_").pop().split(".")[0][:6]
    assert f"{digits[:3]}.{digits[3:6]}" == "130.000"


def test_name_carries_the_channel_the_pilot_selected():
    """118.030 tunes 118.0250. The UI must still say 118.030."""
    assert segment_name("radio", datetime(2026, 1, 1), 118_030).endswith("_118030000.wav")


def test_name_is_legal_on_fat():
    name = segment_name("radio", datetime.now(tz=UTC), 118_700)
    assert not re.search(r'[:*?"<>|\\/]', name)


def test_one_transmission_is_one_valid_wav(tmp_path, clock):
    r = recorder(tmp_path, clock)
    for _ in range(3):
        r.feed(SECOND, True, 118_700)
    clock.advance(3.0)
    r.feed(b"", False, 118_700)
    assert not r.active
    r.close()
    names = wavs(tmp_path)
    assert len(names) == 1 and names[0].endswith("_118700000.wav")
    assert seconds_of(tmp_path / names[0]) == 3.0


def test_duration_matches_what_radio_js_assumes_for_wav():
    """radio.js shows Size / 30000 s, so 16 kHz S16 mono reads within 10 %."""
    assert abs(RATE * 2 / 30000 - 1.0) < 0.1


def test_a_pause_inside_an_exchange_stays_in_one_file(tmp_path, clock):
    r = recorder(tmp_path, clock)
    r.feed(SECOND, True, 118_700)
    clock.advance(1.0)
    r.feed(b"", False, 118_700)
    r.feed(SECOND, True, 118_700)
    r.close()
    assert len(wavs(tmp_path)) == 1


def test_a_long_gap_starts_a_new_file(tmp_path, clock):
    r = recorder(tmp_path, clock)
    r.feed(SECOND, True, 118_700)
    clock.advance(3.0)
    r.feed(b"", False, 118_700)
    r.feed(SECOND, True, 118_700)
    r.close()
    assert len(wavs(tmp_path)) == 2


def test_two_transmissions_in_one_second_get_two_files(tmp_path):
    """The name has one-second resolution and must stay parseable."""
    frozen = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    r = Recorder(config(tmp_path), RATE, utcnow=lambda: frozen)
    r.start()
    for _ in range(2):
        r.feed(SECOND, True, 118_700)
        r.end_segment()
    r.close()
    names = wavs(tmp_path)
    assert len(names) == 2
    assert all(len(("/playback/" + n).split("_")) == 4 for n in names)


def test_a_retune_ends_the_file(tmp_path):
    r = recorder(tmp_path)
    r.feed(SECOND, True, 118_700)
    r.feed(SECOND, True, 121_500)
    r.close()
    assert [n[-13:] for n in wavs(tmp_path)] == ["118700000.wav", "121500000.wav"]


def test_noise_spike_leaves_no_file(tmp_path):
    r = recorder(tmp_path)
    r.feed(b"\0" * 1024, True, 118_700)
    r.close()
    assert wavs(tmp_path) == []
    assert list((tmp_path / INCOMPLETE_DIR).iterdir()) == []


def test_a_stuck_carrier_is_cut_into_segments(tmp_path):
    r = recorder(tmp_path, max_segment_s=2, min_segment_s=0.5)
    for _ in range(5):
        r.feed(SECOND, True, 118_700)
    r.close()
    names = wavs(tmp_path)
    assert len(names) == 3
    assert max(seconds_of(tmp_path / n) for n in names) == 2.0


def test_incomplete_file_is_not_visible_in_the_playback_folder(tmp_path):
    """RB-01 lists every entry. A half-written file must not be offered for playing."""
    r = recorder(tmp_path)
    r.feed(SECOND, True, 118_700)
    assert wait_until(lambda: list((tmp_path / INCOMPLETE_DIR).glob("*.wav")))
    assert wavs(tmp_path) == []
    r.close()


def test_power_cut_is_repaired_at_the_next_start(tmp_path):
    """Header sizes still zero, as they would be after the master switch went off."""
    incomplete = tmp_path / INCOMPLETE_DIR
    incomplete.mkdir()
    name = "radio_20260101_120000_118700000.wav"
    (incomplete / name).write_bytes(wav_header(RATE, 0) + SECOND * 2)
    r = recorder(tmp_path)
    assert wait_until(lambda: (tmp_path / name).exists())
    r.close()
    assert seconds_of(tmp_path / name) == 2.0


def test_power_cut_with_nothing_useful_leaves_nothing(tmp_path):
    incomplete = tmp_path / INCOMPLETE_DIR
    incomplete.mkdir()
    (incomplete / "radio_20260101_120000_118700000.wav").write_bytes(b"RIFF")
    r = recorder(tmp_path)
    r.close()
    assert wavs(tmp_path) == []
    assert list(incomplete.iterdir()) == []


def test_repair_drops_a_torn_sample(tmp_path):
    path = tmp_path / "x.wav"
    path.write_bytes(wav_header(RATE, 0) + b"\0\0\0")
    assert repair_wav(path, RATE) == pytest.approx(1 / RATE)
    with wave.open(str(path)) as w:
        assert w.getnframes() == 1


def make_old(tmp_path, n, size=500_000):
    for i in range(n):
        p = tmp_path / f"radio_20260101_12{i:02d}00_118700000.wav"
        p.write_bytes(b"\0" * size)
        os.utime(p, (1_000_000 + i, 1_000_000 + i))


def test_quota_removes_the_oldest_first(tmp_path):
    make_old(tmp_path, 4)
    r = recorder(tmp_path, max_total_mb=1.5, max_segment_s=10)
    r.feed(SECOND, True, 121_500)
    r.close()
    names = wavs(tmp_path)
    assert "radio_20260101_120000_118700000.wav" not in names
    assert "radio_20260101_120300_118700000.wav" in names
    assert any(n.endswith("121500000.wav") for n in names)


def test_quota_never_touches_files_that_are_not_ours(tmp_path):
    """rbvoice records into the same folder."""
    make_old(tmp_path, 3)
    others = ["vrin_20260101_120000.wav", "vr_20260413203842.json", "old",
              "radio_notes.wav", "radio_20260101_120000_118700000.mp3"]
    for name in others:
        (tmp_path / name).write_bytes(b"\0" * 2_000_000)
    r = recorder(tmp_path, max_total_mb=0.2, max_segment_s=5)
    r.feed(SECOND, True, 121_500)
    r.close()
    for name in others:
        assert (tmp_path / name).exists(), name


def test_low_free_space_sacrifices_old_recordings_first(tmp_path, monkeypatch):
    make_old(tmp_path, 2)
    free = [1, 200_000_000]

    class Usage:
        @property
        def free(self):
            return free.pop(0) if len(free) > 1 else free[0]

    monkeypatch.setattr("rbsdrradio.recorder.shutil.disk_usage", lambda p: Usage())
    r = recorder(tmp_path, min_free_mb=100)
    r.feed(SECOND, True, 121_500)
    r.close()
    assert r.fault is False
    assert len(wavs(tmp_path)) == 2


def test_full_partition_refuses_to_record_and_says_so(tmp_path, monkeypatch):
    class Usage:
        free = 1

    monkeypatch.setattr("rbsdrradio.recorder.shutil.disk_usage", lambda p: Usage())
    r = recorder(tmp_path, min_free_mb=100)
    r.feed(SECOND, True, 121_500)
    assert wait_until(lambda: r.fault)
    r.close()
    assert wavs(tmp_path) == []


def test_read_only_folder_is_a_fault_not_a_crash(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores permissions")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        r = Recorder(config(locked), RATE)
        r.start()
        r.feed(SECOND, True, 118_700)
        assert wait_until(lambda: r.fault)
        r.feed(SECOND, True, 118_700)
        r.close()
    finally:
        locked.chmod(stat.S_IRWXU)


def test_recovers_when_the_folder_comes_back(tmp_path):
    missing = tmp_path / "card"
    (tmp_path / "card").write_text("a file where the folder should be")
    r = Recorder(config(missing), RATE)
    r.start()
    r.feed(SECOND, True, 118_700)
    assert wait_until(lambda: r.fault)
    r.end_segment()
    assert wait_until(lambda: r._q.empty())
    missing.unlink()
    r.feed(SECOND, True, 118_700)
    assert wait_until(lambda: not r.fault)
    r.close()
    assert len(wavs(missing)) == 1


def test_folder_may_be_a_symlink_to_dedicated_storage(tmp_path):
    """What the build script makes of /opt/stratux/www/playback."""
    storage = tmp_path / "usb"
    storage.mkdir()
    link = tmp_path / "playback"
    link.symlink_to(storage)
    r = Recorder(config(link), RATE)
    r.start()
    r.feed(SECOND, True, 118_700)
    r.close()
    assert len(wavs(storage)) == 1


def test_unmounted_storage_is_a_fault_and_writes_nowhere_else(tmp_path):
    """The link is there and the USB stick is not. Audio must not land on the root
    filesystem, and recording must resume once the stick is back."""
    storage = tmp_path / "usb"
    link = tmp_path / "playback"
    link.symlink_to(storage)
    r = Recorder(config(link), RATE)
    r.start()
    r.feed(SECOND, True, 118_700)
    assert wait_until(lambda: r.fault)
    r.end_segment()
    assert wait_until(lambda: r._q.empty())
    assert not storage.exists()
    storage.mkdir()
    r.feed(SECOND, True, 118_700)
    assert wait_until(lambda: not r.fault)
    r.close()
    assert len(wavs(storage)) == 1


def test_producer_never_blocks_when_the_disk_stalls(tmp_path):
    r = Recorder(config(tmp_path), RATE)               # writer thread never started
    for _ in range(1000):
        r.feed(b"\0" * 1024, True, 118_700)
    assert r.dropped > 0
    assert r.active


def test_close_finishes_the_open_file(tmp_path):
    r = recorder(tmp_path)
    r.feed(SECOND, True, 118_700)
    r.close()
    assert seconds_of(tmp_path / wavs(tmp_path)[0]) == 1.0
