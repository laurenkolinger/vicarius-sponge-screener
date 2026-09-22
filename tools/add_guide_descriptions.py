#!/usr/bin/env python3
"""Copy the "Visual description" text of each species from the ID guide into guide.json.

Input: a plain-text or Markdown export of the sponge ID guide (the Google Doc),
in which every species starts with a level-one heading such as
``# ***Agelas clathrodes***`` and its text runs until the ``Images`` heading.
The text is matched to config/species.csv by scientific name.

Usage: python3 tools/add_guide_descriptions.py GUIDE_EXPORT.md
"""

import csv
import json
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SPECIES_CSV = PROJECT / "config" / "species.csv"
GUIDE_JSON = PROJECT / "data" / "guide" / "guide.json"
HEADING = re.compile(r"^#\s+(.+?)\s*$")
IMAGES_HEADING = re.compile(r"^#{2,4}\s+\**\s*Images\s*\**\s*$", re.IGNORECASE)
MAX_DESCRIPTION = 6000

# Spellings in the document that differ from the species list.
DOCUMENT_ALIASES = {"cliona deltrix": "cliona delitrix"}


def clean_heading(text: str) -> str:
    """Turn a heading like ``***Aplysina fulva*** - purple rope`` into ``aplysina fulva``."""
    text = re.sub(r"[*_`]+", "", text)
    text = text.split(" - ")[0].split(" – ")[0].strip().lower()
    return DOCUMENT_ALIASES.get(text, text)


def clean_body(lines) -> str:
    """Join description lines into readable paragraphs, dropping markup and list bullets."""
    out = []
    for line in lines:
        line = re.sub(r"[*_`]+", "", line)
        line = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", line)
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"<!--.*?-->", "", line).strip()
        if line:
            out.append(line)
    text = "\n".join(out)
    return text[:MAX_DESCRIPTION]


def split_species(text: str) -> dict:
    """Map each lower-case species name in the export to its description text."""
    sections = {}
    current = None
    body = []
    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            if current:
                sections[current] = clean_body(body)
            current = clean_heading(heading.group(1))
            body = []
            continue
        if current is None:
            continue
        if IMAGES_HEADING.match(line):
            sections[current] = clean_body(body)
            current = None
            body = []
            continue
        body.append(line)
    if current:
        sections[current] = clean_body(body)
    return sections


def main(argv) -> int:
    """Merge descriptions into guide.json. Returns a process exit code."""
    if len(argv) != 1:
        print("usage: add_guide_descriptions.py GUIDE_EXPORT.md", file=sys.stderr)
        return 2
    export = Path(argv[0]).read_text(encoding="utf-8")
    sections = split_species(export)
    manifest = json.loads(GUIDE_JSON.read_text(encoding="utf-8"))
    species = {row["Code"]: row["ScientificName"] for row in csv.DictReader(SPECIES_CSV.open(newline="", encoding="utf-8"))}
    found, missing = 0, []
    for item in manifest["species"]:
        name = species.get(item["code"], item["name"]).strip().lower()
        text = sections.get(name, "")
        if text:
            item["description"] = text
            found += 1
        else:
            missing.append(item["code"])
    GUIDE_JSON.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"descriptions for {found} of {len(manifest['species'])} species -> {GUIDE_JSON}")
    if missing:
        print("no description found for: " + ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
