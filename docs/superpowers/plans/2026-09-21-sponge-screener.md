# Sponge Screener Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task by task. Each implementer sees the whole plan and owns only the files listed under their task.

**Goal:** A local app that streams TCRMP videos from the public `uviai` S3 bucket, turns each sponge sighting into a click, a species key, and Enter, and writes the ground-truth CSV plus a frame and a crop per sighting.

**Architecture:** A Python standard-library HTTP server on `127.0.0.1:8765` relays S3 videos through parallel range requests into an on-disk chunk cache, converts formats Chrome cannot play with ffmpeg, and stores sightings with atomic CSV writes. A vanilla JS page plays the relayed video, captures the paused frame on a same-origin canvas, and posts the sighting.

**Tech Stack:** Python 3.9 standard library, pytest 8, node 20 (`node --test`, `--experimental-websocket` for the Chrome DevTools driver), ffmpeg and ffprobe at `/opt/homebrew/bin`, Google Chrome.

**Spec:** `docs/superpowers/specs/2026-09-21-sponge-screener-design.md`

## Global Constraints

- Project root: `/Users/laurenkay/SpongeScreener`. Use absolute paths in every command.
- Python standard library only. No pip installs. No npm installs.
- Every function has a docstring (purpose, parameters, returns). Inputs are validated at the entry point. Errors say what failed, where, and why. Named constants live in `screener/config.py`. No dead code, no placeholders.
- Tests come first: write the failing test, run it, watch it fail, then write the code.
- Tests never touch the real bucket or the real `data/` folder, except `tests/test_integration_live.py`, which is marked `live` and only reads.
- The server binds to `127.0.0.1` only. Keys must start with `TCRMP_video_ondeck/`.
- No git commits. The requester has not asked for version control.
- User-facing text: American spelling, no em dashes, plain words. Every control carries a one or two sentence tooltip (`title`) that says who does what.
- Each implementer edits only the files listed under their task. A needed change in another task's file goes in the report, not in the file.
- Run tests with: `cd /Users/laurenkay/SpongeScreener && python3 -m pytest tests -q -m "not live and not e2e"`.

## File ownership ledger

| Task | Owner | Files |
|---|---|---|
| 0 Scaffold | lead | `screener/__init__.py`, `screener/config.py`, `screener/keys.py`, `tests/conftest.py`, `tests/test_keys.py`, `tests/fixtures/geometry_cases.json`, `config/species.csv`, `config/sites.csv`, `pytest.ini`, `.gitignore` |
| 1 Data layer | agent A | `screener/names.py`, `screener/positions.py`, `screener/species.py`, `screener/settings.py`, `screener/store.py`, `screener/export.py`, `tests/test_names.py`, `tests/test_positions.py`, `tests/test_species.py`, `tests/test_settings.py`, `tests/test_store.py`, `tests/test_export.py` |
| 2 Catalog and relay | agent B | `screener/s3catalog.py`, `screener/relay.py`, `tests/fakes3.py`, `tests/test_fakes3.py`, `tests/test_s3catalog.py`, `tests/test_relay.py` |
| 3 Conversion | agent C | `screener/convert.py`, `tests/test_convert.py` |
| 4 Front end | agent D | `static/index.html`, `static/style.css`, `static/geometry.js`, `static/app.js`, `tests/test_geometry.mjs` |
| 5 Server | agent E | `screener/server.py`, `screener.py`, `tests/test_server.py` |
| 6 End to end | agent F | `tests/e2e/cdp_driver.mjs`, `tests/test_e2e.py`, `tests/test_integration_live.py`, `README.md` |
| 7 Review | agent G | reads everything, edits nothing |

Tasks 1, 2, 3, 4 run in parallel. Task 5 follows 1 to 3. Task 6 follows 4 and 5. Task 7 follows 6.

---

## Task 0: Scaffold (done by the lead before dispatch)

`screener/config.py` constants: `APP_ROOT`, `BUCKET_URL = "https://uviai.s3.us-west-2.amazonaws.com"`, `KEY_PREFIX = "TCRMP_video_ondeck/"`, `CHUNK_SIZE = 4 MiB`, `RELAY_WORKERS = 16`, `READ_AHEAD_CHUNKS = 24`, `CHUNK_RETRIES = 3`, `CACHE_CAP_BYTES = 60 GiB`, `CACHE_KEEP_SECONDS = 600`, `HOST = "127.0.0.1"`, `PORT = 8765`, `PLAYABLE_EXTENSIONS = {"mp4","m4v","mov"}`, `VIDEO_EXTENSIONS = {"mp4","m4v","mov","mts","m2t","avi","mxf","wmv"}`, `CROP_SIZE = 512`, `MAX_IMAGE_BYTES = 40 MiB`, `MAX_KEY_LENGTH = 1024`, `MAX_NOTE_LENGTH = 500`, `MAX_TIME_SECONDS = 86400`, `TALLY_TARGET = 3`, `DEFAULT_ANNOTATOR = "LO"`, `DEFAULT_EXPORT_ROOT`, `CATALOG_MAX_AGE_SECONDS = 86400`, `VERSION = "1.0.0"`, and helpers `data_dir()`, `cache_dir()`, `config_dir()`, `static_dir()`.

