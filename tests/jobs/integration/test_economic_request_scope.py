"""Only the economic fixed-input transport receives the serial request scope."""

from contextlib import contextmanager

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tests.jobs.integration.test_model_integration import integration as integration

from baibai_jobs.integration import models


@pytest.mark.parametrize("failure", [False, True])
def test_economic_request_scope_closes_before_generation_or_on_failure(
    integration, monkeypatch, failure
):
    arguments, events = integration

    class PacedStore(MemoryR2Store):
        @contextmanager
        def serial_requests(self, *, min_interval_seconds):
            assert min_interval_seconds == 0.25
            events.append("economic-scope-enter")
            try:
                yield
            finally:
                events.append("economic-scope-exit")

    store = PacedStore()
    store.__dict__.update(arguments["store"].__dict__)
    arguments["store"] = store
    monkeypatch.setattr(models, "Boto3R2Store", PacedStore)
    if failure:

        def fail(*args, **kwargs):
            events.append("rebuild-failed")
            raise ValueError("economic fixed input unavailable")

        monkeypatch.setattr(models.EconomicInputSession, "rebuild", fail)
        with pytest.raises(ValueError, match="fixed input unavailable"):
            models.integrate_models(**arguments)
        assert "generate" not in events
        assert "publish" not in events
    else:
        assert models.integrate_models(**arguments)["models_ready"] is True
        assert events.index("economic-scope-exit") < events.index("generate")
        assert events.index("economic-scope-exit") < events.index("publish")
    assert events.count("economic-scope-enter") == events.count("economic-scope-exit") == 1
    assert events.index("economic-scope-enter") < events.index("retain")
    assert events.index("economic-scope-enter") < events.index("economic-scope-exit")
