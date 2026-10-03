"""Pre-registered study configs, and the guard that makes them binding.

A study's parameters live in ``configs/studies/*.yaml`` and its design in
``docs/research/*_prereg.md``. The pre-registration records the config's
SHA-256; :func:`verify_registration` refuses to run on any other version of
the file, so "just one more tweak" after seeing results cannot happen
silently. It becomes a new config, a new pre-registration and a new trial.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class RegistrationError(RuntimeError):
    """The config was not pre-registered in this exact form."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DataSpec(_Strict):
    source: str
    start: date
    end: date
    train_end: date
    test_start: date


class EventSpec(_Strict):
    confirmatory: list[str]
    exploratory: list[str]
    exclude_categories: list[str] = Field(default_factory=list)
    minute_precision_delay_s: int = 60
    same_type_cooldown_sessions: int = 5


class EntrySpec(_Strict):
    rule: str
    price: str


class ExitSpec(_Strict):
    name: str
    sessions_after_entry: int = Field(ge=0)


class BenchmarkSpec(_Strict):
    index: str
    model: str


class FilterSpec(_Strict):
    series: list[str]
    min_median_turnover_rs: float
    turnover_lookback_sessions: int
    min_prev_close_rs: float
    skip_locked_entry_day: bool
    max_entry_gap: float
    exclude_review_actions: bool


class CostSpec(_Strict):
    round_trip: float = Field(ge=0)


class FdrSpec(_Strict):
    method: str
    q: float
    family: str


class StatSpec(_Strict):
    cluster: str
    bootstrap_resamples: int
    bootstrap_seed: int
    fdr: FdrSpec
    ci: str
    min_test_events: int


class StudyConfig(_Strict):
    study: str
    taxonomy_version: str
    data: DataSpec
    events: EventSpec
    entry: EntrySpec
    exits: list[ExitSpec]
    benchmark: BenchmarkSpec
    filters: FilterSpec
    costs: CostSpec
    statistics: StatSpec

    @property
    def event_types(self) -> list[str]:
        return [*self.events.confirmatory, *self.events.exploratory]


def config_hash(path: Path) -> str:
    """SHA-256 of the file with LF line endings (same on Windows and Linux)."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_study(path: Path) -> tuple[StudyConfig, str]:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    return StudyConfig.model_validate(raw), config_hash(path)


def registered_hash(prereg: Path) -> str:
    """The config hash written in a pre-registration document."""
    found = re.search(r"SHA-256[^`]*`([0-9a-f]{64})`", prereg.read_text(encoding="utf-8"))
    if found is None:
        raise RegistrationError(f"{prereg} records no config SHA-256")
    return found.group(1)


def verify_registration(config: Path, prereg: Path) -> tuple[StudyConfig, str]:
    """Load the study only if the config is exactly the registered one."""
    study, actual = load_study(config)
    expected = registered_hash(prereg)
    if actual != expected:
        raise RegistrationError(
            f"{config} (sha256 {actual[:12]}...) is not the pre-registered config "
            f"({expected[:12]}...). Results from a changed design are a new study: "
            "write a new pre-registration and log a new trial."
        )
    return study, actual
