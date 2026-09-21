"""Tests for screener.species: the species list, default pins, and pin validation."""

from pathlib import Path

import pytest

from screener import species as species_module
from screener.species import PIN_KEYS, Species, default_pins, load_species, species_by_code, validate_pins

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_SPECIES = PROJECT_ROOT / "config" / "species.csv"
HEADER = "Code,ScientificName,GuidePart,DefaultPin\n"
SHIPPED_PINS = {
    "1": "ACAU",
    "2": "AFUL",
    "3": "CDEL",
    "4": "MLAE",
    "5": "CPLI",
    "6": "ACRA",
    "7": "ACOM",
    "8": "XMUT",
    "9": None,
    "0": None,
}
NO_PINS = {key: None for key in PIN_KEYS}


def write_species(tmp_path, text):
    """Write a species file into the test folder and return its path."""
    path = tmp_path / "species.csv"
    path.write_text(text, encoding="utf-8")
    return path


def small_list():
    """Return three species with pins on keys 1 and 2."""
    return [
        Species(code="ACAU", name="Aplysina cauliformis", part="1", default_pin="1"),
        Species(code="AFUL", name="Aplysina fulva", part="2", default_pin="2"),
        Species(code="UNKN", name="Unknown sponge", part="", default_pin=""),
    ]


def empty_pins():
    """Return a fresh copy of the ten pin keys with nothing pinned."""
    return dict(NO_PINS)


def test_loads_shipped_species_file():
    loaded = load_species(SHIPPED_SPECIES)
    assert len(loaded) == 38
    assert loaded[0] == Species(code="ACAU", name="Aplysina cauliformis", part="1", default_pin="1")
    codes = [item.code for item in loaded]
    assert "UNKN" in codes
    assert len(set(codes)) == 38
    unknown = loaded[codes.index("UNKN")]
    assert unknown == Species(code="UNKN", name="Unknown sponge", part="", default_pin="")


def test_default_pins_match_shipped_file():
    pins = default_pins(load_species(SHIPPED_SPECIES))
    assert pins == SHIPPED_PINS
    assert tuple(pins) == PIN_KEYS


def test_pin_keys_are_the_ten_number_keys_in_keyboard_order():
    assert PIN_KEYS == ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0")


def test_species_is_frozen():
    item = small_list()[0]
    with pytest.raises(Exception):
        item.code = "ZZZZ"


def test_loads_file_with_byte_order_mark_windows_line_ends_blank_lines_and_extra_columns(tmp_path):
    path = tmp_path / "species.csv"
    text = (
        "\ufeffCode,ScientificName,GuidePart,DefaultPin,Comment\r\n\r\n"
        " ACAU , Aplysina cauliformis ,1,1,rope\r\nUNKN,Unknown sponge\r\n"
    )
    path.write_bytes(text.encode("utf-8"))
    assert load_species(path) == [
        Species(code="ACAU", name="Aplysina cauliformis", part="1", default_pin="1"),
        Species(code="UNKN", name="Unknown sponge", part="", default_pin=""),
    ]


def test_loads_names_with_commas_quotes_and_html_as_plain_text(tmp_path):
    path = write_species(tmp_path, HEADER + 'ACAU,"Aplysina ""rope"", <b>bold</b>",1,1\n')
    assert load_species(path)[0].name == 'Aplysina "rope", <b>bold</b>'


def test_rejects_duplicate_code(tmp_path):
    path = write_species(tmp_path, HEADER + "ACAU,Aplysina cauliformis,1,1\nACAU,Aplysina fulva,2,2\n")
    with pytest.raises(ValueError) as caught:
        load_species(path)
    message = str(caught.value)
    assert "ACAU" in message and "line 3" in message and "line 2" in message and "species.csv" in message


@pytest.mark.parametrize("code", ["acau", "ACA", "ACAUL", "AC4U", "AC-U", "A CU", "", "\u00c0CAU", "ACAU\u200b"])
def test_rejects_bad_code_shape(tmp_path, code):
    path = write_species(tmp_path, HEADER + f"{code},Aplysina cauliformis,1,1\n")
    with pytest.raises(ValueError) as caught:
        load_species(path)
    assert "Code" in str(caught.value) and "line 2" in str(caught.value)


