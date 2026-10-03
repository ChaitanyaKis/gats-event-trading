"""Event taxonomy (T3.1), on real filing wordings."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select

from gats.db import repo
from gats.db.schema import announcement_event_types
from gats.ingest import Services
from gats.rawstore import RawStore
from gats.recorder import ClassifyJob
from gats.research.taxonomy import (
    OTHER,
    Filing,
    Taxonomy,
    attachment_words,
    classify_pending,
    coverage,
    event_type_of,
)
from gats.sources.models import AnnouncementRecord

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "event_taxonomy.yaml"
NOW = datetime(2026, 10, 3, tzinfo=UTC)


@pytest.fixture(scope="module")
def tax() -> Taxonomy:
    return Taxonomy.load(CONFIG)


def nse(category: str, details: str, name: str = "Acme Limited", url: str | None = None) -> Filing:
    return Filing.of("NSE", category, None, category, details, name, url, "ACME")


def bse(category: str, sub: str, topic: str, details: str = "", name: str = "Acme Ltd") -> Filing:
    return Filing.of("BSE", category, sub, f"Acme Ltd - 500001 - {topic}", details, name)


@pytest.mark.parametrize(
    ("filing", "expected"),
    [
        (
            nse(
                "Bagging/Receiving of orders/contracts",
                "Acme Limited has informed the Exchange about "
                "Bagging/Receiving of orders/contracts",
            ),
            "ORDER_WIN",
        ),
        (
            bse("Company Update", "General", "POWERGRID Declared As Successful Bidder Under TBCB"),
            "ORDER_WIN",
        ),
        (
            bse(
                "Company Update",
                "General",
                "Limited Emerges As The Lowest Bidder (L1) From East Coast Railway",
            ),
            "ORDER_WIN",
        ),
        # A tax order is not an order win.
        (
            bse(
                "Company Update",
                "General",
                "Assessment Order From Income Tax Department",
                "Material Assessment Order received from Income tax Department",
            ),
            "PENALTY_LITIGATION",
        ),
        (bse("Result", "Financial Results", "Unaudited Financial Results"), "RESULTS"),
        (
            nse(
                "Outcome of Board Meeting",
                "Acme Limited has submitted to the Exchange, the financial results "
                "for the period ended Jun 30, 2026.",
            ),
            "RESULTS",
        ),
        # The intimation that a board WILL consider results is not the results.
        (
            bse(
                "Board Meeting",
                "Board Meeting",
                "Board Meeting Intimation for Approval Of Unaudited Financial "
                "Results For The Quarter",
            ),
            "AGM_NOISE",
        ),
        (
            bse(
                "Company Update",
                "Credit Rating",
                "Announcement under Regulation 30 (LODR)-Credit Rating",
                "Upgrade in Credit Rating of the Company.",
            ),
            "RATING_UP",
        ),
        (
            nse(
                "Credit Rating- Revision",
                "Acme Limited has informed the Exchange that the rating was downgraded",
            ),
            "RATING_DOWN",
        ),
        (
            nse("Credit Rating", "Acme Limited has informed the Exchange about Credit Rating"),
            "RATING_OTHER",
        ),
        # An AGM notice that mentions the dividend is still an AGM notice.
        (
            nse("Shareholders meeting", "Notice of AGM; final dividend of Rs 5 recommended"),
            "AGM_NOISE",
        ),
        (
            bse("Insider Trading / SAST", "Closure of Trading Window", "Closure of Trading Window"),
            "AGM_NOISE",
        ),
        (
            bse(
                "Insider Trading / SAST",
                "Disclosures under Reg. 31(1) and 31(2) of SEBI (SAST) Regulations, 2011",
                "Disclosures",
            ),
            "PLEDGE_CHANGE",
        ),
        (
            bse(
                "Insider Trading / SAST",
                "Disclosures under Reg. 29(2) of SEBI (SAST) Regulations, 2011",
                "Disclosures",
            ),
            "HOLDING_CHANGE",
        ),
        (
            bse(
                "Corp. Action",
                "Sub-division / Stock Split",
                "Corporate Action-Intimation of Sub division / Stock Split",
            ),
            "BONUS_SPLIT",
        ),
        (
            bse("Company Update", "General", "Commencement Of The Commercial Production"),
            "BUSINESS_UPDATE",
        ),
        (
            nse(
                "Press Release",
                "Acme Limited has informed the Exchange regarding a press release "
                "dated September 15, 2026, which is self explanatory..",
            ),
            "PRESS_RELEASE",
        ),
        (
            nse("General Updates", "Acme Limited has informed the Exchange about General Updates"),
            OTHER,
        ),
    ],
)
def test_real_wordings(tax: Taxonomy, filing: Filing, expected: str) -> None:
    assert tax.classify(filing)[0] == expected


def test_company_name_cannot_trigger_a_rule(tax: Taxonomy) -> None:
    # "CARE Ratings Limited" filing an AGM-related general update is not a rating event.
    filing = nse(
        "General Updates",
        "CARE Ratings Limited has informed the Exchange about General Updates",
        name="CARE Ratings Limited",
    )
    assert tax.classify(filing)[0] == OTHER


def test_attachment_name_types_a_general_update(tax: Taxonomy) -> None:
    url = "https://nsearchives.nseindia.com/corporate/ACME_29092026181112_SE_intimation_Botswana_acquisition.pdf"
    assert attachment_words(url, "ACME") == "SE intimation Botswana acquisition"
    filing = nse(
        "General Updates", "Acme Limited has informed the Exchange about General Updates", url=url
    )
    assert tax.classify(filing)[0] == "ACQUISITION"
    assert attachment_words(
        "https://x/EMCURE_02102026213119_EPLReg30IntimationOrder.pdf", "EMCURE"
    ) == ("EPL Reg30Intimation Order")


def test_text_rules_do_not_override_a_specific_category(tax: Taxonomy) -> None:
    # A trading-window filing that mentions "results" stays a trading window.
    filing = bse(
        "Insider Trading / SAST",
        "Closure of Trading Window",
        "Closure of Trading Window",
        "till 48 hours after the financial results",
    )
    assert tax.classify(filing)[0] == "AGM_NOISE"


def test_event_type_of_picks_the_most_informative_member() -> None:
    assert event_type_of([("OTHER", None), ("ORDER_WIN", 19)]) == "ORDER_WIN"
    assert event_type_of([("OTHER", None), ("AGM_NOISE", 18)]) == "AGM_NOISE"
    assert event_type_of([("PRESS_RELEASE", 40), ("ACQUISITION", 25)]) == "ACQUISITION"
    assert event_type_of([("AGM_NOISE", 18), ("PRESS_RELEASE", 40)]) == "PRESS_RELEASE"
    # Two specific types: the category rule (lower index) beats a text rule.
    assert event_type_of([("PENALTY_LITIGATION", 27), ("ORDER_WIN", 1)]) == "ORDER_WIN"
    assert event_type_of([]) == OTHER


def test_version_carries_a_hash_of_the_file(tmp_path: Path) -> None:
    original = Taxonomy.load(CONFIG)
    assert (
        original.version.startswith("taxonomy-v1+")
        and len(original.version) == len("taxonomy-v1+") + 8
    )
    edited = tmp_path / "t.yaml"
    edited.write_text(
        CONFIG.read_text(encoding="utf-8").replace("'upgrad'", "'upgrade'", 1), encoding="utf-8"
    )
    assert Taxonomy.load(edited).version != original.version


def test_invalid_configs_fail_loudly(tmp_path: Path) -> None:
    bad_regex = tmp_path / "a.yaml"
    bad_regex.write_text(
        "version: x\ngeneric: []\nrules:\n  - type: A\n    when:\n      - {text: '(unclosed'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        Taxonomy.load(bad_regex)
    unknown_key = tmp_path / "b.yaml"
    unknown_key.write_text(
        "version: x\ngeneric: []\nrules:\n  - type: A\n    when:\n      - {colour: red}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        Taxonomy.load(unknown_key)


def _store(engine: Engine, store: RawStore) -> None:
    records = [
        AnnouncementRecord(
            source="NSE",
            source_ann_id=str(i),
            symbol="ACME",
            scrip_code=None,
            isin=None,
            company_name="Acme Limited",
            category=cat,
            subcategory=None,
            subject=cat,
            details=details,
            attachment_url=None,
            exch_submitted_ts=None,
            exch_disseminated_ts=NOW,
            event_ts=NOW,
        )
        for i, (cat, details) in enumerate(
            [
                ("Bagging/Receiving of orders/contracts", "order"),
                ("Trading Window", "closure"),
                ("General Updates", "General Updates"),
                ("General Updates", "General Updates"),
            ]
        )
    ]
    with engine.begin() as conn:
        doc = repo.save_raw(
            conn, store, b"x", kind="t", source="NSE", url="u", content_type=None, fetched_at=NOW
        )
        repo.insert_announcements(
            conn,
            records,
            raw_doc_id=doc,
            parser_version="v",
            mode="backfill",
            fetched_at=NOW,
            now=NOW,
        )


def test_classify_pending_is_idempotent_and_per_version(
    engine: Engine, store: RawStore, tax: Taxonomy, tmp_path: Path
) -> None:
    _store(engine, store)
    with engine.begin() as conn:
        first = classify_pending(conn, tax, NOW)
        again = classify_pending(conn, tax, NOW)
        cov = coverage(conn, tax.version)
    assert (first.classified, again.classified) == (4, 0)
    assert cov.by_type == {"ORDER_WIN": 1, "AGM_NOISE": 1, OTHER: 2}
    assert cov.other_share_excl_noise == pytest.approx(2 / 3)
    # A changed taxonomy is a new version: everything is typed again, alongside.
    edited = tmp_path / "t.yaml"
    edited.write_text(CONFIG.read_text(encoding="utf-8") + "\n# edit\n", encoding="utf-8")
    with engine.begin() as conn:
        assert classify_pending(conn, Taxonomy.load(edited), NOW).classified == 4
        versions = set(conn.execute(select(announcement_event_types.c.taxonomy_version)).scalars())
    assert len(versions) == 2


async def test_classify_job(svc: Services, tmp_path: Path) -> None:
    path = tmp_path / "t.yaml"
    path.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    job = ClassifyJob("classify", 120, path)
    assert (await job.run_once(svc)).ok
    path.unlink()
    assert not (await job.run_once(svc)).ok  # missing file is an error, not a crash
