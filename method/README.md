# Production methodology

採用済みのdated規則をコードと一緒にGitでreview・配布する。18件（Screening 10件・Reading 8件）のうち既定は2件、残り16件は歴史revisionである。現在の選択先は[repository_layout.py](../engine/src/baibai_engine/foundation/repository_layout.py)、読取は[Screening loader](../engine/src/baibai_engine/screening/rule_config.py)と[Reading loader](../engine/src/baibai_engine/market_context/reading/rules.py)が所有する。別のcurrent copyやDBを作らない。

Screeningは`screening/rules/`、Readingは保存判断が引用する元の`macro/reading/`を維持する。Readingのfilename stemと、Screeningの規則内容・計算revisionによるidentityを物理pathと混同しない。歴史16件すべてが現loaderでの再実行に必須という意味ではないが、過去参照を一括削除しない。

意味を変える規則は明示PRで採用し、新revisionとして追加する。配置・説明の変更だけで過去判断が引用するpathや数値を変えない。reportからproductionを自動更新しない。

調査の実行順は[Research skill](../.agents/skills/research/SKILL.md)、経済的な問いは[Stock Assessment](../docs/reference/stock-assessment.md)、判断の基準は[doctrine](../docs/doctrine.md)にある。
