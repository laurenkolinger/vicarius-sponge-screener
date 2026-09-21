"""Tests for screener.names: video file name parsing, site names, and file-safe labels."""

from pathlib import Path

import pytest

from screener import names
from screener.names import VideoName, file_safe, load_site_names, parse_video_name

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_SITES = PROJECT_ROOT / "config" / "sites.csv"
FOLDER = "TCRMP_video_ondeck/2024Annual/"
REAL_2023 = "TCRMP_video_ondeck/main/TCRMP2023_video/"
REAL_2024 = "TCRMP_video_ondeck/main/TCRMP2024_video/Annual/"
REAL_2025_MISC = "TCRMP_video_ondeck/main/TCRMP2025_video/PBL/MISC/"
SITES = {"FLC": "Flat Cay", "BIT": "Buck Island"}


def write_sites(tmp_path, text):
    """Write a sites file into the test folder and return its path."""
    path = tmp_path / "sites.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_standard_name():
    parsed = parse_video_name(FOLDER + "TCRMP20241022_video_FLC_T1.MP4", SITES)
    assert parsed == VideoName(
        file_name="TCRMP20241022_video_FLC_T1.MP4",
        date="20241022",
        year=2024,
        site_code="FLC",
        site_name="Flat Cay",
        transect="T1",
        standard=True,
    )


def test_lowercase_extension_and_mixed_case():
    parsed = parse_video_name(FOLDER + "tcrmp20160705_Video_flc_T3.mp4", SITES)
    assert parsed.standard is True
    assert parsed.file_name == "tcrmp20160705_Video_flc_T3.mp4"
    assert parsed.date == "20160705"
    assert parsed.year == 2016
    assert parsed.site_code == "FLC"
    assert parsed.site_name == "Flat Cay"
    assert parsed.transect == "T3"


@pytest.mark.parametrize("label", ["T1-6", "T1+T3-6", "T5.2-6", "BL", "Other8"])
def test_multi_transect_labels(label):
    parsed = parse_video_name(f"TCRMP_video_ondeck/main/TCRMP2005_video/TCRMP20051013_video_SSJ_{label}.mp4", SITES)
    assert parsed.standard is True
    assert parsed.transect == label
    assert parsed.site_code == "SSJ"
    assert parsed.year == 2005


def test_site_name_falls_back_to_code():
    parsed = parse_video_name(FOLDER + "TCRMP20241022_video_SSJ_T2.MP4", SITES)
    assert parsed.site_code == "SSJ"
    assert parsed.site_name == "SSJ"


def test_site_name_falls_back_to_code_when_the_mapping_holds_a_blank_name():
    parsed = parse_video_name(FOLDER + "TCRMP20241022_video_SSJ_T2.MP4", {"SSJ": "  "})
    assert parsed.site_name == "SSJ"


def test_nonstandard_name_returns_blanks():
    parsed = parse_video_name(FOLDER + "MVI_0203.MOV", SITES)
    assert parsed == VideoName(
        file_name="MVI_0203.MOV", date="", year=None, site_code="", site_name="", transect="", standard=False
    )


def test_year_from_folder_when_name_is_nonstandard():
    parsed = parse_video_name("TCRMP_video_ondeck/main/TCRMP2016_video/PeakBL/MVI_0203.MOV", SITES)
    assert parsed.standard is False
    assert parsed.year == 2016
    assert parsed.date == ""


def test_year_from_folder_ignores_case_and_prefers_the_deepest_folder():
    parsed = parse_video_name("TCRMP_video_ondeck/tcrmp2009_VIDEO/TCRMP2011_video/clip 7.avi", SITES)
    assert parsed.year == 2011


@pytest.mark.parametrize(
    "key",
    [
        "TCRMP_video_ondeck/TCRMP2016_videos/MVI_0203.MOV",
        "TCRMP_video_ondeck/xTCRMP2016_video/MVI_0203.MOV",
        "TCRMP_video_ondeck/TCRMP0000_video/MVI_0203.MOV",
        "TCRMP_video_ondeck/TCRMP9999_video/MVI_0203.MOV",
        "TCRMP_video_ondeck/2024Annual/MVI_0203.MOV",
    ],
)
def test_year_from_folder_needs_the_exact_folder_shape_and_a_sane_year(key):
    assert parse_video_name(key, SITES).year is None


