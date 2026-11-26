from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from tools.experiments.us_research_mvp import _fixture, main, render_research

from baibai_engine.data_collection.providers.alpaca import DailyBarsQuery


def _replace_body(record, files, value):
    body = json.dumps(value).encode()
    files[record["file"]] = body
    record["sha256"] = sha256(body).hexdigest()


def test_default_cli_reaches_one_real_evaluation_branch_and_a_missing_branch(capsys):
    assert main([]) == 0
    report = capsys.readouterr().out
    assert "合成fixture。実データ" in report
    assert "AAPL | research_candidate" in report
    assert "観測20本のSMA: USD 102.50" in report
    assert "SEC実績売上: USD 1,234,567,890" in report
    assert "SEC提出一覧の銘柄対応: `sec-edgar:" in report
    assert "#/tickers/0`" in report
    assert "市場・株式クラスは表紙で別確認。" in report
    assert "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax" in report
    assert "NVDA | hold_for_evidence" in report
    assert "alpaca_fewer_than_20_observed_closes, sec_companyfacts_receipt_missing" in report
    assert report.count("売買: `no_action`") == 2
    assert main([]) == 0
    assert capsys.readouterr().out == report


def test_external_body_bytes_actually_drive_report(tmp_path, capsys):
    bundle, files = _fixture()
    fact = bundle["subjects"][0]["companyfacts"]
    facts = json.loads(files[fact["file"]])
    (concept,) = facts["facts"]["us-gaap"].values()
    concept["units"]["USD"][0]["val"] = 590001
    _replace_body(fact, files, facts)
    (tmp_path / "manifest.json").write_text(json.dumps(bundle), encoding="utf-8")
    for name, body in files.items():
        (tmp_path / name).write_bytes(body)
    assert main(["--bundle", str(tmp_path / "manifest.json")]) == 0
    report = capsys.readouterr().out
    assert "再認証しない" in report
    assert "SEC実績売上: USD 590,001" in report
    assert "1234567890" not in report


@pytest.mark.parametrize("period_start", ["2026-03-29", "2025-09-28"])
def test_mvp_accepts_aapl_quarter_but_not_the_same_filing_ytd(period_start):
    bundle, files = _fixture()
    subject = bundle["subjects"][0]
    # AAPL accession 0000320193-26-000020 reports both durations for the same
    # end date. Keep its dates and values; the surrounding captures are fixtures.
    accession = "0000320193-26-000020"
    subject.update(period_start=period_start, period_end="2026-06-27")
    subject["filing"]["accession"] = accession
    record = subject["companyfacts"]
    facts = json.loads(files[record["file"]])
    (concept,) = facts["facts"]["us-gaap"].values()
    (row,) = concept["units"]["USD"]
    row = dict(row, accn=accession, end=subject["period_end"])
    concept["units"]["USD"] = [
        dict(row, start="2025-09-28", val=364_357_000_000),
        dict(row, start="2026-03-29", val=109_417_000_000),
    ]
    _replace_body(record, files, facts)

    report = render_research(bundle, files.__getitem__, synthetic=False)
    assert "観測20本のSMA" in report
    assert "364,357,000,000" not in report
    if period_start == "2026-03-29":
        assert "AAPL | research_candidate" in report
        assert "SEC実績売上: USD 109,417,000,000 (2026-03-29〜2026-06-27" in report
    else:
        assert "AAPL | hold_for_evidence" in report
        assert "sec_period_outside_supported_quarter" in report
        assert "SEC実績売上" not in report


@pytest.mark.parametrize(
    ("days", "form", "reason"),
    [
        (88, "10-Q", "sec_period_outside_supported_quarter"),
        (89, "10-Q", None),  # February through April in a non-leap year.
        (98, "10-Q/A", None),  # A 14-week quarter of a 53-week fiscal year.
        (99, "10-Q", "sec_period_outside_supported_quarter"),
        (98, "10-K", "sec_period_outside_supported_fiscal_year"),
        (364, "10-K", None),
        (365, "10-K", None),
        (366, "10-K", None),
        (371, "10-K", None),
        (372, "10-K", "sec_period_outside_supported_fiscal_year"),
        (364, "10-K/A", "sec_filing_form_not_covered"),
        (364, "10-Q", "sec_period_outside_supported_quarter"),
    ],
)
def test_mvp_requires_supported_span_and_filing_form(days, form, reason):
    bundle, files = _fixture()
    subject = bundle["subjects"][0]
    end = date(2026, 4, 30)
    start = end - timedelta(days=days - 1)
    subject.update(period_start=start.isoformat(), period_end=end.isoformat())
    subject["filing"]["form"] = form
    record = subject["companyfacts"]
    facts = json.loads(files[record["file"]])
    (concept,) = facts["facts"]["us-gaap"].values()
    (row,) = concept["units"]["USD"]
    row.update(start=start.isoformat(), end=end.isoformat(), form=form)
    _replace_body(record, files, facts)

    report = render_research(bundle, files.__getitem__, synthetic=False)
    assert "観測20本のSMA" in report
    if reason is None:
        assert "AAPL | research_candidate" in report
        label = "SEC通期実績売上" if form == "10-K" else "SEC実績売上"
        assert f"{label}: USD 1,234,567,890 ({start}〜{end}" in report
        assert ("より新しい提出の有無は確認していない" in report) == (form == "10-K")
    else:
        assert "AAPL | hold_for_evidence" in report
        assert f"未充足: `{reason}`" in report
        assert "実績売上" not in report


