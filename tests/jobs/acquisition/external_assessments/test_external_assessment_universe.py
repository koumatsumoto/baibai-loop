"""外部評価の母集団はexact日付の完全なnative受領からだけ選ぶ。"""

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from tests.helpers.screening_sqlite import make_master_records

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage, complete_request
from baibai_engine.data_collection.providers.jquants.dependencies import dependency_from_request
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_jobs.acquisition.external_assessments.universe import received_master_universe

DAY = date(2026, 10, 9)
AT = datetime(2026, 10, 9, 7, tzinfo=UTC)


def dependency(rows, *, day=DAY):
    return dependency_from_request(
        "native-master",
        complete_request(
            (
                JQuantsPage(
                    "master", {"date": day.isoformat()}, AT, json.dumps({"data": rows}).encode()
                ),
            )
        ),
    )


def test_native_universe_keeps_banks_alpha_and_all_sectors_but_excludes_other_products():
    rows = make_master_records(DAY)
    rows.extend(
        [
            {**rows[0], "Code": "83060", "S33Nm": "銀行業"},
            {**rows[0], "Code": "130A0", "S33Nm": "その他金融業"},
            {**rows[0], "Code": "90000", "ProdCat": "014"},
            {**rows[0], "Code": "90010", "MktNm": "TOKYO PRO MARKET"},
            {**rows[0], "Code": "90020", "MktNm": "RESIDUAL"},
        ]
    )
    result = received_master_universe(dependency(rows), DAY)
    assert result == tuple(sorted((*map(str, range(1000, 3500)), "130A", "8306")))


@pytest.mark.parametrize("requested", [DAY - timedelta(days=1), DAY + timedelta(days=1)])
def test_native_universe_never_falls_back_to_a_nearby_master_date(requested):
    with pytest.raises(ValueError, match="exact-date master"):
        received_master_universe(dependency(make_master_records(DAY)), requested)


def test_future_master_date_is_rejected_before_decoding_the_receipt():
    future = datetime.now(ZoneInfo("Asia/Tokyo")).date() + timedelta(days=1)
    with pytest.raises(ValueError, match="master cannot be in the future"):
        received_master_universe({}, future)


@pytest.mark.parametrize(
    "invalid",
    ["empty", "below_floor", "non_common", "excluded_market", "missing_product", "invalid_code"],
)
def test_native_universe_requires_complete_distinct_common_stock_population(invalid):
    rows = make_master_records(DAY)
    if invalid == "empty":
        rows = []
    elif invalid == "below_floor":
        rows.pop()
    elif invalid == "non_common":
        for row in rows[1:]:
            row["ProdCat"] = "014"
    elif invalid == "excluded_market":
        for row in rows[1:]:
            row["MktNm"] = "TOKYO PRO MARKET"
    elif invalid == "missing_product":
        rows[0].pop("ProdCat")
    else:
        rows[0]["Code"] = "７２０３"
    with pytest.raises((ValueError, JQuantsProviderError)):
        received_master_universe(dependency(rows), DAY)
