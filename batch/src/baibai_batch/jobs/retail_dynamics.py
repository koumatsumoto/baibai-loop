"""Manual bounded acquisition/stage import; no scheduler or publication lease."""

from __future__ import annotations

from baibai_engine.batch_api import retail_dynamics_main


def main(argv: list[str] | None = None) -> int:
    return retail_dynamics_main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
