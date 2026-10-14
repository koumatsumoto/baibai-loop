"""保存済みL2候補を読取用JSONへ投影する。L3選定は含めない。"""

from typing import Any

from baibai_engine.screening.discovery.contracts import PublishedReviewSet
from baibai_engine.screening.earnings_lag import interpreted_next_earnings_status


def review_set_candidates(review_set: PublishedReviewSet) -> tuple[dict[str, Any], ...]:
    result = []
    for entry in review_set.entries:
        snapshot = entry.model_dump(mode="json")
        ticker = snapshot.pop("ticker")
        context = snapshot["analysis"]["context"]
        context["next_earnings_status"] = interpreted_next_earnings_status(
            context["next_earnings_status"]
        )
        result.append({"ticker": ticker, "snapshot": snapshot})
    return tuple(result)
