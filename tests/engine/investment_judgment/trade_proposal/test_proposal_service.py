from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import yaml
from pydantic import ValidationError
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.operation_proposal import seed_proposal
from tests.helpers.proposal import (
    PREPARED,
    PROPOSAL_ID,
    PUBLISHED,
    backend_case,
    bind_review,
    fixture_review,
    read_saved_proposal,
    reviewed_proposal,
)
from tests.helpers.screening_sqlite import seed_daily_bars

from baibai_engine.cli import investment_judgment as judgment_cli
from baibai_engine.cli.investment_judgment import main
from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal import contracts as proposal_contracts
from baibai_engine.investment_judgment.trade_proposal import service as proposal_service
from baibai_engine.investment_judgment.trade_proposal.conditions import no_action_blocker
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalConflictError,
    ProposalError,
    TradeProposal,
    load_proposal_draft,
    proposal_draft_sha256,
)
from baibai_engine.investment_judgment.trade_proposal.fingerprint import proposal_inputs_fingerprint
from baibai_engine.investment_judgment.trade_proposal.inputs import (
    DEFAULT_BUDGET_MAX_YEN,
    DEFAULT_BUDGET_MIN_YEN,
    build_proposal_context,
    collect_proposal_inputs,
    proposal_market_snapshot,
)
from baibai_engine.investment_judgment.trade_proposal.preview import preview_current_terms
from baibai_engine.investment_judgment.trade_proposal.scaffold import scaffold_proposal
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.investment_judgment.workflow import selection as selection_owner
from baibai_engine.investment_judgment.workflow.operation.progress import progress_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationCompletionError
from baibai_engine.investment_judgment.workflow.selection import selection_sha256
from baibai_engine.portfolio.holdings.market_observations import read_order_session_bounds
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_web.sources.proposal import read_proposal_views


@pytest.fixture
def case(tmp_path):
    return backend_case(tmp_path)


def _counts(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        return {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "investment_proposal",
                "investment_proposal_item",
                "position_decision_reference",
                "ledger_event",
                "ledger_meta",
            )
        }


def _ledger_snapshot(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        return {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("ledger_event", "ledger_meta")
        }


@pytest.mark.parametrize("complete", [False, True])
def test_authoring_context_uses_one_fixed_selection(tmp_path, capsys, mocker, complete):
    case = backend_case(tmp_path, tickers=("1234",) if complete else ("1234", "5678"))
    parse_selection = mocker.spy(selection_owner, "parse_selection")
    rc = main(
        [
            "trade-proposal-context",
            "--operation-id",
            case.operation.operation_id,
            "--proposal-id",
            PROPOSAL_ID,
            "--db",
            str(case.app),
            "--sqlite-path",
            str(case.market),
        ],
        now=PREPARED,
    )
    captured = capsys.readouterr()
    assert not captured.err
    payload = yaml.safe_load(captured.out)
    if complete:
        assert rc == 0
        assert payload["research_scope"]["selection_sha256"] == selection_sha256(case.selected)
        assert payload["research_scope"]["operation_id"] == case.operation.operation_id
    else:
        assert rc == 3
        assert payload == {
            "status": "invalid_source",
            "operation_id": None,
            "reason": "selected targets must be covered by reviewed and unfinished outcomes",
        }
    assert parse_selection.call_count == 1


def test_real_selection_pair_scope_publish_atomic_and_ledger_unchanged(case):
    proposal = reviewed_proposal(case)
    assert proposal.research_scope.selection_sha256 == selection_sha256(case.selected)
    assert proposal.research_scope.operation_id == case.operation.operation_id
    assert proposal.inputs.checked_at < PUBLISHED
    before = _counts(case.app)
    ledger_before = _ledger_snapshot(case.app)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(proposal).valid
    published = service.publish(proposal)
    after = _counts(case.app)
    assert after["ledger_event"] == before["ledger_event"]
    assert after["ledger_meta"] == before["ledger_meta"]
    assert _ledger_snapshot(case.app) == ledger_before
    assert (
        after["investment_proposal"]
        == after["investment_proposal_item"]
        == after["position_decision_reference"]
        == 1
    )
    with closing(sqlite3.connect(case.app)) as connection, connection:
        binding = connection.execute(
            "SELECT ticker,origin,source_digest FROM position_decision_reference"
        ).fetchone()
    assert binding == ("1234", "proposal", published.review.draft_sha256)
    assert read_saved_proposal(proposal_id=published.proposal_id, db_path=case.app) == published
    completed = case.operations.complete(
        case.operation.operation_id,
        proposal_id=published.proposal_id,
        result="合成提案を発行した。",
        next="本人が採否と証券画面の条件を確認する。",
        completed_at=PUBLISHED,
    )
    assert completed.status == "completed"


@pytest.mark.parametrize("publication", ["before_read", "after_read", "changed_reason"])
def test_published_unbound_unfinished_proposal_cannot_be_skipped(case, monkeypatch, publication):
    operation = case.operations.record_unfinished(
        case.operation.operation_id,
        ticker="1234",
        reason="追加資料の確認が未完了",
        references=("synthetic-pending-source",),
        now=PREPARED,
    )
    proposal = reviewed_proposal(case, action="defer")
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    if publication == "after_read":
        from baibai_engine.investment_judgment.workflow.operation import service as operation_owner

        open_writer = operation_owner.connect_existing

        def publish_before_operation_write(path):
            monkeypatch.setattr(operation_owner, "connect_existing", open_writer)
            service.publish(proposal)
            return open_writer(path)

        monkeypatch.setattr(operation_owner, "connect_existing", publish_before_operation_write)
    else:
        service.publish(proposal)
    if publication == "changed_reason":
        operation = case.operations.record_unfinished(
            operation.operation_id,
            ticker="1234",
            reason="公開後に追加の反証を確認し、別の問いが残った",
            references=("synthetic-new-counterevidence",),
            now=PUBLISHED,
        )
    assert progress_binding(operation.payload).proposal_ref is None
    with pytest.raises(OperationCompletionError, match="published Trade Proposal"):
        case.operations.complete(
            operation.operation_id,
            result="評価未完了のまま作業を終了",
            next="不足する資料を確認する",
            completed_at=PUBLISHED,
        )
    assert case.operations.get(operation.operation_id) == operation
    assert read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app).payload() == {
        **proposal.payload(),
        "published_at": PUBLISHED.isoformat(),
    }
    if publication == "changed_reason":
        with pytest.raises(OperationCompletionError, match="fixed coverage"):
            case.operations.complete(
                operation.operation_id,
                proposal_id=proposal.proposal_id,
                result="理由変更",
                next="再確認",
                completed_at=PUBLISHED,
            )
        assert case.operations.get(operation.operation_id) == operation
    else:
        completed = case.operations.complete(
            operation.operation_id,
            proposal_id=proposal.proposal_id,
            result="未完了を明示した提案を保存し作業を終了",
            next="不足する資料を確認する",
            completed_at=PUBLISHED,
        )
        assert completed.status == "completed"
        assert progress_binding(completed.payload).proposal_ref.proposal_id == proposal.proposal_id


def test_reviewed_defer_still_requires_a_bound_published_proposal(tmp_path):
    case = backend_case(tmp_path, valuation_unresolved=True)
    proposal = reviewed_proposal(case, action="defer")
    assert proposal.research_scope.outcomes[0].status == "reviewed"
    assert proposal.items[0].action == "defer"
    notes = {"result": "不確実な評価を保存", "next": "新しい開示を確認", "completed_at": PUBLISHED}
    with pytest.raises(OperationCompletionError, match="published Trade Proposal"):
        case.operations.complete(case.operation.operation_id, **notes)
    published = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(proposal)
    assert (
        case.operations.complete(
            case.operation.operation_id, proposal_id=published.proposal_id, **notes
        ).status
        == "completed"
    )


def test_unfinished_completion_prevents_later_new_proposal_publication(case):
    case.operations.record_unfinished(
        case.operation.operation_id,
        ticker="1234",
        reason="資料待ちで評価未完了",
        references=("synthetic-pending-source",),
        now=PREPARED,
    )
    proposal = reviewed_proposal(case, action="defer")
    completed = case.operations.complete(
        case.operation.operation_id,
        result="今回の作業を終了",
        next="不足する資料を確認する",
        completed_at=PUBLISHED,
    )
    before = _counts(case.app)
    with pytest.raises(ProposalConflictError, match="active Research Operation"):
        TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
            proposal
        )
    assert _counts(case.app) == before
    assert case.operations.get(completed.operation_id) == completed


