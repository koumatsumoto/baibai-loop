"""native切替後の旧maintenanceは成功を装わず、原本取得前に新入口を案内する。"""

import json
import sqlite3
from datetime import UTC, date, datetime

import pytest

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.maintenance.market import cache, cli
from baibai_jobs.maintenance.source_refresh.jquants import JQuantsRefresh

DAY = date(2026, 10, 30)
AT = datetime(2026, 11, 1, tzinfo=UTC)


class NativeClient:
    def __init__(self):
        self.calls = []

    def close(self):
        pass

    def pages(self, source, query):
        self.calls.append((source, dict(query)))
        rows = {
            "daily_bars": [{"Code": "13010", "Date": DAY.isoformat(), "C": 220, "AdjFactor": 1}],
            "market_calendar": [{"Date": DAY.isoformat(), "HolDiv": "1"}],
            "short_sale_reports": [],
            "margin_alerts": [],
        }[source]
        yield JQuantsPage(source, query, AT, json.dumps({"data": rows}).encode())


@pytest.fixture
def refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "false")
    path = tmp_path / "stores" / "market" / "market.sqlite"
    open_connection(path).close()
    client = NativeClient()
    return JQuantsRefresh(path, None, client=client), client, path


def invoke(refresh, method):
    arguments = {"revision_overlap_days": 7} if method == "refresh_fin_summaries" else {}
    return getattr(refresh, method)(DAY, DAY, **arguments)


@pytest.mark.parametrize("method", ["ensure_fin_summaries", "refresh_fin_summaries"])
@pytest.mark.parametrize("old_flag", [None, "false", "true", "invalid"])
@pytest.mark.parametrize("covered", [False, True])
def test_financial_writer_is_permanently_retired_before_get_or_mutation(
    refresh, monkeypatch, method, old_flag, covered
):
    from baibai_engine.machine_analysis.observations.jquants_writer import (
        store_jquants_fin_summaries,
    )

    maintenance, client, path = refresh
    if old_flag is None:
        monkeypatch.delenv("BAIBAI_FINANCIAL_NATIVE", raising=False)
    else:
        monkeypatch.setenv("BAIBAI_FINANCIAL_NATIVE", old_flag)
    if covered:
        store_jquants_fin_summaries(path, [], requested_start=DAY, requested_end=DAY)
    before = path.read_bytes()
    with pytest.raises(JQuantsProviderError, match="jquants-financial-native"):
        invoke(maintenance, method)
    assert client.calls == []
    assert path.read_bytes() == before


def prepare_cli(refresh, tmp_path, monkeypatch):
    maintenance, client, path = refresh
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JQUANTS_API_KEY", "synthetic-key")
    monkeypatch.setenv("EDINET_API_KEY", "")
    monkeypatch.setattr(cli, "load_project_env", lambda: None)
    monkeypatch.setattr(cli, "JQuantsRefresh", lambda *_args, **_kwargs: maintenance)
    return client, path


@pytest.mark.parametrize("old_flag", [None, "false", "true", "invalid"])
def test_financial_bootstrap_cli_reports_cutover_instead_of_legacy_success(
    refresh, tmp_path, monkeypatch, capsys, old_flag
):
    client, path = prepare_cli(refresh, tmp_path, monkeypatch)
    if old_flag is None:
        monkeypatch.delenv("BAIBAI_FINANCIAL_NATIVE", raising=False)
    else:
        monkeypatch.setenv("BAIBAI_FINANCIAL_NATIVE", old_flag)
    # The old CLI reports retirement before any provider request.
    monkeypatch.setattr(cache, "FIN_INPUT_WINDOW_DAYS", 0)
    monkeypatch.setattr(cache, "NORMALIZED_EPS_HISTORY_WINDOW_DAYS", 0)
    before = path.read_bytes()
    result = cli.main(
        [
            "bootstrap-cache",
            "--asof",
            DAY.isoformat(),
            "--source-group",
            "jquants-financial",
            "--sqlite-path",
            str(path),
        ]
    )
    output = capsys.readouterr()
    assert result == 1
    assert "jquants-financial-native" in output.err
    assert "rebuild-financial-native" in output.err
    assert "bootstrap-cache done" not in output.out
    assert client.calls == []
    assert path.read_bytes() == before


def test_backfill_cli_reports_both_native_sources_and_continues_other_sources(
    refresh, tmp_path, monkeypatch, capsys
):
    client, path = prepare_cli(refresh, tmp_path, monkeypatch)
    result = cli.main(["backfill-history", "--start", DAY.isoformat(), "--end", DAY.isoformat()])
    output = capsys.readouterr()
    assert result == 1
    assert "jquants-price-native" in output.err
    assert "rebuild-price-native" in output.err
    assert "jquants-financial-native" in output.err
    assert "rebuild-financial-native" in output.err
    assert "backfill-history done" not in output.out
    assert [source for source, _ in client.calls] == [
        "market_calendar",
    ]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM jquants_fin_summaries").fetchone()[0] == 0
        sources = {
            row[0] for row in connection.execute("SELECT DISTINCT source FROM jquants_source_pages")
        }
        assert sources == {"market_calendar"}


@pytest.mark.parametrize("old_setting", [None, "false", "true", "invalid"])
@pytest.mark.parametrize("covered", [False, True])
def test_price_writer_is_retired_regardless_of_old_setting(
    refresh, monkeypatch, old_setting, covered
):
    maintenance, client, path = refresh
    if old_setting is None:
        monkeypatch.delenv("BAIBAI_PRICE_NATIVE", raising=False)
    else:
        monkeypatch.setenv("BAIBAI_PRICE_NATIVE", old_setting)
    if covered:
        from baibai_engine.machine_analysis.observations.jquants_writer import (
            store_jquants_daily_bars,
        )

        store_jquants_daily_bars(
            path,
            [{"Code": "13010", "Date": DAY.isoformat(), "C": 220}],
            requested_start=DAY,
            requested_end=DAY,
        )
    before = path.read_bytes()
    with pytest.raises(
        JQuantsProviderError, match=r"jquants-price-native acquire.*rebuild-price-native"
    ):
        maintenance.ensure_daily_bars(DAY, DAY)
    assert client.calls == []
    assert path.read_bytes() == before
