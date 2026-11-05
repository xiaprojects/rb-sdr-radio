"""What goes on the wire, and when."""

import asyncio
import json

import pytest

from rbsdrradio.publisher import FIELDS, METERS, Publisher, PublishPolicy

CFG = {"keepalive_s": 5.0, "meter_period_s": 0.5, "meter_deadband_db": 1.0}


def base(**over):
    payload = dict.fromkeys(FIELDS, 0)
    payload.update(over)
    return payload


def test_first_payload_goes():
    assert PublishPolicy(CFG).decide(base()) is True


def test_identical_payload_does_not_go(clock):
    policy = PublishPolicy(CFG, clock)
    policy.decide(base())
    clock.advance(1.0)
    assert policy.decide(base()) is False


@pytest.mark.parametrize("field", [f for f in FIELDS if f not in METERS])
def test_state_change_goes_at_once(clock, field):
    policy = PublishPolicy(CFG, clock)
    policy.decide(base())
    assert policy.decide(base(**{field: 1})) is True


def test_meters_are_rate_limited(clock):
    """RB-01 logs and re-broadcasts the whole bridge on every change."""
    policy = PublishPolicy(CFG, clock)
    policy.decide(base())
    sent = 0
    for i in range(30):                    # 3 s of a jumping meter at 10 Hz
        clock.advance(0.1)
        sent += policy.decide(base(sdrradiosnr=(i % 2) * 20))
    assert sent <= 6


def test_meter_jitter_inside_the_deadband_stays_quiet(clock):
    policy = PublishPolicy({**CFG, "meter_deadband_db": 2.0}, clock)
    policy.decide(base(sdrradiosignal=-40))
    clock.advance(1.0)
    assert policy.decide(base(sdrradiosignal=-41)) is False
    assert policy.decide(base(sdrradiosignal=-42)) is True


def test_keepalive_repeats_a_steady_state(clock):
    """It is what rebuilds RB-01's map after RB-01 restarts."""
    policy = PublishPolicy(CFG, clock)
    policy.decide(base())
    clock.advance(4.9)
    assert policy.decide(base()) is False
    clock.advance(0.2)
    assert policy.decide(base()) is True


def test_keepalive_zero_disables_it(clock):
    policy = PublishPolicy({**CFG, "keepalive_s": 0}, clock)
    policy.decide(base())
    clock.advance(1000.0)
    assert policy.decide(base()) is False


def run_publisher(script):
    async def main():
        sent = []
        await script(sent)
        return sent
    return asyncio.run(main())


def test_only_the_newest_payload_waits():
    """Drop, never queue."""
    async def script(sent):
        async def post(payload):
            sent.append(dict(payload))
            await asyncio.sleep(0.02)

        pub = Publisher(CFG, post)
        task = asyncio.create_task(pub.run())
        pub.publish(base(sdrradiostatus=1))
        await asyncio.sleep(0.005)              # the first is now in flight
        for status in (2, 3, 4):
            pub.publish(base(sdrradiostatus=status))
        await asyncio.sleep(0.06)
        task.cancel()

    sent = run_publisher(script)
    assert [p["sdrradiostatus"] for p in sent] == [1, 4]


def test_failed_post_is_retried_with_the_current_state():
    async def script(sent):
        calls = {"n": 0}

        async def post(payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("bridge restarting")
            sent.append(dict(payload))

        pub = Publisher(CFG, post)
        task = asyncio.create_task(pub.run())
        pub.publish(base(sdrradiostatus=3))
        await asyncio.sleep(0.01)
        pub.publish(base(sdrradiostatus=4))     # happened during the outage
        await asyncio.sleep(0.6)
        task.cancel()
        assert pub.failures == 1

    assert [p["sdrradiostatus"] for p in run_publisher(script)] == [4]


def test_a_dead_bridge_does_not_stop_later_publishing():
    async def script(sent):
        async def post(payload):
            raise ConnectionError("down")

        pub = Publisher(CFG, post)
        task = asyncio.create_task(pub.run())
        pub.publish(base())
        await asyncio.sleep(0.01)
        assert not task.done()
        task.cancel()

    run_publisher(script)


def test_payload_serialises_to_plain_json_numbers():
    text = json.dumps(base(sdrradiosignal=-40))
    assert all(isinstance(v, int) for v in json.loads(text).values())