def test_cli_yaml_output_failure_preserves_successful_publication(
    case, tmp_path, monkeypatch, capsys
):
    proposal = reviewed_proposal(case)
    draft = tmp_path / "proposal.yaml"
    draft.write_text(yaml.safe_dump(proposal.payload()))

    def fail_output(*_args, **_kwargs):
        raise yaml.YAMLError("synthetic publication output failure")

    monkeypatch.setattr("baibai_engine.cli.investment_judgment.yaml.safe_dump", fail_output)
    with pytest.raises(yaml.YAMLError, match="synthetic publication output failure"):
        main(
            [
                "trade-proposal-publish",
                str(draft),
                "--db",
                str(case.app),
                "--sqlite-path",
                str(case.market),
            ],
            now=PUBLISHED,
        )
    assert capsys.readouterr().out == ""
    stored = read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app)
    assert stored.model_dump(mode="json", exclude={"published_at"}) == proposal.payload()
    assert stored.published_at == PUBLISHED


def test_context_and_work_file_keep_one_app_market_and_reviewed_pair_snapshot(
    case, tmp_path, monkeypatch, capsys
):
    from baibai_engine.investment_judgment.trade_proposal import inputs, scaffold

    draft = tmp_path / "work.yaml"
    counts = Counter()

    def count(owner):
        original = getattr(inputs, owner)

        def observed(*args, **kwargs):
            counts[owner] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(inputs, owner, observed)

    for owner in ("connect_read_only", "connect_market_read_only", "require_assessment_ref"):
        count(owner)
    project = scaffold.scaffold_proposal

    def intervening_correction(context):
        with closing(sqlite3.connect(case.market)) as market, market:
            market.execute("UPDATE jquants_daily_bars SET close=900 WHERE ticker='1234'")
        return project(context)

    monkeypatch.setattr(scaffold, "scaffold_proposal", intervening_correction)
    before = case.app.read_bytes()
    assert (
        main(
            [
                "trade-proposal-context",
                "--operation-id",
                case.operation.operation_id,
                "--proposal-id",
                PROPOSAL_ID,
                "--db",
                str(case.app),
                "--sqlite-path",
                str(case.market),
                "--out",
                str(draft),
            ],
            now=PREPARED,
        )
        == 0
    )
    context = yaml.safe_load(capsys.readouterr().out)
    work = yaml.safe_load(draft.read_text())
    assert counts == dict.fromkeys(
        ("connect_read_only", "connect_market_read_only", "require_assessment_ref"), 1
    )
    assert context["inputs"] == work["inputs"]
    assert float(work["inputs"]["quotes"][0]["price_yen"]) == 1100
    with closing(sqlite3.connect(case.market)) as market:
        assert market.execute(
            "SELECT close FROM jquants_daily_bars WHERE ticker='1234'"
        ).fetchone() == (900,)
    assert case.app.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "seed"),
    [
        ("headline", "提案の要点を記述してください。"),
        ("forgone", "採らなかった行動とその理由を記述してください。"),
        (
            "comparison",
            "docs/reference/trade-proposal.md#limit-selectionに従い、指値と数量、残cash、no-addで増量できない代償を、現金維持で逃す反発・回避できる損失と比べる。 選んだ行動と、それを変える次の価格または企業事実を記す。",
        ),
        (
            "rationale",
            "観測価格で割安と判断したかと、同じ資金で今買う・指値で待つ・別案や現金を選ぶかを分ける。価格だけを待つ場合と、不足があり調査を保留する場合を区別する。 モデル観測・一次資料によって企業の利益・普通株に帰属する資金の見通し、価格条件、または次に確かめる事実の何が変わったかを記す。変わらない場合も区別する。",
        ),
        ("review_conditions", "買付では未約定・部分約定・新情報の後に何を再確認するかを記す。"),
        ("resume_conditions", "不足を確認して行動と理由を記述し、独立検証を行う。"),
        (
            "review_conditions",
            (
                "前回の指値・対象日を今回へ流用せず、元提案全体への本人報告があっても約定・現保有や"
                "今後の一律見送りと決めず、企業の根拠・観測価格・取引条件のどれが今回の"
                "「買う・待つ・見送る」を変えたか（変化なしも含む）を記す。"
            ),
        ),
        ("blocker_message", "行動・具体条件・比較理由の執筆と独立検証が未了です。"),
        ("reviewer_identity", "独立Reviewerのidentityを記述してください。"),
    ],
)
def test_known_scaffold_instruction_is_not_authored_even_with_a_matching_review(case, field, seed):
    raw = reviewed_proposal(
        case, action="defer" if field in {"resume_conditions", "blocker_message"} else "buy"
    ).payload()
    if field == "reviewer_identity":
        raw["review"][field] = seed
    elif field in {"review_conditions", "resume_conditions"}:
        raw["items"][0][field] = [seed]
    elif field == "blocker_message":
        raw["items"][0]["blockers"] = [{"code": "primary_source_pending", "message": seed}]
    elif field == "rationale":
        raw["items"][0][field] = seed
    else:
        raw[field] = seed
    bound = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    assert proposal_draft_sha256(bound) == bound.review.draft_sha256
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    before = case.app.read_bytes()
    for require_review in (False, True):
        with pytest.raises(ProposalError, match="reserved authoring"):
            service.check(bound, require_review=require_review)
    with pytest.raises(ProposalError, match="reserved authoring"):
        service.publish(bound)
    assert case.app.read_bytes() == before


def test_author_can_retain_a_source_caution_without_using_a_scaffold_as_the_decision(case):
    raw = reviewed_proposal(case).payload()
    # This observation used to be conditionally supplied by the scaffold. Citing
    # a source caution is legitimate; it is not an instruction to write a reason.
    source = (
        "EDINETの保存一覧に起点SFICT001の合成届出を確認。株式移転と対価の支払期日は別に照合した。"
        "結果報告書の存在は成立や新規取得者への入金の証明ではない。"
    )
    raw["items"][0]["review_conditions"] = [source]
    bound = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(bound).valid
    assert service.publish(bound).items[0].review_conditions == (source,)


