"""Tests for screener.settings: the annotator, the pins, and the export folder on disk."""

import json
import os
import re
from pathlib import Path

import pytest

from screener import settings as settings_module
from screener.config import DEFAULT_ANNOTATOR, DEFAULT_EXPORT_ROOT
from screener.settings import Settings, load_settings, save_settings, validate_annotator
from screener.species import PIN_KEYS, Species, default_pins

# The settings tests bring their own species, so edits to the shipped species
# file cannot change what these tests expect.
SPECIES = [
    Species(code="ACAU", name="Aplysina cauliformis", part="1", default_pin="1"),
    Species(code="AFUL", name="Aplysina fulva", part="2", default_pin="2"),
    Species(code="CDEL", name="Cliona delitrix", part="1", default_pin="3"),
    Species(code="XMUT", name="Xestospongia muta", part="2", default_pin="8"),
    Species(code="UNKN", name="Unknown sponge", part="", default_pin=""),
]
SET_ASIDE_PATTERN = re.compile(r"^settings\.json\.corrupt-\d{8}T\d{6}Z(-\d+)?$")


def defaults():
    """Return the settings a fresh install starts with."""
    return Settings(annotator=DEFAULT_ANNOTATOR, pins=default_pins(SPECIES), export_root=str(DEFAULT_EXPORT_ROOT))


def custom_pins():
    """Return pins that differ from the shipped defaults."""
    pins = {key: None for key in PIN_KEYS}
    pins.update({"1": "XMUT", "9": "UNKN", "0": "ACAU"})
    return pins


def set_aside_files(folder):
    """Return the set-aside copies of settings.json in a folder."""
    return sorted(path for path in folder.iterdir() if path.name.startswith("settings.json.corrupt-"))


def test_missing_file_gives_defaults(tmp_path):
    path = tmp_path / "settings.json"
    loaded = load_settings(path, SPECIES)
    assert loaded == defaults()
    assert loaded.annotator == "LO"
    assert loaded.pins["1"] == "ACAU" and loaded.pins["9"] is None
    assert list(tmp_path.iterdir()) == []


def test_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    saved = Settings(annotator="M.K-2_b", pins=custom_pins(), export_root=str(tmp_path / "exports with space"))
    save_settings(path, saved)
    assert load_settings(path, SPECIES) == saved
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {"annotator": "M.K-2_b", "pins": custom_pins(), "export_root": str(tmp_path / "exports with space")}
    assert [item.name for item in tmp_path.iterdir()] == ["settings.json"]


def test_save_replaces_an_older_file_and_creates_the_folder(tmp_path):
    path = tmp_path / "data" / "nested" / "settings.json"
    save_settings(path, defaults())
    newer = Settings(annotator="AB", pins=custom_pins(), export_root="/tmp/elsewhere")
    save_settings(path, newer)
    assert load_settings(path, SPECIES) == newer


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"",
        b"[1, 2, 3]",
        b'"just text"',
        b"42",
        b"null",
        b'{"annotator": "LO"',
        b'{"annotator": "\xff\xfe"}',
        b"[" * 100000,
    ],
)
def test_corrupt_file_is_set_aside_and_defaults_returned(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_bytes(content)
    loaded = load_settings(path, SPECIES)
    assert loaded == defaults()
    assert not path.exists()
    kept = set_aside_files(tmp_path)
    assert len(kept) == 1
    assert SET_ASIDE_PATTERN.match(kept[0].name)
    assert kept[0].read_bytes() == content


def test_two_corrupt_files_in_one_second_are_both_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_module, "_utc_stamp", lambda: "20260921T120000Z")
    path = tmp_path / "settings.json"
    path.write_text("first {", encoding="utf-8")
    load_settings(path, SPECIES)
    path.write_text("second {", encoding="utf-8")
    load_settings(path, SPECIES)
    kept = set_aside_files(tmp_path)
    assert sorted(item.read_text(encoding="utf-8") for item in kept) == ["first {", "second {"]
    assert all(SET_ASIDE_PATTERN.match(item.name) for item in kept)


def test_corrupt_file_that_cannot_be_set_aside_raises_os_error_and_stays(tmp_path):
    folder = tmp_path / "locked"
    folder.mkdir()
    path = folder / "settings.json"
    path.write_text("{broken", encoding="utf-8")
    folder.chmod(0o555)
    try:
        with pytest.raises(OSError):
            load_settings(path, SPECIES)
    finally:
        folder.chmod(0o755)
    assert path.read_text(encoding="utf-8") == "{broken"


