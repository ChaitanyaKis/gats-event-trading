"""The labelled evaluation set (T4.5): sample, review step, labels file, CLI."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import Engine
from typer.testing import CliRunner

from gats.cli import app
from gats.config import Settings
from gats.db.engine import init_db, make_engine
from gats.extract.labels import (
    GUIDE_VERSION,
    Proposal,
    ReviewItem,
    Sample,
    SampleItem,
    allocate,
    amounts_in_context,
    append_label,
    draw_sample,
    load_labels,
    resolve,
    review_entry,
    review_one,
    summarize,
)
from gats.extract.rules import extract_order_win
from gats.ingest import Services
from gats.rawstore import RawStore
from tests.conftest import TAXONOMY_TEST, seed_filings
from tests.test_llm_cascade import chat, chat_url

NOW = datetime(2026, 10, 3, tzinfo=UTC)
JULY = datetime(2026, 7, 14, 5, 0, tzinfo=UTC)  # 10:30 IST
SEPTEMBER = datetime(2026, 9, 14, 5, 0, tzinfo=UTC)
ANNEXURE = (
    "1. Name of the entity awarding the order(s)/contract(s) NTPC Limited 7. Broad "
    "consideration or size of the order(s)/contract(s) Rs. {n} crore 8. Whether the "
    "promoter group has any interest No"
)
SENTENCE = "The Company has received an order worth Rs. {n} crore from NTPC Limited."
NOTHING = "Kindly take the intimation on record. {n}"


def draw(engine: Engine, n: int, seed: int = 1, source: str | None = None) -> Sample:
    with engine.begin() as conn:
        return draw_sample(
            conn,
            name="t",
            event_type="ORDER_WIN",
            taxonomy_version=TAXONOMY_TEST,
            start=date(2026, 6, 1),
            end=date(2026, 8, 31),
            source=source,
            n=n,
            seed=seed,
            now=NOW,
        )


class TestSample:
    def test_allocation_is_proportional_and_exact(self) -> None:
        assert allocate({"annexure": 70, "sentence": 16, "none": 14}, 50) == {
            "annexure": 35,
            "sentence": 8,
            "none": 7,
        }
        assert allocate({"a": 1, "b": 1, "c": 1}, 2) == {"a": 1, "b": 1, "c": 0}  # ties by name
        assert allocate({"a": 2, "b": 3}, 10) == {"a": 2, "b": 3}

    def test_held_out_one_per_document_stratified_reproducible(
        self, engine: Engine, store: RawStore, tmp_path: Path
    ) -> None:
        july = [{"text": ANNEXURE.format(n=10 + i), "event_ts": JULY} for i in range(6)]
        july += [{"text": SENTENCE.format(n=20 + i), "event_ts": JULY} for i in range(3)]
        july += [{"text": NOTHING.format(n=i), "event_ts": JULY} for i in range(2)]
        seed_filings(engine, store, july)
        seed_filings(
            engine,
            store,
            [
                # the same PDF on BSE: one document, one item
                {"text": ANNEXURE.format(n=10), "event_ts": JULY, "source": "BSE", "pdf": b"pdf 0"},
                # September is the development window: out
                {"text": ANNEXURE.format(n=99), "event_ts": SEPTEMBER},
                # no text: out
                {"text": "", "event_ts": JULY, "needs_ocr": True},
            ],
        )
        sample = draw(engine, n=6)
        assert sample.population == 11
        assert sample.strata == {
            "annexure": {"population": 6, "sample": 3},
            "none": {"population": 2, "sample": 1},
            "sentence": {"population": 3, "sample": 2},
        }
        assert len({i.doc_id for i in sample.items}) == 6
        assert all(i.filed == date(2026, 7, 14) and i.source == "NSE" for i in sample.items)
        assert sorted(i.shown for i in sample.items) == ["llm"] * 3 + ["rules"] * 3
        assert draw(engine, n=6).items == sample.items  # the seed fixes the draw

        path = tmp_path / "labels" / "t.sample.json"
        sample.save(path)
        assert Sample.load(path) == sample
        assert b"\r\n" not in path.read_bytes()

    def test_only_order_wins(self, engine: Engine) -> None:
        with pytest.raises(ValueError, match="ORDER_WIN"), engine.begin() as conn:
            draw_sample(
                conn,
                name="t",
                event_type="DIVIDEND",
                taxonomy_version=TAXONOMY_TEST,
                start=date(2026, 6, 1),
                end=date(2026, 8, 31),
                source=None,
                n=1,
                seed=1,
                now=NOW,
            )


def test_labels_file_latest_line_wins(tmp_path: Path) -> None:
    path = tmp_path / "labels" / "x.jsonl"
    append_label(path, {"doc_id": "a", "status": "skipped"})
    append_label(path, {"doc_id": "b", "status": "labelled"})
    append_label(path, {"doc_id": "a", "status": "labelled", "note": "₹ fine"})
    labels = load_labels(path)
    assert labels["a"] == {"doc_id": "a", "status": "labelled", "note": "₹ fine"}
    assert set(labels) == {"a", "b"}
    assert b"\r\n" not in path.read_bytes()
    assert load_labels(tmp_path / "missing.jsonl") == {}


def test_amounts_in_context() -> None:
    found = amounts_in_context("Turnover Rs. 900 crore. The order is worth Rs. 42.5 crore here.")
    assert [amount for _, amount, _ in found] == ["Rs. 900 crore.", "Rs. 42.5 crore"]
    assert found[1][0].endswith("worth ") and found[1][2] == " here."


# --- the review step ----------------------------------------------------------------


class ScriptedUI:
    """Answers prompts from a script; Enter (an empty answer) takes the default."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.shown: list[str] = []
        self.paged: list[str] = []

    def show(self, text: str = "", *, highlight: bool = False) -> None:
        self.shown.append(text)

    def ask(self, prompt: str, *, default: str | None = None) -> str:
        answer = self.answers.pop(0)
        return default if answer == "" and default is not None else answer

    def page(self, text: str) -> None:
        self.paged.append(text)