`screener/keys.py`: `InvalidKey(ValueError)`, `validate_key(key) -> str`, `validate_prefix(prefix) -> str`, `quote_key(key) -> str` (percent-encodes everything outside unreserved characters and `/`, so `+` becomes `%2B`), `cache_id(key) -> str` (first 16 hex characters of SHA-1), `extension_of(key) -> str` (lowercase, no dot).

Key rules: a string, 1 to 1024 characters, starts with `TCRMP_video_ondeck/`, no `..` segment, no backslash, no control characters, no empty path segment. A prefix follows the same rules and ends with `/`.

---

## Task 1: Data layer (agent A)

**Interfaces consumed:** `screener.config`, `screener.keys`.

### `screener/names.py`

```python
@dataclass(frozen=True)
class VideoName:
    file_name: str          # basename of the key
    date: str               # "20241022", or "" when the name is outside the pattern
    year: Optional[int]     # from the date; else from a "TCRMP2016_video/" folder in the key; else None
    site_code: str          # "FLC" or ""
    site_name: str          # "Flat Cay"; the site code when sites.csv has no name; "" outside the pattern
    transect: str           # "T1", "T1-6", "T1+T3-6", "T5.2-6", "BL", "Other8", or ""
    standard: bool          # True when the name matched the TCRMP pattern

def load_site_names(path: Path) -> Dict[str, str]      # sites.csv: SiteCode,SiteName; blank names are skipped
def parse_video_name(key: str, site_names: Mapping[str, str]) -> VideoName
def file_safe(label: str) -> str                        # keep A-Z a-z 0-9 and "-", turn every other run into "-", strip "-" at the ends, "" becomes "NA"
```

Pattern, case-insensitive: `^TCRMP(\d{8})_video_([A-Za-z0-9]{2,5})_(.+)\.([A-Za-z0-9]+)$`. The date must be a real calendar date between 1990 and 2100, otherwise the name counts as outside the pattern. Site codes are returned in upper case.

Tests (`tests/test_names.py`): `test_standard_name`, `test_lowercase_extension_and_mixed_case`, `test_multi_transect_labels` (`T1-6`, `T1+T3-6`, `T5.2-6`, `BL`, `Other8`), `test_site_name_falls_back_to_code`, `test_nonstandard_name_returns_blanks` (`MVI_0203.MOV`), `test_year_from_folder_when_name_is_nonstandard`, `test_impossible_date_is_nonstandard` (`TCRMP20241345_video_FLC_T1.MP4`), `test_file_safe_strips_hostile_characters` (`T1+T3-6` becomes `T1-T3-6`; `../x` becomes `x`; `""` becomes `NA`), `test_load_site_names_skips_blank_and_rejects_missing_file`.

### `screener/positions.py`

```python
QUADRANTS = ("TOPLEFT", "TOPRIGHT", "BOTTOMLEFT", "BOTTOMRIGHT")
QUADRANT_PHRASES = {"TOPLEFT": "top left", "TOPRIGHT": "top right", "BOTTOMLEFT": "bottom left", "BOTTOMRIGHT": "bottom right"}
def validate_point(point: Any) -> Tuple[float, float]                 # {"x","y"} finite numbers in [0, 1]; ValueError names the field
def validate_box(box: Any) -> Optional[Tuple[float, float, float, float]]   # None passes; {"x","y","w","h"} in [0,1], w>0, h>0, x+w<=1.0001, y+h<=1.0001
def quadrant_of(x: float, y: float) -> str                            # x < 0.5 is LEFT, y < 0.5 is TOP
def anchor_point(point: Tuple[float, float], box: Optional[Tuple[float, float, float, float]]) -> Tuple[float, float]   # box center when a box exists
def format_clock(seconds: float) -> str                               # floor to whole seconds, "MM:SS", minutes may pass 99
```

Tests (`tests/test_positions.py`): `test_quadrant_cases_from_shared_fixture` and `test_clock_cases_from_shared_fixture` (both read `tests/fixtures/geometry_cases.json`), `test_validate_point_rejects_nan_inf_strings_bools_missing_fields_out_of_range`, `test_validate_box_accepts_none_and_edge_touching_box`, `test_validate_box_rejects_zero_negative_overflow`, `test_anchor_point_uses_box_center`, `test_format_clock_rejects_negative_and_nan`.

### `screener/species.py`

```python
@dataclass(frozen=True)
class Species:
    code: str; name: str; part: str; default_pin: str
PIN_KEYS = ("1","2","3","4","5","6","7","8","9","0")
def load_species(path: Path) -> List[Species]          # columns Code,ScientificName,GuidePart,DefaultPin; codes are 4 upper-case letters and unique; names non-empty
def default_pins(species: Sequence[Species]) -> Dict[str, Optional[str]]
def validate_pins(pins: Any, species: Sequence[Species]) -> Dict[str, Optional[str]]   # exactly the ten PIN_KEYS; values are a known code or None; a code appears once
```

