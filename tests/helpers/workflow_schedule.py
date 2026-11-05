"""Keep cron contracts while an explicitly marked cutover pauses GitHub triggers."""

import yaml

PAUSE = "  # #1820 本番切替中は定期起動を停止。再開時は下の元scheduleを復元する。"


def declared_schedule(raw: str) -> list[dict[str, str]]:
    workflow = yaml.load(raw, Loader=yaml.BaseLoader)
    lines = raw.splitlines()
    if PAUSE not in lines:
        # An unmarked missing schedule is still a contract failure.
        return workflow["on"]["schedule"]
    assert "schedule" not in workflow["on"], "marked cutover must not enable cron"
    assert "workflow_dispatch" in workflow["on"], "explicit recovery entry must remain"
    start = lines.index("  # schedule:")
    restored = []
    for line in lines[start:]:
        if not line.startswith("  # "):
            break
        restored.append("  " + line[len("  # ") :])
    planned = yaml.load("on:\n" + "\n".join(restored), Loader=yaml.BaseLoader)
    assert set(planned["on"]) == {"schedule"}
    assert planned["on"]["schedule"], "paused workflow must keep its exact original slots"
    return planned["on"]["schedule"]
