"""Basic front-matter field checks: ticker, playbook, decision."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from baibai_loop.validate.errors import ValidationFinding

_TICKER_PATTERN = re.compile(r"^[0-9A-Z]{4}$")
_KNOWN_OUTCOMES = {"approved", "deferred", "rejected"}
_KNOWN_POSTURES = {"act_now", "wait_for_event", "wait_for_capital", "dropped"}


def _check_ticker(path: Path, front_matter: Mapping[str, object]) -> list[ValidationFinding]:
    ticker = front_matter.get("ticker")
    if not isinstance(ticker, str) or not _TICKER_PATTERN.match(ticker):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.ticker-format",
                message=f"ticker must be 4 alphanumeric uppercase chars (got {ticker!r})",
                location="ticker",
            )
        ]
    return []


def _check_playbook(
    path: Path,
    front_matter: Mapping[str, object],
    known_playbooks: frozenset[str],
) -> list[ValidationFinding]:
    playbook_id = front_matter.get("playbook_id")
    if not isinstance(playbook_id, str) or playbook_id not in known_playbooks:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.unknown-playbook",
                message=f"playbook_id must reference a known playbook family (got {playbook_id!r})",
                location="playbook_id",
            )
        ]
    return []


def _check_decision(path: Path, front_matter: Mapping[str, object]) -> list[ValidationFinding]:
    decision = front_matter.get("research_decision")
    if not isinstance(decision, Mapping):
        return []
    findings: list[ValidationFinding] = []
    outcome = decision.get("outcome")
    posture = decision.get("posture")
    if outcome not in _KNOWN_OUTCOMES:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.unknown-outcome",
                message=f"research_decision.outcome must be one of {_KNOWN_OUTCOMES}",
                location="research_decision.outcome",
            )
        )
    if posture not in _KNOWN_POSTURES:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.unknown-posture",
                message=f"research_decision.posture must be one of {_KNOWN_POSTURES}",
                location="research_decision.posture",
            )
        )
    if outcome == "approved" and posture != "act_now":
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.approved-posture",
                message="approved research decisions must use posture: act_now",
                location="research_decision.posture",
            )
        )
    if outcome == "rejected" and "rejection_reason" not in decision:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.rejection-reason-required",
                message="rejected research decisions require rejection_reason",
                location="research_decision.rejection_reason",
            )
        )
    if outcome == "deferred" and posture not in {"wait_for_event", "wait_for_capital"}:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.deferred-posture",
                message="deferred research decisions must wait for event or capital",
                location="research_decision.posture",
            )
        )
    if outcome == "deferred" and "deferral_reason" not in decision:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.deferral-reason-required",
                message="deferred research decisions require deferral_reason",
                location="research_decision.deferral_reason",
            )
        )
    if outcome == "rejected" and posture != "dropped":
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.rejected-posture",
                message="rejected research decisions must use posture: dropped",
                location="research_decision.posture",
            )
        )
    return findings