Tests: `test_loads_shipped_species_file` (38 rows, ACAU first, UNKN present), `test_default_pins_match_shipped_file` (1 ACAU, 2 AFUL, 3 CDEL, 4 MLAE, 5 CPLI, 6 ACRA, 7 ACOM, 8 XMUT, 9 and 0 empty), `test_rejects_duplicate_code`, `test_rejects_bad_code_shape`, `test_rejects_missing_column`, `test_rejects_empty_file`, `test_validate_pins_rejects_unknown_code_duplicate_code_wrong_keys_non_dict`.

### `screener/settings.py`

```python
@dataclass
class Settings:
    annotator: str; pins: Dict[str, Optional[str]]; export_root: str
def validate_annotator(value: Any) -> str            # 1 to 12 characters from A-Z a-z 0-9 . _ -
def load_settings(path: Path, species: Sequence[Species]) -> Settings   # missing file gives defaults; a corrupt file is renamed to settings.json.corrupt-<UTC stamp> and defaults are returned
def save_settings(path: Path, settings: Settings) -> None               # atomic: temp file in the same folder, then os.replace
```

Tests: `test_missing_file_gives_defaults`, `test_round_trip`, `test_corrupt_file_is_set_aside_and_defaults_returned`, `test_stale_pin_code_is_dropped_on_load`, `test_validate_annotator_rejects_empty_long_and_hostile`.

### `screener/store.py`

```python
OBSERVATION_COLUMNS = ["Site","Transect","Sponge Type","Timestamp","Notes","ID","FileName","FrameFileName","AbbreviatedNote",
                       "SpeciesCode","TimestampSeconds","Quadrant","PointX","PointY","BoxX","BoxY","BoxW","BoxH",
                       "CropFileName","S3Key","Annotator","LoggedAt"]
SCREENED_COLUMNS = ["S3Key","FileName","Status","TargetSpecies","Sightings","Annotator","FirstOpened","MarkedDone"]
class StoreError(Exception): ...

class ObservationStore:
    def __init__(self, data_dir: Path, species: Sequence[Species], site_names: Mapping[str, str],
                 clock: Callable[[], datetime] = <UTC now>)
    def add(self, *, key: str, time_seconds: float, point: Any, box: Any, species_code: str, note: str,
            annotator: str, frame_jpeg: bytes, crop_png: bytes) -> Dict[str, str]
    def delete(self, obs_id: str) -> Dict[str, str]
    def rows(self, key: Optional[str] = None) -> List[Dict[str, str]]
    def tally(self) -> List[Dict[str, Any]]              # one dict per species with a sighting: code, name, sightings, videos, earliest_year (int or None); sorted by code
    def mark_opened(self, key: str, annotator: str, target_species: Sequence[str]) -> Dict[str, str]
    def mark_done(self, key: str, done: bool, annotator: str, target_species: Sequence[str]) -> Dict[str, str]
    def screened(self) -> List[Dict[str, str]]
    def video_status(self) -> Dict[str, Dict[str, Any]]  # key -> {"status": "in progress" | "done", "sightings": int}
```

Row rules:

- `Site` is `VideoName.site_name`. `Transect` is `VideoName.transect`. `Sponge Type` is the scientific name. `Timestamp` is `format_clock(time_seconds)`. `TimestampSeconds` has three decimals.
- `Quadrant` comes from `quadrant_of(*anchor_point(point, box))`. `Notes` is the quadrant phrase, then `", " + note` when a note exists. `AbbreviatedNote` is the quadrant code.
- `ID` is `ID` plus the next number (highest existing number plus one, at least three digits).
- `FrameFileName` is `<ID>_<CODE>_<QUADRANT>_<SITE>_<TRANSECT>.jpg` with `file_safe(site_code)` and `file_safe(transect)`. `CropFileName` has the same stem with `.png`. Images go to `data/frames/` and `data/crops/`.
- `PointX`, `PointY`, `BoxX`, `BoxY`, `BoxW`, `BoxH` have four decimals. Box cells stay empty without a box.
- `FileName` is the basename of the key. `LoggedAt` is UTC, `YYYY-MM-DDTHH:MM:SSZ`.
- Note cleaning: control characters and line breaks become spaces, runs of spaces collapse, the ends are stripped, and more than 500 characters is rejected.
- `time_seconds` is a finite number from 0 to 86400. `frame_jpeg` starts with `FF D8 FF`. `crop_png` starts with the 8-byte PNG signature. Each image is 1 byte to 40 MiB.
- Every write takes `fcntl.flock` on `data/.lock`, rereads the CSV, changes it, writes a temp file in the same folder, and calls `os.replace`. Images are written before the CSV row. A failed CSV write removes the images it just wrote.
- `delete` moves both images into `data/trash/<UTC stamp>_<file name>` and removes the row. An unknown ID raises `StoreError`.
- `videos_screened.csv`: `mark_opened` creates the row with `Status` `in progress` and `FirstOpened`, and leaves an existing row's `FirstOpened` and `done` status alone. `mark_done(done=True)` sets `Status` `done`, `MarkedDone`, `TargetSpecies` (codes joined by `;`), and `Annotator`. `mark_done(done=False)` returns the row to `in progress` and clears `MarkedDone`. `Sightings` is recounted from `observations.csv` on every write to either file.
- A CSV on disk with an unexpected header raises `StoreError` that names the file and both headers. The store never rewrites a file it cannot parse.

