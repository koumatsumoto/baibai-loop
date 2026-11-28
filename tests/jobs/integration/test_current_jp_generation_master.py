from __future__ import annotations

import json
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.financial_native import fixed_empty_financial_input, seed_empty_financial
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.model_generation import materialize_generation
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.machine_analysis.generation.inputs as generation
import baibai_jobs.integration.models as job
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.machine_analysis.generation.contracts import ModelGeneration
from baibai_engine.machine_analysis.generation.edinet_inputs import read_edinet_input
from baibai_engine.machine_analysis.generation.query import read_generation
from baibai_engine.machine_analysis.generation.runners import market_snapshot
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.stocks.contracts import MasterSnapshot
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    JapanListingAttributes,
    ObservedListing,
    refresh_listing_slice,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    CurrentListingMarket,
    encode_current_listing_master,
)
from baibai_engine.machine_analysis.observations.stocks.models import SecurityMaster
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.cli import main as jobs_cli
from baibai_jobs.integration.listing_master_current import LISTING_MASTER_CURRENT_KEY

_DAY = date(2026, 10, 23)
_PRIOR = _DAY - timedelta(weeks=1)
_RECEIVED = datetime(2026, 10, 23, 7, tzinfo=UTC)
_COLS = (
    ("1001", "合成機械A", "プライム", "機械", "011"),
    ("1002", "合成機械B", "スタンダード", "機械", "011"),
    ("1321", "合成比較ETF", "ETF・ETN", "その他", "014"),
)


def _populate_listing(path, *, day=_DAY, received_at=_RECEIVED, rename=None):
    observations = tuple(
        ObservedListing(
            ("XTKS", f"{ticker}0"),
            rename if ticker == "1001" and rename is not None else name,
            "common_stock" if category == "011" else "etf",
            JapanListingAttributes(category, market, sector),
        )
        for ticker, name, market, sector, category in _COLS
    )
    listing = refresh_listing_slice(
        None, market="JP", session=day, observations=observations, complete=True
    ).slice
    section = CurrentListingMarket(listing, "jquants-jpx", received_at, len(observations))
    path.write_bytes(encode_current_listing_master({"JP": section}))


def _observe_listing_reads(monkeypatch):
    calls = []
    read = job.read_local_listing_master

    def tracked(path):
        calls.append(path)
        return read(path)

    monkeypatch.setattr(job, "read_local_listing_master", tracked)
    return calls


@pytest.fixture
def run_inputs(tmp_path):
    rows = [
        (
            ticker,
            (_DAY - timedelta(days=offset)).isoformat(),
            start + 0.2 * (90 - offset),
            1000.0,
        )
        for ticker, start in (("1001", 100.0), ("1002", 200.0), ("1321", 300.0), ("5555", 400.0))
        for offset in range(90, -1, -1)
    ]
    market = market_store(tmp_path / "market.sqlite", daily_bars=rows)
    with closing(open_connection(market)) as connection, connection:
        connection.executemany(
            "INSERT OR REPLACE INTO jquants_market_calendar VALUES (?, ?)",
            [(_DAY.isoformat(), "1"), (_PRIOR.isoformat(), "1")],
        )
    financial = fixed_empty_financial_input(tmp_path / "financial", _DAY)
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=tmp_path / "mirror") as seal:
        yield dict(
            generation_id="same-generation",
            financial_input=financial,
            market_snapshot=seal,
            market_release=InputRelease(release_id="same-release", manifest_sha256="a" * 64),
            economic_input=None,
            as_of=_DAY,
            evaluation_time=_RECEIVED + timedelta(hours=1),
            ttm_rules=DEFAULT_TTM_RULES,
        )


def test_pinned_l2_keeps_exact_session_and_never_reuses_history(tmp_path, run_inputs, monkeypatch):
    path = tmp_path / "current.json"
    _populate_listing(path)
    calls = _observe_listing_reads(monkeypatch)
    pinned = job.pin_current_jp_generation_master(path)
    assert calls == [path]
    # A replacement after the pin must not affect either name or membership.
    _populate_listing(path, received_at=_RECEIVED + timedelta(minutes=1), rename="別の新名称")
    stream = generation.read_and_generate_models(**run_inputs, current_master=pinned)
    assert stream.subjects == ("1001", "1002")
    saved = materialize_generation(stream)
    assert calls == [path]
    assert saved.result("market_context", "1001").payload["master"]["name"] == "合成機械A"
    market = saved.result("market_context", "market").payload["market"]
    older, latest = market["points"][-2:]
    assert latest["observation_date"] == _DAY.isoformat()
    assert latest["breadth_sample_size"] == 2
    assert older["observation_date"] == _PRIOR.isoformat()
    assert older["breadth_pct_above_ma20"] is None
    assert older["breadth_sample_size"] == 0
    assert older["benchmark_return_20d"] is not None
    check = saved.inputs["master"]
    assert check == pinned.input_identity()
    assert check.kind == "current_listing_master"
    assert check.reference == f"{LISTING_MASTER_CURRENT_KEY}#JP"
    assert check.descriptor["session"] == _DAY.isoformat()
    assert check == ModelGeneration.model_validate_json(saved.model_dump_json()).inputs["master"]