def test_folder_year_is_not_read_from_the_file_name_itself():
    assert parse_video_name("TCRMP_video_ondeck/other/TCRMP2016_video", SITES).year is None


def test_impossible_date_is_nonstandard():
    parsed = parse_video_name(FOLDER + "TCRMP20241345_video_FLC_T1.MP4", SITES)
    assert parsed.standard is False
    assert parsed.date == ""
    assert parsed.year is None
    assert parsed.site_code == ""
    assert parsed.transect == ""
    assert parsed.file_name == "TCRMP20241345_video_FLC_T1.MP4"


@pytest.mark.parametrize(
    "file_name",
    [
        "TCRMP20230230_video_FLC_T1.MP4",  # February 30
        "TCRMP18991231_video_FLC_T1.MP4",  # before 1990
        "TCRMP21010101_video_FLC_T1.MP4",  # after 2100
        "TCRMP00000000_video_FLC_T1.MP4",
    ],
)
def test_dates_outside_the_calendar_or_the_year_range_are_nonstandard(file_name):
    assert parse_video_name(FOLDER + file_name, SITES).standard is False


@pytest.mark.parametrize("file_name", ["TCRMP19900101_video_FLC_T1.MP4", "TCRMP21001231_video_FLC_T1.MP4"])
def test_year_range_is_inclusive(file_name):
    assert parse_video_name(FOLDER + file_name, SITES).standard is True


def test_leap_day_is_a_real_date():
    assert parse_video_name(FOLDER + "TCRMP20240229_video_FLC_T1.MP4", SITES).year == 2024


@pytest.mark.parametrize(
    "file_name",
    [
        "TCRMP20241022_video_F_T1.MP4",  # site code too short
        "TCRMP20241022_video_FLCFLC_T1.MP4",  # site code too long
        "TCRMP20241022_video_FLC_T1",  # no extension
        "TCRMP20241022_video_FLC_.MP4",  # no transect
        "TCRMP2024102_video_FLC_T1.MP4",  # seven digit date
        "xTCRMP20241022_video_FLC_T1.MP4",
        "TCRMP20241022_video_FLC_T1.MP4\n",
        "TCRMP\u0662\u0660\u0662\u0664\u0661\u0660\u0662\u0662_video_FLC_T1.MP4",  # Arabic-Indic digits
        "TCRMP20241022_video_FL\u212a_T1.MP4",  # Kelvin sign posing as K
        "",
    ],
)
def test_names_outside_the_pattern(file_name):
    parsed = parse_video_name(FOLDER + file_name, SITES)
    assert parsed.standard is False
    assert parsed.site_code == ""
    assert parsed.file_name == file_name


@pytest.mark.parametrize(
    "key, date, site_code, transect",
    [
        (REAL_2023 + "TCRMP20231109_BID_T1.MP4", "20231109", "BID", "T1"),
        (REAL_2023 + "TCRMP20231109_BID_T5_WRONG.MP4", "20231109", "BID", "T5_WRONG"),
        (REAL_2023 + "TCRMP20231109_BIX_T6.MP4", "20231109", "BIX", "T6"),
        (REAL_2023 + "TCRMP20231112_CBD_T3.MP4", "20231112", "CBD", "T3"),
        (REAL_2023 + "TCRMP20231206_CSE_T4_WRONG.MP4", "20231206", "CSE", "T4_WRONG"),
        (REAL_2023 + "TCRMP20231206_HBE_T1.MP4", "20231206", "HBE", "T1"),
        (REAL_2023 + "TCRMP20231206_HBE_T6_part5.MP4", "20231206", "HBE", "T6_part5"),
        (REAL_2024 + "TCRMP20241029_video_SHR_with3DCamera/TCRMP20241029_SHR_T3.MXF", "20241029", "SHR", "T3"),
        (REAL_2024 + "Other Videos/TCRMP20241111_BIX_1_NMK_dolphins.MOV", "20241111", "BIX", "1_NMK_dolphins"),
    ],
)
def test_name_without_video_token(key, date, site_code, transect):
    parsed = parse_video_name(key, SITES)
    assert parsed == VideoName(
        file_name=key.rsplit("/", 1)[-1],
        date=date,
        year=int(date[:4]),
        site_code=site_code,
        site_name=site_code,
        transect=transect,
        standard=True,
    )


