"""Map filings to event types with a versioned, rule-based taxonomy.

The rules live in ``configs/event_taxonomy.yaml``: exchange categories carry
most of the signal, and free-text regexes are confined to the generic
buckets ("General Updates", "Company Update / General") so a category that
already says what a filing is cannot be overridden by a stray word. Results
are stored per taxonomy version, so a revised taxonomy is a new column of
truth, not an overwrite, and studies record which version they used.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection, and_, func, select

from gats.db import repo
from gats.db.schema import announcement_event_types, announcements

OTHER = "OTHER"
NOISE = "AGM_NOISE"
# Exchange container categories: typed only when no content rule matched.
CONTAINERS = frozenset({"BOARD_OUTCOME", "PRESS_RELEASE"})
_BATCH = 5000


def event_type_of(members: Sequence[tuple[str, int | None]]) -> str:
    """One type for an event from its filings' ``(event_type, rule_no)``.

    The same disclosure on two exchanges is often described better on one
    of them (BSE's subject vs NSE's "General Updates"), so the event takes
    the most informative member: a specific type beats a container, which
    beats noise, which beats OTHER; between two specific types the earlier
    (more authoritative, category-based) rule wins.
    """
    if not members:
        return OTHER

    def rank(member: tuple[str, int | None]) -> tuple[int, int]:
        event_type, rule = member
        tier = 0 if event_type == OTHER else 1 if event_type == NOISE else 2
        if event_type in CONTAINERS:
            tier = 2
        elif tier == 2:
            tier = 3
        return tier, -(rule if rule is not None else 10**6)

    return max(members, key=rank)[0]


class Clause(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str | None = None
    category: list[str] | None = None
    subcategory: list[str] | None = None
    text: str | None = None
    not_text: str | None = None

    @field_validator("text", "not_text")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:  # not a ValueError, so pydantic would not wrap it
                raise ValueError(f"invalid regex {value!r}: {exc}") from exc
        return value


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    when: list[Clause] = Field(min_length=1)
    generic_only: bool = False


class TaxonomyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    generic: list[Clause]
    rules: list[Rule]


@dataclass(frozen=True)
class _Compiled:
    source: str | None
    category: frozenset[str] | None
    subcategory: frozenset[str] | None
    text: re.Pattern[str] | None
    not_text: re.Pattern[str] | None

    @classmethod
    def of(cls, clause: Clause) -> _Compiled:
        return cls(
            source=clause.source.upper() if clause.source else None,
            category=frozenset(c.lower() for c in clause.category) if clause.category else None,
            subcategory=(
                frozenset(c.lower() for c in clause.subcategory) if clause.subcategory else None
            ),
            text=re.compile(clause.text, re.IGNORECASE) if clause.text else None,
            not_text=re.compile(clause.not_text, re.IGNORECASE) if clause.not_text else None,
        )

    def matches(self, f: Filing) -> bool:
        if self.source and f.source != self.source:
            return False
        if self.category is not None and f.category not in self.category:
            return False
        if self.subcategory is not None and f.subcategory not in self.subcategory:
            return False
        if self.text and not self.text.search(f.text):
            return False
        return not (self.not_text and self.not_text.search(f.text))


_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def attachment_words(url: str | None, symbol: str | None) -> str:
    """Words from an NSE attachment file name.

    NSE keeps the uploader's file name (``EMCURE_02102026213119_EPLReg30Intimation
    Order.pdf``, ``..._Botswana acquisition.pdf``), which often names what a
    "General Updates" filing is about; BSE renames uploads to GUIDs. Part of
    the filing row at dissemination, so using it is not look-ahead.
    """
    if not url:
        return ""
    name = url.rsplit("/", 1)[-1]
    name = re.sub(r"\.(pdf|zip|xml|html?|xlsx?|docx?)$", "", name, flags=re.IGNORECASE)
    if symbol:
        name = re.sub(re.escape(symbol), " ", name, flags=re.IGNORECASE)
    name = re.sub(r"\d{6,}", " ", name)  # timestamps and dates
    name = _CAMEL.sub(" ", name)
    return " ".join(re.sub(r"[_\-.%]+", " ", name).split())


@dataclass(frozen=True)
class Filing:
    """The fields the taxonomy looks at, normalised."""

    source: str
    category: str
    subcategory: str
    text: str

    @classmethod
    def of(
        cls,
        source: str,
        category: str | None,
        subcategory: str | None,
        subject: str | None,
        details: str | None,
        company_name: str | None,
        attachment_url: str | None = None,
        symbol: str | None = None,
    ) -> Filing:
        subject = subject or ""
        if source == "BSE":
            # "<Company> - <scrip code> - <topic>": keep the topic.
            parts = subject.split(" - ")
            subject = " - ".join(parts[2:]) if len(parts) >= 3 else subject
        name = attachment_words(attachment_url, symbol) if source == "NSE" else ""
        text = " | ".join(x for x in (category, subcategory, subject, details, name) if x)
        if company_name:
            # A company called "CARE Ratings" must not make every filing a rating.
            core = re.sub(r"\b(limited|ltd\.?)$", "", company_name.strip(), flags=re.IGNORECASE)
            if len(core.strip()) > 3:
                text = re.sub(re.escape(core.strip()), " ", text, flags=re.IGNORECASE)
        return cls(
            source=source.upper(),
            category=(category or "").strip().lower(),
            subcategory=(subcategory or "").strip().lower(),
            text=text,
        )


@dataclass
class Taxonomy:
    version: str
    _generic: list[_Compiled]
    _rules: list[tuple[str, bool, list[_Compiled]]]
    types: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Taxonomy:
        """Load and validate. The stored version is the declared one plus a
        hash of the file, so editing a rule can never silently reuse an old
        version's stored types."""
        payload = path.read_bytes()
        raw: Any = yaml.safe_load(payload.decode("utf-8"))
        spec = TaxonomyFile.model_validate(raw)
        rules = [(r.type, r.generic_only, [_Compiled.of(c) for c in r.when]) for r in spec.rules]
        types = list(dict.fromkeys([r.type for r in spec.rules] + [OTHER]))
        # Line endings normalised, so a Windows checkout hashes like Linux.
        digest = hashlib.sha256(payload.replace(b"\r\n", b"\n")).hexdigest()[:8]
        version = f"{spec.version}+{digest}"
        return cls(version, [_Compiled.of(c) for c in spec.generic], rules, types)

    def is_generic(self, filing: Filing) -> bool:
        return any(clause.matches(filing) for clause in self._generic)

    def classify(self, filing: Filing) -> tuple[str, int | None]:
        """``(event_type, rule index)``; ``(OTHER, None)`` when nothing matches."""
        generic = self.is_generic(filing)
        for index, (event_type, generic_only, clauses) in enumerate(self._rules):
            if generic_only and not generic:
                continue
            if any(clause.matches(filing) for clause in clauses):
                return event_type, index
        return OTHER, None


