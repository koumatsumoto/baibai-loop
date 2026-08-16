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
_R2_ACCEPTANCE = {
    "R2_ACCOUNT_ID": "${{ vars.R2_ACCOUNT_ID }}",
    "R2_ACCESS_KEY_ID": "${{ secrets.R2_LAKE_ACCEPTANCE_ACCESS_KEY_ID }}",
    "R2_SECRET_ACCESS_KEY": "${{ secrets.R2_LAKE_ACCEPTANCE_SECRET_ACCESS_KEY }}",  # nosec B105
}
_PROVIDERS = {
    "JQUANTS_API_KEY": "${{ secrets.JQUANTS_API_KEY }}",
    "ESTAT_APP_ID": "${{ secrets.ESTAT_APP_ID }}",
    "EDINET_API_KEY": "${{ secrets.EDINET_API_KEY }}",
}
_EXPECTED_STEP_CREDENTIALS: dict[tuple[str, str, str], dict[str, str]] = {
    ("cloud-daily-batch.yml", "daily", "Pull stores"): _R2,
    ("cloud-daily-batch.yml", "daily", "Hydrate market store from the L1 release"): _R2,
    ("cloud-daily-batch.yml", "daily", "Run daily batch"): _PROVIDERS,
    ("cloud-daily-batch.yml", "daily", "Publish the L1 release"): _R2,
    ("cloud-daily-batch.yml", "daily", "Upload machine stores and serving views"): _R2,
    ("cloud-daily-batch.yml", "daily", "Publish serving history and freshness"): _R2,
    ("cloud-daily-batch.yml", "daily", "Notify Discord #batch-runs"): {
        "DISCORD_WEBHOOK_URL": "${{ secrets.DISCORD_WEBHOOK_URL }}"
    },
    ("cloud-daily-batch.yml", "daily", "Upload run summary"): _R2,
    (
        "cloud-batch-watchdog.yml",
        "watchdog",
        "Alert Discord #batch-runs when the batch is missing",
    ): {"DISCORD_WEBHOOK_URL": "${{ secrets.DISCORD_WEBHOOK_URL }}"},
    ("cloud-history-backfill.yml", "backfill", "Pull the market store"): _R2,
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
    (
        "lake-acceptance.yml",
        "actual-r2",
        "Run actual R2 acceptance",
    ): _R2_ACCEPTANCE,
}
_EXPECTED_INPUT_ENV: dict[tuple[str, str, str], dict[str, str]] = {
    ("cloud-daily-batch.yml", "daily", "Validate dispatch input"): {
        "MANUAL_ASOF": "${{ inputs.asof }}"
    },
    ("cloud-batch-watchdog.yml", "watchdog", "Validate dispatch input"): {
        "WATCHDOG_CHECK_DATE": "${{ inputs.check_date }}"
    },
    ("cloud-history-backfill.yml", "backfill", "Validate dispatch inputs"): {
        "BACKFILL_START": "${{ inputs.start }}",
        "BACKFILL_END": "${{ inputs.end }}",
        "MASTER_MONTH_END_FROM": "${{ inputs.master_month_end_from }}",
    },
    ("lake-acceptance.yml", "actual-r2", "Validate exact head without credentials"): {
        "EXPECTED_DISPATCH_SHA": "${{ inputs.expected_sha }}"
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
        "cloud-batch-watchdog.yml",
        "watchdog",
        "Alert Discord #batch-runs when the batch is missing",
    ): {"WATCHDOG_CHECK_DATE": "${{ steps.validate-input.outputs.check_date }}"},
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
        python3 -m baibai_batch.validation.workflow_inputs daily --asof "$MANUAL_ASOF"
        echo "asof=$MANUAL_ASOF" >> "$GITHUB_OUTPUT"
    """,
    ("cloud-batch-watchdog.yml", "watchdog", "Validate dispatch input"): """
        python3 -m baibai_batch.validation.workflow_inputs watchdog \\
          --check-date "$WATCHDOG_CHECK_DATE"
        echo "check_date=$WATCHDOG_CHECK_DATE" >> "$GITHUB_OUTPUT"
    """,
    ("cloud-history-backfill.yml", "backfill", "Validate dispatch inputs"): """
        python3 -m baibai_batch.validation.workflow_inputs backfill \\
          --start "$BACKFILL_START" \\
          --end "$BACKFILL_END" \\
          --master-month-end-from "$MASTER_MONTH_END_FROM"
        echo "start=$BACKFILL_START" >> "$GITHUB_OUTPUT"
        echo "end=$BACKFILL_END" >> "$GITHUB_OUTPUT"
        echo "master_month_end_from=$MASTER_MONTH_END_FROM" >> "$GITHUB_OUTPUT"
    """,
    ("lake-acceptance.yml", "actual-r2", "Validate exact head without credentials"): """
        if [[ "$GITHUB_EVENT_NAME" == "workflow_dispatch" ]]; then
          EXPECTED_SHA="$EXPECTED_DISPATCH_SHA"
        elif [[ "$GITHUB_EVENT_NAME" == "pull_request" ]]; then
          EXPECTED_SHA="$EXPECTED_PR_SHA"
        else
          echo "lake acceptance does not support this event" >&2
          exit 2
        fi
        if [[ ! "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
          echo "expected_sha must be a full lowercase commit SHA" >&2
          exit 2
        fi
        if ! git cat-file -e "$EXPECTED_SHA^{commit}" 2>/dev/null; then
          echo "expected_sha is not a commit in this repository" >&2
          exit 2
        fi
        if [[ "$(git rev-parse HEAD)" != "$EXPECTED_SHA" ]]; then
          git checkout --detach "$EXPECTED_SHA"
        fi
        test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"
        echo "expected_sha=$EXPECTED_SHA" >> "$GITHUB_OUTPUT"
    """,
}
# Each digest covers the full reviewed step mapping (run script, env, condition,
# shell, working directory, and error policy), not merely a command substring.
_EXPECTED_CREDENTIAL_STEP_DIGESTS = {
    ("cloud-daily-batch.yml", "daily", "Pull stores"): (
        "3f98c56d975066951777b868cd4ded65038fa85700a5fcc3446043474fbfaa61"
    ),
    ("cloud-daily-batch.yml", "daily", "Hydrate market store from the L1 release"): (
        "27111a652d3d81932589921f36b4567618c70b61e6fb47f42360bd50b6036197"
    ),
    ("cloud-daily-batch.yml", "daily", "Run daily batch"): (
        "13b6a705644d75410e7f1b5bc188e38b5064a0eed5c3081c7d1fc9eeaffdb74d"
    ),
    ("cloud-daily-batch.yml", "daily", "Publish the L1 release"): (
        "2a015cf08ae041e6c97807b942bbc3c757c2d29bb130c06a84e8b2dd34992f5c"
    ),
    ("cloud-daily-batch.yml", "daily", "Upload machine stores and serving views"): (
        "f4edf907d92834cbb69639ba94cfe418711c5566b6abd334e00da778ec977ab6"
    ),
    ("cloud-daily-batch.yml", "daily", "Publish serving history and freshness"): (
        "8ebb0b45bd3a0515fbb06c8d55902316777195de8f433f2df8c0cd4ab9dc987b"
    ),
    ("cloud-daily-batch.yml", "daily", "Notify Discord #batch-runs"): (
        "810c7c768125b20db0179437fcd29ba13fd69f2ea775c8a609c35603be2ec427"
    ),
    ("cloud-daily-batch.yml", "daily", "Upload run summary"): (
        "3d7d91775c0ac950577b25c2792bed82e5af551485965a7c3fb6a31311064259"
    ),
    (
        "cloud-batch-watchdog.yml",
        "watchdog",
        "Alert Discord #batch-runs when the batch is missing",
    ): "6132f60f7e458e06639309446c7224b8e9f8f9c16e884df17b13583c3d65d49f",
    ("cloud-history-backfill.yml", "backfill", "Pull the market store"): (
        "adebb121bed282346210d5a71a832a530661cb8c14a696cedf78328580aa3a31"
    ),
    (
        "cloud-history-backfill.yml",
        "backfill",
        "Backfill and publish committed progress",
    ): "a493a37d0b69a1455cdb0be85fc14019d730493d55412c6847573b42c447a8f6",
    ("cloud-materialize.yml", "materialize", "Pull stores"): (
        "9e9bf135564f525b25aaa86886d154ab52a4b714b4f69b43b9bc33bd947a98e7"
    ),
    ("cloud-materialize.yml", "materialize", "Upload serving objects"): (
        "d5b4145027538c195bddaebd389036be68f4c17eacc8d754f975c1f1a9d5cd51"
    ),
    ("web.yml", "quality", "Deploy Worker and UI assets"): (
        "885b888768015c2ca9aaa48d92aa47c941566db9a28c0909209499bae6418763"
    ),
    (
        "lake-acceptance.yml",
        "actual-r2",
        "Run actual R2 acceptance",
    ): "203fcedaa220abab7cb0f7b0ebbe46c89e01f3e81b737a421e1de24c29f31eb3",
}
_RESTRICTED_ENV_NAMES = frozenset(
    name for credentials in _EXPECTED_STEP_CREDENTIALS.values() for name in credentials
)

_LAKE_ACCEPTANCE_JOB_CONDITION = """\
${{
  github.event_name == 'workflow_dispatch' ||
  (github.event_name == 'pull_request' &&
   github.event.action == 'labeled' &&
   github.event.label.name == 'lake-acceptance-approved' &&
   github.actor == github.repository_owner &&
   github.event.pull_request.head.repo.full_name == github.repository)
}}"""
_LAKE_ACCEPTANCE_WORKFLOW_DIGEST = (
    "4aa1d2556e29006e0195f7840b301344404baf20e2c596d63ef4d2940d6b416e"
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


def _mapping_digest(mapping: Mapping[object, object]) -> str:
    payload = json.dumps(
        _json_value(mapping),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _step_digest(step: Mapping[object, object]) -> str:
    return _mapping_digest(step)


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


def _lake_acceptance_errors(path: Path, workflow: Mapping[object, object]) -> list[str]:
    """Pin the pre-merge label gate that protects acceptance credentials."""
    if path.name != "lake-acceptance.yml":
        return []

    errors: list[str] = []
    if _mapping_digest(workflow) != _LAKE_ACCEPTANCE_WORKFLOW_DIGEST:
        errors.append(f"{path.name}: workflow differs from its reviewed execution context contract")
    expected_trigger = {
        "pull_request": {"types": ["labeled"]},
        "workflow_dispatch": {
            "inputs": {
                "expected_sha": {
                    "description": "Exact 40-character commit SHA under acceptance",
                    "required": "true",
                    "type": "string",
                }
            }
        },
    }
    if _json_value(workflow.get("on")) != expected_trigger:
        errors.append(f"{path.name}: trigger must be owner-approved PR label or exact-SHA dispatch")
    if _json_value(workflow.get("permissions")) != {"contents": "read"}:
        errors.append(f"{path.name}: workflow permissions must stay contents: read")
    if _json_value(workflow.get("concurrency")) != {
        "group": "lake-acceptance",
        "cancel-in-progress": "false",
    }:
        errors.append(f"{path.name}: acceptance runs must stay serialized and non-cancelling")

    jobs = _mapping(workflow.get("jobs"))
    if jobs is None or {str(name) for name in jobs} != {"actual-r2"}:
        errors.append(f"{path.name}: actual-r2 must be the only job")
        return errors
    job = _mapping(jobs.get("actual-r2"))
    if job is None:
        return [*errors, f"{path.name}: actual-r2 job must be a mapping"]
    if job.get("if") != _LAKE_ACCEPTANCE_JOB_CONDITION:
        errors.append(
            f"{path.name}: credential job must require owner label and same-repository head"
        )

    steps = _sequence(job.get("steps"))
    if steps is None:
        return [*errors, f"{path.name}: actual-r2 steps must be a sequence"]
    mapped_steps = [step for raw in steps if (step := _mapping(raw)) is not None]
    checkout = [
        step for step in mapped_steps if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    if len(checkout) != 1 or _json_value(checkout[0].get("with")) != {
        "fetch-depth": "0",
        "persist-credentials": "false",
    }:
        errors.append(
            f"{path.name}: checkout must fetch every ref of this repository without persisting "
            "credentials"
        )
    validation = [
        step
        for step in mapped_steps
        if step.get("name") == "Validate exact head without credentials"
    ]
    if len(validation) == 1 and _json_value(validation[0].get("env")) != {
        "EXPECTED_DISPATCH_SHA": "${{ inputs.expected_sha }}",
        "EXPECTED_PR_SHA": "${{ github.event.pull_request.head.sha }}",
    }:
        errors.append(f"{path.name}: exact head sources must stay event-bound")
    credential_indexes = [
        index
        for index, step in enumerate(mapped_steps)
        if step.get("name") == "Run actual R2 acceptance"
    ]
    if credential_indexes != [len(mapped_steps) - 1]:
        errors.append(f"{path.name}: credential-bearing acceptance must be the final step")
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
    errors.extend(_lake_acceptance_errors(path, workflow))
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