def test_empty_terminal_page_must_be_known_before_whole_chain_is_used():
    bundle, files = _fixture()
    spec = bundle["alpaca"]
    first = spec["pages"][0]
    payload = json.loads(files[first["file"]])
    payload["next_page_token"] = "end+opaque"
    _replace_body(first, files, payload)
    query = DailyBarsQuery(
        ("AAPL", "NVDA"),
        date.fromisoformat(spec["start"]),
        date.fromisoformat(spec["end"]),
        "-",
        "split",
    )
    terminal = {
        "file": "end.json",
        "status": 200,
        "received_at": "2026-11-21T07:00:00Z",
        "request_url": query.url(page_token="end+opaque"),
        "response_url": query.url(page_token="end+opaque"),
        "complete": True,
        "redirected": False,
    }
    _replace_body(terminal, files, {"bars": {}, "next_page_token": None})
    spec["pages"].append(terminal)

    def observed_loader(name):
        assert name != "end.json", "must not read the future receipt body"
        return files[name]

    before = render_research(bundle, observed_loader, synthetic=True)
    assert "AAPL | hold_for_evidence" in before
    assert "alpaca_chain_not_yet_received" in before
    assert "観測20本のSMA" not in before
    assert "SEC実績売上" in before
    bundle["cutoff"] = terminal["received_at"]
    assert "AAPL | research_candidate" in render_research(bundle, files.__getitem__, synthetic=True)


def test_same_ny_day_aggregate_does_not_complete_20_observation_window():
    bundle, files = _fixture()
    page = bundle["alpaca"]["pages"][0]
    page["received_at"] = "2026-11-20T16:01:00Z"
    bundle["cutoff"] = page["received_at"]
    data = json.loads(files[page["file"]])
    data["bars"]["AAPL"] = data["bars"]["AAPL"][-20:]
    _replace_body(page, files, data)
    report = render_research(bundle, files.__getitem__, synthetic=True)
    assert "AAPL | hold_for_evidence" in report
    assert "途中集計扱い" in report
    assert "alpaca_fewer_than_20_observed_closes" in report
    bundle["cutoff"] = "2026-11-23T16:01:00Z"
    replay = render_research(bundle, files.__getitem__, synthetic=True)
    assert "AAPL | hold_for_evidence" in replay
    assert "alpaca_fewer_than_20_observed_closes" in replay


def _write_iex_only_bundle(tmp_path, bundle, files):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(bundle), encoding="utf-8")
    for page in bundle["alpaca"]["pages"]:
        name = page["file"]
        (tmp_path / name).write_bytes(files[name])
    return str(manifest)


@pytest.mark.parametrize(
    ("expected", "status", "exit_code"),
    [("2026-11-20", "一致", 0), ("2026-11-23", "不一致", 1), ("2026-11-19", "不一致", 1)],
)
def test_iex_session_acceptance_compares_exact_latest_date_without_sec(
    tmp_path, capsys, expected, status, exit_code
):
    bundle, files = _fixture()
    bundle["cutoff"] = "2026-11-24T06:00:00Z"
    manifest = _write_iex_only_bundle(tmp_path, bundle, files)
    assert main(["--bundle", manifest, "--accept-iex-session", expected]) == exit_code
    report = capsys.readouterr().out
    assert f"期待NY取引日: {expected}" in report
    assert f"- AAPL: {status}（採用最終日: 2026-11-20）" in report
    assert f"- NVDA: {status}（採用最終日: 2026-11-20）" in report
    assert "リサーチ候補" not in report
    assert "売買判断は確認しない" in report


@pytest.mark.parametrize("old_or_missing", ["2026-11-19", "なし"])
def test_iex_session_acceptance_cannot_use_another_symbols_date(tmp_path, capsys, old_or_missing):
    bundle, files = _fixture()
    page = bundle["alpaca"]["pages"][0]
    data = json.loads(files[page["file"]])
    data["bars"]["NVDA"] = data["bars"]["AAPL"][-2:-1] if old_or_missing != "なし" else []
    _replace_body(page, files, data)
    manifest = _write_iex_only_bundle(tmp_path, bundle, files)
    assert main(["--bundle", manifest, "--accept-iex-session", "2026-11-20"]) == 1
    report = capsys.readouterr().out
    assert "- AAPL: 一致（採用最終日: 2026-11-20）" in report
    assert f"- NVDA: 不一致（採用最終日: {old_or_missing}）" in report


