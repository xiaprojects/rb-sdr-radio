"""The bridge link, against a stand-in that behaves the way RB-01's code does."""

import asyncio
import json
import socket

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from rbsdrradio.bridge_client import (
    KEEPALIVE_IDLE_S,
    BridgeClient,
    enable_tcp_keepalive,
    parse_frame,
)
from tools.mock_bridge import REJECTED, STATE, build_app

CFG = {"ws_url": "", "ws_reconnect_min_s": 0.01, "ws_reconnect_max_s": 0.05}


def client(received, **kwargs):
    return BridgeClient(dict(CFG), received.append, **kwargs)


# -- parsing ------------------------------------------------------------------- #

def test_takes_the_commands():
    frame = parse_frame(json.dumps({"sdrradiofreq": 118.7, "sdrradiovolume": 40,
                                    "sdrradiosquelch": 10, "sdrradiorecord": 1}))
    assert frame.values == {"freq": 118.7, "volume": 40.0, "squelch": 10.0, "record": 1.0}


def test_absent_means_no_change():
    assert parse_frame('{"sdrradiovolume": 40}').values == {"volume": 40.0}


def test_ignores_everything_else_on_the_bridge():
    """Other addons, and our own output echoed back."""
    frame = parse_frame('{"vorfreq": 109.4, "ias_mps": 40, "sdrradiostatus": 4,'
                        ' "sdrradiofreqactive": 118700, "sdrradiosignal": -30}')
    assert frame.error is None and frame.values == {}


def test_null_is_kept_distinct_from_absent():
    assert parse_frame('{"sdrradiofreq": null}').values == {"freq": None}


@pytest.mark.parametrize("text", ['{"sdrradiofreq": "118.7"}', '{"sdrradiofreq": true}',
                                  '{"sdrradiovolume": []}', '{"sdrradiofreq": NaN}',
                                  '{"sdrradiofreq": Infinity}'])
def test_malformed_value_drops_the_frame(text):
    frame = parse_frame(text)
    assert frame.error is not None and frame.values == {}


@pytest.mark.parametrize("text", ["not json", "[1,2]", "3", b"\xff\xfe"])
def test_bad_frames_do_not_raise(text):
    assert parse_frame(text).error is not None


def test_bad_frame_changes_nothing_and_is_counted():
    received = []
    c = client(received)
    c.handle_text("garbage")
    assert received == [] and c.bad_frames == 1


def test_a_frame_with_none_of_our_keys_is_not_a_command():
    received = []
    client(received).handle_text('{"vorfreq": 109.4}')
    assert received == []


# -- socket behaviour ---------------------------------------------------------- #

def test_keepalive_is_set_on_a_real_socket():
    sock = socket.socket()
    try:
        assert enable_tcp_keepalive(sock) is True
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == KEEPALIVE_IDLE_S
    finally:
        sock.close()


def test_keepalive_tolerates_no_socket():
    assert enable_tcp_keepalive(None) is False


class Bridge:
    """The mock bridge on a real port."""

    async def __aenter__(self):
        self.app = build_app()
        self.server = TestServer(self.app)
        await self.server.start_server()
        self.ws_url = str(self.server.make_url("/bridge/float/ws")).replace("http", "ws")
        self.rest_url = str(self.server.make_url("/bridge/float"))
        return self

    async def __aexit__(self, *exc):
        await self.server.close()

    @property
    def state(self):
        return self.app[STATE]

    async def post(self, session, **values):
        async with session.post(self.rest_url, data=json.dumps(values)) as r:
            assert r.status == 200


