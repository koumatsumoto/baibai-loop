"""固定原本unionの検証はstage数倍のgzip再展開を行わない。"""

import json
from contextlib import closing
from datetime import timedelta

from tests.helpers.r2_store import MemoryR2Store
from tests.jobs.maintenance.test_edinet_list_cutover import (
    AT,
    prepare,
    project_fixture,
    row,
    valid_body,
)

from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.maintenance import edinet_list_cutover as cutover


def test_projection_origins_decode_the_same_fixed_union_only_once(tmp_path, monkeypatch):
    store = MemoryR2Store()
    stage_count = 8
    for number in range(stage_count):
        source = cutover.SOURCES[number % len(cutover.SOURCES)]
        packet = tmp_path / f"packet-{number}"
        packet.mkdir()
        original = row(at=AT + timedelta(seconds=number + 1), body=valid_body())
        with closing(open_connection(packet / "receipts.sqlite")) as conn, conn:
            conn.execute("INSERT INTO edinet_list_responses VALUES (?,?,?,?)", original)
        project_fixture(packet / "receipts.sqlite")
        (packet / "result.json").write_text(
            json.dumps(
                {
                    "source": source,
                    "asof": original[1],
                    "started_at": original[2],
                    "list_receipts": [original[0]],
                }
            )
        )
        save_handoff(store, f"{source}-projection-{number}", packet, market=True)
    fixed_before = {key: value.body for key, value in store.values.items()}
    validate = cutover._receipt
    calls = []

    def counted(item, body):
        calls.append(item["receipt_id"])
        return validate(item, body)

    monkeypatch.setattr(cutover, "_receipt", counted)
    _, plan = prepare(tmp_path, [row(body=valid_body())], store=store, configure=project_fixture)
    assert not plan["blocked"]
    assert not plan["missing_raw_days"]
    assert len(plan["receipts"]) == stage_count + 1
    assert len(plan["handoffs"]) == stage_count
    # 受領時のgzip検証と、全originが共有する投影用の展開を各一回。
    assert len(calls) == 2 * len(plan["receipts"])
    assert all(store.values[key].body == value for key, value in fixed_before.items())
