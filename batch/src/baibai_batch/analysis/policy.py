"""Give Research Triage one short meaning contract for manual and scheduled use."""

TRIAGE_POLICY: tuple[str, ...] = (
    (
        "4 ApproachのNominationをprimary authorityとし、Review Setのmembership、"
        "Nomination、machine snapshotを変更しない"
    ),
    (
        "researchは追加のFundamental Researchで確認すべき具体的な未解決問いがあり、"
        "その答えが候補の価値、収益持続性、主要riskの理解をmaterially改善しうる場合に選ぶ"
    ),
    (
        "skipはmachine factsがApproach仮説を明確に崩すか、"
        "追加調査が採否をmaterialに変えない場合だけ選ぶ"
    ),
    "他候補より相対的に弱いだけならskipにせず、researchの低いpriorityで表す",
    (
        "researchは全候補比較による1..Nのpriority、具体的なrationale、"
        "research_question、key_riskを返し、skipはpriorityなしで具体的なrationaleだけを返す"
    ),
    "unknownを否定事実へ変換せず、unknownだけでskipを強制しない",
    (
        "E[r]はvaluation reversionとcarryのsecondary machine return priorであり、"
        "高低、負値、欠損だけでresearch、skip、priorityを決めない"
    ),
    "Approach仮説とE[r]の一致は補強材料、不一致はResearchで解く問いとして扱える",
    "Macro Contextは参考文脈であり、単独gateにしない",
    "買付判断とbroker操作を行わない",
)

__all__ = ["TRIAGE_POLICY"]
