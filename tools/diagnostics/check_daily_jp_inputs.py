"""Check one explicit stock's L3 inputs using this daily runner; never publish a proposal."""

from __future__ import annotations

import json
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime, time
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from baibai_engine.cli.app import main as engine_cli
from baibai_engine.portfolio.holdings.market_observations import read_unadjusted_close
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.primitives.time import JST
from baibai_engine.storage.paths import MARKET_DB_PATH, RUNS_DB_PATH
from baibai_jobs.publication.machine_origin import origin_for_push


def main(*, market: Path = MARKET_DB_PATH, runs: Path = RUNS_DB_PATH) -> int:
    stage = "daily inputs"
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
        ticker = normalize_ticker(event["inputs"]["jp_input_probe_ticker"])
        notice = Path(os.environ["RUNNER_TEMP"]) / "batch-notice.json"
        origin = origin_for_push(notice, market)  # Local-only: same run/attempt and release.
        fixed = json.loads(notice.read_bytes())
        day = date.fromisoformat(fixed["asof"])
        now = datetime.now(UTC)
        stage = "selection-context"
        output = StringIO()
        with (
            TemporaryDirectory() as directory,
            redirect_stdout(output),
            redirect_stderr(StringIO()),
        ):
            # The CLI otherwise defaults to the real App, including saved company judgments.
            code = engine_cli(
                [
                    "investment-judgment",
                    "selection-context",
                    "--scope",
                    "specified_stocks",
                    "--request",
                    "指定した公開テスト銘柄の調査入力確認",
                    "--ticker",
                    ticker,
                    "--generation-id",
                    fixed["generation_id"],
                    "--screening-selection-id",
                    fixed["selection_id"],
                    "--research-asof",
                    day.isoformat(),
                    "--known-at",
                    now.isoformat(),
                    "--runs-path",
                    str(runs),
                    "--db",
                    str(Path(directory) / "no-app.sqlite"),
                ]
            )
        if code != 0:
            raise ValueError
        context = json.loads(output.getvalue())
        rows = context["discovery"]["rows"]
        model_source = context["model_inputs"]["analysis_source"]["inputs"]["market"]
        if not (
            model_source["kind"] == "lake_release"
            and model_source["reference"] == origin["release_id"]
            and model_source["sha256"] == origin["release_manifest_sha256"]
            and context["status"] == "ready"
            and context["generation_id"] == fixed["generation_id"]
            and context["screening_selection_id"] == fixed["selection_id"]
            and context["generation_as_of"] == day.isoformat()
            and len(rows) == 1
            and rows[0]["ticker"] == ticker
            and rows[0]["models_recorded"]
            and context["model_inputs"]["securities"][ticker]["models"]
        ):
            raise ValueError
        stage = "unadjusted close"
        cutoff = min(now, datetime.combine(day, time.max, tzinfo=JST))
        quote = read_unadjusted_close(sqlite_path=market, ticker=ticker, at=cutoff)
        if quote is None or quote.price_as_of.isoformat() != rows[0]["expected_price_date"]:
            raise ValueError
    except (Exception, SystemExit):
        # Validation errors may embed a whole model or a user-supplied argument.
        print(f"FAIL: runner-local JP proposal input probe ({stage}); no proposal created.")
        return 1
    print(
        "PASS: runner-local JP proposal input probe (one fixed selection and unadjusted close); "
        "no proposal created; remote publication not checked."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
