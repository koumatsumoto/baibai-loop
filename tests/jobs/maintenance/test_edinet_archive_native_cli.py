"""明示CLIは通常切替を行わず、固定読取・再公開ではHTTPしない。"""

import json
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.archive_native import (
    EdinetArchiveL1,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.maintenance import edinet_archive_native as cli


def test_explicit_bucket_is_required():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["initialize", "--directory", "not-used"])


def test_initialize_and_freeze_without_provider_or_list_head(tmp_path, monkeypatch, capsys):
    store = MemoryR2Store()
    store.close = Mock()
    factory, http = Mock(return_value=store), Mock()
    monkeypatch.setattr(cli, "Boto3R2Store", factory)
    monkeypatch.setattr(cli, "EDINETHTTP", http)
    options = ["--bucket", "isolated-fixture", "--directory", str(tmp_path)]
    assert cli.main(["initialize", *options]) == 0
    owner = EdinetArchiveL1(store, tmp_path)
    fixed = owner.publish(
        [
            NativeArchive.from_response(
                "S100ONE", 1, EdinetResponse(datetime(2026, 11, 5, tzinfo=UTC), b"legacy bytes")
            )
        ]
    )
    assert cli.main(["freeze", *options]) == 0
    assert json.loads((tmp_path / "archive-head.json").read_bytes()) == fixed.value()
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["l1_identity"] == fixed.identity
    factory.assert_called_with(bucket="isolated-fixture")
    http.assert_not_called()


def test_invalid_acquire_stops_before_remote_or_provider(tmp_path, monkeypatch):
    factory, http = Mock(), Mock()
    monkeypatch.setattr(cli, "Boto3R2Store", factory)
    monkeypatch.setattr(cli, "EDINETHTTP", http)
    assert cli.main(["acquire", "--bucket", "isolated-fixture", "--directory", str(tmp_path)]) == 1
    factory.assert_not_called()
    http.assert_not_called()


def test_commit_work_publishes_preserved_bytes_without_http(tmp_path, monkeypatch):
    store = MemoryR2Store()
    store.close = Mock()
    owner = EdinetArchiveL1(store, tmp_path)
    owner.initialize()
    put_file = store.put_file
    store.put_file = Mock(side_effect=LakePublishError("injected failure"))
    item = NativeArchive.from_response(
        "S100ONE", 1, EdinetResponse(datetime(2026, 11, 5, tzinfo=UTC), b"original")
    )
    with pytest.raises(LakePublishError, match="injected failure"):
        owner.publish([item])
    store.put_file = put_file
    monkeypatch.setattr(cli, "Boto3R2Store", Mock(return_value=store))
    http = Mock()
    monkeypatch.setattr(cli, "EDINETHTTP", http)
    assert (
        cli.main(["commit-work", "--bucket", "isolated-fixture", "--directory", str(tmp_path)]) == 0
    )
    assert owner.freeze().metadata("S100ONE", 1) == item.metadata
    assert owner.freeze().read_xbrl_zip("S100ONE") == item.content
    http.assert_not_called()
