from baibai_engine.data_collection.providers.edinet.archive_plan import select_holder_archives


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


def test_candidate_plan_keeps_all_issuers_and_skips_only_retained_or_unavailable():
    docs = [
        filing("S100HOLDER1", "350", "2026-10-01 09:00", issuerEdinetCode=None),
        filing("S100HOLDER2", "360", "2026-10-02 09:00", issuerEdinetCode="E00002"),
        filing("S100RETAIN", "350", "2026-10-03 09:00"),
        filing("S100NOXBRL", "350", "2026-10-04 09:00", xbrlFlag="0"),
        filing("S100ANNUAL", "120", "2026-10-05 09:00"),
    ]
    candidates, quarantined = select_holder_archives(docs, retained={("S100RETAIN", 1)})
    assert candidates == ["S100HOLDER2", "S100HOLDER1"]
    assert quarantined == 0
