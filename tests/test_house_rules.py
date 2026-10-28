#!/usr/bin/env python3
"""
Module: tests/test_house_rules.py
Purpose: Police the VICARIUS prose rules over every Markdown file in this clone.
Inputs:  every *.md under the repository root, skipping environment, data,
         run and git directories.
Outputs: "PASS" on stdout and exit code 0, or one "file:line: reason" line per
         offence followed by a FAIL summary and exit code 1.

Rules checked, from the "Prose and console" section of
/mnt/rip/vicarius_drive/CLAUDE.md:
  - no em dash (U+2014) and no en dash (U+2013) in prose
  - never the banned verb or its forms, matched as whole words
  - no emoji code points

Text inside backtick code spans and inside fenced code blocks is skipped, so a
quoted parser syntax or a shell snippet never trips the sweep. The banned
literal is assembled from two adjacent string pieces so this file never
contains the contiguous word it looks for.

Usage:
    python3 tests/test_house_rules.py        # from the clone root, or anywhere
    pytest tests/test_house_rules.py         # the same check as a pytest case
"""

from __future__ import annotations

import pathlib
import re
import sys
from typing import Iterator

REPO = pathlib.Path(__file__).resolve().parents[1]

# Directories never scanned: environments, caches, git internals, and the
# run/data trees that a module may keep beside its code.
SKIP_DIRS = {
    ".git", "env", ".venv", "venv", "__pycache__", "node_modules",
    "inprocess", "logs", "runs", "output", "outputs",
    # The screener's own untracked trees: screening data, the video cache,
    # export packages, the tracking environment, and end-to-end screenshots.
    "data", "cache", "exports", ".venv-track", ".pytest_cache",
}

# Code points are built with chr() so this source stays pure ASCII and never
# depends on how an editor or a transport handles escape sequences.
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)

# Whole-word match on the banned verb and its inflections, case-insensitive.
BANNED_VERB = re.compile(r"\b" + "la" "nd" + r"(s|ed|ing)?\b", re.IGNORECASE)

# Emoji code points: the supplemental symbol and pictograph planes
# (U+1F000 to U+1FAFF), the miscellaneous symbols and dingbats blocks
# (U+2600 to U+27BF), the two starred symbols in the arrows block (U+2B50,
# U+2B55), and the emoji variation selector (U+FE0F).
EMOJI = re.compile(
    "["
    + chr(0x1F000) + "-" + chr(0x1FAFF)
    + chr(0x2600) + "-" + chr(0x27BF)
    + chr(0x2B50) + chr(0x2B55)
    + chr(0xFE0F)
    + "]"
)

FENCE = re.compile(r"^\s*(```|~~~)")
CODE_SPAN = re.compile(r"`+[^`\n]*`+")


def markdown_files(repo: pathlib.Path = REPO) -> Iterator[pathlib.Path]:
    """Yield every .md file under the repo whose path has no skipped directory."""
    for path in sorted(repo.rglob("*.md")):
        parents = path.relative_to(repo).parts[:-1]
        if any(part in SKIP_DIRS for part in parents):
            continue
        yield path


def prose_lines(path: pathlib.Path) -> Iterator[tuple[int, str]]:
    """Yield (line_number, text) for each prose line, with code removed.

    Lines inside a fenced block (``` or ~~~) are dropped entirely, as are the
    fence lines themselves; inline backtick spans are cut out of the lines
    that remain, so only prose reaches the checks.
    """
    in_fence = False
    text = path.read_text(encoding="utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        yield number, CODE_SPAN.sub("", line)


def check_line(text: str) -> list[str]:
    """Return one reason string per rule the given prose line breaks."""
    reasons: list[str] = []
    if EM_DASH in text:
        reasons.append("em dash (U+2014)")
    if EN_DASH in text:
        reasons.append("en dash (U+2013)")
    verb = BANNED_VERB.search(text)
    if verb:
        reasons.append(f"banned verb: {verb.group(0)!r}")
    emoji = EMOJI.search(text)
    if emoji:
        reasons.append(f"emoji U+{ord(emoji.group(0)):04X}")
    return reasons


def collect_offences(repo: pathlib.Path = REPO) -> tuple[list[str], int]:
    """Scan the repo; return (offence lines as 'file:line: reason', files scanned)."""
    offences: list[str] = []
    scanned = 0
    for path in markdown_files(repo):
        scanned += 1
        rel = path.relative_to(repo)
        for number, text in prose_lines(path):
            for reason in check_line(text):
                offences.append(f"{rel}:{number}: {reason}")
    return offences, scanned


def test_house_rules() -> None:
    """pytest entry point: the clone's Markdown carries no house-rule offence."""
    offences, _ = collect_offences()
    assert not offences, "\n".join(offences)


def main() -> int:
    """Run the sweep, print PASS or the offence list, return the exit code."""
    offences, scanned = collect_offences()
    if offences:
        print("\n".join(offences))
        print(f"FAIL: {len(offences)} offence(s) in {scanned} Markdown file(s) under {REPO}")
        return 1
    print(f"PASS: {scanned} Markdown file(s) under {REPO} clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
