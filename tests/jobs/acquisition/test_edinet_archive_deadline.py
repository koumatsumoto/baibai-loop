"""通常取得の期限切れでも確定済みnative原本を残し、旧stageへ戻さない。"""

import pytest
from tests.jobs.integration.test_edinet_archive_native_route import native_cli

from baibai_engine.data_collection.transport.http import AcquisitionDeadlineExceeded
from baibai_jobs.integration import source_handoff


@pytest.mark.parametrize("source", ["edinet-metrics", "edinet-facts"])
def test_archive_deadline_keeps_native_originals_without_exporting_legacy_stage(
    source, tmp_path, monkeypatch
):
    owner, client, directory, output, argv, _ = native_cli(tmp_path, monkeypatch, group=source)
    response = client.fetch_archive.return_value
    client.fetch_archive.side_effect = [
        response,
        AcquisitionDeadlineExceeded("acquisition job deadline exhausted"),
    ]
    assert source_handoff.main(argv) == 1
    assert client.fetch_archive.call_count == 2
    assert len(owner.freeze().archive_keys) == 1
    assert output.read_text().splitlines() == ["stage_ready=false", "native_l1=true"]
    assert not (directory / "receipts.sqlite").exists()
    assert not (tmp_path / "absent-market.sqlite").exists()