@pytest.mark.parametrize("case", ["missing", "stale_session", "late_receipt", "descriptor_only"])
def test_new_l2_pin_fails_without_falling_back(tmp_path, run_inputs, case):
    path = tmp_path / "current.json"

    if case == "missing":
        with pytest.raises(FileNotFoundError):
            job.pin_current_jp_generation_master(path)
        return
    if case == "stale_session":
        _populate_listing(path, day=_PRIOR)
    elif case == "late_receipt":
        # Receipt is visible to the nominal clock but not to the sealed input.
        seal = run_inputs["market_snapshot"]
        late = seal.sealed_at + timedelta(seconds=1)
        run_inputs["evaluation_time"] = late
        _populate_listing(path, received_at=late)
    else:
        _populate_listing(path)
        identity = job.pin_current_jp_generation_master(path).input_identity()
        with pytest.raises(TypeError, match="master_identity"):
            generation.read_and_generate_models(**run_inputs, master_identity=identity)
        return
    pinned = job.pin_current_jp_generation_master(path)
    with pytest.raises(ValueError, match="does not match the generation seal and session"):
        generation.read_and_generate_models(**run_inputs, current_master=pinned, tickers=("1001",))


def test_bare_snapshot_has_prices_without_classification(run_inputs):
    snapshot = market_snapshot.build_market_snapshot(
        sqlite_path=run_inputs["market_snapshot"].path, asof_date=_DAY
    )
    assert snapshot["master_snapshot_date"] is None
    assert snapshot["sectors"] == []
    assert snapshot["points"][-1]["observation_date"] == _DAY.isoformat()
    assert snapshot["points"][-1]["breadth_sample_size"] == 0


@pytest.mark.parametrize("has_benchmark", [True, False])
@pytest.mark.parametrize("price_lag", [0, 1])
def test_current_sector_breadth_requires_same_price_day_even_on_weekend(
    tmp_path, has_benchmark, price_lag
):
    sunday = _DAY + timedelta(days=2)
    endpoint = _DAY - timedelta(days=price_lag)
    source = tmp_path / "never-read.sqlite"
    master = MasterSnapshot(
        _DAY,
        {
            ticker: SecurityMaster(ticker, name, market, sector, category == "011")
            for ticker, name, market, sector, category in _COLS
        },
    )
    prices = {
        ticker: [
            PriceBar(endpoint - timedelta(days=offset), base + (70 - offset), None)
            for offset in range(70, -1, -1)
        ]
        for ticker, base in (("1001", 100), ("1002", 200), ("1321", 300))
        if has_benchmark or ticker != "1321"
    }
    prepared = market_snapshot.prepare_market_context_from_bars(
        sqlite_path=source,
        asof_date=sunday,
        master=master,
        bars_by_ticker=prices.items(),
        subjects=("1001", "1002"),
        history_weeks=2,
    )
    result = market_snapshot.build_market_snapshot(
        sqlite_path=source,
        asof_date=sunday,
        prepared=prepared,
        history_weeks=2,
        min_breadth_sample=1,
    )
    point = result["points"][-1]
    assert (point["breadth_pct_above_ma20"], point["breadth_sample_size"]) == (
        (1.0, 2) if has_benchmark and not price_lag else (None, 0)
    )
    if price_lag:
        assert result["sectors"] == []
    else:
        (sector,) = result["sectors"]
        assert sector["observation_date"] == _DAY.isoformat()
        assert (sector["pct_above_ma20"], sector["sample_sizes"]["pct_above_ma20"]) == (1.0, 2)


def _run_cli(root, name, *, listing=None):
    args = [
        "model-integration",
        "--asof",
        _DAY.isoformat(),
        "--market",
        str(root / "market.sqlite"),
        "--economic",
        str(root / "economic.sqlite"),
        "--runs",
        str(root / f"{name}.sqlite"),
        "--mirror",
        str(root / "job-mirror"),
        "--lease-handle",
        str(root / "lease"),
        "--integration-report",
        str(root / "integration.json"),
        "--notice-output",
        str(root / f"{name}.json"),
    ]
    if listing is not None:
        args += ["--current-listing-master", str(listing)]
    status = jobs_cli(args)
    return status, json.loads((root / f"{name}.json").read_bytes())


