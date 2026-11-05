"""Read active GitHub trigger schedules, never commented cutover plans."""

import yaml


def declared_schedule(raw: str) -> list[dict[str, str]]:
    return yaml.load(raw, Loader=yaml.BaseLoader)["on"]["schedule"]
