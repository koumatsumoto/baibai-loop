import gzip
import sqlite3
from contextlib import closing
from dataclasses import replace
from itertools import permutations

import pytest
from pydantic import TypeAdapter, ValidationError
from tests.model_generation_fixtures import generation

from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CompanyCoordinates,
    EconomicPathPolicy,
    EconomicPathSelection,
    PathCoordinates,
    select_economic_paths,
)
from baibai_engine.machine_analysis.screening.policies.two_views import ViewSelection
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import canonical_json


def row(ticker, p, t, *, group="sector", eligible=True, variant="quarter"):
    return CompanyCoordinates(
        ticker,
        ticker,
        group,
        (PathCoordinates("business_expansion", variant, p, t, group, group, eligible, {}),),
        {},
        {},
        (),
    )


def select(*rows, limit=36):
    return select_economic_paths(rows, policy=EconomicPathPolicy(candidate_limit=limit))


def test_balanced_joint_evidence_outranks_one_axis_extreme():
    result = select(row("1001", 100, 1), row("1002", 60, 60), row("1003", 1, 100))
    assert result.cards[0].ticker == "1002"
    assert result.cards[0].paths[0].score == 0.5
    assert result.cards[0].priority == pytest.approx(5 / 6)
    assert result.cards[1].priority < result.cards[0].priority


def test_missing_other_axis_remains_in_reference_population():
    result = select(row("1001", 10, 20), row("1002", 30, None), row("1003", None, 40))
    path = result.cards[0].paths[0]
    assert path.price_population == path.business_population == 2
    assert path.price_percentile == path.business_percentile == 0.25
    assert result.eligible_count == 1


def test_sign_admission_does_not_change_reference_distribution():
    result = select(row("1001", 10, 10), row("1002", -10, -10, eligible=False))
    assert result.cards[0].paths[0].price_percentile == 0.75
    assert result.eligible_count == 1


def test_singleton_and_ties_are_neutral_midrank():
    assert select(row("1001", 10, 20)).cards[0].priority == 0.5
    result = select(*(row(f"{1000 + i}", 10, 20) for i in range(37)))
    assert all(card.priority == 0.5 for card in result.cards)
    for card in result.cards:
        normalization = card.paths[0].normalization
        assert normalization is not None
        assert normalization.population == 37
        assert normalization.midrank == 19.0
    assert result.tickers == tuple(str(1000 + i) for i in range(36))


def test_input_order_and_equal_path_order_do_not_change_result():
    rows = [row(str(1000 + i), 10, 20) for i in range(3)]
    expected = select(*rows, limit=2)
    for order in permutations(rows):
        assert select(*order, limit=2) == expected
    first = rows[0]
    second_path = replace(first.paths[0], path_id="earning_power")
    a = select(replace(first, paths=(first.paths[0], second_path)))
    b = select(replace(first, paths=(second_path, first.paths[0])))
    assert a == b
    assert a.cards[0].priority == 0.5
    assert a.cards[0].strongest_path == "business_expansion"


def test_best_path_does_not_sum_support_or_penalize_missing_path():
    first = row("1001", 10, 20)
    another = replace(first.paths[0], path_id="earning_power")
    result = select(replace(first, paths=(*first.paths, another)))
    assert result.cards[0].priority == select(first).cards[0].priority
    extra_models = replace(first, model_revisions={f"model_{i}": "v1" for i in range(7)})
    assert select(extra_models).cards[0].priority == select(first).cards[0].priority


def test_path_normalization_can_prefer_lower_h_with_higher_within_path_rank():
    earning = [
        replace(
            row(f"1{i:03d}", float(i), float(i)),
            paths=(
                PathCoordinates(
                    "earning_power", "quarter", float(i), float(i), "all", "all", True, {}
                ),
            ),
        )
        for i in range(1, 4)
    ]
    expansion = [
        row(f"2{i:03d}", float(i), float(i), group="all", eligible=i >= 18) for i in range(20)
    ]
    result = select(*earning, *expansion)
    assert result.eligible_count == 5
    assert result.cards[0].ticker == "1003"
    first = result.cards[0].paths[0]
    assert first.normalization is not None
    assert first.normalization.population == 3
    assert first.normalization.midrank == 1.0
    assert first.normalization.percentile == pytest.approx(5 / 6)
    larger_h = next(card for card in result.cards if card.ticker == "2019").paths[0]
    assert larger_h.score > first.score
    assert larger_h.normalization is not None
    assert larger_h.normalization.percentile == 0.75
    assert result.cards[0].priority == first.normalization.percentile


