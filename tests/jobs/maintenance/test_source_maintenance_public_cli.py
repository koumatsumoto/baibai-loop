import pytest

from baibai_jobs.maintenance.market import cli as app


@pytest.mark.parametrize(
    ("command", "handler"),
    [
        ("refresh-tse-capital-policy", "refresh_tse_capital_policy_command"),
        ("refresh-jpx-delistings", "refresh_jpx_delistings_command"),
    ],
)
def test_public_refresh_does_not_initialize_unrelated_providers(tmp_path, mocker, command, handler):
    mocker.patch.object(
        app.SourceMaintenanceConfig,
        "from_env",
        side_effect=AssertionError("unrelated provider config"),
    )
    refresh = mocker.patch.object(app, handler, return_value=0)
    path = tmp_path / "market.sqlite"
    assert app.main([command, "--sqlite-path", str(path)]) == 0
    refresh.assert_called_once_with(sqlite_path=path)
