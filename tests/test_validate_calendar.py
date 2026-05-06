from __future__ import annotations

from pathlib import Path

import yaml

from baibai_loop.validate.calendar import validate_calendar_file


def test_corporate_action_calendar_invalidates_only_catalog_allowed_metrics(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / "records/_config/metric-catalog/2026-05-01T000000+0900.yaml"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        yaml.safe_dump(
            {
                "metrics": [
                    {
                        "metric_id": "p_s",
                        "event_invalidation_rules": [
                            {"corporate_action_kind": "merger"}
                        ],
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    calendar = tmp_path / "records/_calendars/corporate-actions/2026-05.yaml"
    calendar.parent.mkdir(parents=True)
    calendar.write_text(
        yaml.safe_dump(
            {
                "covered_from": "2026-05-01",
                "covered_until": "2026-05-31",
                "last_refreshed_at": "2026-05-05T00:00:00+09:00",
                "source_status": "ok",
                "events": [
                    {
                        "ticker": "2767",
                        "corporate_action_kind": "split",
                        "invalidates_metrics": ["p_s"],
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    codes = {finding.code for finding in validate_calendar_file(calendar)}

    assert "calendar.corporate-action-catalog-mismatch" in codes
