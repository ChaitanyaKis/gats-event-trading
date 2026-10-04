"""The gate in front of live trading (T8.2).

``gats live`` starts only if a human approved exactly this design under
exactly these caps, after the G3 paper report said PASS:

- The approval is a database row that only ``gats gate approve`` writes,
  and that command needs an interactive terminal and a typed sentence that
  repeats the design and the capital at risk. A script, a pipe or an agent
  cannot produce it by accident.
- It is bound to the design hash, the caps hash and the hash of the G3
  report: change the strategy, raise a cap or regenerate the report, and
  the approval no longer applies.
- It expires, and it can be revoked.

:func:`problems` is the single place that decides. There is no flag,
environment variable or argument that skips it, and none may be added.
"""

from __future__ import annotations

import getpass
import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Row, select

from gats.db.schema import live_approvals
from gats.oms.caps import LiveCaps

_VERDICT = re.compile(r"^\*\*G3: (PASS|FAIL)\*\*", re.MULTILINE)


class ApprovalRefused(RuntimeError):
    """The approval was not created."""


def report_digest(report: Path) -> str:
    return hashlib.sha256(report.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def g3_verdict(report: Path) -> str | None:
    """PASS or FAIL as the G3 report states it (``**G3: PASS**`` on a line
    of its own), or None if there is no report or no verdict in it."""
    if not report.exists():
        return None
    found = _VERDICT.search(report.read_text(encoding="utf-8"))
    return None if found is None else found.group(1)


def confirmation(design_hash: str, caps: LiveCaps) -> str:
    """The sentence a person must type, word for word."""
    return (
        f"I approve live trading of design {design_hash} with at most "
        f"Rs {caps.max_capital_rs:,.0f} at risk"
    )


def _current(conn: Connection, design_hash: str, caps: LiveCaps) -> Row[Any] | None:
    return conn.execute(
        select(live_approvals)
        .where(
            live_approvals.c.design_hash == design_hash,
            live_approvals.c.caps_hash == caps.digest,
        )
        .order_by(live_approvals.c.id.desc())
        .limit(1)
    ).first()


def problems(
    conn: Connection, *, design_hash: str, caps: LiveCaps, report: Path, now: datetime
) -> list[str]:
    """Every reason live trading may not start; empty means it may."""
    found = []
    unset = caps.unset()
    if unset:
        found.append(f"the caps are not set ({', '.join(unset)} = 0 in the live config)")
    verdict = g3_verdict(report)
    if verdict is None:
        found.append(f"no G3 verdict: {report} is missing or states none")
    elif verdict != "PASS":
        found.append(f"gate G3 did not pass ({report} says {verdict})")
    approval = _current(conn, design_hash, caps)
    if approval is None:
        found.append(
            f"no approval for design {design_hash} with these caps: a person runs "
            "`gats gate approve` in a terminal"
        )
        return found
    if approval.revoked_at is not None:
        found.append(f"approval #{approval.id} was revoked: {approval.revoked_reason}")
    if now >= approval.expires_at:
        found.append(f"approval #{approval.id} expired on {approval.expires_at:%Y-%m-%d}")
    if verdict is not None and report_digest(report) != approval.g3_report_sha256:
        found.append(f"the G3 report changed after approval #{approval.id} was given")
    return found


def approval_id(conn: Connection, design_hash: str, caps: LiveCaps) -> int | None:
    row = _current(conn, design_hash, caps)
    return None if row is None else int(row.id)


def approve(
    conn: Connection,
    *,
    design_hash: str,
    caps: LiveCaps,
    report: Path,
    typed: str,
    now: datetime,
    valid_days: int,
) -> int:
    """Record the approval. Called only by ``gats gate approve`` once a
    person at a terminal has typed the confirmation."""
    unset = caps.unset()
    if unset:
        raise ApprovalRefused(f"the caps are not set: {', '.join(unset)}")
    verdict = g3_verdict(report)
    if verdict != "PASS":
        raise ApprovalRefused(f"gate G3 has not passed ({report}: {verdict or 'no verdict'})")
    if typed.strip() != confirmation(design_hash, caps):
        raise ApprovalRefused("the confirmation was not typed exactly as shown")
    key = conn.execute(
        live_approvals.insert().values(
            design_hash=design_hash,
            caps_hash=caps.digest,
            caps=caps.model_dump(mode="json"),
            g3_report_sha256=report_digest(report),
            confirmation=typed.strip(),
            created_by=getpass.getuser(),
            created_at=now,
            expires_at=now + timedelta(days=valid_days),
        )
    ).inserted_primary_key
    assert key is not None
    return int(key[0])


def revoke(conn: Connection, approval: int, reason: str, now: datetime) -> bool:
    """Withdraw an approval; True if it existed and was still in force."""
    done = conn.execute(
        live_approvals.update()
        .where(live_approvals.c.id == approval, live_approvals.c.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
    )
    return bool(done.rowcount)
