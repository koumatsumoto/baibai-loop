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
        (98, "10-K", "sec_filing_form_not_covered"),
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
        assert f"SEC実績売上: USD 1,234,567,890 ({start}〜{end}" in report
    else:
        assert "AAPL | hold_for_evidence" in report
        assert f"未充足: `{reason}`" in report
        assert "SEC実績売上" not in report


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


def test_publicly_observed_time_is_not_independent_exact_publication():
    bundle, files = _fixture()
    filing = bundle["subjects"][0]["filing"]
    filing.update(published_at=None, publication_ref=None, public_seen_at="2026-11-10T00:00:00Z")
    before = render_research(bundle, files.__getitem__, synthetic=True)
    assert "初回公表の証明ではない" in before
    assert "sec_publication_unverified" in before
    assert "SEC実績売上" not in before
    filing["form"] = "10-K"
    assert "sec_filing_form_not_covered" in render_research(
        bundle, files.__getitem__, synthetic=True
    )
    filing["public_seen_at"] = "2026-10-01T00:00:00Z"
    invalid = render_research(bundle, files.__getitem__, synthetic=True)
    assert "公開を確認した時刻" not in invalid
    assert "sec_invalid_fixed_evidence" in invalid


def _public_index_bundle():
    bundle, files = _fixture()
    filing = bundle["subjects"][0]["filing"]
    accession = filing["accession"]
    cik = int(filing["issuer_cik"])
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}-index.htm"
    submission = f"/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}.txt"
    body = (
        f"<html><head><title>EDGAR Filing Documents for {accession}</title></head>"
        "<body><h1>Filing Detail</h1>"
        f"<div><strong>SEC Accession No.</strong> {accession}</div>"
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


def _native_index_bundle():
    bundle, files, index = _public_index_bundle()
    head, table = files[index["file"]].split(b"<table>", 1)
    body = (
        head + b'<div id="contentDiv">\n<div class="formDiv">\n'
        b"<!-- START DOCUMENT DIV -->\n<table>"
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
