# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-23 (not yet published)

### Added
- `session.HwpSession`: Hangul COM session with per-tool `gen_py` cache isolation,
  start retries (clean up once, then wait 6/10/14/18/22 s), restart-and-retry on a dead
  COM server, proactive recycling, safe quit that kills only the PID it started, and a
  `watchdog()` that unblocks calls which never return.
- `session.isolated_env`, `check_genpy_cache`, `rebuild_genpy_cache` (child-process
  rebuild; refuses to wipe a shared cache).
- `text`: `insert_text`, `insert_marked` (`<b>/<i>/<u>` via formatting toggles),
  `set_html`, `insert_html` (scratch document + native paste), `get_text`,
  `copy_document_text` (plain + CF_HTML grafting), and pure parsers
  `html_to_marked_text`, `graft_marks`, `strip_cf_html_header`, `split_marked`.
- `lock`: `FileLock` and `BuildQueue` (priorities, FIFO, waits for foreign Hangul,
  PID + start-time liveness), plus `scripts/hwp_queue.ps1` interoperating with it.
- `paths`: `check_path`, `fit_filename`, `safe_save`, `verify_saved`, `count_files`,
  `find_long_paths`, `extended_path`.
- `overflow`: page checker for PDF/HWP/HWPX (empty, under-filled, missing glyphs,
  page count) with PyMuPDF, Hangul or rhwp rendering; ruby-text (덧말) detection.
- CLI: `hwp-autokit doctor | pagecheck | queue status|run | paths scan | genpy`,
  and `hwp-pagecheck`.
- Tests: pure tests run anywhere; COM tests are opt-in (`HWP_AUTOKIT_COM_TESTS=1`).