Tests (`tests/test_store.py`): `test_add_writes_row_and_images`, `test_first_nine_columns_match_january_header` (exact list: Site, Transect, Sponge Type, Timestamp, Notes, ID, FileName, FrameFileName, AbbreviatedNote), `test_ids_increment_and_survive_reload`, `test_box_sets_quadrant_from_box_center_and_fills_box_cells`, `test_note_is_cleaned_and_appended_after_quadrant_phrase`, `test_nonstandard_key_writes_blank_site_and_NA_file_parts`, `test_multi_transect_label_is_file_safe_in_file_names_and_verbatim_in_csv`, `test_rejects_unknown_species`, `test_rejects_bad_time_point_box_note_annotator_key` (parametrized), `test_rejects_images_with_wrong_magic_empty_or_oversized`, `test_failed_csv_write_removes_new_images` (monkeypatch `os.replace` to raise), `test_delete_moves_images_to_trash_and_removes_row`, `test_delete_unknown_id_raises`, `test_rows_filter_by_key`, `test_tally_counts_sightings_videos_and_earliest_year`, `test_mark_opened_then_done_then_reopened`, `test_sightings_recount_after_add_and_delete`, `test_unexpected_header_raises_and_leaves_file_untouched`, `test_concurrent_adds_from_threads_and_processes_give_unique_ids` (8 threads x 10 adds plus 2 subprocesses x 10 adds, 100 unique IDs, 100 rows, 200 image files), `test_csv_survives_commas_quotes_newlines_html_in_note`, `test_formula_leading_note_cannot_start_a_cell` (Notes always opens with the quadrant phrase).

### `screener/export.py`

```python
class ExportError(Exception): ...
def export_package(store: ObservationStore, export_root: Path, today: date) -> Path
```

Creates `<export_root>/spongeGroundTruth_<YYYYMMDD>` (then `_2`, `_3` when taken), copies `observations.csv`, `videos_screened.csv`, every frame and crop that a row references, and writes `README.md` (title, date, counts per species, number of videos, column definitions for both CSV files, the timestamp convention, the statement that a finished video with no rows for a target species is a true absence, and the note that frames from converted videos come from a re-encoded copy). It checks that every referenced image arrived, and raises `ExportError` naming the first missing file. A missing `export_root` and an empty store both raise `ExportError`.

Tests (`tests/test_export.py`): `test_export_creates_dated_package_with_all_files`, `test_second_export_same_day_gets_suffix`, `test_missing_root_raises_with_path`, `test_empty_store_raises`, `test_missing_image_raises_and_names_file`, `test_readme_lists_counts_and_every_column`.

---

## Task 2: Catalog and relay (agent B)

**Interfaces consumed:** `screener.config`, `screener.keys`.

### `tests/fakes3.py`

`class FakeS3` with `start() -> str` (base URL on a free port), `stop()`, `put(key: str, body: bytes)`, `requests: List[Dict]` (method, path, query, range header, time), `fail_next(key, times, status=500)`, `truncate_next(key, times)` (sends half the promised bytes, then closes), `delay_seconds: float`, `page_size: int` (forces listing pagination). It answers `GET /?list-type=2&prefix=&delimiter=/&continuation-token=` with S3 ListObjectsV2 XML (namespace `http://s3.amazonaws.com/doc/2006-03-01/`, `Contents/Key`, `Contents/Size`, `CommonPrefixes/Prefix`, `IsTruncated`, `NextContinuationToken`), `HEAD` and `GET /<percent-encoded key>` with `Range` support (206 with `Content-Range`, 416 past the end, 404 for unknown keys). `tests/test_fakes3.py` proves the fake: listing with a delimiter, pagination, a range read, 404, 416, and an injected failure.

### `screener/s3catalog.py`

```python
class CatalogError(Exception): ...
@dataclass(frozen=True)
class CatalogEntry:
    key: str; name: str; size: int; ext: str; playable: bool
@dataclass(frozen=True)
class CatalogPage:
    prefix: str; folders: List[str]; videos: List[CatalogEntry]; stale: bool
class S3Catalog:
    def __init__(self, bucket_url: str, cache_path: Optional[Path] = None, timeout: float = 30.0,
                 max_age_seconds: int = CATALOG_MAX_AGE_SECONDS, clock: Callable[[], float] = time.time)
    def list(self, prefix: str, refresh: bool = False) -> CatalogPage
```

