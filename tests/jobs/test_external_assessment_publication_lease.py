from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.jobs.external_assessment_publication_fixtures import (
    integrate_ready_plans,
)
from tests.jobs.external_assessment_publication_fixtures import publication as publication

import baibai_jobs.publication.lease as publication_lease
import baibai_jobs.publication.snapshot_publication as publisher
import baibai_jobs.publication.snapshot_release as lake_publish
from baibai_jobs.publication.lease import LeaseError


@pytest.mark.parametrize("failure", ["expired", "replaced"])
def test_lease_lost_during_remote_closure_stops_before_pointer_switch(
    publication, monkeypatch, failure
):
    store, plan, handle, *_ = publication
    previous = publisher._serving_pointer(store).pointer
    previous_pointer_puts = [key for key in store.put_keys if "/pointers/" in key]
    ensure = lake_publish._ensure_immutable
    injected = False

    def lose_lease(*args, **kwargs):
        nonlocal injected
        result = ensure(*args, **kwargs)
        if not injected:
            injected = True
            if failure == "expired":

                class ExpiredClock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        moment = handle.expires_at_utc + timedelta(seconds=1)
                        return moment.astimezone(tz or UTC)

                monkeypatch.setattr(publication_lease, "datetime", ExpiredClock)
            else:
                publication_lease.release(store, handle)
                # One Actions run has a stable owner; model a different writer.
                with monkeypatch.context() as other_writer:
                    other_writer.setenv("GITHUB_ACTIONS", "true")
                    other_writer.setenv(
                        "GITHUB_RUN_ID", "2" if handle.owner == "github:1:1" else "1"
                    )
                    other_writer.setenv("GITHUB_RUN_ATTEMPT", "1")
                    other, _ = publication_lease.acquire(store, "operator")
                assert other.owner != handle.owner
        return result

    monkeypatch.setattr(lake_publish, "_ensure_immutable", lose_lease)
    with pytest.raises(LeaseError):
        integrate_ready_plans(publication, [plan], "rejected-mid-publication")
    assert injected
    assert publisher._serving_pointer(store).pointer == previous
    assert [key for key in store.put_keys if "/pointers/" in key] == previous_pointer_puts
    assert not any(key.endswith("/published.json") for key in store.values)


def test_pointer_generation_conflict_remains_enforced_with_a_valid_lease(publication):
    store, plan, *_ = publication
    previous = publisher._serving_pointer(store).pointer
    store.conflict_pointer = True
    with pytest.raises(lake_publish.LakeCASConflict):
        integrate_ready_plans(publication, [plan], "generation-conflict")
    assert publisher._serving_pointer(store).pointer == previous
    assert not any(key.endswith("/published.json") for key in store.values)