@pytest.mark.parametrize("legacy_publication_time", [False, True])
def test_one_context_authored_unsigned_check_later_review_publish_and_exact_retry(
    case, tmp_path, capsys, monkeypatch, legacy_publication_time
):
    draft = tmp_path / "proposal.yaml"
    common = ["--db", str(case.app), "--sqlite-path", str(case.market)]
    assert (
        main(
            [
                "trade-proposal-context",
                "--operation-id",
                case.operation.operation_id,
                "--proposal-id",
                PROPOSAL_ID,
                "--out",
                str(draft),
                *common,
            ],
            now=PREPARED,
        )
        == 0
    )
    context = yaml.safe_load(capsys.readouterr().out)
    raw = yaml.safe_load(draft.read_text())
    assert "published_at" not in raw
    assert raw["review"] is None
    for field in ("proposal_id", "research_scope", "inputs", "liquidity_context"):
        assert raw[field] == context[field]
    untouched = case.app.read_bytes()
    for command in ("trade-proposal-check", "trade-proposal-publish"):
        assert main([command, str(draft), *common], now=PREPARED) == 3
        capsys.readouterr()
    raw["items"][0]["action"] = "no_action"
    draft.write_text(yaml.safe_dump(raw))
    assert main(["trade-proposal-check", str(draft), *common], now=PREPARED) == 3
    capsys.readouterr()
    raw.update(headline="合成見送り", comparison="現金維持との比較", forgone="買付を見送る")
    raw["items"][0].update(
        rationale="観測終値では利益に対して割高なため現金を維持する。",
        review_conditions=["次回の決算で反復利益を確認する。"],
    )
    if legacy_publication_time:
        raw["published_at"] = PREPARED.isoformat()
    draft.write_text(yaml.safe_dump(raw))
    assert main(["trade-proposal-check", str(draft), *common], now=PREPARED) == 0
    checked = yaml.safe_load(capsys.readouterr().out)
    assert checked["review_binding"] == "unreviewed"
    assert checked["valid"]
    assert checked["check_scope"] == "authored_draft_precheck"
    for unsigned in (raw, {key: value for key, value in raw.items() if key != "review"}):
        with pytest.raises(ValidationError, match="review"):
            TradeProposal.model_validate({**unsigned, "published_at": PUBLISHED.isoformat()})
    assert main(["trade-proposal-publish", str(draft), *common], now=PREPARED) == 4
    assert "review is required" in capsys.readouterr().err
    assert case.app.read_bytes() == untouched

    reviewed_at = PREPARED + timedelta(minutes=5)
    raw["review"] = fixture_review(checked["draft_sha256"], reviewed_at=reviewed_at.isoformat())
    draft.write_text(yaml.safe_dump(raw))
    draft_bytes = draft.read_bytes()
    assert main(["trade-proposal-check", str(draft), *common], now=PUBLISHED) == 0
    checked_again = yaml.safe_load(capsys.readouterr().out)
    assert checked_again["valid"] is True
    assert checked_again["review_binding"] == "match"
    assert checked_again["draft_sha256"] == checked["draft_sha256"]

    parsed_types = []

    def parse(payload):
        parsed_types.append(type(payload))
        return load_proposal_draft(payload)

    monkeypatch.setattr(proposal_contracts, "load_proposal_draft", parse)
    monkeypatch.setattr(proposal_service, "load_proposal_draft", parse)
    published_at = PUBLISHED + timedelta(minutes=1)
    assert main(["trade-proposal-publish", str(draft), *common], now=published_at) == 0
    assert parsed_types == [dict]
    published = TradeProposal.model_validate(yaml.safe_load(capsys.readouterr().out))
    assert published.prepared_at == PREPARED
    assert published.review.reviewed_at == reviewed_at
    assert published.published_at == published_at
    assert proposal_draft_sha256(published) == checked["draft_sha256"]
    assert draft.read_bytes() == draft_bytes

    stored_bytes = case.app.read_bytes()
    # Exact retries are historical reads, even when the caller's clock predates Review.
    assert main(["trade-proposal-publish", str(draft), *common], now=PREPARED) == 0
    assert TradeProposal.model_validate(yaml.safe_load(capsys.readouterr().out)) == published
    assert case.app.read_bytes() == stored_bytes
    assert draft.read_bytes() == draft_bytes


def test_future_review_is_rejected_even_with_a_later_caller_publication_time(case):
    raw = reviewed_proposal(case).payload()
    raw["review"]["reviewed_at"] = (PUBLISHED + timedelta(minutes=1)).isoformat()
    raw["published_at"] = (PUBLISHED + timedelta(minutes=2)).isoformat()
    proposal = load_proposal_draft(raw)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    before = case.app.read_bytes()
    validation = service.check(proposal)
    assert not validation.valid
    assert "proposal_time_after_publish" in {reason.code for reason in validation.blockers}
    with pytest.raises(ProposalError, match="現在発行時刻"):
        service.publish(proposal)
    assert case.app.read_bytes() == before


def test_service_boundaries_reject_an_invalid_nested_typed_draft(case):
    proposal = reviewed_proposal(case)
    invalid = proposal.model_copy(
        update={"review": proposal.review.model_copy(update={"open_findings": ("unresolved",)})}
    )
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    before = case.app.read_bytes()
    with pytest.raises(ValidationError, match="cannot carry open findings"):
        service.check(invalid)
    with pytest.raises(ValidationError, match="cannot carry open findings"):
        service.publish(invalid)
    # A loose mapping may contain model_copy'd children; it is not authored YAML.
    with pytest.raises(TypeError, match="requires a TradeProposalDraft"):
        service.check(dict(invalid))
    with pytest.raises(TypeError, match="requires authored YAML or a TradeProposalDraft"):
        service.publish(dict(invalid))
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("via_cli", [False, True])
def test_new_publication_uses_jst_date_after_writer_acquisition(
    case, tmp_path, monkeypatch, capsys, via_cli
):
    proposal = reviewed_proposal(case)
    instant = PUBLISHED.replace(hour=23, minute=59, second=59)
    after_midnight = instant + timedelta(seconds=2)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: instant)
    assert service.check(proposal).valid
    before = case.app.read_bytes()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz)

    connect = proposal_service.connect_existing

    def connect_crossing_midnight(path):
        connection = connect(path)

        def trace(statement):
            nonlocal instant
            if statement == "BEGIN IMMEDIATE":
                instant = after_midnight

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(proposal_service, "connect_existing", connect_crossing_midnight)
    if via_cli:
        monkeypatch.setattr(proposal_service, "datetime", Clock)
        monkeypatch.setattr(judgment_cli, "datetime", Clock)
        path = tmp_path / "proposal.yaml"
        path.write_text(yaml.safe_dump(proposal.payload()))
        assert (
            main(
                [
                    "trade-proposal-publish",
                    str(path),
                    "--db",
                    str(case.app),
                    "--sqlite-path",
                    str(case.market),
                ]
            )
            == 3
        )
        assert "基準日は本日" in capsys.readouterr().out
    else:
        with pytest.raises(ProposalError, match="基準日は本日"):
            service.publish(proposal)
    assert instant == after_midnight
    assert case.app.read_bytes() == before
    assert _counts(case.app)["investment_proposal"] == 0


def test_cli_context_check_and_preview_keep_limit_arithmetic_separate_from_the_close(
    case, tmp_path, capsys
):
    common = ["--db", str(case.app), "--sqlite-path", str(case.market)]
    assert (
        main(
            [
                "trade-proposal-context",
                "--operation-id",
                case.operation.operation_id,
                "--proposal-id",
                PROPOSAL_ID,
                *common,
            ],
            now=PREPARED,
        )
        == 0
    )
    context = yaml.safe_load(capsys.readouterr().out)["calculations"]["1234"]
    calculations = [context["buy_at_actual_quote"], context["calculation_at_maximum_price"]]
    proposal = reviewed_proposal(case)
    draft = tmp_path / "proposal.yaml"
    draft.write_text(yaml.safe_dump(proposal.payload()))
    assert main(["trade-proposal-check", str(draft), *common], now=PUBLISHED) == 0
    checked = yaml.safe_load(capsys.readouterr().out)
    assert checked["valid"] is True
    assert checked["draft_sha256"] == proposal.review.draft_sha256
    calculations.append(checked["calculations"]["1234"])
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    saved = service.publish(proposal)
    before = case.app.read_bytes()
    assert (
        main(
            ["trade-proposal-preview", "--proposal-id", proposal.proposal_id, *common],
            now=PUBLISHED,
        )
        == 0
    )
    row = yaml.safe_load(capsys.readouterr().out)["items"][0]
    calculations.extend((row["current_quote_calculation"], row["saved_limit_revalidation"]))
    for calculation, limit in zip(
        calculations, ("1100.0", "1000", "1000", "1100", "1000"), strict=True
    ):
        assert calculation["current_price_projection"]["price_yen"] == 1100
        ceiling = calculation["limit_price_projection"]
        assert ceiling["price_context"] == "buy_limit_ceiling"
        assert Decimal(ceiling["calculation_price_yen"]) == Decimal(limit)
        assert Decimal(ceiling["observed_quote"]["price_yen"]) == 1100
        assert ceiling["observed_quote"]["price_as_of"] == PREPARED.date().isoformat()
        assert ceiling["observed_quote"]["price_basis"] == "last_close_unadjusted"
        assert not {"price_yen", "price_as_of", "price_basis"} & ceiling.keys()
    assert case.app.read_bytes() == before
    assert read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == saved