def test_name_without_video_token_still_finds_the_site_name_and_ignores_case():
    parsed = parse_video_name(REAL_2023 + "tcrmp20231109_flc_T2.mp4", SITES)
    assert (parsed.standard, parsed.site_code, parsed.site_name, parsed.transect) == (True, "FLC", "Flat Cay", "T2")


def test_doubled_underscore_without_video_token():
    parsed = parse_video_name(REAL_2023 + "TCRMP20231206__CSE_T1.MP4", SITES)
    assert (parsed.standard, parsed.date, parsed.site_code, parsed.transect) == (True, "20231206", "CSE", "T1")


@pytest.mark.parametrize(
    "key, transect",
    [
        (REAL_2024 + "TCRMP20241029_video SHR_T1.MP4", "T1"),
        (REAL_2024 + "TCRMP20241029_video SHR_T2_incomplete.MP4", "T2_incomplete"),
        (REAL_2024 + "TCRMP20241029_video SHR_T6.MP4", "T6"),
        (REAL_2024 + "TCRMP20241029_video SHR_spacer.MP4", "spacer"),
        (REAL_2024 + "Other Videos/TCRMP20241029_video SHR_1_AG.MP4", "1_AG"),
    ],
)
def test_name_with_space_after_video(key, transect):
    parsed = parse_video_name(key, SITES)
    assert parsed == VideoName(
        file_name=key.rsplit("/", 1)[-1],
        date="20241029",
        year=2024,
        site_code="SHR",
        site_name="SHR",
        transect=transect,
        standard=True,
    )
    assert " " not in parsed.site_code and " " not in parsed.transect


@pytest.mark.parametrize("gap", [" ", "_", " _", "_ ", "  ", "__", " _ "])
def test_any_mix_of_spaces_and_underscores_after_video_is_tolerated(gap):
    parsed = parse_video_name(f"{REAL_2024}TCRMP20241029_video{gap}SHR_T1.MP4", SITES)
    assert (parsed.standard, parsed.site_code, parsed.transect) == (True, "SHR", "T1")


@pytest.mark.parametrize("number", range(1, 7))
def test_misspelled_video_token_vido(number):
    parsed = parse_video_name(f"{REAL_2024}TCRMP20241111_vido_BID_T{number}.MP4", SITES)
    assert (parsed.standard, parsed.date, parsed.site_code, parsed.transect) == (True, "20241111", "BID", f"T{number}")


@pytest.mark.parametrize(
    "file_name",
    [
        "TCRMP20240311_video_T1.MP4",
        "TCRMP20240311_VIDEO_T1.MP4",
        "TCRMP20240311_Video_T1.mp4",
        "TCRMP20240311_vido_T1.MP4",
        "TCRMP20240311__video_T1.MP4",
        "TCRMP20240311_video_video_T1.MP4",
        "TCRMP20240311_video_vido_T1.MP4",
        "TCRMP20240311_video VIDEO_T1.MP4",
        "TCRMP20151004_video_STX-STT_Dolphins.MTS",
        "TCRMP20151004_video_STX-STT_Sperm_Whales_Surface3.MTS",
    ],
)
def test_video_token_is_never_a_site_code(file_name):
    parsed = parse_video_name(FOLDER + file_name, SITES)
    assert parsed.site_code not in ("VIDEO", "VIDO")
    assert parsed == VideoName(
        file_name=file_name, date="", year=None, site_code="", site_name="", transect="", standard=False
    )


@pytest.mark.parametrize(
    "key",
    [
        REAL_2025_MISC + "TCRMP20250411_3D_BID1.MP4",
        REAL_2025_MISC + "TCRMP20250411_3D_BID2.MP4",
        REAL_2025_MISC + "TCRMP20250411_3D_BIDMISC2.MP4",
        REAL_2025_MISC + "TCRMP20250411_MISC.MP4",
        REAL_2025_MISC + "TCRMP20250411_T1_BID.MP4",
        REAL_2025_MISC + "TCRMP20250411_B2_T1.MP4",
        REAL_2025_MISC + "TCRMP20250411_TOOLONG_T1.MP4",
        REAL_2025_MISC + "TCRMP20250411 BID_T1.MP4",
    ],
)
def test_site_code_without_video_token_needs_three_to_five_characters_and_a_leading_letter(key):
    parsed = parse_video_name(key, SITES)
    assert parsed.standard is False
    assert parsed.site_code == ""
    assert parsed.year == 2025


