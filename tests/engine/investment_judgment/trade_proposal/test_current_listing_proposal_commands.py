"""The real proposal CLI uses only the pinned local JP section (synthetic)."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from tests.helpers.proposal import (
    PREPARED,
    PROPOSAL_ID,
    PUBLISHED,
    authored_defer_fixture,
    backend_case,
    bind_review,
    read_saved_proposal,
    write_jp_proposal_listing,
)

from baibai_engine.cli.investment_judgment import main
from baibai_engine.machine_analysis.observations.stocks import listing_master_file
from baibai_engine.machine_analysis.observations.stocks.listing_master import ListingSlice
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    CurrentListingMarket,
    NasdaqDirectoryVersion,
    encode_current_listing_master,
)


@pytest.mark.parametrize(
    "unavailable", ["omitted", "missing", "malformed", "stale", "future", "us_only", "empty"]
)
def test_context_keeps_quote_but_never_recovers_a_legacy_classification(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], unavailable: str
) -> None:
    case = backend_case(tmp_path)  # Deliberately contains the old 12340 TSE row too.
    private = "private-synthetic-listing-data"
    if unavailable == "missing":
        case.listing_path.unlink()
    elif unavailable == "malformed":
        case.listing_path.write_text(f'{{"JP":"{private}"}}', encoding="utf-8")
    elif unavailable == "stale":
        write_jp_proposal_listing(case.listing_path, session=PREPARED.date() - timedelta(days=1))
    elif unavailable == "future":
        write_jp_proposal_listing(
            case.listing_path, received_at=PREPARED + timedelta(microseconds=1)
        )
    elif unavailable == "us_only":
        current = CurrentListingMarket(
            ListingSlice("US", PREPARED.date(), ()),
            "nasdaq-trader",
            PREPARED,
            1,
            tuple(
                NasdaqDirectoryVersion(file, "1014202616:00", "f" * 64)
                for file in ("nasdaqlisted.txt", "otherlisted.txt")
            ),
        )
        case.listing_path.write_bytes(encode_current_listing_master({"US": current}))
    elif unavailable == "empty":
        write_jp_proposal_listing(case.listing_path, tickers=())
    assert (
        main(
            [
                "trade-proposal-context",
                "--proposal-id",
                PROPOSAL_ID,
                "--operation-id",
                case.operation.operation_id,
                "--db",
                str(case.app),
                "--sqlite-path",
                str(case.market),
                *(case.listing_args if unavailable != "omitted" else []),
            ],
            now=PREPARED,
        )
        == 0
    )
    result = capsys.readouterr()
    inputs = json.loads(result.out)["inputs"]
    assert inputs["security_classification"] == []
    assert Decimal(inputs["quotes"][0]["price_yen"]) == Decimal(1100)
    assert private not in result.err + result.out
    assert str(case.listing_path) not in result.err + result.out
    assert ("classification unconfirmed" in result.err) == (unavailable in {"missing", "malformed"})


def test_each_command_pins_once_and_saved_retry_never_decodes_current_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    case = backend_case(tmp_path)
    source = write_jp_proposal_listing(case.listing_path, market_segment="Standard")
    flags = ["--db", str(case.app), "--sqlite-path", str(case.market), *case.listing_args]
    seen = []
    load = listing_master_file.read_local_listing_master

    def read_once(path: Path):
        seen.append(path)
        return load(path)

    monkeypatch.setattr(listing_master_file, "read_local_listing_master", read_once)
    assert (
        main(
            [
                "trade-proposal-context",
                "--proposal-id",
                PROPOSAL_ID,
                "--operation-id",
                case.operation.operation_id,
                *flags,
            ],
            now=PREPARED,
        )
        == 0
    )
    assert (
        json.loads(capsys.readouterr().out)["inputs"]["security_classification"][0][
            "market_segment"
        ]
        == "Standard"
    )
    draft = authored_defer_fixture(
        operation_id=case.operation.operation_id,
        proposal_id=PROPOSAL_ID,
        db_path=case.app,
        sqlite_path=case.market,
        now=PREPARED,
        listing_reader=lambda: source,
    )
    proposal = bind_review(
        draft, {case.pair.assessment_id: case.pair.document}, reviewed_at=PREPARED.isoformat()
    )
    path = tmp_path / "synthetic-proposal.yaml"
    path.write_text(
        yaml.safe_dump(proposal.model_dump(mode="json"), allow_unicode=True), encoding="utf-8"
    )
    check = ["trade-proposal-check", str(path), *flags]
    publish = ["trade-proposal-publish", str(path), *flags]
    write_jp_proposal_listing(case.listing_path, market_segment="Growth")
    assert main(check, now=PUBLISHED) == 4
    assert "proposal input facts changed" in capsys.readouterr().err
    assert main(publish, now=PUBLISHED) == 4
    assert "proposal input facts changed" in capsys.readouterr().err
    case.listing_path.write_bytes(encode_current_listing_master({"JP": source}))
    assert main(check, now=PUBLISHED) == 0
    assert yaml.safe_load(capsys.readouterr().out)["review_binding"] == "match"
    assert main(publish, now=PUBLISHED) == 0
    assert yaml.safe_load(capsys.readouterr().out)["proposal_id"] == PROPOSAL_ID
    before = read_saved_proposal(proposal_id=PROPOSAL_ID, db_path=case.app)
    write_jp_proposal_listing(case.listing_path, market_segment="Growth")
    assert (
        main(["trade-proposal-preview", "--proposal-id", PROPOSAL_ID, *flags], now=PUBLISHED) == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert "input_facts_changed:security_classification" in {
        reason["code"] for reason in preview["change_reasons"]
    }
    assert preview["current_inputs"]["security_classification"][0]["market_segment"] == "Growth"
    assert before == read_saved_proposal(proposal_id=PROPOSAL_ID, db_path=case.app)
    case.listing_path.unlink()
    assert main(publish, now=PUBLISHED) == 0
    result = capsys.readouterr()
    assert yaml.safe_load(result.out)["proposal_id"] == PROPOSAL_ID
    assert result.err == ""
    assert seen == [case.listing_path] * 6