def test_place_limit_publishes_and_previews_separate_quote_and_ceiling(case, mocker):
    from baibai_engine.investment_judgment.stock_assessment import store as assessment_store

    raw = reviewed_proposal(case).payload()
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=875")
    raw["inputs"]["quotes"][0]["price_yen"] = "875"
    raw["forgone"] = "観測875円を保持し、支払上限880円で指値を提案する。"
    item = raw["items"][0]
    item["timing"] = "place_limit"
    item["terms"].update(limit_price_yen="880", notional_yen="264000")
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    ledger_before = _ledger_snapshot(case.app)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(proposal).valid
    published = service.publish(proposal)
    assert read_saved_proposal(proposal_id=published.proposal_id, db_path=case.app) == published
    assert published.inputs.quotes[0].price_yen == Decimal(875)
    assert published.items[0].terms.notional_yen == Decimal(264000)
    exact_pair = mocker.spy(assessment_store, "load_reviewed_assessment")
    preview = preview_current_terms(
        proposal_id=published.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=5),
    )
    exact_pair.assert_called_once()
    assert exact_pair.call_args.args[1] == case.pair.assessment_id
    assert preview.status == "unchanged"
    row = preview.items[0]
    assert Decimal(row["current_quote_calculation"]["calculation_price_yen"]) == Decimal(875)
    saved_limit = row["saved_limit_revalidation"]
    assert saved_limit["hard_blockers"] == []
    assert saved_limit["quantity"] == 300
    assert saved_limit["current_price_projection"]["price_yen"] == 875
    ceiling = saved_limit["limit_price_projection"]
    assert ceiling["price_context"] == "buy_limit_ceiling"
    assert ceiling["calculation_price_yen"] == "880"
    assert Decimal(ceiling["observed_quote"]["price_yen"]) == 875
    assert not {"price_yen", "price_as_of", "price_basis"} & ceiling.keys()
    assert saved_limit["limit_price_projection"]["base"]["total_return_pct"] == 25
    assert _ledger_snapshot(case.app) == ledger_before
    changed = published.payload()
    changed["items"][0]["terms"].update(limit_price_yen="885", notional_yen="265500")
    with pytest.raises(ProposalConflictError, match="content review"):
        service.check(load_proposal_draft(changed))
    assert read_saved_proposal(proposal_id=published.proposal_id, db_path=case.app) == published


def test_same_id_retry_reads_immutable_before_current_clock_latest_or_quote_gates(
    case, monkeypatch
):
    proposal = reviewed_proposal(case)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    published = service.publish(proposal)
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=900")

    def current_inputs_are_not_used(*args, **kwargs):
        pytest.fail("same-ID retry must not sample a clock or revalidate using market data")

    monkeypatch.setattr(proposal_service, "_check_validated_proposal", current_inputs_are_not_used)
    monkeypatch.setattr(proposal_service, "proposal_market_snapshot", current_inputs_are_not_used)
    later = TradeProposalService(
        case.app, sqlite_path=case.market, clock=current_inputs_are_not_used
    )
    assert later.publish(proposal) == published
    raw = proposal.payload()
    raw["headline"] = "同identityの別内容"
    changed = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    with pytest.raises(ProposalConflictError, match="immutable"):
        later.publish(changed)


def test_same_id_retry_requires_the_exact_signed_typed_payload(case):
    proposal = reviewed_proposal(case)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    service.publish(proposal)
    raw = proposal.payload()
    raw["inputs"]["quotes"][0]["price_yen"] = "1100.000"
    with pytest.raises(ProposalConflictError, match="immutable"):
        service.publish(load_proposal_draft(raw))


@pytest.mark.parametrize("change", ["head", "meta", "quote", "source"])
def test_actual_input_fact_change_requires_new_snapshot_and_review(case, change):
    proposal = reviewed_proposal(case)
    if change == "head":
        from baibai_engine.portfolio.ledger.drafts import apply_draft, build_event_draft
        from baibai_engine.portfolio.ledger.events import ContributionEvent
        from baibai_engine.portfolio.ledger.store import LedgerStoreService

        event = ContributionEvent(
            type="contribution",
            event_id="new-confirmed-fact",
            occurred_at=PUBLISHED - timedelta(minutes=1),
            amount_yen=1,
        )
        store = LedgerStoreService(case.app)
        apply_draft(store, build_event_draft(store, event), human_confirmed=True)
    elif change == "meta":
        from baibai_engine.portfolio.ledger.store import LedgerStoreService

        store = LedgerStoreService(case.app)
        ledger, head = store.load_with_head()
        store.apply_document(
            expected_head=head,
            expected_document=ledger,
            replacement=ledger.model_copy(update={"as_of": ledger.as_of + timedelta(minutes=1)}),
        )
    elif change == "quote":
        with closing(sqlite3.connect(case.market)) as connection, connection:
            connection.execute("UPDATE jquants_daily_bars SET close=1099")
    else:
        raw = proposal.payload()
        raw["inputs"]["quotes"][0]["source_ref"] = "other-real-source"
        proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    with pytest.raises(ProposalConflictError, match="input facts changed"):
        service.publish(proposal)
    assert _counts(case.app)["investment_proposal"] == 0


def test_same_instant_utc_and_decimal_equivalent_facts_publish(case):
    proposal = reviewed_proposal(case)
    raw = proposal.payload()
    raw["inputs"]["ledger_as_of"] = "2026-10-14T06:00:00Z"
    raw["inputs"]["quotes"][0]["price_yen"] = "1100.00"
    raw["inputs"]["available_cash_yen"] = "3E+5"
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    published = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(proposal)
    assert published.model_dump(mode="json", exclude={"published_at"}) == proposal.payload()


@pytest.mark.parametrize(
    ("checked_at", "blocker_code", "message"),
    [
        ("2026-10-14T16:19:00+09:00", "proposal_inputs_after_review", "独立Review以前"),
        (
            "2026-10-14T15:20:00+09:00",
            "quote_session_after_inputs",
            "入力収集時点で利用できる取引日以前",
        ),
    ],
)
def test_saved_input_and_quote_times_must_precede_their_reviewed_cutoffs(
    case, checked_at, blocker_code, message
):
    raw = reviewed_proposal(case).payload()
    raw["inputs"]["checked_at"] = checked_at
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    validation = service.check(proposal)
    assert not validation.valid
    assert blocker_code in {reason.code for reason in validation.blockers}
    before = case.app.read_bytes()
    with pytest.raises(ProposalError, match=message):
        service.publish(proposal)
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("checked_at", ["2026-10-14T07:15:00Z", "2026-10-14T06:30:00Z"])
def test_saved_input_review_and_quote_boundaries_accept_the_same_utc_instant(case, checked_at):
    raw = reviewed_proposal(case).payload()
    raw["inputs"]["checked_at"] = checked_at
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED.astimezone(UTC)
    )
    assert proposal.inputs.checked_at <= proposal.review.reviewed_at < PUBLISHED
    assert service.check(proposal).valid
    assert service.publish(proposal).review == proposal.review


def test_review_binds_conditions_cash_scope_and_projection(case):
    proposal = reviewed_proposal(case)
    raw = proposal.payload()
    raw["items"][0]["terms"]["conditions"].append("別の実前提")
    changed = load_proposal_draft(raw)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    stale = service.check(changed, require_review=False)
    assert [b.code for b in stale.blockers] == ["proposal_content_review_stale"]
    assert service.check(changed.model_copy(update={"review": None}), require_review=False).valid
    with pytest.raises(ProposalConflictError, match="content review"):
        service.publish(changed)


@pytest.mark.parametrize("change", ["warning", "exposure", "terms_warning"])
def test_warning_or_exposure_tampering_not_accepted_by_new_review(case, change):
    proposal = reviewed_proposal(case)
    raw = proposal.payload()
    if change == "warning":
        raw["warnings"] = ["made-up-warning"]
    elif change == "exposure":
        raw["portfolio_exposure"]["ticker"]["prospective_pct"] = "99"
    else:
        assert raw["items"][0]["terms"]["warnings"]
        raw["items"][0]["terms"]["warnings"] = []
    changed = load_proposal_draft(raw)
    raw["review"]["draft_sha256"] = proposal_draft_sha256(changed)
    changed = load_proposal_draft(raw)
    assert changed.review.draft_sha256 == proposal_draft_sha256(changed)
    before = _counts(case.app)
    message = "terms warnings" if change == "terms_warning" else "warning/exposure"
    with pytest.raises(ProposalConflictError, match=message):
        TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
            changed
        )
    assert _counts(case.app) == before


