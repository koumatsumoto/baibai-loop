"""Reject GitHub Actions changes that widen a credential trust boundary."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

_GITHUB_EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
_INPUT_CONTEXT_TOKEN = re.compile(r"(?<![A-Za-z0-9_])inputs(?![A-Za-z0-9_])")
_CREDENTIAL_EXPRESSION = re.compile(
    r"\$\{\{[^}]*\bsecrets\b[^}]*\}\}|"
    r"\$\{\{[^}]*\bvars\b[^}]*\bR2_ACCOUNT_ID\b[^}]*\}\}",
    re.DOTALL,
)
_USES_LINE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(?P<reference>[^#\s]+)"
    r"(?:\s+#\s*(?P<comment>\S.*?))?\s*$"
)

# A reviewed upstream release is represented by both its immutable commit and its
# human-readable release. The offline gate accepts only these exact pairs.
_TRUSTED_ACTIONS = {
    "actions/cache": ("55cc8345863c7cc4c66a329aec7e433d2d1c52a9", "v6.1.0"),
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"),
    "actions/setup-node": ("820762786026740c76f36085b0efc47a31fe5020", "v7.0.0"),
    "actions/setup-python": ("5fda3b95a4ea91299a34e894583c3862153e4b97", "v7.0.0"),
    "astral-sh/setup-uv": ("c771a70e6277c0a99b617c7a806ffedaca235ff9", "v9.0.0"),
}

_R2 = {
    "R2_ACCOUNT_ID": "${{ vars.R2_ACCOUNT_ID }}",
    "R2_ACCESS_KEY_ID": "${{ secrets.R2_ACCESS_KEY_ID }}",
    # This is a GitHub expression, not a credential value.
    "R2_SECRET_ACCESS_KEY": "${{ secrets.R2_SECRET_ACCESS_KEY }}",  # nosec B105
}
_PROVIDERS = {
    "JQUANTS_API_KEY": "${{ secrets.JQUANTS_API_KEY }}",
    "ESTAT_APP_ID": "${{ secrets.ESTAT_APP_ID }}",
    "EDINET_API_KEY": "${{ secrets.EDINET_API_KEY }}",
}
_EXPECTED_STEP_CREDENTIALS: dict[tuple[str, str, str], dict[str, str]] = {
    ("cloud-daily-batch.yml", "daily", "Pull stores"): _R2,
    (
        "cloud-daily-batch.yml",
        "daily",
        "Preserve and verify market schema v13 rollback",
    ): _R2,
    ("cloud-daily-batch.yml", "daily", "Run daily batch"): _PROVIDERS,
    ("cloud-daily-batch.yml", "daily", "Upload updated machine stores"): _R2,
    ("cloud-daily-batch.yml", "daily", "Upload serving objects"): _R2,
    ("cloud-daily-batch.yml", "daily", "Notify Discord #batch-runs"): {
        "DISCORD_WEBHOOK_URL": "${{ secrets.DISCORD_WEBHOOK_URL }}"
    },
    ("cloud-daily-batch.yml", "daily", "Upload run summary"): _R2,
    ("cloud-history-backfill.yml", "backfill", "Pull the market store"): _R2,
    (
        "cloud-history-backfill.yml",
        "backfill",
        "Preserve and verify market schema v13 rollback",
    ): _R2,
    (
        "cloud-history-backfill.yml",
        "backfill",
        "Backfill and publish committed progress",
    ): {**_R2, "JQUANTS_API_KEY": "${{ secrets.JQUANTS_API_KEY }}"},
    ("cloud-materialize.yml", "materialize", "Pull stores"): _R2,
    ("cloud-materialize.yml", "materialize", "Upload serving objects"): _R2,
    ("web.yml", "quality", "Deploy Worker and UI assets"): {
        # This is a GitHub expression, not a credential value.
        "CLOUDFLARE_API_TOKEN": "${{ secrets.CLOUDFLARE_API_TOKEN }}",  # nosec B105
        "CLOUDFLARE_ACCOUNT_ID": "${{ vars.R2_ACCOUNT_ID }}",
    },
}
_EXPECTED_INPUT_ENV: dict[tuple[str, str, str], dict[str, str]] = {
    ("cloud-daily-batch.yml", "daily", "Validate dispatch input"): {
        "MANUAL_ASOF": "${{ inputs.asof }}"
    },
    ("cloud-history-backfill.yml", "backfill", "Validate dispatch inputs"): {
        "BACKFILL_START": "${{ inputs.start }}",
        "BACKFILL_END": "${{ inputs.end }}",
        "MASTER_MONTH_END_FROM": "${{ inputs.master_month_end_from }}",
    },
}
_EXPECTED_VALIDATED_OUTPUT_ENV: dict[tuple[str, str, str], dict[str, str]] = {
    ("cloud-daily-batch.yml", "daily", "Run daily batch"): {
        "MANUAL_ASOF": "${{ steps.validate-input.outputs.asof }}"
    },
    ("cloud-daily-batch.yml", "daily", "Notify Discord #batch-runs"): {
        "MANUAL_ASOF": "${{ steps.validate-input.outputs.asof }}"
    },
    (
        "cloud-history-backfill.yml",
        "backfill",
        "Backfill and publish committed progress",
    ): {
        "BACKFILL_START": "${{ steps.validate-input.outputs.start }}",
        "BACKFILL_END": "${{ steps.validate-input.outputs.end }}",
        "MASTER_MONTH_END_FROM": "${{ steps.validate-input.outputs.master_month_end_from }}",
    },
}
_EXPECTED_VALIDATION_SCRIPTS = {
    ("cloud-daily-batch.yml", "daily", "Validate dispatch input"): """
        python3 -m tools.cloud.validate_workflow_inputs daily --asof "$MANUAL_ASOF"
        echo "asof=$MANUAL_ASOF" >> "$GITHUB_OUTPUT"
    """,
    ("cloud-history-backfill.yml", "backfill", "Validate dispatch inputs"): """
        python3 -m tools.cloud.validate_workflow_inputs backfill \\
          --start "$BACKFILL_START" \\
          --end "$BACKFILL_END" \\
          --master-month-end-from "$MASTER_MONTH_END_FROM"
        echo "start=$BACKFILL_START" >> "$GITHUB_OUTPUT"
        echo "end=$BACKFILL_END" >> "$GITHUB_OUTPUT"
        echo "master_month_end_from=$MASTER_MONTH_END_FROM" >> "$GITHUB_OUTPUT"
    """,
}
# Each digest covers the full reviewed step mapping (run script, env, condition,
# shell, working directory, and error policy), not merely a command substring.
_EXPECTED_CREDENTIAL_STEP_DIGESTS = {
    ("cloud-daily-batch.yml", "daily", "Pull stores"): (
        "07a280053b55c74bc983ebd91d5f8f7b3ada2e50c9117de655d1ff28120b0ea9"
    ),
    ("cloud-daily-batch.yml", "daily", "Preserve and verify market schema v13 rollback"): (
        "f81af85e12fbe61a8ed419d56a34f6a0aece9ff8c9ca1d34297061f974b64615"
    ),
    ("cloud-daily-batch.yml", "daily", "Run daily batch"): (
        "6f0117793eecf3af161ca5651d6329c1d646da140dd628ce96a7452a099fa8ac"
    ),
    ("cloud-daily-batch.yml", "daily", "Upload updated machine stores"): (
        "1de8fefcc015bac98e2742825bf367c78fbd7573f424b291f49556ce543eb2c6"
    ),
    ("cloud-daily-batch.yml", "daily", "Upload serving objects"): (
        "df479f9ccf4f9aca46d4c7a354a66aae710783b17c807e2b8518f33fa0bb9cc8"
    ),
    ("cloud-daily-batch.yml", "daily", "Notify Discord #batch-runs"): (
        "ff8dbb534c9cec0563361e131189e97c5bbab2ad22c9fb9dce918ed3e216fb5e"
    ),
    ("cloud-daily-batch.yml", "daily", "Upload run summary"): (
        "68252336012dac8fc5d5bad8e447e96fa38f50b9c364b355633a9b24ef764a22"
    ),
    ("cloud-history-backfill.yml", "backfill", "Pull the market store"): (
        "8d91929fc219b7b4bc506845bc1ac4f79ddd3a11434e25391fea0ed0e628c715"
    ),
    (
        "cloud-history-backfill.yml",
        "backfill",
        "Preserve and verify market schema v13 rollback",
    ): "46539cf22f50ab68016f4cd79fe404baf4db309c32837cdf85c3a8f8871b728d",
    (
        "cloud-history-backfill.yml",
        "backfill",
        "Backfill and publish committed progress",
    ): "28de80b3b8217905d8974f3524b461f0b18487fb5df5889218a4a9fd635b0255",
    ("cloud-materialize.yml", "materialize", "Pull stores"): (
        "406ddbcec94d613165754b844b043db53d1b436695f7c9bfc4272fe4af3a6ada"
    ),
    ("cloud-materialize.yml", "materialize", "Upload serving objects"): (
        "a03ce272b9ff4dbf79507d93e6dcce195e003bd39c7e432765010fc22eb1857f"
    ),
    ("web.yml", "quality", "Deploy Worker and UI assets"): (
        "9d5ffb598490e0ca435a245c68aadb0d1e7b32098a0ac21aec824985f41ec076"
    ),
}
_RESTRICTED_ENV_NAMES = frozenset(
    name for credentials in _EXPECTED_STEP_CREDENTIALS.values() for name in credentials
)

PathPart = str | int
DocumentPath = tuple[PathPart, ...]


def _iter_scalars(value: object, path: DocumentPath = ()) -> Iterator[tuple[DocumentPath, str]]:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            yield from _iter_scalars(child, (*path, key))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            yield from _iter_scalars(child, (*path, index))
    elif isinstance(value, str):
        yield path, value


def _display(path: DocumentPath) -> str:
    return ".".join(f"[{part}]" if isinstance(part, int) else part for part in path)


def _normalize_script(script: str) -> str:
    lines = script.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    indentation = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
    return "\n".join(line[indentation:].rstrip() for line in lines)


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_value(child) for child in value]
    return value


def _step_digest(step: Mapping[object, object]) -> str:
    payload = json.dumps(
        _json_value(step),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _contains_dispatch_input(value: str) -> bool:
    return any(
        _INPUT_CONTEXT_TOKEN.search(match.group(0)) is not None
        for match in _GITHUB_EXPRESSION.finditer(value)
    )


def _mapping(value: object) -> Mapping[object, object] | None:
    return value if isinstance(value, Mapping) else None


def _sequence(value: object) -> Sequence[object] | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return None


def _uses_nodes(node: Node) -> Iterator[tuple[int, str]]:
    if isinstance(node, MappingNode):
        for key_node, value_node in node.value:
            if (
                isinstance(key_node, ScalarNode)
                and key_node.value == "uses"
                and isinstance(value_node, ScalarNode)
            ):
                yield value_node.start_mark.line, value_node.value
            yield from _uses_nodes(value_node)
    elif isinstance(node, SequenceNode):
        for child in node.value:
            yield from _uses_nodes(child)


def _trusted_action_errors(path: Path, text: str) -> list[str]:
    errors: list[str] = []
    root_node = yaml.compose(text, Loader=yaml.BaseLoader)
    if root_node is None:
        return [f"{path.name}: workflow must not be empty"]
    lines = text.splitlines()
    for zero_based_line, reference in _uses_nodes(root_node):
        line_number = zero_based_line + 1
        line = lines[zero_based_line]
        match = _USES_LINE.match(line)
        if reference.startswith("./"):
            continue
        if match is None or match.group("reference") != reference:
            errors.append(
                f"{path.name}:{line_number}: external uses must be a standalone pinned line "
                "with a release comment"
            )
            continue
        if "@" not in reference:
            errors.append(f"{path.name}:{line_number}: external action has no ref")
            continue
        action, revision = reference.rsplit("@", maxsplit=1)
        trusted = _TRUSTED_ACTIONS.get(action)
        if trusted is None:
            errors.append(f"{path.name}:{line_number}: unreviewed external action {action}")
            continue
        expected_revision, expected_comment = trusted
        if revision != expected_revision:
            errors.append(f"{path.name}:{line_number}: {action} must use reviewed full commit SHA")
        if match.group("comment") != expected_comment:
            errors.append(
                f"{path.name}:{line_number}: {action} must carry release comment {expected_comment}"
            )
    return errors


def _expected_step_env_locations(
    *,
    path: Path,
    workflow: Mapping[object, object],
    expected_steps: Mapping[tuple[str, str, str], Mapping[str, str]],
    boundary_name: str,
) -> tuple[dict[DocumentPath, str], list[str]]:
    allowed: dict[DocumentPath, str] = {}
    errors: list[str] = []
    jobs = _mapping(workflow.get("jobs"))
    if jobs is None:
        return allowed, [f"{path.name}: jobs must be a mapping"]

    for (filename, job_name, step_name), expected_env in expected_steps.items():
        if filename != path.name:
            continue
        job = _mapping(jobs.get(job_name))
        steps = _sequence(job.get("steps")) if job is not None else None
        if steps is None:
            errors.append(f"{path.name}: missing {boundary_name} target job {job_name}")
            continue
        matches: list[int] = []
        for index, raw_step in enumerate(steps):
            step = _mapping(raw_step)
            if step is not None and step.get("name") == step_name:
                matches.append(index)
        if len(matches) != 1:
            errors.append(
                f"{path.name}: expected one {boundary_name} target {job_name}/{step_name}, "
                f"found {len(matches)}"
            )
            continue
        index = matches[0]
        for env_name, expression in expected_env.items():
            allowed[("jobs", job_name, "steps", index, "env", env_name)] = expression
    return allowed, errors


def _credential_errors(*, path: Path, workflow: Mapping[object, object]) -> list[str]:
    allowed, errors = _expected_step_env_locations(
        path=path,
        workflow=workflow,
        expected_steps=_EXPECTED_STEP_CREDENTIALS,
        boundary_name="credential",
    )
    seen: set[DocumentPath] = set()

    for location, value in _iter_scalars(workflow):
        has_expression = _CREDENTIAL_EXPRESSION.search(value) is not None
        has_restricted_name = bool(location) and location[-1] in _RESTRICTED_ENV_NAMES
        if not (has_expression or has_restricted_name):
            continue
        expected = allowed.get(location)
        if expected is None:
            errors.append(
                f"{path.name}:{_display(location)}: credential is outside its target step env"
            )
            continue
        seen.add(location)
        if value != expected:
            errors.append(
                f"{path.name}:{_display(location)}: credential expression must be {expected}"
            )

    for location in sorted(set(allowed) - seen, key=str):
        errors.append(f"{path.name}:{_display(location)}: required scoped credential is missing")
    jobs = _mapping(workflow.get("jobs"))
    if jobs is None:
        return errors
    for (
        filename,
        job_name,
        step_name,
    ), expected_digest in _EXPECTED_CREDENTIAL_STEP_DIGESTS.items():
        if filename != path.name:
            continue
        job = _mapping(jobs.get(job_name))
        steps = _sequence(job.get("steps")) if job is not None else None
        if steps is None:
            continue
        matches = [
            step
            for raw_step in steps
            if (step := _mapping(raw_step)) is not None and step.get("name") == step_name
        ]
        if len(matches) == 1 and _step_digest(matches[0]) != expected_digest:
            errors.append(
                f"{path.name}:{job_name}/{step_name}: credential-bearing step differs "
                "from its reviewed command contract"
            )
    return errors


def _validated_output_errors(path: Path, workflow: Mapping[object, object]) -> list[str]:
    expected, errors = _expected_step_env_locations(
        path=path,
        workflow=workflow,
        expected_steps=_EXPECTED_VALIDATED_OUTPUT_ENV,
        boundary_name="validated output",
    )
    actual = dict(_iter_scalars(workflow))
    for location, value in expected.items():
        if actual.get(location) != value:
            errors.append(
                f"{path.name}:{_display(location)}: validated dispatch output must be {value}"
            )
    return errors


def _validation_contract_errors(path: Path, workflow: Mapping[object, object]) -> list[str]:
    errors: list[str] = []
    jobs = _mapping(workflow.get("jobs"))
    if jobs is None:
        return errors
    for (filename, job_name, step_name), expected_script in _EXPECTED_VALIDATION_SCRIPTS.items():
        if filename != path.name:
            continue
        job = _mapping(jobs.get(job_name))
        steps = _sequence(job.get("steps")) if job is not None else None
        if steps is None:
            continue
        matches = [
            (index, step)
            for index, raw_step in enumerate(steps)
            if (step := _mapping(raw_step)) is not None and step.get("name") == step_name
        ]
        if len(matches) != 1:
            continue
        validation_index, validation = matches[0]
        if validation.get("id") != "validate-input":
            errors.append(f"{path.name}:{job_name}/{step_name}: id must be validate-input")
        run = validation.get("run")
        if not isinstance(run, str) or _normalize_script(run) != _normalize_script(expected_script):
            errors.append(
                f"{path.name}:{job_name}/{step_name}: validation command must match "
                "the fail-closed contract"
            )
        for forbidden in ("continue-on-error", "if", "shell"):
            if forbidden in validation:
                errors.append(
                    f"{path.name}:{job_name}/{step_name}: validation step must not set {forbidden}"
                )

        checkout_indexes = [
            index
            for index, raw_step in enumerate(steps)
            if (step := _mapping(raw_step)) is not None
            and str(step.get("uses", "")).startswith("actions/checkout@")
        ]
        if len(checkout_indexes) != 1 or checkout_indexes[0] >= validation_index:
            errors.append(
                f"{path.name}:{job_name}/{step_name}: validation must follow one checkout step"
            )
        if path.name == "cloud-daily-batch.yml":
            smoke_indexes = [
                index
                for index, raw_step in enumerate(steps)
                if (step := _mapping(raw_step)) is not None and step.get("id") == "smoke"
            ]
            if len(smoke_indexes) != 1 or not (
                checkout_indexes and checkout_indexes[0] < smoke_indexes[0] < validation_index
            ):
                errors.append(
                    f"{path.name}:{job_name}/{step_name}: validation must follow notification smoke"
                )
    return errors


def _input_errors(path: Path, workflow: Mapping[object, object]) -> list[str]:
    allowed, errors = _expected_step_env_locations(
        path=path,
        workflow=workflow,
        expected_steps=_EXPECTED_INPUT_ENV,
        boundary_name="dispatch input",
    )
    seen: set[DocumentPath] = set()
    for location, value in _iter_scalars(workflow):
        if not _contains_dispatch_input(value):
            continue
        if location and location[-1] == "run":
            errors.append(f"{path.name}:{_display(location)}: inputs.* must pass through step env")
            continue
        expected = allowed.get(location)
        if expected is None or value != expected:
            errors.append(
                f"{path.name}:{_display(location)}: dispatch input is outside its validation "
                "step env"
            )
            continue
        seen.add(location)
    for location in sorted(set(allowed) - seen, key=str):
        errors.append(f"{path.name}:{_display(location)}: required dispatch input is missing")
    return errors


def check_workflow(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    try:
        # BaseLoader constructs only scalar/list/mapping nodes and preserves
        # workflow keys such as `on` without YAML 1.1 boolean coercion.
        loaded: object = yaml.load(text, Loader=yaml.BaseLoader)  # nosec B506
    except yaml.YAMLError as exc:
        return [f"{path.name}: invalid workflow YAML: {exc}"]
    workflow = _mapping(loaded)
    if workflow is None:
        return [f"{path.name}: workflow must be a mapping"]
    errors = _trusted_action_errors(path, text)
    errors.extend(_input_errors(path, workflow))
    errors.extend(_validated_output_errors(path, workflow))
    errors.extend(_validation_contract_errors(path, workflow))
    errors.extend(_credential_errors(path=path, workflow=workflow))
    return errors


def check(root: Path) -> list[str]:
    credential_targets = set(_EXPECTED_STEP_CREDENTIALS)
    digest_targets = set(_EXPECTED_CREDENTIAL_STEP_DIGESTS)
    if credential_targets != digest_targets:
        return [
            (
                "workflow trust gate configuration: credential targets and step digests differ: "
                f"credentials_only={sorted(credential_targets - digest_targets)}, "
                f"digests_only={sorted(digest_targets - credential_targets)}"
            )
        ]
    workflow_dir = root / ".github" / "workflows"
    paths = sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")))
    return [error for path in paths for error in check_workflow(path)]


def main() -> int:
    errors = check(Path.cwd())
    print("\n".join(errors), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
