from __future__ import annotations

from baibai_web.readmodel.builders import _selection_longlist_entry_view


def _raw_entry(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"rank": 1, "ticker": "1111", "name": "sample"}
    payload.update(overrides)
    return payload


def test_old_selection_payload_degrades_to_not_evaluable() -> None:
    view = _selection_longlist_entry_view(_raw_entry())

    assert view.fv_convergence.status == "not_evaluable"
    assert view.fv_convergence.anchors_yen == {}


def test_warning_provenance_is_exposed_and_invalid_anchor_is_ignored() -> None:
    view = _selection_longlist_entry_view(
        _raw_entry(
            fv_convergence={
                "status": "warning",
                "warning_code": "price_at_or_above_all_fv_anchors",
                "market_price_yen": 500,
                "anchors_yen": {
                    "fv_sector_median_yen": 450,
                    "fv_self_range_yen": "unknown",
                },
                "er_reversion_annual": -0.01,
            }
        )
    )

    assert view.fv_convergence.status == "warning"
    assert view.fv_convergence.warning_code == "price_at_or_above_all_fv_anchors"
    assert view.fv_convergence.market_price_yen == 500
    assert view.fv_convergence.anchors_yen == {"fv_sector_median_yen": 450}
    assert view.fv_convergence.er_reversion_annual == -0.01
