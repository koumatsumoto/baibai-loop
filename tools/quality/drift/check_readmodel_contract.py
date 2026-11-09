"""Check the read-model contract through its exporter without writing artifacts."""

from pathlib import Path

from baibai_web.contracts_export import main as export_main

_ROOT = Path(__file__).resolve().parents[3]


def main() -> int:
    return int(export_main(["--check", "--repo-root", str(_ROOT)]) != 0)


if __name__ == "__main__":
    raise SystemExit(main())
