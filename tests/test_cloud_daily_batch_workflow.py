from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_engine.screening.config import ScreeningConfig
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules

WORKFLOW_PATH = ROOT / ".github" / "workflows" / "cloud-daily-batch.yml"


def _daily_job_env() -> dict[str, str]:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    return {str(name): str(value) for name, value in workflow["jobs"]["daily"]["env"].items()}


class CloudDailyBatchWorkflowTests(unittest.TestCase):
    def test_daily_job_env_covers_every_required_jpx_source(self) -> None:
        # scheduled run が当日 cache 不足で bootstrap-cache に入ると、JPX 規制 provider は
        # active rules の universe.required_jpx_flags に含まれる全 source を要求する。
        # workflow job env がその実ゲートを ScreeningConfig 経由で満たせることを機械的に
        # 検査する (外部 HTTP へは接続せず、source 名の網羅だけを確認する。URL 値の到達性は
        # マージ後の実接続再検証が担う)。
        env = _daily_job_env()
        config = ScreeningConfig.from_env(env)
        rules = load_screening_rules(ROOT / DEFAULT_RULES_PATH)

        required_sources = set(rules.universe.required_jpx_flags)
        self.assertTrue(required_sources)
        missing = required_sources - set(config.jpx_regulation_urls)
        self.assertEqual(missing, set())


if __name__ == "__main__":
    unittest.main()
