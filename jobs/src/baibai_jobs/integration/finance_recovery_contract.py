"""Finite, reviewed inputs for the one-off schema52 forward recovery."""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from baibai_jobs.integration.jpx_recovery_contract import (
    Digest,
    Fingerprint,
    ObjectProof,
    ReleaseID,
    _Strict,
    unique_json,
)
from baibai_jobs.integration.jpx_recovery_verification import MARKET_DATASETS

PREFIX = "coordination/finance-recovery/"
# Reviewed finite input identity; code trust is separately bound to main/GITHUB_SHA.
APPROVED_CONTRACTS: frozenset[str] = frozenset(
    {
        "c207fefe7bb83e775257acbb3310334bbeaf8e3f363982a05702fc31433b3871",
    }
)
REQUIRED_FILES = frozenset({"market.sqlite", "runs.sqlite", "economic.sqlite", "conditions.json"})
INTEGRATION_FILES = frozenset(
    {"integration/plan.json", "integration/applied.json"}
    | {
        f"integration/receipt-{i:05d}/{name}"
        for i in range(17)
        for name in ("receipts.sqlite", "result.json")
    }
)


class InputFile(_Strict):
    sha256: Digest
    size: Annotated[int, Field(gt=0, le=5 * 1024**3)]
    encoding: Literal["raw", "zstd"]
    decoded_sha256: Digest
    decoded_size: Annotated[int, Field(gt=0, le=8 * 1024**3)]

    @model_validator(mode="after")
    def raw_identity(self) -> Self:
        if self.encoding == "raw" and (self.sha256, self.size) != (
            self.decoded_sha256,
            self.decoded_size,
        ):
            raise ValueError("raw input identities differ")
        return self

    @property
    def key(self) -> str:
        return PREFIX + "inputs/" + self.sha256


class Baseline(_Strict):
    current: ObjectProof
    machine_receipt: ObjectProof
    market_etag: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    runs_etag: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    lake_release_id: ReleaseID
    lake_manifest_sha256: Digest


class FinanceContract(_Strict):
    version: Literal[1]
    asof: Literal["2026-11-16"]
    market_schema: Literal[52]
    runs_schema: Literal[8]
    baseline: Baseline
    files: dict[str, InputFile]
    market_tables: dict[str, Fingerprint]
    old_metrics: Fingerprint
    target_metrics: Fingerprint
    legacy_runs: dict[str, Fingerprint]
    prior_generations: dict[str, Any]
    edinet_input: dict[str, Any]

    @model_validator(mode="after")
    def finite_scope(self) -> Self:
        if set(self.files) != REQUIRED_FILES | INTEGRATION_FILES:
            raise ValueError("exact finance recovery input closure required")
        if sum(item.decoded_size for item in self.files.values()) > 12 * 1024**3:
            raise ValueError("finance input closure too large")
        if set(self.market_tables) != set(MARKET_DATASETS):
            raise ValueError("complete fixed market table fingerprints required")
        if (self.old_metrics.count, self.target_metrics.count) != (111213, 3974):
            raise ValueError("accepted financial coverage differs")
        if set(self.legacy_runs) != {"screening_run", "stock_analysis"}:
            raise ValueError("both historical runs tables required")
        prior = self.prior_generations
        if set(prior) != {"generation_ids", "generations"}:
            raise ValueError("invalid prior generation proof")
        ids = prior["generation_ids"]
        if not isinstance(ids, list) or len(ids) != 13 or len(set(ids)) != 13:
            raise ValueError("exactly13 distinct prior generations required")
        if set(prior["generations"]) != set(ids):
            raise ValueError("prior generation identities differ")
        for tables in prior["generations"].values():
            if set(tables) != {"model_generation", "generated_model", "screening_selection"}:
                raise ValueError("incomplete prior generation tables")
            for proof in tables.values():
                Fingerprint.model_validate(proof)
        from baibai_engine.machine_analysis.generation.contracts import InputSnapshot

        fixed = InputSnapshot.model_validate(self.edinet_input)
        if fixed.kind != "native_l1" or fixed.reference != "edinet-" + fixed.sha256:
            raise ValueError("fixed native EDINET input required")
        return self


def decode_contract(payload: bytes, digest: str) -> FinanceContract:
    if digest not in APPROVED_CONTRACTS:
        raise ValueError("contract is not in the reviewed launch allowlist")
    if not 0 < len(payload) <= 2_000_000 or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("finance contract digest or size differs")
    return FinanceContract.model_validate(unique_json(payload))
