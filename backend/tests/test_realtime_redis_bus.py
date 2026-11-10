"""RedisEventBus against a small in-process fake of the `redis.asyncio` API it uses."""

import asyncio
import fnmatch
from collections import defaultdict

import pytest

from app.realtime import redis_bus
from app.realtime.redis_bus import RedisEventBus


class FakePipeline:
    def __init__(self, client: "FakeRedis") -> None:
        self._client = client
        self._calls: list[tuple[str, tuple]] = []

    def __getattr__(self, name: str):
        def record(*args):
            self._calls.append((name, args))
            return self

        return record

    async def execute(self) -> list:
        return [await getattr(self._client, name)(*args) for name, args in self._calls]


class FakePubSub:
    def __init__(self, client: "FakeRedis") -> None:
        self._client = client
        self.patterns: set[str] = set()
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self.fail_next: Exception | None = None
        self.closed = False

    async def psubscribe(self, *patterns: str) -> None:
        self.patterns.update(patterns)

    async def get_message(self, ignore_subscribe_messages: bool = False, timeout: float | None = 0.0) -> dict | None:
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        return await self.queue.get()

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis:
    """Just enough of redis.asyncio.Redis: INCR, RPUSH/LTRIM/LRANGE/EXPIRE, PUBLISH, pipeline, pubsub."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = defaultdict(int)
        self.lists: dict[str, list[str]] = defaultdict(list)
        self.ttls: dict[str, int] = {}
        self.pubsubs: list[FakePubSub] = []
        self.closed = False

    async def ping(self) -> bool:
        return True

    async def incr(self, key: str) -> int:
        self.counters[key] += 1
        return self.counters[key]

    async def rpush(self, key: str, value: str) -> int:
        self.lists[key].append(value)
        return len(self.lists[key])

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        items = self.lists[key]
        stop = len(items) if stop == -1 else stop + 1
        self.lists[key] = items[start:stop] if start >= 0 else items[max(len(items) + start, 0) : stop]
        return True

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        items = self.lists.get(key, [])
        return items[start:] if stop == -1 else items[start : stop + 1]

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    async def publish(self, channel: str, payload: str) -> int:
        delivered = 0
        for ps in self.pubsubs:
            if any(fnmatch.fnmatchcase(channel, p) for p in ps.patterns):
                ps.queue.put_nowait({"type": "pmessage", "pattern": None, "channel": channel, "data": payload})
                delivered += 1
        return delivered

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    def pubsub(self) -> FakePubSub:
        ps = FakePubSub(self)
        self.pubsubs.append(ps)
        return ps

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def fake() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
async def bus(fake: FakeRedis):
    bus = RedisEventBus(fake, history_size=5)
    await bus.start()
    yield bus
    await bus.close()


async def settle() -> None:
    """Let the reader task drain what the fake delivered."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_publish_fans_out_to_local_subscribers_and_history(bus, fake):
    async with await bus.subscribe("event:x") as sub:
        first = await bus.publish("event:x", "a", {"n": 1})
        second = await bus.publish("event:x", "b")
        got = [await sub.get(timeout=1), await sub.get(timeout=1)]
    assert (first.seq, second.seq) == (1, 2) and [e.id for e in got] == [first.id, second.id]
    assert got[1].data == {} and got[1].topic == "event:x"
    assert [e.seq for e in bus.history("event:x")] == [1, 2] and bus.history("event:x", since_seq=1) == [second]
    assert bus.subscriber_count("event:x") == 0
    assert fake.counters["evently:seq:event:x"] == 2 and len(fake.lists["evently:log:event:x"]) == 2
    assert fake.ttls["evently:log:event:x"] == redis_bus.LOG_TTL_SECONDS


async def test_log_is_capped_and_history_follows(bus, fake):
    for i in range(8):
        await bus.publish("event:x", "t", {"i": i})
    await settle()
    assert len(fake.lists["evently:log:event:x"]) == 5
    assert [e.seq for e in bus.history("event:x")] == [4, 5, 6, 7, 8]


async def test_two_workers_share_seq_and_delivery(fake):
    one, two = RedisEventBus(fake, history_size=5), RedisEventBus(fake, history_size=5)
    await one.start()
    await two.start()
    try:
        async with await two.subscribe("event:x") as sub:
            from_one = await one.publish("event:x", "a")
            from_two = await two.publish("event:x", "b")
            got = [await sub.get(timeout=1), await sub.get(timeout=1)]
        assert [e.seq for e in got] == [1, 2] and got[0].id == from_one.id and got[1].id == from_two.id
        await settle()
        assert [e.seq for e in one.history("event:x")] == [1, 2]  # publisher sees it via pub/sub too
    finally:
        await one.close()
        await two.close()


async def test_new_worker_seeds_history_from_redis_log_on_subscribe(fake):
    writer = RedisEventBus(fake, history_size=5)
    await writer.start()
    for i in range(3):
        await writer.publish("event:x", "t", {"i": i})
    await writer.close()

    late = RedisEventBus(fake, history_size=5)
    await late.start()
    try:
        assert late.history("event:x") == []  # nothing seen yet in this process
        async with await late.subscribe("event:x", since_seq=1) as sub:
            assert [e.seq for e in late.history("event:x")] == [1, 2, 3]
            replayed = [await sub.get(timeout=1), await sub.get(timeout=1)]
            assert [e.seq for e in replayed] == [2, 3]
            # A live envelope that was also captured by the seed is not delivered twice.
            live = await late.publish("event:x", "t", {"i": 3})
            assert (await sub.get(timeout=1)).seq == live.seq == 4
            with pytest.raises(TimeoutError):
                await sub.get(timeout=0.05)
    finally:
        await late.close()


async def test_out_of_order_delivery_is_kept_sorted_and_deduplicated(bus):
    from app.realtime.events import Envelope

    for seq in (2, 1, 2):
        bus._deliver(Envelope(type="t", topic="event:x", seq=seq))
    assert [e.seq for e in bus.history("event:x")] == [1, 2]


async def test_reader_failure_marks_subscribers_lagged(bus, fake, monkeypatch):
    monkeypatch.setattr(redis_bus, "RECONNECT_DELAY_SECONDS", 0)
    async with await bus.subscribe("event:x") as sub:
        assert not sub.lagged
        fake.pubsubs[0].fail_next = ConnectionError("gone")
        fake.pubsubs[0].queue.put_nowait({"type": "pmessage", "data": "not json"})  # wakes the reader; ignored
        await settle()
        assert sub.lagged
        envelope = await bus.publish("event:x", "after")  # reader is still alive
        assert (await sub.get(timeout=1)).id == envelope.id


async def test_close_stops_reader_and_subscribe_requires_start(fake):
    bus = RedisEventBus(fake, owns_client=True)
    with pytest.raises(RuntimeError):
        await bus.subscribe("event:x")
    await bus.start()
    reader = bus._reader
    await bus.close()
    assert reader.done() and fake.pubsubs[0].closed and fake.closed


async def test_build_redis_bus_needs_url(settings):
    with pytest.raises(ValueError):
        await redis_bus.build_redis_bus(settings)