@pytest.mark.parametrize(
    ("received_at", "exit_code"),
    [("2026-11-21T04:59:59Z", 1), ("2026-11-21T05:00:00Z", 0)],
)
def test_iex_session_acceptance_counts_receipt_dates_in_ny_not_utc(
    tmp_path, capsys, received_at, exit_code
):
    bundle, files = _fixture()
    bundle["cutoff"] = "2026-11-23T16:01:00Z"
    bundle["alpaca"]["pages"][0]["received_at"] = received_at
    manifest = _write_iex_only_bundle(tmp_path, bundle, files)
    assert main(["--bundle", manifest, "--accept-iex-session", "2026-11-20"]) == exit_code
    assert ("途中集計扱い" in capsys.readouterr().out) == (exit_code == 1)


def test_iex_session_acceptance_fails_when_receipt_is_after_cutoff(tmp_path, capsys):
    bundle, files = _fixture()
    bundle["cutoff"] = "2026-11-21T05:00:00Z"
    manifest = _write_iex_only_bundle(tmp_path, bundle, files)
    assert main(["--bundle", manifest, "--accept-iex-session", "2026-11-20"]) == 1
    assert "alpaca_chain_not_yet_received" in capsys.readouterr().out


def test_iex_session_acceptance_requires_a_bundle(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--accept-iex-session", "2026-11-20"])
    assert exc_info.value.code == 2
    assert "--accept-iex-session requires --bundle" in capsys.readouterr().err


def test_publicly_observed_time_is_not_independent_exact_publication():
    bundle, files = _fixture()
    filing = bundle["subjects"][0]["filing"]
    filing.update(published_at=None, publication_ref=None, public_seen_at="2026-11-10T00:00:00Z")
    before = render_research(bundle, files.__getitem__, synthetic=True)
    assert "初回公表の証明ではない" in before
    assert "sec_publication_unverified" in before
    assert "SEC実績売上" not in before
    filing["form"] = "10-K/A"
    assert "sec_filing_form_not_covered" in render_research(
        bundle, files.__getitem__, synthetic=True
    )
    filing["public_seen_at"] = "2026-10-01T00:00:00Z"
    invalid = render_research(bundle, files.__getitem__, synthetic=True)
    assert "公開を確認した時刻" not in invalid
    assert "sec_invalid_fixed_evidence" in invalid


def _public_index_bundle():
    bundle, files = _fixture()
    subject = bundle["subjects"][0]
    filing = subject["filing"]
    accession = filing["accession"]
    cik = int(filing["issuer_cik"])
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}-index.htm"
    submission = f"/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}.txt"
    body = (
        f"<html><head><title>EDGAR Filing Documents for {accession}</title></head>"
        "<body><h1>Filing Detail</h1>"
        f"<div><strong>SEC Accession No.</strong> {accession}</div>"
        '<div class="formGrouping"><div class="infoHead">Period of Report</div>'
        f'<div class="info">{subject["period_end"]}</div></div>'
        f'<table><tr><td>Complete submission text file</td><td><a href="{submission}">'
        f"{accession}.txt</a></td></tr></table></body></html>"
    ).encode()
    index = {
        "file": "index.htm",
        "sha256": sha256(body).hexdigest(),
        "status": 200,
        "received_at": bundle["cutoff"],
        "request_url": url,
        "response_url": url,
        "complete": True,
        "redirected": False,
        "headers": {"Content-Type": "text/html", "Content-Length": str(len(body))},
    }
    files[index["file"]] = body
    filing.update(published_at=None, publication_ref=None, public_index=index)
    return bundle, files, index


def test_explicit_sec_index_receipt_enables_observation_but_not_exact_publication(tmp_path, capsys):
    bundle, files, index = _public_index_bundle()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(bundle), encoding="utf-8")
    for name, body in files.items():
        (tmp_path / name).write_bytes(body)

    assert main(["--bundle", str(manifest)]) == 0
    report = capsys.readouterr().out
    assert "AAPL | research_candidate" in report
    assert "SEC実績売上: USD 1,234,567,890" in report
    assert "独立した初回公表時刻は未確認" in report
    assert "初回公表の証明ではない" in report
    assert index["request_url"] in report
    assert "`sec-index:" in report
    assert "独立公表証拠" not in report
    assert report.count("売買: `no_action`") == 2