@pytest.mark.parametrize(
    "header",
    [
        "Code,ScientificName,GuidePart\n",
        "Code,GuidePart,DefaultPin\n",
        "ScientificName,GuidePart,DefaultPin\n",
        "code,scientificname,guidepart,defaultpin\n",
        "ACAU,Aplysina cauliformis,1,1\n",
    ],
)
def test_rejects_missing_column(tmp_path, header):
    path = write_species(tmp_path, header + "ACAU,Aplysina cauliformis,1,1\n")
    with pytest.raises(ValueError) as caught:
        load_species(path)
    assert "header" in str(caught.value) and "species.csv" in str(caught.value)


@pytest.mark.parametrize("text", ["", "\n\n\n", HEADER, HEADER + "\n,,,\n"])
def test_rejects_empty_file(tmp_path, text):
    path = write_species(tmp_path, text)
    with pytest.raises(ValueError) as caught:
        load_species(path)
    assert "species.csv" in str(caught.value)


def test_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="species"):
        load_species(tmp_path / "absent.csv")


@pytest.mark.parametrize(
    "rows, fragment",
    [
        ("ACAU,,1,1\n", "ScientificName"),
        ("ACAU,   ,1,1\n", "ScientificName"),
        ('ACAU,"Aplysina\ncauliformis",1,1\n', "ScientificName"),
        ("ACAU,=cmd|' /C calc'!A0,1,1\n", "formula"),
        ("ACAU,+1,1,1\n", "formula"),
        ("ACAU,@SUM(A1),1,1\n", "formula"),
        ("ACAU,-Aplysina,1,1\n", "formula"),
        ("ACAU,Aplysina cauliformis,1,1\nAFUL,aplysina CAULIFORMIS,2,2\n", "ScientificName"),
        ("ACAU,Aplysina cauliformis,1,11\n", "DefaultPin"),
        ("ACAU,Aplysina cauliformis,1,x\n", "DefaultPin"),
        ("ACAU,Aplysina cauliformis,1,-1\n", "DefaultPin"),
        ("ACAU,Aplysina cauliformis,1,1\nAFUL,Aplysina fulva,2,1\n", "DefaultPin"),
        ("ACAU,Aplysina cauliformis,1,1,extra,cells\n", "cells"),
        ("ACAU,Aplysina, cauliformis,1,1\n", "cells"),
    ],
)
def test_rejects_bad_rows_and_names_the_column(tmp_path, rows, fragment):
    path = write_species(tmp_path, HEADER + rows)
    with pytest.raises(ValueError) as caught:
        load_species(path)
    assert fragment in str(caught.value)
    assert "species.csv" in str(caught.value)


def test_rejects_file_that_is_not_utf8(tmp_path):
    path = tmp_path / "species.csv"
    path.write_bytes(HEADER.encode("utf-8") + b"ACAU,Aplysina \xff\xfe,1,1\n")
    with pytest.raises(ValueError, match="UTF-8"):
        load_species(path)


def test_rejects_file_with_nul_bytes(tmp_path):
    path = tmp_path / "species.csv"
    path.write_bytes(HEADER.encode("utf-8") + b"ACAU,Aplysina\x00cauliformis,1,1\n")
    with pytest.raises(ValueError, match="species.csv"):
        load_species(path)


def test_species_by_code_indexes_the_list_in_order():
    indexed = species_by_code(small_list())
    assert list(indexed) == ["ACAU", "AFUL", "UNKN"]
    assert indexed["AFUL"].name == "Aplysina fulva"
    assert species_by_code(tuple(small_list())) == indexed
    assert species_by_code([]) == {}