def test_injected_failure_after_binding_rolls_back_proposal_items_and_binding(case, monkeypatch):
    proposal = reviewed_proposal(case)
    import baibai_engine.investment_judgment.trade_proposal.service as owner

    def fail(*_):
        raise RuntimeError("injected readback failure")

    monkeypatch.setattr(owner, "load_investment_proposal", fail)
    with pytest.raises(RuntimeError, match="injected"):
        owner.TradeProposalService(
            case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
        ).publish(proposal)
    counts = _counts(case.app)
    assert (
        counts["investment_proposal"]
        == counts["investment_proposal_item"]
        == counts["position_decision_reference"]
        == 0
    )
    assert counts["ledger_event"] == 1


def test_nonbuy_has_no_binding_and_date_clock_guard_even_with_same_facts(case):
    proposal = reviewed_proposal(case, action="no_action")
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.publish(proposal).items[0].action == "no_action"
    assert _counts(case.app)["position_decision_reference"] == 0
    raw = proposal.payload()
    raw["proposal_id"] = "investment-proposal-00000000-0000-4000-8000-000000000099"
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    tomorrow = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED + timedelta(days=1)
    )
    with pytest.raises(ProposalError, match="基準日は本日"):
        tomorrow.publish(proposal)


@pytest.mark.parametrize("stance", ["uncertain", "unsupported"])
def test_missing_economic_comparison_is_not_concluded_by_stance(tmp_path, stance):
    case = backend_case(tmp_path, stance=stance, valuation_unresolved=True)
    proposal = reviewed_proposal(case, action="no_action")
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    before = _counts(case.app)
    ledger_before = _ledger_snapshot(case.app)
    checked = service.check(proposal)
    assert not checked.valid
    assert [reason.code for reason in checked.blockers] == ["no_action_comparison_incomplete"]
    with pytest.raises(ProposalError, match="経済比較も未確定"):
        service.publish(proposal)
    assert _counts(case.app) == before
    deferred = reviewed_proposal(case, action="defer")
    assert service.check(deferred).valid
    assert service.publish(deferred).items[0].action == "defer"
    assert _ledger_snapshot(case.app) == ledger_before


@pytest.mark.parametrize(
    ("stance", "broken", "valuation_unresolved"),
    [
        ("supported", False, False),
        ("unsupported", False, False),
        ("unsupported", True, True),
    ],
)
def test_concluded_no_action_does_not_require_all_buy_inputs(
    tmp_path, stance, broken, valuation_unresolved
):
    case = backend_case(
        tmp_path, stance=stance, broken=broken, valuation_unresolved=valuation_unresolved
    )
    if stance == "supported":
        case.market.rename(tmp_path / "unavailable-current-market.sqlite")
    proposal = reviewed_proposal(case, action="no_action")
    if stance == "supported":
        assert proposal.inputs.quotes == ()
        raw = proposal.payload()
        raw.update(
            comparison="期中の支払に同じ元手を残すため、取得時の資金拘束を選ばない。",
            forgone="足元の価格や取引条件は確認し直さず、現在の買付提案はしない。",
        )
        proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    ledger_before = _ledger_snapshot(case.app)
    assert service.check(proposal).valid
    published = service.publish(proposal)
    assert published.items[0].action == "no_action"
    assert read_saved_proposal(proposal_id=published.proposal_id, db_path=case.app) == published
    assert _ledger_snapshot(case.app) == ledger_before


@pytest.mark.parametrize(
    ("scenario", "price_finding", "blocker"),
    [
        ("uncompared", "undetermined", "no_action_comparison_incomplete"),
        ("quoted_rejection", "not_supported_at_quote", None),
        ("different_price", "not_supported_at_quote", "no_action_reviewed_quote_mismatch"),
        ("different_session", "not_supported_at_quote", "no_action_reviewed_quote_mismatch"),
        ("intraday_price", "not_supported_at_quote", "no_action_reviewed_quote_mismatch"),
        ("unconfirmed_right", "not_supported_at_quote", "no_action_reviewed_quote_mismatch"),
        ("business_rebuttal", "undetermined", None),
        ("cash_alternative", "supported_at_quote", None),
    ],
)
def test_new_no_action_matches_the_authored_economic_finding(
    tmp_path, scenario, price_finding, blocker
):
    case = backend_case(tmp_path)
    document, review = pair_payload(
        review_number=83,
        price_case=price_finding,
        quote_as_of="2026-10-13" if scenario == "different_session" else None,
    )
    comparison = "一単元と同額の11万円を同じ12か月で比較した。"
    if price_finding == "not_supported_at_quote":
        document["conclusion"]["stance"] = "uncertain"
        document["investment_case"].update(status="uncertain", status_reason="長期成長は未確定。")
        finding = document["price_value_case"]
        finding["price_compatible_conditions"] = "普通株の分配と残余負担は1100円を支持しない。"
        finding["price_only_condition"] = {
            "explanation": "同じ普通株の権利・費用・期間なら750円以下でのみ見直す。",
            "ceiling_yen": "750",
        }
        if scenario == "different_price":
            document["input_snapshot"]["facts"][0]["value"] = 1101
        if scenario == "intraday_price":
            document["input_snapshot"]["facts"][0]["price_basis"] = "realtime"
        if scenario == "unconfirmed_right":
            finding["acquisition_limit"] = {
                "value_fact_id": "owner_receipt",
                "last_eligible_trade_date": None,
            }
        if scenario == "quoted_rejection":
            # 価格の評価は立会を発注できるという判断ではない。
            with closing(sqlite3.connect(case.market)) as connection, connection:
                connection.execute("DELETE FROM jquants_market_calendar")
        rationale = "観測した普通株の権利への見返りが足りないので、この価格に限り見送る。"
        comparison += "今の取得価格では、普通株の受取額では維持現金を上回らない。"
    elif scenario == "uncompared":
        rationale = "維持投資が未確認で値段も現金・他案も比較していない。"
        comparison = "同額・同期間の比較は未着手。"
    elif scenario == "business_rebuttal":
        document["conclusion"]["stance"] = "unsupported"
        document["investment_case"].update(
            status="broken", status_reason="一次資料で主力契約終了と代替収益源の喪失を確認。"
        )
        document["valuation"].update(
            status="unresolved",
            market_price_fact_id=None,
            horizon_months=None,
            required_annual_return_pct=None,
            base=None,
            downside=None,
            unresolved_reason="主力契約の終了が中核仮説を反証するため数値化しない。",
        )
        review["recalculated_projections"] = []
        with closing(sqlite3.connect(case.market)) as connection, connection:
            connection.execute("UPDATE jquants_daily_bars SET close = NULL")
        rationale = "契約終了を確認したので、株価とは独立に見送る。"
        comparison = "収益源を失った当社ではなく現金を残す。"
    else:
        rationale = "一株価値は1100円を支持するが、同額の現金を選ぶ。"
        comparison += "下振れで一単元4万円を失う余地に対し、現金の名目損失はない。"
    update_review_hash(document, review)
    case.pair = AssessmentStoreService(
        case.app, clock=lambda: PREPARED - timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(83), document, review, supersedes_id=case.pair.assessment_id
    )
    case.operation = case.operations.record_assessment(
        case.operation.operation_id, case.pair.assessment_id, now=PREPARED
    )
    raw = reviewed_proposal(case, action="no_action").payload()
    raw.update(comparison=comparison, forgone="同じ資金を今は当社株に配分しない。")
    raw["items"][0]["rationale"] = rationale
    draft = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    assert draft.research_scope.selection_sha256 == selection_sha256(case.selected)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    ledger = _ledger_snapshot(case.app)
    before = case.app.read_bytes()
    checked = service.check(draft)
    assert [reason.code for reason in checked.blockers] == ([] if blocker is None else [blocker])
    if blocker is not None:
        assert not checked.valid
        with pytest.raises(ProposalError, match="defer"):
            service.publish(draft)
        assert case.app.read_bytes() == before
        assert _counts(case.app)["investment_proposal"] == 0
        if scenario == "uncompared":
            raw["items"][0].update(
                action="defer",
                blockers=[{"code": "maintenance_investment_unconfirmed", "message": rationale}],
                resume_conditions=["維持投資の原典を確認し、同じ元手の現金と比較する。"],
            )
            deferred = service.publish(
                bind_review(raw, {case.pair.assessment_id: case.pair.document})
            )
            assert deferred.items[0].action == "defer"
            assert deferred.items[0].assessment_ref == draft.items[0].assessment_ref
    else:
        assert checked.valid
        saved = service.publish(draft)
        [item] = saved.items
        assert (item.action, item.rationale) == ("no_action", rationale)
        assert item.assessment_ref.core_sha256 == case.pair.core_sha256
        assert item.assessment_ref.review_id == case.pair.review.review_id
        assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
        assert _counts(case.app)["position_decision_reference"] == 0
        viewed = read_proposal_views(case.app, generated_at=PUBLISHED).latest.proposal
        assert (viewed.items[0].action, viewed.items[0].rationale) == ("no_action", rationale)
        assert viewed.items[0].assessment.price_value_case.status == price_finding
        if scenario == "quoted_rejection":
            assessment = viewed.items[0].assessment
            assert assessment.investment_case_status == "uncertain"
            observed = assessment.price_value_case.observed_quote
            assert (observed.price_yen, observed.as_of, observed.price_basis) == (
                Decimal("1100"),
                PUBLISHED.date(),
                "last_close_unadjusted",
            )
            assert assessment.price_value_case.price_only_condition.ceiling_yen == Decimal("750")
    assert _ledger_snapshot(case.app) == ledger