def test_save_after_a_corrupt_load_starts_a_clean_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{broken", encoding="utf-8")
    loaded = load_settings(path, SPECIES)
    save_settings(path, loaded)
    assert load_settings(path, SPECIES) == defaults()
    assert len(set_aside_files(tmp_path)) == 1


def test_stale_pin_code_is_dropped_on_load(tmp_path):
    path = tmp_path / "settings.json"
    pins = custom_pins()
    pins["2"] = "ZZZZ"
    path.write_text(json.dumps({"annotator": "LO", "pins": pins, "export_root": "/tmp/x"}), encoding="utf-8")
    loaded = load_settings(path, SPECIES)
    assert loaded.pins == {**custom_pins(), "2": None}
    assert path.exists()
    assert set_aside_files(tmp_path) == []


def test_load_repairs_pins_that_repeat_a_code_lack_keys_or_hold_wrong_types(tmp_path):
    path = tmp_path / "settings.json"
    pins = {"1": "ACAU", "2": "ACAU", "3": 7, "4": ["AFUL"], "5": "afu", "6": "CDEL", "extra": "XMUT"}
    path.write_text(json.dumps({"annotator": "LO", "pins": pins, "export_root": "/tmp/x"}), encoding="utf-8")
    loaded = load_settings(path, SPECIES)
    expected = {key: None for key in PIN_KEYS}
    expected.update({"1": "ACAU", "6": "CDEL"})
    assert loaded.pins == expected
    assert tuple(loaded.pins) == PIN_KEYS


@pytest.mark.parametrize("pins", [None, [], "ACAU", 5, True])
def test_load_uses_default_pins_when_pins_is_not_an_object(tmp_path, pins):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"annotator": "AB", "pins": pins, "export_root": "/tmp/x"}), encoding="utf-8")
    loaded = load_settings(path, SPECIES)
    assert loaded == Settings(annotator="AB", pins=default_pins(SPECIES), export_root="/tmp/x")


def test_load_fills_each_missing_or_invalid_field_with_its_default(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"export_root": "/tmp/only-root"}), encoding="utf-8")
    assert load_settings(path, SPECIES) == Settings(
        annotator=DEFAULT_ANNOTATOR, pins=default_pins(SPECIES), export_root="/tmp/only-root"
    )
    path.write_text(json.dumps({"annotator": "way too long a name", "export_root": ["/tmp"], "unknown": 1}), encoding="utf-8")
    assert load_settings(path, SPECIES) == defaults()
    path.write_text(json.dumps({"annotator": "<b>", "export_root": "   "}), encoding="utf-8")
    assert load_settings(path, SPECIES) == defaults()
    path.write_text(json.dumps({"export_root": "/tmp/a\nb"}), encoding="utf-8")
    assert load_settings(path, SPECIES).export_root == str(DEFAULT_EXPORT_ROOT)


def test_load_works_with_a_small_species_list(tmp_path):
    corals = [
        Species(code="OANN", name="Orbicella annularis", part="", default_pin="1"),
        Species(code="PAST", name="Porites astreoides", part="", default_pin=""),
    ]
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"pins": {"1": "ACAU", "2": "PAST"}}), encoding="utf-8")
    loaded = load_settings(path, corals)
    assert loaded.pins["1"] is None
    assert loaded.pins["2"] == "PAST"


@pytest.mark.parametrize("good", ["LO", "a", "A.b_c-9", "123456789012", ".", "-", "_"])
def test_validate_annotator_accepts_allowed_text(good):
    assert validate_annotator(good) == good


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "1234567890123",
        "L O",
        " LO",
        "LO\n",
        "L/O",
        "..\\x",
        "<script>",
        "L,O",
        'L"O',
        "=1+1",
        "@LO",
        "L\u00d6",
        "L\x00O",
        "LO\u200b",
        None,
        7,
        True,
        b"LO",
        ["LO"],
    ],
)
def test_validate_annotator_rejects_empty_long_and_hostile(bad):
    with pytest.raises(ValueError, match="^annotator:"):
        validate_annotator(bad)


@pytest.mark.parametrize(
    "broken, fragment",
    [
        (Settings(annotator="", pins=default_pins(SPECIES), export_root="/tmp/x"), "annotator"),
        (Settings(annotator="LO", pins={"1": "ACAU"}, export_root="/tmp/x"), "pins"),
        (Settings(annotator="LO", pins=None, export_root="/tmp/x"), "pins"),
        (Settings(annotator="LO", pins={**default_pins(SPECIES), "9": 7}, export_root="/tmp/x"), "pins.9"),
        (Settings(annotator="LO", pins={**default_pins(SPECIES), "9": "nope"}, export_root="/tmp/x"), "pins.9"),
        (Settings(annotator="LO", pins=default_pins(SPECIES), export_root=""), "export_root"),
        (Settings(annotator="LO", pins=default_pins(SPECIES), export_root=None), "export_root"),
        (Settings(annotator="LO", pins=default_pins(SPECIES), export_root=Path("/tmp/x")), "export_root"),
        ({"annotator": "LO"}, "settings"),
    ],
)
def test_save_rejects_invalid_settings_and_writes_nothing(tmp_path, broken, fragment):
    path = tmp_path / "settings.json"
    with pytest.raises(ValueError, match="^" + re.escape(fragment)):
        save_settings(path, broken)
    assert list(tmp_path.iterdir()) == []


def test_failed_save_keeps_the_old_file_and_leaves_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    save_settings(path, defaults())
    before = path.read_bytes()

    def refuse(source, target):
        """Stand in for os.replace on a full disk."""
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="No space left"):
        save_settings(path, Settings(annotator="AB", pins=custom_pins(), export_root="/tmp/x"))
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert [item.name for item in tmp_path.iterdir()] == ["settings.json"]


def test_settings_is_a_plain_mutable_record():
    current = defaults()
    current.annotator = "AB"
    current.pins["9"] = "UNKN"
    assert current.annotator == "AB" and current.pins["9"] == "UNKN"


def test_load_does_not_share_the_pins_object_between_calls(tmp_path):
    first = load_settings(tmp_path / "settings.json", SPECIES)
    first.pins["1"] = None
    second = load_settings(tmp_path / "settings.json", SPECIES)
    assert second.pins["1"] == "ACAU"
