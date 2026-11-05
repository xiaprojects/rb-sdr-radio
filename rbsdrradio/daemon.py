"""The daemon: wires the parts together and does nothing else."""

from __future__ import annotations

import asyncio
import logging
import signal

import aiohttp

from rbsdrradio import __version__
from rbsdrradio.audio import AlsaSink
from rbsdrradio.bridge_client import BridgeClient
from rbsdrradio.controller import Controller
from rbsdrradio.dsp import AM_AUDIO_HZ
from rbsdrradio.publisher import Publisher
from rbsdrradio.recorder import Recorder
from rbsdrradio.sdnotify import SdNotify
from rbsdrradio.sdr import RtlDevice, SdrReader

__all__ = ["run"]

log = logging.getLogger("rbsdrradio")

_TICK_S = 0.1


async def run(cfg, opener=RtlDevice, stop: asyncio.Event | None = None) -> None:
    """Run until `stop` is set or a signal arrives."""
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    sd = SdNotify()
    reader = SdrReader(cfg.sdr, opener)
    audio = AlsaSink(cfg.audio, AM_AUDIO_HZ)
    recorder = Recorder(cfg.record, AM_AUDIO_HZ)
    recorder.start()

    timeout = aiohttp.ClientTimeout(total=cfg.bridge["http_timeout_s"])
    async with aiohttp.ClientSession(timeout=timeout) as session:
        url = cfg.bridge["rest_url"]

        async def post(payload: dict) -> None:
            async with session.post(url, json=payload) as response:
                if response.status >= 400:
                    raise ConnectionError(f"HTTP {response.status}")

        async def fetch_state() -> str:
            async with session.get(url) as response:
                return await response.text()

        publisher = Publisher(cfg.publish, post)
        controller = Controller(cfg, reader, audio, recorder, publisher, notify=sd.watchdog)
        client = BridgeClient(cfg.bridge, controller.apply, fetch_state,
                              on_connect=controller.on_bridge_connected,
                              on_disconnect=controller.on_bridge_disconnected)

        async def ticker() -> None:
            last = ""
            while True:
                controller.tick()
                status = f"{controller.state}: {controller.reason}"
                if status != last:
                    sd.status(status)
                    last = status
                await asyncio.sleep(_TICK_S)

        async def pump() -> None:
            while True:
                got = False
                for block, gap in reader.blocks():
                    controller.push(block, gap)
                    got = True
                await asyncio.sleep(0.0 if got else (0.005 if reader.is_open else 0.05))

        # A task that dies takes the daemon down with it: under systemd that is a
        # restart, where a silently dead ticker would be a radio that stops
        # answering while still feeding nothing (rb-vor F3).
        failures: list[BaseException] = []

        def watch(task: asyncio.Task) -> None:
            if not task.cancelled() and task.exception() is not None:
                log.error("%s task failed: %r", task.get_name(), task.exception())
                failures.append(task.exception())
                stop.set()

        tasks = [asyncio.create_task(coro, name=name) for coro, name in
                 ((client.run(), "bridge"), (publisher.run(), "publisher"),
                  (ticker(), "ticker"), (pump(), "pump"))]
        for task in tasks:
            task.add_done_callback(watch)

        sd.ready("waiting for a frequency")
        log.info("rb-sdr-radio %s, config %s, bridge %s", __version__, cfg.source,
                 cfg.bridge["ws_url"])
        try:
            await stop.wait()
        finally:
            sd.stopping()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            controller.shutdown()
            await publisher.send_now(controller.payload())
            recorder.close()
    if failures:
        raise failures[0]
