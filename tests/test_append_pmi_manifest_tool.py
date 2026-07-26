from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from tools.append_pmi_manifest import (
    Candidate,
    IndexEntry,
    _append_verified_entry,
    manifest_with_entry,
    newest_candidate,
    observed_month_of,
    parse_release_index,
    resolve_stream,
)

from baibai_engine.macro.indicators.providers.base import IndicatorsProviderError
from baibai_engine.macro.indicators.providers.spglobal_pmi import load_manifest

_INDEX_HTML = """
<div class="listItem">
    <span class="releaseDate">July&nbsp;01&nbsp;2026&nbsp;00:30&nbsp;UTC</span>
    <span class="releaseTitle">S&amp;P Global Japan Manufacturing PMI</span>
    <span class="greenListItem"><a href="https://www.pmi.spglobal.com/Public/Home/PressRelease/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" target="_blank">View More</a></span>
</div>
<div class="listItem">
    <span class="releaseDate">June&nbsp;01&nbsp;2026&nbsp;00:30&nbsp;UTC</span>
    <span class="releaseTitle">S&amp;P Global Japan Manufacturing PMI</span>
    <span class="greenListItem"><a href="https://www.pmi.spglobal.com/Public/Home/PressRelease/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" target="_blank">View More</a></span>
</div>
<div class="listItem">
    <span class="releaseDate">July&nbsp;06&nbsp;2026&nbsp;13:45&nbsp;UTC</span>
    <span class="releaseTitle">S&amp;P Global US Sector PMI</span>
    <span class="greenListItem"><a href="https://www.pmi.spglobal.com/Public/Home/PressRelease/cccccccccccccccccccccccccccccccc" target="_blank">View More</a></span>
</div>
"""

_MANIFEST = """\
schema_version: 2
series:
  jp_manufacturing:
    - observed_at: 2026-05-01
      url: https://www.pmi.spglobal.com/Public/Home/PressRelease/dddddddddddddddddddddddddddddddd
  us_manufacturing:
    - observed_at: 2026-05-01
      url: https://www.pmi.spglobal.com/Public/Home/PressRelease/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
"""


def _manifest_file(tmp_path: Path, text: str = _MANIFEST) -> Path:
    path = tmp_path / "pmi_release_urls.yaml"
    path.write_text(text, encoding="utf-8")
    load_manifest.cache_clear()
    return path


@pytest.fixture(autouse=True)
def _clear_manifest_cache() -> None:
    load_manifest.cache_clear()


def test_parse_release_index_reads_the_date_title_and_link_of_each_listed_release() -> None:
    entries = parse_release_index(_INDEX_HTML)

    assert len(entries) == 3
    assert entries[0] == IndexEntry(
        published_on=date(2026, 7, 1),
        title="S&P Global Japan Manufacturing PMI",
        url=(
            "https://www.pmi.spglobal.com/Public/Home/PressRelease/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        ),
    )


def test_parse_release_index_reads_nothing_from_a_page_that_lists_no_release() -> None:
    """The publisher's WAF answers a plain client with a challenge page and a 200.

    Treating that as an empty index is what lets the caller fall back to a browser
    instead of reporting the month as unpublished.
    """

    assert parse_release_index("<html><body>Verifying...</body></html>") == ()


def test_observed_month_of_is_the_month_before_the_release_is_published() -> None:
    assert observed_month_of(date(2026, 7, 1)) == date(2026, 6, 1)
    assert observed_month_of(date(2026, 1, 6)) == date(2025, 12, 1)


def test_newest_candidate_takes_the_latest_release_the_index_lists_for_the_stream() -> None:
    candidate = newest_candidate(parse_release_index(_INDEX_HTML), stream="jp_manufacturing")

    assert candidate is not None
    assert candidate.observed_at == date(2026, 6, 1)
    assert candidate.url.endswith("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")


def test_newest_candidate_is_none_when_the_index_does_not_list_the_stream() -> None:
    assert newest_candidate(parse_release_index(_INDEX_HTML), stream="us_services") is None


def test_manifest_with_entry_appends_inside_the_stream_it_names() -> None:
    """A stream block ends where the next one begins.

    An entry written past that boundary would join a different PMI, which the
    schema cannot catch because both entries are well formed.
    """

    candidate = Candidate(
        stream="jp_manufacturing",
        observed_at=date(2026, 6, 1),
        url=(
            "https://www.pmi.spglobal.com/Public/Home/PressRelease/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        ),
    )

    updated = manifest_with_entry(_MANIFEST, candidate)

    jp_block, us_block = updated.split("  us_manufacturing:\n")
    assert "2026-06-01" in jp_block
    assert "2026-06-01" not in us_block


def test_manifest_with_entry_refuses_a_stream_the_manifest_does_not_name() -> None:
    candidate = Candidate(stream="uk_services", observed_at=date(2026, 6, 1), url="https://x")

    with pytest.raises(IndicatorsProviderError, match="no stream named"):
        manifest_with_entry(_MANIFEST, candidate)


def test_resolve_stream_does_nothing_when_the_manifest_already_holds_that_month(
    tmp_path: Path,
) -> None:
    manifest = _manifest_file(
        tmp_path,
        _MANIFEST.replace("2026-05-01", "2026-06-01"),
    )

    outcome = resolve_stream(
        "jp_manufacturing",
        parse_release_index(_INDEX_HTML),
        session=None,
        context=None,
        manifest_path=manifest,
        write=True,
    )

    assert outcome.resolved
    assert outcome.detail == "already current through 2026-06-01"
    assert manifest.read_text(encoding="utf-8") == _MANIFEST.replace("2026-05-01", "2026-06-01")


def test_resolve_stream_reports_a_stream_the_index_stopped_listing(tmp_path: Path) -> None:
    outcome = resolve_stream(
        "us_services",
        parse_release_index(_INDEX_HTML),
        session=None,
        context=None,
        manifest_path=_manifest_file(tmp_path),
        write=True,
    )

    assert not outcome.resolved
    assert "lists no release" in outcome.detail


def test_append_verified_entry_leaves_the_manifest_untouched_when_it_stops_loading(
    tmp_path: Path,
) -> None:
    """The manifest is edited as text, so only a reload proves the schema still holds.

    A rejected entry must leave the hand-maintained file exactly as it was rather
    than half-written.
    """

    manifest = _manifest_file(tmp_path)
    candidate = Candidate(
        stream="jp_manufacturing",
        observed_at=date(2026, 6, 1),
        url="https://www.pmi.spglobal.com/Public/Home/PressRelease/not-a-release-id",
    )

    with pytest.raises(IndicatorsProviderError, match="invalid release URL"):
        _append_verified_entry(candidate, manifest_path=manifest)

    assert manifest.read_text(encoding="utf-8") == _MANIFEST
