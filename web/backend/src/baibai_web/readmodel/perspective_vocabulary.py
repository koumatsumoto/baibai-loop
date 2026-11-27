"""JPとUSの表示だけで使う観点の語彙。判断の生成・検証は所有しない。"""

from typing import Literal

PerspectiveModel = Literal[
    "company_financials",
    "company_growth",
    "market_context",
    "external_assessment",
    "technical_analysis",
    "industry_trends",
    "social_trends",
]
PerspectiveAssessment = Literal[
    "supportive", "mixed", "adverse", "neutral", "insufficient", "not_applicable"
]
