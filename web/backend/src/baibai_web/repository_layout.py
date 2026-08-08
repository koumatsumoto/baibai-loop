"""Repository-relative presentation inputs owned by the Web subsystem."""

from pathlib import Path

MACRO_PANEL_CONFIG_PATH = Path("web/config/macro-panel.yaml")
ER_LEVEL_CALIBRATION_CONTEXT_PATH = Path("reports/published/er-level-calibration-latest.yaml")

__all__ = ["ER_LEVEL_CALIBRATION_CONTEXT_PATH", "MACRO_PANEL_CONFIG_PATH"]