def test_site_code_with_a_digit_is_accepted_in_both_forms():
    with_token = parse_video_name(REAL_2024 + "TCRMP20241105_video_LBP3_T1.MP4", SITES)
    without_token = parse_video_name(REAL_2024 + "TCRMP20241105_LBP3_T1.MP4", SITES)
    assert (with_token.standard, with_token.site_code) == (True, "LBP3")
    assert (without_token.standard, without_token.site_code) == (True, "LBP3")


@pytest.mark.parametrize(
    "key",
    [
        "TCRMP_video_ondeck/main/TCRMP2006_video/OtherVideo/TCRMP200602_video_LBH_T1-6.mp4",
        "TCRMP_video_ondeck/main/TCRMP2007_video/OtherVideo/TCRMP2007_video_GRP_T1-6.mp4",
        REAL_2024 + "Other Videos/TCRMP202410129_video_SSJ_01_NMK_lobster.mov",
    ],
)
def test_names_with_a_date_that_is_not_eight_digits_stay_outside_the_pattern(key):
    parsed = parse_video_name(key, SITES)
    assert parsed.standard is False
    assert (parsed.date, parsed.site_code, parsed.site_name, parsed.transect) == ("", "", "", "")
    assert parsed.year == int(key.split("/TCRMP")[1][:4])


def test_impossible_date_without_video_token_is_nonstandard():
    parsed = parse_video_name(REAL_2023 + "TCRMP20231345_BID_T1.MP4", SITES)
    assert parsed.standard is False
    assert parsed.year == 2023


def test_bare_file_name_without_folders_is_parsed():
    parsed = parse_video_name("TCRMP20241022_video_BIT_T4.MP4", SITES)
    assert parsed.standard is True
    assert parsed.site_name == "Buck Island"


def test_transect_keeps_inner_text_and_drops_outer_spaces():
    parsed = parse_video_name(FOLDER + "TCRMP20241022_video_FLC_T1 part 2 .MP4", SITES)
    assert parsed.transect == "T1 part 2"


def test_transect_of_spaces_alone_reads_as_blank_and_keeps_the_date_and_site():
    parsed = parse_video_name(FOLDER + "TCRMP20241022_video_FLC_ .MP4", SITES)
    assert parsed.standard is True
    assert parsed.transect == ""
    assert parsed.site_code == "FLC"
    assert parsed.year == 2024


@pytest.mark.parametrize("bad", [None, 7, b"TCRMP20241022_video_FLC_T1.MP4", ["x"], True])
def test_parse_rejects_non_text_key(bad):
    with pytest.raises(ValueError, match="key"):
        parse_video_name(bad, SITES)


@pytest.mark.parametrize("bad", [None, [("FLC", "Flat Cay")], "FLC", 3])
def test_parse_rejects_site_names_that_are_not_a_mapping(bad):
    with pytest.raises(ValueError, match="site_names"):
        parse_video_name(FOLDER + "TCRMP20241022_video_FLC_T1.MP4", bad)


def test_video_name_is_frozen():
    parsed = parse_video_name(FOLDER + "MVI_0203.MOV", SITES)
    with pytest.raises(Exception):
        parsed.transect = "T9"


def test_file_safe_strips_hostile_characters():
    assert file_safe("T1+T3-6") == "T1-T3-6"
    assert file_safe("../x") == "x"
    assert file_safe("") == "NA"


@pytest.mark.parametrize(
    "label, expected",
    [
        ("T1", "T1"),
        ("T5.2-6", "T5-2-6"),
        ("Other8", "Other8"),
        ("BL", "BL"),
        ("a b\tc", "a-b-c"),
        ("///", "NA"),
        ("..", "NA"),
        ("-T1-", "T1"),
        ("T1_T2", "T1-T2"),
        ("caf\u00e9 7", "caf-7"),
        ("x\x00y\r\nz", "x-y-z"),
        ("<script>alert(1)</script>", "script-alert-1-script"),
        ("C:\\temp\\x", "C-temp-x"),
        ("a--b", "a--b"),
    ],
)
def test_file_safe_cases(label, expected):
    assert file_safe(label) == expected


