"""Tests that prove FakeS3 behaves like the public bucket the app reads."""

import http.client
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import pytest

from tests.fakes3 import LISTING, S3_NAMESPACE, FakeS3

NS = {"s3": S3_NAMESPACE}
ROOT = "TCRMP_video_ondeck/"


@pytest.fixture
def fake():
    """Yield a running FakeS3 and stop it afterward."""
    server = FakeS3()
    server.start()
    yield server
    server.stop()


def _get(url, headers=None):
    """Return (status, headers, body) for a GET, without raising on 4xx or 5xx."""
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def _list(fake, **params):
    """Fetch one listing page and return (status, parsed XML root or None, raw body)."""
    query = urllib.parse.urlencode(dict({"list-type": "2"}, **params), quote_via=urllib.parse.quote)
    status, _, body = _get(fake.url + "/?" + query)
    return status, (ET.fromstring(body) if status == 200 else None), body


def _keys(root):
    return [element.text for element in root.findall("s3:Contents/s3:Key", NS)]


def _prefixes(root):
    return [element.text for element in root.findall("s3:CommonPrefixes/s3:Prefix", NS)]


def test_start_returns_loopback_url_and_refuses_double_start(fake):
    assert fake.url.startswith("http://127.0.0.1:")
    with pytest.raises(RuntimeError, match="already running"):
        fake.start()


def test_listing_with_delimiter_groups_folders(fake):
    fake.put(ROOT + "2024Annual/a.MP4", b"12345")
    fake.put(ROOT + "2024Annual/b.MP4", b"1")
    fake.put(ROOT + "main/TCRMP2004_video/c.avi", b"123")
    fake.put(ROOT + "top.mp4", b"1234")
    fake.put("elsewhere/x.mp4", b"1")

    status, root, _ = _list(fake, prefix=ROOT, delimiter="/")
    assert status == 200
    assert root.tag == "{%s}ListBucketResult" % S3_NAMESPACE
    assert _keys(root) == [ROOT + "top.mp4"]
    assert _prefixes(root) == [ROOT + "2024Annual/", ROOT + "main/"]
    assert root.find("s3:IsTruncated", NS).text == "false"
    assert root.find("s3:NextContinuationToken", NS) is None
    sizes = [element.text for element in root.findall("s3:Contents/s3:Size", NS)]
    assert sizes == ["4"]

    _, inner, _ = _list(fake, prefix=ROOT + "2024Annual/", delimiter="/")
    assert _keys(inner) == [ROOT + "2024Annual/a.MP4", ROOT + "2024Annual/b.MP4"]
    assert _prefixes(inner) == []


def test_listing_without_delimiter_is_flat(fake):
    fake.put(ROOT + "a/b/c.mp4", b"1")
    fake.put(ROOT + "d.mp4", b"1")
    _, root, _ = _list(fake, prefix=ROOT)
    assert _keys(root) == [ROOT + "a/b/c.mp4", ROOT + "d.mp4"]
    assert _prefixes(root) == []


def test_listing_pagination_walks_every_entry_once(fake):
    for index in range(5):
        fake.put(ROOT + "p/v%d.mp4" % index, b"x")
    fake.put(ROOT + "p/sub/inner.mp4", b"x")
    fake.page_size = 2

    seen, pages, token = [], 0, None
    while True:
        params = {"prefix": ROOT + "p/", "delimiter": "/"}
        if token is not None:
            params["continuation-token"] = token
        status, root, _ = _list(fake, **params)
        assert status == 200
        pages += 1
        entries = _keys(root) + _prefixes(root)
        assert 1 <= len(entries) <= 2
        seen.extend(entries)
        if root.find("s3:IsTruncated", NS).text != "true":
            break
        token = root.find("s3:NextContinuationToken", NS).text
        assert any(char in token for char in "+/=")
    assert pages == 3
    assert sorted(seen) == sorted([ROOT + "p/v%d.mp4" % index for index in range(5)] + [ROOT + "p/sub/"])


def test_listing_rejects_unknown_token_and_missing_list_type(fake):
    fake.put(ROOT + "a.mp4", b"x")
    status, _, body = _list(fake, prefix=ROOT, **{"continuation-token": "made up"})
    assert status == 400 and b"continuation token" in body
    status, _, _ = _get(fake.url + "/?prefix=" + ROOT)
    assert status == 400


def test_unencoded_plus_in_token_is_not_recognized(fake):
    for index in range(3):
        fake.put(ROOT + "v%d.mp4" % index, b"x")
    fake.page_size = 1
    _, root, _ = _list(fake, prefix=ROOT)
    token = root.find("s3:NextContinuationToken", NS).text
    raw = fake.url + "/?list-type=2&prefix=" + ROOT + "&continuation-token=" + token
    status, _, _ = _get(raw)
    assert status == 400


