"""Give Research Triage one short meaning contract for manual and scheduled use."""

TRIAGE_POLICY: tuple[str, ...] = (
    "Review Setのmembership、順序、Nomination、machine snapshotを変更しない",
    "researchは具体的なrationale、research_question、key_riskを返す",
    "skipは具体的なrationaleだけを返す",
    "unknownを否定事実へ変換せず、unknownだけでskipを強制しない",
    "E[r]とMacro Contextは参考文脈であり、単独gateにしない",
    "買付判断とbroker操作を行わない",
)

__all__ = ["TRIAGE_POLICY"]
