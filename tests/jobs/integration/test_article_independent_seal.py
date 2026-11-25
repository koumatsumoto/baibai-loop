"""Previously prepared article writes cannot re-enter present-day integration."""

import json

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_jobs.integration import sources


@pytest.mark.parametrize("command", ["apply", "seal"])
@pytest.mark.parametrize(
    "article",
    [
        {"source": "boj_news"},
        {"namespace": "article-sources", "source": "future-or-invalid-source"},
    ],
)
def test_old_article_write_fails_before_source_io_or_mutation(
    tmp_path, monkeypatch, capsys, command, article
):
    work = tmp_path / "integration"
    work.mkdir()
    market = tmp_path / "market.sqlite"
    market.write_bytes(b"the database must not be opened")
    entry = {
        "market": True,
        "stage_id": "old-private-stage",
        "manifest_sha256": "a" * 64,
        **article,
    }
    incoming = work / ("plan.json" if command == "apply" else "applied.json")
    incoming.write_text(
        json.dumps(
            {"handoffs": [entry], "edinet_input": {}}
            if command == "apply"
            else {"handoffs": [{"entry": entry, "applied": True}]}
        )
    )
    before = incoming.read_bytes()
    store = MemoryR2Store()
    monkeypatch.setattr(store, "close", lambda: None, raising=False)

    def unexpected(*_args, **_kwargs):
        pytest.fail("old article plan reached the source or canonical IO")

    for name in (
        "Boto3R2Store",
        "read_edinet_input",
        "download_fixed",
        "_load_handle",
        "verify_held",
        "canonical_generations",
        "seal_integrated",
    ):
        monkeypatch.setattr(sources, name, unexpected)
    assert (
        sources.main(
            [
                command,
                "--directory",
                str(work),
                "--market",
                str(market),
                "--asof",
                "2026-11-25",
                "--lease-handle",
                str(tmp_path / "unused"),
            ]
        )
        == 1
    )
    assert "common source integration failed: RetiredArticleHandoffError" in capsys.readouterr().err
    assert incoming.read_bytes() == before
    assert list(work.iterdir()) == [incoming]
    assert market.read_bytes() == b"the database must not be opened"
    assert not (store.get_keys + store.head_keys + store.put_keys + store.delete_keys)