def test_company_strongest_path_uses_q_even_when_its_other_path_has_higher_h():
    def earning(ticker, value, *, eligible=True):
        source = row(ticker, value, value, group="all", eligible=eligible)
        return replace(source, paths=(replace(source.paths[0], path_id="earning_power"),))

    target = earning("1001", 102.0)
    expansion = row("1001", 5.0, 5.0, group="all")
    target = replace(target, paths=(*target.paths, *expansion.paths))
    earning_active = [
        earning(f"3{i:03d}", float(value)) for i, value in enumerate((100, 101, 103, 104))
    ]
    earning_reference = [
        earning(f"4{i:03d}", float(value), eligible=False) for i, value in enumerate(range(1, 21))
    ]
    expansion_reference = [
        row(f"5{i:03d}", float(value), float(value), group="all", eligible=False)
        for i, value in enumerate((1, 2, 3, 6, 7, 8, 9))
    ]
    result = select(
        target,
        *earning_active,
        *earning_reference,
        row("2001", 4.0, 4.0, group="all"),
        *expansion_reference,
    )
    card = next(card for card in result.cards if card.ticker == "1001")
    expansion_path, earning_path = card.paths
    assert [path.path_id for path in card.paths] == ["business_expansion", "earning_power"]
    assert earning_path.score == pytest.approx(0.9)
    assert expansion_path.score == pytest.approx(0.5)
    assert earning_path.normalization is not None
    assert expansion_path.normalization is not None
    assert earning_path.normalization.percentile == 0.5
    assert expansion_path.normalization.percentile == 0.75
    assert card.strongest_path == "business_expansion"
    assert card.priority == expansion_path.normalization.percentile


def test_annual_population_includes_companies_using_quarter():
    rows = []
    for i in range(102):
        # 100社は四半期の観測があり年次へ切り替えないが、負の増収で経路不適格。
        # 観測の存在と経路の適格性は別。未採用の年次座標も参照群には残す。
        original = row(str(1000 + i), 10, None if i < 2 else -10, eligible=False)
        annual = replace(
            original.paths[0],
            variant="annual_ttm",
            business_value=float(i + 1),
            business_group="sector:annual",
            eligible=i < 2,
        )
        rows.append(replace(original, paths=(*original.paths, annual)))
    result = select(*rows)
    assert result.eligible_count == 2
    candidate = next(card for card in result.cards if card.ticker == "1001")
    path = candidate.paths[0]
    assert path.business_population == 102
    assert path.business_percentile == pytest.approx(1.5 / 102)
    assert path.price_population == 102  # 共通の年次価格座標を一社につき一回だけ数える


def test_inactive_variant_only_supplies_its_reference_coordinates():
    first = row("1001", 10, 20)
    inactive = replace(
        first.paths[0],
        variant="annual_ttm",
        price_value=100,
        business_value=100,
        price_group="annual",
        business_group="annual",
        eligible=False,
    )
    second = row("1002", 50, 50, variant="annual_ttm", group="annual")
    result = select(replace(first, paths=(*first.paths, inactive)), second)
    cards = {card.ticker: card for card in result.cards}
    assert len(cards["1001"].paths) == 1
    assert cards["1001"].paths[0].variant == "quarter"
    assert cards["1001"].paths[0].score == 0.5
    assert cards["1001"].priority == 0.75
    assert cards["1002"].paths[0].price_population == 2
    assert cards["1002"].paths[0].price_percentile == 0.25
    assert cards["1002"].paths[0].business_percentile == 0.25