@pytest.mark.parametrize(
    ("form", "current", "prior"),
    [
        ("10-K", ("2025-09-28", "2026-09-26"), ("2024-09-29", "2025-09-27")),
        ("10-Q", ("2026-07-01", "2026-09-30"), ("2025-07-01", "2025-09-30")),
    ],
)
def test_sec_filing_index_prevents_using_a_comparative_column_as_current(form, current, prior):
    bundle, files, index = _public_index_bundle()
    subject = bundle["subjects"][0]
    filing = subject["filing"]
    filing["form"] = form
    body = files[index["file"]].replace(subject["period_end"].encode(), current[1].encode())
    files[index["file"]] = body
    index["sha256"] = sha256(body).hexdigest()
    record = subject["companyfacts"]
    facts = json.loads(files[record["file"]])
    (concept,) = facts["facts"]["us-gaap"].values()
    (row,) = concept["units"]["USD"]
    concept["units"]["USD"] = [
        dict(row, form=form, start=current[0], end=current[1], val=123),
        dict(row, form=form, start=prior[0], end=prior[1], val=456),
    ]
    _replace_body(record, files, facts)

    subject["period_start"], subject["period_end"] = current
    valid = render_research(bundle, files.__getitem__, synthetic=False)
    assert "AAPL | research_candidate" in valid
    assert "USD 123" in valid

    subject["period_start"], subject["period_end"] = prior
    wrong = render_research(bundle, files.__getitem__, synthetic=False)
    assert "AAPL | hold_for_evidence" in wrong
    assert "sec_filing_period_mismatch" in wrong
    assert "USD 456" not in wrong
    assert "観測20本のSMA" in wrong


@pytest.mark.parametrize("defect", ["missing", "not-a-date", "duplicate", "not-siblings"])
def test_index_requires_one_natively_bound_period(defect):
    bundle, files, index = _public_index_bundle()
    original = files[index["file"]]
    if defect == "missing":
        body = original.replace(b"Period of Report", b"Filing Date")
    elif defect == "not-a-date":
        body = original.replace(b">2026-09-30</div>", b">not-a-date</div>")
    elif defect == "duplicate":
        group = (
            b'<div class="formGrouping"><div class="infoHead">Period of Report</div>'
            b'<div class="info">2025-09-30</div></div>'
        )
        body = original.replace(b"<table>", group + b"<table>")
    else:
        body = original.replace(
            b'Report</div><div class="info"', b'Report</div></div><div class="info"'
        )
    files[index["file"]] = body
    index["sha256"] = sha256(body).hexdigest()
    index["headers"]["Content-Length"] = str(len(body))
    report = render_research(bundle, files.__getitem__, synthetic=False)
    assert "AAPL | hold_for_evidence" in report
    assert "sec_invalid_fixed_evidence" in report


def test_fixed_clock_does_not_round_a_future_index_into_the_cutoff(tmp_path, capsys):
    bundle, files, index = _public_index_bundle()
    manifest = tmp_path / "manifest.json"
    for name, body in files.items():
        (tmp_path / name).write_bytes(body)

    def invoke(cutoff):
        bundle["cutoff"] = cutoff
        manifest.write_text(json.dumps(bundle), encoding="utf-8")
        return main(["--bundle", str(manifest)])

    # Actual SEC capture clock and the instant one nanosecond before it.
    index["received_at"] = "2026-11-25T19:53:01.447438103Z"
    with pytest.raises(SystemExit) as error:
        invoke("2026-11-25T19:53:01.447438102Z")
    assert error.value.code == 2
    assert capsys.readouterr().out == ""

    for unrepresentable in (
        index["received_at"],
        # Negative offset is 100 ms later than the rendered local clock.
        "2026-11-25T19:53:01.447438-00:00:00.100000",
    ):
        index["received_at"] = unrepresentable
        assert invoke("2026-11-25T19:53:01.447438000Z") == 0
        before = capsys.readouterr().out
        assert "AAPL | hold_for_evidence" in before
        assert "sec_invalid_fixed_evidence" in before
        assert "SEC実績売上" not in before

    index["received_at"] = "2026-11-26T04:53:01.447439000+09:00"
    assert invoke("2026-11-25T19:53:01.4474390Z") == 0
    at = capsys.readouterr().out
    assert "AAPL | research_candidate" in at
    assert "SEC実績売上: USD 1,234,567,890" in at


def _native_index_bundle():
    bundle, files, index = _public_index_bundle()
    head, table = files[index["file"]].split(b"<table>", 1)
    head, period = head.split(b'<div class="formGrouping">', 1)
    body = (
        head
        + b'<div id="contentDiv">\n<div class="formGrouping">'
        + period
        + b'\n<div class="formDiv">\n<!-- START DOCUMENT DIV -->\n<table>'
        + table.removesuffix(b"</body></html>")
        + b"</div>\n<!-- END DOCUMENT DIV -->\n<!-- START FILER DIV -->\n"
        b'<div class="filerDiv">Filer</div>\n<!-- END FILER DIV -->\n</div>\n'
    )
    return bundle, files, index, body


@pytest.mark.parametrize(
    ("media", "outer_end"),
    [
        ("text/html", b""),
        ("text/html", b"</body>"),
        ("text/html", b"</html>"),
        ("text/html", b"</body></html>"),
        ("application/xhtml+xml", b"</body></html>"),
    ],
)
def test_sec_index_accepts_complete_native_content_with_valid_outer_end(media, outer_end):
    bundle, files, index, body = _native_index_bundle()
    body += outer_end
    index["headers"]["Content-Type"] = media
    files[index["file"]] = body
    index["sha256"] = sha256(body).hexdigest()
    index["headers"]["Content-Length"] = str(len(body))
    report = render_research(bundle, files.__getitem__, synthetic=True)
    assert "AAPL | research_candidate" in report
    assert "SEC実績売上: USD 1,234,567,890" in report
    assert "初回公表の証明ではない" in report