@pytest.mark.parametrize("quote_age_days", [7, 8])
@pytest.mark.parametrize(
    ("finding", "stance", "price_dependent"),
    [
        ("not_supported_at_quote", "uncertain", True),
        ("supported_at_quote", "uncertain", True),
        ("supported_at_quote", "supported", False),
    ],
)
def test_no_action_only_needs_a_fresh_observation_when_its_comparison_uses_that_price(
    tmp_path, quote_age_days, finding, stance, price_dependent
):
    case = backend_case(tmp_path)
    observed_on = (PUBLISHED.date() - timedelta(days=quote_age_days)).isoformat()
    # Even a current independently reviewed pair cannot refresh an old native bar.
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET traded_at=?", (observed_on,))
    document, review = pair_payload(review_number=89, quote_as_of=observed_on, price_case=finding)
    document["conclusion"]["stance"] = stance
    update_review_hash(document, review)
    case.pair = AssessmentStoreService(
        case.app, clock=lambda: PREPARED - timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(89), document, review, supersedes_id=case.pair.assessment_id
    )
    case.operations.record_assessment(
        case.operation.operation_id, case.pair.assessment_id, now=PREPARED
    )
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    draft = reviewed_proposal(case, action="no_action")
    assert draft.inputs.quotes[0].price_as_of.isoformat() == observed_on
    assert case.pair.document.input_snapshot.as_of == PUBLISHED.date()
    ledger = _ledger_snapshot(case.app)
    if quote_age_days == 8 and price_dependent:
        before = case.app.read_bytes()
        assert [b.code for b in service.check(draft).blockers] == [
            "no_action_reviewed_quote_mismatch"
        ]
        with pytest.raises(ProposalError, match="鮮度"):
            service.publish(draft)
        assert case.app.read_bytes() == before
        # This same signed shape was accepted by the old writer. Preserve its exact ID and digest.
        saved = TradeProposal.model_validate({**draft.payload(), "published_at": PUBLISHED})
        seed_proposal(case.app, saved)
        before = case.app.read_bytes()
        assert service.publish(draft) == saved
        assert case.app.read_bytes() == before
    else:
        assert service.check(draft).valid
        saved = service.publish(draft)
    item = preview_current_terms(
        proposal_id=saved.proposal_id, db_path=case.app, sqlite_path=case.market, now=PUBLISHED
    ).items[0]
    assert (item["current_quote_calculation"] is not None) == (quote_age_days == 7)
    assert (
        read_proposal_views(case.app, generated_at=PUBLISHED).latest.proposal.items[0].action
        == "no_action"
    )
    assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert _ledger_snapshot(case.app) == ledger


@pytest.mark.parametrize(
    ("price_finding", "stance"),
    [
        ("not_supported_at_quote", "uncertain"),
        ("supported_at_quote", "uncertain"),
        ("supported_at_quote", "supported"),
    ],
)
def test_exact_price_comparison_cannot_reuse_a_right_at_its_final_close(
    tmp_path, price_finding, stance
):
    case = backend_case(tmp_path, assessment_as_of="2026-10-13")
    final_close = PREPARED.replace(hour=15, minute=30)
    document, review = pair_payload(
        review_number=87, quote_as_of="2026-10-13", price_case=price_finding
    )
    document["input_snapshot"]["facts"].append(
        dict(
            fact_id="final-session",
            fact_kind="last_eligible_trade_date",
            value="2026-10-14",
            unit="date",
            as_of="2026-10-14",
            source_ids=["annual"],
        )
    )
    document["price_value_case"]["value_fact_ids"].append("final-session")
    document["price_value_case"]["acquisition_limit"] = {
        "value_fact_id": "final-session",
        "last_eligible_trade_date": "2026-10-14",
    }
    observed = final_close - timedelta(minutes=10)
    for source in document["input_snapshot"]["sources"]:
        source["retrieved_at"] = observed.isoformat()
    document["conclusion"].update(stance=stance, assessed_at=observed.isoformat())
    review["reviewed_at"] = (observed + timedelta(minutes=1)).isoformat()
    update_review_hash(document, review)
    pair = AssessmentStoreService(
        case.app, clock=lambda: observed + timedelta(minutes=2)
    ).publish_reviewed_assessment(
        assessment_id(87), document, review, supersedes_id=case.pair.assessment_id
    )
    with closing(sqlite3.connect(case.market)) as connection, connection:
        # The last session is known. No later calendar row or same-day close is needed
        # to recognize expiry once this session ends.
        connection.execute("UPDATE jquants_daily_bars SET traded_at='2026-10-13'")
        connection.execute("DELETE FROM jquants_market_calendar")
        connection.execute("INSERT INTO jquants_market_calendar VALUES ('2026-10-14','1')")
    with (
        closing(connect_read_only(case.app)) as app,
        proposal_market_snapshot(case.market) as market,
    ):
        app.execute("BEGIN")
        for offset in (-1, 0, 1):
            now = final_close + timedelta(microseconds=offset)
            inputs = collect_proposal_inputs(
                app,
                tickers=("1234",),
                proposal_id=PROPOSAL_ID,
                now=now,
                sqlite_path=case.market,
                budget_min_yen=DEFAULT_BUDGET_MIN_YEN,
                budget_max_yen=DEFAULT_BUDGET_MAX_YEN,
                market_connection=market,
            )
            assert inputs.quotes[0].price_as_of.isoformat() == "2026-10-13"
            failure = no_action_blocker(
                pair,
                inputs=inputs,
                now=now,
                sqlite_path=case.market,
                market_connection=market,
                order_sessions=lambda now=now: read_order_session_bounds(
                    sqlite_path=case.market, now=now, connection=market
                ),
            )
            assert (None if failure is None else failure.code) == (
                None if offset < 0 else "no_action_reviewed_quote_mismatch"
            )


