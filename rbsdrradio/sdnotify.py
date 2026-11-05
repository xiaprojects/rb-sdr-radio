"""Minimal sd_notify client for systemd `Type=notify` and `WatchdogSec`.

Carried over from rb-vor unchanged.

Hand-rolled rather than pulled from PyPI: the protocol is a datagram to the
socket named in `$NOTIFY_SOCKET`, and a dependency for twenty lines is a
dependency a remote builder has to obtain. Objective 4 -- a builder anywhere
must reach a working unit from the published instructions -- is easier to meet
with one fewer package.

Everything degrades to a no-op when `$NOTIFY_SOCKET` is unset, so the daemon
runs identically from a shell, in CI and under systemd.
"""

from __future__ import annotations

import logging
import os
import socket

__all__ = ["SdNotify"]

log = logging.getLogger("rbsdrradio.sdnotify")


class SdNotify:
    """No-op unless systemd provided a notify socket."""

    def __init__(self, address: str | None = None):
        self.address = address if address is not None else os.environ.get("NOTIFY_SOCKET")
        self._sock: socket.socket | None = None
        if self.address:
            try:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            except OSError as e:                      # pragma: no cover
                log.warning("sd_notify unavailable: %s", e)
                self._sock = None

    @property
    def enabled(self) -> bool:
        return self._sock is not None and bool(self.address)

    def _send(self, message: str) -> bool:
        if not self.enabled:
            return False
        addr = self.address
        # A leading '@' denotes an abstract namespace socket.
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        try:
            self._sock.sendto(message.encode("utf-8"), addr)
            return True
        except OSError as e:                          # pragma: no cover
            log.warning("sd_notify send failed: %s", e)
            return False

    def ready(self, status: str = "") -> bool:
        msg = "READY=1"
        if status:
            msg += f"\nSTATUS={status}"
        return self._send(msg)

    def watchdog(self) -> bool:
        return self._send("WATCHDOG=1")

    def status(self, text: str) -> bool:
        return self._send(f"STATUS={text}")

    def stopping(self) -> bool:
        return self._send("STOPPING=1")

    @staticmethod
    def watchdog_interval_s() -> float | None:
        """Half of `WATCHDOG_USEC`, which is the recommended ping period."""
        usec = os.environ.get("WATCHDOG_USEC")
        if not usec:
            return None
        try:
            return int(usec) / 2_000_000.0
        except ValueError:                            # pragma: no cover
            return None
