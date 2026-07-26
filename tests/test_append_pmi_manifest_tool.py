from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
import tools.append_pmi_manifest as tool
from tools.append_pmi_manifest import (
    STREAM_TITLES,
    Candidate,
    IndexEntry,
    _append_verified_entry,
    manifest_with_entry,
    newest_candidate,
    observed_month_of,
    parse_release_index,
    resolve_stream,
    verified_value,
)

from baibai_engine.macro.indicators.providers.base import FetchContext, IndicatorsProviderError
from baibai_engine.macro.indicators.providers.spglobal_pmi import (
    MANIFEST_PATH,
    load_manifest,
)

_JP_MFG_URL = (
    "https://www.pmi.spglobal.com/Public/Home/PressRelease/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)
_OLDER_URL = (
    "https://www.pmi.spglobal.com/Public/Home/PressRelease/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
)


def _list_item(published: str, title: str, url: str) -> str:
    return (
        '<div class="listItem">\n'
        f'    <span class="releaseDate">{published}</span>\n'
        f'    <span class="releaseTitle">{title}</span>\n'
        f'    <span class="greenListItem"><a href="{url}" target="_blank">View More</a></span>\n'
        "</div>\n"
    )


_INDEX_HTML = (
    _list_item(
        "July&nbsp;01&nbsp;2026&nbsp;00:30&nbsp;UTC",
        "S&amp;P Global Japan Manufacturing PMI",
        _JP_MFG_URL,
    )
    + _list_item(
        "June&nbsp;01&nbsp;2026&nbsp;00:30&nbsp;UTC",
        "S&amp;P Global Japan Manufacturing PMI",
        _OLDER_URL,
    )
    + _list_item(
        "July&nbsp;06&nbsp;2026&nbsp;13:45&nbsp;UTC",
        "S&amp;P Global US Sector PMI",
        "https://www.pmi.spglobal.com/Public/Home/PressRelease/cccccccccccccccccccccccccccccccc",
    )
)

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

_TODAY = date(2026, 7, 26)


def _release_text_for(title: str, body: str, *, embargoed: str = "1 July 2026") -> str:
    """Release text shaped like the publisher's: the PMI it is, then its embargo."""

    return f"© 2026 S&P Global {title}® News Release Embargoed until 0930 JST {embargoed} {body}"


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
        url=_JP_MFG_URL,
    )


def test_parse_release_index_reads_nothing_from_a_page_that_lists_no_release() -> None:
    """The publisher's WAF answers a plain client with a challenge page and a 200.

    Treating that as an empty index is what lets the caller fall back to a browser
    instead of reporting the month as unpublished.
    """

    assert parse_release_index("<html><body>Verifying...</body></html>") == ()


def test_parse_release_index_drops_a_link_that_is_not_a_release_url() -> None:
    """The links come from a page this tool does not control.

    A link that is not a release URL would be fetched and then written into the
    manifest, so it is dropped where it enters rather than where it is used.
    """

    html = _list_item(
        "July&nbsp;01&nbsp;2026&nbsp;00:30&nbsp;UTC",
        "S&amp;P Global Japan Manufacturing PMI",
        "http://169.254.169.254/latest/meta-data/",
    )

    assert parse_release_index(html) == ()


def test_observed_month_of_is_the_month_before_the_release_is_published() -> None:
    assert observed_month_of(date(2026, 7, 1)) == date(2026, 6, 1)
    assert observed_month_of(date(2026, 1, 6)) == date(2025, 12, 1)


def test_newest_candidate_takes_the_latest_release_the_index_lists_for_the_stream() -> None:
    candidate = newest_candidate(parse_release_index(_INDEX_HTML), stream="jp_manufacturing")

    assert candidate is not None
    assert candidate.observed_at == date(2026, 6, 1)
    assert candidate.url == _JP_MFG_URL


def test_newest_candidate_is_none_when_the_index_does_not_list_the_stream() -> None:
    assert newest_candidate(parse_release_index(_INDEX_HTML), stream="us_services") is None


def test_stream_titles_name_streams_the_manifest_actually_carries() -> None:
    """A stream key the manifest does not hold could never be appended to."""

    assert set(STREAM_TITLES) <= set(load_manifest(MANIFEST_PATH))


