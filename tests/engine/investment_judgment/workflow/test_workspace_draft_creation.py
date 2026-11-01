"""新規下書きは競合相手を上書きせず、明示forceだけが置換する。"""

from datetime import datetime

import pytest
import yaml
from tests.helpers.assessment_v1 import pair_payload
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection

from baibai_engine.investment_judgment.workflow.workspace import (
    ResearchWorkspaceConflictError,
    prepare_selected_workspace,
    scaffold_assessment,
    scaffold_review,
)
from baibai_engine.investment_judgment.workflow.workspace import scaffold as owner

NOW = datetime.fromisoformat("2026-09-07T18:00:00+09:00")


def _case(tmp_path, target):
    workspace, app, runs = tmp_path / "workspace", tmp_path / "app", tmp_path / "runs"
    selection = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",))
    prepare_selected_workspace(
        draft=generated_draft_for(selection),
        db_path=app,
        workspace=workspace,
        runs_path=runs,
        now=NOW,
    )
    directory = workspace / "1234"
    directory.mkdir()
    assessment = directory / "assessment-draft.yaml"
    identity = directory / "assessment-publication.yaml"
    if target == "review":
        document, _ = pair_payload(ticker="1234", as_of="2026-09-07")
        assessment.write_text(yaml.safe_dump(document), encoding="utf-8")
        output = directory / "assessment-review.yaml"

        def create(*, force=False):
            return scaffold_review(workspace=workspace, ticker="1234", db_path=app, force=force)

    else:
        output = tmp_path / "export.yaml" if target == "export" else assessment

        def create(*, force=False):
            return scaffold_assessment(
                workspace=workspace,
                ticker="1234",
                sqlite_path=tmp_path / "absent-market",
                as_of=NOW.date(),
                retrieved_at=NOW,
                db_path=app,
                force=force,
                out=output if target == "export" else None,
            )

    return create, output, identity


@pytest.mark.parametrize("target", ["assessment", "review", "export"])
@pytest.mark.parametrize("collision", ["existing", "dangling_symlink", "concurrent"])
def test_new_draft_preserves_existing_or_concurrent_file(tmp_path, monkeypatch, target, collision):
    create, output, identity = _case(tmp_path, target)
    original = "# another author's draft\nrefresh: null\n"
    missing = tmp_path / "missing-target"
    if collision == "dangling_symlink":
        output.symlink_to(missing)
    elif collision == "existing":
        output.write_text(original, encoding="utf-8")
    else:
        write_new = owner.write_text_new

        def compete(path, content):
            if path == output:
                path.write_text(original, encoding="utf-8")
            write_new(path, content)

        monkeypatch.setattr(owner, "write_text_new", compete)
    identity_before = identity.read_bytes() if identity.exists() else None
    with pytest.raises(ResearchWorkspaceConflictError):
        create()
    if collision == "dangling_symlink":
        assert output.is_symlink()
        assert not missing.exists()
    else:
        assert output.read_text(encoding="utf-8") == original
    if target != "export" or collision != "concurrent":
        assert (identity.read_bytes() if identity.exists() else None) == identity_before
    else:
        # Export failure does not undo a successfully created workspace draft.
        assert identity.exists()
        assert (identity.parent / "assessment-draft.yaml").exists()


@pytest.mark.parametrize("target", ["assessment", "review", "export"])
def test_explicit_force_regenerates_draft_without_following_symlink(tmp_path, target):
    create, output, _ = _case(tmp_path, target)
    external = tmp_path / "other-author.yaml"
    external.write_text("refresh: null\n", encoding="utf-8")
    output.symlink_to(external)
    create(force=True)
    assert not output.is_symlink()
    assert output.read_text(encoding="utf-8").startswith("#")
    assert external.read_text(encoding="utf-8") == "refresh: null\n"
