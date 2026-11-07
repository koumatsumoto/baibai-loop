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
            "financial_summaries": [
                {"Code": "13010", "DiscDate": DAY.isoformat(), "DiscNo": "1", "Sales": 180}
            ],
            "market_calendar": [{"Date": DAY.isoformat(), "HolDiv": "1"}],
            "short_sale_reports": [],
            "margin_alerts": [],
        }[source]
        yield JQuantsPage(source, query, AT, json.dumps({"data": rows}).encode())


@pytest.fixture
def refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("BAIBAI_PRICE_NATIVE", "false")
    monkeypatch.setenv("BAIBAI_FINANCIAL_NATIVE", "false")
    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "false")
    path = tmp_path / "stores" / "market" / "market.sqlite"
    open_connection(path).close()
    client = NativeClient()
    return JQuantsRefresh(path, None, client=client), client, path


def invoke(refresh, method):
    arguments = {"revision_overlap_days": 7} if method == "refresh_fin_summaries" else {}
    return getattr(refresh, method)(DAY, DAY, **arguments)


@pytest.mark.parametrize(
    ("method", "flag", "command"),
    [
        ("ensure_daily_bars", "BAIBAI_PRICE_NATIVE", "jquants-price-native"),
        ("ensure_fin_summaries", "BAIBAI_FINANCIAL_NATIVE", "jquants-financial-native"),
        ("refresh_fin_summaries", "BAIBAI_FINANCIAL_NATIVE", "jquants-financial-native"),
    ],
)
@pytest.mark.parametrize("covered", [False, True])
def test_native_writer_guard_precedes_coverage_get_and_mutation(
    refresh, monkeypatch, method, flag, command, covered
):
    maintenance, client, path = refresh
    if covered:
        assert invoke(maintenance, method) == 1
    before = path.read_bytes()
    client.calls.clear()
    monkeypatch.setenv(flag, "true")
    with pytest.raises(JQuantsProviderError, match=command):
        invoke(maintenance, method)
    assert client.calls == []
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "method", ["ensure_daily_bars", "ensure_fin_summaries", "refresh_fin_summaries"]
)
def test_legacy_mode_still_retains_originals_and_projects_requested_day(refresh, method):
    maintenance, client, path = refresh
    assert invoke(maintenance, method) == 1
    assert len(client.calls) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jquants_source_pages").fetchone()[0] == 1
        if method == "ensure_daily_bars":
            assert connection.execute("SELECT close FROM jquants_daily_bars").fetchone() == (220,)
        else:
            payload = connection.execute(
                "SELECT document_payload FROM jquants_fin_summaries"
            ).fetchone()[0]
            assert json.loads(payload)["Sales"] == 180


def prepare_cli(refresh, tmp_path, monkeypatch):
    maintenance, client, path = refresh
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JQUANTS_API_KEY", "synthetic-key")
    monkeypatch.setenv("EDINET_API_KEY", "")
    monkeypatch.setattr(cli, "load_project_env", lambda: None)
    monkeypatch.setattr(cli, "JQuantsRefresh", lambda *_args, **_kwargs: maintenance)
    return client, path


@pytest.mark.parametrize("native", [False, True])
def test_financial_bootstrap_cli_reports_cutover_instead_of_legacy_success(
    refresh, tmp_path, monkeypatch, capsys, native
):
    client, path = prepare_cli(refresh, tmp_path, monkeypatch)
    monkeypatch.setenv("BAIBAI_FINANCIAL_NATIVE", str(native).lower())
    # One synthetic day exercises the real planning/receive/project/CLI chain.
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
    assert result == int(native)
    if native:
        assert "jquants-financial-native" in output.err
        assert "rebuild-financial-native" in output.err
        assert "bootstrap-cache done" not in output.out
        assert client.calls == []
        assert path.read_bytes() == before
    else:
        assert "bootstrap-cache done" in output.out
        assert [source for source, _ in client.calls] == ["financial_summaries"]


def test_backfill_cli_reports_both_native_sources_and_continues_other_sources(
    refresh, tmp_path, monkeypatch, capsys
):
    client, path = prepare_cli(refresh, tmp_path, monkeypatch)
    monkeypatch.setenv("BAIBAI_PRICE_NATIVE", "true")
    monkeypatch.setenv("BAIBAI_FINANCIAL_NATIVE", "true")
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
