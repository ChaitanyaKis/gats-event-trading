"""The strategies a runtime can load by the name in their YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from gats.strategy.base import Strategy
from gats.strategy.order_win import OrderWinDrift

KNOWN: dict[str, type[Strategy[Any]]] = {OrderWinDrift.name: OrderWinDrift}


def load_strategy(path: Path) -> Strategy[Any]:
    """The strategy a YAML file describes; ValueError for an unknown name."""
    name = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("strategy")
    if name not in KNOWN:
        raise ValueError(f"{path}: unknown strategy {name!r} (known: {', '.join(sorted(KNOWN))})")
    return KNOWN[name].from_yaml(path)
