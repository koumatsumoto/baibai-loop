from __future__ import annotations

import yaml

from baibai_loop.validate.calendar import validate_calendar_file


def test_calendar_snapshot_requires_core_metadata(tmp_path) -> None:
    calendar = tmp_path / "records/_calendars/corporate-actions/2026-05.yaml"
    calendar.parent.mkdir(parents=True)
    calendar.write_text(
        yaml.safe_dump(
            {
                "covered_from": "2026-05-01",
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

    assert "calendar.required" in codes