Rules: validate the prefix; follow continuation tokens until `IsTruncated` is false; keep only keys whose extension is in `VIDEO_EXTENSIONS` and whose size is above zero; `playable` is true for `PLAYABLE_EXTENSIONS`; sort folders and videos in natural order (T2 before T10); save each page to the JSON file at `cache_path` with the fetch time; serve the saved page when it is younger than `max_age_seconds` and `refresh` is false; when the network fails and a saved page exists, return it with `stale=True`; when the network fails and no page exists, raise `CatalogError` with the URL and the reason; XML that does not parse raises `CatalogError`.

Tests: `test_lists_folders_and_videos`, `test_follows_pagination` (page size 2, 5 videos), `test_filters_non_video_and_zero_byte_keys`, `test_natural_sort`, `test_playable_flag_by_extension`, `test_keys_with_plus_and_spaces_round_trip`, `test_rejects_prefix_outside_tcrmp`, `test_serves_saved_page_without_network`, `test_refresh_bypasses_saved_page`, `test_network_failure_returns_stale_page`, `test_network_failure_without_saved_page_raises`, `test_malformed_xml_raises`, `test_corrupt_cache_file_is_ignored_and_rewritten`.

### `screener/relay.py`

```python
class RelayError(Exception): ...
class RangeNotSatisfiable(RelayError): ...
def parse_range_header(header: Optional[str], size: int) -> Tuple[int, int]   # inclusive; None gives (0, size-1); supports "bytes=a-b", "bytes=a-", "bytes=-n"; multi-range and garbage raise RangeNotSatisfiable
def chunk_span(start: int, end: int, chunk_size: int) -> range
class ChunkRelay:
    def __init__(self, bucket_url: str, cache_root: Path, chunk_size: int = CHUNK_SIZE, workers: int = RELAY_WORKERS,
                 read_ahead: int = READ_AHEAD_CHUNKS, retries: int = CHUNK_RETRIES, cap_bytes: int = CACHE_CAP_BYTES,
                 keep_seconds: int = CACHE_KEEP_SECONDS, timeout: float = 30.0)
    def size(self, key: str) -> int
    def open_reader(self, key: str, start: int, end: int) -> "RangeReader"
    def prefetch(self, key: str) -> None
    def cached_fraction(self, key: str) -> float
    def evict(self) -> int
    def close(self) -> None
class RangeReader:        # context manager; iterating yields bytes blocks in order and blocks until each chunk is on disk
    def __iter__(self) -> Iterator[bytes]
    def close(self) -> None
```

Rules:

- `size` sends one HEAD, stores `{key, size, chunk_size}` in `<cache_root>/chunks/<cache_id>/meta.json`, and answers from that file afterward. A 404 raises `RelayError` naming the key.
- Chunks live at `<cache_root>/chunks/<cache_id>/<index:06d>.bin`, written as `.part` and renamed. On startup a chunk file with the wrong length is deleted. A `meta.json` whose `chunk_size` differs from the relay's clears that folder.
- Workers hold one persistent `http.client` connection each (HTTPS or HTTP from the URL scheme) and reconnect after an error. A chunk request sends `Range: bytes=a-b`, expects 206, the exact length, and a `Content-Range` total equal to the known size. A different total raises `RelayError` ("object changed").
- Work queue priority: 0 for a chunk a reader is waiting on, 1 for read-ahead, 2 for prefetch. A worker drops a read-ahead task when no open reader sits within `read_ahead` chunks behind it.
- Each chunk gets `retries` attempts with 0.5, 1, 2 second backoff. After the last failure the waiting reader raises `RelayError("chunk N of <key> failed after 3 tries: <reason>")`, and the chunk returns to missing so a later request tries again.
- `evict` removes whole video folders, oldest `meta.json` modification time first, until the cache is under `cap_bytes`. It skips folders touched in the last `keep_seconds`. Opening a reader touches `meta.json`.
- `close` stops the workers and closes their connections. Readers that are waiting raise `RelayError("relay closed")`.

Tests (`tests/test_relay.py`, all against `FakeS3` with a small `chunk_size` such as 1024): `test_parse_range_forms`, `test_parse_range_rejects_garbage_multi_reversed_past_end`, `test_chunk_span`, `test_reads_whole_object_byte_for_byte`, `test_reads_inner_range_across_chunk_edges`, `test_last_short_chunk`, `test_second_read_comes_from_disk` (zero new GETs), `test_fetches_in_parallel` (with a delay, more than one request in flight), `test_read_ahead_stays_within_window`, `test_demand_chunk_jumps_the_queue`, `test_retries_then_succeeds`, `test_gives_up_after_retries_and_later_request_recovers`, `test_truncated_body_is_retried_not_cached`, `test_unknown_key_raises`, `test_object_size_change_raises`, `test_hostile_key_rejected_before_any_request`, `test_key_with_plus_is_percent_encoded`, `test_wrong_length_chunk_file_is_discarded_on_startup`, `test_prefetch_fills_cache_and_fraction_reaches_one`, `test_evict_removes_oldest_and_keeps_recent`, `test_close_unblocks_waiting_reader`, `test_abandoned_reader_stops_read_ahead`.

---

## Task 3: Conversion (agent C)

