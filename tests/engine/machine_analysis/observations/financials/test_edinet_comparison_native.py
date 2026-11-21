"""同じ固定原本のSQL/native読取で、比較vintage・欠測・障害を保つ。"""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import pytest
from tests.engine.machine_analysis.observations.financials.test_edinet_comparisons import (
    ASOF,
    archive,
    metadata,
)
from tests.helpers.edinet_documents import store_edinet_documents
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.archive_native import (
    HEAD_KEY as ARCHIVE_HEAD,
)
from baibai_engine.data_collection.providers.edinet.archive_native import (
    EdinetArchiveL1,
    FixedEdinetArchives,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.archives import retain_source_archive
from baibai_engine.data_collection.providers.edinet.list_native import HEAD_KEY as LIST_HEAD
from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    NativeListReceipt,
)
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.machine_analysis.generation.edinet_inputs import (
    read_edinet_input,
    retain_edinet_input,
)
from baibai_engine.machine_analysis.observations.disclosures import edinet_document_store
from baibai_engine.machine_analysis.observations.financials import edinet_comparison_reader as owner
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.integration.edinet_archive_native import materialize_archives
from baibai_jobs.integration.edinet_list_native import materialize_lists

AT = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture(params=["sql", "native"])
def route(request):
    return request.param


@pytest.fixture
def work(tmp_path):
    with TemporaryDirectory(prefix="case-", dir=tmp_path) as directory:
        yield Path(directory)


def fixed_archives(bodies):
    store = MemoryR2Store()
    items = [
        NativeArchive.from_response(doc_id, kind, EdinetResponse(AT, body))
        for (doc_id, kind), body in bodies.items()
    ]
    for item in items:
        store.store_object(
            f"l1/edinet/archives/v1/objects/{item.metadata.sha256}.zip", item.content
        )
    return store, FixedEdinetArchives(
        store,
        {
            "format": "edinet-archive-native-v1",
            "archives": [item.metadata.model_dump(mode="json") for item in items],
            "acquisition_scope": {"since": None, "doc_ids": [], "legacy_origins": []},
        },
    )


class ByteCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchmany(self, size):
        values, self.rows = self.rows[:size], self.rows[size:]
        return values


class ByteConnection:
    def __init__(self, rows, *, forbidden=False):
        self.rows = rows
        self.forbidden = forbidden

    def execute(self, statement, parameters):
        assert not self.forbidden, "native bytes must not fall back to SQL"
        assert "FROM edinet_source_archives WHERE source_type = 1" in statement
        assert not parameters
        return ByteCursor(self.rows)


def population(monkeypatch, documents, rows, route):
    def read_documents(connection, *, through):
        assert through == ASOF
        return documents

    monkeypatch.setattr(edinet_document_store, "read_observed_documents", read_documents)
    return owner.read_comparison_population(
        ByteConnection(rows, forbidden=route == "native"),
        as_of=ASOF,
        archives=fixed_archives({(doc, 1): body for doc, body in rows})[1]
        if route == "native"
        else None,
    )


def correction(doc_id="S100FIX", parent="S100YRSR"):
    return {
        **metadata(),
        "docID": doc_id,
        "docTypeCode": "130",
        "parentDocID": parent,
        "submitDateTime": "2026-07-25 15:31",
    }


def no_parse(**kwargs):
    pytest.fail("blocked original must retain its byte hash without being parsed")


def test_unreceived_families_are_not_empty_successful_vintages(monkeypatch, route):
    assert population(monkeypatch, [metadata(), correction()], [], route) == {}


def test_received_correction_keeps_missing_ancestors_and_related_descendants(monkeypatch, route):
    sibling = correction("S100NEXT", parent="S100FIX")
    sibling.update(submitDateTime="2026-07-26 15:31", secCode=None)
    unrelated = {**metadata(), "docID": "S100OTHER"}
    result = population(
        monkeypatch, [metadata(), correction(), sibling, unrelated], [("S100FIX", archive())], route
    )["436A"]
    assert [value.source_doc_id for value in result] == ["S100YRSR", "S100FIX", "S100NEXT"]
    assert result[0].reason == result[2].reason == "comparison_archive_not_received"
    assert result[0].source_sha256 is None
    assert result[1].reason is None
    assert result[1].parent_doc_id == "S100YRSR"
    assert result[1].pairs
    assert not result[0].pairs
    assert not result[2].pairs


