---
name: gats
description: Continue building GATS. Runs the CLAUDE.md Work Loop from the next task in docs/PROGRESS.md and keeps going until a HUMAN step, a decision gate, or a real blocker.
disable-model-invocation: true
---

Run the **Work Loop** from `CLAUDE.md` now.

1. Re-read `CLAUDE.md`, then `docs/PROGRESS.md`, then the current milestone
   in `docs/ROADMAP.md`. Read `docs/DESIGN.md` if you are starting a new
   milestone.
2. If the user gave a task ID here ("$ARGUMENTS", e.g. `T2.3`), start with
   that task. Otherwise start with "Next up" in PROGRESS.md.
3. Work task after task: plan briefly → implement with tests → ruff, mypy
   and pytest green → commit → update PROGRESS.md and CHANGELOG.md. Don't
   wait for confirmation between tasks unless a task is tagged `[ASK]`.
4. Stop only at a `HUMAN` step, a `G*` decision gate, a blocker after 3
   honest attempts, or a user interrupt. Then reply with: **what changed ·
   blockers · exact commands the user must run** (PowerShell, copy-pasteable).

Every safety and research-integrity rule in CLAUDE.md applies. In
particular: never place real orders, never read or edit `.env`, never
delete anything under `data/`, never invent APIs or fee values, and never
tune on the holdout.
