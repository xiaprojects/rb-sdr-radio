"""Recording: one WAV per transmission, into the RB-01 playback folder.

The folder is `/opt/stratux/www/playback`, on an RB image a symlink to dedicated USB
storage made by the builder or the build script. Three things shape this module.

* **The storage is finite and shared.** A radio left on must not fill it, so
  `max_total_mb` and `min_free_mb` are hard limits and the oldest recording goes
  first. rbvoice writes to the same folder, so only names matching our own pattern
  are ever deleted.
* **Aircraft power goes without warning.** A WAV header is only correct at the end.
  A segment therefore grows in `.incomplete/` on the same filesystem, is renamed
  into place once finished, and whatever a power cut leaves there is repaired at
  the next start.
* **Writes can stall.** All file work happens on a thread behind a bounded queue, so
  a slow SD card costs recorded audio at worst, never live audio.

File names follow what RB-01's `radio.js` parses:
`radio_YYYYMMDD_HHMMSS_<hertz>.wav`, four `_` fields, the first six digits of the
last read as the frequency. The digits are the channel *name*, not the tuned
frequency, so the UI shows the 118.030 the pilot selected rather than 118.025.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import re
import shutil
import struct
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

__all__ = ["Recorder", "wav_header", "segment_name", "repair_wav"]

log = logging.getLogger("rbsdrradio.recorder")

INCOMPLETE_DIR = ".incomplete"
_QUEUE_BLOCKS = 256
_FSYNC_S = 5.0
_HEADER_BYTES = 44
_BYTES_PER_SAMPLE = 2
_NAME_ATTEMPTS = 60


def wav_header(rate_hz: int, data_bytes: int) -> bytes:
    """Canonical 44-byte RIFF header for mono S16_LE."""
    return (b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate_hz, rate_hz * _BYTES_PER_SAMPLE,
                          _BYTES_PER_SAMPLE, 16)
            + b"data" + struct.pack("<I", data_bytes))


def segment_name(prefix: str, when: datetime, channel_khz: int) -> str:
    # No ':' anywhere: a USB stick may well be FAT, which cannot hold it.
    return f"{prefix}_{when:%Y%m%d_%H%M%S}_{channel_khz * 1000}.wav"


def repair_wav(path: Path, rate_hz: int) -> float:
    """Write the header a finished file should have. Returns the audio seconds."""
    size = path.stat().st_size
    data = max(0, size - _HEADER_BYTES)
    data -= data % _BYTES_PER_SAMPLE
    with open(path, "r+b") as f:
        f.write(wav_header(rate_hz, data))
        f.truncate(_HEADER_BYTES + data)
        f.flush()
        os.fsync(f.fileno())
    return data / (rate_hz * _BYTES_PER_SAMPLE)


class Recorder:
    """Feed it every audio block; it decides what becomes a file."""

    def __init__(self, cfg: dict, rate_hz: int,
                 clock: Callable[[], float] = time.monotonic,
                 utcnow: Callable[[], datetime] = lambda: datetime.now(tz=UTC)):
        self.cfg = cfg
        self.rate_hz = rate_hz
        self.clock = clock
        self.utcnow = utcnow
        self.dir = Path(cfg["dir"])
        self._own = re.compile(rf"^{re.escape(cfg['prefix'])}_\d{{8}}_\d{{6}}_\d+\.wav$")
        self._q: queue.Queue = queue.Queue(maxsize=_QUEUE_BLOCKS)
        self._thread: threading.Thread | None = None
        self._channel: int | None = None
        self._bytes = 0
        self._last_open = 0.0
        self._file = None
        self._path: Path | None = None
        self._final_name = ""
        self._written = 0
        self._last_sync = 0.0
        self.fault = False
        self.dropped = 0
        self.saved = 0

    # -- producer side: called from the DSP path, never touches the disk ---- #

    @property
    def active(self) -> bool:
        return self._channel is not None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="rbsdrradio-rec", daemon=True)
            self._thread.start()

    def feed(self, pcm: bytes, gate_open: bool, channel_khz: int) -> None:
        now = self.clock()
        if self._channel is not None and channel_khz != self._channel:
            self.end_segment()
        if gate_open:
            if self._channel is None:
                self._channel, self._bytes = channel_khz, 0
                self._put(("begin", channel_khz, self.utcnow()))
            self._last_open = now
            self._bytes += len(pcm)
            self._put(("write", pcm))
            limit = self.cfg["max_segment_s"] * self.rate_hz * _BYTES_PER_SAMPLE
            if self._bytes >= limit:
                self.end_segment()
        elif self._channel is not None and now - self._last_open >= self.cfg["hold_s"]:
            # Closed for longer than a pause in an exchange: this one is over.
            self.end_segment()

    def end_segment(self) -> None:
        if self._channel is not None:
            self._channel = None
            self._put(("end",))

    def _put(self, item: tuple) -> None:
        try:
            self._q.put_nowait(item)
        except queue.Full:
            self.dropped += 1
            if item[0] != "write":            # losing an end would leave a file open
                self._evict_for(item)

    def _evict_for(self, item: tuple) -> None:
        with contextlib.suppress(queue.Empty, queue.Full):
            self._q.get_nowait()
            self._q.put_nowait(item)

    def close(self, timeout: float = 5.0) -> None:
        """Finish the open segment and stop. Blocks until the file is safely down."""
        self.end_segment()
        if self._thread is not None:
            with contextlib.suppress(queue.Full):        # a stuck card must not hang shutdown
                self._q.put(None, timeout=timeout)
            self._thread.join(timeout=timeout)
            self._thread = None

    # -- writer thread ------------------------------------------------------ #

    def _run(self) -> None:
        self._recover()
        while True:
            item = self._q.get()
            if item is None:
                self._finish()
                return
            try:
                if item[0] == "begin":
                    self._begin(*item[1:])
                elif item[0] == "write":
                    self._write(item[1])
                else:
                    self._finish()
            except OSError as e:
                self._fail(e)

    def _fail(self, error: OSError) -> None:
        if not self.fault:
            log.warning("recording to %s failed: %s", self.dir, error)
        self.fault = True
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None

    def _begin(self, channel_khz: int, when: datetime) -> None:
        self._finish()
        incomplete = self.dir / INCOMPLETE_DIR
        incomplete.mkdir(parents=True, exist_ok=True)
        if not self._make_room():
            raise OSError(f"less than {self.cfg['min_free_mb']} MB free")
        # Two transmissions in one second: a later second keeps the name parseable.
        for attempt in range(_NAME_ATTEMPTS):
            name = segment_name(self.cfg["prefix"], when + timedelta(seconds=attempt), channel_khz)
            if (self.dir / name).exists():
                continue
            try:
                self._file = open(incomplete / name, "xb")      # noqa: SIM115
            except FileExistsError:
                continue
            self._path, self._final_name = incomplete / name, name
            self._written = 0
            self._last_sync = self.clock()
            self._file.write(wav_header(self.rate_hz, 0))
            self.fault = False
            return
        raise OSError(f"no free file name for {channel_khz} kHz")

    def _write(self, pcm: bytes) -> None:
        if self._file is None:
            return
        self._file.write(pcm)
        self._written += len(pcm)
        now = self.clock()
        if now - self._last_sync >= _FSYNC_S:        # a power cut then costs seconds, not a file
            self._last_sync = now
            self._file.flush()
            os.fsync(self._file.fileno())

    def _finish(self) -> None:
        if self._file is None:
            return
        f, self._file = self._file, None
        f.close()
        self._install(self._path, self._final_name)
        self._prune()

    def _install(self, path: Path, name: str) -> None:
        seconds = repair_wav(path, self.rate_hz)
        if seconds < self.cfg["min_segment_s"]:
            path.unlink()
            return
        path.rename(self.dir / name)
        self.saved += 1
        log.info("recorded %s (%.1f s)", name, seconds)

    def _recover(self) -> None:
        """Finish what the last power cut interrupted."""
        incomplete = self.dir / INCOMPLETE_DIR
        try:
            for path in sorted(incomplete.glob("*.wav")):
                self._install(path, path.name)
        except OSError as e:
            log.debug("nothing recovered from %s: %s", incomplete, e)

    def _own_files(self) -> list[os.DirEntry]:
        with os.scandir(self.dir) as it:
            own = [e for e in it if self._own.match(e.name) and e.is_file()]
        return sorted(own, key=lambda e: e.stat().st_mtime)

    def _prune(self) -> None:
        own = self._own_files()
        total = sum(e.stat().st_size for e in own)
        limit = self.cfg["max_total_mb"] * 1_000_000
        while own and total > limit:
            oldest = own.pop(0)
            total -= oldest.stat().st_size
            os.unlink(oldest.path)

    def _make_room(self) -> bool:
        """True once the filesystem has `min_free_mb`, deleting our oldest if needed."""
        need = self.cfg["min_free_mb"] * 1_000_000
        own = self._own_files()
        while shutil.disk_usage(self.dir).free < need:
            if not own:
                return False
            os.unlink(own.pop(0).path)
        return True
