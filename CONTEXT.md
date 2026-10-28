# sponge_screener: glossary

Last updated: 2026-10-27 (LO)

This file is the vocabulary of the `sponge_screener` module (Sponge Screener): the words its code, page, docs and export package use for the things it handles, one meaning each. The platform glossary at `/mnt/rip/vicarius_drive/CONTEXT.md` covers the terms every module shares (run folder, descriptor, lock, initials, purpose), and `/mnt/rip/vicarius_drive/CONTEXT-MAP.md` indexes every glossary on the drive. Format rules: `/mnt/rip/vicarius_drive/docs/agents/domain.md`.

## Screening

**Sighting**:
One sponge a person marked in one video at one moment, with its species: one row in `observations.csv` with `LabelStatus` `labeled`, plus its frame and its crop. Its ID (`ID001`, `ID002`, ...) counts up within one screening dataset.
_Avoid_: observation (the code's word for any row, Later points included), detection (a model's output), annotation

**Later point**:
A sponge a person marked without naming its species yet: a full row with `LabelStatus` `unlabeled` and `LATER` in place of the species code in its file names. It waits in the To label list and stays out of the tally, the Done status and the export.
_Avoid_: unlabeled sighting, placeholder

**Mark**:
The point or box a person draws on the picture to start a sighting; it rides along the sighting's track afterwards.
_Avoid_: click, annotation

**Pinned species**:
Up to ten species on the number keys 1 to 9 and 0, the species a person screens a video for. A done video records them as its target species.
_Avoid_: favorites, shortcuts

**Done video**:
A video a person marked fully screened. Every pinned species with no sighting in a done video counts as truly absent from it; a video with Later points cannot be marked done.
_Avoid_: finished, complete, reviewed

**Quadrant**:
The quarter of the frame the sponge sits in (`TOPLEFT`, `TOPRIGHT`, `BOTTOMLEFT`, `BOTTOMRIGHT`), from the point or the box center. It appears in file names and, in words, at the start of the Notes column.

**Track**:
The path of one sighting's sponge through its video, from about 12 s before to 15 s after the sighting, as points `[t, x, y, score]` in `tracks/ID###.json`. The score is the likeness of the tracked patch to the sighting's patch, from 0 to 1.
_Avoid_: trajectory, path

## Datasets

**Screening dataset**:
One set of sightings, Later points, screened videos, tracks and settings with its own ID counter. Under VICARIUS it is one run folder; standalone it is the `data/` folder.
_Avoid_: project, session, campaign

**Export package**:
A dated folder `spongeGroundTruth_<YYYYMMDD>` with `observations.csv`, `videos_screened.csv`, `tracks.csv`, every frame and crop of the labeled sightings, and a README; the shape of the January 2026 package sent to OnDeck AI.
_Avoid_: export, bundle, delivery

**Annotator**:
The initials written into every sighting and every screened-video row, kept per screening dataset in `settings.json`. Under VICARIUS the field starts on the initials the person signed in with.
_Avoid_: reviewer, user, operator