async def until(predicate, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def test_commands_set_before_we_connected_are_picked_up():
    """RB-01 says nothing on connect. Without the GET a restarted daemon stays deaf
    to a frequency set long ago. This is a gap in rb-vor's client."""
    async def main():
        import aiohttp
        async with Bridge() as bridge, aiohttp.ClientSession() as session:
            await bridge.post(session, sdrradiofreq=118.7, sdrradiovolume=30)

            async def fetch():
                async with session.get(bridge.rest_url) as r:
                    return await r.text()

            received = []
            c = BridgeClient({**CFG, "ws_url": bridge.ws_url}, received.append, fetch)
            task = asyncio.create_task(c.run())
            await until(lambda: received)
            task.cancel()                          # first, or a failure hangs the server
            await asyncio.gather(task, return_exceptions=True)
            assert received, "the commands already on the bridge never arrived"
            assert received[0]["freq"] == pytest.approx(118.7, abs=1e-4)
            assert received[0]["volume"] == 30.0

    asyncio.run(main())


def test_changes_arrive_over_the_socket():
    async def main():
        import aiohttp
        async with Bridge() as bridge, aiohttp.ClientSession() as session:
            received = []
            c = BridgeClient({**CFG, "ws_url": bridge.ws_url}, received.append)
            task = asyncio.create_task(c.run())
            await until(lambda: c.connected)
            await asyncio.sleep(0.05)
            before = list(received)                  # the bridge volunteers nothing
            await bridge.post(session, sdrradiovolume=70)
            await until(lambda: received)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert before == [] and received == [{"volume": 70.0}]

    asyncio.run(main())


def test_reconnects_and_reports_the_link():
    async def main():
        events = []
        async with Bridge() as bridge:
            c = BridgeClient({**CFG, "ws_url": bridge.ws_url}, lambda v: None,
                             on_connect=lambda: events.append("up"),
                             on_disconnect=lambda: events.append("down"))
            task = asyncio.create_task(c.run())
            assert await until(lambda: "up" in events)
            await bridge.server.close()
            assert await until(lambda: "down" in events)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())


def test_a_failed_state_read_does_not_drop_the_link():
    async def main():
        async with Bridge() as bridge:
            async def fetch():
                raise ConnectionError("no REST today")

            c = BridgeClient({**CFG, "ws_url": bridge.ws_url}, lambda v: None, fetch)
            task = asyncio.create_task(c.run())
            assert await until(lambda: c.connected)
            await asyncio.sleep(0.05)
            assert c.connected
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())


def test_no_ping_is_sent_because_rb01_cannot_answer(monkeypatch):
    """rb-vor A29: the library default killed a healthy link every 50 s, a ping every
    20 s that RB-01 never answers. Too slow to wait for, so the setting is pinned, and
    a peer that never reads must not be dropped."""
    import websockets
    real_connect = websockets.connect
    seen = {}

    def spy(url, **kwargs):
        seen.update(kwargs)
        return real_connect(url, **kwargs)

    monkeypatch.setattr(websockets, "connect", spy)

    async def main():
        async def ws(request):
            sock = web.WebSocketResponse(autoping=False)
            await sock.prepare(request)
            await asyncio.sleep(0.5)                 # reads nothing, answers nothing
            return sock

        app = web.Application()
        app.router.add_get("/ws", ws)
        server = TestServer(app)
        await server.start_server()
        url = str(server.make_url("/ws")).replace("http", "ws")
        downs = []
        c = BridgeClient({**CFG, "ws_url": url}, lambda v: None,
                         on_disconnect=lambda: downs.append(1))
        task = asyncio.create_task(c.run())
        await until(lambda: c.connected)
        await asyncio.sleep(0.3)
        dropped = list(downs)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await server.close()
        assert dropped == []

    asyncio.run(main())
    assert seen["ping_interval"] is None


# -- the stand-in itself: it is only useful if it is as awkward as RB-01 --------- #

def test_mock_discards_the_whole_post_on_one_bad_value():
    async def main():
        import aiohttp
        async with Bridge() as bridge, aiohttp.ClientSession() as session:
            await bridge.post(session, good=1.0, bad="text")
            assert bridge.state == {} and len(bridge.app[REJECTED]) == 1

    asyncio.run(main())


def test_mock_stores_float32():
    async def main():
        import aiohttp
        async with Bridge() as bridge, aiohttp.ClientSession() as session:
            await bridge.post(session, sdrradiofreq=118.03)
            assert bridge.state["sdrradiofreq"] != 118.03
            assert bridge.state["sdrradiofreq"] == pytest.approx(118.03, abs=1e-5)

    asyncio.run(main())
