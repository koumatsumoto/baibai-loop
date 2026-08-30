# Research Triage scenario bank

## Review Set with zero research entries

- **対象層** — Review Set確認、Research Triage、`research` 0件の正常完了
- **題材** — canonical Review Set全件について、調査質問を具体化できないため`skip`とする。Review Setを再生成せず、Research Triage v1を1回だけpublishする。
- **期待品質** — 全entryが`research / skip`のどちらかを持ち、`skip`のrationaleが具体的である。Research Setやhuman confirmationを捏造しない。

## Human admission remains pending

- **対象層** — `research` entryがある通常cycleのsession境界
- **題材** — Review Set全件を評価し、2件を`research`、残りを`skip`としてpublishする。
- **期待品質** — Research Setへの人間admissionを待つためoperation sessionをactiveに保つ。Research TriageだけでFundamental Researchや買付判断へ進まない。
