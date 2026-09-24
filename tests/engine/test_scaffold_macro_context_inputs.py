from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml

from baibai_engine.macro.context.scaffold_inputs import main as scaffold_main
from baibai_engine.macro.indicators.db import (
    ObservationRecord,
    initialize_database,
    insert_observations,
)
from baibai_engine.macro.indicators.definitions import load_definitions


def _definition(series_id: str):
    return next(
        definition for definition in load_definitions().series if definition.series_id == series_id
    )


def _build_store(path: Path) -> None:
    us10y = _definition("us.10y")
    usd_jpy = _definition("usd_jpy")
    connection = initialize_database(path)
    try:
        insert_observations(
            connection,
            [
                ObservationRecord(
                    series_id=us10y.series_id,
                    observed_at=date(2026, 7, 23),
                    value=4.71,
                    unit=us10y.unit,
                    source_url=us10y.source_url,
                    vintage_at=datetime(2026, 7, 24, 9, tzinfo=UTC),
                ),
                # A later vintage restating the same observation: the scaffold must
                # cite the latest ok vintage as the citation's published_at.
                ObservationRecord(
                    series_id=us10y.series_id,
                    observed_at=date(2026, 7, 23),
                    value=4.72,
                    unit=us10y.unit,
                    source_url=us10y.source_url,
                    vintage_at=datetime(2026, 7, 25, 9, tzinfo=UTC),
                ),
                ObservationRecord(
                    series_id=usd_jpy.series_id,
                    observed_at=date(2026, 7, 27),
                    value=163.6,
                    unit=usd_jpy.unit,
                    source_url=usd_jpy.source_url,
                    vintage_at=datetime(2026, 7, 27, 16, tzinfo=UTC),
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _write_spec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_scaffold_derives_inputs_from_store_and_aggregates_sections(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "macro.sqlite"
    _build_store(db_path)
    spec = _write_spec(
        tmp_path / "spec.yaml",
        "\n".join(
            (
                "asof: 2026-07-27",
                "accessed_at: 2026-07-28T14:10:00+09:00",
                "sections:",
                "  金利・金融政策:",
                "    - us.10y",
                "  為替:",
                "    - usd_jpy",
                "    - us.10y",
            )
        ),
    )

    assert scaffold_main([str(spec), "--db", str(db_path)]) == 0

    entries = yaml.safe_load(capsys.readouterr().out)
    by_series = {entry["series_id"]: entry for entry in entries}
    assert set(by_series) == {"us.10y", "usd_jpy"}

    us10y = by_series["us.10y"]
    assert us10y["input_id"] == "series-us-10y"
    assert us10y["provider"] == _definition("us.10y").provider
    assert us10y["observation_as_of"] == "2026-07-23"
    assert us10y["published_at"] == "2026-07-25T09:00:00+00:00"
    assert us10y["accessed_at"] == "2026-07-28T14:10:00+09:00"
    assert us10y["status"] == "ok"
    assert us10y["used_for"] == "金利・金融政策・為替 の fact 根拠"
    assert "asof 2026-07-27" in us10y["window"]

    assert by_series["usd_jpy"]["used_for"] == "為替 の fact 根拠"
    assert by_series["usd_jpy"]["input_id"] == "series-usd-jpy"


def test_scaffold_rejects_a_series_the_registry_does_not_know(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "macro.sqlite"
    _build_store(db_path)
    spec = _write_spec(
        tmp_path / "spec.yaml",
        "\n".join(
            (
                "asof: 2026-07-27",
                "sections:",
                "  金利・金融政策:",
                "    - not.registered",
            )
        ),
    )

    assert scaffold_main([str(spec), "--db", str(db_path)]) == 1
    assert "unregistered series: not.registered" in capsys.readouterr().err


def test_scaffold_rejects_a_series_without_an_observation_at_the_asof(
    tmp_path: Path, capsys
) -> None:
    # jp.10y is registered but this store holds no observation for it, and a
    # citation without an observation would fabricate provenance.
    db_path = tmp_path / "macro.sqlite"
    _build_store(db_path)
    spec = _write_spec(
        tmp_path / "spec.yaml",
        "\n".join(
            (
                "asof: 2026-07-27",
                "sections:",
                "  金利・金融政策:",
                "    - jp.10y",
            )
        ),
    )

    assert scaffold_main([str(spec), "--db", str(db_path)]) == 1
    assert "no observation at or before asof: jp.10y" in capsys.readouterr().err


def test_scaffold_writes_the_list_to_the_requested_output(tmp_path: Path) -> None:
    db_path = tmp_path / "macro.sqlite"
    _build_store(db_path)
    spec = _write_spec(
        tmp_path / "spec.yaml",
        "\n".join(
            (
                "asof: 2026-07-27",
                "sections:",
                "  為替:",
                "    - usd_jpy",
            )
        ),
    )
    output = tmp_path / "inputs.yaml"

    assert scaffold_main([str(spec), "--db", str(db_path), "--output", str(output)]) == 0

    entries = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert [entry["series_id"] for entry in entries] == ["usd_jpy"]


@pytest.mark.parametrize(
    ("series_id", "expected_vintage"),
    [
        ("jp.foreign_flows", "2026-08-29T09:00:00+00:00"),
        ("us.10y", "2026-09-10T09:00:00+00:00"),
    ],
)
def test_scaffold_cites_the_readers_selected_vintage(
    tmp_path: Path, capsys, series_id: str, expected_vintage: str
) -> None:
    definition = _definition(series_id)
    database = tmp_path / "macro.sqlite"
    connection = initialize_database(database)
    try:
        insert_observations(
            connection,
            [
                ObservationRecord(
                    series_id=series_id,
                    observed_at=date(2026, 8, 23),
                    value=value,
                    unit=definition.unit,
                    source_url=definition.source_url,
                    vintage_at=vintage,
                )
                for value, vintage in (
                    (4.0, datetime(2026, 8, 29, 9, tzinfo=UTC)),
                    (4.1, datetime(2026, 9, 10, 9, tzinfo=UTC)),
                )
            ],
        )
        connection.commit()
    finally:
        connection.close()
    spec = _write_spec(
        tmp_path / "spec.yaml",
        f"asof: 2026-08-31\nsections:\n  確認:\n    - {series_id}\n",
    )
    assert scaffold_main([str(spec), "--db", str(database)]) == 0
    (entry,) = yaml.safe_load(capsys.readouterr().out)
    assert entry["observation_as_of"] == "2026-08-23"
    assert entry["published_at"] == expected_vintage


@pytest.mark.parametrize("retracted", [False, True])
def test_scaffold_does_not_cite_ineligible_pit_observations(
    tmp_path: Path, capsys, retracted: bool
) -> None:
    definition = _definition("jp.foreign_flows")
    database = tmp_path / "macro.sqlite"
    connection = initialize_database(database)
    records = [
        ObservationRecord(
            series_id=definition.series_id,
            observed_at=date(2026, 8, 23),
            value=4.1,
            unit=definition.unit,
            source_url=definition.source_url,
            vintage_at=datetime(2026, 9, 10, 9, tzinfo=UTC),
        )
    ]
    if retracted:
        records.extend(
            ObservationRecord(
                series_id=definition.series_id,
                observed_at=date(2026, 8, 23),
                value=4.0,
                unit=definition.unit,
                source_url=definition.source_url,
                vintage_at=datetime(2026, 8, day, 9, tzinfo=UTC),
                fetch_status=status,
            )
            for day, status in ((28, "ok"), (29, "retracted"))
        )
    try:
        insert_observations(connection, records)
        connection.commit()
    finally:
        connection.close()
    spec = _write_spec(
        tmp_path / "spec.yaml",
        "asof: 2026-08-31\nsections:\n  確認:\n    - jp.foreign_flows\n",
    )
    assert scaffold_main([str(spec), "--db", str(database)]) == 1
    assert "no observation at or before asof: jp.foreign_flows" in capsys.readouterr().err