@pytest.mark.parametrize(
    "defect",
    [
        "prefix-after-link",
        "period-after-documents",
        "prefix-with-explicit-outer-end",
        "premature-body-end",
        "markers-without-content-div",
        "no-final-div",
        "unclosed-document-table",
        "missing-document-end",
        "bogus-document-comment",
        "missing-filer-section",
        "empty-filer",
        "script-only-filer",
        "unclosed-filer-div",
        "unclosed-filer-comment",
        "unclosed-link",
        "self-closed-link",
        "ambiguous-href",
        "no-content-length",
        "wrong-content-length",
        "partial",
        "xhtml-missing-closers",
    ],
)
def test_sec_index_optional_closers_never_admit_partial_or_malformed_native_html(defect):
    bundle, files, index, body = _native_index_bundle()
    if defect == "prefix-after-link":
        body = body.split(b"<!-- END DOCUMENT DIV -->", 1)[0]
    elif defect == "period-after-documents":
        start = body.index(b'<div class="formGrouping">')
        end = body.index(b'\n<div class="formDiv">', start)
        period = body[start:end]
        start_filer = b"<!-- START FILER DIV -->"
        body = body.replace(period, b"").replace(start_filer, period + b"\n" + start_filer)
    elif defect == "prefix-with-explicit-outer-end":
        body = body.split(b"<!-- START FILER DIV -->", 1)[0] + b"</body></html>"
    elif defect == "premature-body-end":
        body = body.replace(b"<!-- END DOCUMENT DIV -->", b"</body>\n<!-- END DOCUMENT DIV -->")
    elif defect == "markers-without-content-div":
        body = body.replace(b'id="contentDiv"', b'id="other"') + b"</body></html>"
    elif defect == "no-final-div":
        body = body.removesuffix(b"</div>\n")
    elif defect == "unclosed-document-table":
        body = body.replace(b"</table>", b"")
    elif defect == "missing-document-end":
        body = body.replace(b"<!-- END DOCUMENT DIV -->", b"")
    elif defect == "bogus-document-comment":
        body = body.replace(b"<!-- END DOCUMENT DIV -->", b"<! END DOCUMENT DIV >")
    elif defect == "missing-filer-section":
        body = body.replace(b'<div class="filerDiv">Filer</div>\n', b"")
    elif defect == "empty-filer":
        body = body.replace(b">Filer</div>", b"> </div>") + b"</body></html>"
    elif defect == "script-only-filer":
        body = body.replace(b">Filer</div>", b"><script>placeholder</script></div>")
    elif defect == "unclosed-filer-div":
        body = body.replace(b"Filer</div>", b"Filer")
    elif defect == "unclosed-filer-comment":
        body = body.replace(b"<!-- END FILER DIV -->", b"<!-- END FILER DIV")
    elif defect == "unclosed-link":
        body = body.replace(b"</a>", b"")
    elif defect == "self-closed-link":
        body = body.replace(b'.txt">', b'.txt"/>').replace(b"</a>", b"")
    elif defect == "ambiguous-href":
        body = body.replace(b'<a href="', b'<a href="other.txt" href="', 1)
    files[index["file"]] = body
    index["sha256"] = sha256(body).hexdigest()
    index["headers"]["Content-Length"] = str(len(body))
    if defect == "no-content-length":
        del index["headers"]["Content-Length"]
    elif defect == "wrong-content-length":
        index["headers"]["Content-Length"] = str(len(body) + 1)
    elif defect == "partial":
        index["headers"]["Content-Range"] = f"bytes 0-{len(body) - 1}/{len(body) * 2}"
    elif defect == "xhtml-missing-closers":
        index["headers"]["Content-Type"] = "application/xhtml+xml"
    report = render_research(bundle, files.__getitem__, synthetic=True)
    assert "AAPL | hold_for_evidence" in report
    assert "sec_invalid_fixed_evidence" in report
    assert "SEC実績売上" not in report


def test_future_sec_index_receipt_does_not_read_its_body_or_block_old_exact_proof():
    bundle, files, index = _public_index_bundle()
    filing = bundle["subjects"][0]["filing"]
    future = datetime.fromisoformat(bundle["cutoff"]) + timedelta(microseconds=1)
    index.update(received_at=future.isoformat(), request_url="not yet an eligible witness")

    def read_not_future(name):
        assert name != index["file"]
        return files[name]

    report = render_research(bundle, read_not_future, synthetic=True)
    assert "sec_publication_unverified" in report
    assert "SEC実績売上" not in report
    filing.update(published_at="2026-11-04T21:00:00Z", publication_ref="fixture-publication")
    report = render_research(bundle, read_not_future, synthetic=True)
    assert "AAPL | research_candidate" in report
    assert "独立公表証拠 `fixture-publication`" in report