@pytest.mark.parametrize("blocked", ["orphan_correction", "withdrawn", "unresolved_event"])
def test_blocked_received_original_retains_actual_bytes_hash(monkeypatch, blocked, route):
    original = metadata()
    selected_id = original["docID"]
    if blocked == "orphan_correction":
        selected_id = "S100FIX"
        documents = [correction(parent="S100LOST")]
        reason = "comparison_correction_ancestry_unknown"
    elif blocked == "withdrawn":
        documents = [
            original,
            {
                **original,
                "docID": "S100EVENT",
                "parentDocID": original["docID"],
                "withdrawalStatus": "1",
                "submitDateTime": "2026-07-25 15:31",
                "opeDateTime": "2026-07-25 15:31",
            },
        ]
        reason = "comparison_document_unavailable"
    else:
        documents = [
            original,
            {
                **correction("S100LOST"),
                "docInfoEditStatus": "1",
                "opeDateTime": "2026-07-25 15:31",
            },
        ]
        reason = "comparison_document_events_unresolved"
    content = b"retained blocked original is still actually read"
    monkeypatch.setattr(owner, "extract_comparisons", no_parse)
    result = population(monkeypatch, documents, [(selected_id, content)], route)["436A"]
    assert len(result) == 1
    assert result[0].source_doc_id == selected_id
    assert result[0].reason == reason
    assert result[0].source_sha256 == hashlib.sha256(content).hexdigest()
    assert not result[0].pairs


def test_retained_bad_zip_is_a_labelled_parse_reason_not_an_absent_archive(monkeypatch, route):
    content = b"not a ZIP"
    result = population(monkeypatch, [metadata()], [("S100YRSR", content)], route)["436A"]
    assert len(result) == 1
    assert result[0].reason == "zip_invalid"
    assert result[0].source_sha256 == hashlib.sha256(content).hexdigest()
    assert not result[0].pairs


@pytest.mark.parametrize("damage", ["missing", "changed"])
@pytest.mark.parametrize("blocked", [False, True])
def test_native_member_damage_is_not_missing_acquisition_or_sql_fallback(
    monkeypatch, damage, blocked
):
    document = metadata()
    if blocked:
        document["xbrlFlag"] = "0"
    body = archive()
    store, fixed = fixed_archives({("S100YRSR", 1): body})
    key = f"l1/edinet/archives/v1/objects/{hashlib.sha256(body).hexdigest()}.zip"
    if damage == "missing":
        del store.values[key]
    else:
        store.store_object(key, b"changed")
    monkeypatch.setattr(
        edinet_document_store, "read_observed_documents", lambda *a, **k: [document]
    )
    with pytest.raises(LakePublishError):
        owner.read_comparison_population(
            ByteConnection([("S100YRSR", body)], forbidden=True), as_of=ASOF, archives=fixed
        )


def test_native_reads_only_selected_type_one_and_keeps_publication_cutoff(monkeypatch):
    document = metadata()
    other = {**document, "docID": "S100OTHER", "secCode": "12340"}
    body = archive()
    store, fixed = fixed_archives(
        {
            ("S100YRSR", 1): body,
            ("S100YRSR", 5): b"unrelated CSV",
            ("S100OTHER", 1): b"unselected issuer",
        }
    )
    monkeypatch.setattr(
        edinet_document_store, "read_observed_documents", lambda *a, **k: [document, other]
    )
    published = datetime.fromisoformat(document["submitDateTime"] + "+09:00")
    connection = ByteConnection([], forbidden=True)
    assert (
        owner.read_comparison_population(
            connection,
            as_of=ASOF,
            tickers=("436A",),
            known_at=published - timedelta(seconds=1),
            archives=fixed,
        )
        == {}
    )
    assert store.get_keys == []
    result = owner.read_comparison_population(
        connection, as_of=ASOF, tickers=("436A",), known_at=published, archives=fixed
    )
    assert set(result) == {"436A"}
    assert result["436A"][0].reason is None
    assert store.get_keys == [
        f"l1/edinet/archives/v1/objects/{hashlib.sha256(body).hexdigest()}.zip"
    ]


def test_saved_partial_pair_survives_heads_and_does_not_borrow_sql_original(work):
    store = MemoryR2Store()
    list_owner = EdinetListL1(store, work / "lists")
    list_owner.freeze(require_existing=False)
    documents = [{**metadata(), "seqNumber": 1}, {**correction(), "seqNumber": 2, "secCode": None}]
    lists = list_owner.publish(
        [
            NativeListReceipt.from_response(
                ASOF,
                EdinetResponse(
                    AT,
                    json.dumps(
                        {"metadata": {"resultset": {"count": 2}}, "results": documents}
                    ).encode(),
                ),
                UUID(int=1).hex,
            )
        ]
    )
    archive_owner = EdinetArchiveL1(store, work / "archives")
    archive_owner.initialize()
    archives = archive_owner.publish(
        [NativeArchive.from_response("S100YRSR", 1, EdinetResponse(AT, archive()))]
    )
    saved = retain_edinet_input(store, lists, archives, selected_at=AT)
    path = work / "market.sqlite"
    materialize_lists(lists, path)
    materialize_archives(archives, path)
    with closing(sqlite3.connect(path)) as connection, connection:
        expected = owner.read_comparison_population(connection, as_of=ASOF)
        retain_source_archive(connection, "S100FIX", 1, archive(), stored_on=ASOF)
    archive_owner.publish(
        [NativeArchive.from_response("S100FIX", 1, EdinetResponse(AT, archive()))]
    )
    del store.values[LIST_HEAD]
    del store.values[ARCHIVE_HEAD]
    fixed = read_edinet_input(store, saved)
    before = path.read_bytes()
    before_objects = dict(store.values)
    store.get_keys.clear()

    def deny_raw_reads(action, table, *unused):
        return (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and table == "edinet_source_archives"
            else sqlite3.SQLITE_OK
        )

    with closing(sqlite3.connect(path)) as connection:
        connection.set_authorizer(deny_raw_reads)
        actual = owner.read_comparison_population(connection, as_of=ASOF, archives=fixed.archives)
    assert actual == expected
    assert actual["436A"][1].reason == "comparison_archive_not_received"
    assert path.read_bytes() == before
    assert store.values == before_objects
    assert LIST_HEAD not in store.get_keys
    assert ARCHIVE_HEAD not in store.get_keys


