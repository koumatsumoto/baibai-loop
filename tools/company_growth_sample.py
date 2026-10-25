"""固定releaseの有限サンプルをCIで読む。取得・本番更新・L3判断を行わない。"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from time import perf_counter

from pydantic import BaseModel, ConfigDict, Field

from baibai_engine.foundation.json import canonical_json
from baibai_engine.machine_analysis.company_growth.models import CompanyGrowth, InputRelease
from baibai_engine.machine_analysis.company_growth.query import company_growth


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    start: date
    end: date


class SampleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_release: InputRelease
    as_of: date
    samples: tuple[Sample, ...] = Field(min_length=1)


def measure_samples(
    path: Path, spec: SampleInput, *, mirror: Path = Path("stores")
) -> dict[str, object]:
    reports: list[dict[str, object]] = []
    for sample in spec.samples:
        started = perf_counter()
        result = company_growth(
            path,
            ticker=sample.ticker,
            as_of=spec.as_of,
            start=sample.start,
            end=sample.end,
            mirror=mirror,
        )
        elapsed = perf_counter() - started
        if result.input_release != spec.input_release:
            raise ValueError("sample market store does not match the requested fixed release")
        reports.append(_report(result, elapsed))
    return {
        "input_release": spec.input_release.model_dump(mode="json"),
        "as_of": spec.as_of.isoformat(),
        "samples": reports,
        "acceptance": "requires_original_value_and_l3_comparison_review",
    }


def _report(result: CompanyGrowth, elapsed: float) -> dict[str, object]:
    thin_table = [
        {
            "period_start": period.period_start.isoformat(),
            "period_end": period.period_end.isoformat(),
            "fiscal_period": period.fiscal_period,
            "reported": {
                metric: value.value
                for observation in period.observations
                if observation.measurement == "reported"
                for metric, value in observation.metrics.items()
            },
        }
        for period in result.periods
    ]
    payload = result.model_dump(mode="json")
    return {
        "ticker": result.ticker,
        "query_seconds": elapsed,
        "company_growth_json_bytes": len(canonical_json(payload).encode()),
        "thin_reported_table_json_bytes": len(canonical_json(thin_table).encode()),
        "company_growth": payload,
        "thin_reported_table": thin_table,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument("--mirror", type=Path, required=True)
    parser.add_argument("--sample-input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    spec = SampleInput.model_validate_json(args.sample_input.read_text(encoding="utf-8"))
    report = measure_samples(args.sqlite, spec, mirror=args.mirror)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