@pytest.mark.parametrize(
    "bad, fragment",
    [
        (None, "species:"),
        ("ACAU", "species:"),
        ({"ACAU": "Aplysina cauliformis"}, "species:"),
        (["ACAU"], "species[0]:"),
        ([Species("ACAU", "Aplysina cauliformis", "1", "1"), None], "species[1]:"),
        ([Species("ACAU", "Aplysina cauliformis", "1", "1"), Species("ACAU", "Aplysina fulva", "2", "")], "species[1]:"),
    ],
)
def test_species_by_code_rejects_wrong_types_and_repeated_codes(bad, fragment):
    with pytest.raises(ValueError) as caught:
        species_by_code(bad)
    assert str(caught.value).startswith(fragment)


def test_pin_functions_reject_a_list_with_a_repeated_code():
    twice = [small_list()[0], small_list()[0]]
    with pytest.raises(ValueError, match="ACAU"):
        default_pins(twice)
    with pytest.raises(ValueError, match="ACAU"):
        validate_pins(empty_pins(), twice)


def test_default_pins_for_a_small_list():
    pins = default_pins(small_list())
    assert pins == {**empty_pins(), "1": "ACAU", "2": "AFUL"}


def test_default_pins_rejects_two_species_on_one_key_unknown_keys_and_wrong_types():
    clash = small_list() + [Species(code="CDEL", name="Cliona delitrix", part="1", default_pin="1")]
    with pytest.raises(ValueError, match="CDEL"):
        default_pins(clash)
    with pytest.raises(ValueError, match="default_pin"):
        default_pins([Species(code="CDEL", name="Cliona delitrix", part="1", default_pin="12")])
    with pytest.raises(ValueError, match="species"):
        default_pins(["ACAU"])
    with pytest.raises(ValueError, match="species"):
        default_pins(None)


def test_validate_pins_accepts_a_full_valid_set_and_returns_keyboard_order():
    submitted = {key: None for key in reversed(PIN_KEYS)}
    submitted.update({"0": "UNKN", "2": "ACAU", "1": "AFUL"})
    checked = validate_pins(submitted, small_list())
    assert checked == {**empty_pins(), "1": "AFUL", "2": "ACAU", "0": "UNKN"}
    assert tuple(checked) == PIN_KEYS
    assert checked is not submitted


def test_validate_pins_accepts_all_empty():
    assert validate_pins(empty_pins(), small_list()) == empty_pins()


@pytest.mark.parametrize(
    "pins, starts_with",
    [
        ({**NO_PINS, "3": "ZZZZ"}, "pins.3:"),
        ({**NO_PINS, "3": "acau"}, "pins.3:"),
        ({**NO_PINS, "3": ""}, "pins.3:"),
        ({**NO_PINS, "3": 7}, "pins.3:"),
        ({**NO_PINS, "3": ["ACAU"]}, "pins.3:"),
        ({**NO_PINS, "3": True}, "pins.3:"),
        ({**NO_PINS, "1": "ACAU", "4": "ACAU"}, "pins.4:"),
        ({}, "pins:"),
        ({key: None for key in PIN_KEYS[:-1]}, "pins:"),
        ({**NO_PINS, "10": None}, "pins:"),
        ({**NO_PINS, "a": "ACAU"}, "pins:"),
        ({**{key: None for key in PIN_KEYS[1:]}, 1: None}, "pins:"),
        (None, "pins:"),
        ([], "pins:"),
        (list(PIN_KEYS), "pins:"),
        ("1234567890", "pins:"),
        (7, "pins:"),
    ],
)
def test_validate_pins_rejects_unknown_code_duplicate_code_wrong_keys_non_dict(pins, starts_with):
    with pytest.raises(ValueError) as caught:
        validate_pins(pins, small_list())
    assert str(caught.value).startswith(starts_with)


def test_validate_pins_does_not_change_the_submitted_object():
    submitted = {**empty_pins(), "1": "ACAU"}
    snapshot = dict(submitted)
    validate_pins(submitted, small_list())
    assert submitted == snapshot


def test_module_exposes_the_planned_interface():
    assert {"Species", "PIN_KEYS", "load_species", "default_pins", "validate_pins"} <= set(dir(species_module))