@pytest.mark.parametrize(
    "defect",
    [
        "digest",
        "status",
        "wrong-url",
        "wrong-body",
        "wrong-submission-link",
        "pre-acceptance",
        "prefix",
        "partial-header",
        "length-header",
        "encoding-header",
        "media-header",
        "other-response-url",
        "null-url",
        "redirect",
        "incomplete",
    ],
)
def test_ineligible_sec_index_receipt_cannot_substitute_for_publication(defect):
    bundle, files, index = _public_index_bundle()
    if defect == "digest":
        index["sha256"] = "0" * 64
    elif defect == "status":
        index["status"] = 206
    elif defect == "wrong-url":
        index["request_url"] += "?unbound"
        index["response_url"] = index["request_url"]
    elif defect in {"wrong-body", "wrong-submission-link"}:
        accession = bundle["subjects"][0]["filing"]["accession"].encode()
        if defect == "wrong-submission-link":
            accession += b".txt"
        body = files[index["file"]].replace(accession, b"0" * len(accession))
        files[index["file"]] = body
        index["sha256"] = sha256(body).hexdigest()
    elif defect == "pre-acceptance":
        index["received_at"] = "2026-11-04T20:44:59Z"
    elif defect == "prefix":
        body = files[index["file"]].removesuffix(b"</body></html>")
        files[index["file"]] = body
        index["sha256"] = sha256(body).hexdigest()
        del index["headers"]["Content-Length"]
    elif defect == "partial-header":
        index["headers"]["content-range"] = "bytes 0-30/999"
    elif defect == "length-header":
        index["headers"]["Content-Length"] = str(len(files[index["file"]]) + 1)
    elif defect == "encoding-header":
        index["headers"]["Content-Encoding"] = "gzip"
    elif defect == "media-header":
        index["headers"]["Content-Type"] = "application/json"
    elif defect == "other-response-url":
        index["response_url"] += ".other"
    elif defect == "null-url":
        index.update(request_url=None, response_url=None)
    elif defect == "redirect":
        index["redirected"] = True
    else:
        assert defect == "incomplete"
        index["complete"] = False
    report = render_research(bundle, files.__getitem__, synthetic=True)
    assert "AAPL | hold_for_evidence" in report
    assert "sec_invalid_fixed_evidence" in report
    assert "SEC実績売上" not in report
    assert "観測20本のSMA" in report


@pytest.mark.parametrize("changed", [False, True])
def test_simultaneous_native_concepts_do_not_become_a_sales_measure(changed):
    bundle, files = _fixture()
    fact = bundle["subjects"][0]["companyfacts"]
    payload = json.loads(files[fact["file"]])
    gaap = payload["facts"]["us-gaap"]
    (source,) = tuple(gaap.values())
    alt_fact = dict(source["units"]["USD"][0])
    alt_fact["val"] += int(changed)
    gaap["Revenues"] = {"units": {"USD": [alt_fact]}}
    _replace_body(fact, files, payload)
    report = render_research(bundle, files.__getitem__, synthetic=True)
    reason = "conflicting_amounts" if changed else "semantic_equivalence_unverified"
    assert "AAPL | hold_for_evidence" in report
    assert f"sec_revenue_concepts_unresolved:{reason}" in report
    assert "未採用のSECネイティブ概念 `us-gaap:Revenues`" in report
    assert "SEC実績売上" not in report


@pytest.mark.parametrize(
    "defect", ["request", "response", "redirect", "incomplete", "partial-range", "unknown"]
)
def test_unbound_alpaca_page_preserves_financials_but_cannot_supply_iex_prices(defect):
    bundle, files = _fixture()
    page = bundle["alpaca"]["pages"][0]
    actual = page["request_url"]
    if defect in {"request", "response"}:
        page[f"{defect}_url"] = actual.replace("feed=iex", "feed=sip")
    elif defect == "redirect":
        page["redirected"] = True
    elif defect == "incomplete":
        page["complete"] = False
    elif defect == "partial-range":
        page["headers"] = {"content-Range": "bytes 0-39/800"}
    else:
        del page["response_url"]

    report = render_research(bundle, files.__getitem__, synthetic=False)
    assert "AAPL | hold_for_evidence" in report
    assert "alpaca_invalid_fixed_receipt" in report
    assert "観測20本のSMA" not in report
    assert "SEC実績売上: USD 1,234,567,890" in report


