"""The offline paper session over the mock exchange (T7.1) stays runnable.

Loopback only: the mock exchange is a real HTTP server on 127.0.0.1 and the
whole session runs on a simulated clock. Nothing leaves the machine.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "paper_smoke.py"


def test_a_full_paper_session_runs_over_the_mock_exchange(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("paper_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    where = tmp_path / "smoke"
    assert smoke.main(["--data-dir", str(where), "--quiet"]) == 0  # every check held
    assert (where / "gats.db").exists() and not (where / "data" / "KILL").exists()
    assert smoke.main(["--data-dir", str(where), "--quiet"]) == 2  # never reuses a directory