@pytest.mark.parametrize("days_to_deadline", [3, 15])
def test_dated_no_action_distinguishes_expiry_from_unconfirmed_acquisition(
    tmp_path, mocker, days_to_deadline
):
    case = backend_case(tmp_path)
    friday = datetime.fromisoformat("2026-10-16T16:20:00+09:00")
    seed_daily_bars(case.market, [("1234", friday.date().isoformat(), 1100.0, 1.0)])
    prefix = [
        ((friday.date() + timedelta(days=offset)).isoformat(), "0")
        for offset in range(1, days_to_deadline)
    ]
    deadline = (friday.date() + timedelta(days=days_to_deadline)).isoformat()
    following = (friday.date() + timedelta(days=days_to_deadline + 1)).isoformat()
    closed = [*prefix, (deadline, "3")]  # Futures-only on the last eligible day.
    expired, unknown = "right_acquisition_expired", "right_acquisition_unconfirmed"
    calendars = (
        ("open_on_deadline", [*prefix, (deadline, "2")], deadline, None),
        ("next_after_deadline", [*closed, (following, "1")], following, expired),
        ("closed_with_no_later_row", closed, None, expired),
        ("missing", [], None, unknown),
        ("gap_before_deadline", [*closed[:-2], (deadline, "3"), (following, "1")], None, unknown),
        (
            "invalid_before_deadline",
            [*closed[:-2], (prefix[-1][0], "?"), (deadline, "3"), (following, "1")],
            None,
            unknown,
        ),
    )
    variants = (
        ("not_supported_at_quote", "uncertain"),
        ("supported_at_quote", "uncertain"),
        ("supported_at_quote", "supported"),
    )
    ledger = _ledger_snapshot(case.app)
    prior = case.pair.assessment_id
    fingerprints = set()
    read = mocker.spy(proposal_service, "read_order_session_bounds")
    writer = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: friday + timedelta(minutes=2)
    )
    for index, (finding, stance) in enumerate(variants):
        document, review = pair_payload(
            as_of=friday.date().isoformat(), review_number=100 + index, price_case=finding
        )
        document["conclusion"]["stance"] = stance
        document["input_snapshot"]["facts"].append(
            {
                "fact_id": "final-session",
                "fact_kind": "last_eligible_trade_date",
                "value": deadline,
                "unit": "date",
                "as_of": friday.date().isoformat(),
                "source_ids": ["annual"],
            }
        )
        document["price_value_case"]["value_fact_ids"].append("final-session")
        document["price_value_case"]["acquisition_limit"] = {
            "value_fact_id": "final-session",
            "last_eligible_trade_date": deadline,
        }
        update_review_hash(document, review)
        pair = AssessmentStoreService(
            case.app, clock=lambda index=index: friday - timedelta(minutes=11 - index)
        ).publish_reviewed_assessment(
            assessment_id(100 + index), document, review, supersedes_id=prior
        )
        prior = pair.assessment_id
        case.operations.record_assessment(case.operation.operation_id, prior, now=friday)
        saved = saved_draft = None
        for offset, (name, rows, session, acquisition_problem) in enumerate(calendars):
            with closing(sqlite3.connect(case.market)) as connection, connection:
                connection.execute("DELETE FROM jquants_market_calendar")
                connection.executemany("INSERT INTO jquants_market_calendar VALUES (?,?)", rows)
            context = build_proposal_context(
                operation_id=case.operation.operation_id,
                proposal_id=PROPOSAL_ID[:-2] + f"{50 + index * 10 + offset:02x}",
                db_path=case.app,
                sqlite_path=case.market,
                now=friday,
            )
            assert context.research_scope.selection_sha256 == selection_sha256(case.selected)
            assert context.inputs.quotes[0].price_as_of == friday.date()
            assert (
                None
                if context.next_order_session is None
                else context.next_order_session.isoformat()
            ) == session
            fingerprints.add(proposal_inputs_fingerprint(context.inputs))
            raw = scaffold_proposal(context)
            raw.update(
                headline="合成の取得条件と資金比較",
                comparison="同じ30万円・12か月で、期中支払と分配までの必要資金を比較した。",
                forgone="期中支払まで現金を残すため、取得できる場合も買付を選ばない。",
            )
            raw["items"][0].update(
                action="no_action",
                rationale="観測価格・普通株の取得権と同額の現金を比較。",
                review_conditions=["取得期限または期中の資金用途を再確認する。"],
            )
            draft = bind_review(
                raw,
                {prior: pair.document},
                reviewed_at=(friday + timedelta(minutes=1)).isoformat(),
            )
            reads_before = read.call_count
            checked = writer.check(draft)
            assert read.call_count == reads_before + 1
            assert checked.valid == (acquisition_problem is None), (finding, stance, name)
            if name == "open_on_deadline":
                saved_draft, saved = draft, writer.publish(draft)
                assert saved.items[0].action == "no_action"
            if acquisition_problem is not None:
                before = case.app.read_bytes()
                assert [reason.code for reason in checked.blockers] == [
                    "no_action_reviewed_quote_mismatch"
                ]
                expected = "期限まで" if acquisition_problem == expired else "最終立会が未確認"
                with pytest.raises(ProposalError, match=expected):
                    writer.publish(draft)
                assert writer.publish(saved_draft) == saved
                assert case.app.read_bytes() == before
            # An uncertain or expired comparison cannot change a signed historical action.
            preview = preview_current_terms(
                proposal_id=saved.proposal_id,
                db_path=case.app,
                sqlite_path=case.market,
                now=friday + timedelta(minutes=2),
            ).items[0]
            why = preview.get("current_quote_unavailable_reason")
            assert (None if why is None else why["code"]) == acquisition_problem
            assert (preview["current_quote_calculation"] is not None) == (
                name == "open_on_deadline"
            )
            assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert len(fingerprints) == 1
    assert _counts(case.app)["position_decision_reference"] == 0
    assert _ledger_snapshot(case.app) == ledger


@pytest.mark.parametrize(
    ("closing_days", "expected"),
    [
        (("2026-10-15", "2026-10-16"), "right_acquisition_expired"),
        (("2026-10-15",), "right_acquisition_unconfirmed"),
    ],
)
def test_buy_uses_calendar_lower_bound_only_to_prove_right_expiry(tmp_path, closing_days, expected):
    case = backend_case(tmp_path)
    document, review = pair_payload(review_number=90)
    deadline = "2026-10-16"
    document["input_snapshot"]["facts"].append(
        {
            "fact_id": "last-session",
            "fact_kind": "last_eligible_trade_date",
            "value": deadline,
            "unit": "date",
            "as_of": "2026-10-14",
            "source_ids": ["annual"],
        }
    )
    document["price_value_case"]["value_fact_ids"].append("last-session")
    document["price_value_case"]["acquisition_limit"] = {
        "value_fact_id": "last-session",
        "last_eligible_trade_date": deadline,
    }
    update_review_hash(document, review)
    case.pair = AssessmentStoreService(
        case.app, clock=lambda: PREPARED - timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(90), document, review, supersedes_id=case.pair.assessment_id
    )
    case.operations.record_assessment(
        case.operation.operation_id, case.pair.assessment_id, now=PREPARED
    )
    ledger = _ledger_snapshot(case.app)
    writer = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    original = reviewed_proposal(case)
    saved = writer.publish(original)
    assert saved.items[0].terms is not None
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute("DELETE FROM jquants_market_calendar WHERE day > '2026-10-14'")
        connection.executemany(
            "INSERT INTO jquants_market_calendar VALUES (?,'0')", [(day,) for day in closing_days]
        )
    before = (case.app.read_bytes(), case.market.read_bytes())
    new_id = PROPOSAL_ID[:-2] + "79"
    context = build_proposal_context(
        operation_id=case.operation.operation_id,
        proposal_id=new_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PREPARED,
    )
    assert context.next_order_session is None
    calculation = context.calculations["1234"]["buy_at_actual_quote"]
    assert {reason["code"] for reason in calculation["hard_blockers"]} == {
        expected,
        "order_session_unknown",
    }
    draft = reviewed_proposal(case, proposal_id=new_id)
    assert {reason.code for reason in writer.check(draft).blockers} == {
        expected,
        "order_session_unknown",
    }
    with pytest.raises(ProposalError):
        writer.publish(draft)
    preview = preview_current_terms(
        proposal_id=saved.proposal_id, db_path=case.app, sqlite_path=case.market, now=PUBLISHED
    )
    assert preview.status == "blocked"
    row = preview.items[0]
    assert {reason["code"] for reason in row["blockers"]} == {expected, "order_session_unknown"}
    assert row["saved_terms"] == saved.items[0].terms.model_dump(mode="json")
    for projection in (calculation, row["saved_limit_revalidation"]):
        assert projection["quantity"] == 0
        assert projection["capital_comparison"] is None
    assert writer.publish(original) == saved
    assert (case.app.read_bytes(), case.market.read_bytes()) == before
    assert _ledger_snapshot(case.app) == ledger


def test_old_uncertain_no_action_keeps_exact_read_and_retry(tmp_path):
    case = backend_case(tmp_path, valuation_unresolved=True)
    proposal = reviewed_proposal(case, action="no_action")
    historical = TradeProposal.model_validate(
        {**proposal.model_dump(mode="python"), "published_at": PUBLISHED}
    )
    # A prior writer accepted this payload. New policy must not rewrite its saved meaning.
    seed_proposal(case.app, historical)
    before = _counts(case.app)
    assert read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == historical
    service = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED + timedelta(days=1)
    )
    assert service.publish(proposal) == historical
    assert _counts(case.app) == before