@pytest.mark.parametrize(
    ("headers", "valid"),
    [
        ({"content-Length": "actual", "Content-Encoding": "identity"}, True),
        ({"Transfer-Encoding": "chunked"}, True),
        ({"Content-Length": "1"}, False),
        ({"Content-Encoding": "gzip"}, False),
        ({"Transfer-Encoding": "gzip"}, False),
        ({"Transfer-Encoding": "chunked", "Content-Length": "actual"}, False),
    ],
)
def test_recorded_alpaca_response_framing_must_agree_with_body(tmp_path, capsys, headers, valid):
    bundle, files = _fixture()
    page = bundle["alpaca"]["pages"][0]
    page["headers"] = {
        key: str(len(files[page["file"]])) if value == "actual" else value
        for key, value in headers.items()
    }
    manifest = _write_iex_only_bundle(tmp_path, bundle, files)
    expected = 0 if valid else 1
    assert main(["--bundle", manifest, "--accept-iex-session", "2026-11-20"]) == expected
    assert ("alpaca_invalid_fixed_receipt" in capsys.readouterr().out) is not valid

    report = render_research(bundle, files.__getitem__, synthetic=False)
    assert ("AAPL | research_candidate" in report) is valid
    assert ("観測20本のSMA" in report) is valid
    assert "SEC実績売上: USD 1,234,567,890" in report


@pytest.mark.parametrize("symbol", ["GOOG", "GOOGL"])
def test_alphabet_price_cannot_join_valid_apple_financials(symbol):
    bundle, files = _fixture()
    subject = bundle["subjects"][0]
    subject.update(symbol=symbol, identity_ref=f"claimed {symbol} / Apple")
    spec = bundle["alpaca"]
    page = spec["pages"][0]
    data = json.loads(files[page["file"]])
    data["bars"][symbol] = data["bars"].pop("AAPL")
    _replace_body(page, files, data)
    page["request_url"] = page["response_url"] = DailyBarsQuery(
        (symbol, "NVDA"),
        date.fromisoformat(spec["start"]),
        date.fromisoformat(spec["end"]),
        "-",
        "split",
    ).url()

    def load(name):
        assert name != subject["companyfacts"]["file"], "wrong issuer cannot unlock financials"
        return files[name]

    report = render_research(bundle, load, synthetic=False)
    assert f"{symbol} | hold_for_evidence" in report
    assert "sec_identity:ticker_not_reported" in report
    assert "観測20本のSMA" in report
    assert "SEC実績売上" not in report


def test_display_label_cannot_substitute_for_missing_submissions():
    bundle, files = _fixture()
    subject = bundle["subjects"][0]
    subject.pop("submissions", None)
    subject["identity_ref"] = "claimed AAPL / Apple"
    report = render_research(bundle, files.__getitem__, synthetic=False)
    assert "AAPL | hold_for_evidence" in report
    assert "sec_identity:submissions_receipt_missing" in report
    assert "観測20本のSMA" in report
    assert "SEC実績売上" not in report


def test_future_submissions_are_not_read_or_backdated_to_the_price():
    bundle, files = _fixture()
    subject = bundle["subjects"][0]
    record = subject["submissions"]
    future = datetime.fromisoformat(bundle["cutoff"]) + timedelta(microseconds=1)
    record["received_at"] = future.isoformat()

    def load(name):
        assert name not in {record["file"], subject["companyfacts"]["file"]}
        return files[name]

    report = render_research(bundle, load, synthetic=False)
    assert "AAPL | hold_for_evidence" in report
    assert "sec_identity:not_yet_received" in report
    assert "観測20本のSMA" in report
    assert "SEC実績売上" not in report
    bundle["cutoff"] = record["received_at"]
    assert "AAPL | research_candidate" in render_research(
        bundle, files.__getitem__, synthetic=False
    )


@pytest.mark.parametrize("defect", ["issuer", "response", "digest"])
def test_submissions_source_must_match_before_financials_are_joined(defect):
    bundle, files = _fixture()
    record = bundle["subjects"][0]["submissions"]
    if defect == "issuer":
        submitted = json.loads(files[record["file"]])
        submitted["cik"] = "1652044"
        _replace_body(record, files, submitted)
        reason = "issuer_mismatch"
    elif defect == "response":
        record["response_url"] = record["response_url"] + "?changed"
        reason = "submissions_response_scope_unverified"
    else:
        record["sha256"] = "0" * 64
        reason = "invalid_fixed_submissions"
    report = render_research(bundle, files.__getitem__, synthetic=False)
    assert "AAPL | hold_for_evidence" in report
    assert f"sec_identity:{reason}" in report
    assert "観測20本のSMA" in report
    assert "SEC実績売上" not in report


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("submissions", "sec_identity:invalid_fixed_submissions"),
        ("companyfacts", "sec_invalid_fixed_evidence"),
    ],
)
def test_bad_sec_attempt_id_holds_only_that_finance_in_external_bundle(
    source, reason, tmp_path, capsys
):
    bundle, files = _fixture()
    _, untouched = render_research(bundle, files.__getitem__, synthetic=False).split("## NVDA |")
    # A 32-digit JSON number would parse as a UUID if coerced to text.
    bundle["subjects"][0][source]["attempt_id"] = 12345678_12345678_12345678_12345678
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(bundle), encoding="utf-8")
    for name, body in files.items():
        (tmp_path / name).write_bytes(body)

    assert main(["--bundle", str(manifest)]) == 0
    affected, other = capsys.readouterr().out.split("## NVDA |")
    assert "AAPL | hold_for_evidence" in affected
    assert reason in affected
    assert "SEC実績売上" not in affected
    assert "観測20本のSMA: USD 102.50" in affected
    assert other == untouched