# --- storage -----------------------------------------------------------------------


@dataclass
class ClassifyStats:
    version: str
    classified: int = 0
    by_type: Counter[str] = field(default_factory=Counter)


def classify_pending(
    conn: Connection, taxonomy: Taxonomy, now: datetime, *, max_rows: int = 10**8
) -> ClassifyStats:
    """Type every filing not yet typed under this taxonomy version."""
    stats = ClassifyStats(taxonomy.version)
    done = announcement_event_types
    while stats.classified < max_rows:
        rows = conn.execute(
            select(
                announcements.c.id,
                announcements.c.source,
                announcements.c.category,
                announcements.c.subcategory,
                announcements.c.subject,
                announcements.c.details,
                announcements.c.company_name,
                announcements.c.attachment_url,
                announcements.c.symbol,
            )
            .select_from(
                announcements.outerjoin(
                    done,
                    and_(
                        done.c.announcement_id == announcements.c.id,
                        done.c.taxonomy_version == taxonomy.version,
                    ),
                )
            )
            .where(done.c.announcement_id.is_(None))
            .order_by(announcements.c.id)
            .limit(min(_BATCH, max_rows - stats.classified))
        ).all()
        if not rows:
            break
        out = []
        for row in rows:
            event_type, rule = taxonomy.classify(
                Filing.of(
                    row.source,
                    row.category,
                    row.subcategory,
                    row.subject,
                    row.details,
                    row.company_name,
                    row.attachment_url,
                    row.symbol,
                )
            )
            out.append(
                {
                    "announcement_id": row.id,
                    "taxonomy_version": taxonomy.version,
                    "event_type": event_type,
                    "rule_no": rule,
                    "classified_at": now,
                }
            )
            stats.by_type[event_type] += 1
        repo.insert_ignore(conn, done, out, ["announcement_id", "taxonomy_version"])
        stats.classified += len(rows)
    return stats