def test_live_latest_gate_applies_to_buy_but_not_no_action(case):
    buy = reviewed_proposal(case)
    no_action = reviewed_proposal(case, action="no_action")
    document, review = pair_payload(review_number=22)
    AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(21), document, review, supersedes_id=case.pair.assessment_id
    )
    service = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED + timedelta(minutes=5)
    )
    validation = service.check(buy)
    assert not validation.valid
    assert any(reason.code == "latest_assessment_required" for reason in validation.blockers)
    assert service.check(no_action).valid
    assert service.publish(no_action).items[0].action == "no_action"


def test_late_outcome_does_not_rewrite_saved_scope_but_rejects_stale_new_publication(tmp_path):
    case = backend_case(tmp_path, tickers=("1234", "5678"))
    case.operation = case.operations.record_unfinished(
        case.operation.operation_id,
        ticker="5678",
        reason="確認する資料が残る。",
        references=("https://example.invalid/pending",),
        now=PUBLISHED - timedelta(minutes=12),
    )
    proposal = reviewed_proposal(case, action="no_action")
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    published = service.publish(proposal)
    document, review = pair_payload(ticker="5678", review_number=32)
    pair = AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(assessment_id(31), document, review)
    case.operations.record_assessment(
        case.operation.operation_id, pair.assessment_id, now=PUBLISHED + timedelta(minutes=2)
    )
    stored = read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app)
    assert stored == published
    assert stored.research_scope.outcomes[1].status == "unfinished"
    raw = proposal.payload()
    raw["proposal_id"] = "investment-proposal-00000000-0000-4000-8000-000000000099"
    new = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    with pytest.raises(ProposalConflictError, match="fixed coverage"):
        TradeProposalService(
            case.app, sqlite_path=case.market, clock=lambda: PUBLISHED + timedelta(minutes=3)
        ).publish(new)


def test_same_session_adjustment_does_not_change_native_quote_or_comparison(case):
    proposal = reviewed_proposal(case)
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET adjustment_factor=0.5, ex_right_type='1'")
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(proposal).valid
    assert service.publish(proposal).proposal_id == proposal.proposal_id


@pytest.mark.parametrize(
    ("broken", "assessment_as_of"),
    [(False, "2026-10-14"), (True, "2026-10-14"), (True, "2026-10-13")],
)
@pytest.mark.parametrize("floor", [900, 1200])
def test_author_sell_floor_flows_through_check_publish_get_and_preview(
    tmp_path, broken, assessment_as_of, floor
):
    case = backend_case(
        tmp_path,
        holding=True,
        broken=broken,
        valuation_unresolved=broken,
        assessment_as_of=assessment_as_of,
    )
    raw = reviewed_proposal(case, action="sell").payload()
    item = raw["items"][0]
    item["terms_unavailable_reasons"] = []
    item["terms"] = {
        "side": "sell",
        "limit_price_yen": str(floor),
        "quantity": 100,
        "notional_yen": str(floor * 100),
        "target_session": "2026-10-15",
        "valuation_as_of": assessment_as_of,
        "maximum_entry_price_yen": None,
        "price_grid_rule_id": "tse-common-conservative-20261014",
        "conditions": ["全量の回収下限。未約定時は回復待ちの代償を再確認する。"],
        "warnings": [],
    }
    if not broken:
        item["reason_code"] = "insufficient_remaining_reward"
        item["remaining_reward"] = {
            "status": "insufficient",
            "reason": "現在quoteで今後の損失経路に見返りが見合わない。",
        }
        item["case_reassessment_reason"] = "仮説は維持している。"
    raw["comparison"] = "実quoteと作者下限での保有継続を比べ、下限でも回収を選ぶ。"
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    checked = service.check(proposal)
    assert checked.valid
    calculation = checked.payload()["calculations"]["1234"]
    assert (calculation["sell_limit_projection"] is None) is broken
    if broken:
        wrong_basis = proposal.payload()
        wrong_basis["items"][0]["terms"]["valuation_as_of"] = (
            "2026-10-13" if assessment_as_of == "2026-10-14" else "2026-10-14"
        )
        wrong_basis = bind_review(wrong_basis, {case.pair.assessment_id: case.pair.document})
        rejected = service.check(wrong_basis)
        assert not rejected.valid
        assert "sell_valuation_basis_differs" in {reason.code for reason in rejected.blockers}
    if not broken:
        assert calculation["current_price_projection"]["price_yen"] == 1100
        assert Decimal(calculation["sell_limit_projection"]["calculation_price_yen"]) == floor
    ledger_before = _ledger_snapshot(case.app)
    published = service.publish(proposal)
    assert read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == published
    before_preview = case.app.read_bytes()
    preview = preview_current_terms(
        proposal_id=proposal.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=5),
    )
    assert preview.status == "unchanged"
    assert (
        preview.items[0]["saved_limit_revalidation"]["sell_limit_projection"]
        == calculation["sell_limit_projection"]
    )
    assert Decimal(preview.items[0]["saved_terms"]["limit_price_yen"]) == floor
    assert preview.current_inputs.quotes[0].price_yen == 1100
    assert preview.current_inputs.available_cash_yen == proposal.inputs.available_cash_yen
    assert case.app.read_bytes() == before_preview
    assert _ledger_snapshot(case.app) == ledger_before
    changed = proposal.payload()
    changed["items"][0]["terms"].update(
        limit_price_yen=str(floor + 1), notional_yen=str((floor + 1) * 100)
    )
    with pytest.raises(ProposalConflictError, match="content review"):
        service.check(load_proposal_draft(changed))


def test_policy_revision_invalidates_unpublished_draft_not_saved_identity(case, monkeypatch):
    import baibai_engine.investment_judgment.trade_proposal.inputs as proposal_inputs

    current_policy = proposal_inputs.proposal_input_policy_id()
    with monkeypatch.context() as old:
        old.setattr(proposal_inputs, "proposal_input_policy_id", lambda: "retained-old-policy")
        proposal = reviewed_proposal(case)
        service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
        published = service.publish(proposal)
    assert current_policy != proposal.inputs.policy_id
    before = case.app.read_bytes()
    assert service.publish(proposal) == published
    assert read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == published
    preview = preview_current_terms(
        proposal_id=proposal.proposal_id, db_path=case.app, sqlite_path=case.market, now=PUBLISHED
    )
    assert preview.status == "changed"
    assert "input_facts_changed:policy_id" in {reason.code for reason in preview.change_reasons}
    assert case.app.read_bytes() == before
    with pytest.raises(ProposalConflictError, match="input facts changed"):
        service.check(proposal)


@pytest.mark.parametrize("quantity", [100, 200, 300, 400])
def test_selected_buy_quantity_publication_uses_existing_ceiling(tmp_path, quantity):
    case = backend_case(tmp_path, cash_yen=500000)
    raw = reviewed_proposal(case).payload()
    raw["items"][0]["terms"].update(quantity=quantity, notional_yen=str(quantity * 1000))
    raw["comparison"] = "選択数量と上限量・現金維持を比べ、no-addで増量できない代償も確認。"
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    checked = service.check(proposal)
    assert checked.valid is (quantity <= 300)
    assert checked.calculations["1234"]["maximum_quantity"] == 300
    if quantity > 300:
        assert "selected_quantity_above_maximum" in {reason.code for reason in checked.blockers}
        with pytest.raises(ProposalError, match="数量上限"):
            service.publish(proposal)
        return
    published = service.publish(proposal)
    assert published.items[0].terms.quantity == quantity
    assert published.items[0].terms.notional_yen == quantity * 1000
    assert ("budget_guide_under" in published.items[0].terms.warnings) is (quantity == 100)
    assert published.portfolio_exposure.ticker.prospective_pct == quantity / 5
    changed = proposal.payload()
    replacement = 200 if quantity == 100 else 100
    changed["items"][0]["terms"].update(quantity=replacement, notional_yen=str(replacement * 1000))
    with pytest.raises(ProposalConflictError, match="content review"):
        service.check(load_proposal_draft(changed))
