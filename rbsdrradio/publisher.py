"""Status publishing: `POST /bridge/float`, RB-SDR-RADIO-IF v1.0.

RB-01 decodes the body into `map[string]float32`, all or nothing: a single string,
boolean or nested value and the whole POST is discarded with only a line in its
log. Hence the one type here is `dict[str, int]`.

Split as in rb-vor: `PublishPolicy` decides what goes out and is pure; `Publisher`
gets it there. RB-01 logs and re-broadcasts the whole bridge on every change, so
state changes go at once while the two meters are rate limited and need a real
move. Otherwise a noisy channel would flood every WebSocket client on the bridge.

The periodic repeat rebuilds RB-01's map after RB-01 restarts. It is not a liveness
signal: RB-01 re-broadcasts only what changed, so a repeat reaches no client. That
gap is TD2 in `docs/tech-debt.md`, to be settled for all radios together.

Drop, never queue: the mailbox holds one payload, the newest, so nothing stale can
pile up behind a slow bridge, and a failed send is retried with the current state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

__all__ = ["Publisher", "PublishPolicy", "FIELDS", "METERS"]

log = logging.getLogger("rbsdrradio.publisher")

METERS = ("sdrradiosignal", "sdrradiosnr")

#: Everything on the wire. The interface document is written from this; a test
#: fails if the two disagree.
FIELDS = ("sdrradiostatus", "sdrradiofault", "sdrradiofreqactive", "sdrradiovolumeactive",
          "sdrradiosquelchactive", "sdrradiorecordactive", "sdrradiosquelchopen", *METERS,
          "sdrradiosdr", "sdrradioaudio", "sdrradiorecording")


class PublishPolicy:
    """Whether a payload goes on the wire now. Pure and clock-injected."""

    def __init__(self, cfg_publish: dict, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg_publish
        self.clock = clock
        self._last: dict[str, int] | None = None
        self._sent_at = 0.0

    def decide(self, payload: dict[str, int]) -> bool:
        now = self.clock()
        last = self._last
        send = (last is None
                or any(payload[k] != last[k] for k in payload if k not in METERS)
                or self._meter_moved(payload, last, now)
                or self._keepalive_due(now))
        if send:
            self._last, self._sent_at = dict(payload), now
        return send

    def _meter_moved(self, payload: dict, last: dict, now: float) -> bool:
        if now - self._sent_at < self.cfg["meter_period_s"]:
            return False
        band = self.cfg["meter_deadband_db"]
        return any(payload[k] != last[k] and abs(payload[k] - last[k]) >= band for k in METERS)

    def _keepalive_due(self, now: float) -> bool:
        keepalive = self.cfg["keepalive_s"]
        return bool(keepalive) and now - self._sent_at >= keepalive


class Publisher:
    """The controller's publisher port, draining into an async `post` callable."""

    def __init__(self, cfg_publish: dict, post: Callable[[dict], Awaitable[None]],
                 clock: Callable[[], float] = time.monotonic):
        self.policy = PublishPolicy(cfg_publish, clock)
        self.post = post
        self.failures = 0
        self._pending: dict[str, int] | None = None
        self._wake = asyncio.Event()
        self._backoff = 0.0

    def publish(self, payload: dict[str, int]) -> None:
        if self.policy.decide(payload):
            self._pending = payload
            self._wake.set()

    async def run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._pending is not None:
                payload, self._pending = self._pending, None
                try:
                    await self.post(payload)
                    self._backoff = 0.0
                except asyncio.CancelledError:
                    raise
                except Exception as e:               # noqa: BLE001 - transport agnostic
                    self.failures += 1
                    if self._backoff == 0.0:
                        log.warning("bridge POST failed: %s", e)
                    if self._pending is None:
                        self._pending = payload
                    self._backoff = min(max(self._backoff * 2.0, 0.5), 30.0)
                    await asyncio.sleep(self._backoff)

    async def send_now(self, payload: dict[str, int]) -> None:
        """Last words at shutdown: best effort, past any backoff."""
        try:
            await self.post(payload)
        except Exception as e:                       # noqa: BLE001
            log.debug("final POST failed: %s", e)
