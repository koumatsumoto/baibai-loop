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
        "researchは全候補比較による1..Nのpriorityを持つ。Nはresearch件数で、"
        "research entryだけを重複・欠番なく再採番する。具体的なrationale、"
        "research_question、key_riskを返す。skipは具体的なrationaleだけを返し、"
        "priority、research_question、key_riskを必ずJSON nullにする"
    ),
    "出力直前にresearch priority集合がexactly 1..Nで重複・欠番なしと再計算してから返す",
    "unknownを否定事実へ変換せず、unknownだけでskipを強制しない",
    (
        "E[r]はvaluation reversionとcarryのsecondary machine return priorであり、"
        "高低、負値、欠損だけでresearch、skip、priorityを決めない"
    ),
    "Approach仮説とE[r]の一致は補強材料、不一致はResearchで解く問いとして扱える",
    (
        "ADVは実行可能性のsecondary contextであり、固定値未満または欠損だけで"
        "skipやpriorityを決めない"
    ),
    "Macro Contextは参考文脈であり、単独gateにしない",
    "買付判断とbroker操作を行わない",
)

__all__ = ["TRIAGE_POLICY"]