**Interfaces consumed:** `screener.config`, `screener.keys`.

### `screener/convert.py`

```python
class ConvertError(Exception): ...
@dataclass
class ConvertStatus:
    state: str          # "none" | "queued" | "running" | "done" | "failed"
    progress: float     # 0.0 to 1.0
    message: str
def probe(ffprobe: str, source: str, timeout: float = 180.0) -> Dict[str, Any]   # {"codec","width","height","field_order","duration"}
def pick_encoder(ffmpeg: str) -> str                                              # "h264_videotoolbox" when listed by `ffmpeg -encoders`, else "libx264"
def build_ffmpeg_args(ffmpeg: str, source: str, info: Mapping[str, Any], out_path: Path, encoder: str) -> List[str]
class Converter:
    def __init__(self, cache_root: Path, source_for: Callable[[str], str], ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe")
    def start(self, key: str) -> ConvertStatus
    def status(self, key: str) -> ConvertStatus
    def converted_path(self, key: str) -> Optional[Path]
    def close(self) -> None
```

Rules:

- `source_for(key)` returns what ffmpeg reads: the local relay URL in the app, a file path in tests.
- `build_ffmpeg_args`: H.264 with `field_order` progressive or unknown gives `-c:v copy`. Everything else gives the encoder, `-pix_fmt yuv420p`, `-b:v 25M` at 720 rows and above or `8M` below (`-preset veryfast -crf 18` replaces the bitrate for libx264), and `-vf yadif` when `field_order` is `tt`, `bb`, `tb`, or `bt`. Every command has `-an -movflags +faststart -progress pipe:1 -nostats -y` and writes MP4 at the source resolution.
- Output goes to `<cache_root>/converted/<cache_id>.mp4.part` and is renamed to `.mp4` on success, with `<cache_id>.json` beside it (key, probe result, arguments). One job runs at a time in one worker thread. Progress is `out_time` over the probed duration.
- `start` is idempotent: done stays done, queued and running stay as they are, failed starts again. A failure stores the last 400 characters of ffmpeg's stderr in `message` and removes the `.part` file. On startup a finished file on disk reports `done`, and stray `.part` files are removed.
- `close` terminates a running ffmpeg, joins the worker, and removes the `.part` file.

Tests (`tests/test_convert.py`; fixtures build 2 second sample clips with ffmpeg lavfi `testsrc`: progressive H.264 in MPEG-TS, interlaced MPEG-2 in MPEG-TS, DV in AVI at 720x480): `test_probe_reports_codec_size_field_order_duration`, `test_probe_missing_file_raises`, `test_args_copy_for_progressive_h264`, `test_args_transcode_and_deinterlace_for_interlaced_mpeg2`, `test_args_bitrate_by_height`, `test_args_libx264_fallback`, `test_convert_h264_ts_to_playable_mp4` (probe of the output: h264, same size, duration within 0.2 s), `test_convert_mpeg2_interlaced`, `test_convert_dv_avi`, `test_status_progresses_to_done`, `test_start_is_idempotent`, `test_failed_conversion_reports_stderr_and_cleans_part`, `test_failed_can_restart`, `test_finished_file_is_recognized_after_restart`, `test_hostile_key_rejected`, `test_close_stops_running_job`.

---

## Task 4: Front end (agent D)

**Interfaces consumed:** the HTTP contract in Task 5. The page works only against that contract.

### `static/geometry.js` (ES module, pure)

```js
export function contentRect(elemW, elemH, videoW, videoH)   // {x, y, w, h} of the letterboxed picture inside the element
export function toNormalized(px, py, rect)                  // {x, y} in 0..1, or null outside the picture
export function quadrantOf(x, y)                            // "TOPLEFT" | "TOPRIGHT" | "BOTTOMLEFT" | "BOTTOMRIGHT"
export function isDrag(ax, ay, bx, by, thresholdPx = 6)
export function normalizeBox(a, b)                          // two normalized corners -> {x, y, w, h}, clamped to 0..1
export function cropRect(point, box, videoW, videoH, size = 512)   // source pixels {sx, sy, sw, sh}; a box crops the box; a point crops a size x size square shifted to stay inside the frame
export function formatClock(seconds)                        // "MM:SS", floor
export function filterSpecies(list, query)                  // code prefix first, then word prefix in the name, then substring; case-insensitive; empty query returns the list
export function mergeTally(tally, pins, species)            // rows for every pinned species (zeros included) plus any other species with sightings
```

`tests/test_geometry.mjs` (run with `node --test /Users/laurenkay/SpongeScreener/tests/test_geometry.mjs`): quadrant and clock cases from `tests/fixtures/geometry_cases.json`; `contentRect` for wider, taller, and equal aspect; `toNormalized` inside, on the edge, and in the letterbox bars; `isDrag` below and above the threshold; `normalizeBox` with reversed corners and corners outside the picture; `cropRect` at the center, at each corner, with a frame smaller than the crop, and with a box; `filterSpecies` ranking and empty query; `mergeTally` with zeros.

### Page behavior

