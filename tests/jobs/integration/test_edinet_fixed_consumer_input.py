"""固定原本からのcold cacheを既存SQL consumerへ同じ意味で渡す。"""

import io
import json
import os
import sqlite3
import zipfile
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest
from tests.helpers.edinet_research_sources import source_zip
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.archive_native import (
    HEAD_KEY as ARCHIVE_HEAD,
)
from baibai_engine.data_collection.providers.edinet.archive_native import (
    EdinetArchiveL1,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.archives import retain_source_archive
from baibai_engine.data_collection.providers.edinet.facts_scope import (
    FactsScope,
    merge_scope,
    read_scope,
)
from baibai_engine.data_collection.providers.edinet.list_native import (
    HEAD_KEY as LIST_HEAD,
)
from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    NativeListReceipt,
)
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.machine_analysis.observations.disclosures.delistings import (
    DelistingRecord,
    store_jpx_delistings,
)
from baibai_engine.machine_analysis.observations.disclosures.tender_offer import (
    build_tender_offer_exit_values,
)
from baibai_engine.machine_analysis.observations.financials.edinet_comparison_reader import (
    read_comparison_population,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.service import (
    extract_edinet_facts_command,
)
from baibai_engine.machine_analysis.observations.financials.edinet_holder_capital import (
    read_holder_capital,
)
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration import edinet_archive_native
from baibai_jobs.integration.edinet_archive_native import rebuild_fixed_edinet_input
from baibai_jobs.integration.edinet_list_native import materialize_lists

DAY = date(2026, 11, 1)
AT = datetime(2026, 11, 1, 1, tzinfo=UTC)
ASOF = date(2026, 11, 5)
KNOWN = datetime(2026, 11, 5, 8, tzinfo=UTC)
FIXTURES = Path(__file__).parents[2] / "fixtures" / "edinet" / "comparisons"


def zipped(files):
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as bundle:
        for name, body in files.items():
            bundle.writestr(name, body)
    return target.getvalue()


def csv_zip(blocks):
    rows = "要素ID\t項目名\t値\n" + "\n".join(
        f"{key}\t項目\t{value}" for key, value in blocks.items()
    )
    return zipped({"XBRL_TO_CSV/report.csv": rows.encode("utf-16")})


def holder_zip():
    return zipped(
        {
            "XBRL/PublicDoc/report.xbrl": b"""<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:h="http://disclosure.edinet-fsa.go.jp/taxonomy/jplvh/2025-11-01/jplvh_cor"><xbrli:context id="FilingDateInstant"><xbrli:entity><xbrli:identifier scheme="urn:edinet">E00001</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2026-10-28</xbrli:instant></xbrli:period></xbrli:context><xbrli:unit id="u1"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit><h:SecurityCodeOfIssuer contextRef="FilingDateInstant">1234</h:SecurityCodeOfIssuer><h:TotalNumberOfOutstandingStocksEtc contextRef="FilingDateInstant" unitRef="u1" decimals="0">2500000</h:TotalNumberOfOutstandingStocksEtc></xbrli:xbrl>"""
        }
    )


def fixtures(tmp_path):
    store = MemoryR2Store()
    list_owner = EdinetListL1(store, tmp_path / "list-work")
    list_owner.freeze(require_existing=False)
    annual = json.loads((FIXTURES / "S100YRSR.json").read_text())
    documents = [annual, {**annual, "docID": "S100YR5P", "secCode": "18730"}]
    defaults = {
        "legalStatus": "1",
        "withdrawalStatus": "0",
        "disclosureStatus": "0",
        "docInfoEditStatus": "0",
        "xbrlFlag": "1",
        "csvFlag": "1",
    }
    documents.extend(
        [
            {
                **defaults,
                "docID": "S100HOLD",
                "docTypeCode": "350",
                "submitDateTime": "2026-10-30 10:00",
            },
            {
                **defaults,
                "docID": "S100SELF",
                "docTypeCode": "120",
                "secCode": "20000",
                "edinetCode": "TARGET",
                "submitDateTime": "2026-10-01 09:00",
            },
            {
                **defaults,
                "docID": "S100OFFER",
                "docTypeCode": "240",
                "edinetCode": "BIDDER",
                "subjectEdinetCode": "TARGET",
                "submitDateTime": "2026-10-01 10:00",
            },
            {
                **defaults,
                "docID": "S100RESULT",
                "docTypeCode": "270",
                "edinetCode": "BIDDER",
                "subjectEdinetCode": "TARGET",
                "submitDateTime": "2026-10-20 10:00",
            },
        ]
    )
    body = json.dumps(
        {
            "metadata": {"resultset": {"count": len(documents)}},
            "results": [{**doc, "seqNumber": n} for n, doc in enumerate(documents, 1)],
        }
    ).encode()
    lists = list_owner.publish(
        [NativeListReceipt.from_response(DAY, EdinetResponse(AT, body), UUID(int=1).hex)]
    )
    originals = [
        (
            "S100YRSR",
            1,
            zipped(
                {
                    "XBRL/PublicDoc/" + path.name: path.read_bytes()
                    for path in (FIXTURES / "S100YRSR.xbrl", FIXTURES / "S100YRSR_pre.xml")
                }
            ),
        ),
        ("S100YR5P", 1, source_zip()),
        ("S100HOLD", 1, holder_zip()),
        (
            "S100OFFER",
            5,
            csv_zip(
                {
                    "test:PriceOfPurchaseEtcTextBlock": "普通株式1株につき2000円",
                    "test:FundEtcForPurchaseEtcTextBlock": "金銭以外の対価の種類―",
                }
            ),
        ),
        (
            "S100RESULT",
            5,
            csv_zip(
                {
                    "test:SuccessOrFailureOfTenderOfferTextBlock": "応募株券等の全部の買付け等を行います。"
                }
            ),
        ),
    ]
    archive_owner = EdinetArchiveL1(store, tmp_path / "archive-work")
    archive_owner.initialize()
    items = [
        NativeArchive.from_response(
            doc, kind, EdinetResponse(datetime(2026, 11, 2, 3, tzinfo=UTC), body)
        )
        for doc, kind, body in originals
    ]
    items[0] = NativeArchive(
        items[0].metadata.model_copy(update={"received_at_utc": None}), items[0].content
    )
    scope = FactsScope(
        date(2025, 1, 1), frozenset({"S100YR5P", "S100MISSING"}), frozenset({"a" * 64})
    )
    archives = archive_owner.publish(items, scope=scope)
    return store, list_owner, archive_owner, lists, archives, items


def consumer_outputs(database):
    reader = EDINETReader(database)
    store_jpx_delistings(
        database,
        [
            DelistingRecord(
                delisted_on=ASOF,
                ticker="2000",
                name="fixture",
                market="プライム",
                reason="公開買付け",
            )
        ],
    )
    exits = build_tender_offer_exit_values(database, reader=reader, asof=ASOF)
    assert exits[0][0].offer_price_yen == 2000
    with closing(open_connection(database)) as connection:
        comparisons = read_comparison_population(connection, as_of=ASOF)
        assert comparisons["436A"][0].pairs
        holders = read_holder_capital(connection, as_of=ASOF, known_at=KNOWN, tickers=("1234",))
        assert holders[0]["1234"][0].total_stocks == 2500000
    assert (
        extract_edinet_facts_command(
            asof=ASOF, sqlite_path=database, reader=reader, tickers=("1873",), stdout=io.StringIO()
        )
        == 0
    )
    with sqlite3.connect(database) as connection:
        facts = connection.execute("SELECT * FROM edinet_segment_facts ORDER BY 1,2,3,4").fetchall()
        assert facts
    return reader.list_documents(DAY), comparisons, holders, exits, facts


def test_cold_input_preserves_exact_originals_scope_and_existing_consumers(tmp_path):
    store, _, _, lists, archives, items = fixtures(tmp_path)
    legacy = tmp_path / "legacy.sqlite"
    materialize_lists(lists, legacy)
    with closing(open_connection(legacy)) as connection:
        for item in items:
            retain_source_archive(
                connection,
                item.metadata.doc_id,
                item.metadata.source_type,
                item.content,
                stored_on=item.metadata.stored_on,
            )
        with connection:
            merge_scope(connection, archives.scope)
    expected = consumer_outputs(legacy)
    store.put_keys.clear()
    for name in ("cold", "cold-again"):
        output = tmp_path / f"{name}.sqlite"
        report = rebuild_fixed_edinet_input(lists, archives, output)
        assert report == {
            "status": "completed",
            "list_identity": lists.identity,
            "archive_identity": archives.identity,
            "list_failed_days": [],
            "archive_count": len(items),
        }
        with sqlite3.connect(output) as connection:
            assert read_scope(connection) == archives.scope
            assert (
                connection.execute("SELECT count(*) FROM edinet_segment_facts").fetchone()[0] == 0
            )
            assert connection.execute("SELECT count(*) FROM edinet_metrics").fetchone()[0] == 0
            assert connection.execute(
                "SELECT doc_id, source_type, stored_on, zip_content FROM edinet_source_archives ORDER BY doc_id, source_type"
            ).fetchall() == sorted(
                (
                    item.metadata.doc_id,
                    item.metadata.source_type,
                    item.metadata.stored_on.isoformat(),
                    item.content,
                )
                for item in items
            )
        assert consumer_outputs(output) == expected
    assert store.put_keys == []


def test_saved_heads_do_not_advance_or_fallback_to_current_head(tmp_path):
    store, _, owner, lists, archives, _ = fixtures(tmp_path)
    owner.publish(
        [NativeArchive.from_response("S100LATER", 1, EdinetResponse(KNOWN, b"later"))],
        scope=FactsScope(doc_ids=frozenset({"S100LATER"})),
    )
    del store.values[ARCHIVE_HEAD]
    del store.values[LIST_HEAD]
    store.get_keys.clear()
    output = tmp_path / "fixed.sqlite"
    rebuild_fixed_edinet_input(lists, archives, output)
    assert ARCHIVE_HEAD not in store.get_keys
    assert LIST_HEAD not in store.get_keys
    with sqlite3.connect(output) as connection:
        assert read_scope(connection) == archives.scope
        assert (
            connection.execute(
                "SELECT count(*) FROM edinet_source_archives WHERE doc_id='S100LATER'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("existing", ["file", "symlink"])
def test_existing_output_is_never_touched_or_read_remotely(tmp_path, existing):
    store, _, _, lists, archives, _ = fixtures(tmp_path)
    output = tmp_path / "existing.sqlite"
    if existing == "file":
        output.write_bytes(b"existing authority")
    else:
        output.symlink_to(tmp_path / "missing-target")
    store.get_keys.clear()
    with pytest.raises(FileExistsError, match="new output"):
        rebuild_fixed_edinet_input(lists, archives, output)
    assert store.get_keys == []
    assert (
        output.read_bytes() == b"existing authority" if existing == "file" else output.is_symlink()
    )


@pytest.mark.parametrize("suffix", [".zip", ".body.gz"])
@pytest.mark.parametrize("failure", ["missing", "changed"])
def test_bad_original_does_not_publish_partial_output(tmp_path, failure, suffix):
    store, _, _, lists, archives, _ = fixtures(tmp_path)
    body = next(key for key in store.values if key.endswith(suffix))
    if failure == "missing":
        del store.values[body]
    else:
        store.store_object(body, b"changed")
    output = tmp_path / "unpublished.sqlite"
    with pytest.raises(LakePublishError):
        rebuild_fixed_edinet_input(lists, archives, output)
    assert not output.exists()
    assert not list(tmp_path.glob("edinet-read-*"))


def test_competing_output_is_not_overwritten_after_input_rebuild(tmp_path, monkeypatch):
    _, _, _, lists, archives, _ = fixtures(tmp_path)
    output = tmp_path / "competing.sqlite"
    link = os.link

    def race_with_completed_output(source, destination):
        output.write_bytes(b"competing output")
        link(source, destination)

    monkeypatch.setattr(edinet_archive_native.os, "link", race_with_completed_output)
    with pytest.raises(FileExistsError):
        rebuild_fixed_edinet_input(lists, archives, output)
    assert output.read_bytes() == b"competing output"
    assert not list(tmp_path.glob("edinet-read-*"))


def test_empty_scope_remains_uninitialized_and_malformed_list_is_reported(tmp_path):
    _, list_owner, _, lists, _, _ = fixtures(tmp_path)
    empty_owner = EdinetArchiveL1(MemoryR2Store(), tmp_path / "empty-owner")
    archives = empty_owner.initialize()
    lists = list_owner.publish(
        [NativeListReceipt.from_response(DAY, EdinetResponse(KNOWN, b"{"), UUID(int=2).hex)]
    )
    output = tmp_path / "partial.sqlite"
    report = rebuild_fixed_edinet_input(lists, archives, output)
    assert report["status"] == "failed"
    assert report["list_failed_days"] == [DAY.isoformat()]
    with sqlite3.connect(output) as connection:
        assert read_scope(connection) == FactsScope()
        assert connection.execute("SELECT count(*) FROM edinet_source_archives").fetchone()[0] == 0
    assert EDINETReader(output).list_documents(DAY)
