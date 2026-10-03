# Labels (T4.5)

Human-verified facts for measuring the extractors (T4.6). Guide version
**label-guide-v1**; each label records the version it followed.

| File | What |
|---|---|
| `order_win_v1.sample.json` | The filings to label, drawn once, before labelling: seeded, stratified by how the rules read each filing, from NSE filings of 2026-06-01 → 2026-08-31 (held out: the rules were built on 2026-09 filings). |
| `order_win_v1.jsonl` | One line per decision, appended as you go; the latest line for a document wins. |

## How to label

```powershell
cd C:\Projects\GATS
.venv\Scripts\gats label review     # resumable; type q to stop, run again to continue
.venv\Scripts\gats label stats      # progress
```

Each filing shows its summary, the disclosure annexure as the rules read
it, every amount in the text with its context, and **one proposal**: the
rules' or the LLM's, chosen at random. Proposals are often wrong. Check
every field against the text ('t' shows the full text) before accepting.

| Key | Use when |
|---|---|
| `a` accept | It is a new order and **every** field is right. |
| `e` edit | Anything is wrong or missing, or it is an amendment. You go field by field; Enter keeps the shown value. |
| `n` not-order | Not an order win: declared lowest bidder only, or other (see kinds); fields are not asked. |
| `s` skip | You cannot tell (unreadable text, missing pages). Use rarely. |
| `t` text | Show the full text, then decide. |
| `q` quit | Stop; nothing is lost. |

## Field rules

- **kind**: `new_order` (an order, contract, work order, LoA/LoI or purchase
  order the company has received); `amendment` (changes an existing
  order's value or scope; give the revised total, if stated, as the amount);
  `lowest_bidder` (declared L1 or lowest bidder, order not yet received);
  `other` (anything else: an order the company *placed*, a cancellation, a
  press release about something else).
- **amount_text**: the total value of the order(s) this filing announces,
  copied as written with currency and unit (`Rs. 72.77 crores`,
  `USD 12.5 million`). Use the headline figure as stated, with or without
  GST; do not adjust for tax.
  - Several orders with a stated total: the total. No total stated: type the
    sum (`Rs. 123.45 crore`) and note `summed`.
  - A range (`Rs. 10-17 crores`): the lower bound (`Rs. 10 crore`); note
    `range`.
  - Amount only in words: type it as figures (`Rs. 217.56 crore`).
  - Not disclosed: `-`. Never the company's turnover, the EMD, a bank
    guarantee or a stamp.
- **counterparty**: who placed the order, as written, without `M/s`.
  Unnamed ("a leading PSU", "a global client"): `-`.
- **domestic_or_export**: `export` if the customer is outside India (even if
  paid in rupees), `domestic` if in India, `unknown` if the filing does not
  say and you cannot tell.
- **duration_months**: the execution period. Type `18`, `2 years` or an end
  date (`31.03.2027`, converted from the filing date). Not stated: `-`.
- **is_repeat_order**: `yes` only if the filing says repeat or follow-on
  order; `no` only if it says new; otherwise `-`.
- **note**: anything unusual, in a few words.
