from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from tests.batch.test_external_assessment_publication import publication as publication
from tests.batch.test_external_assessment_publication_partial import partial_batch as partial_batch

from baibai_batch.jobs import external_assessment_daily as daily
from baibai_batch.observability import external_assessment_discord as notify
from baibai_batch.storage import external_assessment_staging as staging


def outputs(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def notify_from_outputs(monkeypatch, values, *, partial, previous=False, acquire_skipped=False):
    monkeypatch.setenv(
        "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/id/synthetic-secret"
    )
    sent = []

    def transport(_url, body, _timeout):
        sent.append(json.loads(body))
        return 200

    arguments = [
        "--plan-result",
        "success",
        "--acquire-result",
        "skipped" if partial or previous or acquire_skipped else "success",
        "--publish-result",
        "skipped" if previous else "success",
        "--terminal-status",
        values.get("terminal_status", "pending"),
        "--publication-mode",
        "confirmed_partial" if partial else "complete",
    ]
    for counter in notify.COUNTERS:
        arguments += [
            f"--{counter.replace('_', '-')}",
            values.get(f"external_assessment_{counter}", "unknown"),
        ]
    assert notify.main(arguments, transport=transport) == 0
    assert len(sent) == 1
    assert sent[0]["allowed_mentions"] == {"parse": []}
    assert "synthetic-secret" not in sent[0]["content"]
    return sent[0]["content"]


@pytest.mark.parametrize("old_binding", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_real_cli_plan_publication_seal_and_resume_route_to_one_safe_notice(
    publication, partial_batch, tmp_path, monkeypatch, partial, old_binding
):
    # Both paths use the real MemoryR2Store, publisher and typed readback. Only
    # fixture binding is made equivalent to the saved-master CLI assertion.
    if partial:
        store, original_plan, handle, database, mirror, root, _ = partial_batch
    else:
        store, original_plan, handle, database, mirror, root = publication
    p = replace(original_plan, universe_binding=json.dumps({"master_asof": "2026-10-09"}))
    if old_binding:
        p = replace(p, collector_binding="previous-collector")

    def forbidden_http(*args, **kwargs):
        raise AssertionError("confirmed stage must not acquire provider responses")

    monkeypatch.setattr(daily, "external_assessment_collect_pair", forbidden_http)
    journals = staging.read_stages(store, original_plan)
    for index, (checkpoint, _) in journals.items():
        rebound = replace(checkpoint, result=replace(checkpoint.result, plan_sha256=p.plan_sha256))
        store.store_object(staging.stage_key(p, index), staging.stage_bytes(p, rebound))
    staging.save_new_plan(store, p)
    monkeypatch.setattr(daily, "Boto3R2Store", lambda **kwargs: store)
    from baibai_batch.jobs import external_assessment_publication as external_assessment_publication
    from baibai_batch.storage import publication_lease

    monkeypatch.setattr(publication_lease, "_load_handle", lambda *args: handle)
    # The store mirror is the fixture's equivalent of canonical market/lake root.
    publish = external_assessment_publication.publish_and_verify

    def with_fixture_mirror(*args, **kwargs):
        return publish(*args, **{**kwargs, "mirror": mirror})

    monkeypatch.setattr(external_assessment_publication, "publish_and_verify", with_fixture_mirror)
    path = tmp_path / "plan-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    plan_arguments = ["plan", "--run-id", p.run_id, "--master-asof", "2026-10-09"]
    if partial:
        plan_arguments += ["--require-existing-plan"]
    assert daily.main(plan_arguments) == 0
    first_plan = outputs(path)
    assert first_plan["terminal_status"] == "pending"
    assert first_plan["cohorts"] == "[]"  # confirmed stage proceeds directly to publication
    # Read the real conditions the workflow uses; a sealed plan gates both jobs.
    workflow = yaml.load(
        (
            Path(__file__).resolve().parents[2] / ".github/workflows/cloud-external-assessment.yml"
        ).read_text(),
        Loader=yaml.BaseLoader,
    )
    assert "needs.plan.outputs.terminal_status == 'pending'" in workflow["jobs"]["acquire"]["if"]
    assert "needs.plan.outputs.terminal_status == 'pending'" in workflow["jobs"]["publish"]["if"]
    assert "!cancelled()" in workflow["jobs"]["publish"]["if"]
    assert "needs.plan.outputs.cohorts == '[]'" in workflow["jobs"]["publish"]["if"]
    assert "needs.plan.outputs.cohorts != '[]'" in workflow["jobs"]["acquire"]["if"]
    if partial:
        # Pending pairs are eligible; the fixture refuses provider acquisition.
        assert daily.main(["acquire", "--run-id", p.run_id, "--cohort", "1"]) == 1
    report_path = tmp_path / "publish-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(report_path))
    command = "publish-confirmed-partial" if partial else "publish"
    assert (
        daily.main(
            [
                command,
                "--run-id",
                p.run_id,
                "--db",
                str(database),
                "--directory",
                str(root / "workflow-readback"),
                "--lease-handle",
                str(root / "lease"),
            ]
        )
        == 0
    )
    current = outputs(report_path)
    message = notify_from_outputs(monkeypatch, current, partial=partial, acquire_skipped=True)
    assert message.startswith("[DEGRADED]")  # full fixture has one known parse failure
    if partial:
        assert current["external_assessment_unattempted_pairs"] == "3"
        assert "確定観測のみ部分公開確認済み" in message
    # A terminal remains authoritative after trial checkpoints are removed.
    terminal = staging.read_terminal(store, p)
    original_receipt = store.values[staging.prefix(p.run_id) + "/published.json"].body
    for cohort in p.cohorts:
        store.values.pop(staging.stage_key(p, cohort.index), None)
    store.get_keys.clear()
    store.head_keys.clear()
    store.put_keys.clear()

    def forbidden(*args, **kwargs):
        raise AssertionError("sealed workflow must skip acquisition and fresh publication")

    monkeypatch.setattr(daily, "external_assessment_collect_pair", forbidden)
    monkeypatch.setattr(daily, "read_stages", forbidden)
    monkeypatch.setattr(external_assessment_publication, "publish_and_verify", forbidden)
    monkeypatch.setattr(publication_lease, "_load_handle", forbidden)
    repeated_output = tmp_path / "repeat-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(repeated_output))
    assert daily.main(plan_arguments) == 0
    repeated = outputs(repeated_output)
    assert repeated["cohorts"] == "[]"
    assert repeated["terminal_status"] == staging.terminal_status(terminal)
    assert repeated["external_assessment_rows_added"] == "0"
    assert daily.main(["acquire", "--run-id", p.run_id, "--cohort", "0"]) == 0
    assert daily.main(["publish", "--run-id", p.run_id]) == 0
    assert daily.main(["publish-confirmed-partial", "--run-id", p.run_id]) == 0
    previous_message = notify_from_outputs(monkeypatch, repeated, partial=partial, previous=True)
    assert previous_message.startswith("[SKIPPED]")
    assert ("以前の部分公開確認済み" if partial else "以前の完全公開確認済み") in previous_message
    assert store.values[staging.prefix(p.run_id) + "/published.json"].body == original_receipt
    assert store.put_keys == []
    allowed = {staging.prefix(p.run_id) + suffix for suffix in ("/plan.json", "/published.json")}
    assert set(store.get_keys) <= allowed
    assert set(store.head_keys) <= allowed
