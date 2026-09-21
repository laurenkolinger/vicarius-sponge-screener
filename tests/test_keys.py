"""Tests for screener.keys: S3 key validation, quoting, and cache identifiers."""

import re

import pytest

from screener import keys
from screener.keys import InvalidKey

GOOD = "TCRMP_video_ondeck/2024Annual/TCRMP20241022_video_FLC_T1.MP4"


def test_accepts_standard_key():
    assert keys.validate_key(GOOD) == GOOD


def test_accepts_plus_dots_and_spaces():
    for name in ("TCRMP20051013_video_SSJ_T1+T3-6.mp4", "TCRMP20110419_video_GBF_T5.2-6.avi", "MVI 0203.MOV"):
        key = "TCRMP_video_ondeck/main/TCRMP2005_video/PeakBL/" + name
        assert keys.validate_key(key) == key


@pytest.mark.parametrize("bad", [None, 7, b"TCRMP_video_ondeck/x.mp4", ["TCRMP_video_ondeck/x.mp4"], True])
def test_rejects_non_string(bad):
    with pytest.raises(InvalidKey, match="key"):
        keys.validate_key(bad)


def test_rejects_empty():
    with pytest.raises(InvalidKey):
        keys.validate_key("")


@pytest.mark.parametrize("bad", ["other/x.mp4", "/TCRMP_video_ondeck/x.mp4", "tcrmp_video_ondeck/x.mp4", "TCRMP_video_ondeck"])
def test_rejects_outside_prefix(bad):
    with pytest.raises(InvalidKey, match="TCRMP_video_ondeck/"):
        keys.validate_key(bad)


@pytest.mark.parametrize(
    "bad",
    [
        "TCRMP_video_ondeck/../secret.mp4",
        "TCRMP_video_ondeck/a/../../b.mp4",
        "TCRMP_video_ondeck/..",
    ],
)
def test_rejects_dotdot(bad):
    with pytest.raises(InvalidKey, match=r"\.\."):
        keys.validate_key(bad)


def test_rejects_backslash():
    with pytest.raises(InvalidKey, match="backslash"):
        keys.validate_key("TCRMP_video_ondeck\\x.mp4")
    with pytest.raises(InvalidKey, match="backslash"):
        keys.validate_key("TCRMP_video_ondeck/a\\b.mp4")


@pytest.mark.parametrize("char", ["\x00", "\n", "\r", "\t", "\x1f", "\x7f"])
def test_rejects_control_chars(char):
    with pytest.raises(InvalidKey, match="control"):
        keys.validate_key("TCRMP_video_ondeck/a" + char + "b.mp4")


@pytest.mark.parametrize("bad", ["TCRMP_video_ondeck/a\ud800b.mp4", "TCRMP_video_ondeck/\udfff.mp4"])
def test_rejects_text_that_cannot_be_encoded(bad):
    with pytest.raises(InvalidKey, match="UTF-8"):
        keys.validate_key(bad)
    with pytest.raises(InvalidKey, match="UTF-8"):
        keys.validate_prefix(bad + "/")


def test_accepts_non_ascii_names():
    key = "TCRMP_video_ondeck/main/Botany Bay ñ/vidéo.mp4"
    assert keys.validate_key(key) == key
    assert keys.quote_key(key).startswith("TCRMP_video_ondeck/main/Botany%20Bay%20%C3%B1/")
    assert len(keys.cache_id(key)) == 16


def test_rejects_empty_segment():
    with pytest.raises(InvalidKey, match="empty"):
        keys.validate_key("TCRMP_video_ondeck//x.mp4")


def test_rejects_key_that_names_a_folder():
    with pytest.raises(InvalidKey, match="folder"):
        keys.validate_key("TCRMP_video_ondeck/main/")


def test_rejects_too_long():
    with pytest.raises(InvalidKey, match="1024"):
        keys.validate_key("TCRMP_video_ondeck/" + "a" * 1100 + ".mp4")


def test_prefix_requires_trailing_slash():
    assert keys.validate_prefix("TCRMP_video_ondeck/") == "TCRMP_video_ondeck/"
    assert keys.validate_prefix("TCRMP_video_ondeck/main/TCRMP2004_video/") == "TCRMP_video_ondeck/main/TCRMP2004_video/"
    with pytest.raises(InvalidKey, match="/"):
        keys.validate_prefix("TCRMP_video_ondeck/main")


@pytest.mark.parametrize("bad", [None, "", "main/", "TCRMP_video_ondeck/../", "TCRMP_video_ondeck//", "TCRMP_video_ondeck/a\nb/"])
def test_prefix_rejects_hostile_values(bad):
    with pytest.raises(InvalidKey):
        keys.validate_prefix(bad)


def test_quote_key_encodes_plus_space_and_keeps_slashes():
    quoted = keys.quote_key("TCRMP_video_ondeck/a b/T1+T3-6.mp4")
    assert quoted == "TCRMP_video_ondeck/a%20b/T1%2BT3-6.mp4"


def test_cache_id_is_stable_16_hex_and_differs_by_key():
    first = keys.cache_id(GOOD)
    assert first == keys.cache_id(GOOD)
    assert re.fullmatch(r"[0-9a-f]{16}", first)
    assert first != keys.cache_id(GOOD.replace("T1", "T2"))


def test_extension_is_lowercase_without_dot():
    assert keys.extension_of(GOOD) == "mp4"
    assert keys.extension_of("TCRMP_video_ondeck/x.tar.M2T") == "m2t"
    assert keys.extension_of("TCRMP_video_ondeck/noextension") == ""
