"""Fail CI when the checked-in read-model contract no longer matches the models.

The UI's types are generated from `baibai_web.readmodel.models` (see
`baibai_web.contracts_export`). Without this gate a backend field rename would land
green — the export writes the new payload the next batch while the UI keeps reading a
key that is gone — because nothing in the Python tree compiles the TypeScript.

Regenerating here rather than shelling out keeps the failure specific: it names which
file is stale and the one command that fixes it.
"""

from __future__ import annotations

from pathlib import Path

from baibai_web.contracts_export import ContractError, generated_files

_ROOT = Path(__file__).resolve().parents[3]


def check() -> tuple[str, ...]:
    try:
        files = generated_files(_ROOT)
    except ContractError as exc:
        return (f"the read models describe a shape the generator cannot render: {exc}",)
    failures = []
    for path, expected in files.items():
        try:
            actual: str | None = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            actual = None
        if actual != expected:
            failures.append(f"{path.relative_to(_ROOT)} is stale")
    if failures:
        failures.append(
            "run `uv run python -m baibai_web.contracts_export` and commit the result; "
            "then check the UI still compiles against the regenerated types"
        )
    return tuple(failures)


def main() -> int:
    failures = check()
    if failures:
        for failure in failures:
            print(f"error: {failure}")
        return 1
    print("ok: the read-model contract and the UI types match the read models")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
