#!/usr/bin/env python3
"""Pull reference photos for the screener's species from the benthic-species-viewer repo.

The viewer (github.com/laurenkolinger/benthic-species-viewer, private) holds
web-sized photos of 262 Caribbean sponges. This script matches the species in
config/species.csv to that manifest by scientific name, downloads their photos
through the GitHub API (so it needs `gh auth login`), and writes
data/guide/guide.json plus data/guide/<CODE>/<file>. Existing description text
in guide.json is kept. Nothing here is committed: the photos are for internal
use only.

Usage: python3 tools/fetch_guide_images.py [--force]
"""

import base64
import csv
import json
import subprocess
import sys
from pathlib import Path

REPO = "laurenkolinger/benthic-species-viewer"
GROUP = "Sponge"
PROJECT = Path(__file__).resolve().parent.parent
SPECIES_CSV = PROJECT / "config" / "species.csv"
GUIDE_DIR = PROJECT / "data" / "guide"
GUIDE_JSON = GUIDE_DIR / "guide.json"

# Names in the viewer that differ from the names in the ID guide.
NAME_ALIASES = {
    "Neopetrosia proxima": ["Xestospongia proxima", "Neopetrosia proxima"],
    "Cliona delitrix": ["Cliona delitrix"],
    "Siphonodictyon xamaycaense": ["Siphonodictyon xamaycaense", "Aka xamaycaense"],
}


def gh_api(path: str, raw: bool = False) -> bytes:
    """Call the GitHub API through the gh CLI.

    Args:
        path: The API path after ``repos/``, for example ``contents/imgs/x.jpeg``.
        raw: When true, ask for the file's raw bytes instead of JSON.

    Returns:
        The response body.

    Raises:
        RuntimeError: When gh exits with an error.
    """
    command = ["gh", "api", f"repos/{REPO}/{path}"]
    if raw:
        command[2:2] = ["-H", "Accept: application/vnd.github.raw"]
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def load_manifest() -> dict:
    """Fetch species_manifest.json from the viewer repo and index the sponge group by name."""
    payload = json.loads(gh_api("contents/species_manifest.json"))
    manifest = json.loads(base64.b64decode(payload["content"]))
    return {entry["name"].strip().lower(): entry for entry in manifest[GROUP]}


def find_entry(name: str, by_name: dict) -> dict:
    """Find the manifest entry for a scientific name, trying known aliases.

    Args:
        name: The scientific name from species.csv.
        by_name: Manifest entries keyed by lower-case name.

    Returns:
        The entry, or an empty dict when no name matches.
    """
    for candidate in NAME_ALIASES.get(name, [name]):
        entry = by_name.get(candidate.strip().lower())
        if entry:
            return entry
    return {}


def main(argv) -> int:
    """Download the photos and write guide.json. Returns a process exit code."""
    force = "--force" in argv
    species = list(csv.DictReader(SPECIES_CSV.open(newline="", encoding="utf-8")))
    by_name = load_manifest()
    existing = {}
    if GUIDE_JSON.exists():
        existing = {item["code"]: item for item in json.loads(GUIDE_JSON.read_text(encoding="utf-8")).get("species", [])}
    guide = []
    missing = []
    downloaded = 0
    for row in species:
        code, name = row["Code"], row["ScientificName"]
        entry = find_entry(name, by_name)
        item = {
            "code": code,
            "name": name,
            "part": row["GuidePart"],
            "description": existing.get(code, {}).get("description", ""),
            "source": entry.get("source", ""),
            "viewer_name": entry.get("name", ""),
            "images": [],
        }
        if not entry:
            missing.append(f"{code} {name}")
        for file_name in entry.get("images", []):
            target = GUIDE_DIR / code / file_name
            if force or not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(gh_api(f"contents/imgs/{file_name}", raw=True))
                downloaded += 1
            item["images"].append(file_name)
        guide.append(item)
    GUIDE_DIR.mkdir(parents=True, exist_ok=True)
    GUIDE_JSON.write_text(json.dumps({"species": guide}, indent=2) + "\n", encoding="utf-8")
    total = sum(len(item["images"]) for item in guide)
    print(f"{len(guide)} species, {total} photos on disk, {downloaded} downloaded now -> {GUIDE_JSON}")
    if missing:
        print("no photos in the viewer for: " + "; ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
