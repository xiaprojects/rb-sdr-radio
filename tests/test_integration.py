"""The real daemon, end to end: real sockets, real child player, real files.

Only the dongle is replaced. The bridge is the mock that copies RB-01's behaviour, so
a payload RB-01 would discard is discarded here too and fails the run.
"""

import asyncio
import json
import stat
import time
import wave

import aiohttp
import numpy as np
import pytest
from aiohttp.test_utils import TestServer

from rbsdrradio import daemon, synth
from rbsdrradio.dsp import FS_HZ, TUNER_OFFSET_HZ
from rbsdrradio.sdr import FakeDevice
from tools.mock_bridge import REJECTED, STATE, build_app


class Air:
    """Replays IQ at real time, as a dongle would."""

    def __init__(self):
        self.signal = (synth.am(1.0, -30.0) + synth.noise(1.0, -60.0)).astype(np.complex64)
        self.quiet = synth.noise(1.0, -60.0).astype(np.complex64)
        self.on_air = True
        self.position = 0
        self.devices = []

    def read(self, n):
        time.sleep(n / FS_HZ)
        source = self.signal if self.on_air else self.quiet
        start = self.position % (source.size - n)
        self.position += n
        return source[start:start + n]

    def open(self, cfg):
        device = FakeDevice(self.read)
        self.devices.append(device)
        return device


@pytest.fixture
def harness(cfg, tmp_path):
    heard = tmp_path / "heard.raw"
    pids = tmp_path / "players"
    player = tmp_path / "player"
    player.write_text(f'#!/bin/sh\necho $$ >> "{pids}"\nexec cat >> "{heard}"\n')
    player.chmod(player.stat().st_mode | stat.S_IEXEC)

    class Harness:
        pass

    h = Harness()
    h.cfg = cfg
    h.player, h.heard, h.pids = str(player), heard, pids
    h.playback = tmp_path / "playback"
    return h


async def until(predicate, timeout=6.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    assert predicate()
    return True


class Running:
    def __init__(self, harness, air, grace=5.0):
        self.h = harness
        self.air = air
        self.grace = grace

    async def __aenter__(self):
        self.app = build_app()
        self.server = TestServer(self.app)
        await self.server.start_server()
        url = str(self.server.make_url("/bridge/float"))
        self.url = url
        self.session = aiohttp.ClientSession()
        return self

    async def start(self):
        cfg = self.h.cfg.with_overrides(
            bridge={"ws_url": url_ws(self.url), "rest_url": self.url, "ws_grace_s": self.grace},
            audio={"player": self.h.player}, radio={"debounce_ms": 50})
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(daemon.run(cfg, opener=self.air.open, stop=self.stop))

    async def __aexit__(self, *exc):
        if hasattr(self, "task"):
            self.stop.set()
            await self.task
        await self.session.close()
        await self.server.close()

    @property
    def state(self):
        return self.app[STATE]

    def field(self, key):
        return self.state.get(key)

    async def post(self, **values):
        async with self.session.post(self.url, data=json.dumps(values)) as r:
            assert r.status == 200


def url_ws(url):
    return url.replace("http", "ws") + "/ws"


def test_full_session(harness):
    """Everything an RB-01 session does, in order."""
    air = Air()

    async def main():
        async with Running(harness, air) as r:
            await r.post(sdrradiofreq=118.03, sdrradiovolume=40, sdrradiosquelch=10)
            await r.start()                    # the commands were there before we were

            await until(lambda: r.field("sdrradiofreqactive") == 118_030)
            await until(lambda: r.field("sdrradiostatus") == 4)
            assert air.devices[0].freq_hz == 118_025_000 + TUNER_OFFSET_HZ
            assert r.field("sdrradiosdr") == 1 and r.field("sdrradioaudio") == 1
            assert r.field("sdrradiosquelchopen") == 1
            assert r.field("sdrradiosnr") > 20
            await until(lambda: harness.heard.exists() and harness.heard.stat().st_size > 2000)

            # Volume 0: the sound device goes, the radio stays.
            await r.post(sdrradiovolume=0)
            await until(lambda: r.field("sdrradioaudio") == 0)
            assert r.field("sdrradiosdr") == 1 and r.field("sdrradiostatus") == 4
            size = harness.heard.stat().st_size
            await asyncio.sleep(0.2)
            assert harness.heard.stat().st_size == size

            # Back up: a fresh player.
            await r.post(sdrradiovolume=60)
            await until(lambda: r.field("sdrradioaudio") == 1)
            await until(lambda: len(harness.pids.read_text().split()) == 2)

            # Carrier drops: squelch shuts, and the file is finished.
            air.on_air = False
            await until(lambda: r.field("sdrradiostatus") == 3)
            await until(lambda: list(harness.playback.glob("radio_*_118030000.wav")))

            # Off: everything goes back.
            await r.post(sdrradiofreq=0)
            await until(lambda: r.field("sdrradiosdr") == 0)
            await until(lambda: r.field("sdrradiostatus") == 0)
            assert air.devices[0].closed
            assert r.field("sdrradioaudio") == 0 and r.field("sdrradiofreqactive") == 0

            # And on again, reopening the dongle.
            await r.post(sdrradiofreq=121.5)
            await until(lambda: r.field("sdrradiofreqactive") == 121_500)
            await until(lambda: r.field("sdrradiosdr") == 1)
            assert len(air.devices) == 2

            assert r.app[REJECTED] == []        # RB-01 would have kept every POST
            return dict(r.state)

    final = asyncio.run(main())
    assert all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in final.values())

    recording = next(harness.playback.glob("radio_*_118030000.wav"))
    with wave.open(str(recording)) as w:
        assert w.getframerate() == 16_000 and w.getnframes() > 1600


