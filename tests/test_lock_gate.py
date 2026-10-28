"""Tests for the VICARIUS module lock: screener.lock_status and the gate in the request handler.

The registry is a temporary module_locks.json that VICARIUS_LOCKS_PATH points
at, so the real platform registry is never read or written. The gate tests
start a real server on a free port with temporary folders and a FakeS3.
"""

import json
from pathlib import Path

import pytest

from screener import lock_status, lockgate
from tests.app_helpers import KEY_T1, observation, start_app
from tests.fakes3 import FakeS3

MAILTO = 'href="mailto:lauren.olinger@uvi.edu"'


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """Point the reader at a temporary registry and return a function that writes it."""
    path = tmp_path / "module_locks.json"
    monkeypatch.setenv("VICARIUS_LOCKS_PATH", str(path))
    monkeypatch.delenv("VICARIUS_LOCK_BYPASS", raising=False)
    monkeypatch.setattr(lock_status, "_warned", set())

    def write(content: object) -> None:
        """Write content as JSON, or as raw text when it is a str."""
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")

    return write


@pytest.fixture
def fake():
    """A FakeS3 with one small video."""
    server = FakeS3()
    server.start()
    server.put(KEY_T1, b"\0" * 5000)
    yield server
    server.stop()


@pytest.fixture
def running(tmp_path, fake, registry):
    """One running standalone app on temporary folders."""
    app = start_app(tmp_path, fake.url, data_dir=tmp_path / "data")
    yield app
    app.stop()


def lock(registry, locked: bool = True) -> None:
    """Write a registry that locks or unlocks sponge_screener."""
    registry({"schema_version": 1, "locks": {"sponge_screener": {"locked": locked, "by": "LO"}}})


# ----- lock_status -----


def test_entry_is_read_fresh_on_every_call(registry):
    lock(registry, True)
    assert lock_status.is_locked() is True
    lock(registry, False)
    assert lock_status.is_locked() is False


def test_other_modules_and_a_missing_entry_leave_the_screener_unlocked(registry):
    registry({"locks": {"reef_point_seg": {"locked": True}}})
    assert lock_status.is_locked() is False


