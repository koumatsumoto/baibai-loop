"""Check that the exact App snapshot to publish contains the requested decision."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from baibai_engine.investment_judgment.trade_proposal.query import load_investment_proposal
from baibai_engine.investment_judgment.us_proposal import load_us_proposal
from baibai_engine.storage.sqlite.application.read import connect_read_only


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("proposal_id")
    args = parser.parse_args()
    try:
        with closing(connect_read_only(args.snapshot)) as db:
            if load_us_proposal(db, args.proposal_id) is None:
                load_investment_proposal(db, args.proposal_id)
    except FileNotFoundError:
        print("requested proposal was not found in the App snapshot", file=sys.stderr)
        return 1
    except (sqlite3.Error, ValueError, RuntimeError):
        print("requested proposal could not be verified in the App snapshot", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
