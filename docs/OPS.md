# Operations (T7.4): running the recorder and the paper runtime 24/7

The laptop is fine for building. For a two-month paper run it is not: a
sleeping PC records nothing, and every gap in the recording is a gap in the
paper record. This page moves both processes to an always-on Linux VM.

**HUMAN steps** are marked. Claude wrote the units and scripts; you
provision the machine and own its credentials.

## The two processes

| Process | Command | Talks to | State |
|---|---|---|---|
| Recorder | `gats record` | NSE, BSE (filings, prices, reference data) | `data/gats.db`, `data/raw/` |
| Paper runtime | `gats paper run --name s1` | Upstox (read-only candles), local Ollama | the same database (`paper_*` tables) |

The recorder is the only process that talks to the exchanges. The paper
runtime reads filings from the database and cannot send an order.

## 1. Provision the VM (HUMAN)

Any small always-on Linux VM in an Indian or nearby region works (2 vCPU,
4 GB RAM, 50 GB disk is plenty for the recorder and the runtime; the
local LLM is the exception, see step 5). Then, as root:

```sh
apt-get update && apt-get install -y python3 python3-venv git sqlite3 rsync
useradd --system --create-home --home-dir /opt/gats gats
sudo -u gats git clone <your private repo URL> /opt/gats/app
cd /opt/gats/app
sudo -u gats python3 -m venv .venv
sudo -u gats .venv/bin/pip install -e ".[research]"
sudo -u gats .venv/bin/gats init
```

Set the clock to be kept by NTP (`timedatectl set-ntp true`): every
latency GATS measures depends on it.

## 2. Move the data (HUMAN)

Stop the recorder on the laptop first (two writers on two copies would
diverge). Copy `data\` to `/opt/gats/app/data` (`scp -r` or `rsync`), then
on the VM:

```sh
sudo chown -R gats:gats /opt/gats/app/data
sudo -u gats .venv/bin/gats status
```

## 3. Secrets (HUMAN)

Create `/opt/gats/app/.env` (mode 600, owner `gats`) from `.env.example`.
For paper trading it needs:

```
GATS_UPSTOX_ANALYTICS_TOKEN=<read-only token, valid one year>
GATS_TELEGRAM_BOT_TOKEN=<bot token>      # optional but recommended
GATS_TELEGRAM_CHAT_ID=<your chat id>
```

## 4. Start both services (HUMAN)

```sh
cp deploy/gats-recorder.service deploy/gats-paper.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gats-recorder
sudo -u gats .venv/bin/gats status        # wait until the jobs report in
systemctl enable --now gats-paper
journalctl -u gats-paper -f
```

## 5. The local LLM

The extraction cascade asks the LLM only when the rules are unsure. On a VM
without a GPU, Ollama on CPU answers in minutes, not seconds. Three honest
options:

- keep the model's host reachable from the VM and set `GATS_OLLAMA_URL`;
- run without it: filings the rules cannot read are handed to the strategy
  without facts (and skipped by it), which the journal records;
- rent a GPU VM, which costs more than the rest of the system.

Whichever you choose is part of what the paper run measures. Changing
`GATS_LLM_MODEL` changes the run's design hash, so it needs a new run name.

## 6. Backups (HUMAN: where they go)

`scripts/backup.sh <data dir> <backup dir>` copies new raw payloads and bar
files and takes a consistent snapshot of the database (keeping 14). Run it
nightly from cron, to a disk that is not the VM's own:

```
30 20 * * *  /opt/gats/app/scripts/backup.sh /opt/gats/app/data /mnt/backup/gats >> /var/log/gats-backup.log 2>&1
```

Test a restore once: a backup that was never restored is a hope.

## 7. Monitoring

- `gats status`: recorder problems in plain words (stopped, a job failing,
  throttling, disk). Exit code of `gats doctor` is 1 on failure: wire it to
  whatever alerts you already use.
- `gats paper status`: what each run has taken, ordered and filled; the
  measured feed and hand-over latencies; when the runtime last ticked.
- Telegram: signals, refusals, fills, errors and a summary after the close.
- `systemctl status gats-paper`: if it shows `start-limit-hit`, the runtime
  refused to start five times. `journalctl -u gats-paper -n 50` says why
  (usually a changed design: start a new run name in the unit file).

## 8. Postgres (optional)

SQLite in WAL mode carries one recorder and one runtime comfortably. Move
to Postgres when a second machine needs the data, not before:

```sh
sudo -u gats .venv/bin/pip install -e ".[postgres]"
# in .env:
GATS_DB_URL=postgresql+psycopg://gats:<password>@localhost/gats
```

`gats init` creates the schema. There is no SQLite-to-Postgres copy tool
yet; the raw store allows rebuilding the parsed tables with `gats reparse`,
but journals and paper records would need an export. Decide before a paper
run starts, not during one.

## Updating the code on the VM

```sh
cd /opt/gats/app && sudo -u gats git pull
sudo -u gats .venv/bin/pip install -e ".[research]"
systemctl restart gats-recorder gats-paper
sudo -u gats .venv/bin/gats paper status
```

If the update changed anything that decides a trade (the strategy, costs,
risk limits, the taxonomy, the extraction code), the paper runtime refuses
to resume the old run. That is intended: a changed system is a new paper
run. Give it a new name in `gats-paper.service` and note the date in
`docs/PROGRESS.md`; gate G3 needs two months of one unchanged system.