def test_file_safe_output_is_always_a_plain_file_name_part():
    hostile = "".join(chr(code) for code in range(0, 300)) + "\u202e\u2028"
    cleaned = file_safe(hostile)
    assert cleaned
    assert all(char.isascii() and (char.isalnum() or char == "-") for char in cleaned)
    assert not cleaned.startswith("-") and not cleaned.endswith("-")


@pytest.mark.parametrize("bad", [None, 5, b"T1", ["T1"]])
def test_file_safe_rejects_non_text(bad):
    with pytest.raises(ValueError, match="label"):
        file_safe(bad)


def test_load_site_names_skips_blank_and_rejects_missing_file(tmp_path):
    path = write_sites(tmp_path, "SiteCode,SiteName\nBID,\nBIT,Buck Island\nFLC,Flat Cay\nSSJ,   \n")
    assert load_site_names(path) == {"BIT": "Buck Island", "FLC": "Flat Cay"}
    missing = tmp_path / "nowhere" / "sites.csv"
    with pytest.raises(FileNotFoundError, match="nowhere"):
        load_site_names(missing)


def test_load_shipped_sites_file():
    loaded = load_site_names(SHIPPED_SITES)
    assert loaded["BIT"] == "Buck Island"
    assert loaded["FLC"] == "Flat Cay"


def test_load_site_names_upper_cases_codes_trims_cells_and_accepts_a_byte_order_mark(tmp_path):
    path = tmp_path / "sites.csv"
    path.write_bytes("\ufeffSiteCode,SiteName\r\n flc , Flat Cay \r\n\r\n".encode("utf-8"))
    assert load_site_names(path) == {"FLC": "Flat Cay"}


def test_load_site_names_accepts_extra_columns_and_quoted_commas(tmp_path):
    path = write_sites(tmp_path, 'SiteName,Region,SiteCode\n"Cay, Flat",STT,FLC\n')
    assert load_site_names(path) == {"FLC": "Cay, Flat"}


def test_load_site_names_header_only_file_gives_empty_mapping(tmp_path):
    assert load_site_names(write_sites(tmp_path, "SiteCode,SiteName\n")) == {}


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("", "empty"),
        ("\n\n", "empty"),
        ("Code,Name\nFLC,Flat Cay\n", "SiteCode"),
        ("SiteCode\nFLC\n", "SiteName"),
        ("SiteCode,SiteName\nFLC,Flat Cay\nFLC,Flat Key\n", "line 3"),
        ("SiteCode,SiteName\nF,Flat Cay\n", "line 2"),
        ("SiteCode,SiteName\nTOOLONG,Flat Cay\n", "line 2"),
        ("SiteCode,SiteName\nF-C,Flat Cay\n", "line 2"),
        ("SiteCode,SiteName\n,Flat Cay\n", "line 2"),
        ("SiteCode,SiteName\nFLC,Flat Cay,extra\n", "line 2"),
        ("SiteCode,SiteName\nFLC,=HYPERLINK(1)\n", "line 2"),
        ('SiteCode,SiteName\nFLC,"Flat\nCay"\n', "SiteName"),
        ("SiteCode,SiteName,SiteCode\nFLC,Flat Cay,FLC\n", "twice"),
    ],
)
def test_load_site_names_rejects_bad_files_and_names_the_problem(tmp_path, text, fragment):
    path = write_sites(tmp_path, text)
    with pytest.raises(ValueError) as caught:
        load_site_names(path)
    assert fragment in str(caught.value)
    assert "sites.csv" in str(caught.value)


def test_load_site_names_rejects_bytes_that_are_not_utf8(tmp_path):
    path = tmp_path / "sites.csv"
    path.write_bytes(b"SiteCode,SiteName\nFLC,Flat \xff\xfe Cay\n")
    with pytest.raises(ValueError, match="UTF-8"):
        load_site_names(path)


def test_load_site_names_rejects_a_folder(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_site_names(tmp_path)


def test_module_exposes_the_planned_interface():
    assert {"VideoName", "load_site_names", "parse_video_name", "file_safe"} <= set(dir(names))
