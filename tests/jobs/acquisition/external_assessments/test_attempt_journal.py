"""原本upload後の試行履歴は、可変stage CASの成否と独立に残す。"""

import json

import pytest
from tests.helpers.external_assessment_stages import FakeObjectStore, plan, wire_success

import baibai_jobs.acquisition.external_assessments.daily as daily
import baibai_jobs.acquisition.external_assessments.staging as staging
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.acquisition.external_assessments.attempts import read_attempts, save_attempts


@pytest.mark.parametrize("fault", ["after", "bad_receipt"])
def test_ambiguous_journal_write_reads_same_payload_before_checkpoint(monkeypatch, fault):
    store, p = FakeObjectStore(), plan()
    wire_success(monkeypatch, store, p)
    store.put_fault = (2, fault)
    assert daily.acquire_cohort(store, p, 0)["cohort_complete"]
    journals = read_attempts(store, p)
    assert len(journals) == 1
    saved = staging.read_stage(store, p, 0)[0]
    assert journals[0]["candidate_stage"] == json.loads(staging.stage_bytes(p, saved))
    before = list(store.puts)
    assert save_attempts(store, p, saved, journals[0]["attempts"]) == journals[0]["key"]
    assert store.puts == before


def test_journal_failure_keeps_original_and_does_not_commit_checkpoint(monkeypatch):
    store, p = FakeObjectStore(), plan()
    wire_success(monkeypatch, store, p)
    store.put_fault = (2, "before")
    with pytest.raises(LakePublishError):
        daily.acquire_cohort(store, p, 0)
    assert staging.read_stage(store, p, 0) is None
    assert read_attempts(store, p) == []
    assert any(key.startswith("external-assessment/retained/") for key in store.objects)


def test_journal_is_read_only_and_rejects_content_key_mismatch(monkeypatch):
    store, p = FakeObjectStore(), plan()
    wire_success(monkeypatch, store, p)
    daily.acquire_cohort(store, p, 0)
    journal = read_attempts(store, p)[0]
    before = list(store.puts)
    assert read_attempts(store, p) == [journal]
    assert store.puts == before
    key = journal.pop("key")
    journal["attempts"][0]["requests"] += 1
    store.seed(key, json.dumps(journal).encode())
    with pytest.raises(ValueError, match="binding changed"):
        read_attempts(store, p)