@pytest.fixture
def cli_case(tmp_path, run_inputs, monkeypatch):
    narrow_release_policy(monkeypatch)
    initialize_database(tmp_path / "economic.sqlite").close()
    store = MemoryR2Store()
    seed_empty_financial(store, tmp_path / "financial-cli", _DAY)
    edinet = empty_edinet_input(store, tmp_path)
    (tmp_path / "integration.json").write_text(
        json.dumps({"asof": _DAY.isoformat(), "edinet_input": edinet.model_dump(mode="json")})
    )
    listing = tmp_path / "current.json"
    seal = run_inputs["market_snapshot"]
    known = seal.sealed_at + timedelta(minutes=1)
    prepared = SimpleNamespace(
        serving=SimpleNamespace(pointer=None),
        release=SimpleNamespace(
            release_id="no-master-fixture",
            datasets={"jquants.daily_bars": object()},
        ),
        release_sha256="b" * 64,
        source_snapshot=seal,
        edinet_input=None,
        price_identity=None,
        calendar_identity=None,
        margin_alerts_identity=None,
        margin_interest_identity=None,
        short_sale_reports_identity=None,
    )
    calls = []

    class FakeClient:
        def __new__(cls, **_kwargs):
            return store

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz):
            return known.astimezone(tz)

    @contextmanager
    def market_preparation(**args):
        calls.append("prepare")
        prepared.edinet_input = read_edinet_input(args["store"], args["edinet_input"])
        if listing.exists():
            # The CLI already decoded this file. Replacing it cannot change this run.
            _populate_listing(listing, rename="次の実行の名称", received_at=known)
        yield prepared

    def stage(prepared_release, **_):
        assert prepared_release is prepared
        calls.append("stage")
        return object(), SimpleNamespace(
            as_dict=lambda: {"release_id": prepared.release.release_id}
        )

    def publish(**kwargs):
        calls.append("publish")
        saved = read_generation(f"models-{prepared.release.release_id}", path=kwargs["runs"])
        assert saved is not None  # Real local ModelGenerationStore/Screening ran before this seam.
        return {}

    monkeypatch.setattr(job, "Boto3R2Store", FakeClient)
    monkeypatch.setattr(job, "datetime", FixedClock)
    monkeypatch.setattr(job, "read_machine_current", lambda store: None)
    monkeypatch.setattr(job, "_load_handle", lambda path: object())
    monkeypatch.setattr(job, "verify_held", lambda *_: None)
    monkeypatch.setattr(job, "prepare_market_lake", market_preparation)
    monkeypatch.setattr(job, "stage_prepared_market_lake", stage)
    monkeypatch.setattr(job, "_publish_machine", publish)
    return SimpleNamespace(listing=listing, prepared=prepared, calls=calls, seal=seal, store=store)


def test_jobs_cli_generates_stock_models_from_one_fixed_current_file(
    tmp_path, monkeypatch, cli_case
):
    _populate_listing(cli_case.listing)
    reads = _observe_listing_reads(monkeypatch)

    status, notice = _run_cli(tmp_path, "fixed", listing=cli_case.listing)
    assert status == 0
    assert notice["models_ready"] is True
    assert notice["company_count"] == 2
    new = read_generation(notice["generation_id"], path=tmp_path / "fixed.sqlite")
    assert reads == [cli_case.listing]
    assert LISTING_MASTER_CURRENT_KEY not in cli_case.store.get_keys
    assert cli_case.calls == ["prepare", "stage", "publish"]
    assert new.inputs["market_snapshot"].sha256 == cli_case.seal.ref.sha256
    assert new.inputs["master"].kind == "current_listing_master"
    assert new.result("market_context", "1001").payload["master"]["name"] == "合成機械A"


@pytest.mark.parametrize("case", ["missing_file", "malformed_file", "future_receipt"])
def test_jobs_cli_bad_artifact_never_publishes(tmp_path, monkeypatch, cli_case, case):
    if case == "malformed_file":
        cli_case.listing.write_bytes(b'{"JP":{"bad":"shape"}}')
    elif case == "future_receipt":
        # Visible by the nominal model clock but not by the frozen calendar/price seal.
        _populate_listing(
            cli_case.listing, received_at=cli_case.seal.sealed_at + timedelta(seconds=1)
        )
    if case in ("missing_file", "malformed_file"):
        monkeypatch.setattr(job, "Boto3R2Store", lambda **kwargs: pytest.fail("bad file opened R2"))

    result, notice = _run_cli(tmp_path, "failed", listing=cli_case.listing)
    assert result == 1
    assert notice["models_ready"] is False
    assert cli_case.calls == (["prepare"] if case == "future_receipt" else [])
    assert not (tmp_path / "failed.sqlite").exists()
