"""Tests for the page's tooltips: every one follows the VICARIUS opening rule.

A tooltip never opens with "You" (a control's tooltip opens with what the
person does, a status's with what it shows), never names the Mac (the app also
runs on the lab server), and never points at the screen. The static tooltips
are the title attributes in static/index.html; the run-time ones are the
title strings static/app.js writes.
"""

import html
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE = PROJECT_ROOT / "static" / "index.html"
SCRIPT = PROJECT_ROOT / "static" / "app.js"
TITLE_ATTRIBUTE = re.compile(r'\btitle="([^"]*)"')
# title: '...' or title: `...` in an element builder, the two arms of a title: x ? a : b,
# and title = '...' or `...` assignments.
SCRIPT_TITLE = re.compile(r"""title(?::|\s=)\s*(?:[\w.]+\s*(?:===\s*'[^']*')?\s*\n?\s*\?\s*)?(['`])((?:(?!\1).)*)\1""", re.S)
SCRIPT_ELSE_ARM = re.compile(r"""\n\s*:\s*(['`])((?:(?!\1).)*)\1[,;]""", re.S)
BANNED_OPENING = re.compile(r"^\s*you\b", re.IGNORECASE)
BANNED_PHRASES = ("this mac", "on this screen", "above", "this tab", "this window")


def page_tooltips():
    """Return every title attribute of the page, unescaped."""
    return [html.unescape(text) for text in TITLE_ATTRIBUTE.findall(PAGE.read_text(encoding="utf-8")) if text]


def script_tooltips():
    """Return every tooltip string the page script writes, both arms of a conditional included."""
    text = SCRIPT.read_text(encoding="utf-8")
    found = [match.group(2) for match in SCRIPT_TITLE.finditer(text)]
    found += [match.group(2) for match in SCRIPT_ELSE_ARM.finditer(text) if "title" in text[max(0, match.start() - 400):match.start()]]
    return [item for item in found if item.strip()]


def test_the_page_has_tooltips_to_check():
    assert len(page_tooltips()) >= 30
    assert len(script_tooltips()) >= 20


@pytest.mark.parametrize("tooltip", page_tooltips() + script_tooltips())
def test_no_tooltip_opens_with_you_or_points_at_the_screen(tooltip):
    assert not BANNED_OPENING.search(tooltip), tooltip
    lowered = tooltip.lower()
    assert not any(phrase in lowered for phrase in BANNED_PHRASES), tooltip
    assert chr(0x2014) not in tooltip and chr(0x2013) not in tooltip, tooltip
