#!/usr/bin/env python3
"""Stand-in for RB-01's float bridge, faithful to the awkward parts.

Runs the daemon end to end with no RB-01, and is what the integration tests talk to.
It deliberately copies `handleBridgeFloatSetRequest` and `uibroadcast.go`:

* one non-numeric value discards the whole POST, reported only in the log;
* a broadcast goes out only when a value changed, and carries the whole map;
* a new WebSocket client is told nothing until the next change.

    tools/mock_bridge.py --port 8099
    curl -X POST localhost:8099/bridge/float -d '{"sdrradiofreq":118.7,"sdrradiovolume":50}'
    curl localhost:8099/bridge/float

Point the daemon at it by setting `bridge.ws_url` to ws://localhost:8099/bridge/float/ws
and `bridge.rest_url` to http://localhost:8099/bridge/float.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import struct

from aiohttp import web

STATE = web.AppKey("state", dict)
SOCKETS = web.AppKey("sockets", set)
REJECTED = web.AppKey("rejected", list)


def _float32(value: float) -> float:
    """What survives Go's float32: the bridge stores nothing wider."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _decode(body: bytes) -> dict[str, float] | None:
    """`map[string]float32` semantics: all keys decode, or none do."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for key, value in data.items():
        if value is None:                         # Go leaves a null map value at zero
            out[key] = 0.0
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        else:
            out[key] = _float32(value)
    return out


async def bridge_float(request: web.Request) -> web.Response:
    app = request.app
    state = app[STATE]
    if request.method == "GET":
        return web.json_response(state)
    body = await request.read()
    message = _decode(body)
    if message is None:
        app[REJECTED].append(body)
        print(f"  [bridge] discarded: {body[:120]!r}")
        return web.Response(status=200)           # RB-01 answers 200 even then
    changed = [k for k, v in message.items() if state.get(k) != v]
    state.update(message)
    if changed:
        print(f"  [bridge] {json.dumps({k: state[k] for k in sorted(changed)})}")
        frame = json.dumps(state)
        for ws in list(app[SOCKETS]):
            with contextlib.suppress(ConnectionError):
                await ws.send_str(frame)
    return web.Response(status=200)


async def bridge_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    sockets = request.app[SOCKETS]
    sockets.add(ws)
    try:
        async for _ in ws:
            pass
    finally:
        sockets.discard(ws)
    return ws


async def _close_sockets(app: web.Application) -> None:
    """Without this a server shutdown waits out aiohttp's timeout on every open socket."""
    for ws in list(app[SOCKETS]):
        await ws.close()


def build_app() -> web.Application:
    app = web.Application()
    app[STATE] = {}
    app[SOCKETS] = set()
    app[REJECTED] = []
    app.on_shutdown.append(_close_sockets)
    app.router.add_route("*", "/bridge/float", bridge_float)
    app.router.add_get("/bridge/float/ws", bridge_ws)
    return app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args(argv)
    print(f"mock RB-01 bridge on http://localhost:{args.port}/bridge/float")
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        web.run_app(build_app(), port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
