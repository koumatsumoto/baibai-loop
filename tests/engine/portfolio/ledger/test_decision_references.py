from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.helpers.manual_facts import NOW, _binding_count, _service

from baibai_engine.portfolio.ledger.decision_references import (
    DecisionReferenceBinding,
    resolve_buy_reference,
)
from baibai_engine.portfolio.ledger.drafts import (
    LedgerDraft,
    apply_draft,
    build_broker_fact_draft,
    load_draft,
    write_draft,
)
from baibai_engine.portfolio.ledger.read import LedgerConflictError
from baibai_engine.portfolio.ledger.store import LedgerStoreService

REFERENCE = "manual-fact-00000000-0000-4000-8000-000000000001"


def _draft(service: LedgerStoreService) -> LedgerDraft:
    draft, _ = build_broker_fact_draft(
        service,
        lambda _reference: "1234",
        decision_reference=REFERENCE,
        status="open",
        ticker="1234",
        quantity=200,
        sector="情報・通信業",
        occurred_at=NOW,
        price_guard_yen=Decimal("1000"),
        expires_at=NOW + timedelta(days=1),
        order_id="synthetic-broker-order",
        now=NOW,
    )
    assert draft is not None
    return LedgerDraft.model_validate(
        {
            **draft.model_dump(mode="json"),
            "binding_addition": {
                "reference": REFERENCE,
                "ticker": "1234",
                "source_id": REFERENCE,
            },
        }
    )


def test_manual_binding_and_ledger_commit_together_after_confirmation(tmp_path: Path) -> None:
    service, path = _service(tmp_path)
    draft = _draft(service)
    with pytest.raises(ValueError, match="human confirmation"):
        apply_draft(service, draft, human_confirmed=False)
    assert service.append_head() == 1
    assert _binding_count(path) == 0
    result = apply_draft(service, draft, human_confirmed=True)
    assert result.append_head == 2
    assert _binding_count(path) == 1
    assert resolve_buy_reference(REFERENCE, db_path=path) == "1234"
    assert service.load().events[-1].decision_reference == REFERENCE


def test_exception_after_ledger_append_rolls_back_binding_and_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, path = _service(tmp_path)
    draft = _draft(service)

    def fail_meta(*_args):
        raise RuntimeError("synthetic failure after append")

    monkeypatch.setattr("baibai_engine.portfolio.ledger.store._replace_meta", fail_meta)
    with pytest.raises(RuntimeError, match="after append"):
        apply_draft(service, draft, human_confirmed=True)
    assert service.load() == draft.source
    assert service.append_head() == 1
    assert _binding_count(path) == 0


def test_conflicting_binding_does_not_append_ledger(tmp_path: Path) -> None:
    service, path = _service(tmp_path)
    draft = _draft(service)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO position_decision_reference VALUES (?, ?, ?, ?, ?, ?, ?)",
            (REFERENCE, "9999", "buy", "retained_reference", None, "old-source", "a" * 64),
        )
    with pytest.raises(LedgerConflictError, match="binding conflicts"):
        apply_draft(service, draft, human_confirmed=True)
    assert service.load() == draft.source
    assert _binding_count(path) == 1


def test_stale_head_is_checked_before_binding_insert(tmp_path: Path) -> None:
    service, path = _service(tmp_path)
    draft = _draft(service)
    stale = draft.model_copy(update={"expected_head": 0})
    with pytest.raises(LedgerConflictError, match="stale"):
        apply_draft(service, stale, human_confirmed=True)
    assert _binding_count(path) == 0
    assert service.append_head() == 1


def test_saved_draft_retains_uuid_and_old_draft_requires_reprepare(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    draft = _draft(service)
    file = tmp_path / "draft.yaml"
    write_draft(file, draft)
    assert load_draft(file) == draft
    apply_draft(service, load_draft(file), human_confirmed=True)
    with pytest.raises(LedgerConflictError, match="stale"):
        apply_draft(service, load_draft(file), human_confirmed=True)
    raw = draft.model_dump(mode="json")
    raw["schema_version"] = 1
    raw.pop("binding_addition")
    with pytest.raises(ValidationError):
        LedgerDraft.model_validate(raw)


def test_matching_reference_cannot_be_saved_without_new_buy_event(tmp_path: Path) -> None:
    service, path = _service(tmp_path)
    draft = _draft(service)
    with pytest.raises(LedgerConflictError, match="new buy event"):
        service.apply_document(
            expected_head=draft.expected_head,
            expected_document=draft.source,
            replacement=draft.source,
            binding_addition=draft.binding_addition,
        )
    assert _binding_count(path) == 0


@pytest.mark.parametrize("altered", [{"ticker": "9999"}, {"side": "sell"}])
def test_every_new_event_for_reference_must_match_binding(tmp_path: Path, altered) -> None:
    service, path = _service(tmp_path)
    draft = _draft(service)
    first = draft.replacement.events[-1]
    second = first.model_copy(
        update={
            "event_id": "synthetic-mismatched-reservation",
            "reservation_id": "synthetic-mismatched-reservation",
            "order_id": "synthetic-mismatched-order",
            **altered,
        }
    )
    replacement = draft.replacement.model_copy(
        update={"events": (*draft.replacement.events, second)}
    )
    with pytest.raises(LedgerConflictError, match="all manual-fact events"):
        service.apply_document(
            expected_head=draft.expected_head,
            expected_document=draft.source,
            replacement=replacement,
            binding_addition=draft.binding_addition,
        )
    assert _binding_count(path) == 0
    assert service.load() == draft.source


def test_reference_namespace_cannot_overwrite_unknown_old_decision() -> None:
    with pytest.raises(ValidationError):
        DecisionReferenceBinding(
            reference="capital-allocation-assessment-unknown",
            ticker="1234",
            source_id="capital-allocation-assessment-unknown",
        )


def test_unknown_reference_is_read_only_and_never_resolves_a_ticker(tmp_path: Path) -> None:
    _service_instance, path = _service(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="unavailable"):
        resolve_buy_reference("unknown-old-reference", db_path=path)
    assert path.read_bytes() == before
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(ValueError, match="unavailable"):
        resolve_buy_reference(REFERENCE, db_path=missing)
    assert not missing.exists()


@pytest.mark.parametrize("version", [True, 2.0, "2"])
def test_draft_version_is_an_exact_integer(tmp_path: Path, version) -> None:
    service, _ = _service(tmp_path)
    raw = _draft(service).model_dump(mode="json")
    raw["schema_version"] = version
    with pytest.raises(ValidationError, match="integer"):
        LedgerDraft.model_validate(raw)


def test_old_saved_draft_requests_reprepare(tmp_path: Path) -> None:
    path = tmp_path / "old.yaml"
    path.write_text("schema_version: 1\n")
    with pytest.raises(ValueError, match="prepare a new draft"):
        load_draft(path)


@pytest.mark.parametrize("absolute", [False, True])
def test_draft_output_path_accepts_explicit_paths_without_overwriting(
    tmp_path: Path, absolute: bool
) -> None:
    from baibai_engine.cli.portfolio import _root_path

    service, _ = _service(tmp_path)
    draft = _draft(service)
    relative = Path("drafts") / "buy.yaml"
    supplied = tmp_path / relative if absolute else relative
    output = _root_path(tmp_path, supplied)
    assert output == tmp_path / relative
    write_draft(output, draft)
    assert load_draft(output) == draft
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        write_draft(output, draft)
    assert output.read_bytes() == before
