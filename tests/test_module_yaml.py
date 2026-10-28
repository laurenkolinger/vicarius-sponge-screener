"""Tests for module.yaml: the VICARIUS contract stays in step with the code.

The version, the port, the launch script, the run folders, the default pins
and the output bindings are read from module.yaml with PyYAML and compared
with the code, so a change to one without the other fails here.
"""

import os
import re
from pathlib import Path

import pytest

from screener import config
from screener.species import PIN_KEYS, default_pins, load_species

# PyYAML is in requirements.txt; a bare system Python without it skips this file with a reason.
yaml = pytest.importorskip("yaml", reason="PyYAML is not installed; env/bin/pip install -r requirements.txt")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODULE_YAML = PROJECT_ROOT / "module.yaml"
EXPECTED_DATASETS = {
    "sponge_screener_observations",
    "sponge_screener_images",
    "sponge_screener_videos_screened",
    "sponge_screener_tracks",
    "sponge_screener_export",
}
# A setting's tooltip opens with what the person does (VICARIUS tooltip rule 2).
TOOLTIP_IMPERATIVES = {"Type", "Pin", "Set", "Pick", "Tick", "Choose", "Name"}
# A sentence ends at a period, question mark or exclamation mark followed by a space;
# the dot inside settings.json is not an end.
SENTENCE_END = re.compile(r"[.!?] ")
SCREEN_REFERENCES = ("above", "below", "this tab", "this window", "this page")
REQUIRED_KEYS = ("name", "display_name", "tags", "version", "description", "author", "created", "type", "inputs",
                 "outputs", "parameters", "runtime", "cli", "ui")


@pytest.fixture(scope="module")
def contract():
    """The parsed module.yaml."""
    return yaml.safe_load(MODULE_YAML.read_text(encoding="utf-8"))


def test_every_required_key_is_present(contract):
    missing = [key for key in REQUIRED_KEYS if key not in contract]
    assert missing == []
    assert contract["name"] == "sponge_screener"
    assert contract["display_name"] and len(contract["display_name"]) <= 20
    assert contract["tags"] and all(isinstance(tag, str) and tag for tag in contract["tags"])


def test_version_matches_the_code(contract):
    assert contract["version"] == config.VERSION


def test_launch_block_matches_the_vicarius_port_and_script(contract):
    launch = contract["ui"]["launch"]
    assert contract["ui"]["style"] == "launcher"
    assert launch["port"] == config.VICARIUS_PORT
    assert launch["url"] == f"http://127.0.0.1:{config.VICARIUS_PORT}"
    script = PROJECT_ROOT / launch["script"]
    assert script.is_file() and os.access(script, os.X_OK)
    assert contract["cli"]["main_script"] == launch["script"]
    assert contract["cli"]["interactive"] is False


def test_runs_are_minted_by_vicarius_under_inprocess(contract):
    runs = contract["ui"]["runs"]
    assert runs == {"dir": "inprocess", "pattern": "run_*", "status_source": "project.json", "naming": "auto"}


def test_outputs_bind_every_dataset_with_eml(contract):
    datasets = {entry["dataset"] for entry in contract["outputs"]}
    assert datasets == EXPECTED_DATASETS
    for entry in contract["outputs"]:
        assert entry["eml"] is True
        assert entry["path_template"].startswith("${run_dir}/")


def test_every_parameter_has_a_tooltip_that_follows_the_rule(contract):
    """Each setting's text opens with an imperative, runs one or two sentences, and points at nothing on screen."""
    for parameter in contract["parameters"]:
        text = parameter["description"]
        assert text.split()[0] in TOOLTIP_IMPERATIVES, (parameter["name"], text)
        assert 1 <= len(SENTENCE_END.findall(text + " ")) <= 2, (parameter["name"], text)
        lowered = text.lower()
        assert not any(phrase in lowered for phrase in SCREEN_REFERENCES), (parameter["name"], text)
        assert chr(0x2014) not in text and chr(0x2013) not in text, parameter["name"]


def test_default_pins_match_species_csv(contract):
    species = load_species(config.config_dir() / "species.csv")
    pins = default_pins(species)
    expected = [pins[key] for key in PIN_KEYS if pins[key] is not None]
    (pin_parameter,) = [item for item in contract["parameters"] if item["name"] == "pins"]
    assert pin_parameter["default"] == expected


def test_work_root_default_matches_the_shipped_ini(contract):
    (work_root,) = [item for item in contract["parameters"] if item["name"] == "work_root"]
    ini = (config.config_dir() / "vicarius.ini").read_text(encoding="utf-8")
    assert f"work_root = {work_root['default']}" in ini
