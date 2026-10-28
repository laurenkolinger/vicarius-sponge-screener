# Decision records for sponge_screener

Last updated: 2026-09-02 (LO)

This folder holds the architecture decision records (ADRs) for the `sponge_screener` module: short notes that record that a decision was made and why, so a future reader does not undo it by accident. Format rules for every level are in `/mnt/rip/vicarius_drive/docs/agents/domain.md`; the rule that triggers a record is in the `Every change batch` section of `/mnt/rip/vicarius_drive/CLAUDE.md` under "Decision records".

## Numbering

- Files are `NNNN-slug.md`, four digits, sequential: `0001-first-decision.md`, then `0002-...`. Scan this folder for the highest number and add one.
- `0000-template.md` is the blank form. Copy it to start a record; never renumber it, delete it, or count it as a decision.
- A superseded record stays in place with `Status: superseded by ADR-NNNN`; the newer record names the one it replaces.

## The bar

Write a record only when all three hold:

1. Hard to reverse: changing the decision later costs real work or breaks something downstream.
2. Surprising without context: a future reader will look at the code and wonder why it was done this way.
3. A real trade-off: there were genuine alternatives and one was chosen for specific reasons.

An easy-to-reverse choice gets reversed, not recorded. An obvious choice surprises nobody. A choice with no alternative has nothing to record beyond "we did the obvious thing". What qualifies: architectural shape; integration patterns with other modules or with the platform; technology choices that carry lock-in; boundary and scope decisions, including an explicit "this module does not own X"; deliberate deviations from the obvious path; constraints not visible in the code; and rejected alternatives when the rejection is non-obvious.

## Required lines

- `Source:` citing the TODO uid (the `T20260902-214210` form) from `/mnt/rip/vicarius_drive/vicarius/_METADATA/logs/todo.md`, or the path of the spec or plan under `/mnt/rip/vicarius_drive/docs/superpowers/`, that the decision came from. A record without a source is incomplete.
- `Cost if wrong:` one sentence on what undoing the decision takes.

## Which folder

- Decisions about this module's own code, data shapes, UI or boundaries: here.
- Decisions that bind the platform or more than one module (the launcher, the lock registry, the data dictionary, the hooks, the rules file): `/mnt/rip/vicarius_drive/docs/adr/` in the harness, never here.
- Decisions about the TCRMP 3D registry: `/mnt/rip/vicarius_drive/vicarius/_METADATA/3d/docs/adr/`.

## When

A record is written in the same change as the decision, alongside the changelog line in `/mnt/rip/vicarius_drive/vicarius/_METADATA/logs/changelog.md`, and committed with the code it explains.
