<!-- template stub: remove this line when the glossary has content, then add this module to /mnt/rip/vicarius_drive/CONTEXT-MAP.md -->
# {module_name}: glossary

Last updated: 2026-09-02 (LO)

This file is the vocabulary of the `{module_name}` module: the words its code, UI, docs and log rows use for the things it handles, one meaning each. Agents read it before naming anything in this clone. The platform glossary at `/mnt/rip/vicarius_drive/CONTEXT.md` covers the terms every module shares, and `/mnt/rip/vicarius_drive/CONTEXT-MAP.md` indexes every glossary on the drive. Format rules for all of them are in `/mnt/rip/vicarius_drive/docs/agents/domain.md`.

## How to fill this file

- What belongs: terms specific to this module's own domain, meaning the objects it takes in, produces, tracks or displays (a transect record, a mask, a tile, a batch, a colony) and the states those objects move through. If a word would confuse a reader who already knows the platform glossary, it belongs here.
- What does not belong: general programming concepts (timeout, retry, cache, error type, worker thread), platform-wide terms already defined in `/mnt/rip/vicarius_drive/CONTEXT.md` (run folder, descriptor, EML sidecar, lock, initials, purpose), and anything defined by the TCRMP 3D registry at `/mnt/rip/vicarius_drive/vicarius/_METADATA/3d/CONTEXT.md`. Point at the other glossary instead of restating it.
- One or two sentences per term. Define what the thing is, not what the code does with it. Be opinionated: when several words exist for one concept, pick one and list the rest under `_Avoid_`.
- Mark collisions with `_Avoid_`. When a word means something different in another module or at platform level, name the other meaning in the `_Avoid_` line so a reader knows which sense is in force here, for example `_Avoid_: mask (the reef_point_seg per-image mask)`.
- Group terms under subheadings when natural clusters appear; a flat list is fine when every term belongs to one area.
- Stamp `Last updated: YYYY-MM-DD (INITIALS)` near the top on every edit.
- When the first real term is in, delete the stub comment on line 1 and add a line for this module to `/mnt/rip/vicarius_drive/CONTEXT-MAP.md`.

## Language

The entry below is a worked example of the shape. Replace it with this module's first real term.

**Batch**:
The set of input files this module processes in one run, listed in the run's `analysis_params.yaml`; each input file yields one output subfolder named after the file's stem.
_Avoid_: job, queue, input set
