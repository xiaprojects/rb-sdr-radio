"""ALSA output. A real child process throughout, because release is the point."""

import os
import shutil
import stat
import subprocess
import threading
import time

import pytest
from conftest import wait_until

from rbsdrradio.audio import AlsaSink, AudioError, volume_gain

AUDIO = {"device": "default", "player": "aplay", "buffer_ms": 150, "retry_s": 5.0}


def script(tmp_path, body, name="player"):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def sink_for(player, rate=16000):
    return AlsaSink({**AUDIO, "player": player}, rate)


def test_command_line_is_what_aplay_expects():
    cmd = AlsaSink({**AUDIO, "device": "plughw:1,0"}, 16000).command()
    assert cmd[:4] == ["aplay", "-q", "-D", "plughw:1,0"]
    for pair in (["-t", "raw"], ["-f", "S16_LE"], ["-c", "1"], ["-r", "16000"]):
        i = cmd.index(pair[0])
        assert cmd[i:i + 2] == pair
    assert "--buffer-time=150000" in cmd


def test_audio_reaches_the_player(tmp_path):
    out = tmp_path / "heard"
    sink = sink_for(script(tmp_path, f'exec cat > "{out}"'))
    sink.open()
    sink.write(b"\x01\x02" * 256)
    assert wait_until(lambda: out.exists() and out.stat().st_size == 512)
    sink.close()
    assert out.read_bytes() == b"\x01\x02" * 256


def test_close_really_ends_the_process(tmp_path):
    sink = sink_for(script(tmp_path, "exec sleep 60"))
    sink.open()
    pid = sink._proc.pid
    sink.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert sink.is_open is False


def test_close_kills_a_player_that_ignores_sigterm(tmp_path):
    sink = sink_for(script(tmp_path, "trap '' TERM\nexec sleep 60"))
    sink.open()
    pid = sink._proc.pid
    time.sleep(0.1)                                # let the trap install
    sink.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_close_twice_is_safe(tmp_path):
    sink = sink_for(script(tmp_path, "exec cat > /dev/null"))
    sink.open()
    sink.close()
    sink.close()


def test_open_twice_keeps_one_player(tmp_path):
    sink = sink_for(script(tmp_path, "exec cat > /dev/null"))
    sink.open()
    first = sink._proc.pid
    sink.open()
    assert sink._proc.pid == first
    sink.close()


def test_write_when_closed_is_a_no_op():
    AlsaSink(AUDIO, 16000).write(b"\0\0")


def test_a_stuck_player_never_blocks_the_caller(tmp_path):
    """The whole design: the sound device can hang, the radio must not."""
    sink = sink_for(script(tmp_path, "exec sleep 60"))
    sink.open()

    def write_lots():
        for _ in range(200):
            sink.write(b"\0" * 1024)

    writer = threading.Thread(target=write_lots, daemon=True)  # a regression must fail, not hang
    writer.start()
    writer.join(timeout=2.0)
    blocked = writer.is_alive()
    sink.close()
    assert not blocked
    assert sink.dropped > 0


def test_pipe_is_cut_down_so_delay_stays_bounded(tmp_path):
    """64 kB at 16 kHz would be two seconds of air traffic control, late."""
    sink = sink_for(script(tmp_path, "exec sleep 60"))
    sink.open()
    blocks = 0
    while sink.dropped == 0 and blocks < 100:
        sink.write(b"\0" * 1024)
        blocks += 1
    sink.close()
    seconds_buffered = (blocks - 1) * 1024 / (16000 * 2)
    assert seconds_buffered <= 0.15


def test_player_has_a_short_buffer_of_its_own():
    """aplay's default buffer would add half a second on top of the pipe."""
    command = AlsaSink(AUDIO, 16000).command()
    assert any(arg == "--buffer-time=150000" for arg in command)


def test_missing_player_is_an_audio_error():
    with pytest.raises(AudioError, match="cannot start"):
        sink_for("/nonexistent/aplay").open()


def test_player_exit_is_seen_and_reports_why(tmp_path):
    sink = sink_for(script(tmp_path, 'echo "audio open error: Device or resource busy" >&2\n'
                                     "exit 1"))
    sink.open()
    wait_until(lambda: sink._proc.poll() is not None)
    wait_until(lambda: "busy" in sink.last_error)
    with pytest.raises(AudioError, match="busy"):
        sink.poll()
    assert sink.is_open is False


def test_write_to_a_dead_player_raises_instead_of_killing_the_daemon(tmp_path):
    sink = sink_for(script(tmp_path, "exit 0"))
    sink.open()
    wait_until(lambda: sink._proc.poll() is not None)
    with pytest.raises(AudioError):
        for _ in range(3):
            sink.write(b"\0" * 1024)
    assert sink.is_open is False


def test_volume_curve():
    assert volume_gain(0) == 0.0
    assert volume_gain(100) == 1.0
    assert 0.0 < volume_gain(50) < 0.5
    assert volume_gain(150) == 1.0 and volume_gain(-5) == 0.0


@pytest.mark.alsa
def test_real_aplay_on_the_alsa_null_device():
    """The genuine binary and libasound, on a device that makes no sound."""
    if shutil.which("aplay") is None:
        pytest.skip("aplay not installed")
    sink = AlsaSink({**AUDIO, "device": "null"}, 16000)
    sink.open()
    for _ in range(10):
        sink.write(b"\0" * 1024)
        time.sleep(0.02)
    sink.poll()
    assert sink.is_open
    sink.close()


@pytest.mark.alsa
def test_real_aplay_reports_a_device_that_does_not_exist():
    if shutil.which("aplay") is None:
        pytest.skip("aplay not installed")
    sink = AlsaSink({**AUDIO, "device": "no_such_device_here"}, 16000)
    sink.open()
    assert sink._proc.wait(timeout=5) != 0
    with pytest.raises(AudioError):
        wait_until(lambda: sink.last_error != "")
        sink.poll()


def test_no_zombie_left_behind(tmp_path):
    sink = sink_for(script(tmp_path, "exec cat > /dev/null"))
    sink.open()
    pid = sink._proc.pid
    sink.close()
    children = subprocess.run(["ps", "--ppid", str(os.getpid()), "-o", "pid="],
                              capture_output=True, text=True).stdout.split()
    assert str(pid) not in children
