# Live pilot runbook (M8)

**Everything on this page is done by the human.** Claude builds and tests
the code; it never switches live trading on, never runs `gats live` or
`gats gate approve`, never touches `.env`, and never edits the caps in
`configs/live.yaml`. Real money, accounts and credentials are yours.

Live trading is a small pilot of the system that was paper-traded and
passed gate G3. It is not a way to find out whether the strategy works:
that is what G1 to G3 are for.

## What stands between the code and a real order

Three independent locks. Each must be opened by you; none has a bypass.

| Lock | Where | Opened by |
|---|---|---|
| 1. The switch | `GATS_LIVE_ENABLED` in `.env` (default: off) | you, by hand |
| 2. The approval | a row in `live_approvals`, for one design, one set of caps, one G3 report | `gats gate approve`, typed at a terminal |
| 3. The caps | `configs/live.yaml` (shipped as 0 = not set) | you, by editing the file |

`gats gate status` lists, at any time, every reason live trading may not
start. It changes nothing and is safe to run.

## Before the first live day (once)

1. **Gate G3 must say PASS.** `reports/M7_paper.md` (written after at least
   two months of paper trading) has a line `**G3: PASS**`. If it says FAIL
   or does not exist, stop here.
2. **Static IP (SEBI requirement).** Brokers must accept API orders only
   from a static IP registered by the client (NSE circular NSE/INVG/67858
   and the exchange FAQ of 2025-11-03; see `docs/DATA_SOURCES.md`). Get a
   static IP for the machine that will run `gats live` (a cloud VM has one;
   a home connection usually does not) and register it with Upstox in the
   developer console. Without it the broker answers `UDAPI1154`.
3. **Broker API app.** In the Upstox developer console create an app and
   note its API key and secret. Keep them out of the repository.
4. **Decide the caps.** Edit `configs/live.yaml` and replace every 0:

   ```yaml
   caps:
     max_capital_rs: <most rupees deployed at once>
     max_daily_loss_rs: <loss at which it switches itself off for the day>
     max_position_rs: <most rupees in one stock>
     max_orders_per_day: <real orders per day, entries and exits together>
   ```

   Start with an amount whose complete loss would not matter to you. The
   pilot's purpose is to measure real fills and slippage against the paper
   run, not to make money. Commit the file: the approval is bound to these
   exact numbers, and changing any of them voids it.
5. **Order status words.** `configs/live.yaml` maps the broker's status
   words that end an order to GATS's states. Only `complete` is confirmed
   from the documentation. Open Upstox's order-status appendix and add the
   words for a cancelled and a rejected order:

   ```yaml
   order_end_statuses:
     complete: filled
     <the broker's word>: cancelled
     <the broker's word>: rejected
   ```

   An unlisted word is read as "still working", which is safe (the order
   keeps being followed and reconciled) but slower to settle.
6. **Alerts.** Create a Telegram bot with @BotFather, send it a message,
   and put its token and your chat id in `.env`:

   ```
   GATS_TELEGRAM_BOT_TOKEN=<token>
   GATS_TELEGRAM_CHAT_ID=<chat id>
   ```

   Run paper trading for a day and check that messages arrive. Do not go
   live without working alerts: a switch-off you do not hear about is a
   position nobody is watching.

## Approving (once per design and set of caps; expires after 30 days)

```powershell
cd C:\Projects\GATS
.venv\Scripts\gats gate status      # read every line
.venv\Scripts\gats gate approve     # shows the design and the caps, then asks
```

`gats gate approve` prints the design, the G3 verdict and each cap in
rupees, then a sentence to type exactly, naming the design and the capital
at risk. Read the numbers before typing. It refuses if the caps are unset,
if G3 did not pass, or if it is not run from an interactive terminal.

Withdraw an approval at any time: `gats gate revoke <number> --reason "..."`.

## Each live day

1. **Access token.** Upstox's order token is valid for one trading day.
   Generate it through the login flow of your API app and set it in `.env`:

   ```
   GATS_UPSTOX_ACCESS_TOKEN=<today's token>
   GATS_LIVE_ENABLED=true
   ```

   (The read-only Analytics Token cannot place orders and is not enough.)
2. **Check.** `gats status` (recorder healthy), `gats gate status`
   ("live trading MAY start"), and that `data\KILL` does not exist.
3. **Start**, with the recorder already running in its own terminal:

   ```powershell
   .venv\Scripts\gats live --name pilot1
   ```

4. **Watch** the Telegram messages: every real order, every fill, every
   error, and a summary after the close.
5. **Stop** with Ctrl+C. Then set `GATS_LIVE_ENABLED=false` again.

## Stopping in a hurry

| You want | Do |
|---|---|
| No new entries, exits still allowed | `New-Item data\KILL` |
| Everything to stop sending orders | Ctrl+C in the `gats live` terminal |
| To be flat now | Close the positions in the Upstox app. GATS will see the difference at its next reconciliation and switch itself off. |
| Live trading impossible until further notice | `gats gate revoke <number>` and `GATS_LIVE_ENABLED=false` |

The kill switch is a file on purpose: stopping new entries must not depend
on any software working.

## When GATS switches itself off

It creates `data\KILL`, records why in `live_breaches`, and alerts you. The
three causes:

- **`cap`**: an order would have broken a cap. The order was not sent.
  Something upstream (sizing, the risk limits) let through what the caps
  forbid: find out what before resuming.
- **`unknown_order`**: a request to place an order got no answer. The order
  may or may not exist at the broker; GATS does not guess and does not send
  it again. Open the Upstox order book, look for the tag `gats-<run>-<n>`,
  and cancel or keep it by hand.
- **`reconcile`**: the fills GATS stored do not add up to the positions the
  broker reports. Trust the broker. Flatten or adopt the difference by hand.

To resume: understand the cause, make the broker's book and yours agree,
delete `data\KILL`, and start `gats live` again.

## Known limits of the pilot design

- GATS keeps its simulated book as the decision maker and mirrors each
  order to the broker. If a real order fills differently from the
  simulation (a partial fill, no fill), the books drift, reconciliation
  catches it, and trading stops. It does not try to repair the difference.
- Exits are sent as limit orders at the protection band, like entries. In a
  fast fall a limit exit may not fill; the broker's own intraday square-off
  is the backstop for intraday positions.
- The order API follows Upstox's documentation as read on 2026-10-04. It
  has never been called: the first real order is also its first test. Use
  the broker's sandbox first if your app has access to it.

## Daily checks after the close

- The Telegram summary matches the Upstox contract note (quantities,
  prices, charges). Differences in charges go into
  `configs/costs/india_equity.yaml` with their source.
- `gats paper status --name pilot1`: orders, fills, latencies.
- No open intraday position at the broker.
