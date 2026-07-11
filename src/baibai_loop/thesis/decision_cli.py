"""Read-only CLI for deterministic decision-packet evaluation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from .decision_packet import (
    DecisionPacketError,
    evaluate_decision_packet,
    load_decision_packet,
    load_independent_review,
    result_to_payload,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baibai-loop-decision")
    parser.add_argument("packet", type=Path)
    args = parser.parse_args(argv)
    try:
        document = load_decision_packet(args.packet)
        review_path = _review_path(args.packet, document.independent_review_ref)
        review = load_independent_review(review_path) if review_path is not None else None
        result = evaluate_decision_packet(document, review=review)
    except DecisionPacketError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    yaml.safe_dump(
        result_to_payload(result),
        sys.stdout,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return 0 if result.decision_readiness == "ready" else 2


def _review_path(packet_path: Path, review_ref: str | None) -> Path | None:
    if review_ref is None:
        return None
    root = packet_path.resolve().parent
    resolved = (root / review_ref).resolve()
    if not resolved.is_relative_to(root):
        raise DecisionPacketError("independent_review_ref must stay beside the packet")
    return resolved


if __name__ == "__main__":
    raise SystemExit(main())