def test_verified_value_refuses_a_release_that_does_not_name_the_month() -> None:
    """The month has to be proven from the release text, not from the publication date.

    A release states its headline without naming the month ("posted 54.8"), and
    ``extract_pmi_value`` accepts such a statement for whichever month it is told the
    release reports. Asking for the month that way would accept the same value for any
    month at all, which is exactly the mistake this tool exists to prevent.
    """

    text = _release_text_for(
        "S&P Global Japan Manufacturing PMI",
        "The S&P Global Japan Manufacturing PMI posted 54.8, up from 54.5, signalling "
        "a further improvement in operating conditions.",
    )
    candidate = Candidate(
        stream="jp_manufacturing",
        observed_at=date(2026, 6, 1),
        published_on=date(2026, 7, 1),
        url=_JP_MFG_URL,
    )

    with (
        FetchContext(purpose="refresh") as context,
        patch.object(tool, "release_text", return_value=text),
        pytest.raises(IndicatorsProviderError, match="no headline value for that month"),
    ):
        verified_value(candidate, context=context)


def test_verified_value_reads_a_release_that_ties_its_headline_to_the_month() -> None:
    text = _release_text_for(
        "S&P Global Japan Manufacturing PMI",
        "The S&P Global Japan Manufacturing PMI posted 54.8 in June, up from 54.5 in May, "
        "signalling a further improvement in operating conditions.",
    )
    candidate = Candidate(
        stream="jp_manufacturing",
        observed_at=date(2026, 6, 1),
        published_on=date(2026, 7, 1),
        url=_JP_MFG_URL,
    )

    with (
        FetchContext(purpose="refresh") as context,
        patch.object(tool, "release_text", return_value=text),
    ):
        assert verified_value(candidate, context=context) == 54.8


def test_manifest_with_entry_appends_inside_the_stream_it_names() -> None:
    """A stream block ends where the next one begins.

    An entry written past that boundary would join a different PMI, which the
    schema cannot catch because both entries are well formed.
    """

    candidate = Candidate(
        stream="jp_manufacturing",
        observed_at=date(2026, 6, 1),
        published_on=date(2026, 7, 1),
        url=_JP_MFG_URL,
    )

    updated = manifest_with_entry(_MANIFEST, candidate)

    jp_block, us_block = updated.split("  us_manufacturing:\n")
    assert "2026-06-01" in jp_block
    assert "2026-06-01" not in us_block


def test_manifest_with_entry_appends_to_the_last_stream_in_the_file() -> None:
    """The last block ends at the end of the file, not at another stream key."""

    candidate = Candidate(
        stream="us_manufacturing",
        observed_at=date(2026, 6, 1),
        published_on=date(2026, 7, 1),
        url=_JP_MFG_URL,
    )

    updated = manifest_with_entry(_MANIFEST, candidate)

    assert updated.endswith(f"    - observed_at: 2026-06-01\n      url: {_JP_MFG_URL}\n")


def test_manifest_with_entry_does_not_join_a_file_whose_last_line_is_unterminated() -> None:
    """A manifest saved without a trailing newline would take the entry onto that line.

    The result parses as one entry with a mangled URL, so the existing month would be
    destroyed rather than the new one rejected.
    """

    candidate = Candidate(
        stream="us_manufacturing",
        observed_at=date(2026, 6, 1),
        published_on=date(2026, 7, 1),
        url=_JP_MFG_URL,
    )

    updated = manifest_with_entry(_MANIFEST.rstrip("\n"), candidate)

    assert "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\n    - observed_at: 2026-06-01" in updated


def test_manifest_with_entry_refuses_a_stream_the_manifest_does_not_name() -> None:
    candidate = Candidate(
        stream="uk_services",
        observed_at=date(2026, 6, 1),
        published_on=date(2026, 7, 1),
        url="https://x",
    )

    with pytest.raises(IndicatorsProviderError, match="no stream named"):
        manifest_with_entry(_MANIFEST, candidate)


