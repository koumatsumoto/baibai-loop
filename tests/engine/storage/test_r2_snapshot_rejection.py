"""Lake readers preserve operation-wide rejection controls and explicit credentials."""

from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from baibai_engine.storage.objects import r2
from baibai_engine.storage.snapshots.keys import current_release_pointer_key
from baibai_engine.storage.snapshots.objects import (
    LakeObjectError,
    R2ObjectSource,
    R2ReadCredentials,
)


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("stream", [False, True])
def test_snapshot_adapter_preserves_strict_rejection_and_default(
    monkeypatch, tmp_path, strict, status, stream
):
    monkeypatch.delenv("R2_STOP_ON_REJECTION", raising=False)
    monkeypatch.delenv("R2_REJECTION_STOP_FILE", raising=False)
    stop_file = tmp_path / "rejected"
    if strict:
        monkeypatch.setenv("R2_STOP_ON_REJECTION", "1")
        monkeypatch.setenv("R2_REJECTION_STOP_FILE", str(stop_file))
    # Explicit read credentials must not be replaced by ambient credentials.
    monkeypatch.setenv("R2_ACCOUNT_ID", "b" * 32)
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "B" * 32)
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "ambient-secret")
    credentials = R2ReadCredentials("a" * 32, "A" * 32, "read-secret", "private-test")
    configurations = []
    requests = []

    def get_object(**kwargs):
        requests.append(kwargs)
        raise ClientError(
            {
                "Error": {"Code": str(status), "Message": "PRIVATE"},
                "ResponseMetadata": {"HTTPStatusCode": status},
            },
            "GetObject",
        )

    def client(*args, **kwargs):
        configurations.append(kwargs)
        return SimpleNamespace(get_object=get_object, close=lambda: None)

    monkeypatch.setattr(r2.boto3.session, "Session", lambda: SimpleNamespace(client=client))
    source = R2ObjectSource(credentials)
    key = current_release_pointer_key()

    def read():
        if stream:
            source.download_file(key, tmp_path / "download", max_bytes=42)
        else:
            source.read_bytes(key)

    expected = r2.R2OperationStopped if strict else LakeObjectError
    for _ in range(2):
        with pytest.raises(expected):
            read()
    assert len(requests) == (1 if strict else 2)
    assert len(configurations) == 1
    config = configurations[0]
    assert config["config"].retries["total_max_attempts"] == (1 if strict else 3)
    assert config["aws_access_key_id"] == credentials.access_key_id
    assert config["aws_secret_access_key"] == credentials.secret_access_key
    assert config["endpoint_url"] == f"https://{credentials.account_id}.r2.cloudflarestorage.com"
    assert not (tmp_path / "download").exists()
    source.close()
    if strict:
        assert stop_file.read_text() == f"{status}\n"
        second = R2ObjectSource(credentials)
        with pytest.raises(r2.R2OperationStopped, match="previously stopped"):
            second.read_bytes(key)
        assert len(configurations) == 1
        second.close()
    else:
        assert not stop_file.exists()