FILED = date(2026, 7, 14)
ITEM = SampleItem("d" * 64, "NSE", "7", "NTPCX", FILED, "sentence", "rules")
TEXT = SENTENCE.format(n=42.5)


def entry(order_text: str | None = TEXT) -> ReviewItem:
    order = extract_order_win(order_text, filed=FILED).order if order_text else None
    return ReviewItem(ITEM, TEXT, "Company 7 | order", Proposal(order, "rules", "sentence", "v"))


def clock() -> datetime:
    return NOW


class TestReview:
    def test_accept_after_reading_the_text(self) -> None:
        ui = ScriptedUI("t", "a", "")
        record = review_one(ui, entry(), "1/9", clock)
        assert record is not None and ui.paged == [TEXT]
        assert (record["decision"], record["status"], record["kind"]) == (
            "accept",
            "labelled",
            "new_order",
        )
        assert record["amount_inr"] == pytest.approx(425_000_000)
        assert record["fields"]["amount_text"] == "Rs. 42.5 crore"
        assert record["shown"] == "rules" and record["proposal"] == record["fields"]
        assert record["guide"] == GUIDE_VERSION and record["labelled_at"] == NOW.isoformat()
        assert any("[Rs. 42.5 crore]" in line for line in ui.shown)  # amounts in context

    def test_edit_field_by_field(self) -> None:
        ui = ScriptedUI(
            "e",  # decision
            "",  # kind: new_order (default)
            "about ninety",  # not an amount: asked again
            "Rs. 99 lakh",
            "",  # counterparty: keep the proposal's
            "d",  # domestic
            "31.03.2027",  # an end date, counted from the filing date
            "-",  # repeat: not stated
            "checked",
        )
        record = review_one(ui, entry(), "1/9", clock)
        assert record is not None and record["decision"] == "edit"
        assert any("not an amount" in line for line in ui.shown)
        fields = record["fields"]
        assert record["amount_inr"] == pytest.approx(9_900_000)
        assert fields["counterparty"] == entry().proposal.order.counterparty  # type: ignore[union-attr]
        assert fields["domestic_or_export"] == "domestic"
        assert fields["duration_months"] == 8.6  # 260 days / 30.4
        assert fields["is_repeat_order"] is None and record["note"] == "checked"

    def test_not_an_order(self) -> None:
        record = review_one(ScriptedUI("n", "l", "L1 only"), entry(), "1/9", clock)
        assert record is not None
        assert (record["status"], record["kind"], record["fields"]) == (
            "labelled",
            "lowest_bidder",
            None,
        )

    def test_skip_and_quit(self) -> None:
        skipped = review_one(ScriptedUI("s", "scanned"), entry(), "1/9", clock)
        assert skipped is not None and (skipped["status"], skipped["kind"]) == ("skipped", None)
        assert review_one(ScriptedUI("q"), entry(), "1/9", clock) is None

    def test_nothing_to_accept_without_a_proposal(self) -> None:
        ui = ScriptedUI("a", "s", "")
        record = review_one(ui, entry(order_text=None), "1/9", clock)
        assert any("choose one of" in line for line in ui.shown)
        assert record is not None and record["decision"] == "skip"