@pytest.mark.parametrize("axis", ["price_value", "business_value"])
def test_contradictory_same_company_reference_values_are_rejected(axis):
    first = row("1001", 1, 2)
    another = replace(first.paths[0], variant="annual_ttm", eligible=False, **{axis: 3})
    with pytest.raises(ValueError, match="one value per company"):
        select(replace(first, paths=(*first.paths, another)))


def test_duplicate_company_or_path_variant_is_not_an_extra_vote():
    first = row("1001", 1, 2)
    with pytest.raises(ValueError, match="one row per ticker"):
        select(first, first)
    with pytest.raises(ValueError, match="variants must be unique"):
        select(replace(first, paths=(*first.paths, *first.paths)))


def test_reference_groups_do_not_mix_sectors_or_path_meanings():
    first = row("1001", 1, 2)
    other_sector = row("1002", 100, 200, group="other_sector")
    other_path = replace(
        row("1003", 100, 200),
        paths=(
            replace(first.paths[0], path_id="earning_power", price_value=100, business_value=200),
        ),
    )
    result = select(first, other_sector, other_path)
    assert all(card.priority == 0.5 for card in result.cards)
    assert all(card.paths[0].price_population == 1 for card in result.cards)


def test_multiple_active_periods_are_not_a_cherry_picking_route():
    first = row("1001", 1, 2)
    annual = replace(first.paths[0], variant="annual_ttm")
    with pytest.raises(ValueError, match="select the period"):
        select(replace(first, paths=(*first.paths, annual)))


@pytest.mark.parametrize("axis", ["price_value", "business_value"])
@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf"), True, "1"])
@pytest.mark.parametrize("eligible", [False, True])
def test_non_finite_coordinates_are_not_ranked(invalid, axis, eligible):
    first = row("1001", 1, 1, eligible=eligible)
    with pytest.raises(ValueError, match="finite"):
        select(replace(first, paths=(replace(first.paths[0], **{axis: invalid}),)))


def test_disabled_or_empty_does_not_fabricate_candidates():
    assert select().cards == ()
    assert select(row("1001", 1, 2), limit=0).cards == ()
    assert select(row("1001", None, None)).cards == ()
    assert select(row("1001", 1, None), row("1002", None, 2)).eligible_count == 0


@pytest.mark.parametrize("invalid", [-1, 37, True])
def test_candidate_limit_is_a_finite_attention_cap(invalid):
    with pytest.raises(ValidationError):
        EconomicPathPolicy(candidate_limit=invalid)


def test_card_evidence_context_and_missing_models_round_trip_with_new_result():
    first = row("1001", 0.100049, 0.25)
    first = replace(
        first,
        paths=(
            replace(
                first.paths[0],
                evidence={
                    "period": {"start": "2025-04-01", "end": "2026-03-31"},
                    "price_date": "2026-10-28",
                    "missing": None,
                    "source": ["reported", {"document_id": "fixture"}],
                },
            ),
        ),
        context={
            "reported_loss": -50,
            "cash_is_not_surplus": True,
            "future_model": {"detail_reference": "fixed-generation"},
        },
        model_revisions={f"model_{i}": "v1" for i in range(7)},
        not_generated=("model_8",),
    )
    selection = ScreeningSelection("fixed-generation", ("1001",), select(first))
    codec = TypeAdapter(ScreeningSelection)
    restored = codec.validate_json(codec.dump_json(selection))
    assert restored == selection
    assert isinstance(restored.result, EconomicPathSelection)
    card = restored.result.cards[0]
    assert card.paths[0].price_value == 0.100049
    assert card.paths[0].normalization is not None
    assert card.paths[0].normalization.population == 1
    assert card.paths[0].normalization.midrank == 1.0
    assert card.context == first.context
    assert card.paths[0].evidence == first.paths[0].evidence
    assert card.not_generated == ("model_8",)


