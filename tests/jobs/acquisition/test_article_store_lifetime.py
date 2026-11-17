"""The article command releases its store without changing source failure boundaries."""

import json

import pytest

from baibai_jobs.acquisition import articles


@pytest.mark.parametrize("failure", [None, OSError, KeyboardInterrupt])
def test_owned_store_closes_after_source_pass(tmp_path, monkeypatch, capsys, failure):
    events, stores = [], []
    marker = tmp_path / "saved-receipt"
    marker.write_bytes(b"original")

    class Store:
        def __init__(self, **kwargs):
            self.closed = False
            stores.append(self)
            events.append("store")

        def close(self):
            assert not self.closed
            self.closed = True
            events.append("close")

    class Session:
        def __enter__(self):
            events.append("session")
            return self

        def __exit__(self, *_):
            events.append("session-close")

    def acquire(store, *, source, **kwargs):
        assert store.store is stores[0]
        assert not stores[0].closed
        events.append(source)
        if source == "first" and failure is not None:
            raise failure("PRIVATE_CANARY")
        return {"source": source, "status": "completed"}

    monkeypatch.setitem(articles.GROUPS, "public", ("first", "second"))
    monkeypatch.setattr(articles, "Boto3R2Store", Store)
    monkeypatch.setattr(articles.requests, "Session", Session)
    monkeypatch.setattr(articles, "acquire_source", acquire)
    args = [
        "--group",
        "public",
        "--created-at",
        "2026-10-29T13:13:00+00:00",
        "--identity",
        "synthetic",
        "--directory",
        str(tmp_path),
    ]
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt, match="PRIVATE_CANARY"):
            articles.main(args)
    else:
        assert articles.main(args) == int(failure is not None)
    assert events == ["store", "session", "first"] + (
        [] if failure is KeyboardInterrupt else ["second"]
    ) + ["session-close", "close"]
    assert len(stores) == 1
    assert stores[0].closed
    assert marker.read_bytes() == b"original"
    captured = capsys.readouterr()
    assert "PRIVATE_CANARY" not in captured.out + captured.err
    if failure is KeyboardInterrupt:
        assert captured.out == ""
    else:
        reports = json.loads(captured.out)["sources"]
        assert reports[0] == (
            {"source": "first", "status": "completed"}
            if failure is None
            else {"source": "first", "status": "failed", "reason": "OSError"}
        )
        assert reports[1] == {"source": "second", "status": "completed"}


@pytest.mark.parametrize("boundary", ["metrics", "session", "output"])
def test_owned_store_closes_when_invocation_setup_or_output_fails(tmp_path, monkeypatch, boundary):
    events = []
    before = list(tmp_path.iterdir())

    class Store:
        def __init__(self, **kwargs):
            events.append("store")

        def close(self):
            events.append("close")

    def fail(*args, **kwargs):
        raise OSError("synthetic boundary failure")

    monkeypatch.setattr(articles, "Boto3R2Store", Store)
    monkeypatch.setitem(articles.GROUPS, "public", ())
    if boundary == "metrics":
        monkeypatch.setattr(articles, "ArticleObjectMetrics", fail)
    elif boundary == "session":
        monkeypatch.setattr(articles.requests, "Session", fail)
    else:
        monkeypatch.setattr("builtins.print", fail)
    with pytest.raises(OSError, match="synthetic boundary failure"):
        articles.main(
            [
                "--group",
                "public",
                "--created-at",
                "2026-10-29T13:13:00+00:00",
                "--identity",
                "synthetic",
                "--directory",
                str(tmp_path),
            ]
        )
    assert events == ["store", "close"]
    assert list(tmp_path.iterdir()) == before


def test_failed_constructor_has_no_store_to_close(tmp_path, monkeypatch):
    events = []
    before = list(tmp_path.iterdir())

    class Store:
        def __init__(self, **kwargs):
            events.append("construct")
            raise OSError("synthetic constructor failure")

        def close(self):
            pytest.fail("no completed store constructor")

    monkeypatch.setattr(articles, "Boto3R2Store", Store)
    with pytest.raises(OSError, match="synthetic constructor failure"):
        articles.main(
            [
                "--group",
                "public",
                "--created-at",
                "2026-10-29T13:13:00+00:00",
                "--identity",
                "synthetic",
                "--directory",
                str(tmp_path),
            ]
        )
    assert events == ["construct"]
    assert list(tmp_path.iterdir()) == before
