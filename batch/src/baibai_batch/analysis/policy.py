"""Give Research Triage one short meaning contract for manual and scheduled use."""

TRIAGE_POLICY: tuple[str, ...] = (
    "Review Setのmembership、順序、Nomination、machine snapshotを変更しない",
    (
        "researchは追加のFundamental Researchで確認すべき具体的な未解決問いがあり、"
        "その答えが候補の価値、収益持続性、主要riskの理解をmaterially改善しうる場合に選ぶ"
    ),
    "skipは追加調査で識別すべき具体的な問いを現在のmachine factsから特定できない場合に選ぶ",
    "researchは具体的なrationale、research_question、key_riskを返し、skipは具体的なrationaleだけを返す",
    "unknownを否定事実へ変換せず、unknownだけでskipを強制しない",
    "E[r]とMacro Contextは参考文脈であり、単独gateにしない",
    "買付判断とbroker操作を行わない",
)

__all__ = ["TRIAGE_POLICY"]