@pytest.mark.parametrize(
    "defect", ["wrong-response-url", "incomplete", "redirected", "missing-urls"]
)
def test_companyfacts_capture_scope_holds_only_affected_finance_in_cli(
    defect, tmp_path, capsys, monkeypatch
):
    bundle, files = _fixture()
    record = bundle["subjects"][0]["companyfacts"]
    manifest = tmp_path / "manifest.json"
    for name, body in files.items():
        (tmp_path / name).write_bytes(body)

    def run():
        manifest.write_text(json.dumps(bundle), encoding="utf-8")
        assert main(["--bundle", str(manifest)]) == 0
        return capsys.readouterr().out.split("## NVDA |")

    def price_lines(affected):
        return [
            line
            for line in affected.splitlines()
            if line.startswith(("- IEX", "- 同観測", "- 観測20本"))
        ]

    valid, unaffected = run()
    assert "AAPL | research_candidate" in valid
    assert "SEC実績売上: USD 1,234,567,890" in valid
    assert "観測20本のSMA: USD 102.50" in valid
    if defect == "wrong-response-url":
        record["response_url"] = record["response_url"].replace("0000320193", "0001652044")
    elif defect == "incomplete":
        record["complete"] = False
    elif defect == "redirected":
        record["redirected"] = True
    else:
        del record["request_url"]
        del record["response_url"]

    reads = []
    read_bytes = Path.read_bytes

    def record_read(path):
        reads.append(path)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", record_read)
    affected, other = run()
    assert "AAPL | hold_for_evidence" in affected
    assert "sec_companyfacts_response_scope_unverified" in affected
    assert "SEC実績売上" not in affected
    assert "売買: `no_action`" in affected
    assert price_lines(affected) == price_lines(valid)
    assert other == unaffected
    assert tmp_path / record["file"] not in reads


def test_opt_in_sec_comparison_uses_one_issuer_and_does_not_read_price(tmp_path, capsys):
    bundle, files, index = _public_index_bundle()
    subject = bundle["subjects"][0]
    bundle["subjects"] = [subject, {**subject, "symbol": "SECOND"}]
    del bundle["alpaca"]
    record = subject["companyfacts"]
    facts = json.loads(files[record["file"]])
    gaap = facts["facts"]["us-gaap"]
    (sample,) = next(iter(gaap.values()))["units"]["USD"]
    gaap["OperatingIncomeLoss"] = {
        "units": {
            "USD": [
                dict(sample, val=123),
                dict(sample, val=100, start="2025-07-01", end="2025-09-30"),
            ]
        }
    }
    _replace_body(record, files, facts)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(bundle), encoding="utf-8")
    for name in (record["file"], index["file"]):
        (tmp_path / name).write_bytes(files[name])
    cmd = [
        "--bundle",
        str(manifest),
        "--compare-sec",
        subject["cik"],
        "Q3",
        "2025-07-01",
        "2025-09-30",
        "2026-01-01",
        "2025-01-01",
    ]
    assert main(cmd) == 0
    report = capsys.readouterr().out
    assert "operating_income (quarter, current) 2026-07-01〜2026-09-30: USD 123" in report
    assert "operating_income (quarter, prior) 2025-07-01〜2025-09-30: USD 100" in report
    assert report.count("operating_income (quarter,") == 2
    assert "operating_cash_flow (year_to_date, current)" in report
    assert "unmapped_concept_or_unit" in report
    assert "SMA" not in report
    assert "research_candidate" not in report

    for row in bundle["subjects"]:
        row.update(period_start="2025-07-01", period_end="2025-09-30")
    manifest.write_text(json.dumps(bundle), encoding="utf-8")
    assert main([*cmd[:5], "2024-07-01", "2024-09-30", "2025-01-01", "2024-01-01"]) == 0
    wrong = capsys.readouterr().out
    assert "sec_filing_period_mismatch" in wrong
    assert "operating_income (" not in wrong

    subject["filing"]["form"] = "10-K"
    bundle["subjects"][1]["filing"] = subject["filing"]
    manifest.write_text(json.dumps(bundle), encoding="utf-8")
    assert main(cmd) == 0
    excluded = capsys.readouterr().out
    assert "sec_filing_form_not_covered" in excluded
    assert "operating_income (quarter," not in excluded
    subject["filing"]["form"] = "10-Q"
    bundle["subjects"][1]["period_start"] = "2026-07-02"
    manifest.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(cmd)
    assert exc.value.code == 2
    assert "invalid fixed US research receipt bundle" in capsys.readouterr().err