def test_resolve_stream_does_nothing_when_the_manifest_already_holds_that_month(
    tmp_path: Path,
) -> None:
    text = _MANIFEST.replace("2026-05-01", "2026-06-01")
    manifest = _manifest_file(tmp_path, text)

    outcome = resolve_stream(
        "jp_manufacturing",
        parse_release_index(_INDEX_HTML),
        context=None,
        manifest_path=manifest,
        write=True,
        today=_TODAY,
    )

    assert outcome.resolved
    assert outcome.detail == "already current through 2026-06-01"
    assert manifest.read_text(encoding="utf-8") == text


def test_resolve_stream_reports_a_stream_the_index_stopped_listing(tmp_path: Path) -> None:
    outcome = resolve_stream(
        "us_services",
        parse_release_index(_INDEX_HTML),
        context=None,
        manifest_path=_manifest_file(tmp_path),
        write=True,
        today=_TODAY,
    )

    assert not outcome.resolved
    assert "lists no release" in outcome.detail


def test_resolve_stream_refuses_to_append_across_a_missing_month(tmp_path: Path) -> None:
    """The provider's currency guard only looks at the newest manifest month.

    An entry written past a hole satisfies that guard forever, so the skipped months
    would stop being reported. Refusing keeps the failure visible.
    """

    manifest = _manifest_file(tmp_path, _MANIFEST.replace("2026-05-01", "2026-03-01"))

    outcome = resolve_stream(
        "jp_manufacturing",
        parse_release_index(_INDEX_HTML),
        context=None,
        manifest_path=manifest,
        write=True,
        today=_TODAY,
    )

    assert not outcome.resolved
    assert "the months between have to be added by hand" in outcome.detail
    assert "2026-06-01" not in manifest.read_text(encoding="utf-8")


def test_resolve_stream_refuses_a_month_that_cannot_have_been_reported_yet(
    tmp_path: Path,
) -> None:
    """A misread publication date must not push the manifest ahead of the calendar."""

    html = _list_item(
        "December&nbsp;01&nbsp;2027&nbsp;00:30&nbsp;UTC",
        "S&amp;P Global Japan Manufacturing PMI",
        _JP_MFG_URL,
    )
    manifest = _manifest_file(tmp_path)

    outcome = resolve_stream(
        "jp_manufacturing",
        parse_release_index(html),
        context=None,
        manifest_path=manifest,
        write=True,
        today=_TODAY,
    )

    assert not outcome.resolved
    assert "not a month that can have been reported yet" in outcome.detail
    assert manifest.read_text(encoding="utf-8") == _MANIFEST


def test_append_verified_entry_leaves_the_manifest_untouched_when_the_entry_is_rejected(
    tmp_path: Path,
) -> None:
    """Only a reload proves a text edit kept the schema, so it runs on a copy.

    The canonical file is written by the move that follows the check, which is what
    keeps a rejected entry from ever reaching the file the provider reads.
    """

    manifest = _manifest_file(tmp_path)
    candidate = Candidate(
        stream="jp_manufacturing",
        observed_at=date(2026, 6, 1),
        published_on=date(2026, 7, 1),
        url="https://www.pmi.spglobal.com/Public/Home/PressRelease/not-a-release-id",
    )

    with pytest.raises(IndicatorsProviderError, match=r"unreadable|did not survive"):
        _append_verified_entry(candidate, manifest_path=manifest)

    assert manifest.read_text(encoding="utf-8") == _MANIFEST


def test_append_verified_entry_leaves_the_manifest_untouched_when_the_edit_breaks_yaml(
    tmp_path: Path,
) -> None:
    """A text edit can produce something YAML cannot parse at all.

    That failure is not the manifest's own error type, so catching only the provider's
    error would leave the canonical file unreadable.
    """

    manifest = _manifest_file(tmp_path)
    candidate = Candidate(
        stream="jp_manufacturing",
        observed_at=date(2026, 6, 1),
        published_on=date(2026, 7, 1),
        url=_JP_MFG_URL,
    )

    with (
        patch.object(tool, "manifest_with_entry", return_value="series: [unclosed\n"),
        pytest.raises(IndicatorsProviderError, match="unreadable"),
    ):
        _append_verified_entry(candidate, manifest_path=manifest)

    assert manifest.read_text(encoding="utf-8") == _MANIFEST


