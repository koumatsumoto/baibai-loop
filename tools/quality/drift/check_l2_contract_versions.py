"""A published L2 schema and its contract version must move together.

The contract version is in the object key, so it is the one thing an external reader can
use to decide whether it understands a file. Changing a row type without moving the
version leaves two different column sets under the same `contract=v1` prefix: this
repository's reader still refuses the mismatch because it compares the full Arrow schema,
but anything that trusts the version reads the wrong shape.
"""

from __future__ import annotations

import sys

from baibai_engine.screening.calibration.lake import verify_l2_schema_signatures


def main() -> int:
    drifted = verify_l2_schema_signatures()
    for message in drifted:
        print(message, file=sys.stderr)
    if drifted:
        print(
            "bump the dataset's contract_version and record the new signature in "
            "_RECORDED_SCHEMA_SIGNATURES",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
