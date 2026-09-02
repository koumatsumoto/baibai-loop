"""Measure analysis-ops verification without inventing a second quality standard."""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class Gate:
    name: str
    argv: tuple[str, ...]
    cwd: Path = ROOT
    environment: tuple[tuple[str, str], ...] = ()


def _fast() -> list[Gate]:
    return [
        Gate(
            "analysis-format",
            (
                "uv",
                "run",
                "ruff",
                "format",
                "--check",
                "batch/src/baibai_batch/analysis",
                "tests/batch/test_analysis_ops.py",
            ),
        ),
        Gate(
            "analysis-lint",
            (
                "uv",
                "run",
                "ruff",
                "check",
                "batch/src/baibai_batch/analysis",
                "tests/batch/test_analysis_ops.py",
            ),
        ),
        Gate(
            "analysis-types",
            (
                "uv",
                "run",
                "mypy",
                "batch/src/baibai_batch/analysis",
                "batch/src/baibai_batch/jobs/daily.py",
            ),
        ),
        Gate(
            "analysis-tests",
            (
                "uv",
                "run",
                "pytest",
                "-n",
                "0",
                "-q",
                "tests/batch/test_analysis_ops.py",
                "tests/batch/test_cloud_daily_batch.py",
                "tests/batch/test_cli.py",
                "tests/engine/test_cli_seam_screening_analysis.py",
                "tests/engine/test_operation_session.py",
            ),
            environment=(("TMPDIR", "/dev/shm"),),  # nosec B108 - repository gate contract
        ),
    ]


def _full(requirements: Path, worker_bundle: Path) -> list[Gate]:
    gates = [
        Gate("uv-sync", ("uv", "sync", "--frozen", "--all-groups")),
        Gate("format", ("uv", "run", "ruff", "format", "--check", ".")),
        Gate("lint", ("uv", "run", "ruff", "check", ".")),
        Gate("mypy", ("uv", "run", "mypy")),
        Gate("imports", ("uv", "run", "lint-imports")),
    ]
    for path in sorted((ROOT / "tools/quality/drift").glob("check_*.py")):
        gates.append(
            Gate(
                f"drift-{path.stem}",
                ("uv", "run", "python", "-m", f"tools.quality.drift.{path.stem}"),
            )
        )
    gates.extend(
        [
            Gate(
                "pytest",
                ("uv", "run", "pytest", "-n", "4", "--cov", "--cov-report=term-missing"),
                environment=(("TMPDIR", "/dev/shm"),),  # nosec B108 - §9 exact command
            ),
            Gate(
                "bandit",
                (
                    "uv",
                    "run",
                    "bandit",
                    "-c",
                    "pyproject.toml",
                    "-q",
                    "-r",
                    "engine/src/baibai_engine",
                    "web/backend/src/baibai_web",
                    "batch/src/baibai_batch",
                    "tools",
                ),
            ),
            Gate(
                "dependency-export",
                (
                    "uv",
                    "export",
                    "--format",
                    "requirements.txt",
                    "--locked",
                    "--all-groups",
                    "--no-emit-project",
                    "--no-hashes",
                    "--output-file",
                    str(requirements),
                ),
            ),
            Gate("pip-audit", ("uv", "run", "pip-audit", "-r", str(requirements))),
            Gate(
                "frontend-audit",
                ("npm", "audit", "--package-lock-only", "--audit-level=high"),
                cwd=ROOT / "web/frontend",
            ),
            Gate("frontend-install", ("npm", "ci"), cwd=ROOT / "web/frontend"),
            Gate("frontend-lint", ("npm", "run", "lint"), cwd=ROOT / "web/frontend"),
            Gate("frontend-build", ("npm", "run", "build"), cwd=ROOT / "web/frontend"),
            Gate("frontend-test", ("npm", "test"), cwd=ROOT / "web/frontend"),
            Gate(
                "edge-audit",
                ("npm", "audit", "--package-lock-only", "--audit-level=high"),
                cwd=ROOT / "web/edge",
            ),
            Gate("edge-install", ("npm", "ci"), cwd=ROOT / "web/edge"),
            Gate("edge-types-generate", ("npm", "run", "types:check"), cwd=ROOT / "web/edge"),
            Gate("edge-types", ("npm", "run", "typecheck"), cwd=ROOT / "web/edge"),
            Gate("edge-test", ("npm", "test"), cwd=ROOT / "web/edge"),
            Gate(
                "edge-bundle",
                ("npx", "wrangler", "deploy", "--dry-run", "--outdir", str(worker_bundle)),
                cwd=ROOT / "web/edge",
            ),
        ]
    )
    return gates


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("fast", "full"), required=True)
    parser.add_argument("--summary-json", type=Path)
    args = parser.parse_args()
    temp = Path(tempfile.mkdtemp(prefix="baibai-analysis-verify-"))
    log_dir = temp / "logs"
    log_dir.mkdir(mode=0o700)
    gates = (
        _fast()
        if args.profile == "fast"
        else _full(temp / "requirements.txt", temp / "worker-bundle")
    )
    results: list[dict[str, object]] = []
    failed = False
    for number, gate in enumerate(gates, start=1):
        started = time.monotonic()
        environment = os.environ.copy()
        environment.update(dict(gate.environment))
        completed = subprocess.run(  # nosec B603
            gate.argv,
            cwd=gate.cwd,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        duration = time.monotonic() - started
        log_path = log_dir / f"{number:02d}-{gate.name}.log"
        log_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
        log_path.chmod(0o600)
        result = {
            "name": gate.name,
            "exit_code": completed.returncode,
            "duration_seconds": round(duration, 6),
            "log_path": str(log_path),
        }
        results.append(result)
        print(
            f"gate={gate.name} exit={completed.returncode} duration={duration:.1f}s log={log_path}"
        )
        if completed.returncode != 0:
            failed = True
            break
    summary = {
        "schema_version": 1,
        "profile": args.profile,
        "gates": results,
        "passed": not failed,
    }
    rendered = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    if args.summary_json is not None:
        args.summary_json.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