def test_append_verified_entry_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    manifest = _manifest_file(tmp_path)
    candidate = Candidate(
        stream="jp_manufacturing",
        observed_at=date(2026, 6, 1),
        published_on=date(2026, 7, 1),
        url=_JP_MFG_URL,
    )

    _append_verified_entry(candidate, manifest_path=manifest)

    assert not [path for path in tmp_path.iterdir() if path.name.endswith(".tmp")]
    assert "2026-06-01" in manifest.read_text(encoding="utf-8")


def test_verified_value_refuses_a_release_of_a_different_pmi() -> None:
    """Proving the month does not prove which PMI the release belongs to.

    A services release states its headline the same way a manufacturing one does, so
    a title mapped to the wrong PMI would still yield a value for the right month.
    """

    text = _release_text_for(
        "S&P Global Japan Services PMI",
        "The S&P Global Japan Services PMI posted 52.2 in June, signalling growth.",
    )
    candidate = Candidate(
        stream="jp_manufacturing",
        observed_at=date(2026, 6, 1),
        published_on=date(2026, 7, 1),
        url=_JP_MFG_URL,
    )

    with (
        FetchContext(purpose="refresh") as context,
        patch.object(tool, "release_text", return_value=text),
        pytest.raises(IndicatorsProviderError, match="does not name"),
    ):
        verified_value(candidate, context=context)


def test_verified_value_refuses_a_release_the_index_dated_in_another_month() -> None:
    """The release states its own embargo date, which is what it was published on.

    An index that dates a release a month early would have the month before it read
    out of the restatement, which is a value the manifest records differently.
    """

    text = _release_text_for(
        "S&P Global Japan Manufacturing PMI",
        "The S&P Global Japan Manufacturing PMI posted 54.8 in June, up from 54.5 in May.",
        embargoed="1 July 2026",
    )
    candidate = Candidate(
        stream="jp_manufacturing",
        observed_at=date(2026, 5, 1),
        published_on=date(2026, 6, 1),
        url=_JP_MFG_URL,
    )

    with (
        FetchContext(purpose="refresh") as context,
        patch.object(tool, "release_text", return_value=text),
        pytest.raises(IndicatorsProviderError, match="embargoed until"),
    ):
        verified_value(candidate, context=context)


def test_verified_value_refuses_a_release_that_states_no_embargo_date() -> None:
    text = "S&P Global Japan Manufacturing PMI posted 54.8 in June."
    candidate = Candidate(
        stream="jp_manufacturing",
        observed_at=date(2026, 6, 1),
        published_on=date(2026, 7, 1),
        url=_JP_MFG_URL,
    )

    with (
        FetchContext(purpose="refresh") as context,
        patch.object(tool, "release_text", return_value=text),
        pytest.raises(IndicatorsProviderError, match="no embargo date"),
    ):
        verified_value(candidate, context=context)


def test_append_verified_entry_keeps_the_permissions_the_manifest_had(tmp_path: Path) -> None:
    """The entry arrives by replacing the file, and a fresh copy starts private."""

    manifest = _manifest_file(tmp_path)
    manifest.chmod(0o644)
    candidate = Candidate(
        stream="jp_manufacturing",
        observed_at=date(2026, 6, 1),
        published_on=date(2026, 7, 1),
        url=_JP_MFG_URL,
    )

    _append_verified_entry(candidate, manifest_path=manifest)

    assert manifest.stat().st_mode & 0o777 == 0o644


def test_append_verified_entry_leaves_the_manifest_untouched_when_a_key_repeats(
    tmp_path: Path,
) -> None:
    """A repeated mapping key is rejected by the loader as a plain value error.

    Catching only the manifest's own error type would leave the canonical file
    holding an edit that no longer loads.
    """

    manifest = _manifest_file(tmp_path)
    candidate = Candidate(
        stream="jp_manufacturing",
        observed_at=date(2026, 6, 1),
        published_on=date(2026, 7, 1),
        url=_JP_MFG_URL,
    )

    with (
        patch.object(
            tool,
            "manifest_with_entry",
            return_value="schema_version: 2\nseries:\n  a: 1\n  a: 2\n",
        ),
        pytest.raises(IndicatorsProviderError, match="unreadable"),
    ):
        _append_verified_entry(candidate, manifest_path=manifest)

    assert manifest.read_text(encoding="utf-8") == _MANIFEST
