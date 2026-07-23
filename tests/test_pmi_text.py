from __future__ import annotations

from datetime import date

import pytest
from tools.macro.pmi_text import extract_pmi_value, latest_seed_matches


def test_extract_pmi_value_keeps_previous_month_value() -> None:
    text = (
        "Japan Manufacturing PMI data were collected 2024 April. "
        "The headline Japan Manufacturing PMI recorded 49.6 in April. "
        "The index was noticeably higher than March’s 48.2."
    )

    assert extract_pmi_value(
        text,
        expected_observed_at=date(2024, 3, 1),
        release_observed_at=date(2024, 4, 1),
    ) == pytest.approx(48.2)


def test_extract_pmi_value_rejects_unbound_previous_value() -> None:
    text = (
        "Japan Manufacturing PMI data were collected 2024 April. "
        "Output rose from 44.0. "
        "The headline Japan Manufacturing PMI recorded 49.6 in April."
    )

    assert (
        extract_pmi_value(
            text,
            expected_observed_at=date(2024, 3, 1),
            release_observed_at=date(2024, 4, 1),
        )
        is None
    )


def test_extract_pmi_value_rejects_unrelated_month_value_near_headline() -> None:
    text = (
        "The headline Japan Manufacturing PMI was little-changed in October. "
        "The output index recorded 44.0 in October. "
        "After accounting for seasonal factors, the index recorded 48.7, "
        "up from 48.5."
    )

    assert (
        extract_pmi_value(
            text,
            expected_observed_at=date(2023, 10, 1),
            release_observed_at=date(2023, 10, 1),
        )
        is None
    )


def test_extract_pmi_value_reads_current_month() -> None:
    text = (
        "The headline au Jibun Bank Japan Manufacturing PMI posted 51.6 in March. "
        "Japan Manufacturing PMI data were collected March 2026."
    )

    assert extract_pmi_value(
        text,
        expected_observed_at=date(2026, 3, 1),
        release_observed_at=date(2026, 3, 1),
    ) == pytest.approx(51.6)


def test_extract_pmi_value_reads_linked_prior_value() -> None:
    text = (
        "The headline au Jibun Bank Japan Manufacturing PMI was little-changed "
        "in October. After accounting for seasonal factors, the index recorded "
        "48.7, up from 48.5."
    )

    assert extract_pmi_value(
        text,
        expected_observed_at=date(2023, 9, 1),
        release_observed_at=date(2023, 10, 1),
    ) == pytest.approx(48.5)


def test_extract_pmi_value_reads_linked_current_value() -> None:
    text = (
        "The headline au Jibun Bank Japan Manufacturing PMI was little-changed "
        "in October. After accounting for seasonal factors, the index recorded "
        "48.7, up from 48.5."
    )

    assert extract_pmi_value(
        text,
        expected_observed_at=date(2023, 10, 1),
        release_observed_at=date(2023, 10, 1),
    ) == pytest.approx(48.7)


def test_latest_seed_matches_compares_only_latest_vintage() -> None:
    entries = [
        {
            "value": 48.2,
            "source_url": "https://example.test/release",
            "entered_at": "2026-07-20T00:00:00+00:00",
        },
        {
            "value": 48.3,
            "source_url": "https://example.test/release",
            "entered_at": "2026-07-21T00:00:00+00:00",
        },
    ]

    assert not latest_seed_matches(
        entries,
        value=48.2,
        source_url="https://example.test/release",
    )
    assert latest_seed_matches(
        entries,
        value=48.3,
        source_url="https://example.test/release",
    )
