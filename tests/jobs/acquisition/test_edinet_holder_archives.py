from datetime import date
from sqlite3 import connect

from baibai_jobs.acquisition import edinet_archives as subject


def filing(doc_id, kind, submitted, **overrides):
    result = {
        "seqNumber": 1,
        "docID": doc_id,
        "docTypeCode": kind,
        "xbrlFlag": "1",
        "legalStatus": "1",
        "disclosureStatus": "0",
        "withdrawalStatus": "0",
        "docInfoEditStatus": "0",
        "submitDateTime": submitted,
    }
    result.update(overrides)
    return result


def test_candidate_plan_keeps_all_issuers_and_skips_only_retained_or_unavailable(
    tmp_path, monkeypatch
):
    path = tmp_path / "market.sqlite"
    connect(path).close()
    docs = [
        filing("S100HOLDER1", "350", "2026-10-01 09:00", issuerEdinetCode=None),
        filing("S100HOLDER2", "360", "2026-10-02 09:00", issuerEdinetCode="E00002"),
        filing("S100RETAIN", "350", "2026-10-03 09:00"),
        filing("S100NOXBRL", "350", "2026-10-04 09:00", xbrlFlag="0"),
        filing("S100ANNUAL", "120", "2026-10-05 09:00"),
    ]
    monkeypatch.setattr(subject, "read_observed_documents", lambda *_args, **_kwargs: docs)
    candidates, quarantined = subject.holder_archive_candidates(
        path, asof=date(2026, 11, 3), retained={("S100RETAIN", 1)}
    )
    assert candidates == ["S100HOLDER2", "S100HOLDER1"]
    assert quarantined == 0


def test_holder_access_denial_stops_without_repeating_or_affecting_old_receipts(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(subject, "prepare_fact_acquisition", lambda *_, **__: None)
    monkeypatch.setattr(
        subject, "holder_archive_candidates", lambda *_args, **_kw: (["S100FIRST", "S100NEXT"], 0)
    )
    calls = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

        def download_xbrl_zip(self, doc_id):
            calls.append(doc_id)
            raise subject.EdinetAcquisitionError("EDINET request failed with status 403")

    monkeypatch.setattr(subject, "EDINETRefresh", Client)
    monkeypatch.setattr(subject.time, "sleep", lambda _seconds: None)
    report = subject.acquire_archives(
        tmp_path / "db",
        group="edinet-facts",
        asof=date(2026, 11, 3),
        api_key="fake",
        retained=set(),
    )
    assert calls == ["S100FIRST"]
    assert report["status"] == "failed"
    assert report["failed"] == ["S100FIRST"]


def test_holder_planning_failure_does_not_prevent_independent_annual_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subject,
        "prepare_fact_acquisition",
        lambda *_, **__: [{"docID": "S100ANNUAL"}],
    )
    monkeypatch.setattr(
        subject,
        "needs_archive",
        lambda document, **_: True,
    )

    def fail(*_args, **_kwargs):
        raise ValueError("bad holder input")

    monkeypatch.setattr(subject, "holder_archive_candidates", fail)
    calls = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

        def download_xbrl_zip(self, doc_id):
            calls.append(doc_id)

    monkeypatch.setattr(subject, "EDINETRefresh", Client)
    report = subject.acquire_archives(
        tmp_path / "db",
        group="edinet-facts",
        asof=date(2026, 11, 3),
        api_key="fake",
        retained=set(),
    )
    assert calls == ["S100ANNUAL"]
    assert report["downloaded"] == 1
    assert report["status"] == "failed"
    assert report["holder_planning_failed"] is True


def test_holder_batch_is_bounded_and_unprocessed_originals_remain_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(subject, "prepare_fact_acquisition", lambda *_, **__: None)
    docs = [f"S100{n:04d}" for n in range(102)]
    monkeypatch.setattr(subject, "holder_archive_candidates", lambda *_args, **_kw: (docs, 0))
    calls = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

        def download_xbrl_zip(self, doc_id):
            calls.append(doc_id)

    monkeypatch.setattr(subject, "EDINETRefresh", Client)
    monkeypatch.setattr(subject.time, "sleep", lambda _seconds: None)
    result = subject.acquire_archives(
        tmp_path / "db",
        group="edinet-facts",
        asof=date(2026, 11, 3),
        api_key="fake",
        retained=set(),
    )
    assert calls == docs[:100]
    assert result["downloaded"] == 100
    assert result["holder_not_selected_this_batch"] == 2
    assert result["status"] == "received"