@pytest.mark.parametrize("invalid", [True, float("nan"), float("inf")])
def test_saved_card_coordinates_must_remain_finite_numbers(invalid):
    codec = TypeAdapter(ScreeningSelection)
    payload = codec.dump_python(
        ScreeningSelection("g", ("1001",), select(row("1001", 1, 2))), mode="json"
    )
    payload["result"]["cards"][0]["paths"][0]["price_value"] = invalid
    with pytest.raises(ValidationError):
        codec.validate_python(payload)


@pytest.mark.parametrize(
    "method",
    [
        "earnings-price-views-v1",
        "best-economic-path-v1",
        "best-economic-path-v2",
        "best-economic-path-v3",
    ],
)
def test_historical_selection_payload_keeps_shape_and_idempotent_publication(tmp_path, method):
    path = tmp_path / "runs.sqlite"
    fixed = generation()
    ModelGenerationStore(path).publish(fixed)
    # 合成した固定保存契約fixture。本番履歴ではなく、現行計算・codecから生成しない。
    # 新しい既定fieldに合わせて更新せず、旧bytesを維持できるかを検証する。
    result = {
        "policy": {
            "method": "earnings-price-views-v1",
            "earnings_limit": 1,
            "combined_limit": 1,
            "earnings_scale": 0.1,
            "momentum_scale": 0.5,
            "earnings_weight": 0.6,
        },
        "views": [
            {
                "view_id": "earnings-price",
                "limit": 1,
                "eligible_count": 1,
                "companies": [{"ticker": "1001", "rank": 1, "ranking_value": 0.1}],
            },
            {
                "view_id": "earnings-price-response",
                "limit": 1,
                "eligible_count": 1,
                "companies": [{"ticker": "1001", "rank": 1, "ranking_value": -0.123456}],
            },
        ],
    }
    if method != "earnings-price-views-v1":
        result = {
            "policy": {"method": method, "candidate_limit": 36},
            "eligible_count": 1,
            "cards": [
                {
                    "ticker": "1001",
                    "name": "1001",
                    "sector": "sector",
                    "rank": 1,
                    "priority": 0.5,
                    "strongest_path": "business_expansion",
                    "paths": [
                        {
                            "path_id": "business_expansion",
                            "variant": "quarter",
                            "price_value": 0.100049,
                            "business_value": 0.2,
                            "price_group": "sector",
                            "business_group": "sector",
                            "price_percentile": 0.5,
                            "business_percentile": 0.5,
                            "price_population": 1,
                            "business_population": 1,
                            "score": 0.5,
                            "evidence": {},
                        }
                    ],
                    "context": {},
                    "model_revisions": {},
                    "not_generated": [],
                }
            ],
        }
    expected = {
        "generation_id": "synthetic-generation",
        "universe_tickers": ["1001"],
        "result": result,
    }
    payload = canonical_json(expected).encode()
    compressed = gzip.compress(payload, mtime=0)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO screening_selection VALUES (?,?,?,?)",
            (
                "selection",
                fixed.generation_id,
                "2026-10-29T00:00:00+00:00",
                compressed,
            ),
        )
    store = ScreeningSelectionStore(path)
    restored = store.read("selection")
    assert restored is not None
    assert isinstance(
        restored.result,
        ViewSelection if method == "earnings-price-views-v1" else EconomicPathSelection,
    )
    assert restored.result.tickers == ("1001",)
    codec = TypeAdapter(ScreeningSelection)
    assert canonical_json(codec.dump_python(restored, mode="json")).encode() == payload
    assert not store.publish("selection", restored)
    with closing(sqlite3.connect(path)) as connection:
        saved = connection.execute(
            "SELECT payload_gzip FROM screening_selection WHERE selection_id='selection'"
        ).fetchone()[0]
    assert saved == compressed
    assert gzip.decompress(saved) == payload


@pytest.mark.parametrize(
    "method", ["best-economic-path-v1", "best-economic-path-v2", "best-economic-path-v3"]
)
def test_historical_economic_method_is_read_only_not_relabelled_with_current_behavior(method):
    with pytest.raises(ValueError, match="read-only"):
        select_economic_paths((), policy=EconomicPathPolicy(method=method))