def test_listing_escapes_xml_special_characters(fake):
    fake.put(ROOT + "a&b <c>.mp4", b"x")
    _, root, _ = _list(fake, prefix=ROOT)
    assert _keys(root) == [ROOT + "a&b <c>.mp4"]


def test_get_whole_object_and_head(fake):
    body = bytes(range(256)) * 4
    fake.put(ROOT + "x.mp4", body)
    status, headers, got = _get(fake.url + "/" + ROOT + "x.mp4")
    assert (status, got) == (200, body)
    assert headers["Accept-Ranges"] == "bytes"

    connection = http.client.HTTPConnection("127.0.0.1", int(fake.url.rsplit(":", 1)[1]), timeout=5)
    connection.request("HEAD", "/" + ROOT + "x.mp4")
    response = connection.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Length") == str(len(body))
    assert response.read() == b""
    connection.close()


def test_range_read_forms(fake):
    body = bytes(range(256)) * 4
    fake.put(ROOT + "x.mp4", body)
    url = fake.url + "/" + ROOT + "x.mp4"

    status, headers, got = _get(url, {"Range": "bytes=10-19"})
    assert (status, got) == (206, body[10:20])
    assert headers["Content-Range"] == "bytes 10-19/1024"
    assert headers["Content-Length"] == "10"

    status, headers, got = _get(url, {"Range": "bytes=1000-"})
    assert (status, got) == (206, body[1000:])
    assert headers["Content-Range"] == "bytes 1000-1023/1024"

    status, headers, got = _get(url, {"Range": "bytes=-24"})
    assert (status, got) == (206, body[1000:])

    status, headers, got = _get(url, {"Range": "bytes=1000-5000"})
    assert (status, got) == (206, body[1000:])
    assert headers["Content-Range"] == "bytes 1000-1023/1024"

    status, _, got = _get(url, {"Range": "bytes=0-1023"})
    assert (status, got) == (206, body)


def test_range_past_end_is_416(fake):
    fake.put(ROOT + "x.mp4", b"0123456789")
    status, headers, _ = _get(fake.url + "/" + ROOT + "x.mp4", {"Range": "bytes=10-20"})
    assert status == 416
    assert headers["Content-Range"] == "bytes */10"


def test_malformed_range_is_ignored_like_s3(fake):
    fake.put(ROOT + "x.mp4", b"0123456789")
    status, _, got = _get(fake.url + "/" + ROOT + "x.mp4", {"Range": "bytes=9-2"})
    assert (status, got) == (200, b"0123456789")


def test_unknown_key_is_404_for_get_and_head(fake):
    status, _, body = _get(fake.url + "/" + ROOT + "missing.mp4")
    assert status == 404 and b"NoSuchKey" in body
    connection = http.client.HTTPConnection("127.0.0.1", int(fake.url.rsplit(":", 1)[1]), timeout=5)
    connection.request("HEAD", "/" + ROOT + "missing.mp4")
    assert connection.getresponse().status == 404
    connection.close()


def test_percent_encoded_key_round_trips_and_bare_plus_means_space(fake):
    key = ROOT + "main/TCRMP20051013_video_SSJ_T1+T3-6 copy.mp4"
    fake.put(key, b"plus")
    status, _, got = _get(fake.url + "/" + urllib.parse.quote(key, safe="/"))
    assert (status, got) == (200, b"plus")
    assert fake.requests[-1]["key"] == key
    assert "%2B" in fake.requests[-1]["path"] and "%20" in fake.requests[-1]["path"]

    status, _, _ = _get(fake.url + "/" + key.replace(" ", "%20"))
    assert status == 404
    assert fake.requests[-1]["key"] == key.replace("+", " ")


def test_injected_failure_then_recovery(fake):
    fake.put(ROOT + "x.mp4", b"abc")
    fake.fail_next(ROOT + "x.mp4", 2, status=503)
    url = fake.url + "/" + ROOT + "x.mp4"
    assert _get(url)[0] == 503
    assert _get(url)[0] == 503
    assert _get(url)[::2] == (200, b"abc")


def test_failure_is_per_method_and_listing_can_fail(fake):
    fake.put(ROOT + "x.mp4", b"abc")
    fake.fail_next(ROOT + "x.mp4", 1, method="HEAD")
    assert _get(fake.url + "/" + ROOT + "x.mp4")[0] == 200
    connection = http.client.HTTPConnection("127.0.0.1", int(fake.url.rsplit(":", 1)[1]), timeout=5)
    connection.request("HEAD", "/" + ROOT + "x.mp4")
    response = connection.getresponse()
    response.read()
    assert response.status == 500
    connection.request("HEAD", "/" + ROOT + "x.mp4")
    assert connection.getresponse().status == 200
    connection.close()

    fake.fail_next(LISTING, 1, status=503)
    assert _list(fake, prefix=ROOT)[0] == 503
    assert _list(fake, prefix=ROOT)[0] == 200