Layout: a header (name, annotator field, Export button, `?` shortcuts card), a left panel (breadcrumb, search, folders, videos with status and format badges and a queue button), a center column (stage with the video and an overlay canvas, transport row, species strip with the ten pinned slots, filter input, note input, Save, Save and stay, Cancel), and a right panel (sightings in this video with a crop thumbnail from `/media/crops/<name>`, click to jump, delete; species tally with rows under `tally_target` highlighted). Dark neutral theme, magenta marker and box with a white outline, quadrant highlight at 12 percent white, 13 px minimum type, visible focus rings, `aria-label` on icon buttons. The layout holds at 946 px and 1280 px wide with no horizontal scroll.

Modes and keys:

- Screening mode: click pauses and starts a mark at the point; drag past 6 px draws a box; Space toggles play; Left and Right jump 2 s; Comma and Period step 1/30 s; `[` and `]` step speed through 0.5, 0.75, 1, 1.5, 2, 3; G toggles the grid and center line; Z removes the last sighting in this video; D toggles done; N opens the next video in the list; Home seeks to 0; `?` toggles the shortcuts card.
- Marking mode: 1 to 9 and 0 choose a pinned species; a letter key moves focus to the filter input and types there; Up and Down move through matches; Enter in the filter chooses the highlighted match; Tab reaches the note; Enter with a chosen species saves and resumes; Shift+Enter saves and stays paused; Escape cancels and resumes; a new click or drag replaces the mark; Comma and Period still step; Space does nothing.
- Keys typed inside the annotator, search, filter, or note inputs type normally, except Enter, Escape, and Tab.
- Save: draw the paused frame on an offscreen canvas at `videoWidth` x `videoHeight`, make the JPEG at quality 0.95 and the PNG crop from `cropRect`, post to `/api/observations`. On success add the row, refresh the tally, toast `Saved ID### CODE`, and resume or stay. On failure keep the mark and show the server's error text.
- Opening a video posts `/api/videos/open`, starts muted autoplay, and resumes from the position saved in `localStorage` for that key minus 2 s. A video with `playable` false, or a `video` error event, starts `/api/convert` and polls every 2 s with a progress message, then plays `/video?key=...&converted=1`.
- Every mutating request sends the header `X-Screener: 1`.
- `window.__screener` exposes `{ state }` (mode, current key, pending mark, rows) for the end-to-end driver.

---

## Task 5: Server and entry point (agent E)

**Interfaces consumed:** every module from Tasks 0 to 3, exactly as written above.

### `screener/server.py` and `screener.py`

```python
class ScreenerApp:
    def __init__(self, data_dir: Path, cache_dir: Path, config_dir: Path, static_dir: Path, bucket_url: str, port: int)
    def close(self) -> None
def make_server(app: ScreenerApp, host: str, port: int) -> ThreadingHTTPServer
```

`screener.py`: `main(argv) -> int` with `--port` (default 8765), `--no-browser`, `--data-dir`, `--cache-dir`, `--bucket-url`. It starts the server, prints the URL, opens Google Chrome with `open -a "Google Chrome" <url>` when Chrome is installed and the default browser otherwise, and shuts down cleanly on Ctrl+C.

HTTP contract (JSON unless noted; errors are `{"error": "<field>: <reason>"}`):

| Route | Answer |
|---|---|
| `GET /` and `GET /static/<file>` | the page and its files; only files inside `static/`; correct content types |
| `GET /api/health` | `{"ok": true, "version": "1.0.0"}` |
| `GET /api/catalog?prefix=P[&refresh=1]` | `{"prefix","parent","stale","folders":[{"prefix","name"}],"videos":[{"key","name","size","ext","playable","status","sightings","converted"}]}`; `parent` is null at `TCRMP_video_ondeck/`; `status` is `new`, `in progress`, or `done`; `converted` is a `ConvertStatus.state` |
| `GET /api/species` | `{"species":[{"code","name","part"}],"pins":{...},"annotator","tally_target"}` |
| `POST /api/settings` `{"annotator"?, "pins"?}` | same shape as `GET /api/species` |
| `GET /api/observations?key=K` | `{"rows":[...]}` |
| `POST /api/observations` `{"key","time","point":{"x","y"},"box":null or {"x","y","w","h"},"species","note","frame_jpeg","crop_png"}` (images as base64, a `data:` URL prefix is accepted) | 201 `{"row": {...}}` |
| `DELETE /api/observations/<ID>` | `{"deleted": {...}}`; unknown ID is 404 |
| `GET /api/tally` | `{"tally":[{"code","name","sightings","videos","earliest_year"}],"total"}` |
| `POST /api/videos/open` `{"key"}` and `POST /api/videos/done` `{"key","done"}` | `{"video": {...}}` |
| `POST /api/convert` `{"key"}` and `GET /api/convert?key=K` | `{"state","progress","message"}` |
| `POST /api/prefetch` `{"key"}` and `GET /api/prefetch?key=K` | `{"cached": 0.0 to 1.0}` |
| `POST /api/export` | `{"path","observations"}` |
| `GET /video?key=K` | the relayed bytes; honors `Range` with 206, `Content-Range`, `Accept-Ranges: bytes`, exact `Content-Length`; 416 for a bad range; `video/mp4` for mp4, m4v, mov and `application/octet-stream` otherwise |
| `GET /video?key=K&converted=1` | the converted MP4 from disk with the same range rules; 404 until conversion is done |
| `GET /media/frames/<name>` and `GET /media/crops/<name>` | the image; names must match `^[A-Za-z0-9_.-]+$` and exist |

