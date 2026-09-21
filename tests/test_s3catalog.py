"""Tests for screener.s3catalog: bucket listing, pagination, filtering, and the saved copy."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from screener import s3catalog
from screener.keys import InvalidKey
from screener.s3catalog import CatalogEntry, CatalogError, CatalogPage, S3Catalog
from tests.fakes3 import LISTING, S3_NAMESPACE, FakeS3

ROOT = "TCRMP_video_ondeck/"
YEAR = ROOT + "2024Annual/"


class Clock:
    """A hand-set clock, so tests age a saved page without sleeping."""

    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class CannedServer:
    """Answers every GET with the next prepared (status, body) pair, for malformed-answer tests."""

    def __init__(self, answers):
        outer = self
        self.answers = list(answers)
        self.hits = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                pass

            def do_GET(self):  # noqa: N802
                status, body = outer.answers[min(outer.hits, len(outer.answers) - 1)]
                outer.hits += 1
                self.send_response(status)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def fake():
    server = FakeS3()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def canned():
    servers = []

    def make(answers):
        server = CannedServer(answers)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()


def _page_xml(contents="", truncated="false", extra=""):
    return (
        '<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="%s"><Name>b</Name>'
        "<IsTruncated>%s</IsTruncated>%s%s</ListBucketResult>" % (S3_NAMESPACE, truncated, extra, contents)
    ).encode("utf-8")


def test_lists_folders_and_videos(fake, tmp_path):
    fake.put(YEAR + "TCRMP20241022_video_FLC_T1.MP4", b"x" * 10)
    fake.put(YEAR + "TCRMP20241022_video_FLC_T2.MP4", b"x" * 20)
    fake.put(ROOT + "main/TCRMP2004_video/old.avi", b"x")
    fake.put(ROOT + "loose.mov", b"xyz")

    catalog = S3Catalog(fake.url, tmp_path / "catalog.json")
    top = catalog.list(ROOT)
    assert isinstance(top, CatalogPage)
    assert top.prefix == ROOT
    assert top.folders == [YEAR, ROOT + "main/"]
    assert top.videos == [CatalogEntry(key=ROOT + "loose.mov", name="loose.mov", size=3, ext="mov", playable=True)]
    assert top.stale is False

    year = catalog.list(YEAR)
    assert year.folders == []
    assert [(video.name, video.size) for video in year.videos] == [
        ("TCRMP20241022_video_FLC_T1.MP4", 10),
        ("TCRMP20241022_video_FLC_T2.MP4", 20),
    ]
    assert year.videos[0].key == YEAR + "TCRMP20241022_video_FLC_T1.MP4"

    listing = [entry for entry in fake.requests if entry["key"] == LISTING]
    assert listing[0]["query"] == {"list-type": "2", "prefix": ROOT, "delimiter": "/"}


def test_follows_pagination(fake, tmp_path):
    for index in range(5):
        fake.put(YEAR + "v%d.mp4" % index, b"x")
    fake.put(YEAR + "sub/inner.mp4", b"x")
    fake.page_size = 2
    page = S3Catalog(fake.url, tmp_path / "catalog.json").list(YEAR)
    assert [video.name for video in page.videos] == ["v0.mp4", "v1.mp4", "v2.mp4", "v3.mp4", "v4.mp4"]
    assert page.folders == [YEAR + "sub/"]
    assert fake.count("GET", LISTING) == 3
    tokens = [entry["query"].get("continuation-token") for entry in fake.requests]
    assert tokens[0] is None and all(tokens[1:])


def test_filters_non_video_and_zero_byte_keys(fake, tmp_path):
    fake.put(YEAR + "keep.mp4", b"x")
    fake.put(YEAR + "empty.mp4", b"")
    fake.put(YEAR + "notes.txt", b"x")
    fake.put(YEAR + "Thumbs.db", b"x")
    fake.put(YEAR + "noextension", b"x")
    fake.put(YEAR, b"")
    page = S3Catalog(fake.url, tmp_path / "catalog.json").list(YEAR)
    assert [video.name for video in page.videos] == ["keep.mp4"]


def test_natural_sort(fake, tmp_path):
    for name in ("x_T10.mp4", "x_T2.mp4", "x_T1.mp4", "X_t3.mp4", "x_T1-6.mp4"):
        fake.put(YEAR + name, b"x")
    for folder in ("TCRMP2010_video/", "TCRMP2004_video/", "Set10/", "Set9/", "set1/"):
        fake.put(YEAR + folder + "v.mp4", b"x")
    page = S3Catalog(fake.url, tmp_path / "catalog.json").list(YEAR)
    assert [video.name for video in page.videos] == ["x_T1.mp4", "x_T1-6.mp4", "x_T2.mp4", "X_t3.mp4", "x_T10.mp4"]
    assert page.folders == [
        YEAR + "set1/",
        YEAR + "Set9/",
        YEAR + "Set10/",
        YEAR + "TCRMP2004_video/",
        YEAR + "TCRMP2010_video/",
    ]


def test_natural_key_orders_digit_runs_by_value():
    names = ["T10", "T2", "T1", "T1a", "T01", "A", "10", "9"]
    assert sorted(names, key=s3catalog.natural_key) == ["9", "10", "A", "T01", "T1", "T1a", "T2", "T10"]


def test_playable_flag_by_extension(fake, tmp_path):
    names = ["a.MP4", "b.m4v", "c.MOV", "d.MTS", "e.m2t", "f.avi", "g.mxf", "h.wmv"]
    for name in names:
        fake.put(YEAR + name, b"x")
    page = S3Catalog(fake.url, tmp_path / "catalog.json").list(YEAR)
    assert [(video.ext, video.playable) for video in page.videos] == [
        ("mp4", True),
        ("m4v", True),
        ("mov", True),
        ("mts", False),
        ("m2t", False),
        ("avi", False),
        ("mxf", False),
        ("wmv", False),
    ]


def test_keys_with_plus_and_spaces_round_trip(fake, tmp_path):
    folder = ROOT + "main/TCRMP2005 video+extra/"
    names = ["TCRMP20051013_video_SSJ_T1+T3-6.mp4", "TCRMP20110419_video_GBF_T5.2-6.avi", "MVI 0203.MOV", "a&b.mp4"]
    for name in names:
        fake.put(folder + name, b"x")
    catalog = S3Catalog(fake.url, tmp_path / "catalog.json")
    assert catalog.list(ROOT + "main/").folders == [folder]
    page = catalog.list(folder)
    assert sorted(video.key for video in page.videos) == sorted(folder + name for name in names)
    assert fake.requests[-1]["query"]["prefix"] == folder

    reloaded = S3Catalog(fake.url, tmp_path / "catalog.json").list(folder)
    assert reloaded.videos == page.videos


@pytest.mark.parametrize(
    "bad",
    ["", "other/", "TCRMP_video_ondeck", "TCRMP_video_ondeck/../", "TCRMP_video_ondeck//", "TCRMP_video_ondeck/a\nb/", None, 7],
)
def test_rejects_prefix_outside_tcrmp(fake, tmp_path, bad):
    catalog = S3Catalog(fake.url, tmp_path / "catalog.json")
    with pytest.raises(InvalidKey, match="prefix"):
        catalog.list(bad)
    assert fake.requests == []


def test_serves_saved_page_without_network(fake, tmp_path):
    fake.put(YEAR + "a.mp4", b"x")
    clock = Clock()
    url = fake.url
    first = S3Catalog(url, tmp_path / "catalog.json", clock=clock).list(YEAR)
    fake.stop()

    clock.now += 3600
    again = S3Catalog(url, tmp_path / "catalog.json", clock=clock).list(YEAR)
    assert again.videos == first.videos
    assert again.stale is False


def test_saved_page_is_served_from_memory_without_a_second_request(fake, tmp_path):
    fake.put(YEAR + "a.mp4", b"x")
    catalog = S3Catalog(fake.url, tmp_path / "catalog.json")
    catalog.list(YEAR)
    catalog.list(YEAR)
    assert fake.count("GET", LISTING) == 1


def test_saved_page_expires_after_max_age(fake, tmp_path):
    fake.put(YEAR + "a.mp4", b"x")
    clock = Clock()
    catalog = S3Catalog(fake.url, tmp_path / "catalog.json", max_age_seconds=100, clock=clock)
    catalog.list(YEAR)
    clock.now += 99
    catalog.list(YEAR)
    assert fake.count("GET", LISTING) == 1
    fake.put(YEAR + "b.mp4", b"x")
    clock.now += 2
    assert [video.name for video in catalog.list(YEAR).videos] == ["a.mp4", "b.mp4"]
    assert fake.count("GET", LISTING) == 2


def test_refresh_bypasses_saved_page(fake, tmp_path):
    fake.put(YEAR + "a.mp4", b"x")
    catalog = S3Catalog(fake.url, tmp_path / "catalog.json")
    catalog.list(YEAR)
    fake.put(YEAR + "b.mp4", b"x")
    assert [video.name for video in catalog.list(YEAR).videos] == ["a.mp4"]
    fresh = catalog.list(YEAR, refresh=True)
    assert [video.name for video in fresh.videos] == ["a.mp4", "b.mp4"]
    assert fresh.stale is False
    assert [video.name for video in S3Catalog(fake.url, tmp_path / "catalog.json").list(YEAR).videos] == ["a.mp4", "b.mp4"]


def test_network_failure_returns_stale_page(fake, tmp_path):
    fake.put(YEAR + "a.mp4", b"x")
    clock = Clock()
    catalog = S3Catalog(fake.url, tmp_path / "catalog.json", timeout=2.0, max_age_seconds=100, clock=clock)
    first = catalog.list(YEAR)
    fake.stop()

    clock.now += 1000
    expired = catalog.list(YEAR)
    assert expired.stale is True
    assert expired.videos == first.videos and expired.folders == first.folders

    forced = catalog.list(YEAR, refresh=True)
    assert forced.stale is True and forced.videos == first.videos


def test_http_error_returns_stale_page_when_one_exists(fake, tmp_path):
    fake.put(YEAR + "a.mp4", b"x")
    catalog = S3Catalog(fake.url, tmp_path / "catalog.json")
    catalog.list(YEAR)
    fake.fail_next(LISTING, 1, status=503)
    page = catalog.list(YEAR, refresh=True)
    assert page.stale is True and [video.name for video in page.videos] == ["a.mp4"]
    assert catalog.list(YEAR, refresh=True).stale is False


def test_network_failure_without_saved_page_raises(tmp_path):
    fake = FakeS3()
    url = fake.start()
    fake.stop()
    catalog = S3Catalog(url, tmp_path / "catalog.json", timeout=2.0)
    with pytest.raises(CatalogError) as caught:
        catalog.list(YEAR)
    assert url in str(caught.value)
    assert not (tmp_path / "catalog.json").exists()


def test_http_error_without_saved_page_raises_with_status(fake, tmp_path):
    fake.fail_next(LISTING, 1, status=503)
    with pytest.raises(CatalogError, match="503") as caught:
        S3Catalog(fake.url, tmp_path / "catalog.json").list(YEAR)
    assert fake.url in str(caught.value)


def test_failure_on_a_later_page_discards_the_partial_listing(fake, tmp_path):
    for index in range(4):
        fake.put(YEAR + "v%d.mp4" % index, b"x")
    fake.page_size = 2
    catalog = S3Catalog(fake.url, tmp_path / "catalog.json")

    original = fake._answer_listing
    calls = []

    def flaky(handler, method, query):
        calls.append(query)
        if len(calls) == 2:
            fake._send_error(handler, method, 500, "InternalError", "second page fails")
            return
        original(handler, method, query)

    fake._answer_listing = flaky
    with pytest.raises(CatalogError, match="500"):
        catalog.list(YEAR)
    assert [video.name for video in catalog.list(YEAR).videos] == ["v0.mp4", "v1.mp4", "v2.mp4", "v3.mp4"]


def test_malformed_xml_raises(canned, tmp_path):
    server = canned([(200, b"<html><body>Sign in to hotel wifi</body>")])
    with pytest.raises(CatalogError, match="XML") as caught:
        S3Catalog(server.url, tmp_path / "catalog.json").list(YEAR)
    assert server.url in str(caught.value)
    assert not (tmp_path / "catalog.json").exists()


def test_page_cut_off_mid_body_raises(canned, tmp_path):
    whole = _page_xml("<Contents><Key>%sa.mp4</Key><Size>5</Size></Contents>" % YEAR)
    server = canned([(200, whole[: len(whole) // 2])])
    with pytest.raises(CatalogError, match="XML"):
        S3Catalog(server.url, tmp_path / "catalog.json").list(YEAR)


def test_wrong_root_element_raises(canned, tmp_path):
    server = canned([(200, b'<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>')])
    with pytest.raises(CatalogError, match="ListBucketResult"):
        S3Catalog(server.url, tmp_path / "catalog.json").list(YEAR)


def test_truncated_page_without_token_raises(canned, tmp_path):
    server = canned([(200, _page_xml(truncated="true"))])
    with pytest.raises(CatalogError, match="NextContinuationToken"):
        S3Catalog(server.url, tmp_path / "catalog.json").list(YEAR)


def test_repeating_token_raises_instead_of_looping(canned, tmp_path):
    body = _page_xml(truncated="true", extra="<NextContinuationToken>same</NextContinuationToken>")
    server = canned([(200, body)])
    with pytest.raises(CatalogError, match="token"):
        S3Catalog(server.url, tmp_path / "catalog.json").list(YEAR)
    assert server.hits == 2


def test_bad_size_raises(canned, tmp_path):
    body = _page_xml("<Contents><Key>%sa.mp4</Key><Size>big</Size></Contents>" % YEAR)
    server = canned([(200, body)])
    with pytest.raises(CatalogError, match="Size"):
        S3Catalog(server.url, tmp_path / "catalog.json").list(YEAR)


def test_malformed_answer_with_saved_page_returns_stale(canned, tmp_path):
    good = _page_xml("<Contents><Key>%sa.mp4</Key><Size>5</Size></Contents>" % YEAR)
    server = canned([(200, good), (200, b"not xml at all")])
    catalog = S3Catalog(server.url, tmp_path / "catalog.json")
    assert catalog.list(YEAR).stale is False
    page = catalog.list(YEAR, refresh=True)
    assert page.stale is True and [video.name for video in page.videos] == ["a.mp4"]


def test_hostile_keys_in_listing_are_skipped(canned, tmp_path):
    contents = "".join(
        "<Contents><Key>%s</Key><Size>5</Size></Contents>" % key
        for key in (
            YEAR + "good.mp4",
            YEAR + "back\\slash.mp4",
            YEAR + "../escape.mp4",
            "elsewhere/outside.mp4",
            YEAR + "double//slash.mp4",
        )
    )
    prefixes = "".join(
        "<CommonPrefixes><Prefix>%s</Prefix></CommonPrefixes>" % prefix
        for prefix in (YEAR + "fine/", YEAR + "bad//", "elsewhere/", YEAR + "../")
    )
    server = canned([(200, _page_xml(contents + prefixes))])
    page = S3Catalog(server.url, tmp_path / "catalog.json").list(YEAR)
    assert [video.key for video in page.videos] == [YEAR + "good.mp4"]
    assert page.folders == [YEAR + "fine/"]


def test_corrupt_cache_file_is_ignored_and_rewritten(fake, tmp_path):
    fake.put(YEAR + "a.mp4", b"x")
    path = tmp_path / "catalog.json"
    for garbage in (b"{not json", b"[]", b'{"version": 1, "pages": {"TCRMP_video_ondeck/2024Annual/": {"fetched_at": "x"}}}', b"\xff\xfe"):
        path.write_bytes(garbage)
        page = S3Catalog(fake.url, path).list(YEAR)
        assert [video.name for video in page.videos] == ["a.mp4"]
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert YEAR in saved["pages"]


def test_cache_file_with_a_key_outside_the_folder_is_ignored(fake, tmp_path):
    fake.put(YEAR + "a.mp4", b"x")
    path = tmp_path / "catalog.json"
    S3Catalog(fake.url, path).list(YEAR)
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["pages"][YEAR]["videos"][0]["key"] = "elsewhere/secret.mp4"
    path.write_text(json.dumps(saved), encoding="utf-8")
    page = S3Catalog(fake.url, path).list(YEAR)
    assert [video.key for video in page.videos] == [YEAR + "a.mp4"]
    assert fake.count("GET", LISTING) == 2


def test_cache_file_from_another_bucket_is_ignored(fake, tmp_path):
    fake.put(YEAR + "a.mp4", b"x")
    path = tmp_path / "catalog.json"
    S3Catalog(fake.url, path).list(YEAR)

    other = FakeS3()
    other.start()
    try:
        other.put(YEAR + "different.mp4", b"x")
        page = S3Catalog(other.url, path).list(YEAR)
        assert [video.name for video in page.videos] == ["different.mp4"]
    finally:
        other.stop()


def test_works_without_cache_path(fake):
    fake.put(YEAR + "a.mp4", b"x")
    catalog = S3Catalog(fake.url)
    assert [video.name for video in catalog.list(YEAR).videos] == ["a.mp4"]
    catalog.list(YEAR)
    assert fake.count("GET", LISTING) == 1


def test_cache_folder_is_created_and_unwritable_path_does_not_break_listing(fake, tmp_path, capsys):
    fake.put(YEAR + "a.mp4", b"x")
    nested = tmp_path / "new" / "folder" / "catalog.json"
    S3Catalog(fake.url, nested).list(YEAR)
    assert nested.exists()

    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a folder should be", encoding="utf-8")
    page = S3Catalog(fake.url, blocker / "catalog.json").list(YEAR)
    assert [video.name for video in page.videos] == ["a.mp4"]
    assert "catalog" in capsys.readouterr().err


def test_no_temp_files_left_beside_the_cache_file(fake, tmp_path):
    fake.put(YEAR + "a.mp4", b"x")
    catalog = S3Catalog(fake.url, tmp_path / "catalog.json")
    catalog.list(YEAR)
    catalog.list(ROOT)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["catalog.json"]


def test_concurrent_lists_are_safe(fake, tmp_path):
    for folder in range(6):
        for index in range(3):
            fake.put(ROOT + "f%d/v%d.mp4" % (folder, index), b"x")
    path = tmp_path / "catalog.json"
    catalog = S3Catalog(fake.url, path)
    errors = []

    def work(folder):
        try:
            for _ in range(5):
                page = catalog.list(ROOT + "f%d/" % folder, refresh=True)
                assert len(page.videos) == 3
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=work, args=(folder,)) for folder in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(saved["pages"]) == [ROOT + "f%d/" % folder for folder in range(6)]


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"bucket_url": "ftp://example.com"}, "bucket_url"),
        ({"bucket_url": ""}, "bucket_url"),
        ({"bucket_url": None}, "bucket_url"),
        ({"bucket_url": "https://"}, "bucket_url"),
        ({"timeout": 0}, "timeout"),
        ({"timeout": "fast"}, "timeout"),
        ({"max_age_seconds": -1}, "max_age_seconds"),
        ({"clock": 5}, "clock"),
        ({"cache_path": 5}, "cache_path"),
    ],
)
def test_constructor_validates_arguments(kwargs, match):
    arguments = {"bucket_url": "http://127.0.0.1:1"}
    arguments.update(kwargs)
    with pytest.raises(ValueError, match=match):
        S3Catalog(**arguments)


def test_refresh_must_be_a_bool(fake, tmp_path):
    with pytest.raises(ValueError, match="refresh"):
        S3Catalog(fake.url, tmp_path / "catalog.json").list(YEAR, refresh="yes")
