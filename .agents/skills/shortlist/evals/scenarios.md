# Shortlist scenario bank

## Current run and zero-selection completion

- **対象層** — coverage確認、canonical previous、follow-up trigger、selected 0 の operation completion
- **題材** — 2026-08-09、ASOF 2026-08-07。pull済みstoreのcoverageは完全で、greatest prior as-of に複数 revision があり、application DB の canonical shortlist だけが一方を束縛する。runとselectを各1回実行し、全 entry を rejected とする。1 件は過去 event を財務へ反映済みで将来 estimated date があり、もう 1 件は次回日程が無い。read-only sandbox で実行コマンド、判断、follow-up、final payload、報告を生成する。
- **期待品質** — canonical shortlist の束縛先をpreviousとして明示し、runとselectを重複実行しない。消化済みの過去日 task を作らず、将来 estimated date または undated condition へ進む。selected 0 は `completion_reason: no-shortlist-selection` と `selected_count: 0` artifact で表し、`human_confirmation` を偽装しない。
- **判定** — 採用（2026-08-09、#869）。blind A/B で candidate が目的達成・scope・成果物品質・事故リスクの全軸で優位。
- **トレードオフ / 注記** — run と select は各1回に限定し、previous identityが曖昧な場合だけ明示IDで解決する。

## Selected candidates remain active

- **対象層** — selected がある通常 cycle の session 終端
- **題材** — pull済みstoreをcoverage確認し、runとselectを各1回実行して、Research Gate narrativeを持つselected 2件をpublishする標準cycle。
- **期待品質** — primary-research set の人間選択を待つため opportunity session を active に保つ。zero-selection completion を適用せず、追加の completion 儀式を持ち込まない。
- **判定** — 回帰題材として採用（2026-08-09、#869）。