Rules: `protocol_version = "HTTP/1.1"` with a correct `Content-Length` on every answer; a request whose `Host` is not `127.0.0.1:<port>` or `localhost:<port>` gets 403; POST and DELETE without `X-Screener: 1` get 403; a body above 120 MiB gets 413; malformed JSON gets 400; a client that hangs up mid-stream ends the handler quietly; an unexpected exception gets 500 with a JSON error and a traceback on stderr; request logging goes to stderr in one line per request, and range streams log once. The converter's `source_for` is `http://127.0.0.1:<port>/video?key=<quoted key>`. Target species for `mark_opened` and `mark_done` are the currently pinned codes.

Tests (`tests/test_server.py`, a real server on a free port, temp data and cache folders, `FakeS3` as the bucket): `test_health`, `test_serves_page_and_static_with_types`, `test_static_traversal_blocked`, `test_catalog_shape_and_status_merge`, `test_catalog_rejects_bad_prefix`, `test_species_and_settings_round_trip`, `test_settings_rejects_unknown_pin_code`, `test_video_range_bytes_match_source`, `test_video_without_range_streams_everything`, `test_video_bad_range_416`, `test_video_unknown_key_404`, `test_video_key_outside_prefix_400`, `test_save_observation_end_to_end` (row returned, CSV on disk, images on disk, `data:` prefix accepted), `test_save_rejects_each_bad_field_with_named_error` (parametrized: key, time, point, box, species, note, frame_jpeg, crop_png), `test_delete_observation_and_unknown_id_404`, `test_tally`, `test_open_and_done`, `test_media_route_serves_images_and_blocks_traversal`, `test_mutations_require_header`, `test_foreign_host_header_403`, `test_malformed_json_400`, `test_oversized_body_413`, `test_client_disconnect_mid_stream_leaves_server_healthy`, `test_convert_routes` (with a tiny MPEG-TS sample in `FakeS3`), `test_prefetch_routes`, `test_export_route_writes_package` (export root set through `data/settings.json`), `test_unknown_route_404`.

---

## Task 6: End to end, live check, pixels, README (agent F)

- `tests/e2e/cdp_driver.mjs`: run with `node --experimental-websocket`. It launches Chrome headless with a temp profile and `--remote-debugging-port=0`, opens the app URL given as an argument, sets the viewport, and drives it with real DevTools input events (`Input.dispatchMouseEvent`, `Input.dispatchKeyEvent`): open a folder, open a video, wait for playback, click the picture, press `1`, press Enter, drag a box, type a filter, save, press `z`, press `d`, and take screenshots with `Page.captureScreenshot` at 946 x 900 and 1280 x 900. It prints one JSON line with the observed state after each step.
- `tests/test_e2e.py` (marked `e2e`): starts `FakeS3` holding a 6 second 1280x720 H.264 MP4 made with ffmpeg lavfi and one MPEG-TS clip, starts the app on a free port with temp folders, runs the driver, and asserts: playback advanced, the video paused on click, the saved row has the expected species, quadrant, and a timestamp within 0.5 s of the click, the frame JPEG is 1280x720 and the crop PNG is 512x512 (read the sizes from the file headers), the box sighting stores box cells, undo removed the last row and moved its images to trash, done status reached `videos_screened.csv`, the MPEG-TS clip converted and played, and both screenshots exist and are larger than 30 KB.
- `tests/test_integration_live.py` (marked `live`, read only): lists `TCRMP_video_ondeck/2024Annual/` from the real bucket and finds 12 videos; relays bytes 100,000,000 to 100,999,999 of `TCRMP20241022_video_FLC_T1.MP4` and compares them with a direct `urllib` range read of the same bytes; measures relay throughput over 64 MB and asserts it beats 52 Mbps; compares the January header, read from the first line of the January `observations.csv` when that file is reachable, with `OBSERVATION_COLUMNS[:9]`.
- `README.md`: what the tool does, how to start it, the keys, where data lives, what the export contains, how to add site names and species, how to run the tests.
- Pixel review: read both screenshots as images and answer the seven questions (spacing, clarity, functionality, layout, accessibility, consistency, ease of use) with a one-line verdict each. Report defects in files owned by other tasks to the lead.

---

## Task 7: Independent review (agent G)

Read the spec, the plan, and every file. Run the full suite, the e2e test, and the live test. Take fresh screenshots at 946 and 1280 px and answer the seven questions. Report defects (wrong output, a crash, a silent miscount, a broken control) apart from notes (wording, a test that could bite harder, style). The lead closes every defect, then asks for one re-review.
