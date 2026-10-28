# A VICARIUS run is one screening dataset, and the page names its run on every call

Date: 2026-10-27 (LO)

Sponge Screener kept one data folder per server. Under VICARIUS the launcher mints run folders (`inprocess/run_<YY-MM-DD>_<INITIALS>/` with `project.json`) and opens the window with `?project_dir=`. We decided that each run folder is a whole screening dataset (its own `observations.csv`, images, tracks, settings, ID counter and export packages, written beside `project.json`), that the server keeps one workspace per run opened on first use, and that the page names its run on every call (the `X-Screener-Run` header, or `?run=` on an image), as VISTA's window does. The video cache stays shared and off the synced drive, in the work folder that `config/vicarius.ini` names.

Status: proposed (waiting for Lauren's review of the pull request)

## Considered options

- One shared dataset for every run, with runs as sessions: rejected, because the run folder would hold nothing and VICARIUS's run cards, purpose and initials would describe no data.
- One active run per server, swapped by `POST /api/project/open`: rejected, because a second window on another run, or a request in flight during the swap, would write into the wrong run without any error.
- A data folder below the run folder (`run_*/data/`): rejected as an extra level with no benefit; the run folder already belongs to this module.

## Consequences

- Sighting IDs restart at ID001 in every run, and each run exports its own package. Merging several runs into one package for OnDeck is not built.
- A data call without a run answers 409 under VICARIUS; standalone (`python3 screener.py`) keeps its single `data/` folder and refuses a run header.
- A run stores `export_root` as the relative `exports`, so a run folder that moves keeps exporting into itself.
- Lauren's existing screening data (a standalone `data/` folder) goes into one run folder whole, with its `observations.csv`, `frames/`, `crops/`, `tracks/`, `videos_screened.csv` and `settings.json`.

Source: Relay handoff 20261025T023243Z, Part C; plan /mnt/rip/chain_build/sponge-harness-staging/PLAN.md, decisions 3 and 4
Cost if wrong: moving to a shared dataset later means merging per-run tables with renumbered IDs and renamed images, and removing the run routing from the server and the page.
