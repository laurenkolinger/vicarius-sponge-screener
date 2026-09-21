"""Tests for screener.relay: range parsing, the chunk cache, parallel fetch, retry, and eviction."""

import json
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from screener import relay as relay_module
from screener.keys import InvalidKey, cache_id
from screener.relay import (
    ChunkRelay,
    ObjectNotFound,
    RangeNotSatisfiable,
    RangeReader,
    RelayError,
    chunk_span,
    parse_range_header,
)
from tests.fakes3 import FakeS3

CHUNK = 1024
ROOT = "TCRMP_video_ondeck/"
KEY = ROOT + "2024Annual/TCRMP20241022_video_FLC_T1.MP4"


def _body(size, seed=7):
    """Return reproducible pseudo-random bytes, so a misplaced chunk never matches by luck."""
    return random.Random(seed).getrandbits(8 * size).to_bytes(size, "big") if size else b""


def _read(relay, key, start, end):
    """Read an inclusive byte range through a reader and return the joined bytes."""
    with relay.open_reader(key, start, end) as reader:
        return b"".join(reader)


def _chunks_requested(fake, key):
    """Return the chunk index of every GET for a key, in arrival order."""
    return [int(entry["range"].split("=")[1].split("-")[0]) // CHUNK for entry in fake.gets(key)]


def _wait_until(condition, seconds=10.0):
    """Poll until condition() is true; fail the test when the time runs out."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached within %.1f seconds" % seconds)


def _chunk_path(cache_root, key, index):
    return cache_root / "chunks" / cache_id(key) / ("%06d.bin" % index)


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    """Shrink the retry pauses so failure tests finish in milliseconds."""
    monkeypatch.setattr(relay_module, "RETRY_BACKOFF_SECONDS", (0.01, 0.01, 0.01))


@pytest.fixture
def fake():
    server = FakeS3()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def make_relay(fake, tmp_path):
    """Build relays on the fake bucket with small chunks, and close them all afterward."""
    made = []

    def build(**kwargs):
        options = {"chunk_size": CHUNK, "workers": 4, "read_ahead": 4, "timeout": 5.0}
        options.update(kwargs)
        cache_root = options.pop("cache_root", tmp_path / "cache")
        bucket_url = options.pop("bucket_url", None) or fake.url
        relay = ChunkRelay(bucket_url, cache_root, **options)
        made.append(relay)
        return relay

    yield build
    for relay in made:
        relay.close()


# ----- parse_range_header and chunk_span -----


def test_parse_range_forms():
    assert parse_range_header(None, 1000) == (0, 999)
    assert parse_range_header("bytes=0-499", 1000) == (0, 499)
    assert parse_range_header("bytes=500-999", 1000) == (500, 999)
    assert parse_range_header("bytes=500-", 1000) == (500, 999)
    assert parse_range_header("bytes=-200", 1000) == (800, 999)
    assert parse_range_header("bytes=-5000", 1000) == (0, 999)
    assert parse_range_header("bytes=0-0", 1000) == (0, 0)
    assert parse_range_header("bytes=999-", 1000) == (999, 999)
    assert parse_range_header("bytes=900-5000", 1000) == (900, 999)
    assert parse_range_header("  Bytes = 10 - 20  ", 1000) == (10, 20)
    assert parse_range_header(None, 0) == (0, -1)


@pytest.mark.parametrize(
    "header",
    [
        "",
        "bytes",
        "bytes=",
        "bytes=-",
        "bytes=abc-def",
        "bytes=5",
        "bytes=1-2-3",
        "bytes=--5",
        "bytes=-0",
        "bytes=0-10,20-30",
        "bytes=0-10, 20-30",
        "items=0-10",
        "bytes=500-100",
        "bytes=1000-",
        "bytes=1000-2000",
        "bytes=99999999999999999999999999-",
        "bytes=0x10-0x20",
        "bytes=1.5-9",
        "bytes=١-٩",
        "bytes=0-10\r\nX-Evil: 1",
        b"bytes=0-10",
        17,
    ],
)
def test_parse_range_rejects_garbage_multi_reversed_past_end(header):
    with pytest.raises(RangeNotSatisfiable):
        parse_range_header(header, 1000)


def test_parse_range_on_empty_object_and_bad_size():
    with pytest.raises(RangeNotSatisfiable):
        parse_range_header("bytes=0-", 0)
    with pytest.raises(RangeNotSatisfiable):
        parse_range_header("bytes=-5", 0)
    for bad in (-1, 1.5, "10", None, True):
        with pytest.raises(ValueError, match="size"):
            parse_range_header("bytes=0-1", bad)


def test_range_not_satisfiable_and_object_not_found_are_relay_errors():
    assert issubclass(RangeNotSatisfiable, RelayError)
    assert issubclass(ObjectNotFound, RelayError)


def test_chunk_span():
    assert chunk_span(0, 0, 1024) == range(0, 1)
    assert chunk_span(0, 1023, 1024) == range(0, 1)
    assert chunk_span(0, 1024, 1024) == range(0, 2)
    assert chunk_span(1023, 1024, 1024) == range(0, 2)
    assert chunk_span(1024, 2047, 1024) == range(1, 2)
    assert chunk_span(5000, 9999, 1024) == range(4, 10)
    for start, end, size in ((-1, 5, 1024), (6, 5, 1024), (0, 5, 0), (0, 5, -4), (0.0, 5, 1024), (0, "5", 1024), (True, 5, 1024)):
        with pytest.raises(ValueError):
            chunk_span(start, end, size)


def test_backoff_constant_matches_the_plan(monkeypatch):
    monkeypatch.undo()
    assert relay_module.RETRY_BACKOFF_SECONDS == (0.5, 1.0, 2.0)


# ----- reading -----


def test_reads_whole_object_byte_for_byte(fake, make_relay):
    body = _body(10 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay()
    assert relay.size(KEY) == len(body)
    with relay.open_reader(KEY, 0, len(body) - 1) as reader:
        assert isinstance(reader, RangeReader)
        blocks = list(reader)
    assert all(isinstance(block, bytes) and block for block in blocks)
    assert b"".join(blocks) == body


def test_reads_inner_range_across_chunk_edges(fake, make_relay):
    body = _body(10 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay()
    for start, end in ((1000, 3100), (1023, 1024), (1024, 2047), (2047, 2047), (5, 9), (3 * CHUNK - 1, 7 * CHUNK)):
        assert _read(relay, KEY, start, end) == body[start: end + 1]


def test_last_short_chunk(fake, make_relay, tmp_path):
    body = _body(3 * CHUNK + 17)
    fake.put(KEY, body)
    relay = make_relay()
    assert _read(relay, KEY, 0, len(body) - 1) == body
    assert _read(relay, KEY, len(body) - 1, len(body) - 1) == body[-1:]
    assert _chunk_path(tmp_path / "cache", KEY, 3).stat().st_size == 17
    assert "bytes=%d-%d" % (3 * CHUNK, 3 * CHUNK + 16) in [entry["range"] for entry in fake.gets(KEY)]


def test_single_byte_object_and_object_smaller_than_a_chunk(fake, make_relay):
    fake.put(KEY, b"Z")
    other = ROOT + "small.mp4"
    fake.put(other, _body(100))
    relay = make_relay()
    assert _read(relay, KEY, 0, 0) == b"Z"
    assert _read(relay, other, 10, 89) == _body(100)[10:90]


def test_empty_object_yields_nothing(fake, make_relay):
    fake.put(KEY, b"")
    relay = make_relay()
    assert relay.size(KEY) == 0
    assert _read(relay, KEY, 0, -1) == b""
    assert relay.cached_fraction(KEY) == 1.0
    with pytest.raises(RangeNotSatisfiable):
        relay.open_reader(KEY, 0, 0)


def test_second_read_comes_from_disk(fake, make_relay):
    body = _body(6 * CHUNK + 5)
    fake.put(KEY, body)
    first = make_relay()
    assert _read(first, KEY, 0, len(body) - 1) == body
    gets, heads = fake.count("GET"), fake.count("HEAD")
    assert gets == 7 and heads == 1

    assert _read(first, KEY, 0, len(body) - 1) == body
    assert _read(first, KEY, 2000, 5000) == body[2000:5001]
    first.close()

    second = make_relay()
    assert second.size(KEY) == len(body)
    assert _read(second, KEY, 0, len(body) - 1) == body
    assert (fake.count("GET"), fake.count("HEAD")) == (gets, heads)


def test_size_writes_meta_json(fake, make_relay, tmp_path):
    fake.put(KEY, _body(5000))
    relay = make_relay()
    assert relay.size(KEY) == 5000
    meta = json.loads((tmp_path / "cache" / "chunks" / cache_id(KEY) / "meta.json").read_text(encoding="utf-8"))
    assert meta == {"key": KEY, "size": 5000, "chunk_size": CHUNK}
    assert relay.size(KEY) == 5000
    assert fake.count("HEAD") == 1


def test_concurrent_size_calls_send_one_head(fake, make_relay):
    fake.put(KEY, _body(5000))
    fake.delay_seconds = 0.1
    relay = make_relay()
    results = []
    threads = [threading.Thread(target=lambda: results.append(relay.size(KEY))) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [5000] * 8
    assert fake.count("HEAD") == 1


def test_fetches_in_parallel(fake, make_relay):
    body = _body(8 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay(workers=4, read_ahead=8)
    relay.size(KEY)
    fake.delay_seconds = 0.15
    fake.max_in_flight = 0
    assert _read(relay, KEY, 0, len(body) - 1) == body
    assert 2 <= fake.max_in_flight <= 4


def test_two_readers_on_one_key_share_fetches(fake, make_relay):
    body = _body(12 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay(workers=3)
    relay.size(KEY)
    fake.delay_seconds = 0.02
    results = {}

    def work(name, start):
        results[name] = _read(relay, KEY, start, len(body) - 1)

    threads = [threading.Thread(target=work, args=(name, start)) for name, start in (("a", 0), ("b", 0), ("c", 3000))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == {"a": body, "b": body, "c": body[3000:]}
    assert sorted(_chunks_requested(fake, KEY)) == list(range(12))


def test_many_threads_random_ranges(fake, make_relay):
    body = _body(40 * CHUNK + 333)
    fake.put(KEY, body)
    relay = make_relay(workers=6, read_ahead=3)
    errors = []

    def work(seed):
        picker = random.Random(seed)
        try:
            for _ in range(12):
                start = picker.randrange(len(body))
                end = min(len(body) - 1, start + picker.randrange(1, 6 * CHUNK))
                assert _read(relay, KEY, start, end) == body[start: end + 1]
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=work, args=(seed,)) for seed in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    requested = _chunks_requested(fake, KEY)
    assert len(requested) == len(set(requested))


def test_read_ahead_stays_within_window(fake, make_relay):
    body = _body(40 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay(workers=4, read_ahead=4)
    with relay.open_reader(KEY, 0, len(body) - 1) as reader:
        blocks = iter(reader)
        assert next(blocks) == body[:CHUNK]
        _wait_until(lambda: sorted(_chunks_requested(fake, KEY)) == [0, 1, 2, 3, 4])
        time.sleep(0.2)
        assert sorted(_chunks_requested(fake, KEY)) == [0, 1, 2, 3, 4]
        assert next(blocks) == body[CHUNK: 2 * CHUNK]
        _wait_until(lambda: sorted(_chunks_requested(fake, KEY)) == [0, 1, 2, 3, 4, 5])
        time.sleep(0.2)
        assert sorted(_chunks_requested(fake, KEY)) == [0, 1, 2, 3, 4, 5]


def test_read_ahead_stops_at_the_end_of_the_requested_range(fake, make_relay):
    body = _body(40 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay(workers=4, read_ahead=8)
    assert _read(relay, KEY, 10 * CHUNK, 12 * CHUNK - 1) == body[10 * CHUNK: 12 * CHUNK]
    time.sleep(0.2)
    assert sorted(_chunks_requested(fake, KEY)) == [10, 11]


def test_demand_chunk_jumps_the_queue(fake, make_relay):
    body = _body(30 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay(workers=1, read_ahead=0)
    relay.size(KEY)
    fake.delay_seconds = 0.05
    relay.prefetch(KEY)
    _wait_until(lambda: len(fake.gets(KEY)) >= 1)
    assert _read(relay, KEY, 29 * CHUNK, 30 * CHUNK - 1) == body[29 * CHUNK:]
    order = _chunks_requested(fake, KEY)
    assert order.index(29) <= 2
    fake.delay_seconds = 0.0
    _wait_until(lambda: relay.cached_fraction(KEY) == 1.0)


def test_retries_then_succeeds(fake, make_relay):
    body = _body(CHUNK)
    fake.put(KEY, body)
    relay = make_relay(workers=1, read_ahead=0, retries=3)
    fake.fail_next(KEY, 2, status=500)
    assert _read(relay, KEY, 0, CHUNK - 1) == body
    assert _chunks_requested(fake, KEY) == [0, 0, 0]


def test_gives_up_after_retries_and_later_request_recovers(fake, make_relay, tmp_path):
    body = _body(2 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay(workers=1, read_ahead=0, retries=3)
    fake.fail_next(KEY, 3, status=503)
    with pytest.raises(RelayError) as caught:
        _read(relay, KEY, 0, CHUNK - 1)
    message = str(caught.value)
    assert message.startswith("chunk 0 of %s failed after 3 tries: " % KEY)
    assert "503" in message
    assert _chunks_requested(fake, KEY) == [0, 0, 0]
    assert not _chunk_path(tmp_path / "cache", KEY, 0).exists()

    assert _read(relay, KEY, 0, 2 * CHUNK - 1) == body
    assert relay.cached_fraction(KEY) == 1.0


def test_network_loss_mid_session_raises_and_cached_chunks_still_read(make_relay):
    body = _body(4 * CHUNK)
    bucket = FakeS3()
    url = bucket.start()
    try:
        bucket.put(KEY, body)
        relay = make_relay(bucket_url=url, workers=2, read_ahead=0, retries=2, timeout=2.0)
        assert _read(relay, KEY, 0, CHUNK - 1) == body[:CHUNK]
    finally:
        bucket.stop()
    with pytest.raises(RelayError, match="chunk 1 of .* failed after 2 tries"):
        _read(relay, KEY, CHUNK, 2 * CHUNK - 1)
    assert _read(relay, KEY, 0, CHUNK - 1) == body[:CHUNK]


def test_truncated_body_is_retried_not_cached(fake, make_relay, tmp_path):
    body = _body(2 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay(workers=1, read_ahead=0)
    fake.truncate_next(KEY, 1)
    assert _read(relay, KEY, 0, CHUNK - 1) == body[:CHUNK]
    assert _chunks_requested(fake, KEY) == [0, 0]
    folder = tmp_path / "cache" / "chunks" / cache_id(KEY)
    assert _chunk_path(tmp_path / "cache", KEY, 0).read_bytes() == body[:CHUNK]
    assert sorted(path.name for path in folder.iterdir()) == ["000000.bin", "meta.json"]


def test_always_truncated_body_gives_up_and_caches_nothing(fake, make_relay, tmp_path):
    fake.put(KEY, _body(2 * CHUNK))
    relay = make_relay(workers=1, read_ahead=0, retries=2)
    fake.truncate_next(KEY, 2)
    with pytest.raises(RelayError, match="failed after 2 tries"):
        _read(relay, KEY, 0, CHUNK - 1)
    folder = tmp_path / "cache" / "chunks" / cache_id(KEY)
    assert sorted(path.name for path in folder.iterdir()) == ["meta.json"]


def test_stale_keep_alive_connection_costs_no_retry(fake, make_relay):
    body = _body(4 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay(workers=1, read_ahead=0, retries=1)
    assert _read(relay, KEY, 0, CHUNK - 1) == body[:CHUNK]
    fake.drop_connections()
    time.sleep(0.05)
    assert _read(relay, KEY, CHUNK, 2 * CHUNK - 1) == body[CHUNK: 2 * CHUNK]


def test_workers_reuse_one_connection_each(fake, make_relay):
    body = _body(20 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay(workers=2, read_ahead=4)
    relay.size(KEY)
    opened = []
    original = relay_module.ChunkRelay._new_connection

    def counting(self):
        connection = original(self)
        opened.append(connection)
        return connection

    relay_module.ChunkRelay._new_connection = counting
    try:
        assert _read(relay, KEY, 0, len(body) - 1) == body
    finally:
        relay_module.ChunkRelay._new_connection = original
    assert 1 <= len(opened) <= 2


def test_the_worker_that_went_idle_last_gets_the_next_chunk(fake, make_relay):
    body = _body(12 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay(workers=4, read_ahead=0)
    relay.size(KEY)
    time.sleep(0.05)
    opened = []
    original = relay_module.ChunkRelay._new_connection

    def counting(self):
        connection = original(self)
        opened.append(connection)
        return connection

    relay_module.ChunkRelay._new_connection = counting
    try:
        for index in range(12):
            assert _read(relay, KEY, index * CHUNK, (index + 1) * CHUNK - 1) == body[index * CHUNK: (index + 1) * CHUNK]
    finally:
        relay_module.ChunkRelay._new_connection = original
    assert len(opened) == 1


def test_unknown_key_raises(fake, make_relay, tmp_path):
    relay = make_relay()
    missing = ROOT + "missing.mp4"
    for call in (lambda: relay.size(missing), lambda: relay.open_reader(missing, 0, 10), lambda: relay.prefetch(missing)):
        with pytest.raises(RelayError) as caught:
            call()
        assert isinstance(caught.value, ObjectNotFound)
        assert missing in str(caught.value) and "404" in str(caught.value)
    assert not (tmp_path / "cache" / "chunks" / cache_id(missing)).exists()
    assert fake.count("GET") == 0


def test_size_retries_a_failing_head_then_reports(fake, make_relay):
    fake.put(KEY, _body(100))
    relay = make_relay(retries=3)
    fake.fail_next(KEY, 2, status=500, method="HEAD")
    assert relay.size(KEY) == 100
    assert fake.count("HEAD") == 3

    other = ROOT + "other.mp4"
    fake.put(other, _body(100))
    fake.fail_next(other, 3, status=500, method="HEAD")
    with pytest.raises(RelayError) as caught:
        relay.size(other)
    assert other in str(caught.value) and "500" in str(caught.value) and "3 tries" in str(caught.value)
    assert relay.size(other) == 100


def test_object_size_change_raises(fake, make_relay):
    fake.put(KEY, _body(4 * CHUNK))
    relay = make_relay(workers=1, read_ahead=0)
    assert relay.size(KEY) == 4 * CHUNK
    fake.put(KEY, _body(6 * CHUNK, seed=9))
    with pytest.raises(RelayError, match="object changed"):
        _read(relay, KEY, 0, CHUNK - 1)


def test_object_change_clears_the_cache_and_a_new_read_recovers(fake, make_relay, tmp_path):
    old, new = _body(4 * CHUNK), _body(6 * CHUNK, seed=9)
    fake.put(KEY, old)
    relay = make_relay(workers=1, read_ahead=0)
    assert _read(relay, KEY, 0, CHUNK - 1) == old[:CHUNK]
    fake.put(KEY, new)
    with pytest.raises(RelayError, match="object changed"):
        _read(relay, KEY, CHUNK, 2 * CHUNK - 1)
    assert relay.size(KEY) == len(new)
    assert _read(relay, KEY, 0, len(new) - 1) == new


def test_hostile_key_rejected_before_any_request(fake, make_relay, tmp_path):
    relay = make_relay()
    hostile = [
        "other/x.mp4",
        ROOT + "../secret.mp4",
        ROOT + "a\\b.mp4",
        ROOT + "a//b.mp4",
        ROOT + "a\nb.mp4",
        ROOT + "folder/",
        "",
        None,
        7,
        ROOT + "x" * 1100,
    ]
    for key in hostile:
        for call in (relay.size, relay.prefetch, relay.cached_fraction, lambda value: relay.open_reader(value, 0, 1)):
            with pytest.raises(InvalidKey):
                call(key)
    assert fake.requests == []
    assert list((tmp_path / "cache" / "chunks").iterdir()) == []


def test_key_with_plus_is_percent_encoded(fake, make_relay):
    key = ROOT + "main/TCRMP2005_video/Peak BL/TCRMP20051013_video_SSJ_T1+T3-6.mp4"
    body = _body(3 * CHUNK)
    fake.put(key, body)
    relay = make_relay()
    assert _read(relay, key, 0, len(body) - 1) == body
    expected = "/TCRMP_video_ondeck/main/TCRMP2005_video/Peak%20BL/TCRMP20051013_video_SSJ_T1%2BT3-6.mp4"
    assert {entry["path"] for entry in fake.requests} == {expected}


def test_open_reader_rejects_bad_ranges(fake, make_relay):
    fake.put(KEY, _body(1000))
    relay = make_relay()
    for start, end in ((0, 1000), (1000, 1000), (500, 499), (-1, 5), (0, -1)):
        with pytest.raises(RangeNotSatisfiable):
            relay.open_reader(KEY, start, end)
    for start, end in ((0.0, 5), (0, "5"), (None, 5), (True, 5)):
        with pytest.raises(ValueError):
            relay.open_reader(KEY, start, end)
    assert fake.count("GET") == 0


def test_reader_context_manager_and_idempotent_close(fake, make_relay):
    body = _body(3 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay()
    reader = relay.open_reader(KEY, 0, len(body) - 1)
    assert (reader.key, reader.start, reader.end, reader.length) == (KEY, 0, len(body) - 1, len(body))
    with reader as same:
        assert same is reader
        assert next(iter(reader)) == body[:CHUNK]
    reader.close()
    reader.close()
    with pytest.raises(RelayError, match="closed"):
        list(reader)


def test_iteration_resumes_where_it_stopped(fake, make_relay):
    body = _body(3 * CHUNK + 10)
    fake.put(KEY, body)
    relay = make_relay()
    with relay.open_reader(KEY, 100, len(body) - 1) as reader:
        first = next(iter(reader))
        rest = b"".join(reader)
    assert first + rest == body[100:]


# ----- the cache folder -----


def test_wrong_length_chunk_file_is_discarded_on_startup(fake, make_relay, tmp_path):
    body = _body(4 * CHUNK + 100)
    fake.put(KEY, body)
    first = make_relay()
    assert _read(first, KEY, 0, len(body) - 1) == body
    first.close()
    gets = fake.count("GET")

    cache_root = tmp_path / "cache"
    _chunk_path(cache_root, KEY, 1).write_bytes(b"short and wrong")
    _chunk_path(cache_root, KEY, 4).write_bytes(bytes(CHUNK))
    stray = _chunk_path(cache_root, KEY, 2).with_name("000002.bin.part")
    stray.write_bytes(b"half a chunk")
    beyond = _chunk_path(cache_root, KEY, 9)
    beyond.write_bytes(bytes(CHUNK))

    second = make_relay()
    for path in (_chunk_path(cache_root, KEY, 1), _chunk_path(cache_root, KEY, 4), stray, beyond):
        assert not path.exists()
    assert _chunk_path(cache_root, KEY, 0).exists()
    assert 0.0 < second.cached_fraction(KEY) < 1.0
    assert _read(second, KEY, 0, len(body) - 1) == body
    assert sorted(_chunks_requested(fake, KEY)[gets:]) == [1, 4]
    assert fake.count("HEAD") == 1


def test_meta_with_another_chunk_size_clears_the_folder(fake, make_relay, tmp_path):
    body = _body(4 * CHUNK)
    fake.put(KEY, body)
    first = make_relay()
    assert _read(first, KEY, 0, len(body) - 1) == body
    first.close()

    second = make_relay(chunk_size=2 * CHUNK)
    assert not (tmp_path / "cache" / "chunks" / cache_id(KEY)).exists()
    assert second.cached_fraction(KEY) == 0.0
    assert _read(second, KEY, 0, len(body) - 1) == body
    assert _chunk_path(tmp_path / "cache", KEY, 0).stat().st_size == 2 * CHUNK


def test_folder_with_corrupt_or_missing_meta_is_cleared_on_startup(fake, make_relay, tmp_path):
    body = _body(2 * CHUNK)
    fake.put(KEY, body)
    other = ROOT + "other.mp4"
    fake.put(other, body)
    first = make_relay()
    _read(first, KEY, 0, len(body) - 1)
    _read(first, other, 0, len(body) - 1)
    first.close()
    chunks = tmp_path / "cache" / "chunks"
    (chunks / cache_id(KEY) / "meta.json").write_text("{broken", encoding="utf-8")
    (chunks / cache_id(other) / "meta.json").unlink()

    second = make_relay()
    assert not (chunks / cache_id(KEY)).exists() and not (chunks / cache_id(other)).exists()
    assert _read(second, KEY, 0, len(body) - 1) == body


def test_cached_chunk_file_vanishing_is_refetched(fake, make_relay, tmp_path):
    body = _body(3 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay()
    assert _read(relay, KEY, 0, len(body) - 1) == body
    _chunk_path(tmp_path / "cache", KEY, 1).unlink()
    assert _read(relay, KEY, 0, len(body) - 1) == body
    assert sorted(_chunks_requested(fake, KEY)) == [0, 1, 1, 2]


def test_disk_write_failure_reports_the_reason(fake, make_relay, monkeypatch):
    fake.put(KEY, _body(CHUNK))
    relay = make_relay(workers=1, read_ahead=0, retries=1)
    relay.size(KEY)

    def refuse(source, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(relay_module.os, "replace", refuse)
    with pytest.raises(RelayError, match="No space left on device"):
        _read(relay, KEY, 0, CHUNK - 1)
    monkeypatch.undo()
    assert _read(relay, KEY, 0, CHUNK - 1) == _body(CHUNK)


def test_prefetch_fills_cache_and_fraction_reaches_one(fake, make_relay, tmp_path):
    body = _body(9 * CHUNK + 1)
    fake.put(KEY, body)
    relay = make_relay(workers=3)
    assert relay.cached_fraction(KEY) == 0.0
    assert fake.requests == []
    assert relay.prefetch(KEY) is None
    _wait_until(lambda: relay.cached_fraction(KEY) == 1.0)
    assert sorted(_chunks_requested(fake, KEY)) == list(range(10))
    gets = fake.count("GET")
    assert _read(relay, KEY, 0, len(body) - 1) == body
    relay.prefetch(KEY)
    time.sleep(0.1)
    assert fake.count("GET") == gets


def test_cached_fraction_counts_bytes(fake, make_relay):
    body = _body(4 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay(workers=1, read_ahead=0)
    _read(relay, KEY, 0, CHUNK - 1)
    assert relay.cached_fraction(KEY) == 0.25
    _read(relay, KEY, 3 * CHUNK, 4 * CHUNK - 1)
    assert relay.cached_fraction(KEY) == 0.5


def test_prefetch_survives_a_reader_that_comes_and_goes(fake, make_relay):
    body = _body(20 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay(workers=1, read_ahead=6)
    relay.size(KEY)
    fake.delay_seconds = 0.01
    relay.prefetch(KEY)
    with relay.open_reader(KEY, 10 * CHUNK, len(body) - 1) as reader:
        assert next(iter(reader)) == body[10 * CHUNK: 11 * CHUNK]
    _wait_until(lambda: relay.cached_fraction(KEY) == 1.0)
    requested = _chunks_requested(fake, KEY)
    assert sorted(requested) == list(range(20))


def test_evict_removes_oldest_and_keeps_recent(fake, make_relay, tmp_path):
    keys = [ROOT + "a.mp4", ROOT + "b.mp4", ROOT + "c.mp4"]
    body = _body(4 * CHUNK)
    for key in keys:
        fake.put(key, body)
    filler = make_relay(cap_bytes=10 ** 9)
    for key in keys:
        assert _read(filler, key, 0, len(body) - 1) == body
    assert filler.evict() == 0
    filler.close()

    chunks = tmp_path / "cache" / "chunks"
    now = time.time()
    os.utime(chunks / cache_id(keys[0]) / "meta.json", (now - 5000, now - 5000))
    os.utime(chunks / cache_id(keys[1]) / "meta.json", (now - 3000, now - 3000))

    roomy = make_relay(cap_bytes=2 * len(body) + 2000, keep_seconds=600)
    assert roomy.evict() == 1
    assert not (chunks / cache_id(keys[0])).exists()
    assert (chunks / cache_id(keys[1])).exists() and (chunks / cache_id(keys[2])).exists()
    assert roomy.evict() == 0
    roomy.close()

    tight = make_relay(cap_bytes=100, keep_seconds=600)
    assert tight.evict() == 1
    assert not (chunks / cache_id(keys[1])).exists()
    assert (chunks / cache_id(keys[2])).exists()
    assert tight.cached_fraction(keys[2]) == 1.0

    gets = fake.count("GET")
    assert _read(tight, keys[0], 0, len(body) - 1) == body
    assert fake.count("GET") == gets + 4


def test_opening_a_reader_touches_meta_and_protects_the_video(fake, make_relay, tmp_path):
    body = _body(2 * CHUNK)
    fake.put(KEY, body)
    filler = make_relay()
    _read(filler, KEY, 0, len(body) - 1)
    filler.close()
    meta = tmp_path / "cache" / "chunks" / cache_id(KEY) / "meta.json"
    os.utime(meta, (time.time() - 5000, time.time() - 5000))

    relay = make_relay(cap_bytes=1, keep_seconds=600)
    assert _read(relay, KEY, 0, 10) == body[:11]
    assert time.time() - meta.stat().st_mtime < 60
    assert relay.evict() == 0
    assert meta.exists()


def test_evict_skips_a_video_with_an_open_reader(fake, make_relay, tmp_path):
    body = _body(2 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay(cap_bytes=1, keep_seconds=0)
    with relay.open_reader(KEY, 0, len(body) - 1) as reader:
        blocks = iter(reader)
        assert next(blocks) == body[:CHUNK]
        assert relay.evict() == 0
        assert next(blocks) == body[CHUNK:]
    _wait_until(lambda: relay.evict() == 1)
    assert not (tmp_path / "cache" / "chunks" / cache_id(KEY)).exists()
    assert _read(relay, KEY, 0, len(body) - 1) == body


# ----- shutdown and abandonment -----


def test_close_unblocks_waiting_reader(fake, make_relay):
    fake.put(KEY, _body(4 * CHUNK))
    relay = make_relay(workers=2)
    relay.size(KEY)
    fake.delay_seconds = 3.0
    outcome = {}

    def work():
        try:
            outcome["data"] = _read(relay, KEY, 0, 4 * CHUNK - 1)
        except RelayError as error:
            outcome["error"] = str(error)

    thread = threading.Thread(target=work)
    thread.start()
    _wait_until(lambda: fake.in_flight >= 1)
    started = time.time()
    relay.close()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert time.time() - started < 2.5
    assert outcome == {"error": "relay closed"}


def test_calls_after_close_raise(fake, make_relay):
    fake.put(KEY, _body(100))
    relay = make_relay()
    relay.size(KEY)
    relay.close()
    relay.close()
    for call in (lambda: relay.size(KEY), lambda: relay.open_reader(KEY, 0, 5), lambda: relay.prefetch(KEY)):
        with pytest.raises(RelayError, match="relay closed"):
            call()


def test_close_stops_every_worker_thread(fake, make_relay):
    before = threading.active_count()
    relay = make_relay(workers=5)
    assert threading.active_count() == before + 5
    relay.close()
    assert threading.active_count() == before


def test_abandoned_reader_stops_read_ahead(fake, make_relay):
    body = _body(40 * CHUNK)
    fake.put(KEY, body)
    relay = make_relay(workers=1, read_ahead=12)
    relay.size(KEY)
    fake.delay_seconds = 0.05
    with relay.open_reader(KEY, 0, len(body) - 1) as reader:
        assert next(iter(reader)) == body[:CHUNK]
    time.sleep(0.6)
    assert len(fake.gets(KEY)) <= 3
    assert fake.in_flight == 0

    assert _read(relay, KEY, 20 * CHUNK, 21 * CHUNK - 1) == body[20 * CHUNK: 21 * CHUNK]


def test_reader_closed_from_another_thread_stops_waiting(fake, make_relay):
    fake.put(KEY, _body(2 * CHUNK))
    relay = make_relay(workers=1)
    relay.size(KEY)
    fake.delay_seconds = 1.0
    reader = relay.open_reader(KEY, 0, 2 * CHUNK - 1)
    outcome = {}

    def work():
        try:
            outcome["data"] = b"".join(reader)
        except RelayError as error:
            outcome["error"] = str(error)

    thread = threading.Thread(target=work)
    thread.start()
    _wait_until(lambda: fake.in_flight >= 1)
    reader.close()
    thread.join(timeout=0.8)
    assert not thread.is_alive()
    assert "closed" in outcome.get("error", "")


def test_stress_readers_eviction_prefetch_and_failures_together(fake, make_relay):
    keys = [ROOT + "stress/v%d+x y.mp4" % index for index in range(4)]
    bodies = {key: _body(23 * CHUNK + 7 * index, seed=index) for index, key in enumerate(keys)}
    for key, body in bodies.items():
        fake.put(key, body)
    relay = make_relay(workers=5, read_ahead=4, retries=3, cap_bytes=30 * CHUNK, keep_seconds=0)
    stop_at = time.time() + 2.0
    problems = []
    reads = []

    def read_ranges(seed):
        picker = random.Random(seed)
        while time.time() < stop_at:
            key = picker.choice(keys)
            body = bodies[key]
            start = picker.randrange(len(body))
            end = picker.randrange(start, len(body))
            quit_after = picker.choice([None, None, 1, 2])
            try:
                blocks = []
                with relay.open_reader(key, start, end) as reader:
                    for number, block in enumerate(reader, 1):
                        blocks.append(block)
                        if quit_after is not None and number >= quit_after:
                            break
                data = b"".join(blocks)
                if data != body[start: start + len(data)] or (quit_after is None and len(data) != end - start + 1):
                    problems.append("bytes differ for %s %d-%d" % (key, start, end))
                reads.append(len(data))
            except Exception as error:  # noqa: BLE001
                problems.append(repr(error))

    def disturb():
        picker = random.Random(99)
        while time.time() < stop_at:
            key = picker.choice(keys)
            try:
                relay.evict()
                action = picker.randrange(4)
                if action == 0:
                    relay.prefetch(key)
                elif action == 1:
                    assert 0.0 <= relay.cached_fraction(key) <= 1.0
                elif action == 2:
                    fake.fail_next(key, 1, status=500)
                else:
                    fake.drop_connections()
            except Exception as error:  # noqa: BLE001
                problems.append(repr(error))
            time.sleep(0.01)

    threads = [threading.Thread(target=read_ranges, args=(seed,)) for seed in range(6)] + [threading.Thread(target=disturb)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert [thread for thread in threads if thread.is_alive()] == []
    assert problems == []
    assert len(reads) > 50


# ----- a bucket that answers wrongly -----


class WrongBucket:
    """A server that knows one object and answers range GETs in a chosen wrong way."""

    def __init__(self, body, mode):
        outer = self
        self.body, self.mode = body, mode
        self.release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):  # noqa: A002
                pass

            def do_HEAD(self):  # noqa: N802
                self.send_response(200)
                if outer.mode != "head_without_length":
                    self.send_header("Content-Length", str(len(outer.body)))
                else:
                    self.send_header("Connection", "close")
                self.end_headers()

            def do_GET(self):  # noqa: N802
                first, last = (int(part) for part in self.headers["Range"].split("=")[1].split("-"))
                payload, status = outer.body[first: last + 1], 206
                content_range = "bytes %d-%d/%d" % (first, last, len(outer.body))
                if outer.mode == "ignores_range":
                    payload, status, content_range = outer.body, 200, None
                elif outer.mode == "wrong_span":
                    content_range = "bytes %d-%d/%d" % (first + 1, last + 1, len(outer.body))
                elif outer.mode == "no_content_range":
                    content_range = None
                elif outer.mode == "garbled_content_range":
                    content_range = "bytes lots"
                elif outer.mode == "short_but_consistent":
                    payload = payload[:-1]
                elif outer.mode == "no_length_short":
                    payload = payload[:-1]
                self.send_response(status)
                if content_range is not None:
                    self.send_header("Content-Range", content_range)
                if outer.mode in ("no_length_short", "no_length_ok"):
                    self.send_header("Connection", "close")
                    self.close_connection = True
                elif outer.mode == "promises_too_much":
                    self.send_header("Content-Length", str(10 ** 12))
                    self.end_headers()
                    self.wfile.flush()
                    outer.release.wait(3.0)
                    self.close_connection = True
                    return
                else:
                    self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def stop(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.mark.parametrize(
    "mode, match",
    [
        ("no_length_short", "bytes"),
        ("promises_too_much", "Content-Length"),
        ("ignores_range", "206"),
        ("wrong_span", "Content-Range"),
        ("no_content_range", "Content-Range"),
        ("garbled_content_range", "Content-Range"),
        ("short_but_consistent", "bytes"),
    ],
)
def test_wrong_answers_are_never_cached(make_relay, tmp_path, mode, match):
    bucket = WrongBucket(_body(4 * CHUNK), mode)
    try:
        relay = make_relay(bucket_url=bucket.url, workers=1, read_ahead=0, retries=2)
        started = time.time()
        with pytest.raises(RelayError, match=match):
            _read(relay, KEY, 0, CHUNK - 1)
        assert time.time() - started < 2.0
        folder = tmp_path / "cache" / "chunks" / cache_id(KEY)
        assert sorted(path.name for path in folder.iterdir()) == ["meta.json"]
    finally:
        bucket.stop()


def test_answer_without_content_length_is_read_to_the_exact_size(make_relay):
    body = _body(3 * CHUNK + 9)
    bucket = WrongBucket(body, "no_length_ok")
    try:
        relay = make_relay(bucket_url=bucket.url, workers=2, read_ahead=2, retries=1)
        assert _read(relay, KEY, 0, len(body) - 1) == body
    finally:
        bucket.stop()


def test_connection_close_answers_cost_no_retry(make_relay):
    body = _body(6 * CHUNK)
    bucket = WrongBucket(body, "no_length_ok")
    try:
        relay = make_relay(bucket_url=bucket.url, workers=1, read_ahead=0, retries=1)
        for index in range(6):
            assert _read(relay, KEY, index * CHUNK, (index + 1) * CHUNK - 1) == body[index * CHUNK: (index + 1) * CHUNK]
    finally:
        bucket.stop()


def test_head_without_content_length_raises(make_relay):
    bucket = WrongBucket(_body(CHUNK), "head_without_length")
    try:
        relay = make_relay(bucket_url=bucket.url, retries=1)
        with pytest.raises(RelayError, match="Content-Length"):
            relay.size(KEY)
    finally:
        bucket.stop()


# ----- constructor -----


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"bucket_url": "ftp://example.com"}, "bucket_url"),
        ({"bucket_url": "https://"}, "bucket_url"),
        ({"bucket_url": 5}, "bucket_url"),
        ({"cache_root": 5}, "cache_root"),
        ({"chunk_size": 0}, "chunk_size"),
        ({"chunk_size": 1.5}, "chunk_size"),
        ({"workers": 0}, "workers"),
        ({"workers": True}, "workers"),
        ({"read_ahead": -1}, "read_ahead"),
        ({"retries": 0}, "retries"),
        ({"cap_bytes": -1}, "cap_bytes"),
        ({"keep_seconds": -1}, "keep_seconds"),
        ({"timeout": 0}, "timeout"),
        ({"timeout": "slow"}, "timeout"),
    ],
)
def test_constructor_validates_arguments(tmp_path, kwargs, match):
    arguments = {"bucket_url": "http://127.0.0.1:1", "cache_root": tmp_path / "cache"}
    arguments.update(kwargs)
    before = threading.active_count()
    with pytest.raises(ValueError, match=match):
        ChunkRelay(**arguments)
    assert threading.active_count() == before


def test_defaults_come_from_config(tmp_path):
    from screener import config

    relay = ChunkRelay("http://127.0.0.1:1", tmp_path / "cache")
    try:
        assert relay.chunk_size == config.CHUNK_SIZE
        assert (tmp_path / "cache" / "chunks").is_dir()
    finally:
        relay.close()