def test_missing_registry_file_is_unlocked(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VICARIUS_LOCKS_PATH", str(tmp_path / "absent.json"))
    monkeypatch.delenv("VICARIUS_LOCK_BYPASS", raising=False)
    monkeypatch.setattr(lock_status, "_warned", set())
    assert lock_status.is_locked() is False
    assert "absent.json" in capsys.readouterr().err


@pytest.mark.parametrize(
    "content",
    [
        "not json{",
        "",
        "[]",
        {"locks": []},
        {"locks": {"sponge_screener": "yes"}},
        {"locks": {"sponge_screener": {}}},
        {"locks": {"sponge_screener": {"locked": "true"}}},
        {"locks": {"sponge_screener": {"locked": 1}}},
        "\x00\x01\x02 binary junk",
    ],
)
def test_malformed_registries_are_fail_open_with_one_warning(registry, capsys, content):
    registry(content)
    assert lock_status.is_locked() is False
    assert lock_status.is_locked() is False
    warnings = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(warnings) == 1 and "sponge_screener" in warnings[0]


def test_bypass_wins_over_a_locked_registry(registry, monkeypatch):
    lock(registry, True)
    monkeypatch.setenv("VICARIUS_LOCK_BYPASS", "1")
    assert lock_status.is_locked() is False


def test_default_path_derives_from_vicarius_root(monkeypatch):
    monkeypatch.delenv("VICARIUS_LOCKS_PATH", raising=False)
    assert str(lock_status.locks_path()) == lock_status.LOCKS_PATH
    assert lock_status.LOCKS_PATH.endswith("/_METADATA/module_locks.json")
    assert lock_status.MODULE_SLUG == "sponge_screener"


# ----- lockgate.refusal, without a server -----


@pytest.mark.parametrize(
    "method, path, accept, expected",
    [
        ("GET", "/", None, True),
        ("GET", "/", "text/html,application/xhtml+xml", True),
        ("GET", "/", "*/*", True),
        ("GET", "/anything", "", True),
        ("GET", "/", "application/json", False),
        ("GET", "/api/species", "text/html", False),
        ("GET", "/media/crops/ID001_ACAU_TOPLEFT_FLC_T1.png", "*/*", False),
        ("GET", "/guide/guide.json", "*/*", False),
        ("GET", "/video", "*/*", False),
        ("POST", "/", "text/html", False),
    ],
)
def test_page_request_rule(method, path, accept, expected):
    assert lockgate.is_page_request(method, path, accept) is expected


def test_refusal_is_none_while_unlocked(registry):
    lock(registry, False)
    assert lockgate.refusal("GET", "/api/species", None) is None


def test_refusal_lets_static_files_and_health_through(registry):
    lock(registry, True)
    assert lockgate.refusal("GET", "/static/style.css", None) is None
    assert lockgate.refusal("GET", "/api/health", "application/json") is None


def test_lock_page_carries_the_exact_copy_and_the_credit():
    page = lockgate.LOCK_PAGE_HTML
    assert "Under development. Please come back later." in page
    assert MAILTO + ">Email Lauren</a> for questions." in page
    assert "Lauren Olinger" in page


# ----- the gate in the running server -----


def test_unlocked_app_serves_the_page(running, registry):
    lock(registry, False)
    answer = running.client.get("/", headers={"Accept": "text/html"})
    assert answer.status == 200
    assert "<title>Sponge Screener</title>" in answer.text


def test_locked_page_request_gets_the_lock_page(running, registry):
    lock(registry, True)
    answer = running.client.get("/?project_dir=/x&initials=LO", headers={"Accept": "text/html"})
    assert answer.status == 200
    assert answer.headers["Content-Type"].startswith("text/html")
    assert "Under development. Please come back later." in answer.text
    assert MAILTO in answer.text
    assert "app.js" not in answer.text


@pytest.mark.parametrize(
    "path",
    ["/api/species", "/api/tally", "/api/catalog", "/media/crops/ID001_ACAU_TOPLEFT_FLC_T1.png", "/video?key=" + KEY_T1],
)
def test_locked_api_reads_answer_423(running, registry, path):
    lock(registry, True)
    answer = running.client.get(path, headers={"Accept": "application/json"})
    assert answer.status == 423
    assert answer.error.startswith("locked: Under development.")


def test_locked_save_answers_423_and_writes_nothing(running, registry, tmp_path):
    lock(registry, True)
    answer = running.client.post("/api/observations", observation())
    assert answer.status == 423
    assert not (tmp_path / "data" / "observations.csv").exists()


def test_locked_post_without_the_page_header_still_answers_423(running, registry):
    lock(registry, True)
    answer = running.client.post("/api/settings", {"annotator": "LO"}, mutation_header=False)
    assert answer.status == 423


def test_locked_project_open_answers_423(running, registry):
    lock(registry, True)
    answer = running.client.post("/api/project/open", {"project_dir": "/nowhere"}, mutation_header=False)
    assert answer.status == 423


def test_static_files_and_health_pass_while_locked(running, registry):
    lock(registry, True)
    assert running.client.get("/static/style.css").status == 200
    health = running.client.get("/api/health")
    assert health.status == 200
    assert health.json()["locked"] is True


def test_unlock_takes_effect_on_the_next_request(running, registry):
    lock(registry, True)
    assert running.client.get("/api/species").status == 423
    lock(registry, False)
    answer = running.client.get("/api/species")
    assert answer.status == 200
    assert running.client.get("/api/health").json()["locked"] is False


def test_wrong_host_is_refused_before_the_lock(running, registry):
    lock(registry, True)
    answer = running.client.get("/", headers={"Host": "evil.example:80", "Accept": "text/html"})
    assert answer.status == 403


def test_corrupt_registry_leaves_the_app_usable(running, registry):
    registry("{{{ not json")
    assert running.client.get("/api/species").status == 200
