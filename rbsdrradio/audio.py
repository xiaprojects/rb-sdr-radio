"""ALSA output through an `aplay` child process.

A child process rather than an in-process binding, for two reasons:

* Release is absolute. "Volume zero frees the sound device" comes down to the
  process exiting, and the kernel then closes the PCM whatever state ALSA was in.
* A stuck sound device can only stall the child. The write here never blocks, so
  it cannot hold up the SDR.

Latency stays bounded: the pipe is cut to one page, and a block that does not fit
is dropped. With the default 64 kB pipe a sound card running a little slower than
the dongle would quietly build up two seconds of delay on air traffic control.

Volume is applied to the samples, not to a mixer control: the sound device is
shared with voice alerts, and the radio has no business moving their level.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import subprocess
import threading
from collections.abc import Callable

__all__ = ["AlsaSink", "AudioError", "volume_gain"]

log = logging.getLogger("rbsdrradio.audio")

#: fcntl.F_SETPIPE_SZ, spelled out because it needs Python 3.10 and Linux.
_F_SETPIPE_SZ = 1031
_PIPE_BYTES = 4096
_STOP_TIMEOUT_S = 1.0


class AudioError(RuntimeError):
    """The player could not be started or has died."""


def volume_gain(volume: int) -> float:
    """0-100 to a sample gain. Squared, because loudness is not linear in amplitude."""
    return (max(0, min(100, volume)) / 100.0) ** 2


class AlsaSink:
    def __init__(self, cfg_audio: dict, rate_hz: int,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen):
        self.cfg = cfg_audio
        self.rate_hz = rate_hz
        self.popen = popen
        self._proc: subprocess.Popen | None = None
        self._fd: int | None = None
        self.dropped = 0
        self.last_error = ""

    @property
    def is_open(self) -> bool:
        return self._proc is not None

    def command(self) -> list[str]:
        buffer_us = int(self.cfg["buffer_ms"] * 1000)
        return [self.cfg["player"], "-q", "-D", self.cfg["device"], "-t", "raw",
                "-f", "S16_LE", "-c", "1", "-r", str(self.rate_hz),
                f"--buffer-time={buffer_us}", f"--period-time={buffer_us // 4}"]

    def open(self) -> None:
        if self._proc is not None:
            return
        try:
            proc = self.popen(self.command(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, close_fds=True)
        except OSError as e:
            self.last_error = str(e)
            raise AudioError(f"cannot start {self.cfg['player']}: {e}") from e
        self._proc = proc
        self._fd = proc.stdin.fileno()
        with contextlib.suppress(OSError):           # Linux only; the drop logic still works
            fcntl.fcntl(self._fd, _F_SETPIPE_SZ, _PIPE_BYTES)
        os.set_blocking(self._fd, False)
        threading.Thread(target=self._drain_stderr, args=(proc,), name="rbsdrradio-aplay",
                         daemon=True).start()
        log.info("audio open: %s on %s", self.cfg["player"], self.cfg["device"])

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        """Keep the stderr pipe empty, or `aplay` blocks on its own underrun messages."""
        with contextlib.suppress(ValueError, OSError):
            for line in proc.stderr:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self.last_error = text
                    log.debug("aplay: %s", text)

    def write(self, pcm: bytes) -> None:
        """Hand one block to the player. Never blocks. Raises `AudioError` if it died."""
        if self._fd is None:
            return
        try:
            os.write(self._fd, pcm)
        except BlockingIOError:
            self.dropped += 1
        except OSError as e:
            detail = self.last_error or str(e)
            self.close()
            raise AudioError(f"audio output died: {detail}") from e

    def poll(self) -> None:
        """Raise `AudioError` if the player exited on its own (device busy, unplugged)."""
        if self._proc is not None and self._proc.poll() is not None:
            detail = self.last_error or f"exit status {self._proc.returncode}"
            self.close()
            raise AudioError(f"audio output died: {detail}")

    def close(self) -> None:
        """Release the sound device: on return the player has gone. Idempotent."""
        proc, self._proc, self._fd = self._proc, None, None
        if proc is None:
            return
        with contextlib.suppress(OSError):
            proc.stdin.close()
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.info("audio released")
