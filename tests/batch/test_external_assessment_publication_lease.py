from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.batch.test_external_assessment_publication import publication as publication

from baibai_batch.jobs import external_assessment_publication as external_assessment
from baibai_batch.storage import lake_publish, publication_lease
from baibai_batch.storage import publish_market_lake as publisher
from baibai_batch.storage.publication_lease import LeaseError


@pytest.mark.parametrize("failure", ["expired", "replaced"])
def test_lease_lost_during_remote_closure_stops_before_pointer_switch(
    publication, monkeypatch, failure
):
    store, plan, handle, database, mirror, tmp_path = publication
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
        external_assessment.publish_and_verify(
            store,
            plan,
            handle=handle,
            sqlite_path=database,
            mirror=mirror,
            directory=tmp_path / "rejected-mid-publication",
        )
    assert injected
    assert publisher._serving_pointer(store).pointer == previous
    assert [key for key in store.put_keys if "/pointers/" in key] == previous_pointer_puts
    assert not any(key.endswith("/published.json") for key in store.values)


def test_pointer_generation_conflict_remains_enforced_with_a_valid_lease(publication):
    store, plan, handle, database, mirror, tmp_path = publication
    previous = publisher._serving_pointer(store).pointer
    store.conflict_pointer = True
    with pytest.raises(lake_publish.LakeCASConflict):
        external_assessment.publish_and_verify(
            store,
            plan,
            handle=handle,
            sqlite_path=database,
            mirror=mirror,
            directory=tmp_path / "generation-conflict",
        )
    assert publisher._serving_pointer(store).pointer == previous
    assert not any(key.endswith("/published.json") for key in store.values)
