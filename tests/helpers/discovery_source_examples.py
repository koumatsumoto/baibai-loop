"""価格・原資料・旧表示を区別するための合成入力。経済計算や旧順位は持たない。"""

NOTICE = {
    "disclosure_number": "synthetic-accounting-change",
    "disclosed_at": "2026-01-05",
    "disclosed_time": None,
    "period_start": "2025-01-01",
    "period_end": "2025-12-31",
    "document_payload_sha256": "b" * 64,
    "flags": {
        "ChgByASRev": "",
        "ChgNoASRev": "true",
        "ChgAcEst": None,
        "RetroRst": "false",
    },
}

BOOK = {
    "amount_yen": 20000.0,
    "method": "bps_times_current_shares",
    "measurement_date": "2025-06-30",
    "statement_basis": "consolidated",
    "accounting_standard": "JP",
    "disclosure_number": "synthetic-book-source",
    "disclosed_at": "2025-08-12",
    "disclosed_time": "15:00:00",
    "document_type": "2QFinancialStatements_Consolidated_JP",
    "operands": {"bps_yen": 200.0, "shares_ex_treasury": 100.0, "share_basis_as_of": "2026-01-05"},
}


CASH_FLOW = {
    "cfo": -200.0,
    "cfo_source": {
        "period_start": "2026-04-01",
        "period_end": "2026-09-30",
        "period_type": "2Q",
        "accounting_standard": "JP",
        "statement_basis": "consolidated",
        "disclosed_at": "2026-11-13",
        "disclosed_time": "15:30:00",
        "disclosure_number": "synthetic-cash-flow",
        "source_trace": {"unknown_field": None},
    },
    "ocf_ttm": -500.0,
    "ocf_ttm_period": {
        "start": "2025-10-01",
        "end": "2026-09-30",
        "method": "fy_plus_current_ytd_minus_prior_ytd",
        "components": [{"amount_yen": -200.0, "coefficient": 1, "source_id": "synthetic"}],
    },
    "ttm_quality_ocf_yield": "unavailable",
}

FORECAST = {
    "forecast_profit_yen": -1000.0,
    "forecast_eps_yen": -10.5,
    "forecast_ordinary_profit_yen": 1500.0,
    "forecast_full_year_loss_flag": True,
    "earnings_forecast_source": {
        "status": "confirmed",
        "period_start": "2026-06-01",
        "period_end": "2027-05-31",
        "basis": "consolidated",
        "target_basis": "consolidated",
        "disclosed_at": "2026-10-15",
        "disclosed_time": "15:30:00",
        "disclosure_number": "synthetic-forecast",
        "forecast_profit_source_records": "synthetic-forecast",
        "forecast_eps_source_records": "synthetic-forecast",
        "scope_trace": {"unverified_detail": None},
    },
}

SUBJECT = "industry:synthetic-survey"

BASIS = "One group business; not whole-company exposure or survey membership"
