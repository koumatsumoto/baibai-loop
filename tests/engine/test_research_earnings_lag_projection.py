import yaml
from tests.helpers.research_triage import (
    published_review_set,
    research_entry,
    research_triage_payload,
)

from baibai_engine.research.triage.cli import scaffold_research_triage
from baibai_engine.research.triage.contracts import StoredResearchTriage
from baibai_engine.research.workspace.admission import _review_set_entries_from_triage
from baibai_engine.screening.run_store.read import ReviewSetPublication


def test_workspace_input_interprets_legacy_announced_without_rewriting_triage() -> None:
    entry = research_entry()
    entry["candidate_snapshot"]["analysis"]["context"]["next_earnings_status"] = "announced"
    triage = StoredResearchTriage.from_payload(research_triage_payload(entries=[entry]))

    projected = _review_set_entries_from_triage(triage)

    assert projected[0]["analysis"]["context"]["next_earnings_status"] == ("scheduled_date_reached")
    assert (
        triage.publication_payload()["entries"][0]["candidate_snapshot"]["analysis"]["context"][
            "next_earnings_status"
        ]
        == "announced"
    )


def test_manual_triage_draft_interprets_legacy_status_without_changing_publication(
    tmp_path, mocker, capsys
) -> None:
    payload = published_review_set(tickers=("2331",))
    payload["entries"][0]["analysis"]["context"]["next_earnings_status"] = "announced"
    publication = ReviewSetPublication(
        review_set_id=payload["review_set_id"],
        run_revision_id=payload["run_revision_id"],
        as_of_date=payload["as_of"],
        created_at=payload["created_at"],
        payload=payload,
    )
    reader = mocker.patch("baibai_engine.research.triage.cli.ScreeningRunReader")
    reader.return_value.get_review_set.return_value = publication
    mocker.patch("baibai_engine.research.triage.cli.latest_assessment_payload", return_value=None)
    mocker.patch("baibai_engine.research.triage.cli.latest_research_triage_id", return_value=None)

    output = tmp_path / "draft.yaml"
    assert scaffold_research_triage(publication.review_set_id, output_path=output) == 0
    capsys.readouterr()

    draft = yaml.safe_load(output.read_text())
    assert (
        draft["entries"][0]["candidate_snapshot"]["analysis"]["context"]["next_earnings_status"]
        == "scheduled_date_reached"
    )
    assert payload["entries"][0]["analysis"]["context"]["next_earnings_status"] == ("announced")
