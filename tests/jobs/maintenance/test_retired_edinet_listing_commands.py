"""移行済み一覧の旧取得入口を戻さず、保存原本の再生成を残す。"""

import pytest

from baibai_jobs.maintenance.market import cli
from baibai_jobs.maintenance.source_refresh.edinet import EDINETRefresh


@pytest.mark.parametrize(
    "argv",
    [
        ["refresh-edinet-documents", "--asof", "2026-11-07"],
        ["backfill-edinet-identity", "--start", "2026-11-06", "--end", "2026-11-07"],
        ["bootstrap-cache", "--asof", "2026-11-07", "--source-group", "edinet-list"],
    ],
)
def test_retired_list_commands_fail_before_environment_or_provider_access(monkeypatch, argv):
    monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", "false")

    def unexpected(*_args, **_kwargs):
        pytest.fail("retired list command reached environment or provider setup")

    monkeypatch.setattr(cli, "load_project_env", unexpected)
    monkeypatch.setattr(cli, "EDINETRefresh", unexpected)
    with pytest.raises(SystemExit) as error:
        cli.main(argv)
    assert error.value.code == 2


def test_removed_writer_methods_do_not_remove_saved_list_rebuild():
    for name in (
        "bootstrap_cache",
        "refresh_document_state",
        "backfill_document_identity",
        "_refresh_document_state",
    ):
        assert not hasattr(EDINETRefresh, name)
    args = cli.build_parser().parse_args(
        ["rebuild-edinet-lists", "--start", "2026-11-06", "--end", "2026-11-07"]
    )
    assert args.command == "rebuild-edinet-lists"