def test_sparse_annual_correction_reads_own_xbrl_at_cutoff_without_revising_origin(work):
    early, late = metadata(), {**correction(), "secCode": None}
    # An interim document with no security code is deliberately outside this annual rule.
    interim = {
        **late,
        "seqNumber": 1,
        "docID": "S100OTHER",
        "docTypeCode": "170",
    }
    path = work / "market.sqlite"
    store_edinet_documents(path, date(2026, 7, 24), [early])
    store_edinet_documents(path, date(2026, 7, 25), [late, interim])
    old, new = archive(), archive(replace={b"3525761000": b"3600000000"})
    with closing(sqlite3.connect(path)) as connection:
        for key, body in (("S100YRSR", old), ("S100FIX", new), ("S100OTHER", b"unrelated type")):
            retain_source_archive(connection, key, 1, body, stored_on=ASOF)
    store, fixed = fixed_archives(
        {
            ("S100YRSR", 1): old,
            ("S100FIX", 1): new,
            ("S100OTHER", 1): b"unrelated type",
        }
    )
    before = path.read_bytes(), dict(store.values)
    cutoff = datetime.fromisoformat(late["submitDateTime"] + "+09:00")
    with closing(sqlite3.connect(path)) as connection:
        early = owner.read_comparison_population(
            connection, as_of=ASOF, known_at=cutoff - timedelta(microseconds=1), archives=fixed
        )["436A"]
        assert [record.source_doc_id for record in early] == ["S100YRSR"]
        assert store.get_keys == [
            f"l1/edinet/archives/v1/objects/{hashlib.sha256(old).hexdigest()}.zip"
        ]
        native = owner.read_comparison_population(
            connection, as_of=ASOF, known_at=cutoff, tickers=("436A",), archives=fixed
        )["436A"]
        finite_sql = owner.read_comparison_population(
            connection, as_of=ASOF, known_at=cutoff, tickers=("436A",)
        )["436A"]
        full_sql = owner.read_comparison_population(connection, as_of=ASOF, known_at=cutoff)["436A"]
    assert native == finite_sql == full_sql
    assert native[:1] == early
    revised = native[1]
    assert revised.source_doc_id == "S100FIX"
    assert revised.parent_doc_id == "S100YRSR"
    assert revised.source_sha256 == hashlib.sha256(new).hexdigest()
    assert revised.reason is None
    assert [read.pairs[0].current.amount for read in native] == ["3525761000", "3600000000"]
    assert (path.read_bytes(), dict(store.values)) == before


@pytest.mark.parametrize(
    "damage",
    [
        "orphan",
        "future_parent",
        "withdrawn_parent",
        "conflicting_security",
        "conflicting_issuer",
        "nonannual_parent",
    ],
)
def test_sparse_correction_does_not_guess_unusable_ancestry(monkeypatch, route, damage):
    origin = metadata()
    sparse = {**correction(), "secCode": None}
    documents = [origin, sparse]
    if damage == "orphan":
        sparse["parentDocID"] = "S100MISSING"
    elif damage == "future_parent":
        origin["submitDateTime"] = "2026-07-25 15:31:00.000001"
    elif damage == "withdrawn_parent":
        origin.update(legalStatus="0", withdrawalStatus="2")
    elif damage == "conflicting_issuer":
        sparse["edinetCode"] = "E00001"
    elif damage == "nonannual_parent":
        origin["docTypeCode"] = "160"
    else:
        intermediate = correction("S100MID")
        intermediate["secCode"] = "12340"
        sparse["parentDocID"] = "S100MID"
        documents.append(intermediate)
    observed = population(
        monkeypatch,
        documents,
        [("S100YRSR", archive()), ("S100FIX", archive(replace={b"3525761000": b"3600000000"}))],
        route,
    )
    assert [row.source_doc_id for row in observed["436A"]] == ["S100YRSR"]
    assert all(row.source_doc_id != "S100FIX" for rows in observed.values() for row in rows)


def test_sparse_annual_correction_still_requires_own_xbrl_security_identity(monkeypatch, route):
    body = archive(replace={b">436A0<": b">12340<"})
    observed = population(
        monkeypatch, [metadata(), {**correction(), "secCode": None}], [("S100FIX", body)], route
    )["436A"]
    original, revised = observed
    assert original.reason == "comparison_archive_not_received"
    assert revised.source_doc_id == "S100FIX"
    assert revised.reason == "comparison_security_mismatch"
    assert revised.source_sha256 == hashlib.sha256(body).hexdigest()
    assert not revised.pairs
