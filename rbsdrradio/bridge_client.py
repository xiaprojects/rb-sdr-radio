"""WebSocket consumer for RB-01's `/bridge/float/ws`.

Behaviour measured against RB-01's source (`managementinterface.go`,
`uibroadcast.go`), and all of it is unusual enough to state:

* **The socket is push-only and says nothing on connect.** It carries the whole
  float map, but only after some POST changes a value. Without the `GET` here a
  restarted daemon would sit deaf to a frequency RB-01 set long ago, until an
  unrelated addon happened to change something.
* **Frames contain everything**: every addon's keys, and our own output echoed
  back. Only the keys in `CONSUMED` are read.
* **No ping, no pong, no close frame** (rb-vor A29). The library keepalive would
  tear a healthy link down on a timer, so it is off, and kernel TCP keepalive
  notices a peer that vanished, since the peer's kernel answers those probes.
* **`null` cannot be sent**: RB-01's map holds float32, so "off" is 0.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["ParsedFrame", "parse_frame", "BridgeClient", "enable_tcp_keepalive", "CONSUMED"]

log = logging.getLogger("rbsdrradio.bridge")

#: Bridge key -> name handed to `on_values`.
CONSUMED = {"sdrradiofreq": "freq", "sdrradiovolume": "volume",
            "sdrradiosquelch": "squelch", "sdrradiorecord": "record"}

CLOSE_TIMEOUT_S = 0.5
KEEPALIVE_IDLE_S = 10
KEEPALIVE_INTERVAL_S = 5
KEEPALIVE_COUNT = 3


@dataclass(frozen=True)
class ParsedFrame:
    """`values` holds only keys present in the frame: absent means no change."""

    values: dict[str, float | None] = field(default_factory=dict)
    error: str | None = None


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def parse_frame(text: str | bytes) -> ParsedFrame:
    """Parse one bridge frame. Never raises: a bad frame must not drop the link."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            return ParsedFrame(error="frame is not valid UTF-8")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        return ParsedFrame(error=f"invalid JSON: {e}")
    if not isinstance(data, dict):
        return ParsedFrame(error=f"expected an object, got {type(data).__name__}")

    values: dict[str, float | None] = {}
    for key, name in CONSUMED.items():
        if key not in data:
            continue
        if data[key] is None:
            values[name] = None
            continue
        number = _num(data[key])
        if number is None:
            return ParsedFrame(error=f"{key} is not a number: {data[key]!r}")
        values[name] = number
    return ParsedFrame(values)


def enable_tcp_keepalive(sock) -> bool:
    """Turn on kernel keepalive probes. Never raises."""
    if sock is None:
        return False
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", KEEPALIVE_IDLE_S),
                            ("TCP_KEEPINTVL", KEEPALIVE_INTERVAL_S),
                            ("TCP_KEEPCNT", KEEPALIVE_COUNT)):
            opt = getattr(socket, name, None)
            if opt is not None:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
    except OSError as e:
        log.warning("TCP keepalive not enabled (%s)", e)
        return False
    return True


class BridgeClient:
    """Async WebSocket client with exponential backoff."""

    def __init__(self, cfg_bridge: dict, on_values: Callable[[dict], None],
                 fetch_state: Callable[[], Awaitable[str]] | None = None,
                 on_connect: Callable[[], None] | None = None,
                 on_disconnect: Callable[[], None] | None = None):
        self.cfg = cfg_bridge
        self.on_values = on_values
        self.fetch_state = fetch_state
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.connected = False
        self.frames = 0
        self.bad_frames = 0

    def handle_text(self, text: str | bytes) -> ParsedFrame:
        """Apply one frame. Separated from the socket so tests need no server."""
        frame = parse_frame(text)
        self.frames += 1
        if frame.error is not None:
            self.bad_frames += 1
            log.warning("dropping bridge frame: %s", frame.error)
        elif frame.values:
            self.on_values(frame.values)
        return frame

    async def _sync_state(self) -> None:
        """Read what RB-01 already holds. The socket will not say until it changes."""
        if self.fetch_state is None:
            return
        try:
            self.handle_text(await self.fetch_state())
        except asyncio.CancelledError:
            raise
        except Exception as e:                       # noqa: BLE001
            log.warning("could not read bridge state: %s", e)

    async def run(self) -> None:
        import websockets
        backoff = self.cfg["ws_reconnect_min_s"]
        while True:
            try:
                async with websockets.connect(self.cfg["ws_url"], ping_interval=None,
                                              close_timeout=CLOSE_TIMEOUT_S) as ws:
                    enable_tcp_keepalive(ws.transport.get_extra_info("socket"))
                    self.connected = True
                    backoff = self.cfg["ws_reconnect_min_s"]
                    if self.on_connect:
                        self.on_connect()
                    log.info("bridge connected: %s", self.cfg["ws_url"])
                    # Subscribed first, so a change during the GET cannot be missed.
                    await self._sync_state()
                    async for message in ws:
                        self.handle_text(message)
            except asyncio.CancelledError:
                raise
            except Exception as e:                   # noqa: BLE001
                log.warning("bridge connection lost: %s", e)
            finally:
                if self.connected:
                    self.connected = False
                    if self.on_disconnect:
                        self.on_disconnect()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, self.cfg["ws_reconnect_max_s"])
