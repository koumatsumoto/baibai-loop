"""Only bounded aggregate facts may leave an explicitly selected TradingView receipt copy."""

import json
from datetime import timedelta

import pytest
from mcp.types import CallToolResult, TextContent
from tests.helpers.tradingview import DAY, _sdk, payload, retain_result, store

from baibai_engine.machine_analysis.observations.external_assessments.tradingview.contract import (
    FIELDS,
)
from baibai_jobs.integration.tradingview_receipts import diagnose_receipts, main


def test_selected_shapes_account_for_every_row_without_leaking_inputs(tmp_path, capsys):
    path = store(tmp_path / "copy.sqlite", 1)
    symbols = ("TSE:PRIVATE-SYMBOL", "TSE:PRIVATE-MISSING", "TSE:PRIVATE-WRAPPED")
    first = payload(list(symbols), missing=[symbols[1]])
    first["data"][symbols[0]].update(AnalystRating="NoRating", recommendation_total=0)
    first["data"][symbols[2]] = {"PRIVATE_EXTRA_KEY": "PRIVATE_VALUE"}
    first_id = retain_result(path, _sdk(first), symbols=symbols)
    second = payload(list(symbols[:2]))
    second["data"][symbols[0]].pop("close")
    second["data"][symbols[1]] = "PRIVATE_NON_OBJECT"
    second_id = retain_result(path, _sdk(second), symbols=symbols[:2])
    third = payload([symbols[0]])
    third["data"][symbols[0]].pop("AnalystRating")
    third_id = retain_result(path, _sdk(third), symbols=symbols[:1])
    # A valid but unselected receipt must not be read or enrich the selected scope.
    retain_result(path, _sdk(payload([symbols[0]])), symbols=symbols[:1])
    before = path.read_bytes()
    files = set(tmp_path.iterdir())
    args = ["--asof", DAY.isoformat(), "--sqlite", str(path)]
    selection = [
        item for rid in (first_id, second_id, third_id) for item in ("--diagnose-receipt", rid)
    ]
    assert main(args + selection) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert all(
        text not in output.out for text in ("PRIVATE", "NoRating", first_id, second_id, third_id)
    )
    r = json.loads(output.out)
    assert r["selected_receipts"] == 3
    assert (r["requested_symbol_appearances"], r["distinct_requested_symbols"]) == (6, 3)
    assert r["receipt_outcomes"] == {"incomplete_shape": 3}
    assert r["row_shapes"] == {
        "all_fields": 1,
        "some_fields": 2,
        "no_fields": 1,
        "non_object": 1,
        "explicit_missing": 1,
    }
    assert r["object_rows_with_extra_keys"] == 1
    assert r["fields"].keys() == FIELDS.keys()
    assert r["fields"]["analyst_rating"] == {
        "absent": 2,
        "null": 1,
        "non_null": 1,
        "receipts_with_absence": 2,
    }
    assert r["fields"]["quote_close"] == {
        "absent": 2,
        "null": 0,
        "non_null": 2,
        "receipts_with_absence": 2,
    }
    assert r["fields"]["recommendation_total"]["non_null"] == 1
    assert all(v["absent"] + v["null"] + v["non_null"] == 4 for v in r["fields"].values())
    assert main(args) == 0
    assert capsys.readouterr().out == "4 0\n"
    assert path.read_bytes() == before
    assert set(tmp_path.iterdir()) == files


def test_unaccounted_envelopes_and_explicit_missing_are_not_field_absences(tmp_path, capsys):
    path = store(tmp_path / "copy.sqlite", 1)
    symbol = "TSE:1000"
    mismatch = payload([symbol])
    mismatch["count"] += 1
    invalid_projection = payload([symbol])
    invalid_projection["data"][symbol]["close"] = "PRIVATE_INVALID_VALUE"

    def tool_failed(text):
        return CallToolResult(isError=True, content=[TextContent(type="text", text=text)])

    results = [
        _sdk(payload([symbol], missing=[symbol])),
        _sdk(invalid_projection),
        _sdk(mismatch),
        _sdk({"success": True}),
        tool_failed("PRIVATE_BODY"),
        tool_failed("HTTP 429 PRIVATE"),
        tool_failed("status code 403 PRIVATE"),
        tool_failed("HTTP 503 PRIVATE"),
    ]
    args = ["--asof", DAY.isoformat(), "--sqlite", str(path)]
    for result in results:
        args.extend(["--diagnose-receipt", retain_result(path, result)])
    assert main(args) == 0
    output = capsys.readouterr().out
    assert "PRIVATE" not in output
    assert symbol not in output
    r = json.loads(output)
    assert r["receipt_outcomes"] == {
        "all_explicit_missing": 1,
        "complete_shape": 1,
        "response_count_mismatch": 1,
        "malformed_envelope": 1,
        "tool_error": 1,
        "provider_http_429": 1,
        "provider_http_403": 1,
        "provider_http_other": 1,
    }
    assert r["row_shapes"] == {"all_fields": 1, "explicit_missing": 1}
    assert r["fields"]["quote_close"] == {
        "absent": 0,
        "null": 0,
        "non_null": 1,
        "receipts_with_absence": 0,
    }


@pytest.mark.parametrize(
    "case", ["missing_id", "wrong_day", "other_basis", "columns", "duplicate_id", "no_copy"]
)
def test_fixed_selection_fails_closed_without_echoing_ids(tmp_path, capsys, case):
    path = store(tmp_path / "copy.sqlite", 1)
    result = _sdk(payload(["TSE:1000"]))
    rid = retain_result(path, result)
    args = ["--asof", DAY.isoformat(), "--diagnose-receipt", rid]
    if case != "no_copy":
        args.extend(["--sqlite", str(path)])
    if case == "wrong_day":
        args[1] = (DAY + timedelta(days=1)).isoformat()
    elif case == "missing_id":
        args.extend(["--diagnose-receipt", "PRIVATE_MISSING_ID"])
    elif case == "duplicate_id":
        args.extend(["--diagnose-receipt", rid])
    elif case == "other_basis":
        args.extend(
            ["--diagnose-receipt", retain_result(path, result, master=DAY - timedelta(days=1))]
        )
    elif case == "columns":
        args.extend(["--diagnose-receipt", retain_result(path, result, columns=("close",))])
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert rid not in output.err
    assert "PRIVATE" not in output.err


def test_single_attempt_selection_is_bounded_before_opening_db(tmp_path):
    missing = tmp_path / "not-opened.sqlite"
    with pytest.raises(ValueError, match="within the limit"):
        diagnose_receipts(missing, DAY, [str(i) for i in range(101)])
    assert not missing.exists()


def test_diagnosis_rejects_copy_with_outstanding_wal(tmp_path):
    path = store(tmp_path / "copy.sqlite", 1)
    rid = retain_result(path, _sdk(payload(["TSE:1000"])))
    path.with_name("copy.sqlite-wal").touch()
    with pytest.raises(ValueError, match="still has a journal"):
        diagnose_receipts(path, DAY, [rid])
