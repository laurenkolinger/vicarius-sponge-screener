"""Tests for screener/custom_species.py: genera and species added while screening."""

import json
from pathlib import Path

import pytest

from screener.custom_species import add_to_guide, clean_name, load_custom_species, new_code, save_custom_species
from screener.species import Species


@pytest.mark.parametrize(
    "typed, wanted",
    [
        ("cinachyrella", "Cinachyrella"),
        ("  CINACHYRELLA  ", "Cinachyrella"),
        ("cinachyrella   apion", "Cinachyrella apion"),
        ("Cinachyrella sp.", "Cinachyrella sp."),
        ("cinachyrella spp.", "Cinachyrella spp."),
    ],
)
def test_clean_name(typed, wanted):
    assert clean_name(typed) == wanted


@pytest.mark.parametrize("bad", ["", "ab", "a1b2", "<script>", "Cina/../x", "one two three four", "x" * 90, 3, None])
def test_bad_name(bad):
    with pytest.raises(ValueError, match="^name"):
        clean_name(bad)


@pytest.mark.parametrize(
    "name, taken, wanted",
    [
        ("Cinachyrella", [], "CINA"),
        ("Cinachyrella apion", [], "CAPI"),
        ("Cinachyrella sp.", [], "CINA"),
        ("Aka", [], "AKAX"),
    ],
)
def test_new_code(name, taken, wanted):
    assert new_code(name, taken) == wanted


def test_new_code_steps_past_a_taken_code():
    assert new_code("Cinachyrella", ["CINA"]) == "CINB"


def test_new_code_runs_out_loudly():
    taken = ["CINA"] + ["CIN" + chr(letter) for letter in range(65, 91)]
    with pytest.raises(ValueError, match="^name"):
        new_code("Cinachyrella", taken)


def test_saved_species_come_back(tmp_path: Path):
    item = Species(code="CINA", name="Cinachyrella", part="", default_pin="")
    save_custom_species(tmp_path, [item])
    assert load_custom_species(tmp_path, ["ACAU"]) == [item]


def test_no_file_means_no_added_species(tmp_path: Path):
    assert load_custom_species(tmp_path, []) == []


def test_bad_rows_are_skipped(tmp_path: Path, capsys):
    (tmp_path / "custom_species.csv").write_text(
        "Code,ScientificName\nACAU,Copy of a built-in\nbad,Lower case code\nCINA,\nCINB,Cinachyrella\nCINB,Twice\n", encoding="utf-8"
    )
    assert [item.code for item in load_custom_species(tmp_path, ["ACAU"])] == ["CINB"]
    assert "skipped" in capsys.readouterr().err


def test_excel_marker_at_the_start_keeps_every_row(tmp_path: Path):
    (tmp_path / "custom_species.csv").write_text("\ufeffCode,ScientificName\nCINA,Cinachyrella\n", encoding="utf-8")
    assert [item.code for item in load_custom_species(tmp_path, [])] == ["CINA"]


@pytest.mark.parametrize("content", [b"\xff\xfe\x00 not text", b"Other,Columns\nA,B\n", b"Code,ScientificName\nCINA,x\x00y\n"])
def test_unreadable_file_moves_aside_and_the_app_still_starts(tmp_path: Path, content: bytes, capsys):
    (tmp_path / "custom_species.csv").write_bytes(content)
    load_custom_species(tmp_path, [])
    if not (tmp_path / "custom_species.csv").exists():
        aside = list(tmp_path.glob("custom_species.csv.damaged-*"))
        assert len(aside) == 1 and aside[0].read_bytes() == content
        assert "could not be read" in capsys.readouterr().err


def test_guide_without_a_species_list_is_refused(tmp_path: Path):
    (tmp_path / "guide").mkdir()
    (tmp_path / "guide" / "guide.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="^guide"):
        add_to_guide(tmp_path, Species(code="CINA", name="Cinachyrella", part="", default_pin=""))


def test_skipped_rows_keep_a_copy_before_any_rewrite(tmp_path: Path):
    content = "Code,ScientificName\nbad,Lower case code\nCINA,Cinachyrella\n"
    (tmp_path / "custom_species.csv").write_text(content, encoding="utf-8")
    load_custom_species(tmp_path, [])
    copies = list(tmp_path.glob("custom_species.csv.before-skip-*"))
    assert len(copies) == 1 and copies[0].read_text(encoding="utf-8") == content


def test_folder_that_refuses_the_move_still_starts(tmp_path: Path, monkeypatch):
    (tmp_path / "custom_species.csv").write_bytes(b"Other,Columns\nA,B\n")

    def refuse(*args):
        raise PermissionError("read-only")

    monkeypatch.setattr("screener.custom_species.os.replace", refuse)
    assert load_custom_species(tmp_path, []) == []


def test_guide_entry_left_on_another_name_is_corrected(tmp_path: Path):
    add_to_guide(tmp_path, Species(code="CINA", name="Cinachyra", part="", default_pin=""))
    add_to_guide(tmp_path, Species(code="CINA", name="Cinachyrella", part="", default_pin=""))
    guide = json.loads((tmp_path / "guide" / "guide.json").read_text(encoding="utf-8"))
    assert [entry["name"] for entry in guide["species"]] == ["Cinachyrella"]


def test_guide_with_a_stray_item_still_takes_a_species(tmp_path: Path):
    (tmp_path / "guide").mkdir()
    (tmp_path / "guide" / "guide.json").write_text('{"species": ["stray"]}', encoding="utf-8")
    add_to_guide(tmp_path, Species(code="CINA", name="Cinachyrella", part="", default_pin=""))
    guide = json.loads((tmp_path / "guide" / "guide.json").read_text(encoding="utf-8"))
    assert guide["species"][1]["code"] == "CINA"


def test_guide_entry_is_added_once(tmp_path: Path):
    item = Species(code="CINA", name="Cinachyrella", part="", default_pin="")
    add_to_guide(tmp_path, item)
    add_to_guide(tmp_path, item)
    guide = json.loads((tmp_path / "guide" / "guide.json").read_text(encoding="utf-8"))
    assert [entry["code"] for entry in guide["species"]] == ["CINA"]
    assert guide["species"][0]["images"] == []