@dataclass
class TaxonomyCoverage:
    version: str
    total: int
    by_type: dict[str, int]
    unmapped: list[tuple[str, str, str, int]]  # (source, category, subcategory, n)

    @property
    def other_share_excl_noise(self) -> float:
        """The acceptance metric: OTHER as a share of all non-noise filings."""
        non_noise = self.total - self.by_type.get(NOISE, 0)
        return self.by_type.get(OTHER, 0) / non_noise if non_noise else 0.0


def coverage(
    conn: Connection, version: str, since: datetime | None = None, top: int = 25
) -> TaxonomyCoverage:
    t = announcement_event_types
    base = announcements.join(t, t.c.announcement_id == announcements.c.id)
    where: list[Any] = [t.c.taxonomy_version == version]
    if since is not None:
        where.append(announcements.c.event_ts >= since)
    by_type = {
        str(event_type): int(n)
        for event_type, n in conn.execute(
            select(t.c.event_type, func.count())
            .select_from(base)
            .where(*where)
            .group_by(t.c.event_type)
        )
    }
    unmapped = [
        (str(s), str(c), str(sc), int(n))
        for s, c, sc, n in conn.execute(
            select(
                announcements.c.source,
                announcements.c.category,
                announcements.c.subcategory,
                func.count(),
            )
            .select_from(base)
            .where(*where, t.c.event_type == OTHER)
            .group_by(announcements.c.source, announcements.c.category, announcements.c.subcategory)
            .order_by(func.count().desc())
            .limit(top)
        )
    ]
    return TaxonomyCoverage(version, sum(by_type.values()), by_type, unmapped)


def sample_texts(rows: Sequence[Any], taxonomy: Taxonomy) -> list[tuple[str, str]]:
    """``(event_type, text)`` pairs for eyeballing a rule change."""
    return [
        (
            taxonomy.classify(
                Filing.of(r.source, r.category, r.subcategory, r.subject, r.details, r.company_name)
            )[0],
            Filing.of(
                r.source, r.category, r.subcategory, r.subject, r.details, r.company_name
            ).text,
        )
        for r in rows
    ]


def event_level_coverage(conn: Connection, version: str) -> TaxonomyCoverage:
    """Coverage after typing each cross-exchange event from its best member."""
    from gats.db.schema import announcement_event_group as g

    t = announcement_event_types
    groups: dict[int, list[tuple[str, int | None]]] = {}
    for ann_id, group_id, event_type, rule in conn.execute(
        select(t.c.announcement_id, g.c.event_group_id, t.c.event_type, t.c.rule_no)
        .select_from(t.outerjoin(g, g.c.announcement_id == t.c.announcement_id))
        .where(t.c.taxonomy_version == version)
    ):
        key = group_id if group_id is not None else -int(ann_id)
        groups.setdefault(key, []).append((str(event_type), rule))
    by_type: Counter[str] = Counter()
    for members in groups.values():
        by_type[event_type_of(members)] += len(members)  # filings, as in the metric
    return TaxonomyCoverage(version, sum(by_type.values()), dict(by_type), [])