def test_summary_counts_acceptance_by_proposer() -> None:
    items = [replace(ITEM, doc_id=str(i), shown=("rules", "llm")[i % 2]) for i in range(4)]
    sample = Sample("t", "ORDER_WIN", "v", FILED, FILED, None, 1, 4, {}, "r", NOW, items)
    labels: dict[str, Any] = {
        "0": {"status": "labelled", "decision": "accept", "kind": "new_order", "shown": "rules"},
        "1": {"status": "labelled", "decision": "edit", "kind": "new_order", "shown": "llm"},
        "2": {"status": "skipped", "decision": "skip", "kind": None, "shown": "rules"},
        "elsewhere": {"status": "labelled", "decision": "accept", "kind": "x", "shown": "llm"},
    }
    lines = [line.strip() for line in summarize(sample, labels)]
    assert lines[0] == "2 labelled, 1 skipped, 1 to go (target 300)"
    assert "rules proposals accepted as shown: 1/1 (100%)" in lines
    assert "llm proposals accepted as shown: 0/1 (0%)" in lines


class TestEntries:
    @respx.mock
    async def test_each_item_gets_its_assigned_proposal(self, svc: Services) -> None:
        respx.post(chat_url(svc)).mock(return_value=httpx.Response(200, json=chat()))
        (ann,) = seed_filings(
            svc.engine, svc.store, [{"text": TEXT, "event_ts": JULY, "details": "Order from X"}]
        )
        sample = replace(draw(svc.engine, n=1), items=[])
        with svc.engine.begin() as conn:
            doc = draw(svc.engine, n=1).items[0].doc_id
            item = replace(ITEM, doc_id=doc, source_ann_id="0")
            assert resolve(conn, replace(sample, items=[item])) == {doc: ann}
        ruled = await review_entry(svc, item, ann)
        assert ruled is not None and ruled.proposal.by == "rules"
        assert ruled.proposal.note == "sentence" and "Order from X" in ruled.summary
        asked = await review_entry(svc, replace(item, shown="llm"), ann)
        assert asked is not None and asked.proposal.by == "llm" and asked.proposal.note == "ok"
        assert await review_entry(svc, replace(item, shown="llm"), ann, llm_ok=False) is None

    @respx.mock
    async def test_no_llm_answer_leaves_the_item_pending(self, svc: Services) -> None:
        respx.post(chat_url(svc)).mock(side_effect=httpx.ConnectError("refused"))
        (ann,) = seed_filings(svc.engine, svc.store, [{"text": TEXT, "event_ts": JULY}])
        doc = draw(svc.engine, n=1).items[0].doc_id
        item = replace(ITEM, doc_id=doc, source_ann_id="0", shown="llm")
        assert await review_entry(svc, item, ann) is None


# --- the commands -------------------------------------------------------------------


runner = CliRunner()


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)  # no stray .env; labels/ is created here
    monkeypatch.setenv("GATS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def test_sample_is_never_silently_redrawn(workdir: Path) -> None:
    path = workdir / "labels" / "order_win_v1.sample.json"
    path.parent.mkdir()
    path.write_text("{}", encoding="utf-8")
    result = runner.invoke(app, ["label", "sample", "--start", "2026-06-01", "--end", "2026-08-31"])
    assert result.exit_code == 1 and "exists" in result.output
    assert path.read_text(encoding="utf-8") == "{}"


def test_review_saves_each_decision_and_resumes(workdir: Path) -> None:
    settings = Settings(_env_file=None, data_dir=workdir / "data")  # type: ignore[call-arg]
    settings.ensure_dirs()
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    seed_filings(
        engine,
        RawStore(settings.raw_dir),
        [{"text": SENTENCE.format(n=10 + i), "event_ts": JULY} for i in range(3)],
    )
    sample = draw(engine, n=3)
    engine.dispose()
    sample = replace(sample, items=[replace(i, shown="rules") for i in sample.items])
    sample.save(workdir / "labels" / "order_win_v1.sample.json")

    first = runner.invoke(app, ["label", "review"], input="a\n\nq\n")
    assert first.exit_code == 0, first.output
    assert "1 labelled, 0 skipped, 2 to go" in first.output
    second = runner.invoke(app, ["label", "review"], input="s\nunclear\na\n\n")
    assert second.exit_code == 0, second.output
    assert "1 done, 2 to go" in second.output  # resumed after the first decision
    assert "2 labelled, 1 skipped, 0 to go" in second.output
    lines = (workdir / "labels" / "order_win_v1.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["decision"] for line in lines] == ["accept", "skip", "accept"]
    stats = runner.invoke(app, ["label", "stats"])
    assert "rules proposals accepted as shown: 2/2 (100%)" in stats.output
