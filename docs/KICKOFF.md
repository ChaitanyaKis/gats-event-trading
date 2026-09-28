# Building GATS with Claude Code

## One-time setup (5 minutes)

1. Open the `gats` folder in VS Code (File → Open Folder).
2. Open a terminal (Terminal → New Terminal) and start the recorder. Leave it
   running:
   ```powershell
   py -m venv .venv
   .venv\Scripts\python -m pip install -e ".[dev]"
   .venv\Scripts\gats init
   powershell -ExecutionPolicy Bypass -File scripts\run_recorder.ps1
   ```
   (Skip the first three lines if `.venv` already exists.)
3. Open the Claude Code panel. Click the mode indicator at the bottom of the
   prompt box and choose **Auto**, so it doesn't ask before every edit. The
   project's `.claude/settings.json` pre-approves tests, `gats` commands and
   git commits, and blocks the dangerous ones (live trading, reading `.env`,
   deleting `data/`, force-push).

## First session: paste this once

```
You are the lead engineer on GATS. Read CLAUDE.md, then docs/PROGRESS.md, docs/DESIGN.md, docs/ROADMAP.md and docs/DATA_SOURCES.md. Then run the Work Loop from CLAUDE.md, starting with the "Next up" task in PROGRESS.md, and keep going task after task until you reach a HUMAN step, a decision gate, or a real blocker. Follow every rule in CLAUDE.md, especially: verify every external API before relying on it, never place real orders, never read .env, and update PROGRESS.md after every task so the next session can resume with /gats.
```

## Every later session

Type:

```
/gats
```

To jump to a specific task: `/gats T2.3`. If `/gats` doesn't appear in the
menu, just type `continue`; CLAUDE.md tells Claude Code what that means.

## What still needs you

Claude Code builds and tests everything, but it stops for:

- **long runs:** multi-hour backfills in a separate terminal (it gives you
  the exact command),
- **accounts and keys:** GitHub, broker API keys, the Telegram bot, a VM,
- **judgement:** labelling ~300 filings (M4), decision gates G1–G3, and
  anything involving money (M8 is switched on only by you).

When it stops, it ends with exactly what to run or send. Do that, then type
`/gats`.