def test_fail_next_and_truncate_next_validate_arguments(fake):
    with pytest.raises(ValueError, match="times"):
        fake.fail_next(ROOT + "x.mp4", -1)
    with pytest.raises(ValueError, match="method"):
        fake.fail_next(ROOT + "x.mp4", 1, method="POST")
    with pytest.raises(ValueError, match="times"):
        fake.truncate_next(ROOT + "x.mp4", -1)
    with pytest.raises(TypeError, match="body"):
        fake.put(ROOT + "x.mp4", "text")


def test_truncated_body_sends_half_then_closes(fake):
    body = bytes(1000)
    fake.put(ROOT + "x.mp4", body)
    fake.truncate_next(ROOT + "x.mp4", 1)
    connection = http.client.HTTPConnection("127.0.0.1", int(fake.url.rsplit(":", 1)[1]), timeout=5)
    connection.request("GET", "/" + ROOT + "x.mp4", headers={"Range": "bytes=0-999"})
    response = connection.getresponse()
    assert response.status == 206
    assert response.getheader("Content-Length") == "1000"
    with pytest.raises(http.client.IncompleteRead) as caught:
        response.read()
    assert len(caught.value.partial) == 500
    connection.close()
    assert _get(fake.url + "/" + ROOT + "x.mp4")[2] == body


def test_keep_alive_serves_many_requests_on_one_connection(fake):
    fake.put(ROOT + "x.mp4", bytes(range(100)))
    connection = http.client.HTTPConnection("127.0.0.1", int(fake.url.rsplit(":", 1)[1]), timeout=5)
    for start in (0, 10, 20):
        connection.request("GET", "/" + ROOT + "x.mp4", headers={"Range": "bytes=%d-%d" % (start, start + 9)})
        response = connection.getresponse()
        assert response.read() == bytes(range(start, start + 10))
    sock = connection.sock
    assert sock is not None
    connection.close()


def test_requests_log_records_method_path_query_range_and_time(fake):
    fake.put(ROOT + "x.mp4", b"0123456789")
    before = time.time()
    _get(fake.url + "/" + ROOT + "x.mp4", {"Range": "bytes=2-3"})
    _list(fake, prefix=ROOT, delimiter="/")
    first, second = fake.requests
    assert first["method"] == "GET" and first["path"] == "/" + ROOT + "x.mp4"
    assert first["range"] == "bytes=2-3" and first["key"] == ROOT + "x.mp4"
    assert before <= first["time"] <= time.time()
    assert second["path"] == "/" and second["key"] == LISTING
    assert second["query"] == {"list-type": "2", "prefix": ROOT, "delimiter": "/"}
    assert second["range"] is None
    assert fake.count("GET") == 2 and fake.count("GET", ROOT + "x.mp4") == 1 and fake.count("HEAD") == 0
    assert [entry["range"] for entry in fake.gets(ROOT + "x.mp4")] == ["bytes=2-3"]


def test_delay_and_in_flight_counter(fake):
    fake.put(ROOT + "x.mp4", b"abc")
    fake.delay_seconds = 0.2
    started = time.time()
    threads = [threading.Thread(target=_get, args=(fake.url + "/" + ROOT + "x.mp4",)) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert time.time() - started >= 0.2
    assert fake.max_in_flight >= 2
    assert fake.in_flight == 0


def test_drop_connections_kills_keep_alive_but_not_the_server(fake):
    fake.put(ROOT + "x.mp4", b"abc")
    connection = http.client.HTTPConnection("127.0.0.1", int(fake.url.rsplit(":", 1)[1]), timeout=5)
    connection.request("GET", "/" + ROOT + "x.mp4")
    assert connection.getresponse().read() == b"abc"
    fake.drop_connections()
    with pytest.raises((http.client.HTTPException, OSError)):
        connection.request("GET", "/" + ROOT + "x.mp4")
        connection.getresponse()
    connection.close()
    assert _get(fake.url + "/" + ROOT + "x.mp4")[2] == b"abc"


def test_stop_refuses_new_connections_and_is_idempotent():
    server = FakeS3()
    url = server.start()
    server.put(ROOT + "x.mp4", b"abc")
    assert _get(url + "/" + ROOT + "x.mp4")[0] == 200
    server.stop()
    server.stop()
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(url + "/" + ROOT + "x.mp4", timeout=2)
    with pytest.raises(RuntimeError, match="not running"):
        server.url  # noqa: B018


def test_context_manager_starts_and_stops():
    with FakeS3() as server:
        server.put(ROOT + "x.mp4", b"abc")
        assert _get(server.url + "/" + ROOT + "x.mp4")[2] == b"abc"
        url = server.url
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(url + "/" + ROOT + "x.mp4", timeout=2)