def test_invalid_frequency_flags_and_holds_no_dongle(harness):
    air = Air()

    async def main():
        async with Running(harness, air) as r:
            await r.start()
            await until(lambda: r.field("sdrradiostatus") == 0)
            await r.post(sdrradiofreq=142.0)
            await until(lambda: r.field("sdrradiofault") == 1)
            assert air.devices == [] and r.field("sdrradiosdr") == 0

    asyncio.run(main())


def test_sound_device_failure_does_not_stop_the_radio(harness):
    air = Air()
    harness.player = "/nonexistent/aplay"

    async def main():
        async with Running(harness, air) as r:
            await r.post(sdrradiofreq=118.7, sdrradiosquelch=10)
            await r.start()
            await until(lambda: r.field("sdrradiostatus") == 4)
            await until(lambda: r.field("sdrradiofault") == 4)
            assert r.field("sdrradiosdr") == 1

    asyncio.run(main())


def test_no_dongle_is_a_status_not_a_crash(harness):
    from rbsdrradio.sdr import SdrError

    class NoDongle(Air):
        def open(self, cfg):
            raise SdrError("no RTL-SDR device found")

    async def main():
        async with Running(harness, NoDongle()) as r:
            await r.post(sdrradiofreq=118.7)
            await r.start()
            await until(lambda: r.field("sdrradiostatus") == 1)
            assert r.field("sdrradiofault") == 2
            assert not r.task.done()

    asyncio.run(main())


def test_bridge_loss_releases_and_return_restores(harness):
    air = Air()

    async def main():
        async with Running(harness, air, grace=0.2) as r:
            await r.post(sdrradiofreq=118.7, sdrradiovolume=40)
            await r.start()
            await until(lambda: r.field("sdrradiosdr") == 1)
            port = r.server.port
            state = dict(r.state)
            await r.server.close()
            await until(lambda: air.devices[0].closed)

            # RB-01 comes back with its map intact and says nothing on the socket.
            app = build_app()
            app[STATE].update(state)
            server = TestServer(app, port=port)
            await server.start_server()
            try:
                await until(lambda: len(air.devices) == 2, timeout=10.0)
                await until(lambda: app[STATE].get("sdrradiosdr") == 1)
            finally:
                r.stop.set()
                await r.task
                await server.close()
            del r.task

    asyncio.run(main())


def test_shutdown_tells_rb01_and_finishes_the_file(harness):
    air = Air()

    async def main():
        async with Running(harness, air) as r:
            await r.post(sdrradiofreq=118.7, sdrradiovolume=0)
            await r.start()
            await until(lambda: r.field("sdrradiostatus") == 4)
            await asyncio.sleep(0.7)
            r.stop.set()
            await r.task
            del r.task
            assert r.field("sdrradiostatus") == 0 and r.field("sdrradiosdr") == 0
            assert air.devices[0].closed

    asyncio.run(main())
    assert len(list(harness.playback.glob("radio_*.wav"))) == 1
    assert list((harness.playback / ".incomplete").iterdir()) == []
